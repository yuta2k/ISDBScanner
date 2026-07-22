from isdb_scanner.catv.cas import _CalculateCRC32MPEG
from isdb_scanner.catv.mmt import (
    TLV_PACKET_TYPE_COMPRESSED_IP,
    TLV_PACKET_TYPE_SIGNALING,
    TLV_SYNC_BYTE,
    ExtractTLVCarrierGroupInfo,
    ExtractTLVStream,
    MMTAnalyzer,
)
from isdb_scanner.catv.tsmf import TLV_CELL_PID, TS_PACKET_SIZE, TS_SYNC_BYTE, TSMF_HEADER_PID, GetPID


def AppendCRC32(section_without_crc: bytes) -> bytes:
    """セクション本体 (CRC32 を除く) から CRC32 (CRC-32/MPEG-2) を計算し、末尾に付加する (test_cas.py と同じヘルパー)"""
    crc = _CalculateCRC32MPEG(section_without_crc)
    return section_without_crc + crc.to_bytes(4, byteorder='big')


def BuildTLVPacket(packet_type: int, payload: bytes) -> bytes:
    """テスト用に TLV パケット (sync + type + length + payload) を1個組み立てる"""
    return bytes([TLV_SYNC_BYTE, packet_type, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


def BuildTLVCells(data: bytes) -> bytes:
    """
    連結済みの TLV パケット列 (BuildTLVPacket で組み立てたものを結合したバイト列) を PID 0x002D のセル列に詰め直す
    ExtractTLVStream のテスト用: 最初のセルは開始セル (flag=0x40, pointer_field=0)、以降は継続セルとして分割する
    """
    cells = bytearray()
    offset = 0
    is_first = True
    while offset < len(data):
        packet = bytearray(TS_PACKET_SIZE)
        packet[0] = TS_SYNC_BYTE
        if is_first:
            chunk = data[offset : offset + 184]
            packet[1] = 0x40 | ((TLV_CELL_PID >> 8) & 0x1F)
            packet[2] = TLV_CELL_PID & 0xFF
            packet[3] = 0x00  # pointer_field = 0 (このセルの先頭から新しい TLV パケットが始まる)
            packet[4 : 4 + len(chunk)] = chunk
            offset += len(chunk)
            is_first = False
        else:
            chunk = data[offset : offset + 185]
            packet[1] = (TLV_CELL_PID >> 8) & 0x1F  # flag (0x40) をクリア = 継続セル
            packet[2] = TLV_CELL_PID & 0xFF
            packet[3 : 3 + len(chunk)] = chunk
            offset += len(chunk)
        cells += packet
    return bytes(cells)


def BuildAssetEntry(asset_type: bytes, *, location_type: int, location_body: bytes) -> bytes:
    """テスト用に MPT のアセットループ1件分を組み立てる (asset_id_scheme はダミーの全0固定)"""
    entry = bytearray()
    entry += bytes([0x00])  # identifier_type
    entry += bytes(4)  # asset_id_scheme (ダミー)
    entry += bytes([0x00])  # asset_id_length = 0 (asset_id なし)
    entry += asset_type  # asset_type (4バイト FourCC)
    entry += bytes([0x00])  # asset_clock_relation_flags
    entry += bytes([0x01])  # location_count = 1
    entry += bytes([location_type]) + location_body
    entry += bytes([0x00, 0x00])  # asset_descriptors_length = 0
    return bytes(entry)


def BuildMPTTable(package_id: int, assets: list[bytes]) -> bytes:
    """テスト用に MPT (table_id=0x20) を組み立てる"""
    body = bytearray()
    body += bytes([0x00])  # MPT_mode (下位2bit) + reserved
    body += bytes([0x01])  # package_id_length = 1
    body += bytes([package_id & 0xFF])
    body += bytes([0x00, 0x00])  # MPT_descriptors_length = 0
    body += bytes([len(assets)])  # number_of_assets
    for asset in assets:
        body += asset
    header = bytes([0x20, 0x01]) + len(body).to_bytes(2, byteorder='big')  # table_id + version + length
    return header + bytes(body)


def BuildSignallingTLVPacket(packet_id: int, message_body: bytes) -> bytes:
    """
    テスト用に、シグナリングメッセージ (payload_type=0x02) を運ぶ「ヘッダ圧縮 IP パケット」の TLV パケットを組み立てる
    message_body には MPT 等のテーブルをそのまま渡す (PA message の外側ヘッダは省略し、_ScanForMPT のスキャンで見つかる形にする)
    """
    mmtp_header = bytes([0x00, 0x02]) + packet_id.to_bytes(2, byteorder='big') + bytes(8)  # timestamp(4)+packet_sequence_number(4)
    fragmentation_header = bytes([0x00])  # fragmentation_indicator=0, length_extension_flag=0, aggregation_flag=0
    mmtp_payload = mmtp_header + fragmentation_header + message_body
    compressed_ip_payload = bytes([0x00, 0x01, 0x61]) + mmtp_payload  # context_id(2B) + header_type(0x61=圧縮済み)
    return BuildTLVPacket(TLV_PACKET_TYPE_COMPRESSED_IP, compressed_ip_payload)


def BuildTLVNITPacket(network_id: int, network_name: str, tlv_stream_ids: list[int]) -> bytes:
    """テスト用に TLV-NIT (table_id=0x40) を運ぶ TLV-SI パケットを組み立てる"""
    name_bytes = network_name.encode('utf-8')
    network_descriptors = bytes([0x40, len(name_bytes)]) + name_bytes  # ネットワーク名記述子 (tag=0x40, UTF-8)

    stream_loop = bytearray()
    for stream_id in tlv_stream_ids:
        stream_loop += stream_id.to_bytes(2, byteorder='big')  # tlv_stream_id
        stream_loop += bytes([0x00, 0x00])  # original_network_id (ダミー)
        stream_loop += bytes([0xF0, 0x00])  # reserved(4bit) + TLV_stream_descriptors_length(12bit)=0

    body = bytearray()
    body += network_id.to_bytes(2, byteorder='big')
    body += bytes([0xC1, 0x00, 0x00])  # reserved/version_number/current_next_indicator, section_number, last_section_number
    body += bytes([0xF0 | ((len(network_descriptors) >> 8) & 0x0F), len(network_descriptors) & 0xFF])
    body += network_descriptors
    body += bytes([0xF0 | ((len(stream_loop) >> 8) & 0x0F), len(stream_loop) & 0xFF])
    body += stream_loop

    section_length = len(body) + 4  # +4: CRC32
    section = bytes([0x40, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]) + bytes(body)
    section_with_crc = AppendCRC32(section)
    return BuildTLVPacket(TLV_PACKET_TYPE_SIGNALING, section_with_crc)


class TestExtractTLVStreamSynthetic:
    """合成データによる ExtractTLVStream の基本動作テスト (CI でも実行可能)"""

    def test_single_short_tlv_packet(self):
        packet = BuildTLVPacket(0xFF, b'\x00' * 10)
        cells = BuildTLVCells(packet)
        extracted = ExtractTLVStream(cells)
        assert extracted == packet

    def test_tlv_packet_spanning_multiple_cells(self):
        # 184+185 バイトを超える (継続セルが複数回発生する) 長さの TLV パケットが壊れず復元できること
        packet = BuildTLVPacket(0xFE, bytes(range(256)) * 3)
        cells = BuildTLVCells(packet)
        extracted = ExtractTLVStream(cells)
        assert extracted == packet

    def test_multiple_tlv_packets(self):
        packets = BuildTLVPacket(0x02, b'\x01' * 50) + BuildTLVPacket(0xFF, b'\x00' * 300)
        cells = BuildTLVCells(packets)
        extracted = ExtractTLVStream(cells)
        assert extracted == packets

    def test_no_tlv_cells_returns_empty(self):
        # PID 0x002D のセルが1つも無い場合は空バイト列を返す
        non_tlv_packet = bytearray(TS_PACKET_SIZE)
        non_tlv_packet[0] = TS_SYNC_BYTE
        non_tlv_packet[1] = 0x00
        non_tlv_packet[2] = 0x00
        assert ExtractTLVStream(bytes(non_tlv_packet)) == b''

    def test_ignores_non_tlv_pid_packets_interleaved(self):
        # PID 0x002D 以外のパケットが混在していても無視して正しく復元できること
        other_pid_packet = bytearray(TS_PACKET_SIZE)
        other_pid_packet[0] = TS_SYNC_BYTE
        other_pid_packet[1] = 0x00
        other_pid_packet[2] = 0x11
        packet = BuildTLVPacket(0xFF, b'\xab' * 20)
        cells = bytes(other_pid_packet) + BuildTLVCells(packet) + bytes(other_pid_packet)
        extracted = ExtractTLVStream(cells)
        assert extracted == packet
        assert GetPID(memoryview(cells)[0:TS_PACKET_SIZE]) == 0x0011

    def test_truncated_packet_is_excluded_and_collected(self):
        # 宣言長 1000 バイトのパケットの先頭 184 バイトだけを載せた開始セルの直後に、
        # 別のパケットの開始セル (pointer_field=0) が来た場合 (8K マルチキャリア分散伝送で頻発する打ち切り)、
        # 打ち切られたパケットは出力から除外され、truncated_packets に先頭部分が収集される
        truncated_source = BuildTLVPacket(0x03, b'\x55' * 1000)
        cells = bytearray(BuildTLVCells(truncated_source)[:TS_PACKET_SIZE])  # 先頭セル (184バイト分) だけを使う
        complete_packet = BuildTLVPacket(0xFF, b'\xaa' * 30)
        cells += BuildTLVCells(complete_packet)

        truncated_packets: list[bytes] = []
        extracted = ExtractTLVStream(bytes(cells), truncated_packets)

        assert extracted == complete_packet
        assert len(truncated_packets) == 1
        assert truncated_packets[0] == truncated_source[:184]

    def test_new_packet_starting_mid_cell_via_pointer_field(self):
        # 開始セルの pointer_field > 0 の場合、そのセルの先頭 pointer_field バイトは直前パケットの継続分になる
        packet_a = BuildTLVPacket(0xFF, b'\x11' * 200)  # 204 バイト (先頭セルに 184、次セルに 20 バイト)
        packet_b = BuildTLVPacket(0x02, b'\x22' * 100)  # 104 バイト
        cell1 = bytearray(TS_PACKET_SIZE)
        cell1[0] = TS_SYNC_BYTE
        cell1[1] = 0x40 | ((TLV_CELL_PID >> 8) & 0x1F)
        cell1[2] = TLV_CELL_PID & 0xFF
        cell1[3] = 0x00  # pointer_field = 0
        cell1[4:] = packet_a[:184]
        cell2 = bytearray(TS_PACKET_SIZE)
        cell2[0] = TS_SYNC_BYTE
        cell2[1] = 0x40 | ((TLV_CELL_PID >> 8) & 0x1F)
        cell2[2] = TLV_CELL_PID & 0xFF
        cell2[3] = 20  # pointer_field = 20 (packet_a の残り 20 バイトの直後から packet_b が始まる)
        cell2[4:24] = packet_a[184:]
        cell2[24 : 24 + len(packet_b)] = packet_b

        extracted = ExtractTLVStream(bytes(cell1 + cell2))
        assert extracted == packet_a + packet_b


def BuildTLVTSMFHeaderPacket(
    tlv_stream_id: int, network_id: int, group_id: int, group_carrier_count: int, group_carrier_index: int
) -> bytes:
    """テスト用に TLV キャリアの TSMF 多重フレームヘッダパケット (PID 0x002F) を生成する"""
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = 0x40 | ((TSMF_HEADER_PID >> 8) & 0x1F)
    packet[2] = TSMF_HEADER_PID & 0xFF
    packet[3] = 0x10
    frame_sync = 0x1A86
    packet[4] = (frame_sync >> 8) & 0x1F
    packet[5] = frame_sync & 0xFF
    packet[9] = (tlv_stream_id >> 8) & 0xFF
    packet[10] = tlv_stream_id & 0xFF
    packet[11] = (network_id >> 8) & 0xFF
    packet[12] = network_id & 0xFF
    packet[127] = group_id
    packet[128] = group_carrier_count
    packet[129] = group_carrier_index
    return bytes(packet)


class TestExtractTLVCarrierGroupInfoSynthetic:
    """合成データによる ExtractTLVCarrierGroupInfo のテスト (CI でも実行可能)"""

    def test_extracts_group_info_from_tsmf_header(self):
        stream = BuildTLVTSMFHeaderPacket(0xB0E0, 0x000B, 8, 3, 2) * 3
        carrier_group = ExtractTLVCarrierGroupInfo(stream)
        assert carrier_group is not None
        assert carrier_group.tlv_stream_id == 0xB0E0
        assert carrier_group.network_id == 0x000B
        assert carrier_group.group_id == 8
        assert carrier_group.group_carrier_count == 3
        assert carrier_group.group_carrier_index == 2

    def test_majority_vote_against_corrupted_header(self):
        # ビット化けしたヘッダが混ざっていても、最頻値が採用される
        good = BuildTLVTSMFHeaderPacket(0xB110, 0x000B, 5, 1, 1)
        corrupted = BuildTLVTSMFHeaderPacket(0xFFFF, 0x000B, 5, 1, 1)
        carrier_group = ExtractTLVCarrierGroupInfo(good * 3 + corrupted)
        assert carrier_group is not None
        assert carrier_group.tlv_stream_id == 0xB110
        assert carrier_group.group_carrier_count == 1

    def test_no_tsmf_header_returns_none(self):
        assert ExtractTLVCarrierGroupInfo(b'\x00' * TS_PACKET_SIZE) is None


class TestMMTAnalyzerSynthetic:
    """合成データによる MMTAnalyzer の基本動作テスト (CI でも実行可能)"""

    def test_tlv_nit_network_info(self):
        packet = BuildTLVNITPacket(network_id=11, network_name='テスト用ネットワーク', tlv_stream_ids=[0xB070, 0xB071])
        tlv_stream = ExtractTLVStream(BuildTLVCells(packet))
        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert mmt_info.network is not None
        assert mmt_info.network.network_id == 11
        assert mmt_info.network.network_name == 'テスト用ネットワーク'
        assert mmt_info.network.tlv_stream_ids == [0xB070, 0xB071]

    def test_single_service_no_multi_carrier(self):
        # location_type=0x00 (同一データフロー) のみのアセット構成では is_multi_carrier_partial は False
        video_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF100).to_bytes(2, 'big'))
        audio_asset = BuildAssetEntry(b'mp4a', location_type=0x00, location_body=(0xF110).to_bytes(2, 'big'))
        mpt = BuildMPTTable(package_id=0x65, assets=[video_asset, audio_asset])
        packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=mpt)
        tlv_stream = ExtractTLVStream(BuildTLVCells(packet))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert len(mmt_info.services) == 1
        service = mmt_info.services[0]
        assert service.package_id == 0x65
        assert [asset.asset_type for asset in service.assets] == ['hev1', 'mp4a']
        assert service.assets[0].packet_id == 0xF100
        assert service.assets[1].packet_id == 0xF110
        assert mmt_info.is_multi_carrier_partial is False
        assert mmt_info.external_references == []

    def test_multi_carrier_partial_detection_via_location_type_3(self):
        # location_type=0x03 (別放送網参照) のアセットを含む場合、8K のようなマルチキャリア分散伝送とみなす
        local_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF100).to_bytes(2, 'big'))
        external_location_body = (0x0011).to_bytes(2, 'big') + (0xB071).to_bytes(2, 'big') + bytes([0x00, 0x00])
        external_asset = BuildAssetEntry(b'aapp', location_type=0x03, location_body=external_location_body)
        mpt = BuildMPTTable(package_id=0x66, assets=[local_asset, external_asset])
        packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=mpt)
        tlv_stream = ExtractTLVStream(BuildTLVCells(packet))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert len(mmt_info.services) == 1
        service = mmt_info.services[0]
        assert service.assets[1].external_network_id == 0x0011
        assert service.assets[1].external_tsid == 0xB071
        assert mmt_info.is_multi_carrier_partial is True
        assert len(mmt_info.external_references) == 1
        assert mmt_info.external_references[0].network_id == 0x0011
        assert mmt_info.external_references[0].transport_stream_id == 0xB071

    def test_no_tlv_data_returns_empty_info(self):
        mmt_info = MMTAnalyzer().analyze(b'')
        assert mmt_info.network is None
        assert mmt_info.services == []
        assert mmt_info.is_multi_carrier_partial is False


class TestMMTAnalyzerTruncatedSalvage:
    """打ち切られたシグナリングパケット (8K マルチキャリア分散伝送で常態的に発生する) からの MPT 部分解析のテスト"""

    def test_truncated_mpt_salvage(self):
        # 宣言長より短く打ち切られた MPT を含むシグナリングパケットから、先頭アセットだけでもサルベージできること
        video_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF100).to_bytes(2, 'big'))
        audio_asset = BuildAssetEntry(b'mp4a', location_type=0x00, location_body=(0xF110).to_bytes(2, 'big'))
        mpt = BuildMPTTable(package_id=0x66, assets=[video_asset, audio_asset])
        packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=mpt)

        # TLV パケットの末尾 15 バイトを打ち切る (2番目のアセット (17バイト) の途中で切れ、先頭アセットは無傷で残る)
        truncated = packet[: len(packet) - 15]
        mmt_info = MMTAnalyzer().analyze(b'', truncated_packets=[truncated])

        assert len(mmt_info.services) == 1
        service = mmt_info.services[0]
        assert service.package_id == 0x66
        assert service.assets[0].asset_type == 'hev1'
        assert service.assets[0].packet_id == 0xF100

    def test_multi_carrier_detection_via_carrier_group(self):
        # TSMF ヘッダ由来のキャリアグループ情報 (count >= 2) だけでもマルチキャリア分散伝送と判定されること
        from isdb_scanner.catv.constants import TLVCarrierGroupInfo

        carrier_group = TLVCarrierGroupInfo(
            tlv_stream_id=0xB0E0, network_id=0x000B, group_id=1, group_carrier_count=3, group_carrier_index=1
        )
        mmt_info = MMTAnalyzer().analyze(b'', carrier_group=carrier_group)
        assert mmt_info.is_multi_carrier_partial is True
        assert mmt_info.carrier_group == carrier_group

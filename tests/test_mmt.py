from isdb_scanner.catv.cas import _CalculateCRC32MPEG
from isdb_scanner.catv.constants import (
    CarrierType,
    CATVCarrierInfo,
    CATVMMTInfo,
    MMTAssetInfo,
    MMTSDTServiceInfo,
    MMTServiceInfo,
    TLVCarrierGroupInfo,
    TLVNetworkInfo,
    TLVStreamEntryInfo,
)
from isdb_scanner.catv.mmt import (
    MH_SDT_TABLE_ID_ACTUAL,
    MH_SDT_TABLE_ID_OTHER,
    MMT_SI_PACKET_ID_MH_SDT,
    TLV_PACKET_TYPE_COMPRESSED_IP,
    TLV_PACKET_TYPE_SIGNALING,
    TLV_SYNC_BYTE,
    ExtractTLVCarrierGroupInfo,
    ExtractTLVStream,
    MergeMultiCarrierGroupMMTInfo,
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


def BuildSignallingTLVPacket(
    packet_id: int,
    message_body: bytes,
    *,
    fragmentation_indicator: int = 0,
    fragment_counter: int = 0,
    packet_sequence_number: int = 0,
) -> bytes:
    """
    テスト用に、シグナリングメッセージ (payload_type=0x02) を運ぶ「ヘッダ圧縮 IP パケット」の TLV パケットを組み立てる
    message_body には MPT 等のテーブルをそのまま渡す (PA message の外側ヘッダは省略し、_ScanForMPT のスキャンで見つかる形にする)
    シグナリングメッセージのペイロードヘッダは 2 バイト固定 (フラグ群 + fragment_counter) で、
    fragmentation_indicator を指定すればメッセージを複数パケットに分割したケースも組み立てられる
    """
    mmtp_header = (
        bytes([0x00, 0x02])
        + packet_id.to_bytes(2, byteorder='big')
        + bytes(4)  # timestamp
        + packet_sequence_number.to_bytes(4, byteorder='big')
    )
    # 1バイト目: fragmentation_indicator(2bit) + reserved(4bit) + length_extension_flag(1bit) + aggregation_flag(1bit)
    signalling_header = bytes([(fragmentation_indicator & 0x03) << 6, fragment_counter & 0xFF])
    mmtp_payload = mmtp_header + signalling_header + message_body
    compressed_ip_payload = bytes([0x00, 0x01, 0x61]) + mmtp_payload  # context_id(2B) + header_type(0x61=圧縮済み)
    return BuildTLVPacket(TLV_PACKET_TYPE_COMPRESSED_IP, compressed_ip_payload)


def BuildMHServiceDescriptor(service_type: int, provider_name: str, service_name: str) -> bytes:
    """テスト用に MH-サービス記述子 (tag=0x8019, MMT-SI なので tag は 16bit) を組み立てる (文字符号は BOM 無し UTF-8)"""
    provider_name_bytes = provider_name.encode('utf-8')
    service_name_bytes = service_name.encode('utf-8')
    body = bytes([service_type, len(provider_name_bytes)]) + provider_name_bytes + bytes([len(service_name_bytes)]) + service_name_bytes
    return bytes([0x80, 0x19, len(body)]) + body


def BuildMHSDTSection(
    table_id: int,
    tlv_stream_id: int,
    original_network_id: int,
    services: list[tuple[int, bytes, bool]],
) -> bytes:
    """
    テスト用に MH-SDT セクション (table_id=0x9F: 自ストリーム / 0xA0: 他ストリーム) を組み立てる
    services には (service_id, 記述子ループのバイト列, is_free) のタプルを渡す
    """
    service_loop = bytearray()
    for service_id, descriptors, is_free in services:
        service_loop += service_id.to_bytes(2, byteorder='big')
        service_loop += bytes([0xFF])  # reserved_future_use(3bit) + EIT フラグ群(5bit)
        free_ca_mode = 0 if is_free else 1
        # running_status(3bit)=4 (動作中) + free_CA_mode(1bit) + descriptors_loop_length(12bit)
        service_loop += bytes([(4 << 5) | (free_ca_mode << 4) | ((len(descriptors) >> 8) & 0x0F), len(descriptors) & 0xFF])
        service_loop += descriptors

    body = bytearray()
    body += tlv_stream_id.to_bytes(2, byteorder='big')
    body += bytes([0xC1, 0x00, 0x00])  # reserved/version_number/current_next_indicator, section_number, last_section_number
    body += original_network_id.to_bytes(2, byteorder='big')
    body += bytes([0xFF])  # reserved_future_use
    body += service_loop

    section_length = len(body) + 4  # +4: CRC32
    section = bytes([table_id, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]) + bytes(body)
    return AppendCRC32(section)


def BuildM2SectionMessage(section: bytes) -> bytes:
    """テスト用に M2 セクションメッセージ (message_id=0x8000) を組み立てる (message_id(2B) + version(1B) + length(2B) + セクション)"""
    return bytes([0x80, 0x00, 0x00]) + len(section).to_bytes(2, byteorder='big') + section


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


class TestMHSDTSynthetic:
    """合成データによる MH-SDT (ARIB STD-B60) 解析のテスト (実在の放送局名や実受信環境の値は一切使わない)"""

    @staticmethod
    def _BuildMHSDTPacket(table_id: int, services: list[tuple[int, bytes, bool]], tlv_stream_id: int = 0xB070) -> bytes:
        """MH-SDT セクションを M2 セクションメッセージに包み、packet_id=0x8004 のシグナリング TLV パケットにして返す"""
        section = BuildMHSDTSection(table_id, tlv_stream_id, original_network_id=0x000B, services=services)
        return BuildSignallingTLVPacket(MMT_SI_PACKET_ID_MH_SDT, BuildM2SectionMessage(section))

    def test_service_name_resolved_from_actual_stream_sdt(self):
        # 自ストリーム (0x9F) の MH-SDT の service_id と MPT の package_id が一致する場合、MPT サービスにサービス名が反映される
        video_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF100).to_bytes(2, 'big'))
        mpt_packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=BuildMPTTable(package_id=0x65, assets=[video_asset]))
        descriptors = BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ')
        sdt_packet = self._BuildMHSDTPacket(MH_SDT_TABLE_ID_ACTUAL, [(0x65, descriptors, True)])
        tlv_stream = ExtractTLVStream(BuildTLVCells(mpt_packet + sdt_packet))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert len(mmt_info.services) == 1
        service = mmt_info.services[0]
        assert service.package_id == 0x65
        assert service.service_id == 0x65
        assert service.service_name == 'テスト４Ｋ'
        assert [asset.asset_type for asset in service.assets] == ['hev1']

        assert len(mmt_info.sdt_services) == 1
        sdt_service = mmt_info.sdt_services[0]
        assert sdt_service.service_id == 0x65
        assert sdt_service.service_name == 'テスト４Ｋ'
        assert sdt_service.service_provider_name == 'テスト事業者'
        assert sdt_service.service_type == 0x01
        assert sdt_service.is_free is True
        assert sdt_service.running_status == 4
        assert sdt_service.on_current_stream is True

    def test_other_stream_sdt_listed_but_not_merged_into_services(self):
        # 他ストリーム (0xA0) の MH-SDT のサービスは sdt_services にのみ載り、MPT のサービス一覧には追加されない
        video_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF100).to_bytes(2, 'big'))
        mpt_packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=BuildMPTTable(package_id=0x65, assets=[video_asset]))
        own_packet = self._BuildMHSDTPacket(
            MH_SDT_TABLE_ID_ACTUAL, [(0x65, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ'), True)]
        )
        other_packet = self._BuildMHSDTPacket(
            MH_SDT_TABLE_ID_OTHER,
            [
                (0x67, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト８Ｋ'), False),
                (0x66, BuildMHServiceDescriptor(0x01, '', '　'), True),
            ],
            tlv_stream_id=0xB071,
        )
        tlv_stream = ExtractTLVStream(BuildTLVCells(mpt_packet + own_packet + other_packet))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert [service.package_id for service in mmt_info.services] == [0x65]
        # sdt_services は自ストリーム/他ストリームを統合し、service_id 昇順で並ぶ
        assert [sdt_service.service_id for sdt_service in mmt_info.sdt_services] == [0x65, 0x66, 0x67]
        assert [sdt_service.on_current_stream for sdt_service in mmt_info.sdt_services] == [True, False, False]
        # 未サービスイン枠 (サービス名が全角空白・有料扱い) も除外せずそのまま出力する
        assert mmt_info.sdt_services[1].service_name == '　'
        assert mmt_info.sdt_services[2].service_name == 'テスト８Ｋ'
        assert mmt_info.sdt_services[2].is_free is False

    def test_service_without_mpt_is_added_with_empty_assets(self):
        # MPT が取得できず自ストリームの MH-SDT だけが取得できた場合 (8K マルチキャリア分散伝送で発生しうる)、
        # アセット一覧が空のサービスとして補完される
        descriptors = BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト８Ｋ')
        sdt_packet = self._BuildMHSDTPacket(MH_SDT_TABLE_ID_ACTUAL, [(0x67, descriptors, True)])
        tlv_stream = ExtractTLVStream(BuildTLVCells(sdt_packet))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert len(mmt_info.services) == 1
        assert mmt_info.services[0].package_id == 0x67
        assert mmt_info.services[0].service_id == 0x67
        assert mmt_info.services[0].service_name == 'テスト８Ｋ'
        assert mmt_info.services[0].assets == []

    def test_section_with_invalid_crc32_is_rejected(self):
        # CRC32 が壊れている MH-SDT セクションは棄却され、サービス名も反映されない
        video_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF100).to_bytes(2, 'big'))
        mpt_packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=BuildMPTTable(package_id=0x65, assets=[video_asset]))
        section = BuildMHSDTSection(
            MH_SDT_TABLE_ID_ACTUAL,
            0xB070,
            original_network_id=0x000B,
            services=[(0x65, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ'), True)],
        )
        corrupted_section = section[:-1] + bytes([section[-1] ^ 0xFF])  # CRC32 の最終バイトを壊す
        sdt_packet = BuildSignallingTLVPacket(MMT_SI_PACKET_ID_MH_SDT, BuildM2SectionMessage(corrupted_section))
        tlv_stream = ExtractTLVStream(BuildTLVCells(mpt_packet + sdt_packet))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert mmt_info.sdt_services == []
        assert len(mmt_info.services) == 1
        assert mmt_info.services[0].service_name == 'Unknown'
        assert mmt_info.services[0].service_id is None

    def test_fragmented_sdt_is_reassembled(self):
        # 複数の MMTP パケットに分割 (fragmentation) された MH-SDT が再組み立てされること
        section = BuildMHSDTSection(
            MH_SDT_TABLE_ID_ACTUAL,
            0xB070,
            original_network_id=0x000B,
            services=[
                (0x65, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ'), True),
                (0x66, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ第二'), True),
            ],
        )
        message = BuildM2SectionMessage(section)
        split1 = len(message) // 3
        split2 = split1 * 2
        packets = (
            BuildSignallingTLVPacket(
                MMT_SI_PACKET_ID_MH_SDT, message[:split1], fragmentation_indicator=1, fragment_counter=2, packet_sequence_number=10
            )
            + BuildSignallingTLVPacket(
                MMT_SI_PACKET_ID_MH_SDT,
                message[split1:split2],
                fragmentation_indicator=2,
                fragment_counter=1,
                packet_sequence_number=11,
            )
            + BuildSignallingTLVPacket(
                MMT_SI_PACKET_ID_MH_SDT, message[split2:], fragmentation_indicator=3, fragment_counter=0, packet_sequence_number=12
            )
        )
        tlv_stream = ExtractTLVStream(BuildTLVCells(packets))

        mmt_info = MMTAnalyzer().analyze(tlv_stream)

        assert [sdt_service.service_name for sdt_service in mmt_info.sdt_services] == ['テスト４Ｋ', 'テスト４Ｋ第二']

    def test_fragment_with_sequence_number_gap_is_discarded(self):
        # 途中のフラグメントが欠落している (packet_sequence_number が飛んでいる) 場合は再組み立てを破棄する
        section = BuildMHSDTSection(
            MH_SDT_TABLE_ID_ACTUAL,
            0xB070,
            original_network_id=0x000B,
            services=[(0x65, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ'), True)],
        )
        message = BuildM2SectionMessage(section)
        split = len(message) // 2
        packets = BuildSignallingTLVPacket(
            MMT_SI_PACKET_ID_MH_SDT, message[:split], fragmentation_indicator=1, packet_sequence_number=10
        ) + BuildSignallingTLVPacket(MMT_SI_PACKET_ID_MH_SDT, message[split:], fragmentation_indicator=3, packet_sequence_number=12)
        tlv_stream = ExtractTLVStream(BuildTLVCells(packets))

        assert MMTAnalyzer().analyze(tlv_stream).sdt_services == []

    def test_non_sdt_packet_id_is_not_parsed_as_sdt(self):
        # MH-SDT 用の packet_id (0x8004) 以外で運ばれてきた M2 セクションメッセージは MH-SDT として扱わない
        section = BuildMHSDTSection(
            MH_SDT_TABLE_ID_ACTUAL,
            0xB070,
            original_network_id=0x000B,
            services=[(0x65, BuildMHServiceDescriptor(0x01, 'テスト事業者', 'テスト４Ｋ'), True)],
        )
        packet = BuildSignallingTLVPacket(packet_id=0x8005, message_body=BuildM2SectionMessage(section))
        tlv_stream = ExtractTLVStream(BuildTLVCells(packet))

        assert MMTAnalyzer().analyze(tlv_stream).sdt_services == []


class TestMergeMultiCarrierGroupMMTInfo:
    """
    8K マルチキャリア分散伝送のグループ内マージ (MergeMultiCarrierGroupMMTInfo) のテスト
    合成データのみを使い、実在の放送局名や実受信環境の ID 値は一切使わない
    """

    @staticmethod
    def _BuildCarrier(
        physical_channel: str,
        *,
        group_carrier_index: int | None = 1,
        group_carrier_count: int = 3,
        tlv_stream_id: int = 0x1001,
        network_id: int = 0x0AAA,
        group_id: int = 0x07,
        services: list[MMTServiceInfo] | None = None,
        sdt_services: list[MMTSDTServiceInfo] | None = None,
        network: TLVNetworkInfo | None = None,
    ) -> CATVCarrierInfo:
        """テスト用の TLV キャリア 1 本分の解析結果を組み立てる (group_carrier_index=None なら carrier_group なし)"""
        carrier_group = (
            None
            if group_carrier_index is None
            else TLVCarrierGroupInfo(
                tlv_stream_id=tlv_stream_id,
                network_id=network_id,
                group_id=group_id,
                group_carrier_count=group_carrier_count,
                group_carrier_index=group_carrier_index,
            )
        )
        return CATVCarrierInfo(
            physical_channel=physical_channel,
            carrier_type=CarrierType.TLV,
            mmt=CATVMMTInfo(
                network=network,
                services=services or [],
                sdt_services=sdt_services or [],
                carrier_group=carrier_group,
                is_multi_carrier_partial=group_carrier_count >= 2,
            ),
        )

    @staticmethod
    def _BuildSDTService(service_id: int, service_name: str = 'Unknown', *, on_current_stream: bool = False) -> MMTSDTServiceInfo:
        return MMTSDTServiceInfo(service_id=service_id, service_name=service_name, on_current_stream=on_current_stream)

    def test_services_and_sdt_services_are_merged_across_group_carriers(self):
        # 同一グループの 3 キャリアに部分的にしか届いていないサービス情報が、マージ後は全キャリアで同一の和集合になる
        carriers = [
            self._BuildCarrier(
                'CATV_C40',
                group_carrier_index=1,
                services=[MMTServiceInfo(package_id=0x1101, service_id=0x1101, service_name='テスト８Ｋ')],
                sdt_services=[self._BuildSDTService(0x1101, 'テスト８Ｋ', on_current_stream=True)],
            ),
            self._BuildCarrier(
                'CATV_C41',
                group_carrier_index=2,
                services=[],
                sdt_services=[self._BuildSDTService(0x1102, 'テスト４Ｋ')],
            ),
            self._BuildCarrier(
                'CATV_C42',
                group_carrier_index=3,
                services=[MMTServiceInfo(package_id=0x1103)],
                sdt_services=[self._BuildSDTService(0x1103, 'テスト４Ｋ２')],
            ),
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)

        for carrier in carriers:
            assert carrier.mmt is not None
            # package_id / service_id 昇順で、グループ内の全サービスが載っている
            assert [service.package_id for service in carrier.mmt.services] == [0x1101, 0x1103]
            assert [sdt_service.service_id for sdt_service in carrier.mmt.sdt_services] == [0x1101, 0x1102, 0x1103]
            assert [sdt_service.service_name for sdt_service in carrier.mmt.sdt_services] == ['テスト８Ｋ', 'テスト４Ｋ', 'テスト４Ｋ２']

    def test_service_with_more_assets_wins_and_service_name_is_complemented(self):
        # 同一 package_id はアセット数が多い方 (= より完全な MPT) を採用しつつ、サービス ID / 名は埋まっている方から補完する
        assets = [MMTAssetInfo(asset_type='hev1', packet_id=0xF100), MMTAssetInfo(asset_type='mp4a', packet_id=0xF101)]
        carriers = [
            # MPT は届いたが自ストリームの MH-SDT が届かず、サービス名が不明なキャリア
            self._BuildCarrier('CATV_C40', group_carrier_index=1, services=[MMTServiceInfo(package_id=0x1101, assets=assets)]),
            # MPT は届かず MH-SDT からサービス名だけ判明したキャリア (アセット一覧は空)
            self._BuildCarrier(
                'CATV_C41',
                group_carrier_index=2,
                services=[MMTServiceInfo(package_id=0x1101, service_id=0x1101, service_name='テスト８Ｋ')],
                sdt_services=[self._BuildSDTService(0x1101, 'テスト８Ｋ', on_current_stream=True)],
            ),
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)

        for carrier in carriers:
            assert carrier.mmt is not None
            assert len(carrier.mmt.services) == 1
            service = carrier.mmt.services[0]
            assert [asset.asset_type for asset in service.assets] == ['hev1', 'mp4a']
            assert service.service_id == 0x1101
            assert service.service_name == 'テスト８Ｋ'

    def test_sdt_service_prefers_current_stream_then_named_entry(self):
        # MH-SDT のサービスは自ストリーム (0x9F) 由来 → サービス名が判明している方、の順で優先される
        carriers = [
            self._BuildCarrier(
                'CATV_C40',
                group_carrier_index=1,
                sdt_services=[
                    self._BuildSDTService(0x1101, 'テスト８Ｋ（他ストリーム）'),
                    self._BuildSDTService(0x1102),
                ],
            ),
            self._BuildCarrier(
                'CATV_C41',
                group_carrier_index=2,
                sdt_services=[
                    self._BuildSDTService(0x1101, 'テスト８Ｋ', on_current_stream=True),
                    self._BuildSDTService(0x1102, 'テスト４Ｋ'),
                ],
            ),
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)

        for carrier in carriers:
            assert carrier.mmt is not None
            assert [sdt_service.service_name for sdt_service in carrier.mmt.sdt_services] == ['テスト８Ｋ', 'テスト４Ｋ']
            assert [sdt_service.on_current_stream for sdt_service in carrier.mmt.sdt_services] == [True, False]

    def test_network_info_is_complemented_only_when_missing(self):
        # TLV-NIT はグループ内で最も情報量の多いものを、取得できなかったキャリアにのみ補完する
        rich_network = TLVNetworkInfo(
            network_id=0x0AAA,
            network_name='テストネットワーク',
            tlv_stream_ids=[0x1001],
            streams=[TLVStreamEntryInfo(tlv_stream_id=0x1001, service_ids=[0x1101], frequencies_hz=[100_000_000, 200_000_000])],
        )
        poor_network = TLVNetworkInfo(network_id=0x0AAA, network_name='テストネットワーク（部分）')
        carriers = [
            self._BuildCarrier('CATV_C40', group_carrier_index=1, network=None),
            self._BuildCarrier('CATV_C41', group_carrier_index=2, network=poor_network),
            self._BuildCarrier('CATV_C42', group_carrier_index=3, network=rich_network),
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)

        # 取得できていなかったキャリアには最も情報量の多い TLV-NIT が補完される
        assert carriers[0].mmt is not None and carriers[0].mmt.network is not None
        assert carriers[0].mmt.network.network_name == 'テストネットワーク'
        assert len(carriers[0].mmt.network.streams) == 1
        # すでに取得できているキャリアの TLV-NIT は上書きしない
        assert carriers[1].mmt is not None and carriers[1].mmt.network is not None
        assert carriers[1].mmt.network.network_name == 'テストネットワーク（部分）'

    def test_merged_services_are_independent_instances(self):
        # マージ結果はキャリアごとに独立したコピーであり、片方への変更が他方に波及しない
        carriers = [
            self._BuildCarrier(
                'CATV_C40',
                group_carrier_index=1,
                services=[MMTServiceInfo(package_id=0x1101, service_name='テスト８Ｋ')],
                sdt_services=[self._BuildSDTService(0x1101, 'テスト８Ｋ', on_current_stream=True)],
            ),
            self._BuildCarrier('CATV_C41', group_carrier_index=2),
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)

        assert carriers[0].mmt is not None and carriers[1].mmt is not None
        assert carriers[0].mmt.services[0] is not carriers[1].mmt.services[0]
        assert carriers[0].mmt.sdt_services[0] is not carriers[1].mmt.sdt_services[0]
        carriers[0].mmt.services[0].service_name = '書き換え'
        assert carriers[1].mmt.services[0].service_name == 'テスト８Ｋ'

    def test_single_carrier_and_other_group_are_not_affected(self):
        # 単独キャリア (group_carrier_count=1) や別グループのキャリアはマージ対象にならない
        single_carrier = self._BuildCarrier(
            'CATV_C30',
            group_carrier_index=1,
            group_carrier_count=1,
            tlv_stream_id=0x1000,
            services=[MMTServiceInfo(package_id=0x1001, service_name='テスト４Ｋ')],
            sdt_services=[self._BuildSDTService(0x1001, 'テスト４Ｋ', on_current_stream=True)],
        )
        other_group_carriers = [
            self._BuildCarrier(
                'CATV_C50',
                group_carrier_index=1,
                tlv_stream_id=0x1002,
                group_id=0x08,
                services=[MMTServiceInfo(package_id=0x1201, service_name='別グループ８Ｋ')],
            ),
            self._BuildCarrier('CATV_C51', group_carrier_index=2, tlv_stream_id=0x1002, group_id=0x08),
        ]
        carriers = [
            single_carrier,
            self._BuildCarrier('CATV_C40', group_carrier_index=1, services=[MMTServiceInfo(package_id=0x1101)]),
            self._BuildCarrier('CATV_C41', group_carrier_index=2, services=[MMTServiceInfo(package_id=0x1102)]),
            *other_group_carriers,
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)

        # 単独キャリアの内容は一切変わらない
        assert single_carrier.mmt is not None
        assert [service.package_id for service in single_carrier.mmt.services] == [0x1001]
        assert [sdt_service.service_id for sdt_service in single_carrier.mmt.sdt_services] == [0x1001]
        # 別グループのキャリア同士でのみマージされ、他グループのサービスは混ざらない
        assert carriers[1].mmt is not None and carriers[2].mmt is not None
        assert [service.package_id for service in carriers[1].mmt.services] == [0x1101, 0x1102]
        assert [service.package_id for service in carriers[2].mmt.services] == [0x1101, 0x1102]
        for carrier in other_group_carriers:
            assert carrier.mmt is not None
            assert [service.package_id for service in carrier.mmt.services] == [0x1201]

    def test_carrier_without_carrier_group_or_mmt_is_ignored(self):
        # TSMF ヘッダから carrier_group を取得できなかった TLV キャリアや、TLV でない (mmt=None の) キャリアは対象外
        no_group_carrier = self._BuildCarrier(
            'CATV_C40', group_carrier_index=None, services=[MMTServiceInfo(package_id=0x1101, service_name='テスト８Ｋ')]
        )
        group_carriers = [
            self._BuildCarrier('CATV_C41', group_carrier_index=1, services=[MMTServiceInfo(package_id=0x1102)]),
            self._BuildCarrier('CATV_C42', group_carrier_index=2, services=[MMTServiceInfo(package_id=0x1103)]),
        ]
        tsmf_carrier = CATVCarrierInfo(physical_channel='CATV_15', carrier_type=CarrierType.TSMF)
        carriers = [no_group_carrier, *group_carriers, tsmf_carrier]

        MergeMultiCarrierGroupMMTInfo(carriers)

        assert no_group_carrier.mmt is not None
        assert [service.package_id for service in no_group_carrier.mmt.services] == [0x1101]
        for carrier in group_carriers:
            assert carrier.mmt is not None
            assert [service.package_id for service in carrier.mmt.services] == [0x1102, 0x1103]
        assert tsmf_carrier.mmt is None

    def test_group_with_only_one_scanned_carrier_is_unchanged(self):
        # グループの一員だが他のキャリアがスキャン対象に含まれていない場合 (--channels で一部のみスキャンした場合など) は何もしない
        carrier = self._BuildCarrier(
            'CATV_C40',
            group_carrier_index=2,
            services=[MMTServiceInfo(package_id=0x1101, service_name='テスト８Ｋ')],
            sdt_services=[self._BuildSDTService(0x1101, 'テスト８Ｋ', on_current_stream=True)],
        )
        services_before = [service.model_copy(deep=True) for service in carrier.mmt.services] if carrier.mmt else []

        MergeMultiCarrierGroupMMTInfo([carrier])

        assert carrier.mmt is not None
        assert carrier.mmt.services == services_before

    def test_merge_is_idempotent(self):
        # マージ済みの結果を再度マージしても内容は変わらない (--from-json 再フォーマット時に二重適用されても安全)
        carriers = [
            self._BuildCarrier(
                'CATV_C40',
                group_carrier_index=1,
                services=[MMTServiceInfo(package_id=0x1101, assets=[MMTAssetInfo(asset_type='hev1', packet_id=0xF100)])],
                sdt_services=[self._BuildSDTService(0x1101, 'テスト８Ｋ', on_current_stream=True)],
            ),
            self._BuildCarrier(
                'CATV_C41',
                group_carrier_index=2,
                services=[MMTServiceInfo(package_id=0x1101, service_id=0x1101, service_name='テスト８Ｋ')],
                sdt_services=[self._BuildSDTService(0x1102, 'テスト４Ｋ')],
            ),
        ]

        MergeMultiCarrierGroupMMTInfo(carriers)
        merged = [carrier.model_copy(deep=True) for carrier in carriers]
        MergeMultiCarrierGroupMMTInfo(carriers)

        assert carriers == merged

from __future__ import annotations

from collections.abc import Iterator

from isdb_scanner.catv.cas import _ParseDescriptors, _VerifySectionCRC32
from isdb_scanner.catv.constants import (
    CATVCarrierInfo,
    CATVMMTInfo,
    MMTAssetInfo,
    MMTExternalReferenceInfo,
    MMTSDTServiceInfo,
    MMTServiceInfo,
    TLVCarrierGroupInfo,
    TLVNetworkInfo,
    TLVStreamEntryInfo,
)
from isdb_scanner.catv.tsmf import (
    TLV_CELL_PID,
    TS_PACKET_SIZE,
    TS_SYNC_BYTE,
    TSMF_FRAME_SYNC_WORDS,
    TSMF_HEADER_PID,
    GetPID,
)


# TLV パケットの同期バイト (MPEG-2 TS の同期バイト 0x47 とは異なる)
TLV_SYNC_BYTE = 0x7F

# TLV パケットの type (ARIB STD-B60)
TLV_PACKET_TYPE_COMPRESSED_IP = 0x03  # ヘッダ圧縮された IP パケット (この中に MMTP パケットが入っている)
TLV_PACKET_TYPE_SIGNALING = 0xFE  # TLV-SI (TLV-NIT など)

# TLV-SI (type=0xFE) の table_id
TLV_NIT_TABLE_ID = 0x40  # TLV-NIT (自ネットワーク)
# ネットワーク名記述子のタグ値 (実データでは本文が UTF-8 で符号化されている。地上波/BS の NIT が使う ARIB 8単位符号とは異なる)
_NETWORK_NAME_DESCRIPTOR_TAG = 0x40
# サービスリスト記述子のタグ値 (DVB/ARIB 標準の service_list_descriptor と同一構造: service_id(2B) + service_type(1B) の繰り返し)
_SERVICE_LIST_DESCRIPTOR_TAG = 0x41
# 周波数リスト記述子のタグ値 (JLabs SPEC-034 相当の CATV トランスモジュレーション独自記述子と推定。12バイトのエントリの繰り返しで、
# 各エントリは周波数 (BCD 8桁, XXXX.XXXX MHz) + FEC/変調方式/シンボルレート等 + グループ番号。実データでは 8K サービスのエントリに
# 伝送に使う全キャリア分の周波数が列挙されていることを確認しており、これがマルチキャリア分散伝送のグループ通知になっている)
_FREQUENCY_LIST_DESCRIPTOR_TAG = 0xF3
# 周波数リスト記述子の1エントリのバイト長
_FREQUENCY_LIST_ENTRY_LENGTH = 12

# MPT (MMT Package Table) の table_id
MMT_TABLE_ID_MPT = 0x20

# MH-SDT が伝送される MMTP パケットの packet_id (ARIB STD-B60 表4-12 でシグナリング用に固定割り当てされている)
MMT_SI_PACKET_ID_MH_SDT = 0x8004

# M2 セクションメッセージの message_id (ARIB STD-B60 表6-4)
# MH-SDT を含む MMT-SI のセクション形式テーブルは、このメッセージに 1 セクションずつ格納されて伝送される
# (message_id ごとに length フィールドの幅が固定されており、M2 セクションメッセージでは 16bit)
MMT_MESSAGE_ID_M2_SECTION = 0x8000

# MH-SDT (ARIB STD-B60 表7-23) の table_id
MH_SDT_TABLE_ID_ACTUAL = 0x9F  # 自ストリーム (このキャリアで伝送されている TLV ストリーム自身のサービス)
MH_SDT_TABLE_ID_OTHER = 0xA0  # 他ストリーム (同一放送網内の他の TLV ストリームのサービス)

# MH-サービス記述子のタグ値 (ARIB STD-B60 表7-69)
# MMT-SI の記述子タグは MPEG-2 PSI/SI と異なり 16bit であることに注意
# 本体は service_type(8) + service_provider_name_length(8) + service_provider_name + service_name_length(8) + service_name で、
# 文字符号は BOM 無し UTF-8 (STD-B60 7.2.2。地上波/BS の SDT が使う ARIB 8単位符号ではない)
_MH_SERVICE_DESCRIPTOR_TAG = 0x8019

# ヘッダ圧縮 IP パケットの header_type (実データから確認した値)
# 0x20/0x21: フルヘッダパケット (コンテキスト確立。IPv6 ヘッダ 40B + UDP ヘッダ 8B を含む)
# 0x60/0x61: 圧縮済みヘッダパケット (差分なし、追加バイトなし)
_COMPRESSED_IP_HEADER_TYPE_FULL = (0x20, 0x21)
_COMPRESSED_IP_HEADER_TYPE_COMPRESSED = (0x60, 0x61)

# MMTP payload_type (ARIB STD-B60)
MMT_PAYLOAD_TYPE_SIGNALING_MESSAGE = 0x02

# MMT_general_location_info() の location_type ごとの固定バイト長
# 0x00 (同一データフロー) と 0x03 (別放送網) は 4K/8K キャリアの実データで構造を確認済み
# 0x01 (IPv4) / 0x02 (IPv6) は ARIB STD-B60 の一般的な構造からの推定であり、手元の実データでは未検証
_MMT_LOCATION_FIXED_LENGTH: dict[int, int] = {
    0x00: 2,  # packet_id
    0x01: 12,  # ipv4_src_addr(4) + ipv4_dst_addr(4) + dst_port(2) + packet_id(2) (未検証)
    0x02: 36,  # ipv6_src_addr(16) + ipv6_dst_addr(16) + dst_port(2) + packet_id(2) (未検証)
    0x03: 6,  # network_id(2) + MMT_general_location_info (transport_stream_id(2) + packet_id(13bit, 上位3bitは予約))
}

# MPT 解析時の壊れたデータ検出用の安全弁 (これを超える値が出た場合は壊れたデータとみなして打ち切る)
_MAX_PACKAGE_ID_LENGTH = 8
_MAX_ASSET_ID_LENGTH = 32
_MAX_LOCATION_COUNT = 16
_MAX_ASSET_COUNT = 60


# pointer_field の最大有効値
# 184 は「このセルの 184 バイトすべてが直前パケットの継続で、新しいパケットは次のセル先頭から始まる」ことを示す
# (実データで pointer_field=184 のセルを少数確認済み。185 以上は不正値として扱う)
_MAX_POINTER_FIELD = 184


def ExtractTLVStream(ts_stream: bytes | bytearray, truncated_packets: list[bytes] | None = None) -> bytes:
    """
    TLV (4K/8K MMT 放送) セル (PID 0x002D) から元の TLV ストリームを再構成する

    セルヘッダは通常の MPEG-2 TS ヘッダとは異なり continuity_counter を持たない特殊な構造になっている
      - 開始セル (byte1 の 0x40 ビットが立っている): byte3 が pointer_field、ペイロードは byte4 から184バイト
      - 継続セル (0x40 ビットが立っていない): pointer_field は無く、ペイロードは byte3 から185バイト
    継続セルも byte4 からのペイロードと誤認すると、継続セル1個につき1バイトずつペイロードが欠落し、
    MPT 等のテーブルのアセットループが2バイトずれて壊れてしまうため、この判定を最優先で正しく行う必要がある

    pointer_field は DVB の PSI セクションと同じ「このセル内で新しい TLV パケットが始まる位置」の宣言であり、
    再構成もこの宣言に厳密に従う (セルペイロードを全連結してから宣言長で読み進める単純な方式もあり得るが、
    8K マルチキャリア分散伝送のキャリアでは 1 キャリアに元ストリームの一部スライスしか載っておらず、パケットが宣言長より
    短く打ち切られることがある。全連結方式ではこの打ち切り境界を跨いで後続の無関係なバイトをパケット内容として
    取り込んでしまい、MPT 等が静かに壊れる):
      - 開始セルに到達したら、それまでに完成しなかった再構成中のパケットは「打ち切られた」ものとして出力から除外する
        (truncated_packets が指定されていれば、そこに先頭部分だけのパケットとして収集する。8K キャリアの部分 MPT 解析用)
      - pointer_field の位置から新しいパケット列の読み取りを開始する
      - 読み取り中に同期バイト (0x7F) が現れなくなった場合 (パケット完了直後に別スライスの無関係なバイトが続く場合など) は、
        次の開始セルまで読み捨てて再同期する
    完全な (打ち切りのない) ストリームに対しては、この方式は全連結方式と完全に同一のバイト列を出力する
    (4K キャリアの実データ全量で、事前抽出済みの参照 TLV ストリームとバイト単位一致することを確認済み)

    Args:
        ts_stream (bytes | bytearray): 188 バイト境界に整列済みの TS ストリーム (TLV キャリアの受信データ)
        truncated_packets (list[bytes] | None): 打ち切られたパケットの先頭部分を収集するリスト (省略可)

    Returns:
        bytes: 再構成された TLV ストリーム (PID 0x002D のセルが1つも無ければ空バイト列)
    """

    view = memoryview(ts_stream)
    output = bytearray()
    # 現在再構成中のパケット列 (先頭は常に TLV パケットの先頭に揃っている)
    pending = bytearray()
    # pending の内容が信頼できる (開始セルの pointer_field で同期済みの) 状態か
    synced = False

    def EmitCompletePackets() -> bool:
        """pending の先頭から完成しているパケットを output へ移す。先頭が同期バイトでない (壊れている) 場合は False を返す"""
        nonlocal pending, output
        position = 0
        intact = True
        while position + 4 <= len(pending):
            if pending[position] != TLV_SYNC_BYTE:
                intact = False
                break
            packet_length = (pending[position + 2] << 8) | pending[position + 3]
            if position + 4 + packet_length > len(pending):
                break
            output += pending[position : position + 4 + packet_length]
            position += 4 + packet_length
        del pending[:position]
        return intact

    for offset in range(0, len(ts_stream) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = view[offset : offset + TS_PACKET_SIZE]
        if packet[0] != TS_SYNC_BYTE:
            continue
        if GetPID(packet) != TLV_CELL_PID:
            continue
        if packet[1] & 0x40:
            # 開始セル: byte3 が pointer_field、ペイロードは byte4 から184バイト
            pointer_field = packet[3]
            if pointer_field > _MAX_POINTER_FIELD:
                # 不正な pointer_field: このセルは信頼できないため同期を破棄する
                pending.clear()
                synced = False
                continue
            if synced:
                # pointer_field より前のバイトは直前パケットの継続分
                pending += packet[4 : 4 + pointer_field]
                EmitCompletePackets()
                if pending:
                    # ここで新しいパケットが始まると宣言されているのに前のパケットが完成していない = 打ち切られた
                    # (8K マルチキャリア分散伝送では正常時でも頻発する。先頭部分だけでも部分解析に使えるよう収集する)
                    if truncated_packets is not None and pending[0] == TLV_SYNC_BYTE:
                        truncated_packets.append(bytes(pending))
                    pending.clear()
            pending = bytearray(packet[4 + pointer_field : TS_PACKET_SIZE])
            synced = True
            if not EmitCompletePackets():
                pending.clear()
                synced = False
        else:
            # 継続セル: pointer_field は無く、ペイロードは byte3 から185バイト
            if not synced:
                continue
            pending += packet[3:TS_PACKET_SIZE]
            if not EmitCompletePackets():
                # パケット完了直後に同期バイト以外が続いた = 別スライスの無関係なバイトが混入している
                # 次の開始セルまで読み捨てて再同期する
                pending.clear()
                synced = False

    # ストリーム終端: 完成している分だけ出力する (未完の残りは打ち切り扱い)
    EmitCompletePackets()
    if truncated_packets is not None and pending and pending[0] == TLV_SYNC_BYTE:
        truncated_packets.append(bytes(pending))

    return bytes(output)


# TSMF 多重フレームヘッダ (PID 0x002F) 内の、TLV キャリア識別・キャリアグループ情報のオフセット (TS パケット先頭からのバイト位置)
# JCTEA STD-002 の相対TS毎の情報領域と、JLabs SPEC-034 (複数QAM変調方式) の拡張領域と推定される
# 手元の複数の 4K/8K キャリア実データの全 TSMF ヘッダで 100% 一貫した値になることを確認済み:
#   - [9:11] 自キャリアの TLV ストリーム ID (TLV-NIT の tlv_stream_id と一致)
#   - [11:13] ネットワーク ID (TLV-NIT の network_id と一致)
#   - [127] キャリアグループ番号 (TLV-NIT の周波数リスト記述子のグループ番号と一致)
#   - [128] グループを構成するキャリア数 (4K=1 / 8K=3)
#   - [129] 自キャリアのグループ内番号 (1始まり。8K の 3 キャリアでそれぞれ 1/2/3)
_TSMF_HEADER_TLV_STREAM_ID_OFFSET = 9
_TSMF_HEADER_NETWORK_ID_OFFSET = 11
_TSMF_HEADER_GROUP_ID_OFFSET = 127
_TSMF_HEADER_GROUP_COUNT_OFFSET = 128
_TSMF_HEADER_GROUP_INDEX_OFFSET = 129


def ExtractTLVCarrierGroupInfo(ts_stream: bytes | bytearray) -> TLVCarrierGroupInfo | None:
    """
    TLV キャリアの TSMF 多重フレームヘッダ (PID 0x002F) から、自キャリアの TLV ストリーム ID・ネットワーク ID と
    キャリアグループ (複数 QAM 分散伝送) 情報を取得する

    8K マルチキャリア分散伝送のキャリアでは TLV データ自体が部分的にしか受信できず、TLV-NIT の取得も安定しないため、
    常に完全な形で受信できる TSMF ヘッダのこの情報がグループ検出の最も確実な手段になる
    ヘッダは繰り返し送信されるため、全ヘッダから最頻値を採用する (ビット化けによる誤検出への耐性)

    Args:
        ts_stream (bytes | bytearray): 188 バイト境界に整列済みの TS ストリーム (TLV キャリアの受信データ)

    Returns:
        TLVCarrierGroupInfo | None: 取得したキャリア情報 (有効な TSMF ヘッダが1つも無ければ None)
    """

    view = memoryview(ts_stream)
    candidates: dict[tuple[int, int, int, int, int], int] = {}
    for offset in range(0, len(ts_stream) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = view[offset : offset + TS_PACKET_SIZE]
        if packet[0] != TS_SYNC_BYTE:
            continue
        if GetPID(packet) != TSMF_HEADER_PID:
            continue
        frame_sync = ((packet[4] & 0x1F) << 8) | packet[5]
        if frame_sync not in TSMF_FRAME_SYNC_WORDS:
            continue
        key = (
            (packet[_TSMF_HEADER_TLV_STREAM_ID_OFFSET] << 8) | packet[_TSMF_HEADER_TLV_STREAM_ID_OFFSET + 1],
            (packet[_TSMF_HEADER_NETWORK_ID_OFFSET] << 8) | packet[_TSMF_HEADER_NETWORK_ID_OFFSET + 1],
            packet[_TSMF_HEADER_GROUP_ID_OFFSET],
            packet[_TSMF_HEADER_GROUP_COUNT_OFFSET],
            packet[_TSMF_HEADER_GROUP_INDEX_OFFSET],
        )
        candidates[key] = candidates.get(key, 0) + 1

    if not candidates:
        return None
    tlv_stream_id, network_id, group_id, group_carrier_count, group_carrier_index = max(candidates, key=lambda k: candidates[k])
    return TLVCarrierGroupInfo(
        tlv_stream_id=tlv_stream_id,
        network_id=network_id,
        group_id=group_id,
        group_carrier_count=group_carrier_count,
        group_carrier_index=group_carrier_index,
    )


def _IterTLVPackets(tlv_stream: bytes) -> Iterator[tuple[int, bytes]]:
    """再構成済みの TLV ストリームから (type, payload) を順に取り出す"""

    position = 0
    length = len(tlv_stream)
    while position + 4 <= length:
        if tlv_stream[position] != TLV_SYNC_BYTE:
            break
        packet_type = tlv_stream[position + 1]
        packet_length = (tlv_stream[position + 2] << 8) | tlv_stream[position + 3]
        if position + 4 + packet_length > length:
            break
        yield packet_type, tlv_stream[position + 4 : position + 4 + packet_length]
        position += 4 + packet_length


def _ParseCompressedIPPacket(payload: bytes) -> tuple[int, int, bytes] | None:
    """
    ヘッダ圧縮 IP パケット (TLV type=0x03) から MMTP パケットを取り出し、(payload_type, packet_id, mmtp_bytes) を返す
    mmtp_bytes は MMTP ヘッダの先頭 (version/flags バイト) から TLV パケットの末尾までのバイト列
    """

    if len(payload) < 4:
        return None
    header_type = payload[2]
    if header_type in _COMPRESSED_IP_HEADER_TYPE_FULL:
        mmtp_start = 3 + 40 + 8  # context_id(2B) + header_type(1B) + IPv6ヘッダ(40B) + UDPヘッダ(8B)
    elif header_type in _COMPRESSED_IP_HEADER_TYPE_COMPRESSED:
        mmtp_start = 3  # context_id(2B) + header_type(1B) の直後から MMTP パケットが始まる
    else:
        # 未知の header_type: 実データでは確認できていないため安全側に倒して読み飛ばす
        return None
    if mmtp_start + 4 > len(payload):
        return None

    b0 = payload[mmtp_start]
    if (b0 >> 6) != 0:
        # MMTP の version は常に 0 のはずなので、そうでなければ誤検出とみなす
        return None
    b1 = payload[mmtp_start + 1]
    payload_type = b1 & 0x3F
    packet_id = (payload[mmtp_start + 2] << 8) | payload[mmtp_start + 3]
    return payload_type, packet_id, payload[mmtp_start:]


def _MMTPPayloadOffset(mmtp_bytes: bytes) -> int | None:
    """MMTP パケットの固定ヘッダ (12バイト) + 可変長フィールドを読み飛ばし、ペイロードの開始オフセットを返す"""

    if len(mmtp_bytes) < 12:
        return None
    b0 = mmtp_bytes[0]
    # version(2bit) + packet_counter_flag(1bit) + FEC_type(2bit) + reserved(1bit) + extension_flag(1bit) + RAP_flag(1bit)
    # + payload_type(6bit, 2バイト目) + packet_id(2B) + timestamp(4B) + packet_sequence_number(4B) = 12バイト
    offset = 12
    if b0 & 0x20:  # packet_counter_flag
        offset += 4
    if b0 & 0x02:  # extension_flag
        if offset + 4 > len(mmtp_bytes):
            return None
        extension_length = (mmtp_bytes[offset + 2] << 8) | mmtp_bytes[offset + 3]
        offset += 4 + extension_length
    if offset > len(mmtp_bytes):
        return None
    return offset


def _IterAggregatedMessages(data: bytes, *, length_extension_flag: bool) -> Iterator[bytes]:
    """aggregation_flag が立っているシグナリングメッセージから、連結された各メッセージ本体を取り出す"""

    length_field_size = 4 if length_extension_flag else 2
    offset = 0
    while offset + length_field_size <= len(data):
        message_length = int.from_bytes(data[offset : offset + length_field_size], byteorder='big')
        offset += length_field_size
        if message_length == 0 or offset + message_length > len(data):
            break
        yield data[offset : offset + message_length]
        offset += message_length


def _DecodeFourCC(data: bytes) -> str:
    """4バイトの FourCC (asset_id_scheme / asset_type) を文字列に変換する (印字不能なバイト列は16進文字列にフォールバック)"""

    try:
        text = data.decode('ascii')
    except UnicodeDecodeError:
        return data.hex()
    return text if text.isprintable() else data.hex()


def _ParseTLVNIT(payload: bytes) -> TLVNetworkInfo | None:
    """
    TLV-NIT (table_id=0x40, 自ネットワーク) を解析する
    MPEG-2/ARIB の NIT (transport_stream_id が tlv_stream_id に置き換わったもの) とほぼ同じ構造を持つ
    ネットワーク名記述子 (tag=0x40) の本文は、地上波/BS の NIT が使う ARIB 8単位符号ではなく UTF-8 で符号化されている
    (実データで日本語のネットワーク名が UTF-8 の文字列として得られることを確認済み)
    """

    try:
        if len(payload) < 10 or payload[0] != TLV_NIT_TABLE_ID:
            return None
        section_length = (((payload[1] & 0x0F) << 8) | payload[2]) + 3
        if section_length > len(payload) or not _VerifySectionCRC32(payload[:section_length]):
            return None

        network_id = (payload[3] << 8) | payload[4]
        network_descriptors_length = ((payload[8] & 0x0F) << 8) | payload[9]
        descriptors_end = 10 + network_descriptors_length
        if descriptors_end > section_length:
            return None

        network_name = 'Unknown'
        for tag, body in _ParseDescriptors(payload[10:descriptors_end]):
            if tag == _NETWORK_NAME_DESCRIPTOR_TAG:
                try:
                    network_name = body.decode('utf-8')
                except UnicodeDecodeError:
                    network_name = body.hex()
                break

        offset = descriptors_end
        streams: list[TLVStreamEntryInfo] = []
        if offset + 2 <= section_length:
            tlv_stream_loop_length = ((payload[offset] & 0x0F) << 8) | payload[offset + 1]
            offset += 2
            # 末尾4バイトは CRC32 なので、それより手前までを TLV ストリームループとして走査する
            loop_end = min(offset + tlv_stream_loop_length, section_length - 4)
            while offset + 6 <= loop_end:
                tlv_stream_id = (payload[offset] << 8) | payload[offset + 1]
                descriptors_length = ((payload[offset + 4] & 0x0F) << 8) | payload[offset + 5]
                stream_descriptors = payload[offset + 6 : offset + 6 + descriptors_length]
                offset += 6 + descriptors_length

                service_ids: list[int] = []
                frequencies_hz: list[int] = []
                for tag, body in _ParseDescriptors(stream_descriptors):
                    if tag == _SERVICE_LIST_DESCRIPTOR_TAG:
                        # サービスリスト記述子: service_id(2B) + service_type(1B) の繰り返し
                        for entry_offset in range(0, len(body) - 2, 3):
                            service_ids.append((body[entry_offset] << 8) | body[entry_offset + 1])
                    elif tag == _FREQUENCY_LIST_DESCRIPTOR_TAG:
                        # 周波数リスト記述子: 先頭4バイトが BCD 8桁の周波数 (XXXX.XXXX MHz) の 12 バイトエントリの繰り返し
                        # 8K 分散伝送のサービスにはここに複数の周波数 (伝送に使う全キャリア) が列挙される
                        for entry_offset in range(0, len(body) - _FREQUENCY_LIST_ENTRY_LENGTH + 1, _FREQUENCY_LIST_ENTRY_LENGTH):
                            bcd_digits = body[entry_offset : entry_offset + 4].hex()
                            if bcd_digits.isdigit():
                                frequencies_hz.append(int(bcd_digits) * 100)  # 0.0001 MHz = 100 Hz 単位
                streams.append(TLVStreamEntryInfo(tlv_stream_id=tlv_stream_id, service_ids=service_ids, frequencies_hz=frequencies_hz))

        return TLVNetworkInfo(
            network_id=network_id,
            network_name=network_name,
            tlv_stream_ids=[stream.tlv_stream_id for stream in streams],
            streams=streams,
        )
    except IndexError:
        return None


def _TryParseMPTAsset(buf: bytes, offset: int, end: int, *, allow_truncated: bool = False) -> tuple[MMTAssetInfo | None, int]:
    """
    MPT のアセットループ1件分を解析する
    壊れたデータ (実データでは特に8Kキャリアで、セル欠落等によりアセットループが途中から壊れるケースを確認している)
    に対して安全に打ち切れるよう、各フィールドの妥当性を都度チェックする。解析に失敗した場合は (None, offset) を返す

    allow_truncated=True の場合、アセットの asset_descriptors がバッファ末尾で切れていても、
    アセット種別と location までは解析済みのためアセット自体は有効として返す (打ち切られた MPT のサルベージ用)
    """

    if offset + 10 > end:
        return None, offset
    offset += 1  # identifier_type (現時点では未使用)
    asset_id_scheme = _DecodeFourCC(buf[offset : offset + 4])
    offset += 4
    asset_id_length = buf[offset]
    offset += 1
    # +6: asset_type(4B) + asset_clock_relation_flags(1B) + location_count(1B) までバッファ内に収まっている必要がある
    if asset_id_length > _MAX_ASSET_ID_LENGTH or offset + asset_id_length + 6 > end:
        return None, offset
    offset += asset_id_length  # asset_id 自体は現時点では使わない
    asset_type = _DecodeFourCC(buf[offset : offset + 4])
    offset += 4
    offset += 1  # asset_clock_relation_flags (現時点では未使用)
    location_count = buf[offset]
    offset += 1
    if location_count > _MAX_LOCATION_COUNT:
        return None, offset

    packet_id: int | None = None
    external_network_id: int | None = None
    external_tsid: int | None = None
    for _ in range(location_count):
        if offset >= end:
            return None, offset
        location_type = buf[offset]
        offset += 1
        location_length = _MMT_LOCATION_FIXED_LENGTH.get(location_type)
        if location_length is None or offset + location_length > end:
            # 未知の location_type、または残りバッファに収まらない場合は、以降のオフセットを信頼できないため打ち切る
            return None, offset
        location_body = buf[offset : offset + location_length]
        offset += location_length
        if location_type == 0x00:
            packet_id = (location_body[0] << 8) | location_body[1]
        elif location_type == 0x03:
            # 別放送網参照 (8K マルチキャリア分散伝送で使われる): network_id(2B) + transport_stream_id(2B) + ...
            external_network_id = (location_body[0] << 8) | location_body[1]
            external_tsid = (location_body[2] << 8) | location_body[3]

    asset_info = MMTAssetInfo(
        asset_id_scheme=asset_id_scheme,
        asset_type=asset_type,
        packet_id=packet_id,
        external_network_id=external_network_id,
        external_tsid=external_tsid,
    )

    if offset + 2 > end:
        # asset_descriptors_length フィールド自体が欠けている: 打ち切りサルベージ時のみアセットとしては有効とする
        return (asset_info, end) if allow_truncated else (None, offset)
    asset_descriptors_length = (buf[offset] << 8) | buf[offset + 1]
    offset += 2
    if offset + asset_descriptors_length > end:
        # asset_descriptors がバッファ末尾で切れている: アセット種別と location までは解析済みのため、
        # 打ち切りサルベージ時はこのアセットまでを有効とする (offset=end を返し、後続のアセット解析は打ち切られる)
        return (asset_info, end) if allow_truncated else (None, offset)
    offset += asset_descriptors_length

    return asset_info, offset


def _TryParseMPT(buf: bytes, start: int, *, allow_truncated: bool = False) -> MMTServiceInfo | None:
    """
    buf[start] を table_id (0x20) とみなして MPT (MMT Package Table) を解析する
    アセットループの途中で解析に失敗した場合は、そこまでに得られたアセットのみを持つ MMTServiceInfo を返す
    1つもアセットを解析できなかった場合は None を返す

    allow_truncated=True の場合、テーブルの宣言長がバッファ末尾を超えていても (= 打ち切られた MPT でも)、
    バッファに残っている範囲でアセットを解析する (8K マルチキャリア分散伝送では 1 キャリアに MPT の先頭部分しか
    載らないことが常態のため、部分解析でも先頭のアセット構成と package_id は取得できる)
    """

    try:
        if buf[start] != MMT_TABLE_ID_MPT:
            return None
        table_length = (buf[start + 2] << 8) | buf[start + 3]
        end = start + 4 + table_length
        if end > len(buf):
            if not allow_truncated:
                return None
            # 打ち切られたテーブル: バッファに残っている範囲で解析する
            end = len(buf)

        offset = start + 4
        if offset + 2 > end:
            return None
        offset += 1  # MPT_mode (下位2bit) + reserved (現時点では未使用)
        package_id_length = buf[offset]
        offset += 1
        if package_id_length > _MAX_PACKAGE_ID_LENGTH or offset + package_id_length > end:
            return None
        package_id = int.from_bytes(buf[offset : offset + package_id_length], byteorder='big') if package_id_length > 0 else 0
        offset += package_id_length

        if offset + 2 > end:
            return None
        mpt_descriptors_length = (buf[offset] << 8) | buf[offset + 1]
        offset += 2
        if offset + mpt_descriptors_length > end:
            return None
        offset += mpt_descriptors_length  # MPT_descriptors (CA記述子等) の中身は現時点では使わない

        if offset + 1 > end:
            return None
        number_of_assets = buf[offset]
        offset += 1
        if number_of_assets < 1 or number_of_assets > _MAX_ASSET_COUNT:
            return None

        assets: list[MMTAssetInfo] = []
        for _ in range(number_of_assets):
            asset_info, offset = _TryParseMPTAsset(buf, offset, end, allow_truncated=allow_truncated)
            if asset_info is None:
                break
            assets.append(asset_info)

        if not assets:
            return None
        return MMTServiceInfo(package_id=package_id, service_name='Unknown', assets=assets)
    except IndexError:
        return None


def _ScanForMPT(buf: bytes, *, allow_truncated: bool = False) -> Iterator[MMTServiceInfo]:
    """
    シグナリングメッセージ本体から MPT (table_id=0x20) を探して解析する

    PA message (message_id=0x0000) の外側のヘッダ (number_of_tables 等) の正確なビット単位の構造は実データからは
    特定できなかった。そのため、MPT 自体は ARIB STD-B60 表 7-6 に基づき正しく構造化して解析しつつ、その開始位置の探索は
    「table_id (0x20) らしきバイトを探し、続く length フィールドと実際のアセットループが矛盾なく解析できるか」
    で検証するスキャン方式を採用している (妥当性チェック自体は _TryParseMPT / _TryParseMPTAsset で行う)
    """

    index = 0
    while index < len(buf) - 4:
        if buf[index] != MMT_TABLE_ID_MPT:
            index += 1
            continue
        service_info = _TryParseMPT(buf, index, allow_truncated=allow_truncated)
        if service_info is None:
            index += 1
            continue
        yield service_info
        # 正しく解析できたテーブルの内部を再スキャンしない (テーブル本文中の偶然の 0x20 を別の MPT と誤検出しないようにする)
        table_length = (buf[index + 2] << 8) | buf[index + 3]
        index = min(index + 4 + table_length, len(buf))


def _ParseMMTSIDescriptors(data: bytes) -> list[tuple[int, bytes]] | None:
    """
    MMT-SI の記述子ループを (descriptor_tag, body) のリストに分解する
    MPEG-2 PSI/SI の記述子と異なり descriptor_tag が 16bit であることに注意 (descriptor_length は tag <= 0xEFFF なら 8bit)
    宣言長がループ全体とぴったり合わない場合は壊れたデータとみなして None を返す
    """

    descriptors: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 3 <= len(data):
        descriptor_tag = (data[offset] << 8) | data[offset + 1]
        descriptor_length = data[offset + 2]
        body = data[offset + 3 : offset + 3 + descriptor_length]
        if len(body) != descriptor_length:
            return None
        descriptors.append((descriptor_tag, body))
        offset += 3 + descriptor_length
    return descriptors if offset == len(data) else None


def _ExtractM2Section(message: bytes) -> bytes | None:
    """
    シグナリングメッセージ本体から M2 セクションメッセージ (message_id=0x8000) のセクション部分を取り出す
    構造は message_id(16bit) + version(8bit) + length(16bit) + セクション1個 で、length は残り全バイト数と一致する
    M2 セクションメッセージでない場合や長さが矛盾する場合は None を返す
    """

    if len(message) < 5 or int.from_bytes(message[0:2], byteorder='big') != MMT_MESSAGE_ID_M2_SECTION:
        return None
    length = int.from_bytes(message[3:5], byteorder='big')
    section = message[5 : 5 + length]
    if len(section) != length:
        return None
    return section


def _ParseMHSDT(section: bytes) -> list[MMTSDTServiceInfo] | None:
    """
    MH-SDT セクション (ARIB STD-B60 表7-23) を解析し、記載されているサービスの一覧を返す
    セクション構造は MPEG-2 の SDT とほぼ同じ (transport_stream_id が tlv_stream_id に置き換わっている) で、
    末尾の CRC_32 も MPEG-2 PSI と同一のため、既存の _VerifySectionCRC32 でそのまま検証できる
    サービス名は記述子ループ内の MH-サービス記述子 (tag=0x8019) から取得する

    table_id が MH-SDT でない場合や、宣言長の矛盾・CRC32 不一致があった場合は None を返す
    """

    if len(section) < 16 or section[0] not in (MH_SDT_TABLE_ID_ACTUAL, MH_SDT_TABLE_ID_OTHER):
        return None
    section_length = (((section[1] & 0x0F) << 8) | section[2]) + 3
    if section_length != len(section) or not _VerifySectionCRC32(section):
        return None

    on_current_stream = section[0] == MH_SDT_TABLE_ID_ACTUAL
    services: list[MMTSDTServiceInfo] = []
    # 0-2: table_id/section_length, 3-4: tlv_stream_id, 5: version_number 等, 6: section_number, 7: last_section_number,
    # 8-9: original_network_id, 10: reserved_future_use → 11 からサービスループが始まる
    offset = 11
    loop_end = len(section) - 4  # 末尾4バイトは CRC_32
    while offset + 5 <= loop_end:
        service_id = (section[offset] << 8) | section[offset + 1]
        # offset + 2: reserved_future_use(3bit) + EIT フラグ群(5bit) (現時点では未使用)
        running_status = (section[offset + 3] >> 5) & 0x07
        free_ca_mode = (section[offset + 3] >> 4) & 0x01
        descriptors_loop_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
        offset += 5
        descriptors_bytes = section[offset : offset + descriptors_loop_length]
        if len(descriptors_bytes) != descriptors_loop_length:
            return None
        offset += descriptors_loop_length

        service_info = MMTSDTServiceInfo(
            service_id=service_id,
            is_free=not free_ca_mode,
            running_status=running_status,
            on_current_stream=on_current_stream,
        )
        for descriptor_tag, body in _ParseMMTSIDescriptors(descriptors_bytes) or []:
            if descriptor_tag != _MH_SERVICE_DESCRIPTOR_TAG or len(body) < 2:
                continue
            service_info.service_type = body[0]
            position = 1
            provider_name_length = body[position]
            position += 1
            service_info.service_provider_name = body[position : position + provider_name_length].decode('utf-8', errors='replace')
            position += provider_name_length
            if position < len(body):
                service_name_length = body[position]
                position += 1
                service_info.service_name = body[position : position + service_name_length].decode('utf-8', errors='replace')
        services.append(service_info)

    return services


def _ExtractSignallingMessageParts(payload: bytes) -> tuple[int, int, int, bool, bool, bytes] | None:
    """
    ヘッダ圧縮 IP パケット (TLV type=0x03) のペイロードからシグナリングメッセージの構成要素を取り出す
    戻り値: (packet_id, packet_sequence_number, fragmentation_indicator, length_extension_flag, aggregation_flag, body)
    シグナリングメッセージでない場合や解析できない場合は None を返す

    シグナリングメッセージのペイロードヘッダ (ARIB STD-B60 表6-3) は 2 バイト固定:
      1バイト目: fragmentation_indicator(2bit) + reserved(4bit) + length_extension_flag(1bit) + aggregation_flag(1bit)
      2バイト目: fragment_counter(8bit)
    body はこの 2 バイトを除いたメッセージ本体 (分割されている場合はその断片) になる
    """

    parsed = _ParseCompressedIPPacket(payload)
    if parsed is None:
        return None
    payload_type, packet_id, mmtp_bytes = parsed
    if payload_type != MMT_PAYLOAD_TYPE_SIGNALING_MESSAGE:
        return None

    payload_offset = _MMTPPayloadOffset(mmtp_bytes)
    if payload_offset is None or payload_offset + 2 > len(mmtp_bytes):
        return None
    # packet_sequence_number は MMTP 固定ヘッダのオフセット 8-11 (fragmentation 再組み立て時の連続性チェックに使う)
    packet_sequence_number = int.from_bytes(mmtp_bytes[8:12], byteorder='big')
    signalling_payload = mmtp_bytes[payload_offset:]

    header_byte = signalling_payload[0]
    fragmentation_indicator = (header_byte >> 6) & 0x3
    length_extension_flag = bool((header_byte >> 1) & 0x1)
    aggregation_flag = bool(header_byte & 0x1)
    return packet_id, packet_sequence_number, fragmentation_indicator, length_extension_flag, aggregation_flag, signalling_payload[2:]


class MMTAnalyzer:
    """
    TLV ストリーム (ExtractTLVStream の出力) を解析し、TLV-NIT (ネットワーク情報)・MPT (サービス/アセット一覧)・
    MH-SDT (サービス名) を取り出すクラス

    8K マルチキャリア分散伝送 (JLabs SPEC-034 相当) の検出は次の3系統で行う (いずれかに該当すれば is_multi_carrier_partial)
      1. TSMF 多重フレームヘッダのキャリアグループ情報 (ExtractTLVCarrierGroupInfo で取得し carrier_group 引数で渡す)
         — 常に完全な形で受信できるため最も確実。実データで 8K の 3 キャリアが (同一グループ, 3キャリア, 番号1/2/3) となる
      2. TLV-NIT のストリームループで、自キャリアのストリーム/サービスを含むエントリに複数の伝送周波数が列挙されている場合
         (実データで 8K サービスのエントリに伝送に使う 3 キャリア分の周波数が列挙されていることを確認済み。
          ただし 8K キャリア自身では TLV データが部分的にしか受信できず TLV-NIT を完全に取得できないことが多い)
      3. MPT の location_type=0x03 (別放送網参照) アセット — ARIB STD-B60 上の正規の手段だが、
         手元の 8K キャリア実データでは全アセットが location_type=0x00 でありこの参照は使われていなかった

    MH-SDT (サービス名) は、シグナリング用に固定割り当てされた packet_id=0x8004 (ARIB STD-B60 表4-12) の MMTP パケットに
    M2 セクションメッセージ (message_id=0x8000) として載っているため、この packet_id に絞った専用経路で決め打ち解析している
    (MPT のように内容をスキャンして探す必要がない)。得られたサービスは次の2通りで使う:
      - 自ストリーム (table_id=0x9F) の service_id は実データの全キャリアで MPT の package_id と完全に一致するため、
        これで MPT のサービスにサービス名を紐づける (MPT が取得できなかったサービスも assets 空のエントリとして補完する)
      - 自ストリーム/他ストリーム (table_id=0xA0) 双方のサービスを sdt_services として放送網全体の一覧に載せる
    MH-SDT はセクションが小さく、MPT が打ち切りサルベージでしか得られない 8K マルチキャリア分散伝送のキャリアでも
    完全な形で取得できることが多いため、8K サービスのサービス名を得る最も確実な手段になっている
    """

    def analyze(
        self,
        tlv_stream: bytes,
        truncated_packets: list[bytes] | None = None,
        carrier_group: TLVCarrierGroupInfo | None = None,
    ) -> CATVMMTInfo:
        """
        TLV ストリームを解析する

        Args:
            tlv_stream (bytes): ExtractTLVStream で再構成済みの TLV ストリーム
            truncated_packets (list[bytes] | None): ExtractTLVStream が収集した打ち切りパケットの一覧 (省略可)
                8K マルチキャリア分散伝送のキャリアでは MPT を含むシグナリングパケットが宣言長まで揃わないことが
                常態のため、打ち切られた先頭部分からの部分解析 (サルベージ) に使う
            carrier_group (TLVCarrierGroupInfo | None): ExtractTLVCarrierGroupInfo で取得した自キャリアの識別・グループ情報 (省略可)

        Returns:
            CATVMMTInfo: TLV キャリアの解析結果
        """

        network_info: TLVNetworkInfo | None = None
        services_by_package_id: dict[int, MMTServiceInfo] = {}
        sdt_services_by_service_id: dict[int, MMTSDTServiceInfo] = {}
        external_references: set[tuple[int, int]] = set()
        # MMTP シグナリングメッセージが複数パケットに分割 (fragmentation) されている場合の再組み立てバッファ
        # packet_id ごとに (連結中のバッファ, 直前フラグメントの packet_sequence_number) を保持し、
        # 先頭フラグメント (fragmentation_indicator=1) から最終フラグメント (=3) までを連結する
        # 各フラグメントの先頭2バイトは _ExtractSignallingMessageParts の時点でペイロードヘッダとして除去済み
        fragment_buffers: dict[int, tuple[bytearray, int]] = {}

        def RegisterSDTServiceInfo(sdt_service_info: MMTSDTServiceInfo) -> None:
            """MH-SDT から解析できたサービスを登録する (自ストリーム (0x9F) の情報を他ストリーム (0xA0) の情報より優先する)"""
            existing = sdt_services_by_service_id.get(sdt_service_info.service_id)
            if existing is None or (sdt_service_info.on_current_stream and not existing.on_current_stream):
                sdt_services_by_service_id[sdt_service_info.service_id] = sdt_service_info

        def RegisterServiceInfo(service_info: MMTServiceInfo) -> None:
            """解析できた MPT を登録する (同一 package_id が複数回得られた場合はアセット数が多い方 = より完全な方を採用する)"""
            existing = services_by_package_id.get(service_info.package_id)
            if existing is None or len(service_info.assets) > len(existing.assets):
                services_by_package_id[service_info.package_id] = service_info
            for asset_info in service_info.assets:
                if asset_info.external_network_id is not None:
                    external_references.add((asset_info.external_network_id, asset_info.external_tsid or 0))

        for packet_type, payload in _IterTLVPackets(tlv_stream):
            if packet_type == TLV_PACKET_TYPE_SIGNALING:
                if network_info is None and len(payload) > 0 and payload[0] == TLV_NIT_TABLE_ID:
                    network_info = _ParseTLVNIT(payload)
                continue

            if packet_type != TLV_PACKET_TYPE_COMPRESSED_IP:
                continue
            parts = _ExtractSignallingMessageParts(payload)
            if parts is None:
                continue
            packet_id, sequence_number, fragmentation_indicator, length_extension_flag, aggregation_flag, body = parts

            message: bytes | None = None
            if fragmentation_indicator == 0:
                message = body
            elif fragmentation_indicator == 1:
                # 先頭フラグメント (この時点ではまだ完成しないので次のフラグメントを待つ)
                fragment_buffers[packet_id] = (bytearray(body), sequence_number)
            elif packet_id in fragment_buffers:
                buffer, last_sequence_number = fragment_buffers[packet_id]
                # packet_sequence_number の連続性を確認し、途中のフラグメントが欠落している場合は破棄する
                # (8K キャリアではパケットの打ち切りによりフラグメントが歯抜けになることがあり、
                #  そのまま連結すると別メッセージの断片同士を繋いだ壊れたメッセージができてしまう)
                if sequence_number != ((last_sequence_number + 1) & 0xFFFFFFFF):
                    del fragment_buffers[packet_id]
                    continue
                # 中間 (2) / 最終 (3) フラグメント
                buffer += body
                if fragmentation_indicator == 3:
                    message = bytes(buffer)
                    del fragment_buffers[packet_id]
                else:
                    fragment_buffers[packet_id] = (buffer, sequence_number)

            if message is None:
                continue

            # aggregation (複数メッセージの連結) は分割されていない (fragmentation_indicator=0) メッセージのみ対応する
            if fragmentation_indicator == 0 and aggregation_flag:
                messages: list[bytes] = list(_IterAggregatedMessages(message, length_extension_flag=length_extension_flag))
            else:
                messages = [message]

            for single_message in messages:
                if packet_id == MMT_SI_PACKET_ID_MH_SDT:
                    # MH-SDT 専用の packet_id: M2 セクションメッセージとして決め打ちで解析する
                    section = _ExtractM2Section(single_message)
                    for sdt_service_info in (_ParseMHSDT(section) if section is not None else None) or []:
                        RegisterSDTServiceInfo(sdt_service_info)
                    continue
                for service_info in _ScanForMPT(single_message):
                    RegisterServiceInfo(service_info)

        # サルベージパス: 打ち切られたシグナリングパケットの先頭部分から MPT を部分解析する
        # 完全な MPT が1つも得られなかった package の情報を補完するのが目的 (完全な解析結果があればそちらを優先)
        for truncated_packet in truncated_packets or []:
            if len(truncated_packet) < 5 or truncated_packet[1] != TLV_PACKET_TYPE_COMPRESSED_IP:
                continue
            parts = _ExtractSignallingMessageParts(truncated_packet[4:])
            if parts is None:
                continue
            packet_id, _, fragmentation_indicator, _, _, body = parts
            # 打ち切られたパケットは末尾が欠けているため、フラグメント再組み立ては不可能 (fragmentation_indicator=0 のみ対象)
            # MH-SDT (packet_id=0x8004) は CRC32 検証が必要で打ち切られたセクションからは解析できないため対象外
            if fragmentation_indicator != 0 or packet_id == MMT_SI_PACKET_ID_MH_SDT:
                continue
            for service_info in _ScanForMPT(body, allow_truncated=True):
                # 打ち切りサルベージの結果は、完全なパケットから解析できた結果より常に劣後する
                # (RegisterServiceInfo はアセット数の多い方を採用するため、そのまま登録してよい)
                RegisterServiceInfo(service_info)

        # MH-SDT の自ストリーム (table_id=0x9F) のサービスを MPT のサービスに紐づける
        # 実データの全キャリアで MPT の package_id と自ストリーム MH-SDT の service_id が完全に一致することを確認済み
        for sdt_service_info in sdt_services_by_service_id.values():
            if not sdt_service_info.on_current_stream:
                continue
            service_info = services_by_package_id.get(sdt_service_info.service_id)
            if service_info is None:
                # MPT が (打ち切りサルベージでも) 取得できなかったサービス: MH-SDT で存在は確認できているため、
                # アセット一覧が空のエントリとして補完する (8K マルチキャリア分散伝送のキャリアで発生しうる)
                service_info = MMTServiceInfo(package_id=sdt_service_info.service_id)
                services_by_package_id[sdt_service_info.service_id] = service_info
            service_info.service_id = sdt_service_info.service_id
            service_info.service_name = sdt_service_info.service_name

        services = sorted(services_by_package_id.values(), key=lambda service_info: service_info.package_id)
        sdt_services = sorted(sdt_services_by_service_id.values(), key=lambda sdt_service_info: sdt_service_info.service_id)
        external_reference_infos = [
            MMTExternalReferenceInfo(network_id=network_id, transport_stream_id=transport_stream_id)
            for network_id, transport_stream_id in sorted(external_references)
        ]

        # TLV-NIT ベースのマルチキャリアグループ検出:
        # 自キャリアのストリーム (TSMF ヘッダの tlv_stream_id で特定。取得できない場合は、実データでは MPT の package_id と
        # NIT のサービス ID が一致することを利用してサービス ID の交差で特定) のエントリに複数の伝送周波数が
        # 列挙されていれば、8K のようなマルチキャリア分散伝送の一部と判定する
        multi_carrier_group: TLVStreamEntryInfo | None = None
        if network_info is not None:
            own_service_ids = set(services_by_package_id.keys())
            for stream_entry in network_info.streams:
                if len(stream_entry.frequencies_hz) < 2:
                    continue
                if carrier_group is not None:
                    # 自キャリアの tlv_stream_id が分かっている場合は、それと一致するエントリのみを対象にする
                    if stream_entry.tlv_stream_id == carrier_group.tlv_stream_id:
                        multi_carrier_group = stream_entry
                        break
                elif own_service_ids & set(stream_entry.service_ids):
                    multi_carrier_group = stream_entry
                    break

        # TSMF ヘッダのキャリアグループ情報 (最も確実) / TLV-NIT の複数周波数 / MPT の location_type=0x03 のいずれかで判定する
        is_multi_carrier_partial = (
            (carrier_group is not None and carrier_group.group_carrier_count >= 2)
            or multi_carrier_group is not None
            or len(external_reference_infos) > 0
        )

        return CATVMMTInfo(
            network=network_info,
            services=services,
            sdt_services=sdt_services,
            carrier_group=carrier_group,
            is_multi_carrier_partial=is_multi_carrier_partial,
            external_references=external_reference_infos,
            multi_carrier_group=multi_carrier_group,
        )


def _MergeMMTServiceInfo(base: MMTServiceInfo, other: MMTServiceInfo) -> MMTServiceInfo:
    """
    同一 package_id の MPT 由来サービス情報 2 つをマージした新しいインスタンスを返す
    アセット一覧はより完全な方 (アセット数が多い方) を採用しつつ、サービス ID / サービス名は埋まっている方から補完する
    (MPT が届いたキャリアと自ストリームの MH-SDT が届いたキャリアが別々になることがあり、
     アセット一覧を持つ側にサービス名が入っていないことがあるため)
    """

    if len(other.assets) > len(base.assets):
        primary, secondary = other, base
    else:
        # アセット数が同じ場合は base (先に登録された = キャリア順で先の方) を優先する
        primary, secondary = base, other
    merged = primary.model_copy(deep=True)
    if merged.service_id is None and secondary.service_id is not None:
        merged.service_id = secondary.service_id
    if merged.service_name == 'Unknown' and secondary.service_name != 'Unknown':
        merged.service_name = secondary.service_name
    return merged


def _MergeMMTSDTServiceInfo(base: MMTSDTServiceInfo, other: MMTSDTServiceInfo) -> MMTSDTServiceInfo:
    """
    同一 service_id の MH-SDT 由来サービス情報 2 つをマージした新しいインスタンスを返す
    自ストリーム (table_id=0x9F) 由来の情報を優先し、次いでサービス名が判明している方を優先する
    (MMTAnalyzer.RegisterSDTServiceInfo のキャリア内での優先順位と同じ考え方)
    """

    def Rank(sdt_service_info: MMTSDTServiceInfo) -> tuple[bool, bool]:
        return (sdt_service_info.on_current_stream, sdt_service_info.service_name != 'Unknown')

    # 優先度が同じ場合は base (先に登録された = キャリア順で先の方) を維持する
    return (other if Rank(other) > Rank(base) else base).model_copy(deep=True)


def MergeMultiCarrierGroupMMTInfo(carriers: list[CATVCarrierInfo]) -> None:
    """
    8K マルチキャリア分散伝送のグループを構成するキャリア同士で MMT シグナリング情報をマージし、
    グループ全メンバーの CATVMMTInfo に同一の内容を反映する (引数の carriers の内容を破壊的に更新する)

    8K 放送のマルチキャリア分散伝送では、1 本の TLV ストリーム (同一 tlv_stream_id) を複数の物理キャリアに分散して
    伝送しているため、どのキャリアに載っているシグナリングも「同じ 1 本の TLV ストリームのシグナリング」であり、
    グループ内でマージした結果こそが本来の TLV ストリームの内容になる
    一方で個々のキャリアに届くシグナリングは分散伝送の都合で断片的で、収録のたびに得られるテーブルの部分集合が変わるため、
    マージせずにキャリア単位の解析結果をそのまま出力すると、以下の問題が起きる:
      - 定期スキャンの差分レポートに、実際には変わっていないサービスの増減が毎回偽陽性として出る
      - MPT も自ストリームの MH-SDT も届かなかったキャリアでは services が空になり、
        Mirakurun/mirakc のチャンネル設定に出力する TLV 除外注記からサービス名が欠落する

    グループの識別には TSMF 多重フレームヘッダ由来の carrier_group (network_id / tlv_stream_id / group_id) を使い、
    group_carrier_count が 2 以上のキャリアのみを対象とする
    TLV-NIT 由来の multi_carrier_group はフォールバックとしても使わない (TLV-NIT はマルチキャリアのキャリアでは
    そもそも取得できないことが多く、かつ「どのキャリアがそのグループの一員か」の判定には結局自キャリアの識別情報が
    必要になるため、常に完全な形で取得できる TSMF ヘッダ由来の情報だけで判定する方が確実かつ単純)

    単独キャリア (group_carrier_count が 2 未満のキャリア・TLV でないキャリア・TSMF ヘッダを取得できなかったキャリア) は
    一切変更しない

    Args:
        carriers (list[CATVCarrierInfo]): スキャンで得られた全キャリアの解析結果 (この関数内で内容が更新される)
    """

    # (network_id, tlv_stream_id, group_id) が一致するキャリアを同一グループとしてまとめる
    # (CATVMMTInfo は carrier.mmt が保持しているインスタンスそのものなので、これを更新すれば carriers 側にも反映される)
    groups: dict[tuple[int, int, int], list[tuple[str, CATVMMTInfo]]] = {}
    for carrier in carriers:
        mmt_info = carrier.mmt
        if mmt_info is None or mmt_info.carrier_group is None:
            continue
        carrier_group = mmt_info.carrier_group
        if carrier_group.group_carrier_count < 2:
            continue
        group_key = (carrier_group.network_id, carrier_group.tlv_stream_id, carrier_group.group_id)
        groups.setdefault(group_key, []).append((carrier.physical_channel, mmt_info))

    for group_members in groups.values():
        # グループの一員であることが分かっていても、そのグループの他のキャリアがスキャン対象に含まれていない
        # (--channels で一部のみスキャンした場合など) ことがある。この場合はマージのしようがないので何もしない
        if len(group_members) < 2:
            continue
        # マージ結果がスキャン順 (並列ワーカーの完了順) に依存しないよう、物理チャンネル順に走査する
        group_members = sorted(group_members, key=lambda member: member[0])

        merged_services: dict[int, MMTServiceInfo] = {}
        merged_sdt_services: dict[int, MMTSDTServiceInfo] = {}
        merged_network: TLVNetworkInfo | None = None
        for _, mmt_info in group_members:
            for service_info in mmt_info.services:
                existing_service = merged_services.get(service_info.package_id)
                merged_services[service_info.package_id] = (
                    service_info.model_copy(deep=True) if existing_service is None else _MergeMMTServiceInfo(existing_service, service_info)
                )
            for sdt_service_info in mmt_info.sdt_services:
                existing_sdt_service = merged_sdt_services.get(sdt_service_info.service_id)
                merged_sdt_services[sdt_service_info.service_id] = (
                    sdt_service_info.model_copy(deep=True)
                    if existing_sdt_service is None
                    else _MergeMMTSDTServiceInfo(existing_sdt_service, sdt_service_info)
                )
            # TLV-NIT はキャリアによって取得できたりできなかったりするため、グループ内で最も情報量の多い
            # (ストリームループのエントリ数が多い) ものをグループ共通のネットワーク情報として採用する
            if mmt_info.network is not None and (merged_network is None or len(mmt_info.network.streams) > len(merged_network.streams)):
                merged_network = mmt_info.network

        services = sorted(merged_services.values(), key=lambda service_info: service_info.package_id)
        sdt_services = sorted(merged_sdt_services.values(), key=lambda sdt_service_info: sdt_service_info.service_id)
        for _, mmt_info in group_members:
            # キャリア間でモデルのインスタンスを共有すると、片方への変更が意図せず他方に波及してしまうため、
            # 各キャリアにはそれぞれ独立したコピーを持たせる
            mmt_info.services = [service_info.model_copy(deep=True) for service_info in services]
            mmt_info.sdt_services = [sdt_service_info.model_copy(deep=True) for sdt_service_info in sdt_services]
            # ネットワーク情報は「取得できなかったキャリアの補完」のみ行い、取得できているキャリアの内容は上書きしない
            # (TLV-NIT はキャリアごとに完全な形で受信できていれば内容も同一のはずのため、無理に統一する必要はない)
            if mmt_info.network is None and merged_network is not None:
                mmt_info.network = merged_network.model_copy(deep=True)

from __future__ import annotations

from collections.abc import Iterator

from isdb_scanner.catv.constants import CASInfo
from isdb_scanner.catv.tsmf import TS_PACKET_SIZE, TS_SYNC_BYTE, GetPID


# CA 記述子のタグ値 (ARIB STD-B10 第2部 6.2.5)
CA_DESCRIPTOR_TAG = 0x09
# スクランブル判定の閾値 (この割合未満なら「実質スクランブルされていない」とみなす)
SCRAMBLE_RATIO_NONE_THRESHOLD = 0.01

# MPEG-2 PSI の CRC32 (CRC-32/MPEG-2: 反転なし・初期値 0xFFFFFFFF・xorout 0x00000000) の生成多項式
_CRC32_MPEG_POLYNOMIAL = 0x04C11DB7
# 256 バイト分の CRC32 テーブルを事前計算しておく (1 バイトずつ計算するより高速)
_CRC32_MPEG_TABLE: list[int] = []
for _byte in range(256):
    _crc = _byte << 24
    for _ in range(8):
        _crc = ((_crc << 1) ^ _CRC32_MPEG_POLYNOMIAL) & 0xFFFFFFFF if (_crc & 0x80000000) else (_crc << 1) & 0xFFFFFFFF
    _CRC32_MPEG_TABLE.append(_crc)


def _CalculateCRC32MPEG(data: bytes) -> int:
    """MPEG-2 PSI で使われる CRC32 (CRC-32/MPEG-2) を計算する"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC32_MPEG_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _VerifySectionCRC32(section: bytes) -> bool:
    """セクション末尾 4 バイトの CRC32 を検証する (末尾 4 バイトを除いた部分から計算した値と一致するか)"""
    if len(section) < 4:
        return False
    expected = int.from_bytes(section[-4:], byteorder='big')
    return _CalculateCRC32MPEG(section[:-4]) == expected


def _ParseDescriptors(data: bytes) -> list[tuple[int, bytes]]:
    """記述子ループのバイト列から (タグ, 記述子本体) のリストを取り出す"""
    descriptors: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 2 <= len(data):
        tag = data[offset]
        length = data[offset + 1]
        body = data[offset + 2 : offset + 2 + length]
        descriptors.append((tag, body))
        offset += 2 + length
    return descriptors


def _IterSections(ts_stream: bytes | bytearray, target_pid: int) -> Iterator[bytes]:
    """
    指定 PID の TS パケットからセクション (PSI) を再構成して yield する
    188 バイト境界に整列済みのストリームを前提とする (TSMFDemultiplexer.demux_all の出力や、単一 TS のダンプを想定)
    CRC32 (CRC-32/MPEG-2) の検証に失敗したセクションは、破損データとして読み飛ばす
    """
    view = memoryview(ts_stream)
    buffer = bytearray()
    # 直前に検出したセクション長 (次パケットへの継続を待っている間の期待値、未確定なら -1)
    expected_length = -1
    for offset in range(0, len(ts_stream) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = view[offset : offset + TS_PACKET_SIZE]
        if packet[0] != TS_SYNC_BYTE:
            continue
        if GetPID(packet) != target_pid:
            continue
        payload_unit_start = (packet[1] & 0x40) != 0
        adaptation_field_control = (packet[3] & 0x30) >> 4
        payload_offset = 4
        if adaptation_field_control in (2, 3):
            payload_offset += 1 + packet[4]
        # adaptation_field のみ (ペイロードなし) や、adaptation_field 長が異常なパケットは無視する
        if adaptation_field_control in (0, 2) or payload_offset >= TS_PACKET_SIZE:
            continue
        payload = packet[payload_offset:]

        if payload_unit_start:
            pointer_field = payload[0]
            continuation_start = 1 + pointer_field
            # 継続中のセクションがあれば、pointer_field が指す位置までのバイト列で完結させる
            if expected_length > 0 and buffer:
                buffer.extend(payload[1 : 1 + pointer_field])
                if len(buffer) >= expected_length and _VerifySectionCRC32(bytes(buffer[:expected_length])):
                    yield bytes(buffer[:expected_length])
            buffer = bytearray(payload[continuation_start:])
        else:
            # 先頭セクションが見つかる前の継続パケットは無視する
            if expected_length < 0 and not buffer:
                continue
            buffer.extend(payload)

        # バッファ内に完結しているセクションがあれば、1 パケットに複数セクションが含まれる場合も考慮して順次 yield する
        while len(buffer) >= 3:
            if buffer[0] == 0xFF:
                # スタッフィングバイト: このパケット以降はもう有効なセクションがない
                buffer = bytearray()
                expected_length = -1
                break
            section_length = (((buffer[1] & 0x0F) << 8) | buffer[2]) + 3
            if len(buffer) >= section_length:
                section = bytes(buffer[:section_length])
                if _VerifySectionCRC32(section):
                    yield section
                buffer = buffer[section_length:]
                expected_length = -1
            else:
                expected_length = section_length
                break


def _ParsePAT(ts_stream: bytes | bytearray) -> tuple[int | None, dict[int, int]]:
    """
    PAT (Program Association Table, PID 0x0000) を解析し、(transport_stream_id, {program_number: PMT の PID}) を返す
    実データでは PAT は 1 セクションに収まることがほとんどのため、最初に見つかった有効なセクションのみを処理する
    """
    for section in _IterSections(ts_stream, 0x0000):
        if section[0] != 0x00:  # table_id: program_association_section
            continue
        transport_stream_id = (section[3] << 8) | section[4]
        pmt_pids: dict[int, int] = {}
        for i in range(8, len(section) - 4, 4):
            program_number = (section[i] << 8) | section[i + 1]
            pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
            if program_number != 0:  # program_number == 0 は NIT の PID を示すエントリなので除外する
                pmt_pids[program_number] = pid
        return transport_stream_id, pmt_pids
    return None, {}


def ExtractTransportStreamId(ts_stream: bytes | bytearray) -> int | None:
    """PAT から自 TS の transport_stream_id を取得する (PAT が取得できない場合は None)"""
    transport_stream_id, _ = _ParsePAT(ts_stream)
    return transport_stream_id


def _ExtractCASystemIds(ts_stream: bytes | bytearray, pmt_pids: dict[int, int]) -> set[int]:
    """CAT (PID 0x0001) と、指定された全 PMT の第1ループ (番組情報記述子) から CA_system_id を収集する"""
    ca_system_ids: set[int] = set()

    # CAT (Conditional Access Table): 実データでは 1 セクションに収まることがほとんどのため、最初の有効なセクションのみ処理する
    for section in _IterSections(ts_stream, 0x0001):
        if section[0] != 0x01:  # table_id: conditional_access_section
            continue
        for tag, body in _ParseDescriptors(section[8:-4]):
            if tag == CA_DESCRIPTOR_TAG and len(body) >= 2:
                ca_system_ids.add((body[0] << 8) | body[1])
        break

    # PMT (Program Map Table): PAT から得られた全プログラムを対象にする (probe 実装にあった先頭6件制限は撤廃)
    for pid in pmt_pids.values():
        for section in _IterSections(ts_stream, pid):
            if section[0] != 0x02:  # table_id: program_map_section
                continue
            program_info_length = ((section[10] & 0x0F) << 8) | section[11]
            for tag, body in _ParseDescriptors(section[12 : 12 + program_info_length]):
                if tag == CA_DESCRIPTOR_TAG and len(body) >= 2:
                    ca_system_ids.add((body[0] << 8) | body[1])
            break  # PMT も実データでは 1 セクションに収まることがほとんど

    return ca_system_ids


def ExtractSDTFreeCAModeMap(ts_stream: bytes | bytearray) -> dict[int, bool]:
    """
    SDT (Service Description Section, 自ネットワーク: table_id 0x42, PID 0x0011) から
    service_id -> is_free (free_CA_mode == 0 なら True) の対応表を取得する
    """
    free_ca_mode_map: dict[int, bool] = {}
    for section in _IterSections(ts_stream, 0x0011):
        if section[0] != 0x42:  # table_id: 自ネットワークの service_description_section
            continue
        i = 11
        # セクション本体の末尾 4 バイトは CRC32 なので、それより手前までを走査する
        while i + 5 <= len(section) - 4:
            service_id = (section[i] << 8) | section[i + 1]
            free_ca_mode = (section[i + 3] & 0x10) != 0
            descriptors_loop_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
            free_ca_mode_map[service_id] = not free_ca_mode
            i += 5 + descriptors_loop_length
        break  # 実データでは SDT も 1 セクションに収まることがほとんど
    return free_ca_mode_map


def AnalyzeCAS(ts_stream: bytes | bytearray) -> CASInfo:
    """
    TS ストリーム (TSMF の場合は分離済みの単一 TS 分) から CAS (限定受信システム) 関連の情報を解析する

    Args:
        ts_stream (bytes | bytearray): 188 バイト境界に整列済みの TS ストリーム (単一 TS 分)

    Returns:
        CASInfo: CAS 関連の解析結果
    """

    _, pmt_pids = _ParsePAT(ts_stream)
    ca_system_ids = _ExtractCASystemIds(ts_stream, pmt_pids)

    # スクランブル率: 全 TS パケットのうち transport_scrambling_control (先頭から4バイト目の上位2bit) が
    # 0 (非スクランブル) 以外になっているものの割合
    total_packet_count = 0
    scrambled_packet_count = 0
    view = memoryview(ts_stream)
    for offset in range(0, len(ts_stream) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = view[offset : offset + TS_PACKET_SIZE]
        if packet[0] != TS_SYNC_BYTE:
            continue
        total_packet_count += 1
        if packet[3] & 0xC0:
            scrambled_packet_count += 1
    scramble_ratio = (scrambled_packet_count / total_packet_count) if total_packet_count > 0 else 0.0

    free_ca_mode_map = ExtractSDTFreeCAModeMap(ts_stream)
    has_free_ca_mode_service = any(not is_free for is_free in free_ca_mode_map.values())

    # 受信に必要な CAS カードの種別を判定する
    if scramble_ratio < SCRAMBLE_RATIO_NONE_THRESHOLD:
        # ほぼスクランブルされていない = 無料放送のみでカード不要
        required_card = 'none'
    elif 0x0006 in ca_system_ids:
        # C-CAS (0x0006) と B-CAS (0x0005) の両方が見つかった場合は、CATV 事業者による再スクランブルの可能性が高い C-CAS を優先する
        required_card = 'C-CAS'
    elif 0x0005 in ca_system_ids:
        required_card = 'B-CAS'
    else:
        # スクランブルされているが CA 記述子が見つからない、または未知の CA_system_id しかない場合
        required_card = 'unknown'

    return CASInfo(
        ca_system_ids=sorted(ca_system_ids),
        scramble_ratio=scramble_ratio,
        has_free_ca_mode_service=has_free_ca_mode_service,
        required_card=required_card,
    )

from isdb_scanner.catv.cas import (
    AnalyzeCAS,
    ExtractSDTFreeCAModeMap,
    ExtractTransportStreamId,
    _CalculateCRC32MPEG,
)
from isdb_scanner.catv.tsmf import TS_PACKET_SIZE, TS_SYNC_BYTE


def AppendCRC32(section_without_crc: bytes) -> bytes:
    """セクション本体 (CRC32 を除く) から CRC32 (CRC-32/MPEG-2) を計算し、末尾に付加する"""
    crc = _CalculateCRC32MPEG(section_without_crc)
    return section_without_crc + crc.to_bytes(4, byteorder='big')


def BuildPATSection(transport_stream_id: int, programs: dict[int, int]) -> bytes:
    """テスト用に PAT (Program Association Section) を生成する"""
    body = bytearray()
    for program_number, pid in programs.items():
        body += program_number.to_bytes(2, byteorder='big')
        body += bytes([0xE0 | ((pid >> 8) & 0x1F), pid & 0xFF])
    header_tail = transport_stream_id.to_bytes(2, byteorder='big') + bytes([0xC1, 0x00, 0x00])
    section_length = len(header_tail) + len(body) + 4  # +4: CRC32
    section = bytes([0x00, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]) + header_tail + bytes(body)
    return AppendCRC32(section)


def BuildCATSection(ca_system_ids: list[int]) -> bytes:
    """テスト用に CAT (Conditional Access Section) を生成する"""
    descriptors = bytearray()
    for ca_system_id in ca_system_ids:
        # CA記述子 (tag=0x09): CA_system_id(2) + reserved(3)+CA_PID(13)
        descriptors += bytes([0x09, 0x04]) + ca_system_id.to_bytes(2, byteorder='big') + bytes([0xE0, 0x10])
    header_tail = bytes(
        [0xFF, 0xFF, 0xC1, 0x00, 0x00]
    )  # reserved / version_number / current_next_indicator / section_number / last_section_number
    section_length = len(header_tail) + len(descriptors) + 4
    section = bytes([0x01, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]) + header_tail + bytes(descriptors)
    return AppendCRC32(section)


def BuildPMTSection(program_number: int, pcr_pid: int, ca_system_ids: list[int]) -> bytes:
    """テスト用に PMT (Program Map Section) を生成する (CA 記述子は番組情報記述子ループ (第1ループ) に格納する)"""
    program_descriptors = bytearray()
    for ca_system_id in ca_system_ids:
        program_descriptors += bytes([0x09, 0x04]) + ca_system_id.to_bytes(2, byteorder='big') + bytes([0xE0, 0x20])
    header = (
        program_number.to_bytes(2, byteorder='big')
        + bytes([0xC1, 0x00, 0x00])  # reserved/version_number/current_next_indicator, section_number, last_section_number
        + bytes([0xE0 | ((pcr_pid >> 8) & 0x1F), pcr_pid & 0xFF])  # PCR_PID
        + bytes([0xF0 | ((len(program_descriptors) >> 8) & 0x0F), len(program_descriptors) & 0xFF])  # program_info_length
    )
    body = header + bytes(program_descriptors)
    section_length = len(body) + 4
    section = bytes([0x02, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]) + body
    return AppendCRC32(section)


def BuildSDTSection(transport_stream_id: int, services: list[tuple[int, bool]]) -> bytes:
    """
    テスト用に SDT (自ネットワーク, Service Description Section) を生成する
    services は (service_id, free_ca_mode_flag) のリスト (free_ca_mode_flag=True は有料放送を意味する)
    """
    loop = bytearray()
    for service_id, free_ca_mode_flag in services:
        descriptors_loop_length = 0  # このテストでは ServiceDescriptor などは省略する (free_CA_mode の判定には不要)
        loop += service_id.to_bytes(2, byteorder='big')
        loop += bytes([0xFC])  # reserved_future_use + EIT_schedule_flag + EIT_present_following_flag + running_status (値は判定に無関係)
        free_ca_mode_bit = 0x10 if free_ca_mode_flag else 0x00
        loop += bytes([0xE0 | free_ca_mode_bit | ((descriptors_loop_length >> 8) & 0x0F), descriptors_loop_length & 0xFF])
    header = (
        transport_stream_id.to_bytes(2, byteorder='big')
        + bytes([0xC1, 0x00, 0x00])  # reserved/version_number/current_next_indicator, section_number, last_section_number
        + bytes([0x00, 0x01, 0xFF])  # original_network_id, reserved_future_use
    )
    body = header + bytes(loop)
    section_length = len(body) + 4
    section = bytes([0x42, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]) + body
    return AppendCRC32(section)


def BuildSectionPacket(pid: int, section: bytes, continuity_counter: int = 0) -> bytes:
    """セクションを 1 個の TS パケット (PUSI=1, pointer_field=0) に格納する (セクションは 183 バイト以下を想定)"""
    assert len(section) <= TS_PACKET_SIZE - 4 - 1
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = 0x40 | ((pid >> 8) & 0x1F)  # payload_unit_start_indicator = 1
    packet[2] = pid & 0xFF
    packet[3] = 0x10 | (continuity_counter & 0x0F)  # adaptation_field_control = payload のみ
    payload = bytes([0x00]) + section  # pointer_field = 0 (パケット先頭からセクションが始まる)
    payload += b'\xff' * (TS_PACKET_SIZE - 4 - len(payload))  # 残りはスタッフィングバイトで埋める
    packet[4:] = payload
    return bytes(packet)


def BuildElementaryStreamPacket(pid: int, scrambled: bool, continuity_counter: int = 0) -> bytes:
    """スクランブル率算出テスト用のダミー ES パケットを生成する"""
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = (pid >> 8) & 0x1F
    packet[2] = pid & 0xFF
    scramble_bits = 0x80 if scrambled else 0x00  # transport_scrambling_control (先頭2bitのいずれかが立っていればスクランブル扱い)
    packet[3] = scramble_bits | 0x10 | (continuity_counter & 0x0F)
    return bytes(packet)


class TestAnalyzeCASSynthetic:
    """合成データによる AnalyzeCAS の基本動作テスト (CI でも実行可能)"""

    def _build_stream(self, ca_system_ids: list[int], scrambled_count: int, free_count: int) -> bytearray:
        transport_stream_id = 0x1234
        pmt_pid = 0x0100
        stream = bytearray()
        stream += BuildSectionPacket(0x0000, BuildPATSection(transport_stream_id, {1: pmt_pid}))
        stream += BuildSectionPacket(0x0001, BuildCATSection(ca_system_ids))
        stream += BuildSectionPacket(pmt_pid, BuildPMTSection(1, pmt_pid, ca_system_ids))
        stream += BuildSectionPacket(0x0011, BuildSDTSection(transport_stream_id, [(1, False), (2, True)]))
        for i in range(scrambled_count):
            stream += BuildElementaryStreamPacket(0x0101, scrambled=True, continuity_counter=i)
        for i in range(free_count):
            stream += BuildElementaryStreamPacket(0x0101, scrambled=False, continuity_counter=i)
        return stream

    def test_extract_transport_stream_id(self):
        stream = self._build_stream([0x0005], scrambled_count=0, free_count=10)
        assert ExtractTransportStreamId(stream) == 0x1234

    def test_analyze_cas_scrambled_with_bcas(self):
        # 4 セクションパケット + スクランブル 60 パケット + 非スクランブル 40 パケット = 全 104 パケット中 60 がスクランブル
        stream = self._build_stream([0x0005], scrambled_count=60, free_count=40)
        cas_info = AnalyzeCAS(stream)
        assert cas_info.ca_system_ids == [0x0005]
        assert abs(cas_info.scramble_ratio - (60 / 104)) < 1e-9
        assert cas_info.required_card == 'B-CAS'
        assert cas_info.has_free_ca_mode_service is True  # service_id=2 が有料 (free_CA_mode=1)

    def test_analyze_cas_ccas_priority_over_bcas(self):
        # CAT/PMT に B-CAS(0x0005) と C-CAS(0x0006) の両方が含まれる場合は C-CAS を優先する
        stream = self._build_stream([0x0005, 0x0006], scrambled_count=10, free_count=0)
        cas_info = AnalyzeCAS(stream)
        assert cas_info.ca_system_ids == [0x0005, 0x0006]
        assert cas_info.required_card == 'C-CAS'

    def test_analyze_cas_unscrambled_requires_no_card(self):
        stream = self._build_stream([], scrambled_count=0, free_count=50)
        cas_info = AnalyzeCAS(stream)
        assert cas_info.ca_system_ids == []
        assert cas_info.scramble_ratio == 0.0
        assert cas_info.required_card == 'none'
        assert cas_info.has_free_ca_mode_service is True  # SDT の有料フラグ自体は立っている (無料放送とは独立した判定)

    def test_analyze_cas_scrambled_without_ca_descriptor_is_unknown(self):
        # スクランブルされているのに CAT/PMT に CA 記述子が全くない場合はカード種別を特定できない
        stream = self._build_stream([], scrambled_count=30, free_count=0)
        cas_info = AnalyzeCAS(stream)
        assert cas_info.ca_system_ids == []
        assert cas_info.required_card == 'unknown'

    def test_extract_sdt_free_ca_mode_map(self):
        stream = self._build_stream([0x0005], scrambled_count=0, free_count=1)
        free_ca_mode_map = ExtractSDTFreeCAModeMap(stream)
        assert free_ca_mode_map == {1: True, 2: False}

    def test_invalid_crc32_section_is_ignored(self):
        # CRC32 が不正なセクションは読み飛ばされ、PAT が見つからない扱いになる
        transport_stream_id = 0x1234
        section = BuildPATSection(transport_stream_id, {1: 0x0100})
        corrupted_section = section[:-1] + bytes([section[-1] ^ 0xFF])  # CRC32 の末尾1バイトを破壊する
        stream = BuildSectionPacket(0x0000, corrupted_section)
        assert ExtractTransportStreamId(stream) is None


class TestAnalyzeCASPrecomputedInputs:
    """PAT/SDT の解析結果を事前に渡した場合の AnalyzeCAS のテスト (analyzer.py からの二重パース回避パス)"""

    def test_precomputed_pmt_pids_and_free_ca_mode_map(self):
        transport_stream_id = 0x1234
        pmt_pid = 0x0100
        stream = bytearray()
        stream += BuildSectionPacket(0x0000, BuildPATSection(transport_stream_id, {1: pmt_pid}))
        stream += BuildSectionPacket(0x0001, BuildCATSection([0x0005]))
        stream += BuildSectionPacket(pmt_pid, BuildPMTSection(1, pmt_pid, [0x0005]))
        for i in range(60):
            stream += BuildElementaryStreamPacket(0x0101, scrambled=True, continuity_counter=i)

        # SDT を含まないストリームでも、free_ca_mode_map を渡せば SDT の再解析なしで判定できる
        cas_info = AnalyzeCAS(stream, pmt_pids={1: pmt_pid}, free_ca_mode_map={1: True, 2: False})
        assert cas_info.ca_system_ids == [0x0005]
        assert cas_info.required_card == 'B-CAS'
        assert cas_info.has_free_ca_mode_service is True

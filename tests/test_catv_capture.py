import pytest

from isdb_scanner.catv.capture import (
    MMTP_VIDEO_PACKET_ID,
    _AssignTuners,
    _ExtractMMTPSequenceNumbers,
    _SequenceRangesOverlap,
)
from isdb_scanner.catv.mmt import TLV_PACKET_TYPE_COMPRESSED_IP, TLV_SYNC_BYTE
from isdb_scanner.catv.tsmf import TLV_CELL_PID, TS_PACKET_SIZE, TS_SYNC_BYTE
from isdb_scanner.catv.tuner import CATVTuner


def BuildTLVPacket(packet_type: int, payload: bytes) -> bytes:
    """テスト用に TLV パケット (sync + type + length + payload) を1個組み立てる (test_mmt.py と同じヘルパー)"""
    return bytes([TLV_SYNC_BYTE, packet_type, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


def BuildTLVCells(data: bytes) -> bytes:
    """
    連結済みの TLV パケット列を PID 0x002D のセル列に詰め直す (test_mmt.py の BuildTLVCells と同じロジック)
    テストで使う程度の短いデータであれば、常に1個の開始セル (flag=0x40, pointer_field=0) に収まる
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
            packet[3] = 0x00  # pointer_field = 0
            packet[4 : 4 + len(chunk)] = chunk
            offset += len(chunk)
            is_first = False
        else:
            chunk = data[offset : offset + 185]
            packet[1] = (TLV_CELL_PID >> 8) & 0x1F
            packet[2] = TLV_CELL_PID & 0xFF
            packet[3 : 3 + len(chunk)] = chunk
            offset += len(chunk)
        cells += packet
    return bytes(cells)


def BuildMMTPCompressedIPPacket(packet_id: int, packet_sequence_number: int) -> bytes:
    """
    テスト用に、MMTP パケット (MPU/シグナリング問わず、payload_type=0x00 固定) を運ぶ「ヘッダ圧縮 IP パケット」の
    TLV パケットを組み立てる (_ExtractMMTPSequenceNumbers は payload_type を問わず packet_id と
    packet_sequence_number だけを見るため、_ParseCompressedIPPacket が解析できる最小限の構造にしている)
    """
    mmtp_header = (
        bytes([0x00, 0x00])  # version(2bit)=0 + packet_counter_flag(1bit)=0 + ... / payload_type(6bit)=0
        + packet_id.to_bytes(2, byteorder='big')
        + bytes(4)  # timestamp (未使用)
        + packet_sequence_number.to_bytes(4, byteorder='big')
    )
    compressed_ip_payload = bytes([0x00, 0x01, 0x61]) + mmtp_header  # context_id(2B) + header_type(0x61=圧縮済み)
    return BuildTLVPacket(TLV_PACKET_TYPE_COMPRESSED_IP, compressed_ip_payload)


class TestExtractMMTPSequenceNumbers:
    """合成データによる _ExtractMMTPSequenceNumbers のテスト (CI でも実行可能)"""

    def test_extracts_sequence_numbers_for_target_packet_id(self):
        packets = (
            BuildMMTPCompressedIPPacket(MMTP_VIDEO_PACKET_ID, 100)
            + BuildMMTPCompressedIPPacket(MMTP_VIDEO_PACKET_ID, 101)
            + BuildMMTPCompressedIPPacket(MMTP_VIDEO_PACKET_ID, 105)
        )
        ts_stream = BuildTLVCells(packets)

        sequence_numbers = _ExtractMMTPSequenceNumbers(ts_stream)
        assert sequence_numbers == {100, 101, 105}

    def test_ignores_other_packet_ids(self):
        packets = BuildMMTPCompressedIPPacket(MMTP_VIDEO_PACKET_ID, 42) + BuildMMTPCompressedIPPacket(0x0000, 999)
        ts_stream = BuildTLVCells(packets)

        sequence_numbers = _ExtractMMTPSequenceNumbers(ts_stream)
        assert sequence_numbers == {42}

    def test_custom_packet_id_filter(self):
        packets = BuildMMTPCompressedIPPacket(0xF110, 7)
        ts_stream = BuildTLVCells(packets)

        assert _ExtractMMTPSequenceNumbers(ts_stream, packet_ids={0xF110}) == {7}
        assert _ExtractMMTPSequenceNumbers(ts_stream, packet_ids={MMTP_VIDEO_PACKET_ID}) == set()

    def test_packet_ids_resolved_from_mpt_when_omitted(self):
        # MPT (hev1 アセット) が取得できる場合は、フォールバック値 (MMTP_VIDEO_PACKET_ID) ではなく
        # MPT から解決した packet_id の MMTP パケットが対象になる
        from tests.test_mmt import BuildAssetEntry, BuildMPTTable, BuildSignallingTLVPacket

        video_asset = BuildAssetEntry(b'hev1', location_type=0x00, location_body=(0xF200).to_bytes(2, 'big'))
        mpt_packet = BuildSignallingTLVPacket(packet_id=0x0000, message_body=BuildMPTTable(package_id=0x01, assets=[video_asset]))
        video_packets = BuildMMTPCompressedIPPacket(0xF200, 10) + BuildMMTPCompressedIPPacket(0xF200, 11)
        fallback_packets = BuildMMTPCompressedIPPacket(MMTP_VIDEO_PACKET_ID, 999)
        ts_stream = BuildTLVCells(mpt_packet + video_packets + fallback_packets)

        assert _ExtractMMTPSequenceNumbers(ts_stream) == {10, 11}

    def test_no_tlv_data_returns_empty_set(self):
        non_tlv_packet = bytearray(TS_PACKET_SIZE)
        non_tlv_packet[0] = TS_SYNC_BYTE
        assert _ExtractMMTPSequenceNumbers(bytes(non_tlv_packet)) == set()


class TestSequenceRangesOverlap:
    """_SequenceRangesOverlap (複数キャリア間の時間範囲重複判定) のテスト"""

    def test_overlapping_ranges(self):
        assert _SequenceRangesOverlap([(0, 100), (50, 150), (80, 200)]) is True

    def test_non_overlapping_ranges(self):
        assert _SequenceRangesOverlap([(0, 100), (200, 300)]) is False

    def test_touching_boundary_counts_as_overlap(self):
        assert _SequenceRangesOverlap([(0, 100), (100, 200)]) is True

    def test_single_range_is_always_true(self):
        assert _SequenceRangesOverlap([(0, 100)]) is True

    def test_empty_list_is_true(self):
        assert _SequenceRangesOverlap([]) is True


class TestAssignTuners:
    """_AssignTuners (チャンネル-チューナー割当ロジック) のテスト。実選局は行わない"""

    def test_explicit_adapters_assignment(self):
        assignments = _AssignTuners(['CATV_C32', 'CATV_C33', 'CATV_C34'], [0, 1, 2])
        assert assignments == [('CATV_C32', 0, 0), ('CATV_C33', 1, 0), ('CATV_C34', 2, 0)]

    def test_explicit_adapters_more_than_channels_uses_only_needed_count(self):
        assignments = _AssignTuners(['CATV_15'], [3, 4, 5])
        assert assignments == [('CATV_15', 3, 0)]

    def test_too_many_channels_for_explicit_adapters_raises(self):
        with pytest.raises(ValueError):
            _AssignTuners(['CATV_C32', 'CATV_C33', 'CATV_C34'], [0, 1])

    def test_duplicate_explicit_adapters_raises(self):
        # 同一アダプタへの重複割当は選局が競合するため、収録開始前にエラーにする
        with pytest.raises(ValueError):
            _AssignTuners(['CATV_C32', 'CATV_C33'], [1, 1])

    def test_auto_detect_uses_available_tuners(self, monkeypatch: pytest.MonkeyPatch):
        fake_tuners = [CATVTuner(2), CATVTuner(5)]
        monkeypatch.setattr(CATVTuner, 'getAvailableCATVTuners', lambda: fake_tuners)

        assignments = _AssignTuners(['CATV_15', 'CATV_16'], None)
        assert assignments == [('CATV_15', 2, 0), ('CATV_16', 5, 0)]

    def test_auto_detect_preserves_frontend_number(self, monkeypatch: pytest.MonkeyPatch):
        # frontend0 以外の CATV 対応フロントエンドが検出された場合、その番号がそのまま割り当てられる
        fake_tuners = [CATVTuner(0, 1), CATVTuner(3, 2)]
        monkeypatch.setattr(CATVTuner, 'getAvailableCATVTuners', lambda: fake_tuners)

        assignments = _AssignTuners(['CATV_15', 'CATV_16'], None)
        assert assignments == [('CATV_15', 0, 1), ('CATV_16', 3, 2)]

    def test_auto_detect_no_tuners_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(CATVTuner, 'getAvailableCATVTuners', lambda: [])

        with pytest.raises(ValueError):
            _AssignTuners(['CATV_15'], None)

    def test_auto_detect_too_many_channels_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(CATVTuner, 'getAvailableCATVTuners', lambda: [CATVTuner(0)])

        with pytest.raises(ValueError):
            _AssignTuners(['CATV_C32', 'CATV_C33'], None)

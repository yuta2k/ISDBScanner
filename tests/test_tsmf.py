from pathlib import Path

from isdb_scanner.catv.constants import CarrierType
from isdb_scanner.catv.tsmf import (
    TS_PACKET_SIZE,
    TS_SYNC_BYTE,
    TSMF_SLOT_COUNT,
    TSMFDemultiplexer,
)


REPOSITORY_ROOT = Path(__file__).parent.parent


def BuildTSMFHeaderPacket(relative_ts_numbers: list[int], frame_sync: int = 0x1A86) -> bytes:
    """テスト用に TSMF 多重フレームヘッダパケット (PID 0x002F) を生成する"""
    assert len(relative_ts_numbers) == TSMF_SLOT_COUNT
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = 0x00
    packet[2] = 0x2F
    packet[3] = 0x10
    packet[4] = (frame_sync >> 8) & 0x1F
    packet[5] = frame_sync & 0xFF
    for index in range(26):
        packet[73 + index] = (relative_ts_numbers[index * 2] << 4) | relative_ts_numbers[index * 2 + 1]
    return bytes(packet)


def BuildSlotPacket(marker: int) -> bytes:
    """テスト用にスロットパケット (PID 0x0100 / ペイロード先頭に marker) を生成する"""
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = 0x01
    packet[2] = 0x00
    packet[3] = 0x10
    packet[4] = marker & 0xFF
    return bytes(packet)


class TestTSMFDemultiplexerSynthetic:
    """合成データによるテスト (実データのダンプに依存しないため CI でも実行可能)"""

    def test_demux_all_splits_by_relative_ts_number(self):
        # 前半 26 スロットを相対 TS 1、後半 26 スロットを相対 TS 2 に割り当てたフレームを 2 周期分生成する
        table = [1] * 26 + [2] * 26
        stream = bytearray()
        for frame_sync in (0x1A86, 0x0579):
            stream += BuildTSMFHeaderPacket(table, frame_sync)
            for slot in range(TSMF_SLOT_COUNT):
                stream += BuildSlotPacket(slot)
        streams = TSMFDemultiplexer.demux_all(stream)
        assert set(streams.keys()) == {1, 2}
        assert len(streams[1]) == 26 * 2 * TS_PACKET_SIZE
        assert len(streams[2]) == 26 * 2 * TS_PACKET_SIZE
        # 振り分け先が相対 TS 番号テーブルの順序通りであること (marker で確認)
        assert streams[1][4] == 0 and streams[2][4] == 26

    def test_empty_slots_are_dropped(self):
        # 相対 TS 番号 0 (空きスロット) のパケットは破棄される
        table = [1] * 10 + [0] * 42
        stream = BuildTSMFHeaderPacket(table) + b''.join(BuildSlotPacket(i) for i in range(TSMF_SLOT_COUNT))
        streams = TSMFDemultiplexer.demux_all(bytearray(stream))
        assert set(streams.keys()) == {1}
        assert len(streams[1]) == 10 * TS_PACKET_SIZE

    def test_frame_sync_lost_drops_excess_packets(self):
        # ヘッダなしで 52 スロットを超えた分のパケットは破棄される (フレーム同期ロスト)
        table = [1] * 52
        stream = BuildTSMFHeaderPacket(table) + b''.join(BuildSlotPacket(i) for i in range(60))
        streams = TSMFDemultiplexer.demux_all(bytearray(stream))
        assert len(streams[1]) == TSMF_SLOT_COUNT * TS_PACKET_SIZE

    def test_invalid_frame_sync_is_ignored(self):
        # フレーム同期信号が不正なヘッダは無視され、テーブルは更新されない
        table = [1] * 52
        stream = BuildTSMFHeaderPacket(table, frame_sync=0x1234) + b''.join(BuildSlotPacket(i) for i in range(52))
        streams = TSMFDemultiplexer.demux_all(bytearray(stream))
        assert streams == {}

    def test_packets_before_first_header_are_dropped(self):
        # 最初のヘッダより前のパケットは同期が取れないため破棄される
        table = [3] * 52
        stream = (
            b''.join(BuildSlotPacket(i) for i in range(10)) + BuildTSMFHeaderPacket(table) + b''.join(BuildSlotPacket(i) for i in range(52))
        )
        streams = TSMFDemultiplexer.demux_all(bytearray(stream))
        assert set(streams.keys()) == {3}
        assert len(streams[3]) == TSMF_SLOT_COUNT * TS_PACKET_SIZE

    def test_demux_stream_matches_demux_all(self):
        # チャンク境界がパケット境界と一致しなくても demux_all と同じ結果になる
        table = [1] * 26 + [2] * 26
        stream = bytearray()
        for frame_sync in (0x1A86, 0x0579, 0x1A86):
            stream += BuildTSMFHeaderPacket(table, frame_sync)
            for slot in range(TSMF_SLOT_COUNT):
                stream += BuildSlotPacket(slot)
        expected = TSMFDemultiplexer.demux_all(stream)[1]
        chunks = [bytes(stream[i : i + 1000]) for i in range(0, len(stream), 1000)]
        actual = b''.join(TSMFDemultiplexer().demux_stream(chunks, 1))
        assert actual == bytes(expected)

    def test_detect_carrier_type_synthetic(self):
        table = [1] * 52
        tsmf_stream = bytearray()
        for frame_sync in (0x1A86, 0x0579):
            tsmf_stream += BuildTSMFHeaderPacket(table, frame_sync)
            tsmf_stream += b''.join(BuildSlotPacket(i) for i in range(TSMF_SLOT_COUNT))
        assert TSMFDemultiplexer.detect_carrier_type(tsmf_stream) == CarrierType.TSMF
        assert TSMFDemultiplexer.detect_carrier_type(b'') == CarrierType.Empty


from __future__ import annotations

from collections.abc import Iterable, Iterator

from isdb_scanner.catv.constants import CarrierType


# TS パケットのサイズ (バイト)
TS_PACKET_SIZE = 188
# TS パケットの同期バイト
TS_SYNC_BYTE = 0x47
# TSMF 多重フレームヘッダの PID (JCTEA STD-002)
TSMF_HEADER_PID = 0x002F
# TSMF フレーム同期信号 (13bit) の有効値 (フレームごとにビット反転した 2 値が交互に現れる)
TSMF_FRAME_SYNC_WORDS = (0x1A86, 0x0579)
# 1 フレームあたりのスロット数 (ヘッダ 1 パケット + スロット 52 パケット = 53 パケット周期)
TSMF_SLOT_COUNT = 52
# TLV セル (4K/8K MMT 放送) の PID
TLV_CELL_PID = 0x002D
# NULL パケットの PID
NULL_PID = 0x1FFF


def GetPID(packet: bytes | bytearray | memoryview) -> int:
    """TS パケットから PID を取得する"""
    return ((packet[1] & 0x1F) << 8) | packet[2]


class TSMFDemultiplexer:
    """
    TSMF (Transport Stream Multiplexing Frame / JCTEA STD-002) の分離処理
    1 フレーム = 多重フレームヘッダパケット (PID 0x002F) 1 個 + スロットパケット 52 個の 53 パケット周期で構成され、
    ヘッダ内の相対 TS 番号テーブル (4bit × 52 スロット) に従って各スロットを元の TS に振り分ける
    """

    def __init__(self) -> None:
        # 各スロット (0-51) に割り当てられた相対 TS 番号 (0: 空きスロット / 1-15: 多重されている TS の識別番号)
        self._relative_stream_numbers: list[int] = []
        # 現在のフレーム内で処理済みのスロット数 (-1: フレーム未同期)
        self._slot_counter: int = -1
        # これまでに検出した有効な多重フレームヘッダの数
        self.header_count: int = 0

    def feed(self, packet: bytes | bytearray | memoryview) -> int | None:
        """
        TS パケットを 1 つ処理し、そのパケットが属する相対 TS 番号 (1-15) を返す
        多重フレームヘッダ・フレーム未同期・フレーム範囲外・空きスロットのパケットでは None を返す
        """
        if GetPID(packet) == TSMF_HEADER_PID:
            # フレーム同期信号を検証し、無効なヘッダは読み捨てる
            frame_sync = ((packet[4] & 0x1F) << 8) | packet[5]
            if frame_sync not in TSMF_FRAME_SYNC_WORDS:
                return None
            # 相対 TS 番号テーブル (オフセット 73-98 の 26 バイト、1 バイトに上位/下位ニブルで 2 スロット分) を読み込む
            table: list[int] = []
            for index in range(26):
                value = packet[73 + index]
                table.append((value & 0xF0) >> 4)
                table.append(value & 0x0F)
            self._relative_stream_numbers = table
            self._slot_counter = 0
            self.header_count += 1
            return None

        # フレーム未同期、または 52 スロットを使い切った後に次のヘッダが来ていない (フレーム同期ロスト) 場合は読み捨てる
        if self._slot_counter < 0 or self._slot_counter > TSMF_SLOT_COUNT - 1:
            return None

        self._slot_counter += 1
        relative_ts_number = self._relative_stream_numbers[self._slot_counter - 1]
        if relative_ts_number == 0:
            return None
        return relative_ts_number

    @staticmethod
    def demux_all(ts_stream: bytes | bytearray) -> dict[int, bytearray]:
        """
        188 バイト境界に整列済みの TS ストリームから、多重されている全相対 TS (1-15) を一括分離する (スキャン用)
        戻り値は相対 TS 番号 → 分離後 TS ストリームの辞書 (TSMF ヘッダが見つからない場合は空辞書)
        """
        demultiplexer = TSMFDemultiplexer()
        streams: dict[int, bytearray] = {}
        view = memoryview(ts_stream)
        for offset in range(0, len(ts_stream) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
            packet = view[offset : offset + TS_PACKET_SIZE]
            if packet[0] != TS_SYNC_BYTE:
                continue
            relative_ts_number = demultiplexer.feed(packet)
            if relative_ts_number is not None:
                streams.setdefault(relative_ts_number, bytearray()).extend(packet)
        return streams

    def demux_stream(self, chunks: Iterable[bytes], relative_ts_number: int) -> Iterator[bytes]:
        """
        任意サイズのチャンク列から指定した相対 TS のパケットのみを取り出すストリーミング処理 (パイプフィルタ用)
        188 バイト境界の同期回復 (次パケット先頭の同期バイト確認) 付き
        """
        buffer = bytearray()
        for chunk in chunks:
            buffer.extend(chunk)
            offset = 0
            while len(buffer) - offset >= TS_PACKET_SIZE:
                if buffer[offset] != TS_SYNC_BYTE:
                    # 同期回復: 次の同期バイト候補までスキップする
                    next_sync = buffer.find(TS_SYNC_BYTE, offset + 1)
                    if next_sync == -1:
                        offset = len(buffer)
                        break
                    offset = next_sync
                    continue
                # 次パケットの先頭も確認できる場合は、同期バイトが連続していることを確かめてから処理する
                if len(buffer) - offset > TS_PACKET_SIZE and buffer[offset + TS_PACKET_SIZE] != TS_SYNC_BYTE:
                    offset += 1
                    continue
                packet = bytes(buffer[offset : offset + TS_PACKET_SIZE])
                if self.feed(packet) == relative_ts_number:
                    yield packet
                offset += TS_PACKET_SIZE
            del buffer[:offset]

    @staticmethod
    def detect_carrier_type(ts_stream: bytes | bytearray) -> CarrierType:
        """
        受信した TS ストリームからキャリア種別を判定する
        TLV キャリアも TSMF フレーム構造を持つ (スロットの中身が TLV セル) ため、TLV 判定を TSMF 判定より優先する
        """
        total_count = 0
        tsmf_header_count = 0
        tlv_cell_count = 0
        null_count = 0
        view = memoryview(ts_stream)
        for offset in range(0, len(ts_stream) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
            packet = view[offset : offset + TS_PACKET_SIZE]
            if packet[0] != TS_SYNC_BYTE:
                continue
            total_count += 1
            pid = GetPID(packet)
            if pid == TSMF_HEADER_PID:
                frame_sync = ((packet[4] & 0x1F) << 8) | packet[5]
                if frame_sync in TSMF_FRAME_SYNC_WORDS:
                    tsmf_header_count += 1
            elif pid == TLV_CELL_PID:
                tlv_cell_count += 1
            elif pid == NULL_PID:
                null_count += 1
        if total_count == 0:
            return CarrierType.Empty
        if tlv_cell_count / total_count > 0.5:
            return CarrierType.TLV
        if tsmf_header_count >= 2:
            return CarrierType.TSMF
        if null_count / total_count > 0.97:
            return CarrierType.Empty
        return CarrierType.SingleTS

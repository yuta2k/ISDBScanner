#!/usr/bin/env python3

# isdb-tsmf-split: TSMF (JCTEA STD-002) 多重ストリームから指定した相対 TS を取り出す標準入出力フィルタ
# TSMF 非対応のレコーダー (mirakc / EDCB など) でも、チューナーコマンドの後段にパイプで挟むことで
# CATV トランスモジュレーションの多重チャンネルを選局できるようにする
#
# 使用例:
#   dvbv5-zap -c dvbv5_channels.conf -P -o - -t 30 CATV_15 | isdb-tsmf-split --rel-ts 1 > output.ts
#   cat multiplexed.ts | isdb-tsmf-split --list

import argparse
import json
import sys

from isdb_scanner.catv.tsmf import TS_PACKET_SIZE, TSMFDemultiplexer


# 標準出力への書き込みをまとめるバッファサイズ (バイト)
OUTPUT_BUFFER_SIZE = TS_PACKET_SIZE * 512


def main() -> None:
    parser = argparse.ArgumentParser(
        prog='isdb-tsmf-split',
        description='TSMF (JCTEA STD-002) 多重ストリームから指定した相対 TS を取り出す標準入出力フィルタ',
    )
    parser.add_argument(
        '--rel-ts',
        '-r',
        type=int,
        choices=range(1, 16),
        metavar='{1-15}',
        help='取り出す相対 TS 番号',
    )
    parser.add_argument(
        '--list',
        action='store_true',
        help='標準入力をすべて読み込み、キャリア種別と相対 TS ごとのパケット数を JSON で表示する',
    )
    args = parser.parse_args()

    if args.list is True:
        ts_stream = sys.stdin.buffer.read()
        carrier_type = TSMFDemultiplexer.detect_carrier_type(ts_stream)
        streams = TSMFDemultiplexer.demux_all(ts_stream)
        result = {
            'carrier_type': carrier_type.value,
            'total_packets': len(ts_stream) // TS_PACKET_SIZE,
            'relative_ts': {str(rel): len(stream) // TS_PACKET_SIZE for rel, stream in sorted(streams.items())},
        }
        print(json.dumps(result, ensure_ascii=False, indent=4))
        return

    if args.rel_ts is None:
        parser.error('--rel-ts か --list のどちらかを指定してください')

    def read_chunks():
        while True:
            chunk = sys.stdin.buffer.read(OUTPUT_BUFFER_SIZE)
            if not chunk:
                break
            yield chunk

    demultiplexer = TSMFDemultiplexer()
    output_buffer = bytearray()
    try:
        for packet in demultiplexer.demux_stream(read_chunks(), args.rel_ts):
            output_buffer.extend(packet)
            if len(output_buffer) >= OUTPUT_BUFFER_SIZE:
                sys.stdout.buffer.write(output_buffer)
                output_buffer.clear()
        if len(output_buffer) > 0:
            sys.stdout.buffer.write(output_buffer)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        # 後段のパイプが先に閉じられた場合や Ctrl+C は正常終了扱いにする
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)


if __name__ == '__main__':
    main()

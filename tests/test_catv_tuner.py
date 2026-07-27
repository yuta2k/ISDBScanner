import os
import stat
import struct
import time
from pathlib import Path

import pytest

from isdb_scanner.catv.tuner import (
    CARRIER_TYPE_DETECTION_MAX_SIZE,
    FE_SCALE_COUNTER,
    FE_SCALE_DECIBEL,
    FE_SCALE_NOT_AVAILABLE,
    FE_SCALE_RELATIVE,
    MIN_OUTPUT_SIZE,
    TS_PACKET_SIZE,
    CATVTuner,
    ShouldExtendRecordingForTLV,
)
from isdb_scanner.tuner import TunerOutputError, TunerTuningError


def BuildDtvFeStatsBytes(entries: list[tuple[int, int]]) -> bytes:
    """
    テスト用に dtv_fe_stats 構造体相当の生バイト列を組み立てる
    entries は (scale, value) のタプルのリスト (len は entries の要素数になる)
    scale が FE_SCALE_DECIBEL の場合は符号付き64bit、それ以外は符号なし64bit として value をパックする
    """

    raw = struct.pack('<B', len(entries))
    for scale, value in entries:
        if scale == FE_SCALE_DECIBEL:
            raw += struct.pack('<Bq', scale, value)
        else:
            raw += struct.pack('<BQ', scale, value)
    return raw


# 実機 (dvbv5-zap) には依存せず、PATH 上に設置した偽の dvbv5-zap で CATVTuner.tune() の正常系/異常系を検証する
# 実機 (Digital Devices Max M4) で確認済みの dvbv5-zap の挙動は以下の通り:
#   - `-o -` (標準出力への出力) は `-o <file>` と完全に同一のバイト列を出力する
#   - ロックに成功すると `-t <seconds>` で指定した秒数だけ TS を出力して exit code 0 で終了する
#   - ロックに失敗すると stderr に "frontend doesn't lock" を出力して exit code 1 で終了する
#   - ロック失敗中に SIGINT を送ると同様に "frontend doesn't lock" を出力してすぐに正常終了する
# 偽の dvbv5-zap はこれらの挙動をチャンネル名で分岐して模擬する
FAKE_DVBV5_ZAP_SOURCE = """#!/usr/bin/env python3
import signal
import sys
import time

args = sys.argv[1:]
channel = args[-1]
recording_time = float(args[args.index("-t") + 1])
output = args[args.index("-o") + 1]

interrupted = {"flag": False}


def handler(signum, frame):
    interrupted["flag"] = True


signal.signal(signal.SIGINT, handler)

# テストの都合上、実在するチャンネル名の一部を「ロックできないチャンネル」「受信データが小さすぎるチャンネル」
# 「-t の秒数だけ TS を出力し続けるチャンネル (TLV / TLV 以外)」に予約して振る舞いを模擬する
# (CATVTuner.tune() は周波数プランに存在しないチャンネル名を dvbv5-zap 起動前に弾いてしまうため、実在名を使う必要がある)
FAKE_TIMEOUT_CHANNEL = "CATV_C63"
FAKE_SMALL_OUTPUT_CHANNEL = "CATV_C13"
FAKE_TLV_STREAM_CHANNEL = "CATV_C14"
FAKE_TS_STREAM_CHANNEL = "CATV_C15"

# ロックできない (実機での "frontend doesn't lock" 相当) チャンネルを模擬する
if channel == FAKE_TIMEOUT_CHANNEL:
    waited = 0.0
    while waited < recording_time and not interrupted["flag"]:
        time.sleep(0.02)
        waited += 0.02
    sys.stderr.write("frontend does not lock\\n")
    sys.exit(1)

# 実機同様、-t で指定された秒数だけ TS を出力し続け、SIGINT を受け取ったらその時点で出力を打ち切って正常終了する
# チャンネルを模擬する (収録時間の延長 / 打ち切りの検証用)
if channel in (FAKE_TLV_STREAM_CHANNEL, FAKE_TS_STREAM_CHANNEL):
    # TLV キャリアは TLV セル (PID 0x002D)、それ以外は通常の PID (0x0100) のパケットを出力し続ける
    pid = 0x002D if channel == FAKE_TLV_STREAM_CHANNEL else 0x0100
    packet = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10]) + bytes(184)
    chunk = packet * 200
    started = time.time()
    while time.time() - started < recording_time and not interrupted["flag"]:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        time.sleep(0.01)
    sys.exit(0)

# 受信データが小さすぎる (選局失敗扱いになる) チャンネルを模擬する
if channel == FAKE_SMALL_OUTPUT_CHANNEL:
    data = bytes(1024)
# それ以外は正常にロックできたものとして、TS パケットっぽいダミーデータを出力する
else:
    data = (bytes([0x47]) + bytes(187)) * 4000

if output == "-":
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
else:
    with open(output, "wb") as f:
        f.write(data)
sys.exit(0)
"""


@pytest.fixture
def fake_dvbv5_zap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH 上に偽の dvbv5-zap コマンドを設置する (実機なしで CATVTuner.tune() を検証するため)"""

    script_path = tmp_path / 'dvbv5-zap'
    script_path.write_text(FAKE_DVBV5_ZAP_SOURCE, encoding='utf-8')
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')
    return script_path


class TestCATVTunerAvailableTuners:
    def test_no_dvb_devices_returns_empty_list(self):
        # このテストを実行するホストには /dev/dvb が存在しない (CI・通常の開発環境を想定)
        # チューナーが接続された実機環境でなくても例外を送出せず、単に空リストを返すことを確認する
        assert Path('/dev/dvb').exists() is False
        assert CATVTuner.getAvailableCATVTuners() == []


class TestCATVTunerConfFile:
    def test_conf_file_contains_expected_channels(self):
        conf_path = CATVTuner._getConfFilePath()
        assert conf_path.is_file()
        content = conf_path.read_text(encoding='utf-8')
        assert '[CATV_15]' in content
        assert 'FREQUENCY = 485000000' in content
        assert 'DELIVERY_SYSTEM = DVBC/ANNEX_A' in content
        assert 'SYMBOL_RATE = 5274000' in content
        assert 'MODULATION = QAM/AUTO' in content
        assert '[CATV_C22]' in content
        assert 'FREQUENCY = 167000000' in content

    def test_conf_file_is_cached(self):
        # 2回目以降の呼び出しでは同一ファイルが返される (毎回 tempfile を生成し直さない)
        first = CATVTuner._getConfFilePath()
        second = CATVTuner._getConfFilePath()
        assert first == second


class TestCATVTunerTune:
    def test_tune_success(self, fake_dvbv5_zap: Path):
        tuner = CATVTuner(0)
        data = tuner.tune('CATV_20', recording_time=1.0, tune_timeout=5.0)
        assert len(data) == TS_PACKET_SIZE * 4000

    def test_tune_timeout_raises_tuner_tuning_error(self, fake_dvbv5_zap: Path):
        tuner = CATVTuner(0)
        with pytest.raises(TunerTuningError):
            tuner.tune('CATV_C63', recording_time=5.0, tune_timeout=0.3)

    def test_tune_small_output_raises_tuner_output_error(self, fake_dvbv5_zap: Path):
        tuner = CATVTuner(0)
        with pytest.raises(TunerOutputError):
            tuner.tune('CATV_C13', recording_time=1.0, tune_timeout=5.0)

    def test_tune_unknown_channel_raises_immediately(self, fake_dvbv5_zap: Path):
        # 周波数プランに存在しない物理チャンネル名は、dvbv5-zap を起動する前に弾かれる
        tuner = CATVTuner(0)
        with pytest.raises(TunerTuningError):
            tuner.tune('CATV_NOT_A_REAL_CHANNEL')

    def test_tune_extends_recording_for_tlv_carrier(self, fake_dvbv5_zap: Path):
        # TLV キャリアと判定された場合は dvbv5-zap を止めずに走らせ続け、合計 tlv_recording_time 秒まで収録が延長される
        tuner = CATVTuner(0)
        start_time = time.time()
        data = tuner.tune('CATV_C14', recording_time=0.4, tune_timeout=5.0, tlv_recording_time=1.2)
        elapsed = time.time() - start_time
        assert tuner.last_recording_extended is True
        # 延長されているので recording_time (0.4 秒) では終わらない (タイミングのぶれを考慮して 1.0 秒で判定する)
        assert elapsed >= 1.0
        assert len(data) > MIN_OUTPUT_SIZE

    def test_tune_does_not_extend_recording_for_non_tlv_carrier(self, fake_dvbv5_zap: Path):
        # TLV キャリアでないと判定された場合は recording_time 経過時点で SIGINT により収録を打ち切る
        # (dvbv5-zap には tlv_recording_time (3.0 秒) を `-t` に指定して起動しているが、そこまでは待たない)
        tuner = CATVTuner(0)
        start_time = time.time()
        data = tuner.tune('CATV_C15', recording_time=0.4, tune_timeout=5.0, tlv_recording_time=3.0)
        elapsed = time.time() - start_time
        assert tuner.last_recording_extended is False
        assert elapsed < 2.0
        assert len(data) > MIN_OUTPUT_SIZE

    def test_tune_does_not_extend_when_tlv_recording_time_is_not_longer(self, fake_dvbv5_zap: Path):
        # tlv_recording_time が recording_time 以下の場合、TLV キャリアでも延長しない (バリデーションエラーにもしない)
        tuner = CATVTuner(0)
        start_time = time.time()
        data = tuner.tune('CATV_C14', recording_time=0.4, tune_timeout=5.0, tlv_recording_time=0.4)
        elapsed = time.time() - start_time
        assert tuner.last_recording_extended is False
        assert elapsed < 2.0
        assert len(data) > MIN_OUTPUT_SIZE

    def test_tune_with_collect_signal_stats_does_not_raise(self, fake_dvbv5_zap: Path):
        # このテストを実行するホストには /dev/dvb が存在しないため、信号品質統計の取得自体は失敗する (last_signal_stats は None のまま)
        # が、collect_signal_stats=True を指定しても tune() 自体は例外を送出せず正常に完了することを確認する
        assert Path('/dev/dvb').exists() is False
        tuner = CATVTuner(0)
        data = tuner.tune('CATV_20', recording_time=1.0, tune_timeout=5.0, collect_signal_stats=True)
        assert len(data) == TS_PACKET_SIZE * 4000
        assert tuner.last_signal_stats is None


def BuildSyntheticTSStream(pid: int, packet_count: int) -> bytes:
    """テスト用に、指定した PID の TS パケットだけが並んだ合成 TS ストリームを組み立てる"""

    packet = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10]) + bytes(TS_PACKET_SIZE - 4)
    return packet * packet_count


class TestShouldExtendRecordingForTLV:
    """
    収録時間の延長判定 (収録途中の受信データからキャリア種別を判定し、TLV キャリアのみ延長する) の単体テスト
    実機・実受信データには依存せず、PID だけを組み立てた合成 TS ストリームで検証する
    """

    # TLV セル (PID 0x002D) / 通常の TS パケット (PID 0x0100) / NULL パケット (PID 0x1FFF)
    TLV_CELL_PID = 0x002D
    NORMAL_PID = 0x0100
    NULL_PID = 0x1FFF

    def test_tlv_stream_should_be_extended(self):
        ts_stream = BuildSyntheticTSStream(self.TLV_CELL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 20.0) is True

    def test_non_tlv_stream_should_not_be_extended(self):
        ts_stream = BuildSyntheticTSStream(self.NORMAL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 20.0) is False

    def test_empty_stream_should_not_be_extended(self):
        assert ShouldExtendRecordingForTLV(b'', 10.0, 20.0) is False

    def test_null_packet_only_stream_should_not_be_extended(self):
        # 何も多重されていない (NULL パケットだけの) Empty キャリアでは延長しない
        ts_stream = BuildSyntheticTSStream(self.NULL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 20.0) is False

    def test_few_tlv_cells_should_not_be_extended(self):
        # ノイズ由来などで TLV セルがごく少数しか含まれない場合は TLV キャリアとみなさない
        ts_stream = BuildSyntheticTSStream(self.TLV_CELL_PID, 10) + BuildSyntheticTSStream(self.NORMAL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 20.0) is False

    def test_not_extended_when_tlv_recording_time_is_shorter(self):
        # TLV キャリアでも、延長後の秒数が通常の録画時間より短ければ延長しない
        ts_stream = BuildSyntheticTSStream(self.TLV_CELL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 5.0) is False

    def test_not_extended_when_tlv_recording_time_is_equal(self):
        # TLV キャリアでも、延長後の秒数が通常の録画時間と同じなら延長しない
        ts_stream = BuildSyntheticTSStream(self.TLV_CELL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 10.0) is False

    def test_not_extended_when_tlv_recording_time_is_none(self):
        ts_stream = BuildSyntheticTSStream(self.TLV_CELL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, None) is False

    def test_only_the_head_of_the_stream_is_used_for_detection(self):
        # 判定に使うのは先頭 CARRIER_TYPE_DETECTION_MAX_SIZE バイトまでのため、それ以降が TLV セルでも判定は変わらない
        # (先頭が TLV セルでない = 延長不要と判定される)
        head_packet_count = CARRIER_TYPE_DETECTION_MAX_SIZE // TS_PACKET_SIZE + 1
        ts_stream = BuildSyntheticTSStream(self.NORMAL_PID, head_packet_count) + BuildSyntheticTSStream(self.TLV_CELL_PID, 1000)
        assert ShouldExtendRecordingForTLV(ts_stream, 10.0, 20.0) is False


class TestCATVTunerGetSignalStats:
    def test_get_signal_stats_returns_none_when_device_missing(self):
        # /dev/dvb が存在しない (フロントエンドをオープンできない) 環境では None を返す
        assert Path('/dev/dvb').exists() is False
        tuner = CATVTuner(0)
        assert tuner.getSignalStats() is None


class TestCATVTunerParseDtvFeStats:
    """
    _parseDtvFeStats() (ioctl (FE_GET_PROPERTY / DTV_STAT_*) で得られる dtv_fe_stats 構造体の生バイト列パース部分) の
    単体テスト。ioctl 呼び出し自体は実機 (DVB フロントエンドデバイス) に依存するため、パース処理だけを合成バイト列で検証する
    """

    def test_empty_bytes_returns_empty_list(self):
        assert CATVTuner._parseDtvFeStats(b'') == []

    def test_decibel_scale_negative_value(self):
        # 信号強度 -45.678 dBm 相当 (0.001dB 単位の符号付き64bit) をパースできること
        raw = BuildDtvFeStatsBytes([(FE_SCALE_DECIBEL, -45678)])
        assert CATVTuner._parseDtvFeStats(raw) == [(FE_SCALE_DECIBEL, -45678)]

    def test_relative_scale_value(self):
        # 0-65535 の相対値 (符号なし) をパースできること
        raw = BuildDtvFeStatsBytes([(FE_SCALE_RELATIVE, 45000)])
        assert CATVTuner._parseDtvFeStats(raw) == [(FE_SCALE_RELATIVE, 45000)]

    def test_counter_scale_large_64bit_value(self):
        # 32bit を超える積算カウンタ値でも符号なし64bitとして正しくパースできること
        large_value = 12_345_678_901
        raw = BuildDtvFeStatsBytes([(FE_SCALE_COUNTER, large_value)])
        assert CATVTuner._parseDtvFeStats(raw) == [(FE_SCALE_COUNTER, large_value)]

    def test_multiple_stats_entries(self):
        # ISDB のような階層伝送対応ドライバでは stat[0] (グローバル値) 以降に各階層の値が続く
        raw = BuildDtvFeStatsBytes([(FE_SCALE_DECIBEL, -12345), (FE_SCALE_NOT_AVAILABLE, 0)])
        assert CATVTuner._parseDtvFeStats(raw) == [(FE_SCALE_DECIBEL, -12345), (FE_SCALE_NOT_AVAILABLE, 0)]

    def test_truncated_bytes_stops_gracefully(self):
        # len が 2 を主張していても、実際のバイト列が1件分しかなければ1件だけ返してそこで打ち切る
        raw = struct.pack('<B', 2) + struct.pack('<Bq', FE_SCALE_DECIBEL, -100)
        assert CATVTuner._parseDtvFeStats(raw) == [(FE_SCALE_DECIBEL, -100)]

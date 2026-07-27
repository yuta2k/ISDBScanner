import os
import stat
from pathlib import Path

import pytest

from isdb_scanner.catv.native_tuner import AsRobustISDBTuners, RobustISDBTuner
from isdb_scanner.tuner import TunerTuningError


# 偽の recisdb コマンド (実機なしで RobustISDBTuner のウォッチドッグを検証するため)
# 環境変数 FAKE_RECISDB_MODE で挙動を切り替える:
#   success:    150KB の TS 風データを出力して正常終了する
#   stall:      200KB を出力した後、ストリーム停止を模して SIGINT/SIGTERM を無視して待ち続ける
#   no-output:  何も出力せず待ち続ける (SIGINT では終了する)
#   silent-signal: checksignal 用。信号レベルを一切出力せず待ち続ける
FAKE_RECISDB_SOURCE = """#!/bin/sh
mode="${FAKE_RECISDB_MODE:-success}"
case "$mode" in
  success)
    dd if=/dev/zero bs=1024 count=150 2>/dev/null
    exit 0
    ;;
  stall)
    dd if=/dev/zero bs=1024 count=200 2>/dev/null
    trap '' INT TERM
    while :; do sleep 1; done
    ;;
  no-output)
    trap 'exit 1' INT
    while :; do sleep 1; done
    ;;
  silent-signal)
    trap 'exit 1' INT
    while :; do sleep 1; done
    ;;
esac
"""


@pytest.fixture
def fake_recisdb_tuner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RobustISDBTuner:
    """PATH 上に偽 recisdb を設置し、__init__ (実デバイス検証) をバイパスした RobustISDBTuner を返す"""

    script_path = tmp_path / 'recisdb'
    script_path.write_text(FAKE_RECISDB_SOURCE, encoding='utf-8')
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')

    # 実デバイスがないため __init__ を通さず、tune() が参照する属性のみを設定する
    tuner = RobustISDBTuner.__new__(RobustISDBTuner)
    tuner._device_path = Path('/dev/null')  # type: ignore[attr-defined]
    tuner.lnb = None
    tuner.output_recisdb_log = False
    return tuner


class TestRobustISDBTunerTune:
    """RobustISDBTuner.tune() のウォッチドッグ (ストリーム停止検出) のテスト (CI でも実行可能)"""

    def test_success_returns_data(self, fake_recisdb_tuner: RobustISDBTuner, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv('FAKE_RECISDB_MODE', 'success')
        data = fake_recisdb_tuner.tune('T13', recording_time=0.5, tune_timeout=2.0)
        assert len(data) >= 100 * 1024

    def test_stalled_stream_raises_within_deadline(self, fake_recisdb_tuner: RobustISDBTuner, monkeypatch: pytest.MonkeyPatch):
        # データが届いた後にストリームが停止した場合、upstream 実装では無限にハングするが、
        # RobustISDBTuner では tune_timeout + recording_time + STALL_TIMEOUT_MARGIN で打ち切られること
        # (偽 recisdb は SIGINT/SIGTERM を無視するため、SIGKILL エスカレーションの動作も同時に検証される)
        monkeypatch.setenv('FAKE_RECISDB_MODE', 'stall')
        monkeypatch.setattr(RobustISDBTuner, 'STALL_TIMEOUT_MARGIN', 1.0)
        import time

        start_time = time.monotonic()
        with pytest.raises(TunerTuningError, match='stalled'):
            fake_recisdb_tuner.tune('T22', recording_time=0.5, tune_timeout=0.5)
        # 全体上限 (0.5 + 0.5 + 1.0 = 2.0 秒) + SIGKILL エスカレーション (10 秒) + 余裕で確実に打ち切られていること
        assert time.monotonic() - start_time < 15.0

    def test_no_output_raises_tune_timeout(self, fake_recisdb_tuner: RobustISDBTuner, monkeypatch: pytest.MonkeyPatch):
        # データが一切届かない場合は upstream 同様、選局タイムアウトとして扱われること
        monkeypatch.setenv('FAKE_RECISDB_MODE', 'no-output')
        with pytest.raises(TunerTuningError, match='timed out'):
            fake_recisdb_tuner.tune('T22', recording_time=0.5, tune_timeout=0.5)


class TestRobustISDBTunerSignalLevel:
    """RobustISDBTuner.getSignalLevelMean() のタイムアウトのテスト (CI でも実行可能)"""

    def test_silent_checksignal_returns_none(self, fake_recisdb_tuner: RobustISDBTuner, monkeypatch: pytest.MonkeyPatch):
        # 信号レベルが一切出力されない場合、upstream 実装では stdout.read(1) で無限にハングするが、
        # RobustISDBTuner では SIGNAL_LEVEL_TIMEOUT で打ち切られ None が返ること
        monkeypatch.setenv('FAKE_RECISDB_MODE', 'silent-signal')
        monkeypatch.setattr(RobustISDBTuner, 'SIGNAL_LEVEL_TIMEOUT', 1.0)
        import time

        start_time = time.monotonic()
        assert fake_recisdb_tuner.getSignalLevelMean('T22') is None
        assert time.monotonic() - start_time < 15.0


class TestAsRobustISDBTuners:
    """AsRobustISDBTuners() のテスト (実デバイス不要な範囲のみ)"""

    def test_empty_list(self):
        assert AsRobustISDBTuners([]) == []

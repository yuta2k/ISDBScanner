import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from isdb_scanner.catv.scan import app
from isdb_scanner.catv.tuner import CATVTuner
from isdb_scanner.tuner import TunerOpeningError

# 実機 (dvbv5-zap) には依存せず、PATH 上に設置した偽の dvbv5-zap でスキャンを検証するためのソースを流用する
# (このモジュール内のフィクスチャは実機・受信環境に一切依存しない合成データのみで構成されている)
from tests.test_catv_tuner import FAKE_DVBV5_ZAP_SOURCE


runner = CliRunner()

# 偽の dvbv5-zap が「ロックできない (受信不可)」「受信データが小さすぎる」チャンネルとして予約している物理チャンネル
# (test_catv_tuner.py の FAKE_DVBV5_ZAP_SOURCE と対応させる。周波数プラン上の一般名であり受信環境固有の情報ではない)
FAKE_TIMEOUT_CHANNEL = 'CATV_C63'
FAKE_SMALL_OUTPUT_CHANNEL = 'CATV_C13'


@pytest.fixture
def fake_dvbv5_zap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH 上に偽の dvbv5-zap コマンドを設置する (実機なしで scan を検証するため)"""

    script_path = tmp_path / 'dvbv5-zap'
    script_path.write_text(FAKE_DVBV5_ZAP_SOURCE, encoding='utf-8')
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')
    return script_path


def PatchAvailableTuners(monkeypatch: pytest.MonkeyPatch, tuners: list[CATVTuner]) -> None:
    """CATVTuner.getAvailableCATVTuners() を、指定した (合成) チューナーリストを返すよう差し替える"""

    monkeypatch.setattr(CATVTuner, 'getAvailableCATVTuners', lambda **kwargs: tuners)


class TestScanTunerResolution:
    """--adapter / --adapters / --parallel の解決・検証ロジックのテスト (実選局は行わない)"""

    def test_adapters_duplicate_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        result = runner.invoke(app, [str(tmp_path / 'out'), '--adapters', '0,0'])
        assert result.exit_code == 1
        assert 'duplicate adapter numbers' in result.output

    def test_adapters_unknown_adapter_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        result = runner.invoke(app, [str(tmp_path / 'out'), '--adapters', '0,5'])
        assert result.exit_code == 1
        assert 'Adapter 5 was not found' in result.output

    def test_adapters_non_integer_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        result = runner.invoke(app, [str(tmp_path / 'out'), '--adapters', '0,x'])
        assert result.exit_code == 1
        assert 'comma-separated list of integers' in result.output

    def test_adapter_with_adapters_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        result = runner.invoke(app, [str(tmp_path / 'out'), '--adapter', '0', '--adapters', '0,1'])
        assert result.exit_code == 1
        assert '--adapter cannot be combined with --adapters' in result.output


class TestScanParallelIntegration:
    """偽 dvbv5-zap + 偽チューナー 2 台による --parallel スキャンの統合テスト"""

    def test_parallel_scan_processes_all_channels(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        output_dir = tmp_path / 'out'

        # 受信可能な 2 チャンネルと、ロック失敗 (受信不可)・受信データ過小の 2 チャンネルを混在させてスキャンする
        target_channels = ['CATV_16', 'CATV_15', FAKE_TIMEOUT_CHANNEL, FAKE_SMALL_OUTPUT_CHANNEL]
        result = runner.invoke(
            app,
            [
                str(output_dir),
                '--parallel',
                '--channels',
                ','.join(target_channels),
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-diff',
            ],
        )

        assert result.exit_code == 0, result.output
        # (a) 全チャンネルが処理される: サマリの分母が対象チャンネル数と一致し、受信可能数は 2
        assert 'Scanned 2 / 4 channel(s). (2 receivable)' in result.output

        # (b) CATV.json は受信可能チャンネルのみを physical_channel 昇順で含む
        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert list(catv_json.keys()) == ['CATV_15', 'CATV_16']

        # (c) ロック失敗・受信データ過小のチャンネルは結果に含まれない
        assert FAKE_TIMEOUT_CHANNEL not in catv_json
        assert FAKE_SMALL_OUTPUT_CHANNEL not in catv_json


class TestScanTunerFailover:
    """スキャン中にチューナーがオープン失敗 (使用不能) になった場合のフェイルオーバーのテスト"""

    def _make_failing_tuner(self, adapter_number: int) -> CATVTuner:
        """tune() が必ず TunerOpeningError を送出する (=オープンできない) 偽チューナーを作る"""

        tuner = CATVTuner(adapter_number)

        def failing_tune(physical_channel: str, **kwargs) -> bytearray:
            raise TunerOpeningError('dvbv5-zap command not found.')

        # インスタンス属性として差し替えるため self は渡されない (physical_channel から始まるシグネチャにする)
        tuner.tune = failing_tune  # type: ignore[method-assign]
        return tuner

    def test_one_tuner_dies_other_completes_all_channels(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # adapter0 は常にオープン失敗、adapter1 は正常に選局できる → adapter1 が全チャンネルを消化して成功する
        dead_tuner = self._make_failing_tuner(0)
        alive_tuner = CATVTuner(1)
        PatchAvailableTuners(monkeypatch, [dead_tuner, alive_tuner])
        output_dir = tmp_path / 'out'

        target_channels = ['CATV_13', 'CATV_14', 'CATV_15', 'CATV_16']
        result = runner.invoke(
            app,
            [
                str(output_dir),
                '--parallel',
                '--channels',
                ','.join(target_channels),
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-diff',
            ],
        )

        assert result.exit_code == 0, result.output
        # 死んだチューナーは警告として表示され、スキャン自体は成功する
        assert 'adapter0 became unavailable during the scan' in result.output
        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert list(catv_json.keys()) == ['CATV_13', 'CATV_14', 'CATV_15', 'CATV_16']

    def test_all_tuners_die_aborts_with_exit_code_1(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # 全チューナーがオープン失敗すると、残チャンネルを消化できないためスキャンを打ち切る (exit code 1)
        PatchAvailableTuners(monkeypatch, [self._make_failing_tuner(0), self._make_failing_tuner(1)])
        output_dir = tmp_path / 'out'

        result = runner.invoke(
            app,
            [
                str(output_dir),
                '--parallel',
                '--channels',
                'CATV_13,CATV_14,CATV_15,CATV_16',
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-diff',
            ],
        )

        assert result.exit_code == 1
        assert 'All tuners became unavailable' in result.output


class TestScanDefaultAndNoParallel:
    """既定 (全チューナー並列) と --no-parallel (単一チューナー逐次) の経路のテスト"""

    def test_default_uses_all_detected_tuners(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # フラグ未指定でも既定で検出された全チューナーを使って並列スキャンする
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        output_dir = tmp_path / 'out'

        target_channels = ['CATV_16', 'CATV_15', FAKE_TIMEOUT_CHANNEL, FAKE_SMALL_OUTPUT_CHANNEL]
        result = runner.invoke(
            app,
            [
                str(output_dir),
                '--channels',
                ','.join(target_channels),
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-diff',
            ],
        )

        assert result.exit_code == 0, result.output
        # 複数チューナーが検出された場合は既定で 'Using tuners:' (複数台) 表示になる
        assert 'Using tuners:' in result.output
        assert 'Scanned 2 / 4 channel(s). (2 receivable)' in result.output
        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert list(catv_json.keys()) == ['CATV_15', 'CATV_16']

    def test_no_parallel_uses_single_tuner(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # --no-parallel を付けると複数チューナー検出時でも先頭 1 台のみで逐次スキャンする
        PatchAvailableTuners(monkeypatch, [CATVTuner(0), CATVTuner(1)])
        output_dir = tmp_path / 'out'

        target_channels = ['CATV_15', FAKE_TIMEOUT_CHANNEL, 'CATV_16']
        result = runner.invoke(
            app,
            [
                str(output_dir),
                '--no-parallel',
                '--channels',
                ','.join(target_channels),
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-diff',
            ],
        )

        assert result.exit_code == 0, result.output
        # 単一チューナー時は 'Using tuner:' 表示になる (複数台の 'Using tuners:' ではない)
        assert 'Using tuner:' in result.output
        assert 'Using tuners:' not in result.output
        assert 'Scanned 2 / 3 channel(s). (2 receivable)' in result.output

        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert list(catv_json.keys()) == ['CATV_15', 'CATV_16']
        assert FAKE_TIMEOUT_CHANNEL not in catv_json

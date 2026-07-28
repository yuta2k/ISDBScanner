import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from isdb_scanner.catv import scan as scan_module
from isdb_scanner.catv.cards import DetectedCard
from isdb_scanner.catv.formatter import CATVJSONFormatter, NativeJSONFormatter
from isdb_scanner.catv.scan import app
from isdb_scanner.catv.tuner import CATVTuner
from isdb_scanner.constants import ServiceInfo, TransportStreamInfo
from isdb_scanner.tuner import ISDBTuner, TunerOpeningError

# 実機 (dvbv5-zap) には依存せず、PATH 上に設置した偽の dvbv5-zap でスキャンを検証するためのソースを流用する
# (このモジュール内のフィクスチャは実機・受信環境に一切依存しない合成データのみで構成されている)
from tests.test_catv_card_assignment import (
    FAKE_BCAS_READER_NAME,
    FAKE_CCAS_READER_NAME,
    BuildBCASCard,
    BuildCCASCard,
)
from tests.test_catv_formatter import BuildSyntheticBSTsInfos, BuildSyntheticCarriers, BuildSyntheticCSTsInfos
from tests.test_catv_tuner import FAKE_DVBV5_ZAP_SOURCE


runner = CliRunner()


def PatchDetectedCards(
    monkeypatch: pytest.MonkeyPatch,
    detected_cards: list[DetectedCard],
    detection_error: str | None = None,
) -> None:
    """出力フェーズで呼ばれる DetectCASCards() を、指定した (合成) カード検出結果を返すよう差し替える"""

    monkeypatch.setattr(scan_module, 'DetectCASCards', lambda: (detected_cards, detection_error))


@pytest.fixture(autouse=True)
def no_real_card_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    既定では DetectCASCards() を「カードリーダー未接続」を返すフェイクに差し替える (全テストに自動適用)
    テストを実行するホストに実際のカードリーダー/CAS カードが接続されていても結果が変わらないようにするためのもので、
    カード検出込みの挙動を検証するテストでは、テスト内で PatchDetectedCards() を呼んで上書きする
    """

    PatchDetectedCards(monkeypatch, [], 'カードリーダーが 1 台も接続されていません')


# 偽の dvbv5-zap が「ロックできない (受信不可)」「受信データが小さすぎる」「-t の秒数だけ TS を出力し続ける (TLV / TLV 以外)」
# チャンネルとして予約している物理チャンネル
# (test_catv_tuner.py の FAKE_DVBV5_ZAP_SOURCE と対応させる。周波数プラン上の一般名であり受信環境固有の情報ではない)
FAKE_TIMEOUT_CHANNEL = 'CATV_C63'
FAKE_SMALL_OUTPUT_CHANNEL = 'CATV_C13'
FAKE_TLV_STREAM_CHANNEL = 'CATV_C14'
FAKE_TS_STREAM_CHANNEL = 'CATV_C15'


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
                '--no-satellite',
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


class TestScanTLVRecordingExtension:
    """--tlv-recording-time (TLV (4K/8K MMT) キャリアと判定されたチャンネルのみ収録時間を延長する) の統合テスト"""

    # 偽の dvbv5-zap は -t で指定された秒数だけ TS を出力し続けるため、収録が延長されたかどうかを実際の挙動で検証できる
    BASE_ARGS = [
        '--recording-time', '0.3',
        '--tlv-recording-time', '0.9',
        '--no-collect-signal-stats',
        '--no-diff',
        '--no-satellite',
    ]  # fmt: skip

    def test_tlv_carrier_recording_is_extended(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # TLV キャリアと判定されたチャンネルは収録時間が延長され、その旨がコンソールに表示される
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), '--channels', FAKE_TLV_STREAM_CHANNEL, *self.BASE_ARGS])

        assert result.exit_code == 0, result.output
        assert 'recording extended to 0.9 seconds' in result.output
        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert catv_json[FAKE_TLV_STREAM_CHANNEL]['carrier_type'] == 'TLV'

    def test_non_tlv_carrier_recording_is_not_extended(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # TLV キャリアでないチャンネルは --recording-time 秒で収録を打ち切り、延長の表示も出ない
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), '--channels', FAKE_TS_STREAM_CHANNEL, *self.BASE_ARGS])

        assert result.exit_code == 0, result.output
        assert 'recording extended' not in result.output
        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert catv_json[FAKE_TS_STREAM_CHANNEL]['carrier_type'] != 'TLV'


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
                '--no-satellite',
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
                '--no-satellite',
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
                '--no-satellite',
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
                '--no-satellite',
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

class FakeISDBSTuner:
    """
    ネイティブ BS/CS/地上波スキャン用の偽 ISDB チューナー (tune() の呼び出しを記録し、ダミーの TS データを返す)
    tuners_catv.yml 生成時に recisdb 選局コマンドの組み立てで参照される type / isTSIDSelectionSupported() / device_path も備える
    (地上波スキャン用にも流用するため、tuner_type は初期化引数で切り替えられる)
    """

    def __init__(self, tuner_type: str = 'ISDB-S', name: str = 'Fake ISDB-S Tuner') -> None:
        self.name = name
        self.type = tuner_type
        self.device_path = Path('/dev/fake-isdbs0')
        self.last_tuner_opening_failed = False
        self.tuned_channels: list[str] = []

    def isTSIDSelectionSupported(self) -> bool:
        return True

    def tune(self, physical_channel_recisdb: str, recording_time: float = 10.0, tune_timeout: float = 7.0) -> bytearray:
        self.tuned_channels.append(physical_channel_recisdb)
        return bytearray(b'fake ts stream data')


class _FakeTransportStreamAnalyzer:
    """偽の TransportStreamAnalyzer (物理チャンネルに応じた合成 TransportStreamInfo を返す)"""

    def __init__(self, ts_stream_data: bytearray, physical_channel: str) -> None:
        self._physical_channel = physical_channel

    def analyze(self) -> list[TransportStreamInfo]:
        if self._physical_channel == 'BS01/TS0':
            return BuildSyntheticBSTsInfos()
        if self._physical_channel == 'ND02':
            return [ts_info for ts_info in BuildSyntheticCSTsInfos() if ts_info.physical_channel == 'ND02']
        if self._physical_channel == 'ND04':
            return [ts_info for ts_info in BuildSyntheticCSTsInfos() if ts_info.physical_channel == 'ND04']
        raise AssertionError(f'Unexpected physical channel: {self._physical_channel}')


@pytest.fixture
def fake_recisdb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH 上に偽の recisdb コマンドを設置する (shutil.which('recisdb') のチェックを通すためだけで、実行はされない)"""

    script_path = tmp_path / 'recisdb'
    script_path.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')
    return script_path


def PatchSatelliteScan(monkeypatch: pytest.MonkeyPatch, isdbs_tuners: list[FakeISDBSTuner]) -> None:
    """ISDBTuner.getAvailableISDBSTuners() と衛星スキャン用の TransportStreamAnalyzer を合成実装に差し替える"""

    monkeypatch.setattr(ISDBTuner, 'getAvailableISDBSTuners', lambda **kwargs: isdbs_tuners)
    # 偽チューナーは実デバイスを持たないため、RobustISDBTuner への変換 (実デバイス検証を伴う) は素通しにする
    monkeypatch.setattr('isdb_scanner.catv.scan.AsRobustISDBTuners', lambda tuners: tuners)
    monkeypatch.setattr('isdb_scanner.catv.satellite.TransportStreamAnalyzer', _FakeTransportStreamAnalyzer)


class TestScanSatelliteIntegration:
    """ネイティブ BS/CS (ISDB-S) スキャン統合の CLI テスト (偽 recisdb + 偽 ISDB-S チューナーによる合成データのみで構成)"""

    BASE_ARGS = ['--channels', 'CATV_15,CATV_16', '--recording-time', '0.5', '--no-collect-signal-stats', '--no-diff']
    SATELLITE_ARGS = [*BASE_ARGS, '--satellite']

    def test_satellite_disabled_by_default(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # --satellite を指定しない限り、ネイティブ BS/CS スキャンは実行されない (デフォルト無効)
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbs_tuner = FakeISDBSTuner()
        PatchSatelliteScan(monkeypatch, [fake_isdbs_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.BASE_ARGS])

        assert result.exit_code == 0, result.output
        assert fake_isdbs_tuner.tuned_channels == []
        assert not (output_dir / 'BS.json').is_file()
        assert 'Scanned satellite:' not in result.output

    def test_satellite_scan_with_flag(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbs_tuner = FakeISDBSTuner()
        PatchSatelliteScan(monkeypatch, [fake_isdbs_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.SATELLITE_ARGS])

        assert result.exit_code == 0, result.output
        # BS01/TS0 (BS) → ND02 (CS1) → ND04 (CS2) の順で recisdb 互換の物理チャンネル表記で選局されること
        assert fake_isdbs_tuner.tuned_channels == ['BS01_0', 'CS02', 'CS04']
        assert 'Scanned satellite: BS=2 TS / CS=2 TS' in result.output

        # BS.json / CS.json が生成されること
        bs_json = json.loads((output_dir / 'BS.json').read_text(encoding='utf-8'))
        assert [ts_info['physical_channel'] for ts_info in bs_json] == ['BS01/TS0', 'BS03/TS1']
        cs_json = json.loads((output_dir / 'CS.json').read_text(encoding='utf-8'))
        assert [ts_info['physical_channel'] for ts_info in cs_json] == ['ND02', 'ND04']

        # Mirakurun / mirakc のチャンネル設定に BS/CS エントリが統合されること
        mirakurun_yml = (output_dir / 'Mirakurun' / 'channels_catv.yml').read_text(encoding='utf-8')
        assert 'type: BS' in mirakurun_yml
        assert 'type: CS' in mirakurun_yml
        assert "satellite: ' --tsid 16400 '" in mirakurun_yml
        mirakc_yml = (output_dir / 'mirakc' / 'channels_catv.yml').read_text(encoding='utf-8')
        assert 'extra-args: --tsid 16400' in mirakc_yml
        assert 'channel: CS04' in mirakc_yml

    def test_no_satellite_flag_disables_scan(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbs_tuner = FakeISDBSTuner()
        PatchSatelliteScan(monkeypatch, [fake_isdbs_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.BASE_ARGS, '--no-satellite'])

        assert result.exit_code == 0, result.output
        assert fake_isdbs_tuner.tuned_channels == []
        assert not (output_dir / 'BS.json').is_file()
        assert not (output_dir / 'CS.json').is_file()
        assert 'Scanned satellite:' not in result.output
        # Mirakurun / mirakc のチャンネル設定にも BS/CS エントリが含まれないこと (ヘッダーコメント行は判定から除く)
        mirakurun_yml = (output_dir / 'Mirakurun' / 'channels_catv.yml').read_text(encoding='utf-8')
        mirakurun_yml_body = '\n'.join(line for line in mirakurun_yml.splitlines() if not line.lstrip().startswith('#'))
        assert 'type: BS' not in mirakurun_yml_body
        assert 'recisdb' not in mirakurun_yml_body

    def test_no_isdbs_tuner_skips_satellite_scan(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        PatchSatelliteScan(monkeypatch, [])  # ISDB-S チューナーが 1 台も見つからない
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.SATELLITE_ARGS])

        assert result.exit_code == 0, result.output
        assert 'No ISDB-S tuner found. Skipping native BS/CS (satellite) scan.' in result.output
        assert not (output_dir / 'BS.json').is_file()
        # CATV スキャン自体は完走すること
        assert (output_dir / 'CATV.json').is_file()

    def test_missing_recisdb_skips_satellite_scan(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbs_tuner = FakeISDBSTuner()
        PatchSatelliteScan(monkeypatch, [fake_isdbs_tuner])
        # PATH を偽 dvbv5-zap のあるディレクトリのみに差し替え、recisdb が見つからない状況を作る
        monkeypatch.setenv('PATH', str(fake_dvbv5_zap.parent))
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.SATELLITE_ARGS])

        assert result.exit_code == 0, result.output
        assert 'recisdb not found. Skipping native BS/CS (satellite) scan.' in result.output
        assert fake_isdbs_tuner.tuned_channels == []
        assert not (output_dir / 'BS.json').is_file()
        assert (output_dir / 'CATV.json').is_file()

    def test_exclude_pay_tv_skips_cs_scan(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbs_tuner = FakeISDBSTuner()
        PatchSatelliteScan(monkeypatch, [fake_isdbs_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.SATELLITE_ARGS, '--exclude-pay-tv'])

        assert result.exit_code == 0, result.output
        # CS (ND02/ND04) はスキャンされないこと
        assert fake_isdbs_tuner.tuned_channels == ['BS01_0']
        # BS.json は生成されるが CS.json は生成されないこと (JSON は常に全チャンネル出力の慣習通り有料 BS も含む)
        bs_json = json.loads((output_dir / 'BS.json').read_text(encoding='utf-8'))
        assert [ts_info['physical_channel'] for ts_info in bs_json] == ['BS01/TS0', 'BS03/TS1']
        assert not (output_dir / 'CS.json').is_file()
        # YAML では有料放送のみの BS03/TS1 と CS エントリが除外されること (ヘッダーコメント行は判定から除く)
        mirakurun_yml = (output_dir / 'Mirakurun' / 'channels_catv.yml').read_text(encoding='utf-8')
        mirakurun_yml_body = '\n'.join(line for line in mirakurun_yml.splitlines() if not line.lstrip().startswith('#'))
        assert 'BS01/TS0' in mirakurun_yml_body
        assert 'BS03/TS1' not in mirakurun_yml_body
        assert 'type: CS' not in mirakurun_yml_body


def BuildSyntheticTerrestrialTsInfos() -> list[TransportStreamInfo]:
    """ネイティブ地上波統合出力のテスト用に、合成の地上波 TransportStreamInfo (物理チャンネル T27) を組み立てる"""

    return [
        TransportStreamInfo(
            physical_channel='T27',
            transport_stream_id=0x7810,
            network_id=0x7880,  # 地上波の network_id レンジ (0x7880-0x7FE8)
            network_name='NHK総合',
            remote_control_key_id=1,
            services=[
                ServiceInfo(channel_number='011', service_id=1024, service_type=0x01, service_name='NHK総合1', is_free=True),
            ],
        ),
    ]


class _FakeTerrestrialTransportStreamAnalyzer:
    """偽の地上波用 TransportStreamAnalyzer (物理チャンネル T27 でのみ合成 TS を返し、それ以外は空を返す)"""

    def __init__(self, ts_stream_data: bytearray, physical_channel: str) -> None:
        self._physical_channel = physical_channel

    def analyze(self) -> list[TransportStreamInfo]:
        if self._physical_channel == 'T27':
            return BuildSyntheticTerrestrialTsInfos()
        return []


def PatchTerrestrialScan(monkeypatch: pytest.MonkeyPatch, isdbt_tuners: list[FakeISDBSTuner]) -> None:
    """ISDBTuner.getAvailableISDBTTuners() と地上波スキャン用の TransportStreamAnalyzer を合成実装に差し替える"""

    monkeypatch.setattr(ISDBTuner, 'getAvailableISDBTTuners', lambda **kwargs: isdbt_tuners)
    # 偽チューナーは実デバイスを持たないため、RobustISDBTuner への変換 (実デバイス検証を伴う) は素通しにする
    monkeypatch.setattr('isdb_scanner.catv.scan.AsRobustISDBTuners', lambda tuners: tuners)
    monkeypatch.setattr('isdb_scanner.catv.terrestrial.TransportStreamAnalyzer', _FakeTerrestrialTransportStreamAnalyzer)


class TestScanTerrestrialIntegration:
    """ネイティブ地上波 (ISDB-T) スキャン統合の CLI テスト (偽 recisdb + 偽 ISDB-T チューナーによる合成データのみで構成)"""

    BASE_ARGS = ['--channels', 'CATV_15,CATV_16', '--recording-time', '0.5', '--no-collect-signal-stats', '--no-diff']

    def test_terrestrial_disabled_by_default(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # --terrestrial を指定しない限り、ネイティブ地上波スキャンは実行されない (デフォルト無効)
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbt_tuner = FakeISDBSTuner()
        PatchTerrestrialScan(monkeypatch, [fake_isdbt_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.BASE_ARGS])

        assert result.exit_code == 0, result.output
        assert fake_isdbt_tuner.tuned_channels == []
        assert not (output_dir / 'Terrestrial.json').is_file()
        assert 'Scanned terrestrial:' not in result.output

    def test_terrestrial_scan_with_flag(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbt_tuner = FakeISDBSTuner()
        PatchTerrestrialScan(monkeypatch, [fake_isdbt_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.BASE_ARGS, '--terrestrial'])

        assert result.exit_code == 0, result.output
        # T13〜T62 が recisdb 互換の物理チャンネル表記 (T13〜T62) で順に tune されること
        assert fake_isdbt_tuner.tuned_channels == [f'T{i}' for i in range(13, 63)]
        assert 'Scanned terrestrial: 1 TS' in result.output

        # Terrestrial.json が生成され、T27 の TS が含まれること
        terrestrial_json = json.loads((output_dir / 'Terrestrial.json').read_text(encoding='utf-8'))
        assert [ts_info['physical_channel'] for ts_info in terrestrial_json] == ['T27']

        # Mirakurun / mirakc のチャンネル設定に GR (T27) エントリが統合されること
        mirakurun_yml = (output_dir / 'Mirakurun' / 'channels_catv.yml').read_text(encoding='utf-8')
        assert 'type: GR' in mirakurun_yml
        mirakc_yml = (output_dir / 'mirakc' / 'channels_catv.yml').read_text(encoding='utf-8')
        assert 'channel: T27' in mirakc_yml

    def test_tuners_yml_smoke(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # tuners_catv.yml が生成され、CATV チューナー用の dvbv5-zap 選局コマンド行を含むこと
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbt_tuner = FakeISDBSTuner()
        PatchTerrestrialScan(monkeypatch, [fake_isdbt_tuner])
        output_dir = tmp_path / 'out'

        result = runner.invoke(app, [str(output_dir), *self.BASE_ARGS, '--terrestrial'])

        assert result.exit_code == 0, result.output
        mirakurun_tuners = (output_dir / 'Mirakurun' / 'tuners_catv.yml').read_text(encoding='utf-8')
        assert 'dvbv5-zap' in mirakurun_tuners
        mirakc_tuners = (output_dir / 'mirakc' / 'tuners_catv.yml').read_text(encoding='utf-8')
        assert 'dvbv5-zap' in mirakc_tuners
        # ISDB-T チューナー用の recisdb 選局コマンド行も含まれること
        assert 'recisdb' in mirakurun_tuners

    def test_prefer_native_smoke(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # --prefer native 指定時は正常終了し、type 重複の警告文言が表示されないこと
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        fake_isdbt_tuner = FakeISDBSTuner()
        PatchTerrestrialScan(monkeypatch, [fake_isdbt_tuner])
        PatchSatelliteScan(monkeypatch, [FakeISDBSTuner()])
        output_dir = tmp_path / 'out'

        result = runner.invoke(
            app, [str(output_dir), *self.BASE_ARGS, '--terrestrial', '--satellite', '--prefer', 'native']
        )

        assert result.exit_code == 0, result.output
        assert 'Both native and CATV-retransmitted channels' not in result.output


class TestScanNativeDiff:
    """ネイティブスキャン (BS/CS/地上波) の差分レポート出力のテスト"""

    BASE_ARGS = ['--channels', 'CATV_15,CATV_16', '--recording-time', '0.5', '--no-collect-signal-stats']

    def test_bs_diff_generated(
        self, fake_dvbv5_zap: Path, fake_recisdb: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # 前回の BS.json (BS01/TS0 のみ) を配置した状態で --satellite スキャンすると、
        # 今回は BS03/TS1 が増えるため BS.diff.txt が生成されること (--no-diff は付けない)
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        PatchSatelliteScan(monkeypatch, [FakeISDBSTuner()])
        output_dir = tmp_path / 'out'
        output_dir.mkdir(parents=True, exist_ok=True)

        # 前回の BS.json = BS01/TS0 のみ (今回のスキャン結果 BS01/TS0 + BS03/TS1 との差分が生じる)
        previous_bs = [BuildSyntheticBSTsInfos()[0]]
        NativeJSONFormatter(output_dir / 'BS.json', previous_bs).save()

        result = runner.invoke(app, [str(output_dir), *self.BASE_ARGS, '--satellite'])

        assert result.exit_code == 0, result.output
        assert (output_dir / 'BS.diff.txt').is_file()
        diff_text = (output_dir / 'BS.diff.txt').read_text(encoding='utf-8')
        assert 'BS03/TS1' in diff_text
        # 上書き後の BS.json は今回のスキャン結果 (2 TS) になっていること
        bs_json = json.loads((output_dir / 'BS.json').read_text(encoding='utf-8'))
        assert [ts_info['physical_channel'] for ts_info in bs_json] == ['BS01/TS0', 'BS03/TS1']


class TestScanFromJson:
    """--from-json 再フォーマットモードのテスト (スキャンを行わず既存 JSON から出力を再生成する)"""

    def test_reformat_regenerates_outputs_without_rewriting_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # 合成 JSON (CATV.json + BS.json) を配置 → --from-json 実行 → 出力ファイル群が再生成され、JSON は書き換わらない
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'
        output_dir.mkdir(parents=True, exist_ok=True)

        CATVJSONFormatter(output_dir / 'CATV.json', BuildSyntheticCarriers()).save()
        NativeJSONFormatter(output_dir / 'BS.json', BuildSyntheticBSTsInfos()).save()
        catv_json_before = (output_dir / 'CATV.json').read_bytes()
        bs_json_before = (output_dir / 'BS.json').read_bytes()

        result = runner.invoke(app, [str(output_dir), '--from-json'])

        assert result.exit_code == 0, result.output
        assert 'Reformatted from existing JSON.' in result.output

        # 出力ファイル群 (conf / channels / tuners / EDCB) が再生成されること
        assert (output_dir / 'dvbv5_channels_catv.conf').is_file()
        assert (output_dir / 'Mirakurun' / 'channels_catv.yml').is_file()
        assert (output_dir / 'Mirakurun' / 'tuners_catv.yml').is_file()
        assert (output_dir / 'mirakc' / 'channels_catv.yml').is_file()
        assert (output_dir / 'mirakc' / 'tuners_catv.yml').is_file()
        assert (output_dir / 'EDCB-Wine').is_dir()

        # BS.json のエントリが channels に統合されていること (再フォーマットでも native 分が反映される)
        assert 'type: BS' in (output_dir / 'Mirakurun' / 'channels_catv.yml').read_text(encoding='utf-8')

        # JSON 群は読み取り専用: CATV.json / BS.json が書き換わらないこと
        assert (output_dir / 'CATV.json').read_bytes() == catv_json_before
        assert (output_dir / 'BS.json').read_bytes() == bs_json_before

    def test_reformat_missing_catv_json_exits_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # CATV.json が存在しない場合は赤エラーで exit 1
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'
        output_dir.mkdir(parents=True, exist_ok=True)

        result = runner.invoke(app, [str(output_dir), '--from-json'])

        assert result.exit_code == 1
        assert 'CATV.json was not found' in result.output


class TestScanCardAssignment:
    """--check-cards/--no-check-cards によるカード検出・受信可能性レポート生成のテスト"""

    def _RunScan(self, tmp_path: Path, extra_args: list[str] | None = None) -> tuple[Path, object]:
        """偽 dvbv5-zap で 1 チャンネルだけスキャンする (呼び出し側で fake_dvbv5_zap フィクスチャを要求すること)"""

        output_dir = tmp_path / 'out'
        result = runner.invoke(
            app,
            [
                str(output_dir),
                '--channels',
                'CATV_15',
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-diff',
                '--no-satellite',
                *(extra_args or []),
            ],
        )
        return output_dir, result

    def test_report_generated_with_both_cards(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        PatchDetectedCards(monkeypatch, [BuildBCASCard(), BuildCCASCard()])

        output_dir, result = self._RunScan(tmp_path)

        assert result.exit_code == 0, result.output
        # コンソールに検出したカードの要約が 1 行出ること
        assert 'Detected cards: B-CAS' in result.output
        assert 'C-CAS' in result.output

        # 受信可能性レポートが出力されること
        report = (output_dir / 'CATV.cards.txt').read_text(encoding='utf-8')
        assert FAKE_BCAS_READER_NAME in report
        assert FAKE_CCAS_READER_NAME in report

        # B-CAS/C-CAS の両方が検出されたため、デコーダースクリプトも生成されること
        assert (output_dir / 'Mirakurun' / 'decoder-bcas.sh').is_file()
        assert (output_dir / 'Mirakurun' / 'decoder-ccas.sh').is_file()
        assert (output_dir / 'mirakc' / 'decode-filter.sh').is_file()
        assert stat.S_IMODE((output_dir / 'mirakc' / 'decode-filter.sh').stat().st_mode) == 0o755

        # tuners.yml の CATV エントリの decoder に、生成したラッパースクリプトの絶対パスが入ること
        tuners_yml = (output_dir / 'Mirakurun' / 'tuners_catv.yml').read_text(encoding='utf-8')
        assert str(output_dir / 'Mirakurun' / 'decoder-bcas.sh') in tuners_yml

        # CATV.json のスキーマは変更しない (decodable などのカード依存の情報を混ぜない)
        catv_json = json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8'))
        assert 'decodable' not in json.dumps(catv_json)

    def test_report_generated_with_bcas_only(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        PatchDetectedCards(monkeypatch, [BuildBCASCard()])

        output_dir, result = self._RunScan(tmp_path)

        assert result.exit_code == 0, result.output
        assert (output_dir / 'CATV.cards.txt').is_file()
        # 単一カード環境ではデコーダースクリプトを生成しない
        assert not (output_dir / 'Mirakurun' / 'decoder-bcas.sh').exists()
        assert not (output_dir / 'mirakc' / 'decode-filter.sh').exists()
        # 代わりに decoder: arib-b25-stream-test が出力される
        assert 'arib-b25-stream-test' in (output_dir / 'Mirakurun' / 'tuners_catv.yml').read_text(encoding='utf-8')

    def test_scan_succeeds_when_no_card_detected(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # カードが 1 枚も検出できなくてもスキャン全体は正常終了し、レポートに理由が書かれる
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        PatchDetectedCards(monkeypatch, [], 'PC/SC サービス (pcscd) が起動していません')

        output_dir, result = self._RunScan(tmp_path)

        assert result.exit_code == 0, result.output
        assert 'No CAS card detected' in result.output
        assert 'pcscd' in (output_dir / 'CATV.cards.txt').read_text(encoding='utf-8')

    def test_no_check_cards_skips_detection(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        # --no-check-cards ではそもそも DetectCASCards() を呼ばないこと
        monkeypatch.setattr(scan_module, 'DetectCASCards', lambda: pytest.fail('DetectCASCards() must not be called with --no-check-cards'))

        output_dir, result = self._RunScan(tmp_path, ['--no-check-cards'])

        assert result.exit_code == 0, result.output
        assert not (output_dir / 'CATV.cards.txt').exists()
        assert not (output_dir / 'mirakc' / 'decode-filter.sh').exists()
        # カード在庫を渡していないため decoder キーも出力されない (従来どおりの出力)
        assert 'decoder' not in (output_dir / 'Mirakurun' / 'tuners_catv.yml').read_text(encoding='utf-8')

    def test_from_json_also_generates_report(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # --from-json 再フォーマットモードでも受信可能性レポート・デコーダースクリプトが生成されること
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        PatchDetectedCards(monkeypatch, [BuildBCASCard(), BuildCCASCard()])
        output_dir = tmp_path / 'out'
        output_dir.mkdir(parents=True, exist_ok=True)
        CATVJSONFormatter(output_dir / 'CATV.json', BuildSyntheticCarriers()).save()

        result = runner.invoke(app, [str(output_dir), '--from-json'])

        assert result.exit_code == 0, result.output
        assert 'Detected cards: B-CAS' in result.output
        assert (output_dir / 'CATV.cards.txt').is_file()
        assert (output_dir / 'Mirakurun' / 'decoder-bcas.sh').is_file()
        assert (output_dir / 'mirakc' / 'decode-filter.sh').is_file()

    def test_from_json_with_no_check_cards(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        monkeypatch.setattr(scan_module, 'DetectCASCards', lambda: pytest.fail('DetectCASCards() must not be called with --no-check-cards'))
        output_dir = tmp_path / 'out'
        output_dir.mkdir(parents=True, exist_ok=True)
        CATVJSONFormatter(output_dir / 'CATV.json', BuildSyntheticCarriers()).save()

        result = runner.invoke(app, [str(output_dir), '--from-json', '--no-check-cards'])

        assert result.exit_code == 0, result.output
        assert not (output_dir / 'CATV.cards.txt').exists()


class TestScanDiffOutputAndFailOnDiff:
    """CATV.diff.json の併産と --fail-on-diff の終了コード (0 = 変化なし / 1 = スキャン失敗 / 2 = 変化あり) のテスト"""

    def _RunScan(self, output_dir: Path, channels: str, extra_args: list[str] | None = None):
        """偽 dvbv5-zap で指定した物理チャンネルをスキャンする (呼び出し側で fake_dvbv5_zap フィクスチャを要求すること)"""

        return runner.invoke(
            app,
            [
                str(output_dir),
                '--channels',
                channels,
                '--recording-time',
                '0.5',
                '--no-collect-signal-stats',
                '--no-satellite',
                *(extra_args or []),
            ],
        )

    def test_fail_on_diff_with_no_diff_raises(self, tmp_path: Path):
        # --no-diff は差分レポート自体を生成しないため、--fail-on-diff とは併用できない
        result = runner.invoke(app, [str(tmp_path / 'out'), '--fail-on-diff', '--no-diff'])

        assert result.exit_code == 1
        assert '--fail-on-diff cannot be combined with --no-diff' in result.output

    def test_first_scan_is_not_treated_as_a_change(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # 前回のスキャン結果がない初回実行では差分自体が生成されないため、--fail-on-diff でも終了コード 0
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'

        result = self._RunScan(output_dir, 'CATV_15', ['--fail-on-diff'])

        assert result.exit_code == 0, result.output
        assert not (output_dir / 'CATV.diff.txt').exists()
        assert not (output_dir / 'CATV.diff.json').exists()

    def test_no_changes_exits_with_code_0(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # 同じチャンネル構成を 2 回スキャンした場合は変化なし → 終了コード 0 / has_changes: false
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'

        assert self._RunScan(output_dir, 'CATV_15,CATV_16').exit_code == 0
        result = self._RunScan(output_dir, 'CATV_15,CATV_16', ['--fail-on-diff'])

        assert result.exit_code == 0, result.output
        assert (output_dir / 'CATV.diff.txt').read_text(encoding='utf-8') == 'No changes detected since the previous scan.\n'

        diff_json = json.loads((output_dir / 'CATV.diff.json').read_text(encoding='utf-8'))
        assert diff_json['has_changes'] is False
        assert diff_json['added_channels'] == []
        assert diff_json['removed_channels'] == []
        assert diff_json['changed_channels'] == []
        assert diff_json['signal_warnings'] == []

    def test_changes_exit_with_code_2(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # 2 回目で受信できる物理チャンネルが減った場合は変化あり → 終了コード 2 (スキャン失敗の 1 とは区別する)
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'

        assert self._RunScan(output_dir, 'CATV_15,CATV_16').exit_code == 0
        result = self._RunScan(output_dir, 'CATV_15', ['--fail-on-diff'])

        assert result.exit_code == 2, result.output
        assert 'Exiting with code 2' in result.output

        # 終了コード 2 でも、スキャン結果の出力ファイル一式は通常どおり生成される
        assert list(json.loads((output_dir / 'CATV.json').read_text(encoding='utf-8')).keys()) == ['CATV_15']
        assert (output_dir / 'dvbv5_channels_catv.conf').is_file()
        assert (output_dir / 'Mirakurun' / 'channels_catv.yml').is_file()

        assert '- CATV_16' in (output_dir / 'CATV.diff.txt').read_text(encoding='utf-8')
        diff_json = json.loads((output_dir / 'CATV.diff.json').read_text(encoding='utf-8'))
        assert diff_json['has_changes'] is True
        assert [channel['physical_channel'] for channel in diff_json['removed_channels']] == ['CATV_16']

    def test_diff_json_is_generated_without_fail_on_diff(self, fake_dvbv5_zap: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # CATV.diff.json は --fail-on-diff の有無に関わらず、CATV.diff.txt と必ず同時に生成される
        PatchAvailableTuners(monkeypatch, [CATVTuner(0)])
        output_dir = tmp_path / 'out'

        assert self._RunScan(output_dir, 'CATV_15,CATV_16').exit_code == 0
        result = self._RunScan(output_dir, 'CATV_15')

        assert result.exit_code == 0, result.output
        assert (output_dir / 'CATV.diff.txt').is_file()
        assert json.loads((output_dir / 'CATV.diff.json').read_text(encoding='utf-8'))['has_changes'] is True

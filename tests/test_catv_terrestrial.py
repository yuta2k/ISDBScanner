from __future__ import annotations

from pathlib import Path

import pytest

from isdb_scanner.catv.terrestrial import GetTerrestrialScanChannels, ScanTerrestrialChannels
from isdb_scanner.constants import ServiceInfo, TransportStreamInfo
from isdb_scanner.tuner import TunerOpeningError, TunerOutputError, TunerTuningError


# 偽チューナー / 偽アナライザーが「受信できる」チャンネルとして扱う物理チャンネル (recisdb 表記 = 物理チャンネル)
# (地上波の周波数プラン上の一般名であり、受信環境固有の情報ではない)
RECEIVABLE_CHANNELS = {
    'T27': (0x7C27, 6, '合成テレビ27'),
    'T13': (0x7C13, 1, '合成テレビ13'),
    'T51': (0x7C51, 8, '合成テレビ51'),
}


def BuildSyntheticTerrestrialTsInfo(physical_channel: str) -> TransportStreamInfo:
    """指定した物理チャンネル (ex: "T27") の合成 TransportStreamInfo を組み立てる"""

    transport_stream_id, remote_control_key_id, network_name = RECEIVABLE_CHANNELS[physical_channel]
    return TransportStreamInfo(
        physical_channel=physical_channel,
        transport_stream_id=transport_stream_id,
        # 地上波のネットワーク ID は 0x7880〜0x7FE8 の範囲 (broadcast_type が Terrestrial と判定される)
        network_id=0x7880 + remote_control_key_id,
        network_name=network_name,
        remote_control_key_id=remote_control_key_id,
        services=[
            ServiceInfo(
                channel_number=f'{remote_control_key_id:01d}01',
                service_id=transport_stream_id,
                service_type=0x01,
                service_name=network_name,
                is_free=True,
            ),
        ],
    )


class FakeISDBTTuner:
    """
    ネイティブ地上波スキャン用の偽 ISDB-T チューナー
    tune() の呼び出しを記録し、チャンネルごとに指定された例外を送出する / ダミーの TS データを返す
    (test_catv_scan.py の FakeISDBSTuner を参考にした自前定義。実機・受信環境には一切依存しない)
    """

    def __init__(
        self,
        name: str = 'Fake ISDB-T Tuner',
        *,
        opening_error_channels: set[str] | None = None,
        tuning_error_channels: set[str] | None = None,
        output_error_channels: set[str] | None = None,
    ) -> None:
        self.name = name
        self.device_path = Path(f'/dev/fake-isdbt-{name}')
        self.last_tuner_opening_failed = False
        self.tuned_channels: list[str] = []
        self._opening_error_channels = opening_error_channels or set()
        self._tuning_error_channels = tuning_error_channels or set()
        self._output_error_channels = output_error_channels or set()

    def tune(self, physical_channel_recisdb: str, recording_time: float = 2.25, tune_timeout: float = 7.0) -> bytearray:
        self.tuned_channels.append(physical_channel_recisdb)
        if physical_channel_recisdb in self._opening_error_channels:
            # チューナーオープン失敗 → __main__ の実装同様、以後このチューナーはスキップ対象になる
            self.last_tuner_opening_failed = True
            raise TunerOpeningError('Failed to open tuner.')
        if physical_channel_recisdb in self._tuning_error_channels:
            raise TunerTuningError('Failed to lock channel.')
        if physical_channel_recisdb in self._output_error_channels:
            raise TunerOutputError('Failed to receive data.')
        return bytearray(b'fake ts stream data')


def MakeFakeAnalyzer(record: dict[str, list[str]] | None = None):
    """
    偽の TransportStreamAnalyzer を生成する
    受信可能チャンネル (RECEIVABLE_CHANNELS) には合成 TS 情報を、それ以外には空リストを返す
    """

    class _FakeTransportStreamAnalyzer:
        def __init__(self, ts_stream_data: bytearray, physical_channel: str) -> None:
            self._physical_channel = physical_channel
            if record is not None:
                record.setdefault('analyzed', []).append(physical_channel)

        def analyze(self) -> list[TransportStreamInfo]:
            if self._physical_channel in RECEIVABLE_CHANNELS:
                return [BuildSyntheticTerrestrialTsInfo(self._physical_channel)]
            return []

    return _FakeTransportStreamAnalyzer


ALL_TERRESTRIAL_CHANNELS = [f'T{i}' for i in range(13, 63)]


class TestGetTerrestrialScanChannels:
    """スキャン対象チャンネル定義 (T13〜T62) のテスト"""

    def test_covers_t13_to_t62(self):
        channels = GetTerrestrialScanChannels()
        assert [ch.physical_channel for ch in channels] == ALL_TERRESTRIAL_CHANNELS
        # 廃止済みの 53ch〜62ch もコミュニティチャンネル用途で含まれること
        assert 'T53' in [ch.physical_channel for ch in channels]
        assert 'T62' in [ch.physical_channel for ch in channels]


class TestScanTerrestrialChannels:
    """ネイティブ地上波スキャンループのテスト (合成チューナー + 合成アナライザーのみで構成)"""

    def test_scans_all_channels_and_returns_sorted(self, monkeypatch: pytest.MonkeyPatch):
        # 全チャンネルが recisdb 表記 (T13〜T62) で tune され、受信できたチャンネルが physical_channel 昇順で返ること
        tuner = FakeISDBTTuner()
        monkeypatch.setattr('isdb_scanner.catv.terrestrial.TransportStreamAnalyzer', MakeFakeAnalyzer())

        result = ScanTerrestrialChannels([tuner])

        # T13〜T62 の全チャンネルが (地上波は recisdb 表記 = 物理チャンネル) 順に tune される
        assert tuner.tuned_channels == ALL_TERRESTRIAL_CHANNELS
        # 受信できたチャンネルのみが physical_channel 昇順で返る (RECEIVABLE_CHANNELS を昇順ソートしたもの)
        assert [ts.physical_channel for ts in result] == ['T13', 'T27', 'T51']
        assert result[0].network_name == '合成テレビ13'
        assert result[0].remote_control_key_id == 1

    def test_tuning_error_skips_channel_without_failover(self, monkeypatch: pytest.MonkeyPatch):
        # TunerTuningError (選局失敗) はそのチャンネルを受信不可としてスキップし、次のチューナーへフェイルオーバーしない
        tuner0 = FakeISDBTTuner(name='tuner0', tuning_error_channels={'T27'})
        tuner1 = FakeISDBTTuner(name='tuner1')
        monkeypatch.setattr('isdb_scanner.catv.terrestrial.TransportStreamAnalyzer', MakeFakeAnalyzer())

        result = ScanTerrestrialChannels([tuner0, tuner1])

        # T27 は tuner0 で選局失敗 → チャンネルごとスキップ (tuner1 では T27 を試さない)
        assert 'T27' not in tuner1.tuned_channels
        assert 'T27' not in [ts.physical_channel for ts in result]
        # 他の受信可能チャンネルは正常に取得される
        assert [ts.physical_channel for ts in result] == ['T13', 'T51']

    def test_output_error_skips_channel_without_failover(self, monkeypatch: pytest.MonkeyPatch):
        # TunerOutputError (受信データ取得失敗) も同様にチャンネルごとスキップされる
        tuner0 = FakeISDBTTuner(name='tuner0', output_error_channels={'T13'})
        tuner1 = FakeISDBTTuner(name='tuner1')
        monkeypatch.setattr('isdb_scanner.catv.terrestrial.TransportStreamAnalyzer', MakeFakeAnalyzer())

        result = ScanTerrestrialChannels([tuner0, tuner1])

        assert 'T13' not in tuner1.tuned_channels
        assert [ts.physical_channel for ts in result] == ['T27', 'T51']

    def test_opening_error_fails_over_to_next_tuner(self, monkeypatch: pytest.MonkeyPatch):
        # TunerOpeningError (チューナーオープン失敗) はチャンネルスキップではなく次のチューナーへフェイルオーバーする
        # tuner0 は T27 でオープン失敗 → 以降 last_tuner_opening_failed=True でスキップされ、tuner1 が全チャンネルを消化する
        tuner0 = FakeISDBTTuner(name='tuner0', opening_error_channels={'T27'})
        tuner1 = FakeISDBTTuner(name='tuner1')
        monkeypatch.setattr('isdb_scanner.catv.terrestrial.TransportStreamAnalyzer', MakeFakeAnalyzer())

        result = ScanTerrestrialChannels([tuner0, tuner1])

        # T27 は tuner0 でオープン失敗した後、tuner1 にフェイルオーバーして受信できる (スキップされない)
        assert 'T27' in tuner1.tuned_channels
        assert [ts.physical_channel for ts in result] == ['T13', 'T27', 'T51']
        # tuner0 は T13〜T26 を消化し、T27 でオープン失敗 (last_tuner_opening_failed=True) → 以降スキップされる
        assert tuner0.tuned_channels == [f'T{i}' for i in range(13, 28)]
        # tuner1 は T27 でフェイルオーバー先として初めて使われ、T28 以降は tuner0 がスキップされるため全て tuner1 が消化する
        assert tuner1.tuned_channels == [f'T{i}' for i in range(27, 63)]

    def test_pre_failed_tuner_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        # 事前に last_tuner_opening_failed=True になっているチューナーは最初からスキップされる
        dead_tuner = FakeISDBTTuner(name='dead')
        dead_tuner.last_tuner_opening_failed = True
        alive_tuner = FakeISDBTTuner(name='alive')
        monkeypatch.setattr('isdb_scanner.catv.terrestrial.TransportStreamAnalyzer', MakeFakeAnalyzer())

        result = ScanTerrestrialChannels([dead_tuner, alive_tuner])

        assert dead_tuner.tuned_channels == []
        assert alive_tuner.tuned_channels == ALL_TERRESTRIAL_CHANNELS
        assert [ts.physical_channel for ts in result] == ['T13', 'T27', 'T51']

import json
from pathlib import Path

from ruamel.yaml import YAML

from isdb_scanner.catv.constants import (
    CATV_FREQUENCY_TABLE,
    CarrierType,
    CATVCarrierInfo,
    CATVServiceInfo,
    CATVTransportStreamInfo,
)
from isdb_scanner.catv.formatter import (
    CATVDvbv5ConfFormatter,
    CATVJSONFormatter,
    CATVMirakcConfigYmlFormatter,
    CATVMirakurunChannelsYmlFormatter,
)


def LoadYaml(text: str):
    """テスト用に YAML 文字列 (先頭のコメント行を除く) をパースして Python オブジェクトに変換する"""
    return YAML().load(text)


class TestCATVJSONFormatterSynthetic:
    """合成データによる CATVJSONFormatter のテスト (CI でも実行可能)"""

    def test_format_contains_expected_fields(self, tmp_path: Path):
        carriers = BuildSyntheticCarriers()

        formatter = CATVJSONFormatter(tmp_path / 'CATV.json', carriers)
        result = json.loads(formatter.format())

        # 物理チャンネル名をキーにした dict になっていること
        assert set(result.keys()) == {'CATV_15', 'CATV_28', 'CATV_C36', 'CATV_C62'}

        assert result['CATV_15']['physical_channel'] == 'CATV_15'
        assert result['CATV_15']['carrier_type'] == 'TSMF'
        ts_info = next(ts for ts in result['CATV_15']['transport_streams'] if ts['tsmf_relative_ts_number'] == 1)
        assert ts_info['transport_stream_id'] == 0x1000
        assert ts_info['network_name'] == 'サンプル放送'
        assert ts_info['retransmission_source'] == 'Terrestrial'

        # Empty キャリアは transport_streams が空のまま出力される
        assert result['CATV_C62']['carrier_type'] == 'Empty'
        assert result['CATV_C62']['transport_streams'] == []

    def test_save_writes_file(self, tmp_path: Path):
        carriers = BuildSyntheticCarriers()
        save_path = tmp_path / 'CATV.json'

        formatted_str = CATVJSONFormatter(save_path, carriers).save()

        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str
        assert json.loads(formatted_str)['CATV_28']['transport_streams'][0]['transport_stream_id'] == 0x2000


class TestCATVDvbv5ConfFormatterSynthetic:
    """合成データによる CATVDvbv5ConfFormatter のテスト (CI でも実行可能)"""

    def test_format_only_includes_receivable_carriers(self, tmp_path: Path):
        carriers = BuildSyntheticCarriers()

        formatted_str = CATVDvbv5ConfFormatter(tmp_path / 'dvbv5_channels_catv.conf', carriers).format()

        assert '[CATV_15]' in formatted_str
        assert f'\tFREQUENCY = {CATV_FREQUENCY_TABLE["CATV_15"]}' in formatted_str
        assert '\tDELIVERY_SYSTEM = DVBC/ANNEX_A' in formatted_str
        assert '\tSYMBOL_RATE = 5274000' in formatted_str
        assert '\tMODULATION = QAM/AUTO' in formatted_str
        # TLV キャリアは (Mirakurun/mirakc の出力からは除外されるが) 受信はできているので conf には含まれる
        assert '[CATV_C36]' in formatted_str
        # Empty キャリア (受信不可) は出力に含まれない
        assert '[CATV_C62]' not in formatted_str

    def test_format_empty_when_no_receivable_carriers(self, tmp_path: Path):
        carriers = [CATVCarrierInfo(physical_channel='CATV_C62', carrier_type=CarrierType.Empty, transport_streams=[])]

        formatted_str = CATVDvbv5ConfFormatter(tmp_path / 'dvbv5_channels_catv.conf', carriers).format()
        assert formatted_str == ''

    def test_save_writes_file(self, tmp_path: Path):
        carriers = BuildSyntheticCarriers()
        save_path = tmp_path / 'dvbv5_channels_catv.conf'

        formatted_str = CATVDvbv5ConfFormatter(save_path, carriers).save()

        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str


def BuildSyntheticCarriers() -> list[CATVCarrierInfo]:
    """
    レコーダー向けフォーマッター (Mirakurun/mirakc) のテスト用に、合成の CATVCarrierInfo 一覧を組み立てる
    - CATV_15: TSMF / 相対TS1 (network_name あり) + 相対TS2 (network_name 不明・サービス名で代用)
    - CATV_28: SingleTS (tsmfRelTs を持たない)
    - CATV_C36: TLV (4K/8K MMT。Mirakurun/mirakc 双方の出力から除外される)
    - CATV_C62: Empty (受信不可。transport_streams が空のため自然に除外される)
    """

    tsmf_carrier = CATVCarrierInfo(
        physical_channel='CATV_15',
        carrier_type=CarrierType.TSMF,
        transport_streams=[
            CATVTransportStreamInfo(
                physical_channel='CATV_15',
                tsmf_relative_ts_number=1,
                transport_stream_id=0x1000,
                network_name='サンプル放送',
                retransmission_source='Terrestrial',
                services=[CATVServiceInfo(service_id=1024, service_name='サンプルサービス1')],
            ),
            CATVTransportStreamInfo(
                physical_channel='CATV_15',
                tsmf_relative_ts_number=2,
                transport_stream_id=0x1234,
                network_name='Unknown',
                retransmission_source='SelfBroadcast',
                services=[CATVServiceInfo(service_id=1, service_name='自主放送チャンネル')],
            ),
        ],
    )
    single_ts_carrier = CATVCarrierInfo(
        physical_channel='CATV_28',
        carrier_type=CarrierType.SingleTS,
        transport_streams=[
            CATVTransportStreamInfo(
                physical_channel='CATV_28',
                tsmf_relative_ts_number=None,
                transport_stream_id=0x2000,
                network_name='Unknown',
                retransmission_source='Unknown',
                services=[],
            ),
        ],
    )
    tlv_carrier = CATVCarrierInfo(physical_channel='CATV_C36', carrier_type=CarrierType.TLV, transport_streams=[])
    empty_carrier = CATVCarrierInfo(physical_channel='CATV_C62', carrier_type=CarrierType.Empty, transport_streams=[])

    return [tsmf_carrier, single_ts_carrier, tlv_carrier, empty_carrier]


class TestCATVMirakurunChannelsYmlFormatterSynthetic:
    """合成データによる CATVMirakurunChannelsYmlFormatter のテスト (CI でも実行可能)"""

    def test_tsmf_rel_ts_key_present_with_int_type(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        catv_15_channels = [channel for channel in channels if channel['channel'] == 'CATV_15']
        assert len(catv_15_channels) == 2
        for channel in catv_15_channels:
            assert 'tsmfRelTs' in channel
            assert isinstance(channel['tsmfRelTs'], int)
        assert {channel['tsmfRelTs'] for channel in catv_15_channels} == {1, 2}

    def test_single_ts_has_no_tsmf_rel_ts_key(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        catv_28_channel = next(channel for channel in channels if channel['channel'] == 'CATV_28')
        assert 'tsmfRelTs' not in catv_28_channel

    def test_channel_name_prefers_network_name_then_service_name(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        rel_ts_1 = next(channel for channel in channels if channel['channel'] == 'CATV_15' and channel['tsmfRelTs'] == 1)
        assert rel_ts_1['name'] == 'サンプル放送'  # network_name が取れているのでそちらを優先する

        rel_ts_2 = next(channel for channel in channels if channel['channel'] == 'CATV_15' and channel['tsmfRelTs'] == 2)
        assert rel_ts_2['name'] == '自主放送チャンネル'  # network_name が不明なのでサービス名で代用する

    def test_all_entries_are_type_gr_and_enabled(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        assert len(channels) == 3  # TSMF x2 + SingleTS x1 (TLV/Empty は除外される)
        for channel in channels:
            assert channel['type'] == 'GR'
            assert channel['isDisabled'] is False

    def test_tlv_carrier_excluded_but_noted_in_comment(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        assert all(channel['channel'] != 'CATV_C36' for channel in channels)
        assert 'CATV_C36' in formatted_str  # 除外した旨のコメント行に含まれている

    def test_header_mentions_dvbv5_zap_tuner_command(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()

        assert 'dvbv5-zap' in formatted_str
        assert 'tsmfRelTs' in formatted_str

    def test_save_writes_file(self, tmp_path: Path):
        carriers = BuildSyntheticCarriers()
        save_path = tmp_path / 'channels_catv.yml'

        formatted_str = CATVMirakurunChannelsYmlFormatter(save_path, carriers).save()

        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str


class TestCATVMirakcConfigYmlFormatterSynthetic:
    """合成データによる CATVMirakcConfigYmlFormatter のテスト (CI でも実行可能)"""

    def test_tsmf_extra_args_contains_rel_ts_number(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        catv_15_channels = {channel['extra-args']: channel for channel in channels if channel['channel'] == 'CATV_15'}
        assert '1' in catv_15_channels
        assert '2' in catv_15_channels

    def test_single_ts_extra_args_is_empty(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        catv_28_channel = next(channel for channel in channels if channel['channel'] == 'CATV_28')
        assert catv_28_channel['extra-args'] == ''

    def test_filter_command_is_documented(self):
        # mirakc は TSMF を分離できないため、isdb-tsmf-split へパイプする運用例がコメントに含まれていること
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()

        assert 'isdb-tsmf-split' in formatted_str
        assert '--rel-ts' in formatted_str
        assert 'dvbv5-zap' in formatted_str

    def test_tlv_carrier_excluded_but_noted_in_comment(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        assert all(channel['channel'] != 'CATV_C36' for channel in channels)
        assert 'CATV_C36' in formatted_str

    def test_save_writes_file(self, tmp_path: Path):
        carriers = BuildSyntheticCarriers()
        save_path = tmp_path / 'channels_catv.yml'

        formatted_str = CATVMirakcConfigYmlFormatter(save_path, carriers).save()

        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str



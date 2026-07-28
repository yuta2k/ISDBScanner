import json
from pathlib import Path

from ruamel.yaml import YAML

from isdb_scanner.catv.constants import (
    CATV_FREQUENCY_TABLE,
    CarrierType,
    CASInfo,
    CATVCarrierInfo,
    CATVMMTInfo,
    CATVServiceInfo,
    CATVTransportStreamInfo,
    MMTServiceInfo,
    PreferredSource,
)
from isdb_scanner.catv.formatter import (
    CATVDvbv5ConfFormatter,
    CATVJSONFormatter,
    CATVMirakcConfigYmlFormatter,
    CATVMirakcTunersYmlFormatter,
    CATVMirakurunChannelsYmlFormatter,
    CATVMirakurunTunersYmlFormatter,
    GetEmittedCATVChannelTypes,
    NativeJSONFormatter,
    NormalizeChannelName,
    SatelliteJSONFormatter,
)
from isdb_scanner.constants import ServiceInfo, TransportStreamInfo

# tuners 設定へのカード在庫統合のテストで使う、合成のカード検出結果ビルダー
from tests.test_catv_card_assignment import BuildBCASCard, BuildCCASCard


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
    tlv_carrier = CATVCarrierInfo(
        physical_channel='CATV_C36',
        carrier_type=CarrierType.TLV,
        transport_streams=[],
        # MH-SDT からサービス名を取得できているケース (除外注記のコメント行に併記される)
        mmt=CATVMMTInfo(services=[MMTServiceInfo(package_id=0x65, service_id=0x65, service_name='テスト４Ｋ')]),
    )
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
        # MH-SDT 由来のサービス名が取得できていれば、どのチャンネルかを特定しやすいよう併記される
        assert '#   CATV_C36: テスト４Ｋ' in formatted_str

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


def BuildSyntheticRetransmissionCarriers() -> list[CATVCarrierInfo]:
    """
    type マッピング・フィルタ系テスト用に、BS/CS 再送信と CAS 種別のバリエーションを含む合成キャリア一覧を組み立てる
    - CATV_20 (TSMF):
      - 相対TS1: BS 再送信・B-CAS・無料 (映像 + 独立データ放送)
      - 相対TS2: BS 再送信・B-CAS・有料 (WOWOW 型: 有料映像 + 無料独立データ放送)
      - 相対TS3: CS 再送信・B-CAS・有料
    - CATV_30 (SingleTS): 自主放送・C-CAS・有料
    """

    return [
        CATVCarrierInfo(
            physical_channel='CATV_20',
            carrier_type=CarrierType.TSMF,
            transport_streams=[
                CATVTransportStreamInfo(
                    physical_channel='CATV_20',
                    tsmf_relative_ts_number=1,
                    transport_stream_id=16400,
                    network_id=4,
                    network_name='ＢＳデジタル',
                    retransmission_source='BS',
                    cas=CASInfo(required_card='B-CAS'),
                    services=[
                        CATVServiceInfo(service_id=700, service_type=0xC0, service_name='ＢＳ朝日データ', is_free=True),
                        CATVServiceInfo(service_id=151, service_type=0x01, service_name='ＢＳ朝日', is_free=True),
                    ],
                ),
                CATVTransportStreamInfo(
                    physical_channel='CATV_20',
                    tsmf_relative_ts_number=2,
                    transport_stream_id=16626,
                    network_id=4,
                    network_name='ＢＳデジタル',
                    retransmission_source='BS',
                    cas=CASInfo(required_card='B-CAS'),
                    services=[
                        CATVServiceInfo(service_id=191, service_type=0x01, service_name='ＷＯＷＯＷプライム', is_free=False),
                        CATVServiceInfo(service_id=192, service_type=0xC0, service_name='ＷＯＷＯＷデータ', is_free=True),
                    ],
                ),
                CATVTransportStreamInfo(
                    physical_channel='CATV_20',
                    tsmf_relative_ts_number=3,
                    transport_stream_id=24608,
                    network_id=6,
                    network_name='スカパー！',
                    retransmission_source='CS',
                    cas=CASInfo(required_card='B-CAS'),
                    services=[
                        CATVServiceInfo(service_id=237, service_type=0x01, service_name='スターチャンネル', is_free=False),
                    ],
                ),
            ],
        ),
        CATVCarrierInfo(
            physical_channel='CATV_30',
            carrier_type=CarrierType.SingleTS,
            transport_streams=[
                CATVTransportStreamInfo(
                    physical_channel='CATV_30',
                    tsmf_relative_ts_number=None,
                    transport_stream_id=0x3000,
                    network_name='コミュニティ有料',
                    retransmission_source='SelfBroadcast',
                    cas=CASInfo(required_card='C-CAS'),
                    services=[
                        CATVServiceInfo(service_id=1, service_type=0x01, service_name='コミュニティ有料ch', is_free=False),
                    ],
                ),
            ],
        ),
    ]


class TestCATVChannelTypeAndNaming:
    """BS/CS 再送信の type マッピングとサービス名ベースのチャンネル名のテスト (CI でも実行可能)"""

    def test_retransmission_source_maps_to_bs_cs_type(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), BuildSyntheticRetransmissionCarriers()).format()
        channels = LoadYaml(formatted_str)

        rel1 = next(channel for channel in channels if channel.get('tsmfRelTs') == 1)
        rel3 = next(channel for channel in channels if channel.get('tsmfRelTs') == 3)
        self_broadcast = next(channel for channel in channels if channel['channel'] == 'CATV_30')
        assert rel1['type'] == 'BS'  # BS 再送信
        assert rel3['type'] == 'CS'  # CS 再送信
        assert self_broadcast['type'] == 'GR'  # 自主放送は GR のまま

    def test_bs_cs_channel_name_uses_video_service_name(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), BuildSyntheticRetransmissionCarriers()).format()
        channels = LoadYaml(formatted_str)

        rel1 = next(channel for channel in channels if channel.get('tsmfRelTs') == 1)
        # network_name (ＢＳデジタル: BS 全 TS で共通) ではなく、映像サービス (0x01) のサービス名が使われること
        # (サービス一覧の先頭がデータ放送 (0xC0) でも、映像サービスを優先すること)
        assert rel1['name'] == 'ＢＳ朝日'
        rel3 = next(channel for channel in channels if channel.get('tsmfRelTs') == 3)
        assert rel3['name'] == 'スターチャンネル'
        # 地上波再送信・自主放送は従来どおり network_name を使うこと
        self_broadcast = next(channel for channel in channels if channel['channel'] == 'CATV_30')
        assert self_broadcast['name'] == 'コミュニティ有料'

    def test_mirakc_uses_same_type_mapping(self):
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), BuildSyntheticRetransmissionCarriers()).format()
        channels = LoadYaml(formatted_str)

        assert [channel['type'] for channel in channels] == ['BS', 'BS', 'CS', 'GR']
        rel1 = next(channel for channel in channels if channel['name'] == 'ＢＳ朝日')
        assert rel1['extra-args'] == '1'  # extra-args は従来どおり TSMF 相対 TS 番号のまま


class TestCATVPayTvAndCASFiltering:
    """--exclude-pay-tv / --bcas-only / --cas-as-sky に対応するフォーマッター動作のテスト (CI でも実行可能)"""

    def test_exclude_pay_tv_filters_catv_entries(self):
        carriers = BuildSyntheticRetransmissionCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers, exclude_pay_tv=True).format()
        channels = LoadYaml(formatted_str)

        # 無料 BS 再送信 (相対TS1) は残る
        assert any(channel['name'] == 'ＢＳ朝日' for channel in channels)
        # 有料映像 + 無料独立データ放送のみの BS 再送信 (WOWOW 型・相対TS2) は丸ごと除外される
        assert all(channel.get('tsmfRelTs') != 2 for channel in channels)
        # CS 再送信は全サービス除外扱いでエントリ自体が出力されない
        assert all(channel['type'] != 'CS' for channel in channels)
        # 有料自主放送 (is_free=False のみ) も除外される
        assert all(channel['channel'] != 'CATV_30' for channel in channels)

    def test_exclude_pay_tv_does_not_mutate_carriers(self):
        carriers = BuildSyntheticRetransmissionCarriers()
        CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers, exclude_pay_tv=True).format()

        # フィルタリングで元の carriers の services が破壊されないこと
        assert len(carriers[0].transport_streams[1].services) == 2
        assert len(carriers[0].transport_streams[2].services) == 1

    def test_bcas_only_excludes_non_bcas_channels(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'), BuildSyntheticRetransmissionCarriers(), bcas_only=True
        ).format()
        channels = LoadYaml(formatted_str)

        # C-CAS が必要な自主放送は除外され、B-CAS で受信可能なチャンネルのみが残る
        assert all(channel['channel'] != 'CATV_30' for channel in channels)
        assert len(channels) == 3

    def test_bcas_only_excludes_unknown_cas(self):
        # required_card が 'unknown' (スクランブルされているが CAS 種別を特定できない) の TS も除外される
        carriers = BuildSyntheticRetransmissionCarriers()
        carriers[1].transport_streams[0].cas = CASInfo(required_card='unknown')
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers, bcas_only=True).format()
        channels = LoadYaml(formatted_str)
        assert all(channel['channel'] != 'CATV_30' for channel in channels)

    def test_cas_as_sky_outputs_sky_type(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'), BuildSyntheticRetransmissionCarriers(), cas_as_sky=True
        ).format()
        channels = LoadYaml(formatted_str)

        # C-CAS が必要な自主放送は type: SKY になる
        self_broadcast = next(channel for channel in channels if channel['channel'] == 'CATV_30')
        assert self_broadcast['type'] == 'SKY'
        # B-CAS で受信可能なチャンネルの type は変わらない
        rel1 = next(channel for channel in channels if channel.get('tsmfRelTs') == 1)
        assert rel1['type'] == 'BS'

    def test_cas_as_sky_in_mirakc(self):
        formatted_str = CATVMirakcConfigYmlFormatter(
            Path('unused.yml'), BuildSyntheticRetransmissionCarriers(), cas_as_sky=True
        ).format()
        channels = LoadYaml(formatted_str)
        self_broadcast = next(channel for channel in channels if channel['channel'] == 'CATV_30')
        assert self_broadcast['type'] == 'SKY'


def BuildSyntheticBSTsInfos() -> list[TransportStreamInfo]:
    """
    ネイティブ BS/CS 統合出力のテスト用に、合成の BS の TransportStreamInfo 一覧を組み立てる
    - BS01/TS0: 無料放送のみの TS
    - BS03/TS1: 有料放送 + 無料独立データ放送 (0xC0) のみの TS (WOWOW 型。--exclude-pay-tv で丸ごと除外される)
    """

    return [
        TransportStreamInfo(
            physical_channel='BS01/TS0',
            transport_stream_id=16400,
            network_id=4,
            network_name='BS朝日',
            satellite_frequency=11.72748,
            satellite_transponder=1,
            satellite_slot_number=0,
            services=[
                ServiceInfo(channel_number='151', service_id=151, service_type=0x01, service_name='BS朝日', is_free=True),
            ],
        ),
        TransportStreamInfo(
            physical_channel='BS03/TS1',
            transport_stream_id=16626,
            network_id=4,
            network_name='WOWOW',
            satellite_frequency=11.76597,
            satellite_transponder=3,
            satellite_slot_number=1,
            services=[
                ServiceInfo(channel_number='191', service_id=191, service_type=0x01, service_name='WOWOWプライム', is_free=False),
                ServiceInfo(channel_number='192', service_id=192, service_type=0xC0, service_name='WOWOWデータ', is_free=True),
            ],
        ),
    ]


def BuildSyntheticCSTsInfos() -> list[TransportStreamInfo]:
    """ネイティブ BS/CS 統合出力のテスト用に、合成の CS (CS1/CS2) の TransportStreamInfo 一覧を組み立てる"""

    return [
        TransportStreamInfo(
            physical_channel='ND02',
            transport_stream_id=24608,
            network_id=6,
            network_name='スカパー！',
            satellite_frequency=12.291,
            satellite_transponder=2,
            services=[
                ServiceInfo(channel_number='237', service_id=237, service_type=0x01, service_name='スターチャンネル', is_free=False),
            ],
        ),
        TransportStreamInfo(
            physical_channel='ND04',
            transport_stream_id=28736,
            network_id=7,
            network_name='スカパー！',
            satellite_frequency=12.331,
            satellite_transponder=4,
            services=[
                ServiceInfo(channel_number='330', service_id=330, service_type=0x01, service_name='キッズステーション', is_free=False),
            ],
        ),
    ]


class TestSatelliteJSONFormatterSynthetic:
    """合成データによる SatelliteJSONFormatter のテスト (CI でも実行可能)"""

    def test_format_is_ts_info_array_with_computed_fields(self, tmp_path: Path):
        result = json.loads(SatelliteJSONFormatter(tmp_path / 'BS.json', BuildSyntheticBSTsInfos()).format())

        # ネイティブ isdb-scanner の Channels.json の "BS" キーの値と同じ、TS 情報の JSON 配列であること
        assert isinstance(result, list)
        assert [ts_info['physical_channel'] for ts_info in result] == ['BS01/TS0', 'BS03/TS1']
        # computed_field (broadcast_type / physical_channel_recisdb) も含まれること
        assert result[0]['broadcast_type'] == 'BS'
        assert result[0]['physical_channel_recisdb'] == 'BS01_0'
        # 有料放送のフィルタリングは行われない (JSON は常に全チャンネル出力)
        assert result[1]['services'][0]['is_free'] is False

    def test_save_writes_file(self, tmp_path: Path):
        save_path = tmp_path / 'CS.json'
        formatted_str = SatelliteJSONFormatter(save_path, BuildSyntheticCSTsInfos()).save()

        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str
        assert json.loads(formatted_str)[1]['physical_channel_recisdb'] == 'CS04'


class TestCATVMirakurunChannelsYmlFormatterSatellite:
    """ネイティブ BS/CS を統合した CATVMirakurunChannelsYmlFormatter のテスト (CI でも実行可能)"""

    def test_satellite_entries_appended_after_catv_entries(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
        ).format()
        channels = LoadYaml(formatted_str)

        # CATV (GR) エントリの後に BS → CS の順で追記されること
        assert [channel['type'] for channel in channels] == ['GR', 'GR', 'GR', 'BS', 'BS', 'CS', 'CS']

        bs01 = next(channel for channel in channels if channel['name'] == 'BS01/TS0')
        assert bs01['type'] == 'BS'
        assert bs01['channel'] == 'BS01_0'  # recisdb 互換フォーマット
        assert bs01['satellite'] == ' --tsid 16400 '  # 前後の半角スペースも保持されること
        assert bs01['isDisabled'] is False
        assert 'tsmfRelTs' not in bs01

        nd04 = next(channel for channel in channels if channel['name'] == 'ND04')
        assert nd04['type'] == 'CS'
        assert nd04['channel'] == 'CS04'
        assert nd04['satellite'] == ' --tsid 28736 '

    def test_header_mentions_recisdb_tuner_example_only_with_satellite(self):
        carriers = BuildSyntheticCarriers()

        with_satellite = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            carriers,
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
        ).format()
        assert 'recisdb' in with_satellite

        # BS/CS なしの場合は従来と同一の出力 (ヘッダーにも recisdb 関連の記述が入らない) になること
        without_satellite = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        assert 'recisdb' not in without_satellite
        assert without_satellite == CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'), carriers, tr_ts_infos=[], bs_ts_infos=[], cs_ts_infos=[], exclude_pay_tv=False
        ).format()

    def test_exclude_pay_tv_removes_pay_only_ts_and_cs(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
            exclude_pay_tv=True,
        ).format()
        channels = LoadYaml(formatted_str)

        # 無料放送を含む BS01/TS0 は残る
        assert any(channel['name'] == 'BS01/TS0' for channel in channels)
        # 有料放送 + 独立データ放送のみの BS03/TS1 (WOWOW 型) は丸ごと除外される
        assert all(channel['name'] != 'BS03/TS1' for channel in channels)
        # CS は全サービスが除外されるため、エントリ自体が出力されない
        assert all(channel['type'] != 'CS' for channel in channels)
        # CATV (GR) エントリのうち、無料サービスを含むものは残る
        # (サービスが1つも解析できていない CATV_28 は、無料放送を確認できないため exclude_pay_tv では除外される)
        assert len([channel for channel in channels if channel['type'] == 'GR']) == 2
        assert all(channel['channel'] != 'CATV_28' for channel in channels)

    def test_exclude_pay_tv_does_not_mutate_input(self):
        bs_ts_infos = BuildSyntheticBSTsInfos()
        cs_ts_infos = BuildSyntheticCSTsInfos()
        CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            bs_ts_infos=bs_ts_infos,
            cs_ts_infos=cs_ts_infos,
            exclude_pay_tv=True,
        ).format()

        # 既存 BaseFormatter と異なり、渡したリストの services が in-place で破壊されないこと
        assert len(bs_ts_infos[1].services) == 2
        assert len(cs_ts_infos[0].services) == 1


class TestCATVMirakcConfigYmlFormatterSatellite:
    """ネイティブ BS/CS を統合した CATVMirakcConfigYmlFormatter のテスト (CI でも実行可能)"""

    def test_satellite_entries_use_tsid_extra_args(self):
        formatted_str = CATVMirakcConfigYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
        ).format()
        channels = LoadYaml(formatted_str)

        assert [channel['type'] for channel in channels] == ['GR', 'GR', 'GR', 'BS', 'BS', 'CS', 'CS']

        bs01 = next(channel for channel in channels if channel['name'] == 'BS01/TS0')
        assert bs01['channel'] == 'BS01_0'
        assert bs01['extra-args'] == '--tsid 16400'
        assert bs01['disabled'] is False

        nd02 = next(channel for channel in channels if channel['name'] == 'ND02')
        assert nd02['type'] == 'CS'
        assert nd02['channel'] == 'CS02'
        assert nd02['extra-args'] == '--tsid 24608'

        # CATV (GR) エントリの extra-args (TSMF 相対 TS 番号) はそのまま維持されること
        catv_15_extra_args = {channel['extra-args'] for channel in channels if channel['channel'].startswith('CATV_15')}
        assert catv_15_extra_args == {'1', '2'}

    def test_output_identical_to_previous_without_satellite(self):
        carriers = BuildSyntheticCarriers()
        without_satellite = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        assert 'recisdb' not in without_satellite
        assert without_satellite == CATVMirakcConfigYmlFormatter(
            Path('unused.yml'), carriers, tr_ts_infos=[], bs_ts_infos=[], cs_ts_infos=[], exclude_pay_tv=False
        ).format()

    def test_exclude_pay_tv_removes_pay_only_ts_and_cs(self):
        formatted_str = CATVMirakcConfigYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
            exclude_pay_tv=True,
        ).format()
        channels = LoadYaml(formatted_str)

        assert any(channel['name'] == 'BS01/TS0' for channel in channels)
        assert all(channel['name'] != 'BS03/TS1' for channel in channels)
        assert all(channel['type'] != 'CS' for channel in channels)


class TestCATVMirakcConfigYmlFormatterSynthetic:
    """合成データによる CATVMirakcConfigYmlFormatter のテスト (CI でも実行可能)"""

    def test_tsmf_extra_args_contains_rel_ts_number(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        # channel は相対 TS ごとにユニーク化されるが、extra-args は従来どおり相対 TS 番号のまま出力される
        catv_15_channels = {channel['extra-args']: channel for channel in channels if channel['channel'].startswith('CATV_15')}
        assert '1' in catv_15_channels
        assert '2' in catv_15_channels
        assert catv_15_channels['1']['channel'] == 'CATV_15#1'
        assert catv_15_channels['2']['channel'] == 'CATV_15#2'

    def test_tsmf_channel_is_uniquified_and_single_ts_is_not(self):
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        # TSMF の各相対 TS は <物理チャンネル名>#<相対TS番号> に、SingleTS は物理チャンネル名のまま
        assert {channel['channel'] for channel in channels} == {'CATV_15#1', 'CATV_15#2', 'CATV_28'}
        # 物理チャンネル名そのままの TSMF エントリ (マージされてしまう形) が残っていないこと
        assert all(channel['channel'] != 'CATV_15' for channel in channels)

    def test_uniquified_channel_survives_yaml_round_trip(self):
        # プレーンスカラー中の `#` がコメント扱いされず、パースし直しても値が壊れないこと
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()

        assert 'CATV_15#1' in formatted_str
        channels = LoadYaml(formatted_str)
        rel_ts_1 = next(channel for channel in channels if channel['extra-args'] == '1')
        assert rel_ts_1['channel'] == 'CATV_15#1'
        assert rel_ts_1['name'] == 'サンプル放送'  # `#` 以降が切り落とされて後続キーが消えたりしていないこと
        assert rel_ts_1['type'] == 'GR'
        assert rel_ts_1['disabled'] is False

    def test_same_type_relative_ts_channels_are_all_unique(self):
        # 同一 type (GR) の相対 TS が複数ある場合でも channel 名が全てユニークであること
        # (mirakc の ChannelConfig::normalize() による (type, channel) 単位のマージを避けるための回帰テスト)
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        gr_channels = [channel['channel'] for channel in channels if channel['type'] == 'GR']
        assert len(gr_channels) == 3
        assert len(set(gr_channels)) == len(gr_channels)

        # BS/CS 再送信が混在するキャリアでも、(type, channel) の組が重複しないこと
        formatted_str = CATVMirakcConfigYmlFormatter(Path('unused.yml'), BuildSyntheticRetransmissionCarriers()).format()
        channels = LoadYaml(formatted_str)
        keys = [(channel['type'], channel['channel']) for channel in channels]
        assert len(set(keys)) == len(keys)

    def test_mirakurun_channel_is_not_uniquified(self):
        # Mirakurun は tsmfRelTs をネイティブサポートしているため、channel は物理チャンネル名のまま維持されること
        carriers = BuildSyntheticCarriers()
        formatted_str = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        channels = LoadYaml(formatted_str)

        assert {channel['channel'] for channel in channels} == {'CATV_15', 'CATV_28'}
        assert all('#' not in channel['channel'] for channel in channels)

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


def BuildSyntheticTerrestrialTsInfos() -> list[TransportStreamInfo]:
    """
    ネイティブ地上波統合出力のテスト用に、合成の地上波の TransportStreamInfo 一覧を組み立てる
    - T21/TSID 0x1000 (=4096): BuildSyntheticCarriers の CATV_15 相対TS1 (Terrestrial・同 TSID) と重複する地上波 TS
    """

    return [
        TransportStreamInfo(
            physical_channel='T21',
            transport_stream_id=0x1000,
            network_id=32752,
            network_name='サンプル放送',
            remote_control_key_id=5,
            services=[
                ServiceInfo(channel_number='051', service_id=1024, service_type=0x01, service_name='サンプルサービス1', is_free=True),
            ],
        ),
    ]


class TestNativeJSONFormatterAndAlias:
    """NativeJSONFormatter (旧 SatelliteJSONFormatter) と後方互換エイリアスのテスト"""

    def test_alias_points_to_native_formatter(self):
        # 後方互換エイリアスが NativeJSONFormatter を指していること
        assert SatelliteJSONFormatter is NativeJSONFormatter

    def test_native_json_formatter_handles_terrestrial(self, tmp_path: Path):
        result = json.loads(NativeJSONFormatter(tmp_path / 'Terrestrial.json', BuildSyntheticTerrestrialTsInfos()).format())

        assert isinstance(result, list)
        assert result[0]['physical_channel'] == 'T21'
        assert result[0]['broadcast_type'] == 'Terrestrial'
        assert result[0]['physical_channel_recisdb'] == 'T21'


class TestCATVTerrestrialIntegration:
    """ネイティブ地上波を統合した CATVMirakurunChannelsYmlFormatter / CATVMirakcConfigYmlFormatter のテスト"""

    def test_terrestrial_appended_before_bs_cs(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            tr_ts_infos=BuildSyntheticTerrestrialTsInfos(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
        ).format()
        channels = LoadYaml(formatted_str)

        # CATV (GR) x3 の後に ネイティブ 地上波(GR) → BS → CS の順で追記されること
        assert [channel['type'] for channel in channels] == ['GR', 'GR', 'GR', 'GR', 'BS', 'BS', 'CS', 'CS']

        native_terrestrial = next(channel for channel in channels if channel['channel'] == 'T21')
        assert native_terrestrial['type'] == 'GR'
        assert native_terrestrial['name'] == 'サンプル放送'  # network_name (= TS 名 = 放送局名) が使われること
        assert native_terrestrial['satellite'] == ' '  # 地上波は satellite が半角スペース1個
        assert native_terrestrial['isDisabled'] is False

    def test_terrestrial_in_mirakc_has_empty_extra_args(self):
        formatted_str = CATVMirakcConfigYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticCarriers(),
            tr_ts_infos=BuildSyntheticTerrestrialTsInfos(),
        ).format()
        channels = LoadYaml(formatted_str)

        native_terrestrial = next(channel for channel in channels if channel['channel'] == 'T21')
        assert native_terrestrial['type'] == 'GR'
        assert native_terrestrial['name'] == 'サンプル放送'
        assert native_terrestrial['extra-args'] == ''  # 地上波は extra-args が空文字列
        assert native_terrestrial['disabled'] is False

    def test_terrestrial_only_still_backward_compatible_default(self):
        # tr/bs/cs すべて省略時は従来と完全に同一の出力になること (既定値互換)
        carriers = BuildSyntheticCarriers()
        default_output = CATVMirakurunChannelsYmlFormatter(Path('unused.yml'), carriers).format()
        explicit_empty = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'), carriers, tr_ts_infos=[], bs_ts_infos=[], cs_ts_infos=[]
        ).format()
        assert default_output == explicit_empty


class TestNormalizeChannelName:
    """NormalizeChannelName ヘルパーと normalize_names パラメータのテスト"""

    def test_fullwidth_alnum_and_symbols_converted(self):
        assert NormalizeChannelName('ＢＳ朝日') == 'BS朝日'  # 全角ラテン英字 → 半角、漢字はそのまま
        assert NormalizeChannelName('ＷＯＷＯＷプライム') == 'WOWOWプライム'
        assert NormalizeChannelName('ＮＨＫ　ＢＳ１') == 'NHK BS1'  # 全角スペース (U+3000) → 半角スペース
        assert NormalizeChannelName('！？＃＆') == '!?#&'  # 全角記号 → 半角記号

    def test_hiragana_katakana_unchanged(self):
        # ひらがな・カタカナ・記号以外の全角文字はそのまま維持する
        assert NormalizeChannelName('スターチャンネル') == 'スターチャンネル'

    def test_normalize_names_applies_to_name_field_mirakurun(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'), BuildSyntheticRetransmissionCarriers(), normalize_names=True
        ).format()
        channels = LoadYaml(formatted_str)

        # ＢＳ朝日 → BS朝日 に正規化されること
        assert any(channel['name'] == 'BS朝日' for channel in channels)
        assert all(channel['name'] != 'ＢＳ朝日' for channel in channels)

    def test_normalize_names_false_keeps_fullwidth(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'), BuildSyntheticRetransmissionCarriers()
        ).format()
        channels = LoadYaml(formatted_str)
        assert any(channel['name'] == 'ＢＳ朝日' for channel in channels)

    def test_normalize_names_applies_in_mirakc(self):
        formatted_str = CATVMirakcConfigYmlFormatter(
            Path('unused.yml'), BuildSyntheticRetransmissionCarriers(), normalize_names=True
        ).format()
        channels = LoadYaml(formatted_str)
        assert any(channel['name'] == 'BS朝日' for channel in channels)


class TestPreferDeduplication:
    """--prefer による重複チャンネルの自動 disable のテスト"""

    def test_prefer_none_all_enabled(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticRetransmissionCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
        ).format()
        channels = LoadYaml(formatted_str)
        assert all(channel['isDisabled'] is False for channel in channels)

    def test_prefer_catv_disables_duplicated_native_entries(self):
        formatted_str = CATVMirakurunChannelsYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticRetransmissionCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
            prefer=PreferredSource.CATV,
        ).format()
        channels = LoadYaml(formatted_str)

        # CATV 再送信側 (channel が CATV_* のエントリ) はすべて有効のまま
        assert all(channel['isDisabled'] is False for channel in channels if channel['channel'].startswith('CATV_'))
        # ネイティブ BS/CS で CATV 再送信と TSID が重複するエントリは disable される
        bs01 = next(channel for channel in channels if channel['name'] == 'BS01/TS0')  # BS TSID 16400 が重複
        assert bs01['isDisabled'] is True
        nd02 = next(channel for channel in channels if channel['name'] == 'ND02')  # CS TSID 24608 が重複
        assert nd02['isDisabled'] is True
        # 重複しないネイティブ CS (ND04 TSID 28736) は有効のまま
        nd04 = next(channel for channel in channels if channel['name'] == 'ND04')
        assert nd04['isDisabled'] is False
        # ヘッダーに disable した旨のコメントが追記されること
        assert 'catv' in formatted_str and 'isDisabled: true' in formatted_str

    def test_prefer_native_disables_duplicated_catv_entries(self):
        formatted_str = CATVMirakcConfigYmlFormatter(
            Path('unused.yml'),
            BuildSyntheticRetransmissionCarriers(),
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
            prefer=PreferredSource.NATIVE,
        ).format()
        channels = LoadYaml(formatted_str)

        # ネイティブ側 (channel が T**/BS**_*/CS** のエントリ) はすべて有効のまま
        assert all(
            channel['disabled'] is False for channel in channels if not channel['channel'].startswith('CATV_')
        )
        # CATV 再送信 BS (TSID 16400) は重複するため disable される
        # (TSMF エントリの channel は相対 TS ごとにユニーク化された名前になる)
        catv_bs = next(channel for channel in channels if channel['channel'] == 'CATV_20#1')
        assert catv_bs['extra-args'] == '1'
        assert catv_bs['disabled'] is True
        # CATV 再送信 CS (TSID 24608) も重複するため disable される
        catv_cs = next(channel for channel in channels if channel['channel'] == 'CATV_20#3')
        assert catv_cs['extra-args'] == '3'
        assert catv_cs['disabled'] is True
        # 自主放送 (CATV_30・ネイティブに対応なし) は有効のまま
        self_broadcast = next(channel for channel in channels if channel['channel'] == 'CATV_30')
        assert self_broadcast['disabled'] is False


class TestGetEmittedCATVChannelTypes:
    """GetEmittedCATVChannelTypes ヘルパーのテスト"""

    def test_returns_types_in_fixed_order(self):
        types = GetEmittedCATVChannelTypes(BuildSyntheticRetransmissionCarriers())
        # CATV_20 (BS/BS/CS) + CATV_30 (GR) → GR → BS → CS の順
        assert types == ['GR', 'BS', 'CS']

    def test_cas_as_sky_adds_sky_type(self):
        types = GetEmittedCATVChannelTypes(BuildSyntheticRetransmissionCarriers(), cas_as_sky=True)
        # C-CAS が必要な CATV_30 (自主放送) が SKY になる
        assert types == ['BS', 'CS', 'SKY']

    def test_only_gr_for_terrestrial_only_carriers(self):
        assert GetEmittedCATVChannelTypes(BuildSyntheticCarriers()) == ['GR']

    def test_prefer_does_not_change_result(self):
        # prefer や tr/bs/cs 引数を渡しても CATV エントリの type 集合は変わらない
        base = GetEmittedCATVChannelTypes(BuildSyntheticRetransmissionCarriers())
        with_prefer = GetEmittedCATVChannelTypes(
            BuildSyntheticRetransmissionCarriers(),
            prefer=PreferredSource.NATIVE,
            bs_ts_infos=BuildSyntheticBSTsInfos(),
            cs_ts_infos=BuildSyntheticCSTsInfos(),
        )
        assert base == with_prefer


class _FakeCATVTuner:
    """CATVMirakurunTunersYmlFormatter / CATVMirakcTunersYmlFormatter が参照する属性のみを持つ CATVTuner の代替"""

    def __init__(self, name: str, adapter_number: int) -> None:
        self.name = name
        self.adapter_number = adapter_number


class _FakeISDBTuner:
    """tuners フォーマッターが参照する属性のみを持つ ISDBTuner の代替"""

    def __init__(self, name: str, device_path: str, tuner_type: str, tsid_supported: bool) -> None:
        self.name = name
        self.device_path = device_path
        self.type = tuner_type
        self._tsid_supported = tsid_supported

    def isTSIDSelectionSupported(self) -> bool:
        return self._tsid_supported


class TestCATVTunersYmlFormatter:
    """tuners 設定自動生成 (CATVMirakurunTunersYmlFormatter / CATVMirakcTunersYmlFormatter) のテスト"""

    def test_mirakurun_catv_and_isdb_tuner_entries(self):
        catv_tuners = [_FakeCATVTuner('DVB-C Tuner', 0)]
        isdbt_tuners = [_FakeISDBTuner('ISDB-T Tuner', '/dev/pt3video0', 'ISDB-T', False)]
        isdbs_tuners = [_FakeISDBTuner('ISDB-S Tuner', '/dev/px4video0', 'ISDB-S', True)]
        formatted_str = CATVMirakurunTunersYmlFormatter(
            Path('unused.yml'), catv_tuners, isdbt_tuners, isdbs_tuners, Path('/tmp/dvbv5.conf'), ['GR', 'BS', 'CS']
        ).format()
        tuners = LoadYaml(formatted_str)

        catv = next(tuner for tuner in tuners if 'adapter0' in tuner['name'])
        assert catv['name'] == 'DVB-C Tuner (adapter0)'
        assert catv['types'] == ['GR', 'BS', 'CS']
        assert catv['command'] == 'dvbv5-zap -c /tmp/dvbv5.conf -a 0 -P -t 0 -o - <channel>'
        assert catv['isDisabled'] is False

        isdbt = next(tuner for tuner in tuners if tuner['name'] == 'ISDB-T Tuner')
        assert isdbt['types'] == ['GR']
        assert isdbt['command'] == 'recisdb tune --device /dev/pt3video0 --channel <channel> -'

        isdbs = next(tuner for tuner in tuners if tuner['name'] == 'ISDB-S Tuner')
        assert isdbs['types'] == ['BS', 'CS']
        # TSID 選局対応の ISDB-S では <satellite> が連続スペースを避ける形で埋め込まれる
        assert isdbs['command'] == 'recisdb tune --device /dev/px4video0 --channel <channel><satellite>-'

    def test_mirakc_catv_and_isdb_tuner_entries(self):
        catv_tuners = [_FakeCATVTuner('DVB-C Tuner', 1)]
        isdbs_tuners = [_FakeISDBTuner('ISDB-S Tuner', '/dev/px4video0', 'ISDB-S', True)]
        formatted_str = CATVMirakcTunersYmlFormatter(
            Path('unused.yml'), catv_tuners, [], isdbs_tuners, Path('/tmp/dvbv5.conf'), ['GR', 'BS', 'CS']
        ).format()
        tuners = LoadYaml(formatted_str)

        catv = next(tuner for tuner in tuners if 'adapter1' in tuner['name'])
        assert catv['command'] == 'dvbv5-zap -c /tmp/dvbv5.conf -a 1 -P -t 0 -o - {{{channel}}}'
        assert catv['disabled'] is False

        isdbs = next(tuner for tuner in tuners if tuner['name'] == 'ISDB-S Tuner')
        assert isdbs['command'] == 'recisdb tune --device /dev/px4video0 --channel {{{channel}}} {{{extra_args}}} -'
        # isdb-tsmf-split のパイプが必要な旨がヘッダーコメントに含まれること
        assert 'isdb-tsmf-split' in formatted_str

    def test_empty_section_omitted(self):
        # ISDB-T チューナーが空ならそのセクションは出力されない (CATV/ISDB-S のみ)
        formatted_str = CATVMirakurunTunersYmlFormatter(
            Path('unused.yml'), [_FakeCATVTuner('DVB-C Tuner', 0)], [], [], Path('/tmp/dvbv5.conf'), ['GR']
        ).format()
        tuners = LoadYaml(formatted_str)
        assert len(tuners) == 1
        assert tuners[0]['name'] == 'DVB-C Tuner (adapter0)'

    def test_all_empty_returns_comment_only(self):
        # 全チューナーが空なら説明コメントのみ (YAML 本体なし) を返す
        formatted_str = CATVMirakurunTunersYmlFormatter(
            Path('unused.yml'), [], [], [], Path('/tmp/dvbv5.conf'), []
        ).format()
        assert LoadYaml(formatted_str) is None
        assert formatted_str.lstrip().startswith('#')

    def test_save_writes_file(self, tmp_path: Path):
        save_path = tmp_path / 'tuners.yml'
        formatted_str = CATVMirakcTunersYmlFormatter(
            save_path, [_FakeCATVTuner('DVB-C Tuner', 0)], [], [], Path('/tmp/dvbv5.conf'), ['GR']
        ).save()
        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str


class TestCATVTunersYmlFormatterCardAssignment:
    """tuners 設定へのカード在庫 (DetectedCard) 統合のテスト"""

    def _MirakurunFormat(self, detected_cards, save_file_path: Path = Path('/output/Mirakurun/tuners_catv.yml')) -> str:
        return CATVMirakurunTunersYmlFormatter(
            save_file_path,
            [_FakeCATVTuner('DVB-C Tuner', 0)],
            [],
            [],
            Path('/tmp/dvbv5.conf'),
            ['GR', 'SKY'],
            detected_cards,
        ).format()

    def _MirakcFormat(self, detected_cards, save_file_path: Path = Path('/output/mirakc/tuners_catv.yml')) -> str:
        return CATVMirakcTunersYmlFormatter(
            save_file_path,
            [_FakeCATVTuner('DVB-C Tuner', 0)],
            [],
            [],
            Path('/tmp/dvbv5.conf'),
            ['GR', 'SKY'],
            detected_cards,
        ).format()

    def test_none_keeps_backward_compatible_output(self):
        # カード在庫を渡さなかった場合 (None) は、引数を省略したときと完全に同一の出力になること (後方互換)
        mirakurun_omitted = CATVMirakurunTunersYmlFormatter(
            Path('/output/Mirakurun/tuners_catv.yml'),
            [_FakeCATVTuner('DVB-C Tuner', 0)],
            [],
            [],
            Path('/tmp/dvbv5.conf'),
            ['GR', 'SKY'],
        ).format()
        assert self._MirakurunFormat(None) == mirakurun_omitted
        # decoder キーもカード関連のコメントも一切出力されないこと
        assert 'decoder' not in mirakurun_omitted
        assert 'CAS カード' not in mirakurun_omitted
        assert LoadYaml(mirakurun_omitted)[0].get('decoder') is None

        mirakc_omitted = CATVMirakcTunersYmlFormatter(
            Path('/output/mirakc/tuners_catv.yml'),
            [_FakeCATVTuner('DVB-C Tuner', 0)],
            [],
            [],
            Path('/tmp/dvbv5.conf'),
            ['GR', 'SKY'],
        ).format()
        assert self._MirakcFormat(None) == mirakc_omitted
        # filters.decode-filter の設定例 (decode-filter: / decode-filter.sh) は一切出力されないこと
        ## ヘッダーコメントには TSMF 分離の説明として decode-filter という語自体は登場するため、
        ## カード関連コメントの有無は設定キー・スクリプト名の形で判定する
        assert 'decode-filter:' not in mirakc_omitted
        assert 'decode-filter.sh' not in mirakc_omitted
        assert 'CAS カード' not in mirakc_omitted

    def test_bcas_only_uses_arib_b25_stream_test(self):
        formatted_str = self._MirakurunFormat([BuildBCASCard()])
        catv_tuner = LoadYaml(formatted_str)[0]
        # 単一カード環境ではリーダーを選ぶ必要がないため、upstream と同じ arib-b25-stream-test を指定する
        assert catv_tuner['decoder'] == 'arib-b25-stream-test'
        assert 'B-CAS カードのみを検出した' in formatted_str

    def test_ccas_only_uses_arib_b25_stream_test(self):
        formatted_str = self._MirakurunFormat([BuildCCASCard()])
        assert LoadYaml(formatted_str)[0]['decoder'] == 'arib-b25-stream-test'
        assert 'C-CAS カードのみを検出した' in formatted_str

    def test_both_cards_use_generated_wrapper_script(self):
        formatted_str = self._MirakurunFormat([BuildBCASCard(), BuildCCASCard()])
        catv_tuner = LoadYaml(formatted_str)[0]
        # 混在環境では、tuners.yml と同じディレクトリに生成されるラッパースクリプトを decoder に指定する
        assert catv_tuner['decoder'] == '/output/Mirakurun/decoder-bcas.sh'
        # SKY 専用チューナー + decoder-ccas.sh の構成例がヘッダーコメントで示されること
        assert '--cas-as-sky' in formatted_str
        assert '/output/Mirakurun/decoder-ccas.sh' in formatted_str
        # アダプタを共有してはならないこと・その理由 (Mirakurun に排他制御が無いこと) が明示されること
        assert 'Tuner.ts' in formatted_str
        assert '二重起動' in formatted_str
        assert '別のアダプタ' in formatted_str

    def test_no_card_detected_omits_decoder(self):
        # カードを 1 枚も検出できなかった場合は decoder を出力せず、確認方法だけを案内する
        formatted_str = self._MirakurunFormat([])
        assert LoadYaml(formatted_str)[0].get('decoder') is None
        assert '--list-card-readers' in formatted_str

    def test_mirakc_header_includes_decode_filter_example(self):
        formatted_str = self._MirakcFormat([BuildBCASCard(), BuildCCASCard()])
        assert 'filters:' in formatted_str
        assert 'decode-filter:' in formatted_str
        assert '/output/mirakc/decode-filter.sh' in formatted_str
        # mustache 変数の実名が書かれていること
        assert '{{{channel_name}}}' in formatted_str
        assert '{{{channel_type}}}' in formatted_str
        assert '{{{channel}}}' in formatted_str

    def test_mirakc_single_card_says_default_is_enough(self):
        formatted_str = self._MirakcFormat([BuildBCASCard()])
        assert '既定の filters.decode-filter.command の設定で足りる' in formatted_str
        assert '/output/mirakc/decode-filter.sh' not in formatted_str

    def test_mirakc_no_card_detected(self):
        formatted_str = self._MirakcFormat([])
        assert '--list-card-readers' in formatted_str



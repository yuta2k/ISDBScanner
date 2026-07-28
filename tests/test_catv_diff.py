import json
from typing import Any

from isdb_scanner.catv.constants import (
    CarrierType,
    CASInfo,
    CATVCarrierInfo,
    CATVMMTInfo,
    CATVServiceInfo,
    CATVSignalStats,
    CATVTransportStreamInfo,
    MMTSDTServiceInfo,
    MMTServiceInfo,
)
from isdb_scanner.catv.diff import (
    SIGNAL_CNR_DROP_WARNING_THRESHOLD_DB,
    CompareScanResults,
    FormatScanDiff,
    FormatScanDiffJSON,
)


def BuildCarrierDict(
    physical_channel: str,
    carrier_type: CarrierType = CarrierType.SingleTS,
    transport_streams: list[CATVTransportStreamInfo] | None = None,
    signal_stats: CATVSignalStats | None = None,
) -> dict[str, Any]:
    """テスト用に、CATV.json の1エントリ分 (json.loads() 済みの dict) を CATVCarrierInfo から組み立てる"""

    carrier = CATVCarrierInfo(
        physical_channel=physical_channel,
        carrier_type=carrier_type,
        transport_streams=transport_streams or [],
        signal_stats=signal_stats,
    )
    return carrier.model_dump(mode='json')


def BuildTransportStream(
    transport_stream_id: int,
    services: list[CATVServiceInfo] | None = None,
    required_card: str = 'none',
    retransmission_source: str = 'Terrestrial',
    network_name: str = 'Test Network',
) -> CATVTransportStreamInfo:
    return CATVTransportStreamInfo(
        physical_channel='CATV_15',
        transport_stream_id=transport_stream_id,
        network_name=network_name,
        retransmission_source=retransmission_source,  # type: ignore[arg-type]
        cas=CASInfo(required_card=required_card),  # type: ignore[arg-type]
        services=services or [],
    )


class TestCompareScanResultsChannelAddRemove:
    def test_added_channel(self):
        previous: dict[str, Any] = {}
        current = {'CATV_15': BuildCarrierDict('CATV_15', carrier_type=CarrierType.SingleTS)}

        diff = CompareScanResults(previous, current)

        assert diff.has_changes is True
        assert len(diff.added_channels) == 1
        assert diff.added_channels[0].physical_channel == 'CATV_15'
        assert diff.added_channels[0].carrier_type == CarrierType.SingleTS
        assert len(diff.removed_channels) == 0
        assert len(diff.changed_channels) == 0

    def test_removed_channel(self):
        previous = {'CATV_15': BuildCarrierDict('CATV_15', carrier_type=CarrierType.SingleTS)}
        current: dict[str, Any] = {}

        diff = CompareScanResults(previous, current)

        assert diff.has_changes is True
        assert len(diff.removed_channels) == 1
        assert diff.removed_channels[0].physical_channel == 'CATV_15'
        assert len(diff.added_channels) == 0
        assert len(diff.changed_channels) == 0

    def test_no_changes(self):
        carrier_dict = BuildCarrierDict(
            'CATV_15',
            carrier_type=CarrierType.SingleTS,
            transport_streams=[
                BuildTransportStream(
                    0x1001,
                    services=[CATVServiceInfo(service_id=100, service_name='Service A')],
                )
            ],
        )
        previous = {'CATV_15': carrier_dict}
        current = {'CATV_15': carrier_dict}

        diff = CompareScanResults(previous, current)

        assert diff.has_changes is False
        assert diff.added_channels == []
        assert diff.removed_channels == []
        assert diff.changed_channels == []


class TestCompareScanResultsChannelChanged:
    def test_carrier_type_changed(self):
        previous = {'CATV_15': BuildCarrierDict('CATV_15', carrier_type=CarrierType.SingleTS)}
        current = {'CATV_15': BuildCarrierDict('CATV_15', carrier_type=CarrierType.TSMF)}

        diff = CompareScanResults(previous, current)

        assert len(diff.changed_channels) == 1
        change = diff.changed_channels[0]
        assert change.previous_carrier_type == CarrierType.SingleTS
        assert change.current_carrier_type == CarrierType.TSMF

    def test_transport_stream_added_and_removed(self):
        previous = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[BuildTransportStream(0x1001, network_name='Old TS')],
            )
        }
        current = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[BuildTransportStream(0x2002, network_name='New TS')],
            )
        }

        diff = CompareScanResults(previous, current)

        assert len(diff.changed_channels) == 1
        change = diff.changed_channels[0]
        assert len(change.added_transport_streams) == 1
        assert change.added_transport_streams[0].transport_stream_id == 0x2002
        assert change.added_transport_streams[0].network_name == 'New TS'
        assert len(change.removed_transport_streams) == 1
        assert change.removed_transport_streams[0].transport_stream_id == 0x1001
        assert change.removed_transport_streams[0].network_name == 'Old TS'

    def test_service_added_removed_and_renamed(self):
        previous = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(
                        0x1001,
                        services=[
                            CATVServiceInfo(service_id=100, service_name='Service A'),
                            CATVServiceInfo(service_id=200, service_name='Service B'),
                        ],
                    )
                ],
            )
        }
        current = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(
                        0x1001,
                        services=[
                            CATVServiceInfo(service_id=100, service_name='Service A Renamed'),
                            CATVServiceInfo(service_id=300, service_name='Service C'),
                        ],
                    )
                ],
            )
        }

        diff = CompareScanResults(previous, current)

        assert len(diff.changed_channels) == 1
        change = diff.changed_channels[0]

        assert len(change.added_services) == 1
        assert change.added_services[0].service_id == 300
        assert change.added_services[0].service_name == 'Service C'

        assert len(change.removed_services) == 1
        assert change.removed_services[0].service_id == 200
        assert change.removed_services[0].service_name == 'Service B'

        assert len(change.renamed_services) == 1
        assert change.renamed_services[0].service_id == 100
        assert change.renamed_services[0].previous_service_name == 'Service A'
        assert change.renamed_services[0].service_name == 'Service A Renamed'

    def test_cas_and_retransmission_source_changed(self):
        previous = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(0x1001, required_card='none', retransmission_source='Terrestrial'),
                ],
            )
        }
        current = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(0x1001, required_card='B-CAS', retransmission_source='BS'),
                ],
            )
        }

        diff = CompareScanResults(previous, current)

        assert len(diff.changed_channels) == 1
        change = diff.changed_channels[0]

        assert len(change.cas_changes) == 1
        assert change.cas_changes[0].previous_required_card == 'none'
        assert change.cas_changes[0].current_required_card == 'B-CAS'

        assert len(change.retransmission_source_changes) == 1
        assert change.retransmission_source_changes[0].previous_source == 'Terrestrial'
        assert change.retransmission_source_changes[0].current_source == 'BS'


def BuildTLVCarrierDict(
    physical_channel: str, services: list[tuple[int, str]], sdt_services: list[tuple[int, str]] | None = None
) -> dict[str, Any]:
    """テスト用に、TLV (4K/8K MMT) キャリアの CATV.json 1エントリ分の dict を組み立てる"""

    carrier = CATVCarrierInfo(
        physical_channel=physical_channel,
        carrier_type=CarrierType.TLV,
        transport_streams=[],
        mmt=CATVMMTInfo(
            services=[MMTServiceInfo(package_id=package_id, service_name=service_name) for package_id, service_name in services],
            sdt_services=[
                MMTSDTServiceInfo(service_id=service_id, service_name=service_name) for service_id, service_name in sdt_services or []
            ],
        ),
    )
    return carrier.model_dump(mode='json')


class TestCompareScanResultsMMTServices:
    """TLV キャリア (transport_streams が空) の MMT サービス単位の差分検出のテスト"""

    def test_mmt_service_added_removed_and_renamed(self):
        previous = {'CATV_C36': BuildTLVCarrierDict('CATV_C36', [(0x01, 'Service A'), (0x02, 'Service B')])}
        current = {'CATV_C36': BuildTLVCarrierDict('CATV_C36', [(0x01, 'Service A Renamed'), (0x03, 'Service C')])}

        diff = CompareScanResults(previous, current)

        assert len(diff.changed_channels) == 1
        change = diff.changed_channels[0]

        assert [service.package_id for service in change.added_mmt_services] == [0x03]
        assert change.added_mmt_services[0].service_name == 'Service C'
        assert [service.package_id for service in change.removed_mmt_services] == [0x02]
        assert len(change.renamed_mmt_services) == 1
        assert change.renamed_mmt_services[0].package_id == 0x01
        assert change.renamed_mmt_services[0].previous_service_name == 'Service A'
        assert change.renamed_mmt_services[0].service_name == 'Service A Renamed'

        text = FormatScanDiff(diff)
        assert '+ MMT service added: package_id=0x0003 (Service C)' in text
        assert '- MMT service removed: package_id=0x0002 (Service B)' in text
        assert '~ MMT service renamed: package_id=0x0001 "Service A" -> "Service A Renamed"' in text

    def test_identical_mmt_services_no_change(self):
        carrier_dict = BuildTLVCarrierDict('CATV_C36', [(0x01, 'Service A')], sdt_services=[(0x01, 'Service A')])
        diff = CompareScanResults({'CATV_C36': carrier_dict}, {'CATV_C36': carrier_dict})
        assert diff.has_changes is False

    def test_mmt_sdt_service_added_removed_and_renamed(self):
        # MH-SDT 由来の放送網内サービス一覧 (service_id 単位) の増減・改名も検出できること
        previous = {
            'CATV_C36': BuildTLVCarrierDict('CATV_C36', [(0x01, 'Service A')], sdt_services=[(0x01, 'Service A'), (0x02, 'Service B')])
        }
        current = {
            'CATV_C36': BuildTLVCarrierDict('CATV_C36', [(0x01, 'Service A')], sdt_services=[(0x01, 'Service A'), (0x03, 'Service C')])
        }

        diff = CompareScanResults(previous, current)

        assert len(diff.changed_channels) == 1
        change = diff.changed_channels[0]
        assert [service.service_id for service in change.added_mmt_sdt_services] == [0x03]
        assert [service.service_id for service in change.removed_mmt_sdt_services] == [0x02]
        assert change.renamed_mmt_sdt_services == []

        text = FormatScanDiff(diff)
        assert '+ MH-SDT service added: service_id=0x0003 (Service C)' in text
        assert '- MH-SDT service removed: service_id=0x0002 (Service B)' in text

    def test_mmt_sdt_service_renamed(self):
        previous = {'CATV_C36': BuildTLVCarrierDict('CATV_C36', [], sdt_services=[(0x01, 'Service A')])}
        current = {'CATV_C36': BuildTLVCarrierDict('CATV_C36', [], sdt_services=[(0x01, 'Service A Renamed')])}

        diff = CompareScanResults(previous, current)

        change = diff.changed_channels[0]
        assert len(change.renamed_mmt_sdt_services) == 1
        assert change.renamed_mmt_sdt_services[0].service_id == 0x01
        assert change.renamed_mmt_sdt_services[0].previous_service_name == 'Service A'
        assert change.renamed_mmt_sdt_services[0].service_name == 'Service A Renamed'
        assert '~ MH-SDT service renamed: service_id=0x0001 "Service A" -> "Service A Renamed"' in FormatScanDiff(diff)


class TestFormatScanDiff:
    def test_no_changes_message(self):
        diff = CompareScanResults({}, {})
        text = FormatScanDiff(diff)
        assert text == 'No changes detected since the previous scan.\n'

    def test_format_contains_expected_markers(self):
        previous = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(
                        0x1001,
                        services=[CATVServiceInfo(service_id=100, service_name='Service A')],
                    )
                ],
            )
        }
        current = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(
                        0x1001,
                        services=[CATVServiceInfo(service_id=100, service_name='Service A Renamed')],
                    )
                ],
            ),
            'CATV_16': BuildCarrierDict('CATV_16', carrier_type=CarrierType.TSMF),
        }

        diff = CompareScanResults(previous, current)
        text = FormatScanDiff(diff)

        assert 'Added Channels:' in text
        assert '  + CATV_16' in text
        assert 'Changed Channels:' in text
        assert '  ~ CATV_15' in text
        assert '"Service A" -> "Service A Renamed"' in text


def BuildCarrierDictWithCNR(physical_channel: str, cnr_db: float | None, with_signal_stats: bool = True) -> dict[str, Any]:
    """テスト用に、指定した CNR (dB) の signal_stats を持つ CATV.json 1エントリ分の dict を組み立てる"""

    return BuildCarrierDict(
        physical_channel,
        signal_stats=CATVSignalStats(cnr_db=cnr_db) if with_signal_stats is True else None,
    )


class TestSignalDegradationWarnings:
    """前回スキャンからの CNR (C/N比) 低下の検出テスト"""

    def test_cnr_drop_over_threshold_is_warned(self):
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 24.5)}

        diff = CompareScanResults(previous, current)

        assert len(diff.signal_warnings) == 1
        warning = diff.signal_warnings[0]
        assert warning.physical_channel == 'CATV_15'
        assert warning.previous_cnr_db == 32.0
        assert warning.current_cnr_db == 24.5
        assert warning.drop_db == 7.5

    def test_cnr_drop_exactly_at_threshold_is_warned(self):
        # ちょうど閾値ぴったりの低下は「警告する」側に含める (>= 判定)
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 30.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 30.0 - SIGNAL_CNR_DROP_WARNING_THRESHOLD_DB)}

        diff = CompareScanResults(previous, current)

        assert len(diff.signal_warnings) == 1
        assert diff.signal_warnings[0].drop_db == SIGNAL_CNR_DROP_WARNING_THRESHOLD_DB

    def test_cnr_drop_just_below_threshold_is_not_warned(self):
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 30.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 25.25)}

        diff = CompareScanResults(previous, current)

        assert diff.signal_warnings == []

    def test_cnr_improvement_is_not_warned(self):
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 20.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0)}

        diff = CompareScanResults(previous, current)

        assert diff.signal_warnings == []

    def test_null_cnr_is_skipped(self):
        # cnr_db が片方でも None なら比較できないためスキップする (signal_stats ごと None の場合も同様)
        previous = {
            'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0),
            'CATV_16': BuildCarrierDictWithCNR('CATV_16', None),
            'CATV_17': BuildCarrierDictWithCNR('CATV_17', 32.0),
        }
        current = {
            'CATV_15': BuildCarrierDictWithCNR('CATV_15', 10.0, with_signal_stats=False),
            'CATV_16': BuildCarrierDictWithCNR('CATV_16', 10.0),
            'CATV_17': BuildCarrierDictWithCNR('CATV_17', None),
        }

        diff = CompareScanResults(previous, current)

        assert diff.signal_warnings == []

    def test_added_or_removed_channel_is_not_warned(self):
        # 前回・今回の双方に存在しない物理チャンネルは比較対象にしない
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0)}
        current = {'CATV_16': BuildCarrierDictWithCNR('CATV_16', 10.0)}

        diff = CompareScanResults(previous, current)

        assert diff.signal_warnings == []

    def test_signal_warning_does_not_count_as_change(self):
        # CNR は測定ごとに揺れるため、信号品質の劣化警告だけでは has_changes を True にしない
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 20.0)}

        diff = CompareScanResults(previous, current)

        assert len(diff.signal_warnings) == 1
        assert diff.has_changes is False

    def test_warning_section_is_placed_at_the_top_of_the_text_report(self):
        previous = {
            'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0),
        }
        current = {
            'CATV_15': BuildCarrierDictWithCNR('CATV_15', 20.0),
            'CATV_16': BuildCarrierDict('CATV_16', carrier_type=CarrierType.TSMF),
        }

        text = FormatScanDiff(CompareScanResults(previous, current))
        lines = text.splitlines()

        assert lines[0] == 'Signal degradation warnings:'
        assert lines[1] == '  ! CATV_15: CNR 32.00 dB -> 20.00 dB (-12.00 dB)'
        # 既存セクションは信号警告セクションの後ろに、空行を1つ挟んで続く
        assert lines[2] == ''
        assert lines[3] == 'Added Channels:'

    def test_warning_is_reported_even_without_changes(self):
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 20.0)}

        text = FormatScanDiff(CompareScanResults(previous, current))

        assert text == (
            'Signal degradation warnings:\n'
            '  ! CATV_15: CNR 32.00 dB -> 20.00 dB (-12.00 dB)\n'
            '\n'
            'No changes detected since the previous scan.\n'
        )

    def test_text_report_is_unchanged_without_warnings(self):
        # 信号警告がない場合、既存の CATV.diff.txt のフォーマットは一切変わらない
        previous: dict[str, Any] = {}
        current = {'CATV_16': BuildCarrierDict('CATV_16', carrier_type=CarrierType.TSMF)}

        text = FormatScanDiff(CompareScanResults(previous, current))

        assert text.startswith('Added Channels:\n')
        assert FormatScanDiff(CompareScanResults({}, {})) == 'No changes detected since the previous scan.\n'


class TestFormatScanDiffJSON:
    """機械可読な差分出力 (CATV.diff.json) のテスト"""

    def test_has_changes_is_true_when_changed(self):
        previous: dict[str, Any] = {}
        current = {'CATV_16': BuildCarrierDict('CATV_16', carrier_type=CarrierType.TSMF)}

        diff_json = json.loads(FormatScanDiffJSON(CompareScanResults(previous, current)))

        assert diff_json['has_changes'] is True
        assert [channel['physical_channel'] for channel in diff_json['added_channels']] == ['CATV_16']
        assert diff_json['removed_channels'] == []
        assert diff_json['changed_channels'] == []
        assert diff_json['signal_warnings'] == []

    def test_has_changes_is_false_when_unchanged(self):
        carrier_dict = BuildCarrierDict('CATV_15')

        diff_json = json.loads(FormatScanDiffJSON(CompareScanResults({'CATV_15': carrier_dict}, {'CATV_15': carrier_dict})))

        assert diff_json['has_changes'] is False
        assert diff_json['added_channels'] == []
        assert diff_json['removed_channels'] == []
        assert diff_json['changed_channels'] == []
        assert diff_json['signal_warnings'] == []

    def test_signal_warnings_are_included_but_has_changes_stays_false(self):
        previous = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 32.0)}
        current = {'CATV_15': BuildCarrierDictWithCNR('CATV_15', 24.5)}

        diff_json = json.loads(FormatScanDiffJSON(CompareScanResults(previous, current)))

        assert diff_json['has_changes'] is False
        assert diff_json['signal_warnings'] == [
            {
                'physical_channel': 'CATV_15',
                'previous_cnr_db': 32.0,
                'current_cnr_db': 24.5,
                'drop_db': 7.5,
            }
        ]

    def test_json_formatting_matches_catv_json_style(self):
        # CATV.json と同じくインデント幅 4・非 ASCII 文字はエスケープせずそのまま出力する
        previous = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(0x1001, services=[CATVServiceInfo(service_id=100, service_name='テスト放送')]),
                ],
            )
        }
        current = {
            'CATV_15': BuildCarrierDict(
                'CATV_15',
                transport_streams=[
                    BuildTransportStream(0x1001, services=[CATVServiceInfo(service_id=100, service_name='テスト放送 2')]),
                ],
            )
        }

        text = FormatScanDiffJSON(CompareScanResults(previous, current))

        assert '\n    "added_channels": [' in text
        assert '"service_name": "テスト放送 2"' in text
        assert '\\u' not in text
        assert text.endswith('}')  # CATV.json に合わせ末尾に改行は付けない
        assert json.loads(text)['has_changes'] is True

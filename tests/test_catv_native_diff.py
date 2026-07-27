import json

from isdb_scanner.catv.native_diff import BuildNativeScanDiffReport
from isdb_scanner.constants import ServiceInfo, TransportStreamInfo, TransportStreamInfoList


def BuildService(
    service_id: int,
    service_name: str = 'Service A',
    is_free: bool = True,
    channel_number: str = '101',
) -> ServiceInfo:
    return ServiceInfo(
        channel_number=channel_number,
        service_id=service_id,
        service_type=0x01,
        service_name=service_name,
        is_free=is_free,
    )


def BuildTransportStream(
    physical_channel: str,
    transport_stream_id: int = 0x4001,
    network_name: str = 'Test Network',
    services: list[ServiceInfo] | None = None,
) -> TransportStreamInfo:
    return TransportStreamInfo(
        physical_channel=physical_channel,
        transport_stream_id=transport_stream_id,
        network_id=4,  # BS
        network_name=network_name,
        satellite_frequency=11.727,
        satellite_transponder=1,
        satellite_slot_number=1,
        services=services or [],
    )


def BuildPreviousScanResult(ts_infos: list[TransportStreamInfo]) -> list[dict]:
    """テスト用に、前回のスキャン結果 (BS.json 等をパースした配列) を TransportStreamInfo の一覧からラウンドトリップで組み立てる"""

    return json.loads(json.dumps(TransportStreamInfoList(root=ts_infos).model_dump(mode='json')))


class TestBuildNativeScanDiffReportNoChanges:
    def test_identical_results_returns_empty_string(self):
        ts_infos = [
            BuildTransportStream(
                'BS01/TS0',
                services=[BuildService(101, 'Service A'), BuildService(102, 'Service B')],
            )
        ]
        previous = BuildPreviousScanResult(ts_infos)

        report = BuildNativeScanDiffReport(previous, ts_infos, 'BS')

        assert report == ''

    def test_empty_results_returns_empty_string(self):
        report = BuildNativeScanDiffReport([], [], 'BS')
        assert report == ''


class TestBuildNativeScanDiffReportChannelAddRemove:
    def test_added_channel(self):
        previous = BuildPreviousScanResult([])
        current = [BuildTransportStream('BS01/TS0', transport_stream_id=0x4010, network_name='NHK BS1')]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert 'BS Scan Diff:' in report
        assert 'Added Channels:' in report
        assert '  + BS01/TS0 (TSID=0x4010, NHK BS1, 0 services)' in report
        assert 'Removed Channels:' not in report
        assert 'Changed Channels:' not in report

    def test_removed_channel(self):
        previous_ts_infos = [BuildTransportStream('BS01/TS0', transport_stream_id=0x4010, network_name='NHK BS1')]
        previous = BuildPreviousScanResult(previous_ts_infos)
        current: list[TransportStreamInfo] = []

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert 'Removed Channels:' in report
        assert '  - BS01/TS0 (TSID=0x4010, NHK BS1, 0 services)' in report
        assert 'Added Channels:' not in report
        assert 'Changed Channels:' not in report


class TestBuildNativeScanDiffReportChannelChanged:
    def test_tsid_changed(self):
        previous = BuildPreviousScanResult([BuildTransportStream('BS01/TS0', transport_stream_id=0x4010)])
        current = [BuildTransportStream('BS01/TS0', transport_stream_id=0x4020)]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert 'Changed Channels:' in report
        assert '  ~ BS01/TS0' in report
        assert '    ~ TSID changed: 0x4010 -> 0x4020' in report

    def test_network_name_changed(self):
        previous = BuildPreviousScanResult([BuildTransportStream('BS01/TS0', network_name='Old Network')])
        current = [BuildTransportStream('BS01/TS0', network_name='New Network')]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert '    ~ Network name changed: "Old Network" -> "New Network"' in report

    def test_service_added_and_removed(self):
        previous = BuildPreviousScanResult(
            [
                BuildTransportStream(
                    'BS01/TS0',
                    services=[BuildService(101, 'Service A'), BuildService(102, 'Service B')],
                )
            ]
        )
        current = [
            BuildTransportStream(
                'BS01/TS0',
                services=[BuildService(101, 'Service A'), BuildService(103, 'Service C')],
            )
        ]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert '    + Service added: service_id=103 (Service C)' in report
        assert '    - Service removed: service_id=102 (Service B)' in report

    def test_service_renamed(self):
        previous = BuildPreviousScanResult(
            [BuildTransportStream('BS01/TS0', services=[BuildService(101, 'Service A')])]
        )
        current = [BuildTransportStream('BS01/TS0', services=[BuildService(101, 'Service A Renamed')])]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert '    ~ Service renamed: service_id=101 "Service A" -> "Service A Renamed"' in report

    def test_service_is_free_changed(self):
        previous = BuildPreviousScanResult(
            [BuildTransportStream('BS01/TS0', services=[BuildService(101, 'Service A', is_free=True)])]
        )
        current = [BuildTransportStream('BS01/TS0', services=[BuildService(101, 'Service A', is_free=False)])]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert '    ~ Service is_free changed: service_id=101 (Service A) True -> False' in report

    def test_multiple_changes_combined_in_single_report(self):
        previous = BuildPreviousScanResult(
            [
                BuildTransportStream(
                    'BS01/TS0',
                    transport_stream_id=0x4010,
                    services=[BuildService(101, 'Service A'), BuildService(102, 'Service B')],
                ),
                BuildTransportStream('BS02/TS0', transport_stream_id=0x4020, network_name='Removed Net'),
            ]
        )
        current = [
            BuildTransportStream(
                'BS01/TS0',
                transport_stream_id=0x4011,
                services=[BuildService(101, 'Service A Renamed'), BuildService(103, 'Service C')],
            ),
            BuildTransportStream('BS03/TS0', transport_stream_id=0x4030, network_name='Added Net'),
        ]

        report = BuildNativeScanDiffReport(previous, current, 'BS')

        assert 'Added Channels:' in report
        assert '  + BS03/TS0 (TSID=0x4030, Added Net, 0 services)' in report
        assert 'Removed Channels:' in report
        assert '  - BS02/TS0 (TSID=0x4020, Removed Net, 0 services)' in report
        assert 'Changed Channels:' in report
        assert '  ~ BS01/TS0' in report
        assert '    ~ TSID changed: 0x4010 -> 0x4011' in report
        assert '    + Service added: service_id=103 (Service C)' in report
        assert '    - Service removed: service_id=102 (Service B)' in report
        assert '    ~ Service renamed: service_id=101 "Service A" -> "Service A Renamed"' in report

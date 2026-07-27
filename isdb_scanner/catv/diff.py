from __future__ import annotations

from typing import Any

from isdb_scanner.catv.constants import (
    CASChangeInfo,
    CATVCarrierInfo,
    CATVTransportStreamInfo,
    ChannelChangeInfo,
    ChannelSummaryInfo,
    MMTSDTServiceDiffInfo,
    MMTServiceDiffInfo,
    RetransmissionSourceChangeInfo,
    ScanDiff,
    ServiceDiffInfo,
    TransportStreamDiffInfo,
)


def CompareScanResults(previous: dict[str, Any], current: dict[str, Any]) -> ScanDiff:
    """
    前回・今回の CATV.json (json.loads() でパース済みの dict。物理チャンネル名 → CATVCarrierInfo 相当の dict) を比較し、
    差分レポート (ScanDiff) を生成する

    CATV.json には選局 (ロック) に成功したチャンネルのみが含まれる (dvbv5-zap でロックできなかったチャンネルは
    scan.py 側で最初からスキップされ出力されない) ため、キーの増減はそのまま「新たに受信できるようになった/
    受信できなくなった物理チャンネル」を意味する

    Args:
        previous (dict[str, Any]): 前回のスキャン結果 (CATV.json をパースした dict)
        current (dict[str, Any]): 今回のスキャン結果 (CATV.json をパースした dict)

    Returns:
        ScanDiff: 差分レポート
    """

    previous_carriers = {channel: CATVCarrierInfo.model_validate(info) for channel, info in previous.items()}
    current_carriers = {channel: CATVCarrierInfo.model_validate(info) for channel, info in current.items()}

    added_channels = [
        _BuildChannelSummary(current_carriers[channel])
        for channel in sorted(set(current_carriers) - set(previous_carriers))
    ]
    removed_channels = [
        _BuildChannelSummary(previous_carriers[channel])
        for channel in sorted(set(previous_carriers) - set(current_carriers))
    ]

    changed_channels: list[ChannelChangeInfo] = []
    for channel in sorted(set(previous_carriers) & set(current_carriers)):
        change = _CompareChannel(previous_carriers[channel], current_carriers[channel])
        if change is not None:
            changed_channels.append(change)

    return ScanDiff(
        added_channels=added_channels,
        removed_channels=removed_channels,
        changed_channels=changed_channels,
    )


def _BuildChannelSummary(carrier: CATVCarrierInfo) -> ChannelSummaryInfo:
    """CATVCarrierInfo から、丸ごと追加/削除された物理チャンネルのレポート用要約情報を組み立てる"""

    if carrier.mmt is not None:
        # TLV (4K/8K MMT) キャリアは TS を持たないため、MMT サービス数をサービス数として扱う
        service_count = len(carrier.mmt.services)
    else:
        service_count = sum(len(ts_info.services) for ts_info in carrier.transport_streams)

    return ChannelSummaryInfo(
        physical_channel=carrier.physical_channel,
        carrier_type=carrier.carrier_type,
        transport_stream_count=len(carrier.transport_streams),
        service_count=service_count,
    )


def _CompareChannel(previous: CATVCarrierInfo, current: CATVCarrierInfo) -> ChannelChangeInfo | None:
    """
    前回・今回とも存在する同一物理チャンネルの CATVCarrierInfo を比較し、変化があれば ChannelChangeInfo を、
    変化がなければ None を返す
    """

    change = ChannelChangeInfo(physical_channel=current.physical_channel)
    has_change = False

    if previous.carrier_type != current.carrier_type:
        change.previous_carrier_type = previous.carrier_type
        change.current_carrier_type = current.carrier_type
        has_change = True

    previous_ts_by_id = {ts_info.transport_stream_id: ts_info for ts_info in previous.transport_streams}
    current_ts_by_id = {ts_info.transport_stream_id: ts_info for ts_info in current.transport_streams}

    for tsid in sorted(set(current_ts_by_id) - set(previous_ts_by_id)):
        change.added_transport_streams.append(_BuildTransportStreamDiff(current_ts_by_id[tsid]))
        has_change = True
    for tsid in sorted(set(previous_ts_by_id) - set(current_ts_by_id)):
        change.removed_transport_streams.append(_BuildTransportStreamDiff(previous_ts_by_id[tsid]))
        has_change = True

    # 前回・今回とも存在する TS については、内包するサービス・CAS 種別・再送信元の変化を見る
    for tsid in sorted(set(previous_ts_by_id) & set(current_ts_by_id)):
        previous_ts = previous_ts_by_id[tsid]
        current_ts = current_ts_by_id[tsid]

        previous_services_by_id = {service.service_id: service for service in previous_ts.services}
        current_services_by_id = {service.service_id: service for service in current_ts.services}

        for service_id in sorted(set(current_services_by_id) - set(previous_services_by_id)):
            service = current_services_by_id[service_id]
            change.added_services.append(
                ServiceDiffInfo(transport_stream_id=tsid, service_id=service_id, service_name=service.service_name)
            )
            has_change = True
        for service_id in sorted(set(previous_services_by_id) - set(current_services_by_id)):
            service = previous_services_by_id[service_id]
            change.removed_services.append(
                ServiceDiffInfo(transport_stream_id=tsid, service_id=service_id, service_name=service.service_name)
            )
            has_change = True
        for service_id in sorted(set(previous_services_by_id) & set(current_services_by_id)):
            previous_service = previous_services_by_id[service_id]
            current_service = current_services_by_id[service_id]
            if previous_service.service_name != current_service.service_name:
                change.renamed_services.append(
                    ServiceDiffInfo(
                        transport_stream_id=tsid,
                        service_id=service_id,
                        service_name=current_service.service_name,
                        previous_service_name=previous_service.service_name,
                    )
                )
                has_change = True

        if previous_ts.cas.required_card != current_ts.cas.required_card:
            change.cas_changes.append(
                CASChangeInfo(
                    transport_stream_id=tsid,
                    previous_required_card=previous_ts.cas.required_card,
                    current_required_card=current_ts.cas.required_card,
                )
            )
            has_change = True

        if previous_ts.retransmission_source != current_ts.retransmission_source:
            change.retransmission_source_changes.append(
                RetransmissionSourceChangeInfo(
                    transport_stream_id=tsid,
                    previous_source=previous_ts.retransmission_source,
                    current_source=current_ts.retransmission_source,
                )
            )
            has_change = True

    # TLV (4K/8K MMT) キャリアは transport_streams が空のため、上記の TS 単位の比較では変化を検出できない
    # MMT サービス (MPT の package_id 単位) の増減・改名をここで比較する
    previous_mmt_services = {service.package_id: service for service in (previous.mmt.services if previous.mmt is not None else [])}
    current_mmt_services = {service.package_id: service for service in (current.mmt.services if current.mmt is not None else [])}

    for package_id in sorted(set(current_mmt_services) - set(previous_mmt_services)):
        service = current_mmt_services[package_id]
        change.added_mmt_services.append(MMTServiceDiffInfo(package_id=package_id, service_name=service.service_name))
        has_change = True
    for package_id in sorted(set(previous_mmt_services) - set(current_mmt_services)):
        service = previous_mmt_services[package_id]
        change.removed_mmt_services.append(MMTServiceDiffInfo(package_id=package_id, service_name=service.service_name))
        has_change = True
    for package_id in sorted(set(previous_mmt_services) & set(current_mmt_services)):
        previous_service = previous_mmt_services[package_id]
        current_service = current_mmt_services[package_id]
        if previous_service.service_name != current_service.service_name:
            change.renamed_mmt_services.append(
                MMTServiceDiffInfo(
                    package_id=package_id,
                    service_name=current_service.service_name,
                    previous_service_name=previous_service.service_name,
                )
            )
            has_change = True

    # MH-SDT 由来の放送網内サービス一覧 (service_id 単位) の増減・改名を比較する
    # 他ストリームのサービスは 1 テーブルずつ順次送出されるため、収録タイミング次第で取得できる範囲が変わる点には注意
    previous_sdt_services = {service.service_id: service for service in (previous.mmt.sdt_services if previous.mmt is not None else [])}
    current_sdt_services = {service.service_id: service for service in (current.mmt.sdt_services if current.mmt is not None else [])}

    for service_id in sorted(set(current_sdt_services) - set(previous_sdt_services)):
        service = current_sdt_services[service_id]
        change.added_mmt_sdt_services.append(MMTSDTServiceDiffInfo(service_id=service_id, service_name=service.service_name))
        has_change = True
    for service_id in sorted(set(previous_sdt_services) - set(current_sdt_services)):
        service = previous_sdt_services[service_id]
        change.removed_mmt_sdt_services.append(MMTSDTServiceDiffInfo(service_id=service_id, service_name=service.service_name))
        has_change = True
    for service_id in sorted(set(previous_sdt_services) & set(current_sdt_services)):
        previous_service = previous_sdt_services[service_id]
        current_service = current_sdt_services[service_id]
        if previous_service.service_name != current_service.service_name:
            change.renamed_mmt_sdt_services.append(
                MMTSDTServiceDiffInfo(
                    service_id=service_id,
                    service_name=current_service.service_name,
                    previous_service_name=previous_service.service_name,
                )
            )
            has_change = True

    return change if has_change else None


def _BuildTransportStreamDiff(ts_info: CATVTransportStreamInfo) -> TransportStreamDiffInfo:
    """CATVTransportStreamInfo から、追加/削除された TS のレポート用情報を組み立てる"""

    return TransportStreamDiffInfo(
        transport_stream_id=ts_info.transport_stream_id,
        tsmf_relative_ts_number=ts_info.tsmf_relative_ts_number,
        network_name=ts_info.network_name,
        service_count=len(ts_info.services),
    )


def FormatScanDiff(diff: ScanDiff) -> str:
    """
    ScanDiff を、人間が読めるプレーンテキストのレポートに整形する (rich マークアップは使わない)
    追加は行頭 "+"、削除は行頭 "-"、変化は行頭 "~" で示す

    Args:
        diff (ScanDiff): 差分レポート

    Returns:
        str: 整形されたレポート文字列 (末尾に改行を1つ含む)
    """

    if diff.has_changes is False:
        return 'No changes detected since the previous scan.\n'

    lines: list[str] = []

    if len(diff.added_channels) > 0:
        lines.append('Added Channels:')
        for channel in diff.added_channels:
            lines.append(
                f'  + {channel.physical_channel} '
                f'({channel.carrier_type.value}, {channel.transport_stream_count} TS, {channel.service_count} services)'
            )

    if len(diff.removed_channels) > 0:
        if len(lines) > 0:
            lines.append('')
        lines.append('Removed Channels:')
        for channel in diff.removed_channels:
            lines.append(
                f'  - {channel.physical_channel} '
                f'({channel.carrier_type.value}, {channel.transport_stream_count} TS, {channel.service_count} services)'
            )

    if len(diff.changed_channels) > 0:
        if len(lines) > 0:
            lines.append('')
        lines.append('Changed Channels:')
        for change in diff.changed_channels:
            lines.append(f'  ~ {change.physical_channel}')

            if change.previous_carrier_type is not None and change.current_carrier_type is not None:
                lines.append(f'    ~ Carrier type: {change.previous_carrier_type.value} -> {change.current_carrier_type.value}')

            for ts_diff in change.added_transport_streams:
                lines.append(
                    f'    + TS added: TSID={ts_diff.transport_stream_id:#06x} '
                    f'({ts_diff.network_name}, {ts_diff.service_count} services)'
                )
            for ts_diff in change.removed_transport_streams:
                lines.append(
                    f'    - TS removed: TSID={ts_diff.transport_stream_id:#06x} '
                    f'({ts_diff.network_name}, {ts_diff.service_count} services)'
                )

            for service_diff in change.added_services:
                lines.append(
                    f'    + Service added: TSID={service_diff.transport_stream_id:#06x} '
                    f'service_id={service_diff.service_id} ({service_diff.service_name})'
                )
            for service_diff in change.removed_services:
                lines.append(
                    f'    - Service removed: TSID={service_diff.transport_stream_id:#06x} '
                    f'service_id={service_diff.service_id} ({service_diff.service_name})'
                )
            for service_diff in change.renamed_services:
                lines.append(
                    f'    ~ Service renamed: TSID={service_diff.transport_stream_id:#06x} '
                    f'service_id={service_diff.service_id} '
                    f'"{service_diff.previous_service_name}" -> "{service_diff.service_name}"'
                )

            for mmt_diff in change.added_mmt_services:
                lines.append(f'    + MMT service added: package_id={mmt_diff.package_id:#06x} ({mmt_diff.service_name})')
            for mmt_diff in change.removed_mmt_services:
                lines.append(f'    - MMT service removed: package_id={mmt_diff.package_id:#06x} ({mmt_diff.service_name})')
            for mmt_diff in change.renamed_mmt_services:
                lines.append(
                    f'    ~ MMT service renamed: package_id={mmt_diff.package_id:#06x} '
                    f'"{mmt_diff.previous_service_name}" -> "{mmt_diff.service_name}"'
                )

            for sdt_diff in change.added_mmt_sdt_services:
                lines.append(f'    + MH-SDT service added: service_id={sdt_diff.service_id:#06x} ({sdt_diff.service_name})')
            for sdt_diff in change.removed_mmt_sdt_services:
                lines.append(f'    - MH-SDT service removed: service_id={sdt_diff.service_id:#06x} ({sdt_diff.service_name})')
            for sdt_diff in change.renamed_mmt_sdt_services:
                lines.append(
                    f'    ~ MH-SDT service renamed: service_id={sdt_diff.service_id:#06x} '
                    f'"{sdt_diff.previous_service_name}" -> "{sdt_diff.service_name}"'
                )

            for cas_change in change.cas_changes:
                lines.append(
                    f'    ~ CAS changed: TSID={cas_change.transport_stream_id:#06x} '
                    f'{cas_change.previous_required_card} -> {cas_change.current_required_card}'
                )
            for source_change in change.retransmission_source_changes:
                lines.append(
                    f'    ~ Retransmission source changed: TSID={source_change.transport_stream_id:#06x} '
                    f'{source_change.previous_source} -> {source_change.current_source}'
                )

    return '\n'.join(lines) + '\n'

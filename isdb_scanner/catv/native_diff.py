from __future__ import annotations

from typing import Any

from isdb_scanner.constants import TransportStreamInfo


def BuildNativeScanDiffReport(
    previous_scan_result: list[dict[str, Any]],
    current_ts_infos: list[TransportStreamInfo],
    band_label: str,
) -> str:
    """
    前回・今回のネイティブスキャン (衛星 BS/CS・地上波) 結果を比較し、差分レポートのプレーンテキストを返す
    (rich マークアップは使わない。追加は行頭 "+"、削除は行頭 "-"、変化は行頭 "~" で示す)

    前回のスキャン結果は json.loads() でパース済みの配列 (BS.json / CS.json / Terrestrial.json の内容、
    すなわち TransportStreamInfoList.model_dump(mode='json') 相当) を、今回のスキャン結果は
    TransportStreamInfo のインスタンス一覧をそのまま渡す

    これらの JSON には選局 (ロック) に成功したチャンネルのみが含まれるため、物理チャンネルのキーの増減は
    そのまま「新たに受信できるようになった/受信できなくなった物理チャンネル」を意味する
    (物理チャンネルは BS/CS では TS 単位 (ex: "BS23/TS3") で採番されているため、CATV.diff.txt と異なり
    「TS の追加/削除」を別途検出する必要はなく、物理チャンネル単位の追加/削除がそのまま TS の追加/削除になる。
    ただし transport_stream_id 自体は BS 帯域再編等で同一物理チャンネルのまま変化しうるため、
    前回・今回とも存在する物理チャンネルについては transport_stream_id の変化も別途検出する)

    前回 JSON が古い形式・壊れている場合は、pydantic の ValidationError などの例外をそのまま送出する
    (isdb_scanner.catv.diff.CompareScanResults と同様、呼び出し側で try/except により保護する契約とする)

    Args:
        previous_scan_result (list[dict[str, Any]]): 前回のスキャン結果 (BS.json 等をパースした配列)
        current_ts_infos (list[TransportStreamInfo]): 今回のスキャン結果
        band_label (str): レポート見出しに使う放送帯域名 (ex: "BS", "CS", "Terrestrial")

    Returns:
        str: 整形されたレポート文字列 (末尾に改行を1つ含む)。差分が1件もない場合は空文字列
    """

    previous_ts_infos = [TransportStreamInfo.model_validate(entry) for entry in previous_scan_result]

    previous_by_channel = {ts_info.physical_channel: ts_info for ts_info in previous_ts_infos}
    current_by_channel = {ts_info.physical_channel: ts_info for ts_info in current_ts_infos}

    added_channels = sorted(set(current_by_channel) - set(previous_by_channel))
    removed_channels = sorted(set(previous_by_channel) - set(current_by_channel))
    common_channels = sorted(set(previous_by_channel) & set(current_by_channel))

    changed_channel_lines: list[str] = []
    for physical_channel in common_channels:
        channel_lines = _CompareTransportStream(previous_by_channel[physical_channel], current_by_channel[physical_channel])
        if len(channel_lines) > 0:
            changed_channel_lines.append(f'  ~ {physical_channel}')
            changed_channel_lines.extend(channel_lines)

    if len(added_channels) == 0 and len(removed_channels) == 0 and len(changed_channel_lines) == 0:
        return ''

    lines: list[str] = [f'{band_label} Scan Diff:']

    if len(added_channels) > 0:
        lines.append('')
        lines.append('Added Channels:')
        for physical_channel in added_channels:
            lines.append(f'  + {_FormatChannelSummary(current_by_channel[physical_channel])}')

    if len(removed_channels) > 0:
        lines.append('')
        lines.append('Removed Channels:')
        for physical_channel in removed_channels:
            lines.append(f'  - {_FormatChannelSummary(previous_by_channel[physical_channel])}')

    if len(changed_channel_lines) > 0:
        lines.append('')
        lines.append('Changed Channels:')
        lines.extend(changed_channel_lines)

    return '\n'.join(lines) + '\n'


def _FormatChannelSummary(ts_info: TransportStreamInfo) -> str:
    """丸ごと追加/削除された物理チャンネル1件分の要約 (行頭のプレフィックスを除く部分) を組み立てる"""

    return (
        f'{ts_info.physical_channel} (TSID={ts_info.transport_stream_id:#06x}, '
        f'{ts_info.network_name}, {len(ts_info.services)} services)'
    )


def _CompareTransportStream(previous: TransportStreamInfo, current: TransportStreamInfo) -> list[str]:
    """
    前回・今回とも存在する同一物理チャンネルの TransportStreamInfo を比較し、変化があればレポート行 (インデント済み) の
    リストを返す (変化がなければ空リストを返す)
    """

    lines: list[str] = []

    if previous.transport_stream_id != current.transport_stream_id:
        lines.append(f'    ~ TSID changed: {previous.transport_stream_id:#06x} -> {current.transport_stream_id:#06x}')

    if previous.network_name != current.network_name:
        lines.append(f'    ~ Network name changed: "{previous.network_name}" -> "{current.network_name}"')

    previous_services = {service.service_id: service for service in previous.services}
    current_services = {service.service_id: service for service in current.services}

    for service_id in sorted(set(current_services) - set(previous_services)):
        service = current_services[service_id]
        lines.append(f'    + Service added: service_id={service_id} ({service.service_name})')
    for service_id in sorted(set(previous_services) - set(current_services)):
        service = previous_services[service_id]
        lines.append(f'    - Service removed: service_id={service_id} ({service.service_name})')

    # 前回・今回とも存在するサービスについては、サービス名・is_free (無料/有料放送) の変化を見る
    for service_id in sorted(set(previous_services) & set(current_services)):
        previous_service = previous_services[service_id]
        current_service = current_services[service_id]

        if previous_service.service_name != current_service.service_name:
            lines.append(
                f'    ~ Service renamed: service_id={service_id} '
                f'"{previous_service.service_name}" -> "{current_service.service_name}"'
            )

        if previous_service.is_free != current_service.is_free:
            lines.append(
                f'    ~ Service is_free changed: service_id={service_id} ({current_service.service_name}) '
                f'{previous_service.is_free} -> {current_service.is_free}'
            )

    return lines

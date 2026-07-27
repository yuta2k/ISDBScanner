#!/usr/bin/env python3

# isdb-catv-scanner から利用する、ネイティブ地上波 (ISDB-T) チャンネルスキャン処理
#
# isdb_scanner/__main__.py の地上波スキャン部 (スキャン対象決定: 96 行付近 / スキャンループ: 120-241 行付近) の複製。
# upstream (tsukumijima/ISDBScanner) との diff を最小化するため、__main__.py を改変して共通化するのではなく
# catv パッケージ配下に複製している (upstream 追従時はこのファイルと複製元の差分に注意すること)
#
# 衛星版 (satellite.py) と異なり、地上波では「選局失敗 (TunerTuningError) / 受信データ取得失敗 (TunerOutputError) =
# その地域ではそのチャンネルは受信不可」とみなし、次のチューナーへフェイルオーバーせずチャンネルごとスキップする。
# 一方でチューナーオープン失敗 (TunerOpeningError) と解析失敗 (TransportStreamAnalyzeError) は次のチューナーへフェイルオーバーする。
# この挙動差を再現するため、tuner ループ全体を囲む外側 try (選局/受信失敗を捕捉) と、tune/analyze を囲む内側 try
# (オープン/解析失敗を捕捉) の二重構造をそのまま複製している。

import time

from rich import print
from rich.rule import Rule
from rich.style import Style

from isdb_scanner.analyzer import TransportStreamAnalyzeError, TransportStreamAnalyzer
from isdb_scanner.constants import TransportStreamInfo
from isdb_scanner.tuner import ISDBTuner, TunerOpeningError, TunerOutputError, TunerTuningError


def GetTerrestrialScanChannels() -> list[TransportStreamInfo]:
    """
    地上波 (ISDB-T) のスキャン対象物理チャンネル (T13〜T62) のリストを取得する

    地上波は衛星放送と異なり NIT から全チャンネルを一括取得できないため、13ch - 62ch を全てフルスキャンする。
    地上波のうち 53ch - 62ch はすでに廃止されているが、依然一部ケーブルテレビのコミュニティチャンネル (自主放送) で
    利用されているため、スキャン対象に含めている。

    Returns:
        list[TransportStreamInfo]: スキャン対象の物理チャンネルのリスト
    """

    return [TransportStreamInfo(physical_channel=f'T{i}') for i in range(13, 63)]


def ScanTerrestrialChannels(isdbt_tuners: list[ISDBTuner]) -> list[TransportStreamInfo]:
    """
    ネイティブ地上波 (ISDB-T) のチャンネルスキャンを実行する
    チャンネルごとに isdbt_tuners を順に試行するが、フェイルオーバーの条件が衛星版とは異なる:
    - TunerOpeningError (チューナーオープン失敗) / TransportStreamAnalyzeError (解析失敗) → 次のチューナーへフェイルオーバー
    - TunerTuningError (選局失敗) / TunerOutputError (受信データ取得失敗) → その地域では受信不可とみなしチャンネルごとスキップ
      (この場合は残りのチューナーを試さず、そのチャンネルは結果に含めない)

    Args:
        isdbt_tuners (list[ISDBTuner]): スキャンに使う ISDB-T チューナーのリスト

    Returns:
        list[TransportStreamInfo]: 地上波の TS 情報リスト (物理チャンネル順にソート済み)
    """

    scan_terrestrial_physical_channels = GetTerrestrialScanChannels()

    # 地上波のチャンネルスキャンを実行 (13ch - 62ch)
    tr_ts_infos: list[TransportStreamInfo] = []
    for channel in scan_terrestrial_physical_channels:
        try:
            for tuner in isdbt_tuners:
                # 前回チューナーオープンに失敗したチューナーはスキップ
                if tuner.last_tuner_opening_failed is True:
                    continue
                # チューナーの起動と TS 解析を実行
                print(Rule(characters='-', style=Style(color='#E33157')))
                print(f'  Channel: [bright_blue]Terrestrial - {channel.physical_channel.replace("T", "")}ch[/bright_blue]')
                print(f'    Tuner: [green]{tuner.name}[/green] ({tuner.device_path})')
                try:
                    # 録画時間: 2.25 秒 (地上波の SI 送出間隔は最大 2 秒周期)
                    start_time = time.time()
                    try:
                        ts_stream_data = tuner.tune(channel.physical_channel_recisdb, recording_time=2.25)
                    finally:
                        print(f'Tune Time: {time.time() - start_time:.2f} seconds')
                    # トランスポートストリームとサービスの情報を解析
                    ts_infos = TransportStreamAnalyzer(ts_stream_data, channel.physical_channel).analyze()
                    tr_ts_infos.extend(ts_infos)
                    for ts_info in ts_infos:
                        print(f'[green]Transport Stream[/green]: {ts_info}')
                        for service_info in ts_info.services:
                            print(f'[green]         Service[/green]: {service_info}')
                    break
                except TunerOpeningError as ex:
                    print(f'[red]Failed to open tuner. {ex}[/red]')
                    print('[red]Trying again with the next tuner...[/red]')
                    continue
                except TransportStreamAnalyzeError as ex:
                    print(f'[red]Failed to analyze transport stream. {ex}[/red]')
                    print('[red]Trying again with the next tuner...[/red]')
                    continue
        except TunerTuningError as ex:
            print(f'[yellow]{ex}[/yellow]')
            print('[yellow]Channel may not be received in your area. Skipping...[/yellow]')
            continue
        except TunerOutputError:
            print('[yellow]Failed to receive data.[/yellow]')
            print('[yellow]Channel may not be received in your area. Skipping...[/yellow]')
            continue

    # 地上波で同一チャンネルが重複して検出された場合の処理
    ## 居住地域によっては、複数の中継所の電波が受信できるなどの理由で、同一チャンネルが複数の物理チャンネルで受信できる場合がある
    ## 同一チャンネルが複数の物理チャンネルから受信できると誤動作の要因になるため、TSID が一致する物理チャンネルを集計し、
    ## 次にどの物理チャンネルが一番信号レベルが高いかを判定して、その物理チャンネルのみを残す
    ## (地上波の TSID は放送局ごとに全国で一意であるため、TSID が一致する物理チャンネルは同一チャンネルであることが保証される)

    # 同一 TSID を持つ物理チャンネルをグループ化
    tsid_grouped_physical_channels: dict[int, list[TransportStreamInfo]] = {}
    for ts_info in tr_ts_infos:
        if ts_info.transport_stream_id not in tsid_grouped_physical_channels:
            tsid_grouped_physical_channels[ts_info.transport_stream_id] = []
        tsid_grouped_physical_channels[ts_info.transport_stream_id].append(ts_info)

    # 同一 TSID を持つ物理チャンネルのうち、信号レベルが最も高い物理チャンネルのみを残す
    for ts_infos in tsid_grouped_physical_channels.values():
        # 同一 TSID を持つ物理チャンネルが1つだけ (正常) の場合は何もしない
        if len(ts_infos) == 1:
            continue

        print(Rule(characters='-', style=Style(color='#E33157')))
        print(
            f'[yellow]{ts_infos[0].network_name} (TSID: {ts_infos[0].transport_stream_id}) '
            'was detected redundantly across multiple physical channels.[/yellow]'
        )
        print('[yellow]Outputs only the physical channel with the highest signal level...[/yellow]')

        # それぞれの物理チャンネルの信号レベルを計測
        signal_levels: dict[str, float] = {}
        for ts_info in ts_infos:
            signal_levels[ts_info.physical_channel] = -99.99  # デフォルト値 (信号レベルを計測できなかった場合用)
            for tuner in isdbt_tuners:
                # 前回チューナーオープンに失敗したチューナーはスキップ
                if tuner.last_tuner_opening_failed is True:
                    continue
                # チューナーの起動と平均信号レベル取得を実行
                ## チューナーの起動失敗などで平均信号レベルが取得できなかった場合は None が返されるので、次のチューナーで試す
                result = tuner.getSignalLevelMean(ts_info.physical_channel)
                if result is None:
                    continue
                signal_levels[ts_info.physical_channel] = result
                print(f'Physical Channel: {ts_info.physical_channel.replace("T", "")}ch | Signal Level: {result:.2f} dB')
                break  # 信号レベルが取得できたら次の物理チャンネルへ
            if signal_levels[ts_info.physical_channel] == -99.99:
                print(f'Physical Channel: {ts_info.physical_channel.replace("T", "")}ch | Signal Level: Failed to get signal level')

        # 信号レベルが最も高い物理チャンネル以外の物理チャンネルを tr_ts_infos から削除
        max_signal_level = max(signal_levels.values())
        for physical_channel, signal_level in signal_levels.items():
            ts_info = next(ts_info for ts_info in ts_infos if ts_info.physical_channel == physical_channel)
            if signal_level != max_signal_level:
                tr_ts_infos.remove(ts_info)
            else:
                print(
                    f'[green]Selected Physical Channel: {ts_info.physical_channel.replace("T", "")}ch | '
                    f'Signal Level: {signal_level:.2f} dB[/green]'
                )

    # 物理チャンネル順にソート
    tr_ts_infos = sorted(tr_ts_infos, key=lambda x: x.physical_channel)

    return tr_ts_infos

#!/usr/bin/env python3

# isdb-catv-scanner から利用する、ネイティブ BS/CS110 (ISDB-S) チャンネルスキャン処理
#
# isdb_scanner/__main__.py の衛星スキャン部 (スキャン対象決定: 96-104 行付近 / スキャンループ: 263-311 行付近) の複製。
# upstream (tsukumijima/ISDBScanner) との diff を最小化するため、__main__.py を改変して共通化するのではなく
# catv パッケージ配下に複製している (upstream 追従時はこのファイルと複製元の差分に注意すること)

import time

from rich import print
from rich.rule import Rule
from rich.style import Style

from isdb_scanner.analyzer import TransportStreamAnalyzeError, TransportStreamAnalyzer
from isdb_scanner.constants import TransportStreamInfo
from isdb_scanner.tuner import ISDBTuner, TunerOpeningError, TunerOutputError, TunerTuningError


def GetSatelliteScanChannels(exclude_pay_tv: bool = False) -> list[TransportStreamInfo]:
    """
    BS・CS110 のスキャン対象物理チャンネルのリストを取得する

    衛星放送では NIT から同一ネットワーク内の全チャンネルの情報を一括で取得できるため、
    各ネットワークの最初の物理チャンネルのみをスキャンすればよい (BS + CS1 + CS2 の最大 3 回)
    BS のデフォルト TS は運用規定で 0x40F1 (NHKBS1: BS15/TS0) だが、手元環境ではなぜか他 TS と比べ NIT の送出間隔が
    不安定 (?) で 20 秒程度録画しないと NIT を確実に取得できないため、ここでは BS01/TS0 (BS朝日) をスキャンする

    Args:
        exclude_pay_tv (bool): True の場合、CS1/CS2 をスキャン対象から除外する

    Returns:
        list[TransportStreamInfo]: スキャン対象の物理チャンネルのリスト
    """

    scan_satellite_physical_channels = [
        TransportStreamInfo(physical_channel='BS01/TS0'),
    ]
    if exclude_pay_tv is False:  # 有料放送を除外しない場合は CS1/CS2 もスキャン
        scan_satellite_physical_channels += [
            TransportStreamInfo(physical_channel='ND02'),
            TransportStreamInfo(physical_channel='ND04'),
        ]
    return scan_satellite_physical_channels


def ScanSatelliteChannels(
    isdbs_tuners: list[ISDBTuner],
    exclude_pay_tv: bool = False,
) -> tuple[list[TransportStreamInfo], list[TransportStreamInfo]]:
    """
    ネイティブ BS・CS110 のチャンネルスキャンを実行する
    チャンネルごとに isdbs_tuners を順に試行し、選局や解析に失敗した場合は次のチューナーにフェイルオーバーする
    (すべてのチューナーで失敗したチャンネルは結果に含まれず、警告表示のみでスキャンは継続する)

    Args:
        isdbs_tuners (list[ISDBTuner]): スキャンに使う ISDB-S チューナーのリスト
        exclude_pay_tv (bool): True の場合、CS1/CS2 をスキャンしない

    Returns:
        tuple[list[TransportStreamInfo], list[TransportStreamInfo]]: (BS の TS 情報リスト, CS の TS 情報リスト)
            (いずれも物理チャンネル順にソート済み)
    """

    scan_satellite_physical_channels = GetSatelliteScanChannels(exclude_pay_tv)

    # BS・CS1・CS2 のチャンネルスキャンを実行
    bs_ts_infos: list[TransportStreamInfo] = []
    cs_ts_infos: list[TransportStreamInfo] = []
    for channel in scan_satellite_physical_channels:
        for tuner in isdbs_tuners:
            # 前回チューナーオープンに失敗したチューナーはスキップ
            if tuner.last_tuner_opening_failed is True:
                continue
            # チューナーの起動と TS 解析を実行
            print(Rule(characters='-', style=Style(color='#E33157')))
            print(f'  Channel: [bright_blue]{channel.broadcast_type} (All channels)[/bright_blue]')
            print(f'    Tuner: [green]{tuner.name}[/green] ({tuner.device_path})')
            try:
                # 録画時間: 11 秒 (BS・CS110 の SI 送出間隔は最大 10 秒周期)
                start_time = time.time()
                try:
                    ts_stream_data = tuner.tune(channel.physical_channel_recisdb, recording_time=11)
                finally:
                    print(f'Tune Time: {time.time() - start_time:.2f} seconds')
                # トランスポートストリームとサービスの情報を解析
                ts_infos = TransportStreamAnalyzer(ts_stream_data, channel.physical_channel).analyze()
                if channel.broadcast_type == 'BS':
                    bs_ts_infos.extend(ts_infos)
                elif channel.broadcast_type == 'CS1' or channel.broadcast_type == 'CS2':
                    cs_ts_infos.extend(ts_infos)
                for ts_info in ts_infos:
                    print(f'[green]Transport Stream[/green]: {ts_info}')
                    for service_info in ts_info.services:
                        print(f'[green]         Service[/green]: {service_info}')
                break
            except TunerOpeningError as ex:
                print(f'[red]Failed to open tuner. {ex}[/red]')
                print('[red]Trying again with the next tuner...[/red]')
                continue
            except TunerTuningError as ex:
                print(f'[red]{ex}[/red]')
                print('[red]Trying again with the next tuner...[/red]')
                continue
            except TunerOutputError:
                print('[red]Failed to receive data.[/red]')
                print('[red]Trying again with the next tuner...[/red]')
                continue
            except TransportStreamAnalyzeError as ex:
                print(f'[red]Failed to analyze transport stream. {ex}[/red]')
                print('[red]Trying again with the next tuner...[/red]')
                continue

    # 物理チャンネル順にソート
    bs_ts_infos = sorted(bs_ts_infos, key=lambda x: x.physical_channel)
    cs_ts_infos = sorted(cs_ts_infos, key=lambda x: x.physical_channel)

    return bs_ts_infos, cs_ts_infos

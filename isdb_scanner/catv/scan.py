#!/usr/bin/env python3

# isdb-catv-scanner: 日本の CATV トランスモジュレーション (J.83 Annex C 64QAM) の物理チャンネルをスキャンし、
# 各キャリアの解析結果 (CATV.json) と、受信できたチャンネルのみを収録した dvbv5 形式の conf ファイルを出力する CLI
#
# 使用例:
#   isdb-catv-scanner ./scanned/
#   isdb-catv-scanner --list-tuners
#   isdb-catv-scanner --channels CATV_15,CATV_C36 --recording-time 15 ./scanned/

import json
import time
from pathlib import Path

import typer
from rich import print
from rich.markup import escape
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.rule import Rule
from rich.style import Style

from isdb_scanner.catv.analyzer import CATVCarrierAnalyzer
from isdb_scanner.catv.constants import CATV_FREQUENCY_TABLE, CATVCarrierInfo
from isdb_scanner.catv.diff import CompareScanResults, FormatScanDiff
from isdb_scanner.catv.formatter import (
    CATVDvbv5ConfFormatter,
    CATVJSONFormatter,
    CATVMirakcConfigYmlFormatter,
    CATVMirakurunChannelsYmlFormatter,
)
from isdb_scanner.catv.tuner import CATVTuner
from isdb_scanner.tuner import TunerOpeningError, TunerOutputError, TunerTuningError


app = typer.Typer()


@app.command(
    help='isdb-catv-scanner: Scans Japanese CATV transmodulation channels (via dvbv5-zap) '
    'and outputs the results as CATV.json / a dvbv5 conf file.'
)
def main(
    output_dir: Path = typer.Argument(Path('scanned/'), help='Output scan results to the specified directory.'),
    channels: str | None = typer.Option(
        None,
        '--channels',
        help='Comma-separated list of physical channels to scan (ex: "CATV_15,CATV_C36"). Defaults to all CATV_* channels.',
    ),
    adapter: int | None = typer.Option(
        None,
        '--adapter',
        help='DVB adapter number to use for scanning. Defaults to the first detected CATV-capable tuner.',
    ),
    recording_time: float = typer.Option(10.0, '--recording-time', help='Recording time (seconds) for each channel.'),
    list_tuners: bool = typer.Option(False, '--list-tuners', help='List available CATV (DVB-C ANNEX_A) tuners and exit.'),
    output_dvbv5_zap_log: bool = typer.Option(False, help='Output dvbv5-zap log to stderr.'),
    collect_signal_stats: bool = typer.Option(
        True,
        '--collect-signal-stats/--no-collect-signal-stats',
        help='Collect signal quality stats (signal strength / CNR / error rate) for each receivable channel.',
    ),
    no_diff: bool = typer.Option(
        False,
        '--no-diff',
        help='Disable the scan diff report against the existing CATV.json in the output directory.',
    ),
):
    print(
        Rule(
            title='ISDBScanner CATV Scanner',
            characters='=',
            style=Style(color='#E33157'),
            align='center',
        )
    )

    # 利用可能な CATV (DVB-C ANNEX_A) 対応チューナーを検出
    tuners = CATVTuner.getAvailableCATVTuners(output_recisdb_log=output_dvbv5_zap_log)

    # --list-tuners が指定されている場合、検出結果を表示して終了
    if list_tuners is True:
        print('[bright_blue]Available CATV (DVB-C ANNEX_A) tuners:[/bright_blue]')
        for tuner in tuners:
            print(f'  [green]{tuner.name}[/green] (adapter{tuner.adapter_number}/frontend{tuner.frontend_number})')
        if len(tuners) == 0:
            print('[yellow]No CATV-capable tuner found.[/yellow]')
        print(Rule(characters='=', style=Style(color='#E33157')))
        return

    if len(tuners) == 0:
        print('[red]No CATV (DVB-C ANNEX_A) tuner found.[/red]')
        print('[red]Please connect a CATV-capable tuner and try again.[/red]')
        print(Rule(characters='=', style=Style(color='#E33157')))
        raise typer.Exit(code=1)

    # 使用するチューナーを決定 (--adapter が指定されていればそのアダプタ番号のチューナー、なければ検出結果の先頭)
    if adapter is not None:
        tuner = next((t for t in tuners if t.adapter_number == adapter), None)
        if tuner is None:
            print(f'[red]Adapter {adapter} was not found among the detected CATV-capable tuners.[/red]')
            raise typer.Exit(code=1)
    else:
        tuner = tuners[0]
    print(f'Using tuner: [green]{tuner.name}[/green]')

    # スキャン対象の物理チャンネルを決定
    if channels is not None:
        target_channels = [channel.strip() for channel in channels.split(',') if channel.strip() != '']
        unknown_channels = [channel for channel in target_channels if channel not in CATV_FREQUENCY_TABLE]
        if len(unknown_channels) > 0:
            print(f'[red]Unknown physical channel(s): {", ".join(unknown_channels)}[/red]')
            raise typer.Exit(code=1)
    else:
        target_channels = list(CATV_FREQUENCY_TABLE.keys())

    scan_start_time = time.time()
    carriers: list[CATVCarrierInfo] = []

    # プログレスバーを表示しつつ、対象の物理チャンネルを順にスキャン
    progress = Progress(
        TextColumn('[progress.description]{task.description}'),
        BarColumn(bar_width=9999),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        transient=True,
    )
    task = progress.add_task('[bright_red]Scanning...', total=len(target_channels))
    with progress:
        for index, physical_channel in enumerate(target_channels):
            progress.update(task, completed=index)
            print(Rule(characters='-', style=Style(color='#E33157')))
            print(f'  Channel: [bright_blue]{physical_channel}[/bright_blue]')

            try:
                start_time = time.time()
                try:
                    ts_stream = tuner.tune(
                        physical_channel,
                        recording_time=recording_time,
                        collect_signal_stats=collect_signal_stats,
                    )
                finally:
                    print(f'Tune Time: {time.time() - start_time:.2f} seconds')
            except TunerOpeningError as ex:
                # チューナー自体がオープンできない場合は以降の選局も見込めないため、スキャン自体を打ち切る
                print(f'[red]Failed to open tuner. {ex}[/red]')
                raise typer.Exit(code=1) from ex
            except TunerTuningError as ex:
                # 選局失敗 (信号なし・タイムアウトなど) は「受信不可」としてスキップし、スキャンを継続する
                print(f'[yellow]{ex}[/yellow]')
                print('[yellow]Channel may not be received in your area. Skipping...[/yellow]')
                continue
            except TunerOutputError:
                print('[yellow]Failed to receive data.[/yellow]')
                print('[yellow]Channel may not be received in your area. Skipping...[/yellow]')
                continue

            carrier_info = CATVCarrierAnalyzer(bytearray(ts_stream), physical_channel).analyze()
            carrier_info.signal_stats = tuner.last_signal_stats
            carriers.append(carrier_info)

            print(f'[green]Carrier Type[/green]: {carrier_info.carrier_type.value}')
            for ts_info in carrier_info.transport_streams:
                print(
                    f'[green]Transport Stream[/green]: TSID={ts_info.transport_stream_id:#06x} | '
                    f'{ts_info.network_name} | {ts_info.retransmission_source} | '
                    f'required_card={ts_info.cas.required_card} | services={len(ts_info.services)}'
                )

            # 信号品質統計はドライバによって取得できる項目が異なる (dB系/%系のどちらか一方だけのことが多い) ため、
            # 実際に値が取れた項目だけを表示する
            if carrier_info.signal_stats is not None:
                stats = carrier_info.signal_stats
                stat_parts: list[str] = []
                if stats.signal_strength_dbm is not None:
                    stat_parts.append(f'Signal={stats.signal_strength_dbm:.1f}dBm')
                if stats.signal_strength_percent is not None:
                    stat_parts.append(f'Signal={stats.signal_strength_percent:.1f}%')
                if stats.cnr_db is not None:
                    stat_parts.append(f'CNR={stats.cnr_db:.1f}dB')
                if stats.cnr_percent is not None:
                    stat_parts.append(f'CNR={stats.cnr_percent:.1f}%')
                if stats.error_rate is not None:
                    stat_parts.append(f'PreBER={stats.error_rate:.3e}')
                if len(stat_parts) > 0:
                    print(f'[green]Signal Stats[/green]: {" | ".join(stat_parts)}')

        progress.update(task, completed=len(target_channels))

    # 出力先ディレクトリがなければ作成 (事前に絶対パスに変換しておく)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 物理チャンネル順にソートしてから出力
    carriers = sorted(carriers, key=lambda carrier: carrier.physical_channel)

    # 出力先に既存の CATV.json があれば、上書きする前に読み込んで前回との差分レポートを生成する
    # (--no-diff が指定されている場合や、そもそも前回のスキャン結果が存在しない場合はスキップする)
    catv_json_path = output_dir / 'CATV.json'
    if no_diff is False and catv_json_path.is_file():
        try:
            previous_scan_result = json.loads(catv_json_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as ex:
            print(f'[yellow]Failed to read the previous CATV.json for the diff report: {ex}[/yellow]')
            previous_scan_result = None

        if previous_scan_result is not None:
            # 前回の CATV.json が古いスキーマや手編集で壊れていても、スキャン結果本体の保存 (この後の save() 群) を
            # 巻き添えにしないよう、差分レポートの生成・出力はまとめて try/except で保護する
            try:
                current_scan_result = {carrier.physical_channel: carrier.model_dump(mode='json') for carrier in carriers}
                diff = CompareScanResults(previous_scan_result, current_scan_result)
                diff_report = FormatScanDiff(diff)
            except Exception as ex:
                print(f'[yellow]Failed to generate the scan diff report: {type(ex).__name__}: {ex}[/yellow]')
                print('[yellow]The previous CATV.json may be corrupted or in an old format. Skipping the diff report.[/yellow]')
            else:
                print(Rule(characters='-', style=Style(color='#E33157')))
                print('[bright_blue]Scan Diff Report (compared to the previous CATV.json)[/bright_blue]')
                # レポート自体は rich マークアップなしのプレーンテキストだが、サービス名などに "[" を含む値が
                # 混ざっていても rich がマークアップとして誤解釈しないよう escape() を通してから表示する
                print(escape(diff_report))
                (output_dir / 'CATV.diff.txt').write_text(diff_report, encoding='utf-8')

    CATVJSONFormatter(catv_json_path, carriers).save()
    CATVDvbv5ConfFormatter(output_dir / 'dvbv5_channels_catv.conf', carriers).save()

    # レコーダー (Mirakurun/mirakc) 向けのチャンネル設定もあわせて出力する
    mirakurun_dir = output_dir / 'Mirakurun'
    mirakurun_dir.mkdir(parents=True, exist_ok=True)
    CATVMirakurunChannelsYmlFormatter(mirakurun_dir / 'channels_catv.yml', carriers).save()

    mirakc_dir = output_dir / 'mirakc'
    mirakc_dir.mkdir(parents=True, exist_ok=True)
    CATVMirakcConfigYmlFormatter(mirakc_dir / 'channels_catv.yml', carriers).save()

    receivable_carrier_count = len([carrier for carrier in carriers if carrier.carrier_type.value != 'Empty'])
    print(Rule(characters='=', style=Style(color='#E33157')))
    print(f'Finished in {time.time() - scan_start_time:.2f} seconds.')
    print(f'Scanned {len(carriers)} / {len(target_channels)} channel(s). ({receivable_carrier_count} receivable)')
    print(Rule(characters='=', style=Style(color='#E33157')))


if __name__ == '__main__':
    app()

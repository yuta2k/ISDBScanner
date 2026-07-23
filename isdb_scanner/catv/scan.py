#!/usr/bin/env python3

# isdb-catv-scanner: 日本の CATV トランスモジュレーション (J.83 Annex C 64QAM) の物理チャンネルをスキャンし、
# 各キャリアの解析結果 (CATV.json) と、受信できたチャンネルのみを収録した dvbv5 形式の conf ファイルを出力する CLI
#
# 使用例:
#   isdb-catv-scanner ./scanned/
#   isdb-catv-scanner --list-tuners
#   isdb-catv-scanner --channels CATV_15,CATV_C36 --recording-time 15 ./scanned/

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer
from rich import print
from rich.markup import escape
from rich.progress import BarColumn, Progress, TaskID, TaskProgressColumn, TextColumn, TimeRemainingColumn
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


def _ScanWorker(
    tuner: CATVTuner,
    channel_queue: queue.Queue[str],
    recording_time: float,
    collect_signal_stats: bool,
    print_lock: threading.Lock,
    progress: Progress,
    task: TaskID,
    carriers: list[CATVCarrierInfo],
) -> int | None:
    """
    1 台のチューナー専有でスキャンを行うワーカー関数 (ThreadPoolExecutor からチューナー1台につき1スレッドで並列実行される)
    共有キュー (channel_queue) から未スキャンの物理チャンネルを1つずつ取り出し、選局・解析して結果を carriers に追記する

    並列化の本体である選局 (tuner.tune()) 中はロックを持たず、選局完了/失敗後に print_lock を取得してから
    1 チャンネル分の表示ブロック・解析・集約・プログレス更新をまとめて直列に行う。これにより rich の出力が
    ワーカー間でインターリーブして崩れるのを防ぎ、共有 list (carriers) への競合も避ける
    (解析は選局の数秒〜十数秒に比べれば短時間で済むため、直列化しても並列スキャンの利得はほとんど損なわれない)。
    tuner は1ワーカーが専有するため、last_signal_stats の読み出しはロック外でも安全 (選局直後に読んでおく)

    Args:
        tuner (CATVTuner): このワーカーが専有するチューナー
        channel_queue (queue.Queue[str]): 未スキャンの物理チャンネル名を格納した共有キュー
        recording_time (float): 各チャンネルの録画時間 (秒)
        collect_signal_stats (bool): 信号品質統計を取得するかどうか
        print_lock (threading.Lock): 表示ブロック・解析・集約を直列化するためのロック
        progress (Progress): プログレスバー (1 チャンネル完了ごとに advance する)
        task (TaskID): プログレスバーのタスク ID
        carriers (list[CATVCarrierInfo]): 解析結果を追記する共有リスト (追記は print_lock 内でのみ行う)

    Returns:
        int | None: チューナーが途中で使用不能 (オープン失敗) になった場合はそのアダプタ番号、
            残チャンネルがなくなるまで正常に処理し終えた場合は None
    """

    while True:
        try:
            physical_channel = channel_queue.get_nowait()
        except queue.Empty:
            # 未スキャンのチャンネルがなくなったので正常終了
            return None

        # 選局 (この間はロックを持たない = ここが並列化の本体)。選局所要時間は成功/失敗に関わらず計測する
        try:
            start_time = time.time()
            try:
                ts_stream = tuner.tune(
                    physical_channel,
                    recording_time=recording_time,
                    collect_signal_stats=collect_signal_stats,
                )
            finally:
                tune_time = time.time() - start_time
        except TunerOpeningError as ex:
            # チューナー自体がオープンできない場合は以降の選局も見込めないため、このチューナーは以後使えないとみなす
            # 取得済みのチャンネルはキューに戻し (他の生存チューナーが拾えるように)、アダプタ番号を返してワーカーを終了する
            channel_queue.put(physical_channel)
            with print_lock:
                print(f'[red]Failed to open tuner {tuner.name}. {ex}[/red]')
            return tuner.adapter_number
        except TunerTuningError as ex:
            # 選局失敗 (信号なし・タイムアウトなど) は「受信不可」としてスキップし、スキャンを継続する
            with print_lock:
                print(Rule(characters='-', style=Style(color='#E33157')))
                print(f'  Channel: [bright_blue]{physical_channel}[/bright_blue]')
                print(f'Tune Time: {tune_time:.2f} seconds')
                print(f'[yellow]{ex}[/yellow]')
                print('[yellow]Channel may not be received in your area. Skipping...[/yellow]')
                progress.advance(task)
            continue
        except TunerOutputError:
            with print_lock:
                print(Rule(characters='-', style=Style(color='#E33157')))
                print(f'  Channel: [bright_blue]{physical_channel}[/bright_blue]')
                print(f'Tune Time: {tune_time:.2f} seconds')
                print('[yellow]Failed to receive data.[/yellow]')
                print('[yellow]Channel may not be received in your area. Skipping...[/yellow]')
                progress.advance(task)
            continue

        # 信号品質統計はワーカー専有チューナーのインスタンス状態のため、ロックを取る前に (選局直後に) 読み出しておく
        signal_stats = tuner.last_signal_stats

        # 選局成功時の表示ブロック・解析・集約・プログレス更新はまとめて print_lock 内で直列に行う
        with print_lock:
            print(Rule(characters='-', style=Style(color='#E33157')))
            print(f'  Channel: [bright_blue]{physical_channel}[/bright_blue]')
            print(f'Tune Time: {tune_time:.2f} seconds')

            carrier_info = CATVCarrierAnalyzer(bytearray(ts_stream), physical_channel).analyze()
            carrier_info.signal_stats = signal_stats
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

            progress.advance(task)


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
    adapters: str | None = typer.Option(
        None,
        '--adapters',
        help='Comma-separated list of DVB adapter numbers to scan with in parallel (ex: "0,1,2"). '
        'Cannot be combined with --adapter.',
    ),
    parallel: bool = typer.Option(
        True,
        '--parallel/--no-parallel',
        help='Scan in parallel using all detected CATV-capable tuners (default). '
        'Use --no-parallel to scan with a single tuner, or --adapter/--adapters to select tuners explicitly.',
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

    # 使用するチューナー (スキャンワーカーに1台ずつ割り当てるチューナー群) を決定する
    # 既定では検出された全チューナーで並列スキャンし、--adapter / --adapters でチューナーを明示指定できる
    # (--adapter は単一チューナー・--adapters は指定した複数チューナーで、いずれも --parallel/--no-parallel より優先される)
    # --adapter (単一指定) と --adapters (複数指定) は指定意図が競合するため併用不可
    if adapter is not None and adapters is not None:
        print('[red]--adapter cannot be combined with --adapters.[/red]')
        raise typer.Exit(code=1)

    if adapter is not None:
        # 単一のアダプタ番号を明示指定 (この場合は1台のみを使う)
        matched_tuner = next((t for t in tuners if t.adapter_number == adapter), None)
        if matched_tuner is None:
            print(f'[red]Adapter {adapter} was not found among the detected CATV-capable tuners.[/red]')
            raise typer.Exit(code=1)
        scan_tuners = [matched_tuner]
    elif adapters is not None:
        # 指定されたアダプタ番号のチューナー群を使って並列スキャンする (frontend 番号は検出結果のものを使う)
        try:
            adapter_numbers = [int(a.strip()) for a in adapters.split(',') if a.strip() != '']
        except ValueError as ex:
            print('[red]--adapters must be a comma-separated list of integers.[/red]')
            raise typer.Exit(code=1) from ex
        if len(adapter_numbers) == 0:
            print('[red]No adapter number specified in --adapters.[/red]')
            raise typer.Exit(code=1)
        if len(set(adapter_numbers)) != len(adapter_numbers):
            print('[red]--adapters contains duplicate adapter numbers.[/red]')
            raise typer.Exit(code=1)
        scan_tuners = []
        for adapter_number in adapter_numbers:
            matched_tuner = next((t for t in tuners if t.adapter_number == adapter_number), None)
            if matched_tuner is None:
                print(f'[red]Adapter {adapter_number} was not found among the detected CATV-capable tuners.[/red]')
                raise typer.Exit(code=1)
            scan_tuners.append(matched_tuner)
    elif parallel is True:
        # 既定: 検出された全 CATV 対応チューナーを使って並列スキャンする
        scan_tuners = list(tuners)
    else:
        # --no-parallel: 検出結果の先頭 1 台のみを使って逐次スキャンする
        scan_tuners = [tuners[0]]

    if len(scan_tuners) == 1:
        print(f'Using tuner: [green]{scan_tuners[0].name}[/green]')
    else:
        print('Using tuners:')
        for tuner in scan_tuners:
            print(f'  [green]{tuner.name}[/green] (adapter{tuner.adapter_number}/frontend{tuner.frontend_number})')

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

    # 対象の物理チャンネルを共有キューに投入し、チューナー1台につき1ワーカーで並列に取り出してスキャンする
    # (1 ワーカーでも同じコードパスを通るため、逐次スキャンは「ワーカー1台の並列スキャン」の特殊ケースになる)
    channel_queue: queue.Queue[str] = queue.Queue()
    for physical_channel in target_channels:
        channel_queue.put(physical_channel)

    # rich の表示・解析・共有 list への追記を直列化するためのロック (詳細は _ScanWorker の docstring 参照)
    print_lock = threading.Lock()
    with progress:
        with ThreadPoolExecutor(max_workers=len(scan_tuners)) as executor:
            futures = [
                executor.submit(
                    _ScanWorker,
                    tuner,
                    channel_queue,
                    recording_time,
                    collect_signal_stats,
                    print_lock,
                    progress,
                    task,
                    carriers,
                )
                for tuner in scan_tuners
            ]
            # 各ワーカーの戻り値 (途中で使用不能になったチューナーのアダプタ番号 / 正常終了なら None) を回収する
            # (ワーカー内で捕捉していない予期しない例外は future.result() でそのまま送出される)
            dead_adapters = sorted({adapter for future in futures if (adapter := future.result()) is not None})

    # 全ワーカー終了後もキューにチャンネルが残っている = 全チューナーがオープン失敗した (残チャンネルを消化できなかった) 場合は、
    # スキャン結果が不完全なため打ち切る (1 ワーカー時は現行同様、チューナーのオープン失敗で即打ち切りになる)
    if not channel_queue.empty():
        print('[red]All tuners became unavailable before the scan could finish. Scan aborted.[/red]')
        print(Rule(characters='=', style=Style(color='#E33157')))
        raise typer.Exit(code=1)

    # 一部のチューナーだけ途中で使用不能になったが、残りのチューナーが全チャンネルを消化できた場合は
    # スキャン自体は成功とみなし、使用不能になったチューナーを警告として表示するにとどめる
    for adapter_number in dead_adapters:
        print(f'[yellow]adapter{adapter_number} became unavailable during the scan (the remaining tuner(s) completed it).[/yellow]')

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

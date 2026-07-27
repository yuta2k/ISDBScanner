#!/usr/bin/env python3

# isdb-catv-scanner: 日本の CATV トランスモジュレーション (J.83 Annex C 64QAM) の物理チャンネルをスキャンし、
# 各キャリアの解析結果 (CATV.json) と、受信できたチャンネルのみを収録した dvbv5 形式の conf ファイルを出力する CLI
# --terrestrial / --satellite 指定時は、ネイティブ地上波 (ISDB-T) / BS/CS (ISDB-S 衛星アンテナ直結) のスキャンも
# 合わせて行い、Mirakurun/mirakc/EDCB 向けのチャンネル・チューナー設定に CATV 分と統合して出力する
# --from-json 指定時は、スキャンを行わず既存 JSON から出力ファイル群だけを再生成する
#
# 使用例:
#   isdb-catv-scanner ./scanned/
#   isdb-catv-scanner --list-tuners
#   isdb-catv-scanner --channels CATV_15,CATV_C36 --recording-time 15 ./scanned/
#   isdb-catv-scanner --terrestrial --satellite ./scanned/
#   isdb-catv-scanner --terrestrial --satellite --prefer native ./scanned/
#   isdb-catv-scanner --from-json ./scanned/

import json
import queue
import shutil
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
from isdb_scanner.catv.constants import CATV_FREQUENCY_TABLE, CATVCarrierInfo, PreferredSource
from isdb_scanner.catv.diff import CompareScanResults, FormatScanDiff
from isdb_scanner.catv.edcb import CATVEDCBChSet4TxtFormatter, CATVEDCBChSet5TxtFormatter
from isdb_scanner.catv.formatter import (
    CATVDvbv5ConfFormatter,
    CATVJSONFormatter,
    CATVMirakcConfigYmlFormatter,
    CATVMirakcTunersYmlFormatter,
    CATVMirakurunChannelsYmlFormatter,
    CATVMirakurunTunersYmlFormatter,
    GetEmittedCATVChannelTypes,
    NativeJSONFormatter,
)
from isdb_scanner.catv.native_diff import BuildNativeScanDiffReport
from isdb_scanner.catv.satellite import ScanSatelliteChannels
from isdb_scanner.catv.terrestrial import ScanTerrestrialChannels
from isdb_scanner.catv.tuner import CATVTuner
from isdb_scanner.constants import LNBVoltage, TransportStreamInfo
from isdb_scanner.tuner import ISDBTuner, TunerOpeningError, TunerOutputError, TunerTuningError


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


def _BuildFormatterKwargs(
    tr_ts_infos: list[TransportStreamInfo],
    bs_ts_infos: list[TransportStreamInfo],
    cs_ts_infos: list[TransportStreamInfo],
    exclude_pay_tv: bool,
    bcas_only: bool,
    cas_as_sky: bool,
    prefer: PreferredSource | None,
    normalize_names: bool,
) -> dict:
    """
    channels/EDCB フォーマッター (CATVMirakurunChannelsYmlFormatter / CATVMirakcConfigYmlFormatter /
    CATVEDCBChSet4TxtFormatter / CATVEDCBChSet5TxtFormatter) に共通で渡すキーワード引数一式を組み立てる
    (通常スキャン時と --from-json 再フォーマット時で同じ配線を使い回すためのヘルパー)
    """

    return {
        'tr_ts_infos': tr_ts_infos,
        'bs_ts_infos': bs_ts_infos,
        'cs_ts_infos': cs_ts_infos,
        'exclude_pay_tv': exclude_pay_tv,
        'bcas_only': bcas_only,
        'cas_as_sky': cas_as_sky,
        'prefer': prefer,
        'normalize_names': normalize_names,
    }


def _EmitNativeScanDiff(
    output_dir: Path,
    band_label: str,
    current_ts_infos: list[TransportStreamInfo],
    no_diff: bool,
) -> None:
    """
    ネイティブ (地上波/BS/CS) スキャン結果について、出力先に残っている前回の JSON (Terrestrial.json / BS.json / CS.json)
    と今回のスキャン結果の差分レポートを生成し、差分があれば表示 + `<band_label>.diff.txt` に保存する
    (CATV.diff.txt と同じ流儀: 前回 JSON の読み込み・diff 生成は JSON を上書きする前に呼ぶこと。壊れた JSON でも
     この後の JSON 保存を巻き添えにしないよう try/except で保護し、差分なし ('') のときは何もしない)

    Args:
        output_dir (Path): 出力先ディレクトリ (絶対パス)
        band_label (str): 放送帯域名 ('Terrestrial' / 'BS' / 'CS')。JSON/diff ファイル名にもそのまま使う
        current_ts_infos (list[TransportStreamInfo]): 今回のスキャン結果
        no_diff (bool): True の場合は差分レポートを生成しない
    """

    if no_diff is True:
        return
    json_path = output_dir / f'{band_label}.json'
    if not json_path.is_file():
        return
    try:
        previous_scan_result = json.loads(json_path.read_text(encoding='utf-8'))
        diff_report = BuildNativeScanDiffReport(previous_scan_result, current_ts_infos, band_label)
    except Exception as ex:
        print(f'[yellow]Failed to generate the {band_label} scan diff report: {type(ex).__name__}: {ex}[/yellow]')
        print(f'[yellow]The previous {band_label}.json may be corrupted or in an old format. Skipping the diff report.[/yellow]')
        return
    if diff_report == '':
        return
    print(Rule(characters='-', style=Style(color='#E33157')))
    print(f'[bright_blue]{band_label} Scan Diff Report (compared to the previous {band_label}.json)[/bright_blue]')
    # レポートはプレーンテキストだが、サービス名などに "[" を含む値が rich マークアップとして誤解釈されないよう escape() を通す
    print(escape(diff_report))
    (output_dir / f'{band_label}.diff.txt').write_text(diff_report, encoding='utf-8')


def _WriteRecorderConfigs(
    output_dir: Path,
    carriers: list[CATVCarrierInfo],
    catv_tuners: list[CATVTuner],
    isdbt_tuners: list[ISDBTuner],
    isdbs_tuners: list[ISDBTuner],
    formatter_kwargs: dict,
    warn_when_no_catv_tuner: bool = False,
) -> None:
    """
    レコーダー (Mirakurun/mirakc) 向けのチャンネル設定 (channels_catv.yml) / チューナー設定 (tuners_catv.yml) と、
    EDCB (EDCB-Wine) 向けの ChSet4/ChSet5 テキストを出力する。通常スキャン時と --from-json 再フォーマット時で共用する

    Args:
        output_dir (Path): 出力先ディレクトリ (絶対パス)
        carriers (list[CATVCarrierInfo]): CATV キャリア情報のリスト
        catv_tuners (list[CATVTuner]): tuners_catv.yml に列挙する CATV チューナー (今回スキャンに使った/ライブ検出したもの)
        isdbt_tuners (list[ISDBTuner]): tuners_catv.yml に列挙する ISDB-T チューナー (未検出なら空リスト)
        isdbs_tuners (list[ISDBTuner]): tuners_catv.yml に列挙する ISDB-S チューナー (未検出なら空リスト)
        formatter_kwargs (dict): channels/EDCB フォーマッターに渡す共通キーワード引数 (_BuildFormatterKwargs() の戻り値)
        warn_when_no_catv_tuner (bool): CATV チューナーが 0 台のとき tuners_catv.yml をスキップした旨を黄警告するか
    """

    # Mirakurun/mirakc 向けチャンネル設定 (CATV 分 + ネイティブ地上波/BS/CS 分)
    mirakurun_dir = output_dir / 'Mirakurun'
    mirakurun_dir.mkdir(parents=True, exist_ok=True)
    CATVMirakurunChannelsYmlFormatter(mirakurun_dir / 'channels_catv.yml', carriers, **formatter_kwargs).save()

    mirakc_dir = output_dir / 'mirakc'
    mirakc_dir.mkdir(parents=True, exist_ok=True)
    CATVMirakcConfigYmlFormatter(mirakc_dir / 'channels_catv.yml', carriers, **formatter_kwargs).save()

    # EDCB (EDCB-Wine) 向けの ChSet4/ChSet5 テキスト (実機検証未了の実験的出力)
    # ネイティブスキャナ (EDCB-Wine の BonDriver_mirakc) の命名に倣い BonDriver_mirakc(BonDriver_mirakc).ChSet4.txt / ChSet5.txt を出力する
    edcb_dir = output_dir / 'EDCB-Wine'
    edcb_dir.mkdir(parents=True, exist_ok=True)
    CATVEDCBChSet4TxtFormatter(edcb_dir / 'BonDriver_mirakc(BonDriver_mirakc).ChSet4.txt', carriers, **formatter_kwargs).save()
    CATVEDCBChSet5TxtFormatter(edcb_dir / 'ChSet5.txt', carriers, **formatter_kwargs).save()

    # チューナー設定 (tuners_catv.yml): channels 側に実際に出力される CATV エントリの type 集合を CATV チューナーの types に列挙する
    if len(catv_tuners) == 0:
        if warn_when_no_catv_tuner is True:
            print('[yellow]No CATV tuner found. Skipping tuners_catv.yml generation.[/yellow]')
        return
    catv_channel_types = GetEmittedCATVChannelTypes(
        carriers,
        exclude_pay_tv=formatter_kwargs['exclude_pay_tv'],
        bcas_only=formatter_kwargs['bcas_only'],
        cas_as_sky=formatter_kwargs['cas_as_sky'],
        prefer=formatter_kwargs['prefer'],
        tr_ts_infos=formatter_kwargs['tr_ts_infos'],
        bs_ts_infos=formatter_kwargs['bs_ts_infos'],
        cs_ts_infos=formatter_kwargs['cs_ts_infos'],
    )
    dvbv5_conf_path = output_dir / 'dvbv5_channels_catv.conf'
    CATVMirakurunTunersYmlFormatter(
        mirakurun_dir / 'tuners_catv.yml', catv_tuners, isdbt_tuners, isdbs_tuners, dvbv5_conf_path, catv_channel_types
    ).save()
    CATVMirakcTunersYmlFormatter(
        mirakc_dir / 'tuners_catv.yml', catv_tuners, isdbt_tuners, isdbs_tuners, dvbv5_conf_path, catv_channel_types
    ).save()


def _ReformatFromJson(
    output_dir: Path,
    exclude_pay_tv: bool,
    bcas_only: bool,
    cas_as_sky: bool,
    prefer: PreferredSource | None,
    normalize_names: bool,
    lnb: LNBVoltage,
    output_recisdb_log: bool,
) -> None:
    """
    --from-json 再フォーマットモード: チューナー検出もスキャンも行わず、出力先に既存の JSON (CATV.json 必須 +
    Terrestrial.json / BS.json / CS.json は任意) だけを読み込んで、出力ファイル群 (dvbv5 conf / Mirakurun・mirakc の
    channels/tuners / EDCB) を再生成する。JSON 群と diff は再生成しない (読み取り専用)

    Args:
        output_dir (Path): 既存 JSON を読み込む出力先ディレクトリ
        exclude_pay_tv / bcas_only / cas_as_sky / prefer / normalize_names: 出力系オプション (channels/EDCB へ受け渡す)
        lnb (LNBVoltage): tuners 生成用のライブチューナー検出時の LNB 給電電圧
        output_recisdb_log (bool): tuners 生成用のライブチューナー検出時に recisdb ログを出力するか
    """

    output_dir = output_dir.resolve()

    # CATV.json は必須。読めない/存在しない場合は赤エラーで終了する
    catv_json_path = output_dir / 'CATV.json'
    if not catv_json_path.is_file():
        print(f'[red]CATV.json was not found in {output_dir}. --from-json requires an existing CATV.json.[/red]')
        print(Rule(characters='=', style=Style(color='#E33157')))
        raise typer.Exit(code=1)
    try:
        catv_json = json.loads(catv_json_path.read_text(encoding='utf-8'))
        carriers = [CATVCarrierInfo.model_validate(value) for value in catv_json.values()]
    except Exception as ex:
        print(f'[red]Failed to read or parse CATV.json: {type(ex).__name__}: {ex}[/red]')
        print(Rule(characters='=', style=Style(color='#E33157')))
        raise typer.Exit(code=1) from ex
    carriers = sorted(carriers, key=lambda carrier: carrier.physical_channel)

    # Terrestrial.json / BS.json / CS.json は存在すれば復元し、無ければ空扱いにする
    def _LoadNativeJson(file_name: str) -> list[TransportStreamInfo]:
        json_path = output_dir / file_name
        if not json_path.is_file():
            return []
        try:
            entries = json.loads(json_path.read_text(encoding='utf-8'))
            return [TransportStreamInfo.model_validate(entry) for entry in entries]
        except Exception as ex:
            print(f'[yellow]Failed to read {file_name}: {type(ex).__name__}: {ex}. Ignoring it.[/yellow]')
            return []

    tr_ts_infos = _LoadNativeJson('Terrestrial.json')
    bs_ts_infos = _LoadNativeJson('BS.json')
    cs_ts_infos = _LoadNativeJson('CS.json')

    output_dir.mkdir(parents=True, exist_ok=True)

    # dvbv5 conf を再生成 (tuners が参照するため channels より先に生成する)
    CATVDvbv5ConfFormatter(output_dir / 'dvbv5_channels_catv.conf', carriers).save()

    # tuners 生成用のチューナー情報はベストエフォートでライブ検出する (見つからなくても exit しない)
    catv_tuners = CATVTuner.getAvailableCATVTuners(output_recisdb_log=output_recisdb_log)
    isdbt_tuners: list[ISDBTuner] = []
    isdbs_tuners: list[ISDBTuner] = []
    if shutil.which('recisdb') is not None:
        isdbt_tuners = ISDBTuner.getAvailableISDBTTuners(lnb=lnb, output_recisdb_log=output_recisdb_log)
        isdbs_tuners = ISDBTuner.getAvailableISDBSTuners(lnb=lnb, output_recisdb_log=output_recisdb_log)

    formatter_kwargs = _BuildFormatterKwargs(
        tr_ts_infos, bs_ts_infos, cs_ts_infos, exclude_pay_tv, bcas_only, cas_as_sky, prefer, normalize_names
    )
    _WriteRecorderConfigs(
        output_dir, carriers, catv_tuners, isdbt_tuners, isdbs_tuners, formatter_kwargs, warn_when_no_catv_tuner=True
    )

    print(Rule(characters='=', style=Style(color='#E33157')))
    print('Reformatted from existing JSON.')
    print(
        f'CATV: {len(carriers)} carrier(s) / Terrestrial: {len(tr_ts_infos)} TS / '
        f'BS: {len(bs_ts_infos)} TS / CS: {len(cs_ts_infos)} TS'
    )
    print(Rule(characters='=', style=Style(color='#E33157')))


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
    satellite: bool = typer.Option(
        False,
        '--satellite/--no-satellite',
        help='Also scan native BS/CS (ISDB-S) channels with recisdb and merge them into the recorder configs '
        '(default: disabled). Skipped with a warning when no ISDB-S tuner (or recisdb) is available.',
    ),
    terrestrial: bool = typer.Option(
        False,
        '--terrestrial/--no-terrestrial',
        help='Also scan native terrestrial (ISDB-T) channels with recisdb and merge them into the recorder configs '
        '(default: disabled). Skipped with a warning when no ISDB-T tuner (or recisdb) is available.',
    ),
    exclude_pay_tv: bool = typer.Option(
        False,
        '--exclude-pay-tv',
        help='Exclude pay-TV channels from the Mirakurun/mirakc channel configs (both CATV and native BS/CS entries). '
        'Also skips the native CS scan when --satellite is set. JSON outputs always include all channels.',
    ),
    bcas_only: bool = typer.Option(
        False,
        '--bcas-only',
        help='Limit the Mirakurun/mirakc channel configs to CATV channels receivable with a B-CAS card '
        '(excludes channels that require C-CAS/A-CAS or have an unknown CAS). JSON outputs always include all channels.',
    ),
    cas_as_sky: bool = typer.Option(
        False,
        '--cas-as-sky',
        help='Output CATV channels that require C-CAS/A-CAS as "type: SKY" in the Mirakurun/mirakc channel configs.',
    ),
    prefer: PreferredSource | None = typer.Option(
        None,
        '--prefer',
        help='When a CATV-retransmitted TS and a native (recisdb) TS point to the same TS, keep the specified side '
        'enabled and mark the other as disabled in the recorder configs ("catv" or "native"). Default: keep both.',
    ),
    normalize_names: bool = typer.Option(
        False,
        '--normalize-names',
        help='Normalize full-width alphanumerics/symbols in channel names to half-width in the recorder configs.',
    ),
    from_json: bool = typer.Option(
        False,
        '--from-json',
        help='Reformat mode: skip tuner detection and scanning, and regenerate the output files (dvbv5 conf, '
        'Mirakurun/mirakc channels & tuners, EDCB) from the existing JSON in the output directory. Requires CATV.json. '
        'JSON outputs and diff reports are not regenerated. Scan-related options (--channels/--adapter/--adapters/'
        '--parallel/--recording-time/--satellite/--terrestrial etc.) are ignored; only output options '
        '(--exclude-pay-tv/--bcas-only/--cas-as-sky/--prefer/--normalize-names/--no-diff) take effect.',
    ),
    lnb: LNBVoltage = typer.Option(
        LNBVoltage.LOW, '--lnb', help='LNB voltage for satellite antenna power supply (native BS/CS scan only).'
    ),
    output_recisdb_log: bool = typer.Option(
        False, '--output-recisdb-log', help='Output recisdb log to stderr (native BS/CS/terrestrial scan only).'
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

    # --from-json (再フォーマットモード): チューナー検出もスキャンも行わず、既存 JSON から出力ファイル群だけを再生成する
    if from_json is True:
        _ReformatFromJson(output_dir, exclude_pay_tv, bcas_only, cas_as_sky, prefer, normalize_names, lnb, output_recisdb_log)
        return

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
        print('[bright_blue]Available ISDB-S tuners (for native BS/CS scan):[/bright_blue]')
        isdbs_tuners = ISDBTuner.getAvailableISDBSTuners()
        for isdbs_tuner in isdbs_tuners:
            print(
                f'  [{isdbs_tuner.device_type}] [green]{isdbs_tuner.name}[/green] ({isdbs_tuner.device_path}) '
                f'{"(Busy)" if isdbs_tuner.isBusy() else ""}'
            )
        if len(isdbs_tuners) == 0:
            print('[yellow]No ISDB-S tuner found.[/yellow]')
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

    # ネイティブ BS/CS (ISDB-S) スキャンに使うチューナーを CATV スキャン開始前に検出しておく
    # (recisdb や ISDB-S チューナーがない場合でも CATV スキャン自体は続行し、衛星スキャンだけをスキップする)
    satellite_tuners: list[ISDBTuner] = []
    if satellite is True:
        if shutil.which('recisdb') is None:
            print('[yellow]recisdb not found. Skipping native BS/CS (satellite) scan.[/yellow]')
            satellite = False
        else:
            satellite_tuners = ISDBTuner.getAvailableISDBSTuners(lnb=lnb, output_recisdb_log=output_recisdb_log)
            if len(satellite_tuners) == 0:
                print('[yellow]No ISDB-S tuner found. Skipping native BS/CS (satellite) scan.[/yellow]')
                satellite = False

    # ネイティブ地上波 (ISDB-T) スキャンに使うチューナーも CATV スキャン開始前に検出しておく (衛星と同じ扱い)
    # (recisdb や ISDB-T チューナーがない場合でも CATV スキャン自体は続行し、地上波スキャンだけをスキップする)
    terrestrial_tuners: list[ISDBTuner] = []
    if terrestrial is True:
        if shutil.which('recisdb') is None:
            print('[yellow]recisdb not found. Skipping native terrestrial (ISDB-T) scan.[/yellow]')
            terrestrial = False
        else:
            terrestrial_tuners = ISDBTuner.getAvailableISDBTTuners(lnb=lnb, output_recisdb_log=output_recisdb_log)
            if len(terrestrial_tuners) == 0:
                print('[yellow]No ISDB-T tuner found. Skipping native terrestrial (ISDB-T) scan.[/yellow]')
                terrestrial = False

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

    # ***** ネイティブ地上波 (ISDB-T) のチャンネルスキャン *****
    ## CATV スキャン (dvbv5-zap / DVB-C) とはチューナーも選局手段 (recisdb) も別系統のため、CATV スキャン完了後に直列で実行する
    ## (衛星より先に実行する: 実行順は CATV → 地上波 → 衛星)
    tr_ts_infos: list[TransportStreamInfo] = []
    terrestrial_scanned = False
    if terrestrial is True:
        print(Rule(characters='=', style=Style(color='#E33157')))
        print('Scanning native ISDB-T (Terrestrial) channels...')
        print(Rule(characters='-', style=Style(color='#E33157')))
        for terrestrial_tuner in terrestrial_tuners:
            print(f'Found Tuner: [green]{terrestrial_tuner.name}[/green] ({terrestrial_tuner.device_path})')
        tr_ts_infos = ScanTerrestrialChannels(terrestrial_tuners)
        terrestrial_scanned = True
        if len(tr_ts_infos) == 0:
            print('[yellow]No terrestrial transport stream could be received. Check the antenna cable connection.[/yellow]')

    # ***** ネイティブ BS/CS (ISDB-S) のチャンネルスキャン *****
    ## CATV スキャン (dvbv5-zap / DVB-C) とはチューナーも選局手段 (recisdb) も別系統のため、CATV スキャン完了後に直列で実行する
    ## (スキャン対象は BS01/TS0 + ND02 + ND04 の最大 3 チャンネルのみなので、直列でも所要時間は 40 秒程度で済む)
    bs_ts_infos: list[TransportStreamInfo] = []
    cs_ts_infos: list[TransportStreamInfo] = []
    satellite_scanned = False
    if satellite is True:
        print(Rule(characters='=', style=Style(color='#E33157')))
        print('Scanning native ISDB-S (Satellite) channels...')
        print(Rule(characters='-', style=Style(color='#E33157')))
        for satellite_tuner in satellite_tuners:
            print(f'Found Tuner: [green]{satellite_tuner.name}[/green] ({satellite_tuner.device_path})')
        bs_ts_infos, cs_ts_infos = ScanSatelliteChannels(satellite_tuners, exclude_pay_tv)
        satellite_scanned = True
        if len(bs_ts_infos) == 0:
            print('[yellow]No BS transport stream could be received. Check the antenna cable and LNB power supply (--lnb).[/yellow]')

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

    # ネイティブ地上波/BS/CS のスキャンを実行した場合は、その解析結果も JSON として出力する
    # (既存の isdb-scanner の慣習に合わせ、JSON のみ --exclude-pay-tv の指定に関わらず取得できた全チャンネルを出力する)
    # 前回の JSON が残っていれば、上書きする前にネイティブスキャン差分レポート (Terrestrial/BS/CS.diff.txt) を生成する
    if terrestrial_scanned is True:
        _EmitNativeScanDiff(output_dir, 'Terrestrial', tr_ts_infos, no_diff)
        NativeJSONFormatter(output_dir / 'Terrestrial.json', tr_ts_infos).save()
    if satellite_scanned is True:
        _EmitNativeScanDiff(output_dir, 'BS', bs_ts_infos, no_diff)
        NativeJSONFormatter(output_dir / 'BS.json', bs_ts_infos).save()
        if exclude_pay_tv is False:
            _EmitNativeScanDiff(output_dir, 'CS', cs_ts_infos, no_diff)
            NativeJSONFormatter(output_dir / 'CS.json', cs_ts_infos).save()

    # レコーダー (Mirakurun/mirakc) 向けのチャンネル設定・チューナー設定・EDCB 出力もあわせて出力する
    # (ネイティブ地上波/BS/CS のスキャンを実行した場合は、CATV 分に加えてそのチャンネルエントリも統合して出力される)
    formatter_kwargs = _BuildFormatterKwargs(
        tr_ts_infos, bs_ts_infos, cs_ts_infos, exclude_pay_tv, bcas_only, cas_as_sky, prefer, normalize_names
    )
    _WriteRecorderConfigs(
        output_dir, carriers, scan_tuners, terrestrial_tuners, satellite_tuners, formatter_kwargs
    )

    # ネイティブスキャンと CATV 再送信の同一放送種別 (地上波/BS/CS) チャンネルを併用すると、レコーダー設定上どちらも
    # type: GR / type: BS / type: CS になり、チューナー (dvbv5-zap / recisdb) の振り分けを type だけでは区別できないため、
    # 該当する場合は注意を促す (--prefer 未指定時のみ。--prefer 指定時は重複エントリが自動で isDisabled/disabled になる)
    overlap_sources: set[str] = set()
    if terrestrial_scanned is True:
        overlap_sources.add('Terrestrial')
    if satellite_scanned is True:
        overlap_sources.update({'BS', 'CS'})
    if prefer is None and any(
        ts_info.retransmission_source in overlap_sources
        for carrier in carriers
        for ts_info in carrier.transport_streams
    ):
        print(
            '[yellow]Both native and CATV-retransmitted channels of the same broadcast type (GR/BS/CS) were found. '
            'They share the same channel type in the recorder configs, so Mirakurun/mirakc cannot distinguish '
            'which tuner (dvbv5-zap / recisdb) to use. Pass --prefer catv or --prefer native to auto-adjust '
            '(the non-preferred duplicate entries are marked as disabled), or disable one of them manually.[/yellow]'
        )

    receivable_carrier_count = len([carrier for carrier in carriers if carrier.carrier_type.value != 'Empty'])
    print(Rule(characters='=', style=Style(color='#E33157')))
    print(f'Finished in {time.time() - scan_start_time:.2f} seconds.')
    print(f'Scanned {len(carriers)} / {len(target_channels)} channel(s). ({receivable_carrier_count} receivable)')
    if terrestrial_scanned is True:
        print(f'Scanned terrestrial: {len(tr_ts_infos)} TS')
    if satellite_scanned is True:
        print(f'Scanned satellite: BS={len(bs_ts_infos)} TS / CS={len(cs_ts_infos)} TS')
    print(Rule(characters='=', style=Style(color='#E33157')))


if __name__ == '__main__':
    app()

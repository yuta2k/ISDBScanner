#!/usr/bin/env python3

# isdb-catv-capture: 複数チューナー搭載機 (Digital Devices Max M4 など) で、複数の CATV 物理チャンネルを
# 同時に収録する CLI。主目的は 8K 放送 (JLabs SPEC-034 相当のマルチキャリア分散伝送) を構成する
# 3 キャリアを同時収録し、後段の合成 (再多重化) 処理に使えるデータを揃えること
#
# 使用例:
#   isdb-catv-capture --channels CATV_C32,CATV_C33,CATV_C34 --time 30 --output-dir ./captured/
#   isdb-catv-capture --channels CATV_C32,CATV_C33,CATV_C34 --adapters 0,1,2

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import typer
from rich import print
from rich.rule import Rule
from rich.style import Style
from rich.table import Table

from isdb_scanner.catv.constants import CATV_FREQUENCY_TABLE, CarrierType
from isdb_scanner.catv.mmt import (
    TLV_PACKET_TYPE_COMPRESSED_IP,
    ExtractTLVStream,
    MMTAnalyzer,
    _IterTLVPackets,
    _ParseCompressedIPPacket,
)
from isdb_scanner.catv.tsmf import TSMFDemultiplexer
from isdb_scanner.catv.tuner import CATVTuner
from isdb_scanner.tuner import TunerOpeningError, TunerOutputError, TunerTuningError


app = typer.Typer()

# MPT から映像アセットの packet_id を解決できなかった場合のフォールバック値
# (8K マルチキャリア分散伝送の主映像アセットで実データ上使われていることを確認済みの MMTP packet_id。
#  ARIB STD-B60 上正式に予約された値ではなく事業者側の割当次第で変わり得るため、
#  原則は MPT の hev1 アセットから解決した packet_id を使い、これはあくまで最後の砦とする)
MMTP_VIDEO_PACKET_ID = 0xF100

# MPT で映像 (HEVC) アセットを示す asset_type の FourCC
_VIDEO_ASSET_TYPE = 'hev1'


@dataclass
class CaptureResult:
    """1 チャンネル分の収録結果"""

    physical_channel: str
    adapter_number: int
    frontend_number: int = 0
    output_path: Path | None = None
    byte_size: int = 0
    carrier_type: CarrierType | None = None
    sequence_range: tuple[int, int] | None = None  # TLV キャリアの MMTP packet_sequence_number の (最小, 最大)
    error: str | None = None


def _ResolveVideoPacketIDs(tlv_stream: bytes, truncated_packets: list[bytes]) -> set[int]:
    """
    MPT (MMT Package Table) を解析し、映像 (hev1) アセットに割り当てられている MMTP packet_id の集合を解決する
    MPT から1つも解決できなかった場合のみ、経験則によるフォールバック値 (MMTP_VIDEO_PACKET_ID) を使う
    (映像アセットの packet_id は事業者側の割当次第で変わり得るため、MPT を唯一の正としつつ、
     8K キャリアで MPT が全く取得できなかった場合でも従来通りの判定を続けられるようにしている)
    """

    mmt_info = MMTAnalyzer().analyze(tlv_stream, truncated_packets)
    packet_ids = {
        asset.packet_id
        for service in mmt_info.services
        for asset in service.assets
        if asset.asset_type == _VIDEO_ASSET_TYPE and asset.packet_id is not None
    }
    return packet_ids if len(packet_ids) > 0 else {MMTP_VIDEO_PACKET_ID}


def _ExtractMMTPSequenceNumbers(ts_stream: bytes, packet_ids: set[int] | None = None) -> set[int]:
    """
    TLV キャリアの受信データから、対象 packet_id の MMTP パケットの packet_sequence_number 集合を取り出す
    複数キャリアで同時収録した場合、この値の範囲がキャリア間で重複していれば、同じ時間帯に受信できたデータであり
    8K 合成 (マルチキャリア分散伝送の再結合) に使える可能性が高いと判断できる
    (packet_sequence_number は単調増加するため、時間軸の代わりに使えるという考え方に基づく)

    Args:
        ts_stream (bytes): TLV キャリアの受信データ (188 バイト境界に整列済みの TS ストリーム)
        packet_ids (set[int] | None, optional): 対象の MMTP packet_id の集合。
            省略時は MPT から映像 (hev1) アセットの packet_id を解決する (解決できなければ MMTP_VIDEO_PACKET_ID)。

    Returns:
        set[int]: 見つかった packet_sequence_number の集合 (TLV データが無い/対象の packet_id が無ければ空集合)
    """

    truncated_packets: list[bytes] = []
    tlv_stream = ExtractTLVStream(ts_stream, truncated_packets)
    if packet_ids is None:
        packet_ids = _ResolveVideoPacketIDs(tlv_stream, truncated_packets)

    sequence_numbers: set[int] = set()
    for packet_type, payload in _IterTLVPackets(tlv_stream):
        if packet_type != TLV_PACKET_TYPE_COMPRESSED_IP:
            continue
        parsed = _ParseCompressedIPPacket(payload)
        if parsed is None:
            continue
        _, mmtp_packet_id, mmtp_bytes = parsed
        if mmtp_packet_id not in packet_ids or len(mmtp_bytes) < 12:
            continue
        # packet_sequence_number は MMTP 固定ヘッダのオフセット 8-11 (mmt.py の _ExtractSignallingMessageParts と同じ位置)
        sequence_numbers.add(int.from_bytes(mmtp_bytes[8:12], byteorder='big'))

    return sequence_numbers


def _SequenceRangesOverlap(ranges: list[tuple[int, int]]) -> bool:
    """
    複数の (最小, 最大) の packet_sequence_number 区間が、すべて重なり合っているかどうかを判定する
    区間が1つ以下の場合は判定不能なため True (重複しているとみなす) を返す

    Args:
        ranges (list[tuple[int, int]]): 各キャリアの (最小, 最大) packet_sequence_number のリスト

    Returns:
        bool: すべての区間が重なり合っていれば True
    """

    if len(ranges) < 2:
        return True
    highest_low = max(low for low, _ in ranges)
    lowest_high = min(high for _, high in ranges)
    return highest_low <= lowest_high


def _AssignTuners(channels: list[str], adapters: list[int] | None) -> list[tuple[str, int, int]]:
    """
    収録対象チャンネルにチューナー (DVB アダプタ/フロントエンド番号) を1台ずつ割り当てる
    --adapters が明示的に指定されていればその番号をそのまま使い (フロントエンドは 0 固定)、省略時は検出済みの
    CATV 対応チューナーをデバイスパスの若い順に自動割当する。チャンネル数がチューナー数を超える場合は ValueError を送出する

    Args:
        channels (list[str]): 収録対象の物理チャンネル名のリスト (順序を保つ)
        adapters (list[int] | None): 明示的に指定された DVB アダプタ番号のリスト (省略時は自動検出する)

    Returns:
        list[tuple[str, int, int]]: (物理チャンネル名, アダプタ番号, フロントエンド番号) のリスト (channels と同じ順序)

    Raises:
        ValueError: チャンネル数がチューナー (アダプタ) 数を超えている場合、または --adapters に重複がある場合
    """

    if adapters is not None:
        if len(set(adapters)) != len(adapters):
            raise ValueError(
                f'--adapters に重複した DVB アダプタ番号が指定されています ({",".join(str(a) for a in adapters)})。'
                '同一のチューナーで複数チャンネルを同時に収録することはできません。'
            )
        available_tuners = [(adapter, 0) for adapter in adapters]
    else:
        # 自動検出時は frontend0 以外の CATV 対応フロントエンドも正しく割り当てる (adapter 番号だけに縮約しない)
        available_tuners = [(tuner.adapter_number, tuner.frontend_number) for tuner in CATVTuner.getAvailableCATVTuners()]

    if len(channels) > len(available_tuners):
        raise ValueError(
            f'収録対象チャンネル数 ({len(channels)}) が利用可能なチューナー数 ({len(available_tuners)}) を超えています。'
        )

    return [
        (channel, adapter_number, frontend_number)
        for channel, (adapter_number, frontend_number) in zip(channels, available_tuners[: len(channels)], strict=False)
    ]


def _CaptureChannel(
    adapter_number: int,
    frontend_number: int,
    physical_channel: str,
    recording_time: float,
    output_path: Path,
    start_barrier: threading.Barrier,
    output_dvbv5_zap_log: bool,
) -> CaptureResult:
    """
    1 チャンネル分の収録を行うワーカー関数 (ThreadPoolExecutor から各チャンネルにつき1スレッドで並列実行される)
    全チャンネルの収録開始タイミングをできるだけ揃えるため、実際の選局 (tune) 呼び出し直前に start_barrier で同期する
    """

    tuner = CATVTuner(adapter_number, frontend_number, output_recisdb_log=output_dvbv5_zap_log)
    result = CaptureResult(
        physical_channel=physical_channel, adapter_number=adapter_number, frontend_number=frontend_number
    )

    # 全ワーカースレッドがここに到達するまで待機し、dvbv5-zap の起動 (延いては収録開始) タイミングを揃える
    start_barrier.wait()

    try:
        ts_stream = tuner.tune(physical_channel, recording_time=recording_time)
    except (TunerOpeningError, TunerTuningError, TunerOutputError) as ex:
        result.error = str(ex)
        return result

    output_path.write_bytes(ts_stream)
    result.output_path = output_path
    result.byte_size = len(ts_stream)
    result.carrier_type = TSMFDemultiplexer.detect_carrier_type(ts_stream)

    if result.carrier_type == CarrierType.TLV:
        sequence_numbers = _ExtractMMTPSequenceNumbers(bytes(ts_stream))
        if len(sequence_numbers) > 0:
            result.sequence_range = (min(sequence_numbers), max(sequence_numbers))

    return result


@app.command(
    help='isdb-catv-capture: Simultaneously captures multiple Japanese CATV transmodulation physical channels '
    'using multiple tuners (ex: for 8K multi-carrier distributed transmission).'
)
def main(
    channels: str = typer.Option(
        ...,
        '--channels',
        help='Comma-separated list of physical channels to capture simultaneously (ex: "CATV_C32,CATV_C33,CATV_C34").',
    ),
    recording_time: float = typer.Option(30.0, '--time', help='Recording time (seconds) for each channel.'),
    output_dir: Path = typer.Option(Path('captured/'), '--output-dir', help='Output directory for the captured TS files.'),
    adapters: str | None = typer.Option(
        None,
        '--adapters',
        help='Comma-separated list of DVB adapter numbers to use, one per channel (ex: "0,1,2"). '
        'Defaults to auto-assigning from detected CATV-capable tuners.',
    ),
    output_dvbv5_zap_log: bool = typer.Option(False, help='Output dvbv5-zap log to stderr.'),
):
    print(
        Rule(
            title='ISDBScanner CATV Parallel Capture',
            characters='=',
            style=Style(color='#E33157'),
            align='center',
        )
    )

    target_channels = [channel.strip() for channel in channels.split(',') if channel.strip() != '']
    if len(target_channels) == 0:
        print('[red]No channel specified.[/red]')
        raise typer.Exit(code=1)
    unknown_channels = [channel for channel in target_channels if channel not in CATV_FREQUENCY_TABLE]
    if len(unknown_channels) > 0:
        print(f'[red]Unknown physical channel(s): {", ".join(unknown_channels)}[/red]')
        raise typer.Exit(code=1)

    adapter_numbers: list[int] | None = None
    if adapters is not None:
        try:
            adapter_numbers = [int(adapter.strip()) for adapter in adapters.split(',') if adapter.strip() != '']
        except ValueError as ex:
            print('[red]--adapters must be a comma-separated list of integers.[/red]')
            raise typer.Exit(code=1) from ex

    try:
        assignments = _AssignTuners(target_channels, adapter_numbers)
    except ValueError as ex:
        print(f'[red]{ex}[/red]')
        raise typer.Exit(code=1) from ex

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f'Capturing {len(assignments)} channel(s) simultaneously for {recording_time:.1f} seconds:')
    for physical_channel, adapter_number, frontend_number in assignments:
        print(f'  [green]{physical_channel}[/green] -> adapter{adapter_number}/frontend{frontend_number}')
    print(Rule(characters='-', style=Style(color='#E33157')))

    # 全ワーカースレッドの選局開始タイミングを揃えるためのバリア (全チャンネル分のスレッドが揃うまで待機する)
    start_barrier = threading.Barrier(len(assignments))
    capture_start_time = time.time()
    with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
        futures = [
            executor.submit(
                _CaptureChannel,
                adapter_number,
                frontend_number,
                physical_channel,
                recording_time,
                output_dir / f'{physical_channel}.ts',
                start_barrier,
                output_dvbv5_zap_log,
            )
            for physical_channel, adapter_number, frontend_number in assignments
        ]
        results = [future.result() for future in futures]

    print(f'Finished in {time.time() - capture_start_time:.2f} seconds.')

    table = Table(title='Capture Results')
    table.add_column('Channel')
    table.add_column('Adapter')
    table.add_column('Size')
    table.add_column('Carrier Type')
    table.add_column('MMTP Sequence Range')
    for result in results:
        if result.error is not None:
            table.add_row(result.physical_channel, str(result.adapter_number), '-', '-', f'[red]{result.error}[/red]')
            continue
        size_str = f'{result.byte_size / (1024 * 1024):.2f} MB'
        carrier_type_str = result.carrier_type.value if result.carrier_type is not None else 'Unknown'
        sequence_str = f'{result.sequence_range[0]}..{result.sequence_range[1]}' if result.sequence_range is not None else '-'
        table.add_row(result.physical_channel, str(result.adapter_number), size_str, carrier_type_str, sequence_str)
    print(table)

    # 映像 MMTP パケットのシーケンス番号が取得できなかった TLV キャリアは重複判定の対象外になるため、その旨を警告しておく
    # (黙って判定をスキップすると「収録は成功した」ようにしか見えず、使えないデータに気付けない)
    tlv_results_without_range = [
        result
        for result in results
        if result.error is None and result.carrier_type == CarrierType.TLV and result.sequence_range is None
    ]
    for result in tlv_results_without_range:
        print(
            f'[yellow]{result.physical_channel}: 映像 MMTP パケットのシーケンス番号を取得できなかったため、'
            '時間範囲の重複判定から除外します。収録データが 8K 合成に使えるかは別途確認してください。[/yellow]'
        )

    # 複数の TLV キャリアで packet_sequence_number が取得できていれば、時間範囲が重複しているか (=8K 合成可能なデータか) を判定する
    tlv_results_with_range = [
        result for result in results if result.carrier_type == CarrierType.TLV and result.sequence_range is not None
    ]
    if len(tlv_results_with_range) >= 2:
        ranges = [result.sequence_range for result in tlv_results_with_range if result.sequence_range is not None]
        if _SequenceRangesOverlap(ranges):
            print(
                f'[green]{len(tlv_results_with_range)} 個の TLV キャリア間で MMTP シーケンス範囲が重複しています。'
                '8K 合成 (マルチキャリア分散伝送の再結合) に使える可能性が高いです。[/green]'
            )
        else:
            print(
                f'[yellow]{len(tlv_results_with_range)} 個の TLV キャリア間で MMTP シーケンス範囲が重複していません。'
                '同時収録に失敗している (収録タイミングがずれた) 可能性があります。[/yellow]'
            )
    elif len(tlv_results_with_range) == 1:
        print('[yellow]TLV キャリアが1つしか収録できなかったため、時間範囲の重複判定はスキップします。[/yellow]')

    error_count = len([result for result in results if result.error is not None])
    print(Rule(characters='=', style=Style(color='#E33157')))
    if error_count > 0:
        print(f'[red]{error_count} / {len(results)} channel(s) failed to capture.[/red]')
        raise typer.Exit(code=1)


if __name__ == '__main__':
    app()

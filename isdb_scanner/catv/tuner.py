from __future__ import annotations

import atexit
import ctypes
import fcntl
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO, ClassVar

from isdb_scanner.catv.constants import (
    CATV_FREQUENCY_TABLE,
    BuildDvbv5ConfEntryLines,
    CarrierType,
    CATVSignalStats,
)
from isdb_scanner.catv.tsmf import TSMFDemultiplexer
from isdb_scanner.tuner import TunerOpeningError, TunerOutputError, TunerTuningError


# TS パケットサイズ (バイト)
TS_PACKET_SIZE = 188

# dvbv5-zap の標準出力を読み込む際の1回あたりの読み込みサイズ
## システムコール回数を減らすため、TS パケットサイズの倍数でまとめて読み込む
READ_CHUNK_SIZE = TS_PACKET_SIZE * 1024

# 受信できていれば（チューナーオープン時間を含めても）最低でもこの程度のデータは得られるはず
## それ未満の場合は選局に失敗しているとみなす (既存 ISDBTuner.tune() の閾値と同じ)
MIN_OUTPUT_SIZE = 100 * 1024

# 収録中のキャリア種別判定 (ShouldExtendRecordingForTLV()) に使う、受信データ先頭からの最大バイト数
## キャリア種別の判定はストリーム全体を走査しなくても十分な精度で行えるため、判定コストを一定に抑えるために先頭部分のみを使う
## (CATV トランスモジュレーション 1 キャリアのビットレートは約 29Mbps = 約 3.6MB/秒 なので、16MB あれば先頭 4 秒強に相当する)
CARRIER_TYPE_DETECTION_MAX_SIZE = 16 * 1024 * 1024

# DVBv5 ioctl (FE_GET_PROPERTY / FE_GET_INFO) で使う定数
# 値は Linux Kernel の include/uapi/linux/dvb/frontend.h (enum fe_property / enum fecap_scale_params) の定義そのもの
FE_GET_PROPERTY = 0x80106F53
FE_GET_INFO = 0x80A86F3D
DTV_ENUM_DELSYS = 44
DTV_STAT_SIGNAL_STRENGTH = 62
DTV_STAT_CNR = 63
DTV_STAT_PRE_ERROR_BIT_COUNT = 64
DTV_STAT_PRE_TOTAL_BIT_COUNT = 65

FE_SCALE_NOT_AVAILABLE = 0
FE_SCALE_DECIBEL = 1
FE_SCALE_RELATIVE = 2
FE_SCALE_COUNTER = 3


# DVBv5 ioctl 用の ctypes 構造体定義
# 既存 isdb_scanner/tuner.py の ISDBTuner.getDVBDeviceInfoFromDVBv5() 内の定義と同じもの (既存コードは無改変のため、
# ここでモジュールレベルに一度だけ定義し、CATVTuner 内の各 ioctl 呼び出しで共用する)
class DtvProperty(ctypes.Structure):
    class _u(ctypes.Union):
        class _buffer(ctypes.Structure):
            _fields_ = [
                ('data', ctypes.c_uint8 * 32),
                ('len', ctypes.c_uint32),
                ('reserved1', ctypes.c_uint32 * 3),
                ('reserved2', ctypes.c_void_p),
            ]

        _fields_ = [('data', ctypes.c_uint32), ('buffer', _buffer)]

    _fields_ = [
        ('cmd', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 3),
        ('u', _u),
        ('result', ctypes.c_int),
    ]


class DtvProperties(ctypes.Structure):
    _fields_ = [('num', ctypes.c_uint32), ('props', ctypes.POINTER(DtvProperty))]


class DvbFrontendInfo(ctypes.Structure):
    _fields_ = [
        ('name', ctypes.c_char * 128),
        ('type', ctypes.c_uint),
        ('frequency_min', ctypes.c_uint32),
        ('frequency_max', ctypes.c_uint32),
        ('frequency_stepsize', ctypes.c_uint32),
        ('frequency_tolerance', ctypes.c_uint32),
        ('symbol_rate_min', ctypes.c_uint32),
        ('symbol_rate_max', ctypes.c_uint32),
        ('symbol_rate_tolerance', ctypes.c_uint32),
        ('notifier_delay', ctypes.c_uint32),
        ('caps', ctypes.c_uint),
    ]

# 選局 (ロック) 検知から信号品質統計を取得するまでの待機時間 (秒)
# 実機 (Digital Devices Max M4) で確認したところ、ロック直後は DTV_STAT_PRE_ERROR_BIT_COUNT / DTV_STAT_PRE_TOTAL_BIT_COUNT
# (誤り率算出用の積算カウンタ) がまだ 0/0 のままで誤り率を算出できなかったため、この秒数だけ待ってから取得する
SIGNAL_STATS_COLLECTION_DELAY = 1.0


def ShouldExtendRecordingForTLV(
    ts_stream: bytes | bytearray,
    recording_time: float,
    tlv_recording_time: float | None,
) -> bool:
    """
    収録途中の受信データからキャリア種別を判定し、収録時間を tlv_recording_time 秒まで延長すべきかどうかを返す

    TLV (4K/8K MMT) キャリアでは、MH-SDT に放送網内の全ストリーム分のサービス情報がセクション分割で載っており、
    既定の 10 秒程度の収録では全セクションが揃わずスキャン結果 (sdt_services) が回によってばらつく
    (実測では 20 秒収録で毎回全サービスが揃う)。一方 TSMF/SingleTS キャリアは 10 秒で十分なため、
    TLV キャリアと判定できた場合のみ収録時間を延長する

    Args:
        ts_stream (bytes | bytearray): 収録途中の受信データ (先頭から recording_time 秒分)
        recording_time (float): 通常の録画時間 (秒)
        tlv_recording_time (float | None): TLV キャリアと判定された場合の合計録画時間 (秒).
            None または recording_time 以下の場合は延長しない

    Returns:
        bool: 収録時間を延長すべき (= TLV キャリアと判定された) 場合は True
    """

    # 延長後の秒数が通常の録画時間以下なら延長する意味がないため、キャリア種別の判定自体を省略する
    if tlv_recording_time is None or tlv_recording_time <= recording_time:
        return False

    # 判定は先頭 CARRIER_TYPE_DETECTION_MAX_SIZE バイトまでで打ち切る (収録中に実行するため、判定コストを一定に抑える)
    detection_target = ts_stream[:CARRIER_TYPE_DETECTION_MAX_SIZE]
    return TSMFDemultiplexer.detect_carrier_type(detection_target) == CarrierType.TLV


class CATVTuner:
    """
    CATV トランスモジュレーション (J.83 Annex C 64QAM) 対応チューナーデバイスを dvbv5-zap 経由で操作するクラス
    recisdb は ISDB-C (CATV トランスモジュレーション) の選局に対応していないため、代わりに dvbv5-zap を利用する

    dvbv5-zap の挙動は実機 (Digital Devices Max M4) で確認済み:
    - `-o -` (標準出力への出力) は `-o <file>` と同一のバイト列を得られる (実測で完全に同一サイズ)
    - `-P` (--all-pids) を指定しないと TSMF ヘッダ (PID 0x002F) や TLV (PID 0x002D) を含む一部の PID が欠落する
    - `-t <seconds>` は「選局(ロック)のタイムアウト」と「レコーディング時間」を兼ねる唯一のオプションで、
      ロックに成功した場合はプロセス起動からおおよそ指定秒数だけ TS を出力して自動終了し (exit code 0)、
      ロックに失敗した場合は指定秒数でリトライを諦めて "frontend doesn't lock" を stderr に出力して終了する (exit code 1)
    - ロックできない場合、`-t` を指定しないと際限なくリトライを続ける。SIGINT を送ると同様に
      "frontend doesn't lock" を出力して正常に (数百ms 程度で) 終了することを確認済み

    上記の特性上、dvbv5-zap 自身の `-t` だけでは「選局タイムアウト」と「録画時間」を独立に制御できないため、
    このクラスでは既存 ISDBTuner.tune() と同様に別スレッドで標準出力を監視し、
    データが1バイトも届かないまま tune_timeout 秒経過した時点で SIGINT を送って選局失敗と判断する
    (ロックさえ成功すれば、あとは dvbv5-zap 自身の `-t recording_time` 経過で自動終了するのを待てばよい)

    また `-t` は起動後に延長できない一方、収録中の SIGINT はいつでも受け付けて収録を終了できるため、
    tune() の tlv_recording_time (TLV キャリアのみ収録時間を延長するオプション) は
    「最初から延長後の秒数で起動しておき、TLV キャリアでなければ recording_time 経過時点で SIGINT を送って打ち切る」
    という形で実現している (詳細は tune() 内のコメント参照)
    """

    # 全チャンネル分の dvbv5 conf ファイルは同一プロセス内で使い回すため、初回生成時にクラス変数へキャッシュする
    _conf_file_path: ClassVar[Path | None] = None
    # 複数チューナーでの並列スキャン/収録では初回の tune() が同時に走るため、conf ファイルの生成をロックで直列化する
    # (ロックがないと各ワーカーが個別に tempfile を生成してしまい、キャッシュを共有できずに余分な一時ファイルが残る)
    _conf_file_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, adapter_number: int, frontend_number: int = 0, output_recisdb_log: bool = False) -> None:
        """
        CATVTuner を初期化する

        Args:
            adapter_number (int): 使用する DVB アダプタ番号 (/dev/dvb/adapterN の N)
            frontend_number (int, optional): 使用するフロントエンド番号 (frontendN の N). Defaults to 0.
            output_recisdb_log (bool, optional): dvbv5-zap のログを出力するかどうか. Defaults to False.
        """

        self.adapter_number = adapter_number
        self.frontend_number = frontend_number
        self.output_recisdb_log = output_recisdb_log
        self.device_path = Path(f'/dev/dvb/adapter{adapter_number}/frontend{frontend_number}')

        # 直近の tune() 呼び出しで取得できた信号品質統計 (collect_signal_stats=False の場合、または取得に失敗した場合は None)
        self.last_signal_stats: CATVSignalStats | None = None

        # 直近の tune() 呼び出しで、TLV キャリアと判定されて収録時間が tlv_recording_time まで延長されたかどうか
        self.last_recording_extended: bool = False

    @property
    def name(self) -> str:
        """デバイス名 (取得できなければ device_path をそのまま使う)"""

        device_name = CATVTuner._getFrontendDeviceName(self.device_path)
        if device_name is None:
            return f'DVB-C Tuner ({self.device_path})'
        return f'{device_name} ({self.device_path})'

    def tune(
        self,
        physical_channel: str,
        recording_time: float = 10.0,
        tune_timeout: float = 10.0,
        collect_signal_stats: bool = False,
        tlv_recording_time: float | None = None,
    ) -> bytearray:
        """
        チューナーデバイスから指定された CATV 物理チャンネルを受信し、選局/受信できなかった場合は例外を送出する

        Args:
            physical_channel (str): 選局する CATV 物理チャンネル (ex: "CATV_15", "CATV_C36")
            recording_time (float, optional): 録画時間 (秒). Defaults to 10.0.
            tune_timeout (float, optional): 選局 (フロントエンドのロック) のタイムアウト時間 (秒). Defaults to 10.0.
            collect_signal_stats (bool, optional): 選局 (ロック) 成功後、信号品質統計を取得し self.last_signal_stats に
                格納するかどうか (取得に失敗しても例外は送出されず、last_signal_stats が None のままになる). Defaults to False.
            tlv_recording_time (float | None, optional): TLV (4K/8K MMT) キャリアと判定された場合に限り、収録を続行して
                合計でこの秒数だけ録画する (None または recording_time 以下の場合は延長しない). Defaults to None.

        Returns:
            bytearray: 受信した TS ストリーム (1 キャリア分)

        Raises:
            TunerOpeningError: dvbv5-zap コマンドが見つからない場合
            TunerTuningError: チャンネルを選局できなかった場合
            TunerOutputError: 受信したデータが小さすぎる場合
        """

        if physical_channel not in CATV_FREQUENCY_TABLE:
            raise TunerTuningError(f'Unknown physical channel: {physical_channel}')

        self.last_signal_stats = None
        self.last_recording_extended = False
        conf_file_path = CATVTuner._getConfFilePath()

        # TLV キャリアのみ収録時間を延長する場合、dvbv5-zap には最初から延長後の秒数を `-t` に指定して起動する
        # dvbv5-zap は起動後に `-t` の秒数を延長できない (逆に、選局中の SIGINT はいつでも受け付けて収録を終了できる) ため、
        # 「長めに録っておき、TLV キャリアでないと判明した時点で SIGINT で打ち切る」形で「TLV のときだけ延長」を実現している
        # dvbv5-zap 側では `-t` のタイムアウトも SIGINT も同じシグナルハンドラ経由で「収録の終了」として扱われるため、
        # TLV でないキャリアでの挙動は `-t recording_time` を指定した従来と実質同じになる
        # (選局しっぱなしのプロセスを再起動せずそのまま録り続けられるので、延長時も選局のやり直しやストリームの不連続が発生しない)
        if tlv_recording_time is not None and tlv_recording_time > recording_time:
            is_extendable = True
            total_recording_time = tlv_recording_time
        else:
            is_extendable = False
            total_recording_time = recording_time

        command = [
            'dvbv5-zap',
            '-c', str(conf_file_path),
            '-a', str(self.adapter_number),
            '-f', str(self.frontend_number),
            '-P',  # 全 PID を出力 (TSMF ヘッダ 0x002F・TLV 0x002D を含む全 mux を取得するため必須)
            '-o', '-',  # 標準出力に TS を出力
            '-t', str(total_recording_time),
            physical_channel,
        ]  # fmt: skip

        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as ex:
            raise TunerOpeningError('dvbv5-zap command not found.') from ex

        # それぞれ別スレッドで標準出力と標準エラー出力の読み込みを開始
        stdout: bytearray = bytearray()
        is_stdout_arrived = False

        def stdout_thread_func():
            nonlocal stdout, is_stdout_arrived
            assert process.stdout is not None
            while True:
                data = process.stdout.read(READ_CHUNK_SIZE)
                if len(data) > 0:
                    is_stdout_arrived = True
                if len(data) == 0:
                    break
                stdout.extend(data)

        stderr: bytearray = bytearray()

        def stderr_thread_func():
            nonlocal stderr
            assert process.stderr is not None
            while True:
                # read1() はブロックせずに読み出せる分だけ返すため、ログのリアルタイム出力を保ちつつ
                # 1バイトずつの read() よりもシステムコール回数とバッファ再確保を減らせる
                data = process.stderr.read1(4096)
                if len(data) == 0:
                    break
                stderr.extend(data)
                if self.output_recisdb_log is True:
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()

        stdout_thread = threading.Thread(target=stdout_thread_func)
        stderr_thread = threading.Thread(target=stderr_thread_func)
        stdout_thread.start()
        stderr_thread.start()

        # プロセスが終了するか、選局 (フロントエンドのロック) のタイムアウト秒数に達するまで待機
        # 標準出力から TS ストリームが出力されるようになったらタイムアウト秒数のカウントを停止する
        # (ロックさえ成功すれば、あとは dvbv5-zap 自身の `-t recording_time` 経過で自動終了するのを待てばよい)
        # collect_signal_stats=True の場合、ロックを検知してから少し経った時点 (dvbv5-zap がまだフロントエンドを開いて
        # 受信を続けている間) に信号品質統計を1回だけ取得する。dvbv5-zap 自身は書き込み用にフロントエンドをオープンして
        # いるはずだが、読み取り専用 (O_RDONLY) でのオープンは競合しないため、別プロセス視点の ioctl 呼び出しとして
        # 安全に実行できる (実機 (Digital Devices Max M4) で確認済み)
        # ロック直後だと DTV_STAT_PRE_ERROR_BIT_COUNT / DTV_STAT_PRE_TOTAL_BIT_COUNT (誤り率算出用の積算カウンタ) が
        # まだ 0/0 のままで誤り率を算出できないことを実機で確認したため、ロック検知から SIGNAL_STATS_COLLECTION_DELAY
        # 秒だけ待ってから取得することで、カウンタがある程度積算された値を得られるようにしている
        # is_extendable=True の場合、標準出力へ TS ストリームが届き始めてから recording_time 秒が経過した時点で、
        # そこまでに受信できたデータからキャリア種別を判定する。TLV キャリアならそのまま dvbv5-zap を走らせ続けて
        # tlv_recording_time 秒まで収録を延長し、そうでなければ SIGINT を送って従来と同じ recording_time 秒で収録を打ち切る
        # (dvbv5-zap の `-t` は「選局 (ロック) 完了後から N 秒」であり、プロセス起動〜ロックまでの時間 (実機で約 1 秒) を
        #  含まないことを実機で確認済み。打ち切り判定の起点もそれに合わせて「最初にデータが届いた時刻」にすることで、
        #  TLV でないキャリアの収録内容が `-t recording_time` を指定した従来とほぼ同一になる)
        tune_timeout_count = 0.0
        signal_stats_collected = False
        lock_detected_time: float | None = None
        signal_stats_collection_delay = min(SIGNAL_STATS_COLLECTION_DELAY, recording_time / 2)
        carrier_type_decided = not is_extendable
        is_interrupted_by_self = False
        stdout_arrived_time: float | None = None
        while process.poll() is None and tune_timeout_count < tune_timeout:
            time.sleep(0.01)
            if is_stdout_arrived is False:
                tune_timeout_count += 0.01
                continue
            if stdout_arrived_time is None:
                stdout_arrived_time = time.time()
            if collect_signal_stats is True and signal_stats_collected is False:
                if lock_detected_time is None:
                    lock_detected_time = time.time()
                if time.time() - lock_detected_time >= signal_stats_collection_delay:
                    self.last_signal_stats = self.getSignalStats()
                    signal_stats_collected = True
            if carrier_type_decided is False and time.time() - stdout_arrived_time >= recording_time:
                carrier_type_decided = True
                # stdout は標準出力読み込みスレッドが随時追記しているため、判定にはスライスしたコピーを使う
                # (bytearray のスライスは GIL 下でアトミックにコピーされるため、追記と競合しても安全)
                if ShouldExtendRecordingForTLV(stdout[:CARRIER_TYPE_DETECTION_MAX_SIZE], recording_time, tlv_recording_time):
                    self.last_recording_extended = True
                else:
                    process.send_signal(signal.SIGINT)
                    is_interrupted_by_self = True

        # この時点でプロセスが終了しておらず、標準出力からまだ TS ストリームを受け取っていない場合
        # 選局(ロック)がタイムアウトしたとみなし、プロセスに SIGINT を送信して終了させる
        # 実機確認済み: dvbv5-zap は SIGINT を受け取ると "frontend doesn't lock" を出力して正常終了する
        if process.poll() is None and is_stdout_arrived is False:
            process.send_signal(signal.SIGINT)
            # ここでプロセスが完全に終了するまで待機しないと、続けて別のチャンネルを選局する際にデバイス使用中エラーが発生してしまう
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            raise TunerTuningError('Channel selection timed out.')

        # プロセスと標準出力・標準エラー出力スレッドの終了を待機
        process.wait()
        stdout_thread.join()
        stderr_thread.join()

        # この時点でリターンコードが 0 でなければ選局または受信に失敗している
        # (ただし TLV キャリアでないと判定して自ら SIGINT で収録を打ち切った場合は、収録自体は所定の秒数だけ成功しているため、
        #  dvbv5-zap 側の終了コードが 0 以外になっていても失敗とはみなさない)
        if is_interrupted_by_self is False and process.returncode != 0:
            stderr_text = stderr.decode('utf-8', errors='replace').strip()
            stderr_lines = [line.strip() for line in stderr_text.splitlines() if line.strip() != '']
            error_message = stderr_lines[-1] if len(stderr_lines) > 0 else 'Channel selection failed due to an unknown error.'
            raise TunerTuningError(error_message)

        # 受信していれば（チューナーオープン時間を含めても）100KB 以上のデータが得られるはず
        # それ未満の場合は選局に失敗している
        if len(stdout) < MIN_OUTPUT_SIZE:
            raise TunerOutputError('The tuner output is too small.')

        return stdout

    def getSignalStats(self) -> CATVSignalStats | None:
        """
        フロントエンドデバイスから DVBv5 ioctl (FE_GET_PROPERTY / DTV_STAT_*) 経由で信号品質統計を取得する

        dvbv5-zap がフロントエンドを掴んで選局中であっても、読み取り専用 (O_RDONLY) でオープンすれば競合せず統計を
        取得できる (Linux の DVB フロントエンドドライバは、チューニング操作を伴わない読み取り専用オープンを
        複数プロセスから許容する設計になっている)。そのためこのメソッドは self.device_path を都度別途オープンするだけで、
        dvbv5-zap プロセス側には一切干渉しない

        フロントエンド自体をオープンできない場合は None を返す。個々の統計項目 (信号強度・CNR・誤り率) は、
        フロントエンドドライバが対応していない・スケールが FE_SCALE_NOT_AVAILABLE のいずれかの場合に None のままになる
        (CATVSignalStats 参照)

        Returns:
            CATVSignalStats | None: 取得できた信号品質統計 (フロントエンドを開けなかった場合のみ None)
        """

        try:
            with open(self.device_path, 'rb', buffering=0) as fe_fd:
                signal_strength_raw = CATVTuner._readDtvStat(fe_fd, DTV_STAT_SIGNAL_STRENGTH)
                cnr_raw = CATVTuner._readDtvStat(fe_fd, DTV_STAT_CNR)
                pre_error_bits_raw = CATVTuner._readDtvStat(fe_fd, DTV_STAT_PRE_ERROR_BIT_COUNT)
                pre_total_bits_raw = CATVTuner._readDtvStat(fe_fd, DTV_STAT_PRE_TOTAL_BIT_COUNT)
        except OSError:
            return None

        stats = CATVSignalStats()

        if signal_strength_raw is not None:
            parsed = CATVTuner._parseDtvFeStats(signal_strength_raw)
            if len(parsed) > 0:
                scale, value = parsed[0]
                if scale == FE_SCALE_DECIBEL:
                    stats.signal_strength_dbm = value / 1000.0
                elif scale == FE_SCALE_RELATIVE:
                    stats.signal_strength_percent = value / 65535.0 * 100.0

        if cnr_raw is not None:
            parsed = CATVTuner._parseDtvFeStats(cnr_raw)
            if len(parsed) > 0:
                scale, value = parsed[0]
                if scale == FE_SCALE_DECIBEL:
                    stats.cnr_db = value / 1000.0
                elif scale == FE_SCALE_RELATIVE:
                    stats.cnr_percent = value / 65535.0 * 100.0

        # 訂正前ビット誤り率 (PRE_ERROR_BIT_COUNT / PRE_TOTAL_BIT_COUNT) は、双方とも FE_SCALE_COUNTER (積算カウンタ) の
        # 場合のみ意味を持つ (どちらかが FE_SCALE_NOT_AVAILABLE ならドライバが対応していないので算出しない)
        pre_error_bits: int | None = None
        pre_total_bits: int | None = None
        if pre_error_bits_raw is not None:
            parsed = CATVTuner._parseDtvFeStats(pre_error_bits_raw)
            if len(parsed) > 0 and parsed[0][0] == FE_SCALE_COUNTER:
                pre_error_bits = parsed[0][1]
        if pre_total_bits_raw is not None:
            parsed = CATVTuner._parseDtvFeStats(pre_total_bits_raw)
            if len(parsed) > 0 and parsed[0][0] == FE_SCALE_COUNTER:
                pre_total_bits = parsed[0][1]
        if pre_error_bits is not None and pre_total_bits is not None and pre_total_bits > 0:
            stats.error_rate = pre_error_bits / pre_total_bits

        return stats

    @staticmethod
    def _readDtvStat(fe_fd: BinaryIO, cmd: int) -> bytes | None:
        """
        オープン済みのフロントエンド fd に対して ioctl (FE_GET_PROPERTY) を1プロパティ (num=1) だけ実行し、
        u (union) 部分の生バイト列 (先頭32バイト) を返す (ioctl 自体が失敗した場合は None)

        モジュールレベルの DtvProperty/DtvProperties は Linux Kernel の struct dtv_property の union を模したもので、
        DTV_STAT_* コマンドに対しては本来 dtv_fe_stats (len: uint8 + stat[4] の各9バイト = 37バイト) が書き込まれるが、
        union は同一メモリ領域を指すため、'buffer.data' (32バイト) を通じてそのまま生バイト列として読み出せる
        (グローバル値である stat[0] は先頭10バイトに収まるため、32バイトあれば必要十分)。
        パース処理自体は _parseDtvFeStats() に分離しており、実機のない環境でも合成バイト列でテストできる
        """

        try:
            dtv_prop = DtvProperty(cmd=cmd)
            dtv_props = DtvProperties(num=1, props=ctypes.pointer(dtv_prop))
            if fcntl.ioctl(fe_fd, FE_GET_PROPERTY, dtv_props) == -1:
                return None
        except OSError:
            return None

        return bytes(dtv_prop.u.buffer.data)

    @staticmethod
    def _parseDtvFeStats(raw: bytes) -> list[tuple[int, int]]:
        """
        ioctl (FE_GET_PROPERTY / DTV_STAT_*) で得られた dtv_fe_stats 相当の生バイト列をパースし、
        (scale, value) のタプルのリストを返す (先頭要素が stat[0]=グローバル値。ISDB 等の階層伝送に対応した
        ドライバでは以降の要素が各階層の値になるが、CATV (DVB-C) では階層分けは行われないため、
        呼び出し側は基本的に先頭要素だけを使えばよい)

        構造体レイアウト (Linux Kernel: include/uapi/linux/dvb/frontend.h, いずれも __attribute__((packed))):
            struct dtv_stats { __u8 scale; union { __u64 uvalue; __s64 svalue; }; }   -- 9 バイト
            struct dtv_fe_stats { __u8 len; struct dtv_stats stat[4]; }               -- 1 + 9*4 = 37 バイト

        scale が FE_SCALE_DECIBEL (=1) の場合は符号付き64bit (svalue) として、それ以外
        (FE_SCALE_RELATIVE/FE_SCALE_COUNTER) の場合は符号なし64bit (uvalue) として値を解釈する
        (FE_SCALE_NOT_AVAILABLE の要素もそのまま (0, 0) 相当で返すので、呼び出し側で scale を見て判断すること)

        Args:
            raw (bytes): dtv_fe_stats 構造体の生バイト列 (先頭1バイトが len、以降が stat[] の並び)

        Returns:
            list[tuple[int, int]]: (scale, value) のタプルのリスト (len 分。パースできる長さがなければ空リスト)
        """

        if len(raw) < 1:
            return []

        length = min(raw[0], 4)
        results: list[tuple[int, int]] = []
        offset = 1
        for _ in range(length):
            chunk = raw[offset : offset + 9]
            if len(chunk) < 9:
                break
            scale = chunk[0]
            if scale == FE_SCALE_DECIBEL:
                value = struct.unpack('<q', chunk[1:9])[0]
            else:
                value = struct.unpack('<Q', chunk[1:9])[0]
            results.append((scale, value))
            offset += 9

        return results

    @staticmethod
    def _getConfFilePath() -> Path:
        """
        全 CATV チャンネル分の dvbv5 形式 conf ファイルを tempfile に生成し、以降は同一プロセス内で使い回す
        DELIVERY_SYSTEM / SYMBOL_RATE / MODULATION は ISDB-C 向けの固定値を利用する (constants.py 参照)
        """

        # 並列スキャン/収録では複数ワーカーがほぼ同時に初回生成へ到達するため、ロックで直列化して二重生成を防ぐ
        # (ダブルチェックロッキング: ロック取得前後の両方でキャッシュの有無を確認し、生成は最初の1回だけに絞る)
        if CATVTuner._conf_file_path is not None and CATVTuner._conf_file_path.is_file():
            return CATVTuner._conf_file_path

        with CATVTuner._conf_file_lock:
            if CATVTuner._conf_file_path is not None and CATVTuner._conf_file_path.is_file():
                return CATVTuner._conf_file_path

            lines: list[str] = []
            for channel_name, frequency in CATV_FREQUENCY_TABLE.items():
                lines.extend(BuildDvbv5ConfEntryLines(channel_name, frequency))

            fd, path_str = tempfile.mkstemp(prefix='isdb_scanner_catv_', suffix='.conf')
            conf_file_path = Path(path_str)
            with open(fd, mode='w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')

            # conf ファイルは dvbv5-zap の起動に必要なだけで、プロセス終了後は不要になるため、
            # 一時ディレクトリに溜まり続けないようプロセス終了時に削除する
            # (Ctrl+C による中断でも KeyboardInterrupt がインタプリタの終了処理まで巻き戻るため削除される)
            atexit.register(CATVTuner._removeConfFile, conf_file_path)

            CATVTuner._conf_file_path = conf_file_path
            return conf_file_path

    @staticmethod
    def _removeConfFile(conf_file_path: Path) -> None:
        """
        _getConfFilePath() が生成した dvbv5 形式 conf ファイルを削除する (プロセス終了時に atexit から呼ばれる)
        既に削除されている場合や、権限などの理由で削除できなかった場合は何もしない
        (一時ファイルの後始末が失敗しただけでプロセスの終了処理を妨げないようにするため)
        """

        try:
            conf_file_path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def getAvailableCATVTuners(output_recisdb_log: bool = False) -> list[CATVTuner]:
        """
        利用可能な CATV (DVB-C ANNEX_A) 対応チューナーのリストを取得する
        既存 isdb_scanner/tuner.py の ISDBTuner.getDVBDeviceInfoFromDVBv5() と同じ DVBv5 ioctl 判定パターンを流用し、
        DTV_ENUM_DELSYS に SYS_DVBC_ANNEX_A を含むデバイスのみを返す (既存 tuner.py 自体は無改変)
        ホスト環境など /dev/dvb が存在しない場合は空リストを返すだけで、例外は発生しない

        Args:
            output_recisdb_log (bool, optional): 返す CATVTuner の dvbv5-zap ログを出力するかどうか. Defaults to False.

        Returns:
            list[CATVTuner]: 利用可能な CATV 対応チューナーのリスト
        """

        tuners: list[CATVTuner] = []
        # /dev/dvb が存在しない環境では glob() は空のイテレータを返すだけで例外は発生しない
        for device_path in sorted(Path('/dev/dvb').glob('adapter*/frontend*')):
            search = re.search(r'/dev/dvb/adapter(\d+)/frontend(\d+)', str(device_path))
            if search is None:
                continue
            if device_path.exists() is False or device_path.is_char_device() is False:
                continue
            if CATVTuner._isDVBCAnnexASupported(device_path) is False:
                continue

            adapter_number = int(search.group(1))
            frontend_number = int(search.group(2))
            tuners.append(CATVTuner(adapter_number, frontend_number, output_recisdb_log=output_recisdb_log))

        return tuners

    @staticmethod
    def _isDVBCAnnexASupported(device_path: Path) -> bool:
        """
        指定されたフロントエンドデバイスが DVB-C ANNEX_A (SYS_DVBC_ANNEX_A) の選局に対応しているかどうかを、
        DVBv5 ioctl API (DTV_ENUM_DELSYS) から判定する
        以下の ioctl 呼び出し・構造体定義は既存 isdb_scanner/tuner.py の ISDBTuner.getDVBDeviceInfoFromDVBv5() を
        コピーして CATV 向けに簡略化したもの (既存コードは無改変のため、ここで重複して定義している)
        """

        delivery_systems = CATVTuner._enumDeliverySystems(device_path)
        if delivery_systems is None:
            return False

        # SYS_DVBC_ANNEX_A の値 (linux/dvb/frontend.h の fe_delivery_system 列挙型を参照)
        SYS_DVBC_ANNEX_A = 1
        return SYS_DVBC_ANNEX_A in delivery_systems

    @staticmethod
    def _getFrontendDeviceName(device_path: Path) -> str | None:
        """DVBv5 ioctl API (FE_GET_INFO) からフロントエンドデバイスの名前を取得する (取得できなければ None)"""

        try:
            with open(device_path, 'rb', buffering=0) as fe_fd:
                fe_info = DvbFrontendInfo()
                if fcntl.ioctl(fe_fd, FE_GET_INFO, fe_info) == -1:
                    return None
        except OSError:
            return None

        return fe_info.name.decode('utf-8', errors='replace')

    @staticmethod
    def _enumDeliverySystems(device_path: Path) -> list[int] | None:
        """DVBv5 ioctl API (DTV_ENUM_DELSYS) からフロントエンドデバイスが対応する配信システムの一覧を取得する (取得できなければ None)"""

        try:
            with open(device_path, 'rb', buffering=0) as fe_fd:
                dtv_prop = DtvProperty(cmd=DTV_ENUM_DELSYS)
                dtv_props = DtvProperties(num=1, props=ctypes.pointer(dtv_prop))
                if fcntl.ioctl(fe_fd, FE_GET_PROPERTY, dtv_props) == -1:
                    return None
        except OSError:
            return None

        return [dtv_prop.u.buffer.data[i] for i in range(dtv_prop.u.buffer.len)]

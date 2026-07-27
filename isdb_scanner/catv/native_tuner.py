#!/usr/bin/env python3

# isdb-catv-scanner のネイティブ (ISDB-T/ISDB-S) スキャンで使う、ハング対策付きの ISDBTuner サブクラス
#
# upstream の ISDBTuner.tune() (isdb_scanner/tuner.py 326-451 行付近) の選局タイムアウトは
# 「最初のデータが標準出力に届くまで」しかカウントされないため、V4L-DVB チューナーで
# 「僅かにデータが届いた後にストリームが停止する」チャンネルに当たると、recisdb が --time の録画秒数に
# 永久に到達せず process.wait() で無限に待ち続ける (実機の DD Max M4 + 地上波空きチャンネルで発生を確認)。
# また getSignalLevelMean() (同 504-532 行付近) も、recisdb checksignal が何も出力しないまま終了しない場合に
# stdout.read(1) で永久にブロックする。
#
# upstream (tsukumijima/ISDBScanner) との diff を最小化するため、tuner.py は改変せず、
# このモジュールで該当メソッドを「複製 + ウォールクロック上限の追加」でオーバーライドする
# (upstream 追従時はこのファイルと複製元の差分に注意すること)

import re
import signal
import subprocess
import sys
import threading
import time

from isdb_scanner.tuner import ISDBTuner, TunerOpeningError, TunerOutputError, TunerTuningError


class RobustISDBTuner(ISDBTuner):
    """
    tune() / getSignalLevelMean() に全体のウォールクロック上限 (ウォッチドッグ) を追加した ISDBTuner
    上限超過時は recisdb を終了させ、ハングせずに TunerTuningError として次のチューナー/チャンネルへ進めるようにする
    """

    # ストリーム停止 (ストール) 判定の余裕時間 (秒)
    ## チューナーのオープン/クローズに掛かる時間 (docstring いわく最大 7 秒程度) を吸収するための余裕で、
    ## tune() の全体上限は tune_timeout + recording_time + この値になる
    STALL_TIMEOUT_MARGIN = 10.0

    # getSignalLevelMean() の全体上限 (秒)
    ## 信号レベルは 1 秒間隔程度で出力されるため、5 サンプル + オープン時間でこの程度あれば十分
    SIGNAL_LEVEL_TIMEOUT = 15.0

    def tune(self, physical_channel_recisdb: str, recording_time: float = 10.0, tune_timeout: float = 7.0) -> bytearray:
        """
        チューナーデバイスから指定された物理チャンネルを受信し、選局/受信できなかった場合は例外を送出する
        upstream の ISDBTuner.tune() の複製に、全体のウォールクロック上限 (ストリーム停止検出) を追加したもの

        Args:
            physical_channel_recisdb (str): recisdb が受け付けるフォーマットの物理チャンネル (ex: "T13", "BS23_3", "CS04")
            recording_time (float, optional): 録画時間 (秒). Defaults to 10.0.
            tune_timeout (float, optional): 選局 (チューナーオープン) のタイムアウト時間 (秒). Defaults to 7.0.

        Returns:
            bytearray: 受信したデータ

        Raises:
            TunerOpeningError: チューナーをオープンできなかった場合
            TunerTuningError: チャンネルを選局できなかった場合 (ストリーム停止によるタイムアウトを含む)
            TunerOutputError: 受信したデータが小さすぎる場合
        """

        self._last_tuner_opening_failed = False

        # BS・CS チャンネルのみ、設定に応じて LNB 電源を出力
        command = [
            'recisdb',
            'tune',
            '--device',
            str(self._device_path),
            '--channel',
            physical_channel_recisdb,
            '--time',
            str(recording_time),
        ]
        if self.lnb is not None and (physical_channel_recisdb.startswith('BS') or physical_channel_recisdb.startswith('CS')):
            command.extend(['--lnb', str(self.lnb)])
        command.extend(['-'])  # 受信データを標準出力に出力

        # recisdb (チューナープロセス) を起動
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # それぞれ別スレッドで標準出力と標準エラー出力の読み込みを開始
        stdout: bytearray = bytearray()
        is_stdout_arrived = False

        def stdout_thread_func():
            nonlocal stdout, is_stdout_arrived
            assert process.stdout is not None
            while True:
                data = process.stdout.read(188)
                is_stdout_arrived = True
                if len(data) == 0:
                    break
                stdout.extend(data)

        stderr: bytes = b''

        def stderr_thread_func():
            nonlocal stderr
            assert process.stderr is not None
            while True:
                data = process.stderr.read(1)
                if len(data) == 0:
                    break
                stderr += data
                if self.output_recisdb_log is True:
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()

        stdout_thread = threading.Thread(target=stdout_thread_func)
        stderr_thread = threading.Thread(target=stderr_thread_func)
        stdout_thread.start()
        stderr_thread.start()

        # プロセスが終了するまで待機しつつ、2 種類のタイムアウトを監視する
        ## 1. 選局タイムアウト (upstream と同一): 標準出力から TS ストリームが届くまでの間だけカウントする
        ## 2. 全体タイムアウト (本サブクラスでの追加): 最初のデータが届いた後でも、録画が
        ##    tune_timeout + recording_time + STALL_TIMEOUT_MARGIN 以内に終わらない場合はストリームが
        ##    停止しているとみなす (recisdb の --time は受信できた秒数ベースのため、ストリームが止まると永久に終了しない)
        start_time = time.monotonic()
        total_timeout = tune_timeout + recording_time + self.STALL_TIMEOUT_MARGIN
        tune_timeout_count = 0.0
        is_stalled = False
        while process.poll() is None:
            time.sleep(0.01)
            if is_stdout_arrived is False:
                tune_timeout_count += 0.01
                if tune_timeout_count >= tune_timeout:
                    break
            if time.monotonic() - start_time >= total_timeout:
                is_stalled = True
                break

        # この時点でプロセスが終了しておらず、標準出力からまだ TS ストリームを受け取っていない場合
        # プロセスを終了 (Ctrl+C を送信) し、タイムアウトエラーを送出する
        if process.poll() is None and is_stdout_arrived is False:
            process.send_signal(signal.SIGINT)
            # ここでプロセスが完全に終了するまで待機しないと、続けて別のチャンネルを選局する際にデバイス使用中エラーが発生してしまう
            self.__waitOrKill(process)
            raise TunerTuningError('Channel selection timed out.')

        # 全体タイムアウトを超過した場合もプロセスを終了し、選局失敗として扱う
        # (SIGINT で終了しない場合に備え、一定時間で SIGKILL にエスカレーションする)
        if is_stalled is True and process.poll() is None:
            process.send_signal(signal.SIGINT)
            self.__waitOrKill(process)
            raise TunerTuningError(
                f'Tuner output stalled (no stream progress within {total_timeout:.0f} seconds).'
            )

        # プロセスと標準エラー出力スレッドの終了を待機
        process.wait()
        stderr_thread.join()

        # この時点でリターンコードが 0 でなければ選局または受信に失敗している
        if process.returncode != 0:
            # エラメッセージを正規表現で取得
            result = re.search(r'ERROR:\s+(.+)', stderr.decode('utf-8'))
            if result is not None:
                error_message = result.group(1)
            else:
                error_message = 'Channel selection failed due to an unknown error.'

            # チューナーオープン時のエラー
            if error_message in [
                'The tuner device does not exist.',
                'The tuner device is already in use.',
                'The tuner device is busy.',
                'The tuner device does not support the ioctl system call.',
            ] or error_message.startswith('Cannot open the device.'):
                self._last_tuner_opening_failed = True
                raise TunerOpeningError(error_message)

            # それ以外は選局/受信時のエラーと判断
            raise TunerTuningError(error_message)

        # 受信していれば（チューナーオープン時間を含めても）100KB 以上のデータが得られるはず
        # それ未満の場合は選局に失敗している
        if len(stdout) < 100 * 1024:
            raise TunerOutputError('The tuner output is too small.')

        # 受信したデータを返す
        return stdout

    def getSignalLevelMean(self, physical_channel_recisdb: str) -> float | None:
        """
        チューナーデバイスから指定された物理チャンネルを受信し、5回の平均信号レベルを返す
        upstream の ISDBTuner.getSignalLevelMean() の複製に、全体のウォールクロック上限を追加したもの
        (recisdb checksignal が信号レベルを出力しないまま終了もしない場合、upstream 実装は stdout.read(1) で永久にブロックする)

        Args:
            physical_channel_recisdb (str): recisdb が受け付けるフォーマットの物理チャンネル (ex: "T13", "BS23_3", "CS04")

        Returns:
            float | None: 平均信号レベル (選局失敗・タイムアウト時は None)
        """

        # 信号レベルを取得するイテレータを取得
        process, iterator = self.getSignalLevel(physical_channel_recisdb)

        # 5回分の信号レベルの取得は別スレッドで行い、SIGNAL_LEVEL_TIMEOUT 秒以内に終わらなければ打ち切る
        ## イテレータ (パイプの read) 自体は外部から中断できないため、プロセスを終了させることで間接的に読み込みを解除する
        signal_levels: list[float] = []

        def collect_thread_func():
            for _ in range(5):
                try:
                    signal_levels.append(next(iterator))
                except (RuntimeError, StopIteration):
                    return

        collect_thread = threading.Thread(target=collect_thread_func, daemon=True)
        collect_thread.start()
        collect_thread.join(timeout=self.SIGNAL_LEVEL_TIMEOUT)

        # プロセスを終了 (タイムアウトの有無に関わらず、プロセスは常にここで終了させる)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        self.__waitOrKill(process)

        # タイムアウトした・十分なサンプルが取れなかった場合は選局失敗扱い
        if len(signal_levels) < 5:
            return None

        # 平均信号レベルを返す
        return sum(signal_levels) / len(signal_levels)

    @staticmethod
    def __waitOrKill(process: subprocess.Popen, timeout: float = 10.0) -> None:
        """プロセスの終了を待機し、タイムアウトした場合は SIGKILL で強制終了する (ゾンビ化とデバイス占有を防ぐ)"""

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def AsRobustISDBTuners(tuners: list[ISDBTuner]) -> list[RobustISDBTuner]:
    """
    ISDBTuner.getAvailableISDBTTuners() / getAvailableISDBSTuners() が返した ISDBTuner インスタンス群を、
    同一デバイスパス・同一設定のまま RobustISDBTuner として作り直す
    (upstream の列挙ロジックを改変せずにハング対策付き tune() を使うためのラッパー)
    """

    return [
        RobustISDBTuner(tuner.device_path, lnb=tuner.lnb, output_recisdb_log=tuner.output_recisdb_log)
        for tuner in tuners
    ]

from __future__ import annotations

import ctypes
import ctypes.util
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from isdb_scanner.catv.constants import ARIB_CAS_SYSTEM_ID, C_CAS_SYSTEM_IDS, GetCASystemName


# 日本の CATV 環境では、地デジ/BS/CS の再送信を見るための B-CAS カードと、CATV 自主放送・専門チャンネルを見るための
# C-CAS カードを 1 台のホストに同時に挿して使い分ける必要がある
# このモジュールは、接続されている PC/SC カードリーダーを列挙し、それぞれに挿さっているカードが申告する CA_system_id を
# 取得して「どのリーダーにどの種類のカードが挿さっているか」を機械的に把握するためのもの
#
# 【スコープ境界】
# このモジュールが行うのはカードの「識別」だけで、ECM/EMM の処理・ワークキーの取得・デスクランブルは一切行わない
# 送信する APDU は ARIB STD-B25 第1部が規定する Initial Setting Conditions コマンド (90 30 00 00 00) 1 種類のみで、
# これは pcsc_scan 相当の情報取得であり、libaribb25 がカード接続時に必ず行う手順と同じもの
# (本プロジェクトはスクランブルされた放送のデコードを行わない方針のため、この境界を超える実装を追加してはならない)


# ARIB STD-B25 第1部 第2章「Initial Setting Conditions コマンド」の APDU
# libaribb25 の aribb25/b_cas_card.c の INITIAL_SETTING_CONDITIONS_CMD と同一のバイト列
# (libaribb25 では A-CAS 用に 4 バイト目を 0x02 に差し替える分岐があるが、CA_system_id の取得には不要なため用いない)
INITIAL_SETTING_CONDITIONS_COMMAND = bytes([0x90, 0x30, 0x00, 0x00, 0x00])

# Initial Setting Conditions コマンドのレスポンス内の各フィールドのバイトオフセット
# 推測ではなく、libaribb25 (tsukumijima 版) の aribb25/b_cas_card.c connect_card() の
# ENABLE_ARIB_STD_B1 が未定義のとき (= 通常の ARIB STD-B25 ビルド。B1 ビルドはスカパー！プレミアム用でレイアウトが異なる) の
# 実装に一致させている:
#   n = load_be_uint16(p+4); if(n != 0x2100){ return 0; }  -> リターンコード (0x2100 でなければ不正な応答とみなす)
#   prv->stat.card_status  = load_be_uint16(p+2);
#   prv->stat.ca_system_id = load_be_uint16(p+6);
#   prv->stat.bcas_card_id = load_be_uint48(p+8);          -> カード ID (6 バイト = 48bit ビッグエンディアン)
#   memcpy(prv->stat.system_key, p+16, 32); memcpy(prv->stat.init_cbc, p+48, 8);
#     -> system_key / init_cbc はデスクランブル (MULTI2 復号) 専用のフィールドのため、本実装では読み出さない
RESPONSE_OFFSET_CARD_STATUS = 2
RESPONSE_OFFSET_RETURN_CODE = 4
RESPONSE_OFFSET_CA_SYSTEM_ID = 6
RESPONSE_OFFSET_CARD_ID = 8
RESPONSE_CARD_ID_LENGTH = 6

# 本実装が読み出すフィールド (カード ID の末尾) までに最低限必要なレスポンス長
# libaribb25 は system_key / init_cbc まで読むため 57 バイト以上を要求するが、本実装はカード ID までしか読まないため、
# 復号用フィールドを持たないカードでも識別だけは行えるよう、必要最小限の長さで判定する
RESPONSE_MIN_LENGTH = RESPONSE_OFFSET_CARD_ID + RESPONSE_CARD_ID_LENGTH

# Initial Setting Conditions コマンドの正常終了を示すリターンコード (libaribb25 の 0x2100 判定と同じ)
RESPONSE_RETURN_CODE_OK = 0x2100

# SCardTransmit() のレスポンス受信バッファサイズ
# libaribb25 の B_CAS_BUFFER_MAX (4096) と同じ値
RECEIVE_BUFFER_SIZE = 4096


# pcsc-lite の定数 (値は pcsc-lite の PCSC/pcsclite.h の定義そのもの)
SCARD_S_SUCCESS = 0x00000000
SCARD_SCOPE_SYSTEM = 0x0002
SCARD_SHARE_SHARED = 0x0002
SCARD_PROTOCOL_T1 = 0x0002
SCARD_LEAVE_CARD = 0x0000
MAX_ATR_SIZE = 33

# pcsc-lite のエラーコード (PCSC/pcsclite.h) のうち、このモジュールで名前付きで扱うもの
SCARD_E_INVALID_HANDLE = 0x80100003
SCARD_E_UNKNOWN_READER = 0x80100009
SCARD_E_SHARING_VIOLATION = 0x8010000B
SCARD_E_NO_SMARTCARD = 0x8010000C
SCARD_E_PROTO_MISMATCH = 0x8010000F
SCARD_F_COMM_ERROR = 0x80100013
SCARD_E_NO_SERVICE = 0x8010001D
SCARD_E_NO_READERS_AVAILABLE = 0x8010002E
SCARD_W_UNRESPONSIVE_CARD = 0x80100066
SCARD_W_UNPOWERED_CARD = 0x80100067
SCARD_W_RESET_CARD = 0x80100068
SCARD_W_REMOVED_CARD = 0x80100069

# エラーコードから日本語のエラーメッセージへの対応表
PCSC_ERROR_MESSAGES: dict[int, str] = {
    SCARD_E_INVALID_HANDLE: 'PC/SC のハンドルが無効です',
    SCARD_E_UNKNOWN_READER: '指定されたカードリーダーが見つかりません',
    SCARD_E_SHARING_VIOLATION: '他のプロセスがカードを排他モードで使用中です',
    SCARD_E_NO_SMARTCARD: 'カードリーダーにカードが挿入されていません',
    SCARD_E_PROTO_MISMATCH: 'カードが T=1 プロトコルに対応していません',
    SCARD_F_COMM_ERROR: 'カードリーダーとの通信エラーが発生しました',
    SCARD_E_NO_SERVICE: 'PC/SC サービス (pcscd) が起動していません',
    SCARD_E_NO_READERS_AVAILABLE: 'カードリーダーが 1 台も接続されていません',
    SCARD_W_UNRESPONSIVE_CARD: 'カードが応答しません (接触不良の可能性があります)',
    SCARD_W_UNPOWERED_CARD: 'カードに電源が供給されていません',
    SCARD_W_RESET_CARD: 'カードがリセットされました',
    SCARD_W_REMOVED_CARD: 'カードが抜かれています',
}

# pcscd に接続できないときに表示する対処のヒント
PCSCD_HINT_MESSAGE = (
    'pcscd (PC/SC スマートカードデーモン) が起動しているか、pcsc-lite がインストールされているかを確認してください '
    '(systemctl status pcscd / sudo systemctl start pcscd)'
)


class CardType(StrEnum):
    """検出されたカードの種別 (CA_system_id から判定)"""

    BCAS = 'B-CAS'  # CA_system_id 0x0005 (ARIB 限定受信方式。A-CAS も同じ ID なので後段でコンテナ種別と併せて解釈する)
    CCAS = 'C-CAS'  # CA_system_id 0x0003 / 0x0004 / 0x0006
    OTHER = 'Other'  # 上記以外の既知/未知の CA_system_id
    UNKNOWN = 'Unknown'  # カード無し・応答なし・解析不能


class DetectedCard(BaseModel):
    """PC/SC カードリーダー 1 台ぶんのカード検出結果"""

    # fmt: off
    reader_name: str                        # SCardListReaders() が返すリーダー名 (recisdb の --card にそのまま渡せる文字列)
    ca_system_id: int | None = None         # カードが申告した CA_system_id (取得できなかった場合は None)
    ca_system_name: str = 'Unknown'         # 限定受信方式の表示名 (CA_SYSTEM_ID_NAMES 由来)
    card_type: CardType = CardType.UNKNOWN  # カード種別 (CA_system_id から判定)
    card_id: str | None = None              # カード ID (取得できた場合。ハイフン区切りの表示用文字列)
    atr: str | None = None                  # ATR の16進表記 (取得できた場合)
    error: str | None = None                # このリーダーで取得に失敗した理由 (日本語)
    # fmt: on


class PCSCLiteInterface(Protocol):
    """
    DetectCASCardsWithLibrary() が必要とする libpcsclite の薄いラッパーのインターフェイス
    テストでは実 libpcsclite の代わりにこのインターフェイスを満たすフェイクを渡すことで、実カード無しで検証できる
    """

    def EstablishContext(self) -> tuple[int, int]:
        """PC/SC のコンテキストを確立する (戻り値: (結果コード, コンテキストハンドル))"""
        ...

    def ListReaders(self, context: int) -> tuple[int, list[str]]:
        """接続されているカードリーダー名を列挙する (戻り値: (結果コード, リーダー名のリスト))"""
        ...

    def Connect(self, context: int, reader_name: str) -> tuple[int, int]:
        """指定されたカードリーダーのカードに接続する (戻り値: (結果コード, カードハンドル))"""
        ...

    def GetATR(self, card: int) -> str | None:
        """カードの ATR を16進表記で取得する (取得できなければ None)"""
        ...

    def Transmit(self, card: int, command: bytes) -> tuple[int, bytes]:
        """カードに APDU を送信し、レスポンスを受け取る (戻り値: (結果コード, レスポンス))"""
        ...

    def Disconnect(self, card: int) -> None:
        """カードとの接続を切断する (カードの状態は変更しない)"""
        ...

    def ReleaseContext(self, context: int) -> None:
        """PC/SC のコンテキストを解放する"""
        ...


class SCardIORequest(ctypes.Structure):
    """
    pcsc-lite の SCARD_IO_REQUEST 構造体 (PCSC/pcsclite.h)
    Linux では DWORD = unsigned long のため、ctypes.c_ulong に対応する
    """

    _fields_ = [
        ('dwProtocol', ctypes.c_ulong),
        ('cbPciLength', ctypes.c_ulong),
    ]


def NormalizeResultCode(result: int) -> int:
    """
    SCard* API の戻り値 (LONG) を符号なし 32bit に正規化する
    pcsc-lite の Linux 版では LONG = long (64bit) のため、環境によって符号やビット幅が揺れるのを吸収する
    """

    return result & 0xFFFFFFFF


def GetPCSCErrorMessage(result: int) -> str:
    """pcsc-lite の結果コードに対応する日本語のエラーメッセージを取得する (未知のコードは 16 進表記にフォールバック)"""

    normalized_result = NormalizeResultCode(result)
    return PCSC_ERROR_MESSAGES.get(normalized_result, f'PC/SC エラー (0x{normalized_result:08X})')


def GetCardTypeFromCASystemId(ca_system_id: int | None) -> CardType:
    """
    CA_system_id からカード種別を判定する
    CA_system_id の割当は ARIB STD-B10 付録M 表M-1 に基づく (isdb_scanner/catv/constants.py 参照)
    """

    if ca_system_id is None:
        return CardType.UNKNOWN
    # 0x0005 (ARIB 限定受信方式) は B-CAS カードと A-CAS カードで共通のため、ここでは B-CAS として扱う
    # (A-CAS との区別は CA_system_id だけでは不可能で、TS キャリアか TLV/MMT キャリアかのコンテナ種別と併せて解釈する必要がある)
    if ca_system_id == ARIB_CAS_SYSTEM_ID:
        return CardType.BCAS
    if ca_system_id in C_CAS_SYSTEM_IDS:
        return CardType.CCAS
    return CardType.OTHER


def FormatCardId(card_id: int) -> str:
    """
    Initial Setting Conditions レスポンスから得た 48bit のカード ID を、ハイフン区切りの表示用文字列に整形する
    カードの券面に印字されている 20 桁のカード番号は、このカード ID に加えて別コマンド (90 32 00 00 00: カード情報取得) で
    得られるチェックコードが必要なため復元できない。本モジュールは Initial Setting Conditions コマンド 1 種類しか送信しない
    スコープ境界を守るため、48bit の生の値を 4 桁ずつ 16 進表記で区切った文字列とする
    """

    hex_string = f'{card_id:012X}'
    return '-'.join(hex_string[i : i + 4] for i in range(0, len(hex_string), 4))


def FormatATR(atr: bytes) -> str:
    """ATR のバイト列を pcsc_scan と同じスペース区切りの16進表記に整形する"""

    return ' '.join(f'{byte:02X}' for byte in atr)


def ParseInitialSettingConditionsResponse(response: bytes) -> tuple[int | None, str | None]:
    """
    Initial Setting Conditions コマンド (90 30 00 00 00) のレスポンスから CA_system_id とカード ID を取り出す

    バイトオフセットは libaribb25 の aribb25/b_cas_card.c connect_card() の実装に一致させている
    (詳細はモジュール冒頭の RESPONSE_OFFSET_* 定数のコメントを参照)
    レスポンスが想定より短い場合やリターンコードが 0x2100 でない場合は、誤った値を読み出さないよう安全側に倒して None を返す

    Args:
        response (bytes): カードから受信したレスポンス

    Returns:
        tuple[int | None, str | None]: (CA_system_id, 表示用のカード ID) / 解析できなかった場合はいずれも None
    """

    # カード ID の末尾まで含まれていない短すぎるレスポンス (エラーステータスのみを返すカードなど) は解析しない
    if len(response) < RESPONSE_MIN_LENGTH:
        return None, None

    # リターンコードが 0x2100 でないレスポンスは、Initial Setting Conditions のレスポンスではないとみなす
    # (libaribb25 も同じ判定で接続を失敗扱いにしている)
    return_code = int.from_bytes(response[RESPONSE_OFFSET_RETURN_CODE : RESPONSE_OFFSET_RETURN_CODE + 2], 'big')
    if return_code != RESPONSE_RETURN_CODE_OK:
        return None, None

    ca_system_id = int.from_bytes(response[RESPONSE_OFFSET_CA_SYSTEM_ID : RESPONSE_OFFSET_CA_SYSTEM_ID + 2], 'big')
    card_id = int.from_bytes(response[RESPONSE_OFFSET_CARD_ID : RESPONSE_OFFSET_CARD_ID + RESPONSE_CARD_ID_LENGTH], 'big')
    return ca_system_id, FormatCardId(card_id)


class PCSCLite:
    """
    libpcsclite.so.1 を ctypes 経由で直接呼び出す薄いラッパー
    pyscard を新規依存に追加せずに済ませるため、既存の isdb_scanner/catv/tuner.py の DVBv5 ioctl と同じく ctypes で実装している
    """

    def __init__(self, library: ctypes.CDLL, t1_pci: SCardIORequest) -> None:
        self.library = library
        self.t1_pci = t1_pci
        self._SetupPrototypes()

    @classmethod
    def Load(cls) -> tuple[PCSCLite | None, str | None]:
        """
        libpcsclite.so.1 をロードして PCSCLite を生成する
        pcsc-lite がインストールされていない環境でも例外を投げず、(None, 理由) を返す

        Returns:
            tuple[PCSCLite | None, str | None]: (ラッパー, エラー理由) / 成功時のエラー理由は None
        """

        # libpcsclite.so.1 を優先し、見つからなければ ldconfig 経由でも探す
        library_names = ['libpcsclite.so.1']
        found_library_name = ctypes.util.find_library('pcsclite')
        if found_library_name is not None and found_library_name not in library_names:
            library_names.append(found_library_name)

        library: ctypes.CDLL | None = None
        for library_name in library_names:
            try:
                library = ctypes.CDLL(library_name, use_errno=True)
                break
            except OSError:
                continue
        if library is None:
            return None, f'libpcsclite.so.1 をロードできませんでした。{PCSCD_HINT_MESSAGE}'

        # T=1 プロトコル用の PCI 構造体は libpcsclite がグローバル変数としてエクスポートしている
        try:
            t1_pci = SCardIORequest.in_dll(library, 'g_rgSCardT1Pci')
        except (AttributeError, ValueError):
            return None, 'libpcsclite.so.1 から g_rgSCardT1Pci を取得できませんでした。pcsc-lite のバージョンを確認してください'

        try:
            return cls(library, t1_pci), None
        except AttributeError:
            return None, 'libpcsclite.so.1 に必要な SCard* API が存在しませんでした。pcsc-lite のバージョンを確認してください'

    def _SetupPrototypes(self) -> None:
        """
        使用する SCard* API の引数・戻り値の型を設定する
        pcsc-lite の Linux 版では LONG = long / DWORD = unsigned long のため、それぞれ c_long / c_ulong に対応する
        (SCARDCONTEXT / SCARDHANDLE はいずれも LONG の typedef)
        """

        self.library.SCardEstablishContext.argtypes = [
            ctypes.c_ulong,  # dwScope
            ctypes.c_void_p,  # pvReserved1
            ctypes.c_void_p,  # pvReserved2
            ctypes.POINTER(ctypes.c_long),  # phContext
        ]
        self.library.SCardEstablishContext.restype = ctypes.c_long

        self.library.SCardReleaseContext.argtypes = [ctypes.c_long]
        self.library.SCardReleaseContext.restype = ctypes.c_long

        self.library.SCardListReaders.argtypes = [
            ctypes.c_long,  # hContext
            ctypes.c_char_p,  # mszGroups
            ctypes.c_char_p,  # mszReaders
            ctypes.POINTER(ctypes.c_ulong),  # pcchReaders
        ]
        self.library.SCardListReaders.restype = ctypes.c_long

        self.library.SCardConnect.argtypes = [
            ctypes.c_long,  # hContext
            ctypes.c_char_p,  # szReader
            ctypes.c_ulong,  # dwShareMode
            ctypes.c_ulong,  # dwPreferredProtocols
            ctypes.POINTER(ctypes.c_long),  # phCard
            ctypes.POINTER(ctypes.c_ulong),  # pdwActiveProtocol
        ]
        self.library.SCardConnect.restype = ctypes.c_long

        self.library.SCardStatus.argtypes = [
            ctypes.c_long,  # hCard
            ctypes.c_char_p,  # szReaderName
            ctypes.POINTER(ctypes.c_ulong),  # pcchReaderLen
            ctypes.POINTER(ctypes.c_ulong),  # pdwState
            ctypes.POINTER(ctypes.c_ulong),  # pdwProtocol
            ctypes.POINTER(ctypes.c_ubyte),  # pbAtr
            ctypes.POINTER(ctypes.c_ulong),  # pcbAtrLen
        ]
        self.library.SCardStatus.restype = ctypes.c_long

        self.library.SCardTransmit.argtypes = [
            ctypes.c_long,  # hCard
            ctypes.POINTER(SCardIORequest),  # pioSendPci
            ctypes.POINTER(ctypes.c_ubyte),  # pbSendBuffer
            ctypes.c_ulong,  # cbSendLength
            ctypes.POINTER(SCardIORequest),  # pioRecvPci
            ctypes.POINTER(ctypes.c_ubyte),  # pbRecvBuffer
            ctypes.POINTER(ctypes.c_ulong),  # pcbRecvLength
        ]
        self.library.SCardTransmit.restype = ctypes.c_long

        self.library.SCardDisconnect.argtypes = [ctypes.c_long, ctypes.c_ulong]
        self.library.SCardDisconnect.restype = ctypes.c_long

    def EstablishContext(self) -> tuple[int, int]:
        """PC/SC のコンテキストを確立する (戻り値: (結果コード, コンテキストハンドル))"""

        context = ctypes.c_long(0)
        result = self.library.SCardEstablishContext(SCARD_SCOPE_SYSTEM, None, None, ctypes.byref(context))
        return NormalizeResultCode(result), context.value

    def ReleaseContext(self, context: int) -> None:
        """PC/SC のコンテキストを解放する"""

        self.library.SCardReleaseContext(ctypes.c_long(context))

    def ListReaders(self, context: int) -> tuple[int, list[str]]:
        """
        接続されているカードリーダー名を列挙する
        SCardListReaders() が返すのは NUL 区切り・末尾二重 NUL の multi-string のため、パースしてリストに変換する
        """

        # 1 回目の呼び出しでバッファサイズを取得する
        buffer_length = ctypes.c_ulong(0)
        result = self.library.SCardListReaders(ctypes.c_long(context), None, None, ctypes.byref(buffer_length))
        if NormalizeResultCode(result) != SCARD_S_SUCCESS:
            return NormalizeResultCode(result), []
        if buffer_length.value == 0:
            return SCARD_S_SUCCESS, []

        # 2 回目の呼び出しで実際のリーダー名を取得する
        buffer = ctypes.create_string_buffer(buffer_length.value)
        result = self.library.SCardListReaders(ctypes.c_long(context), None, buffer, ctypes.byref(buffer_length))
        if NormalizeResultCode(result) != SCARD_S_SUCCESS:
            return NormalizeResultCode(result), []
        return SCARD_S_SUCCESS, ParseReaderNames(buffer.raw[: buffer_length.value])

    def Connect(self, context: int, reader_name: str) -> tuple[int, int]:
        """
        指定されたカードリーダーのカードに接続する
        SCARD_SHARE_SHARED で接続することで、録画中の recisdb など他プロセスが同じカードを使っていても排他エラーにならず、
        APDU 1 往復はリーダー単位の mutex で不可分に保護される (録画を妨げないよう SCARD_SHARE_EXCLUSIVE は使わない)
        """

        card = ctypes.c_long(0)
        active_protocol = ctypes.c_ulong(0)
        result = self.library.SCardConnect(
            ctypes.c_long(context),
            reader_name.encode('utf-8'),
            SCARD_SHARE_SHARED,
            SCARD_PROTOCOL_T1,
            ctypes.byref(card),
            ctypes.byref(active_protocol),
        )
        return NormalizeResultCode(result), card.value

    def GetATR(self, card: int) -> str | None:
        """カードの ATR を16進表記で取得する (取得できなければ None)"""

        reader_name_buffer = ctypes.create_string_buffer(256)
        reader_name_length = ctypes.c_ulong(256)
        state = ctypes.c_ulong(0)
        protocol = ctypes.c_ulong(0)
        atr_buffer = (ctypes.c_ubyte * MAX_ATR_SIZE)()
        atr_length = ctypes.c_ulong(MAX_ATR_SIZE)
        result = self.library.SCardStatus(
            ctypes.c_long(card),
            reader_name_buffer,
            ctypes.byref(reader_name_length),
            ctypes.byref(state),
            ctypes.byref(protocol),
            atr_buffer,
            ctypes.byref(atr_length),
        )
        if NormalizeResultCode(result) != SCARD_S_SUCCESS or atr_length.value == 0:
            return None
        return FormatATR(bytes(atr_buffer[: atr_length.value]))

    def Transmit(self, card: int, command: bytes) -> tuple[int, bytes]:
        """カードに APDU を送信し、レスポンスを受け取る (戻り値: (結果コード, レスポンス))"""

        send_buffer = (ctypes.c_ubyte * len(command))(*command)
        receive_buffer = (ctypes.c_ubyte * RECEIVE_BUFFER_SIZE)()
        receive_length = ctypes.c_ulong(RECEIVE_BUFFER_SIZE)
        result = self.library.SCardTransmit(
            ctypes.c_long(card),
            ctypes.byref(self.t1_pci),
            send_buffer,
            len(command),
            None,
            receive_buffer,
            ctypes.byref(receive_length),
        )
        if NormalizeResultCode(result) != SCARD_S_SUCCESS:
            return NormalizeResultCode(result), b''
        return SCARD_S_SUCCESS, bytes(receive_buffer[: receive_length.value])

    def Disconnect(self, card: int) -> None:
        """カードとの接続を切断する (SCARD_LEAVE_CARD のため、カードの電源状態は変更しない)"""

        self.library.SCardDisconnect(ctypes.c_long(card), SCARD_LEAVE_CARD)


def ParseReaderNames(buffer: bytes) -> list[str]:
    """
    SCardListReaders() が返す multi-string (NUL 区切り・末尾二重 NUL) をリーダー名のリストにパースする

    Args:
        buffer (bytes): SCardListReaders() が返したバッファの内容

    Returns:
        list[str]: リーダー名のリスト
    """

    reader_names: list[str] = []
    for raw_reader_name in buffer.split(b'\x00'):
        if len(raw_reader_name) == 0:
            continue
        # リーダー名にはメーカー名がそのまま入るため、非 ASCII が含まれていても落ちないよう replace でデコードする
        reader_names.append(raw_reader_name.decode('utf-8', errors='replace'))
    return reader_names


def DetectCardFromReader(pcsc: PCSCLiteInterface, context: int, reader_name: str) -> DetectedCard:
    """
    カードリーダー 1 台に挿さっているカードの CA_system_id を取得する

    Args:
        pcsc (PCSCLiteInterface): libpcsclite の薄いラッパー
        context (int): PC/SC のコンテキストハンドル
        reader_name (str): 対象のカードリーダー名

    Returns:
        DetectedCard: 検出結果 (失敗した場合も error に理由を格納して返す)
    """

    result, card = pcsc.Connect(context, reader_name)
    if result != SCARD_S_SUCCESS:
        return DetectedCard(reader_name=reader_name, error=f'カードに接続できませんでした ({GetPCSCErrorMessage(result)})')

    try:
        # ATR はカード種別の判定には使わないが、リーダー/カードの識別情報として表示できるよう取得しておく (失敗しても続行する)
        atr = pcsc.GetATR(card)

        # Initial Setting Conditions コマンドを 1 回だけ送信する (これ以外の APDU は送信しない)
        result, response = pcsc.Transmit(card, INITIAL_SETTING_CONDITIONS_COMMAND)
        if result != SCARD_S_SUCCESS:
            return DetectedCard(
                reader_name=reader_name,
                atr=atr,
                error=f'カードとの通信に失敗しました ({GetPCSCErrorMessage(result)})',
            )

        ca_system_id, card_id = ParseInitialSettingConditionsResponse(response)
        if ca_system_id is None:
            return DetectedCard(
                reader_name=reader_name,
                atr=atr,
                error='カードからの応答を解析できませんでした (CAS カード以外のカードが挿入されている可能性があります)',
            )

        return DetectedCard(
            reader_name=reader_name,
            ca_system_id=ca_system_id,
            ca_system_name=GetCASystemName(ca_system_id),
            card_type=GetCardTypeFromCASystemId(ca_system_id),
            card_id=card_id,
            atr=atr,
        )
    finally:
        pcsc.Disconnect(card)


def DetectCASCardsWithLibrary(pcsc: PCSCLiteInterface) -> tuple[list[DetectedCard], str | None]:
    """
    指定された libpcsclite ラッパーを使って、接続されている PC/SC リーダーと各カードの CA_system_id を取得する
    実 libpcsclite に依存せずテストできるよう、DetectCASCards() から libpcsclite のロード処理だけを分離したもの

    Args:
        pcsc (PCSCLiteInterface): libpcsclite の薄いラッパー

    Returns:
        tuple[list[DetectedCard], str | None]: (検出結果, 全体的なエラー理由 or None)
    """

    result, context = pcsc.EstablishContext()
    if result != SCARD_S_SUCCESS:
        return [], f'PC/SC のコンテキストを確立できませんでした ({GetPCSCErrorMessage(result)})。{PCSCD_HINT_MESSAGE}'

    try:
        result, reader_names = pcsc.ListReaders(context)
        # リーダーが 1 台も接続されていない場合、pcsc-lite は SCARD_E_NO_READERS_AVAILABLE を返す
        if result == SCARD_E_NO_READERS_AVAILABLE:
            return [], 'カードリーダーが 1 台も接続されていません。カードリーダーが接続・認識されているか確認してください'
        if result != SCARD_S_SUCCESS:
            return [], f'カードリーダーを列挙できませんでした ({GetPCSCErrorMessage(result)})。{PCSCD_HINT_MESSAGE}'
        if len(reader_names) == 0:
            return [], 'カードリーダーが 1 台も接続されていません。カードリーダーが接続・認識されているか確認してください'

        # 1 台目でエラーになっても後続のリーダーの検出は継続する (部分的な失敗はそのリーダーの error に格納される)
        return [DetectCardFromReader(pcsc, context, reader_name) for reader_name in reader_names], None
    finally:
        pcsc.ReleaseContext(context)


def DetectCASCards() -> tuple[list[DetectedCard], str | None]:
    """
    接続されている PC/SC リーダーを列挙し、各カードの CA_system_id を取得する

    この機能はオプトインの補助機能のため、libpcsclite がロードできない・pcscd が起動していない・リーダーが 1 台も無いといった
    場合でも例外は投げず、空の検出結果と日本語の理由を返す (スキャン本体を止めてはならない)

    Returns:
        tuple[list[DetectedCard], str | None]: (検出結果, 全体的なエラー理由 or None)
    """

    pcsc, error = PCSCLite.Load()
    if pcsc is None:
        return [], error
    return DetectCASCardsWithLibrary(pcsc)

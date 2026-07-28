from __future__ import annotations

import pytest

from isdb_scanner.catv.cards import (
    INITIAL_SETTING_CONDITIONS_COMMAND,
    SCARD_E_NO_READERS_AVAILABLE,
    SCARD_E_NO_SERVICE,
    SCARD_E_NO_SMARTCARD,
    SCARD_S_SUCCESS,
    SCARD_W_REMOVED_CARD,
    CardType,
    DetectCASCards,
    DetectCASCardsWithLibrary,
    FormatCardId,
    GetCardTypeFromCASystemId,
    NormalizeResultCode,
    ParseInitialSettingConditionsResponse,
    ParseReaderNames,
    PCSCLite,
)


# 実カードには一切依存せず、合成した (実在しない) レスポンスバイト列とフェイクの libpcsclite ラッパーのみでテストする
# (実在のカード ID や受信環境を特定できる情報はテストに含めない)


def BuildInitialSettingConditionsResponse(
    ca_system_id: int,
    card_id: int = 0x0102_0304_0506,
    return_code: int = 0x2100,
    card_status: int = 0x0000,
) -> bytes:
    """
    Initial Setting Conditions コマンドのレスポンスを合成する (テスト用の完全な合成値)
    バイト配置は libaribb25 の aribb25/b_cas_card.c connect_card() の読み出しオフセットに合わせている:
      0-1: 未使用 / 2-3: card_status / 4-5: リターンコード / 6-7: CA_system_id / 8-13: カード ID (48bit)
      16-47: system_key / 48-55: init_cbc (いずれも復号専用のため本実装は読まないが、実カードと同じ長さになるよう埋める)
    """

    response = bytearray(57)
    response[0:2] = b'\x00\x00'
    response[2:4] = card_status.to_bytes(2, 'big')
    response[4:6] = return_code.to_bytes(2, 'big')
    response[6:8] = ca_system_id.to_bytes(2, 'big')
    response[8:14] = card_id.to_bytes(6, 'big')
    return bytes(response)


class FakePCSCLite:
    """PCSCLiteInterface を満たすフェイク実装 (実 libpcsclite / 実カードなしで DetectCASCardsWithLibrary() を検証する)"""

    def __init__(
        self,
        reader_names: list[str],
        transmit_results: dict[str, tuple[int, bytes]] | None = None,
        connect_results: dict[str, int] | None = None,
        establish_context_result: int = SCARD_S_SUCCESS,
        list_readers_result: int = SCARD_S_SUCCESS,
        atrs: dict[str, str | None] | None = None,
    ) -> None:
        self.reader_names = reader_names
        self.transmit_results = transmit_results or {}
        self.connect_results = connect_results or {}
        self.establish_context_result = establish_context_result
        self.list_readers_result = list_readers_result
        self.atrs = atrs or {}
        # 呼び出し履歴 (接続の後始末や送信 APDU の検証に使う)
        self.transmitted_commands: list[bytes] = []
        self.connected_readers: list[str] = []
        self.disconnected_cards: list[int] = []
        self.released_contexts: list[int] = []

    def EstablishContext(self) -> tuple[int, int]:
        return self.establish_context_result, 1000

    def ListReaders(self, context: int) -> tuple[int, list[str]]:
        if self.list_readers_result != SCARD_S_SUCCESS:
            return self.list_readers_result, []
        return SCARD_S_SUCCESS, list(self.reader_names)

    def Connect(self, context: int, reader_name: str) -> tuple[int, int]:
        result = self.connect_results.get(reader_name, SCARD_S_SUCCESS)
        if result != SCARD_S_SUCCESS:
            return result, 0
        self.connected_readers.append(reader_name)
        return SCARD_S_SUCCESS, 2000 + self.reader_names.index(reader_name)

    def GetATR(self, card: int) -> str | None:
        return self.atrs.get(self.reader_names[card - 2000])

    def Transmit(self, card: int, command: bytes) -> tuple[int, bytes]:
        self.transmitted_commands.append(command)
        return self.transmit_results.get(self.reader_names[card - 2000], (SCARD_S_SUCCESS, b''))

    def Disconnect(self, card: int) -> None:
        self.disconnected_cards.append(card)

    def ReleaseContext(self, context: int) -> None:
        self.released_contexts.append(context)


class TestParseInitialSettingConditionsResponse:
    """Initial Setting Conditions コマンドのレスポンス解析のテスト"""

    def test_parse_bcas_response(self) -> None:
        """CA_system_id 0x0005 (ARIB 限定受信方式) のレスポンスを解析できる"""

        ca_system_id, card_id = ParseInitialSettingConditionsResponse(
            BuildInitialSettingConditionsResponse(0x0005, card_id=0x00AB_CDEF_1234)
        )
        assert ca_system_id == 0x0005
        assert card_id == '00AB-CDEF-1234'

    def test_parse_ccas_matsushita_response(self) -> None:
        """CA_system_id 0x0006 (松下 CATV 限定受信方式) のレスポンスを解析できる"""

        ca_system_id, card_id = ParseInitialSettingConditionsResponse(
            BuildInitialSettingConditionsResponse(0x0006, card_id=0x0000_0000_0001)
        )
        assert ca_system_id == 0x0006
        assert card_id == '0000-0000-0001'

    def test_parse_ccas_hitachi_response(self) -> None:
        """CA_system_id 0x0003 (日立方式) のレスポンスを解析できる"""

        ca_system_id, card_id = ParseInitialSettingConditionsResponse(BuildInitialSettingConditionsResponse(0x0003))
        assert ca_system_id == 0x0003
        assert card_id == '0102-0304-0506'

    def test_parse_unknown_ca_system_id_response(self) -> None:
        """ARIB STD-B10 付録M 表M-1 に割当のない CA_system_id もそのまま取り出せる"""

        ca_system_id, card_id = ParseInitialSettingConditionsResponse(BuildInitialSettingConditionsResponse(0x00FF))
        assert ca_system_id == 0x00FF
        assert card_id == '0102-0304-0506'

    def test_parse_too_short_response(self) -> None:
        """レスポンスが短すぎる場合は例外を投げず None を返す"""

        # カード ID の末尾 (14 バイト目) に届かない長さのレスポンス
        assert ParseInitialSettingConditionsResponse(b'') == (None, None)
        assert ParseInitialSettingConditionsResponse(b'\x6d\x00') == (None, None)
        assert ParseInitialSettingConditionsResponse(BuildInitialSettingConditionsResponse(0x0005)[:13]) == (None, None)

    def test_parse_boundary_length_response(self) -> None:
        """カード ID の末尾ちょうどまでのレスポンス (14 バイト) は解析できる"""

        ca_system_id, card_id = ParseInitialSettingConditionsResponse(BuildInitialSettingConditionsResponse(0x0005)[:14])
        assert ca_system_id == 0x0005
        assert card_id == '0102-0304-0506'

    def test_parse_return_code_mismatch(self) -> None:
        """リターンコードが 0x2100 でないレスポンスは解析しない (libaribb25 と同じ判定)"""

        assert ParseInitialSettingConditionsResponse(BuildInitialSettingConditionsResponse(0x0005, return_code=0x9000)) == (
            None,
            None,
        )


class TestGetCardTypeFromCASystemId:
    """CA_system_id からのカード種別判定のテスト"""

    def test_bcas(self) -> None:
        assert GetCardTypeFromCASystemId(0x0005) == CardType.BCAS

    def test_ccas(self) -> None:
        assert GetCardTypeFromCASystemId(0x0003) == CardType.CCAS
        assert GetCardTypeFromCASystemId(0x0004) == CardType.CCAS
        assert GetCardTypeFromCASystemId(0x0006) == CardType.CCAS

    def test_other(self) -> None:
        # 既知だが B-CAS/C-CAS のいずれでもない CA_system_id と、未知の CA_system_id
        assert GetCardTypeFromCASystemId(0x0001) == CardType.OTHER
        assert GetCardTypeFromCASystemId(0x00FF) == CardType.OTHER

    def test_unknown(self) -> None:
        assert GetCardTypeFromCASystemId(None) == CardType.UNKNOWN


class TestFormatCardId:
    """カード ID の表示用文字列整形のテスト"""

    def test_format(self) -> None:
        assert FormatCardId(0x0000_0000_0000) == '0000-0000-0000'
        assert FormatCardId(0xFFFF_FFFF_FFFF) == 'FFFF-FFFF-FFFF'
        assert FormatCardId(0x0012_3456_789A) == '0012-3456-789A'


class TestParseReaderNames:
    """SCardListReaders() が返す multi-string のパースのテスト"""

    def test_parse_multiple_readers(self) -> None:
        """NUL 区切り・末尾二重 NUL の multi-string を正しく分割できる"""

        buffer = b'Fake Card Reader A 00 00\x00Fake Card Reader B 01 00\x00\x00'
        assert ParseReaderNames(buffer) == ['Fake Card Reader A 00 00', 'Fake Card Reader B 01 00']

    def test_parse_single_reader(self) -> None:
        assert ParseReaderNames(b'Fake Card Reader A 00 00\x00\x00') == ['Fake Card Reader A 00 00']

    def test_parse_empty(self) -> None:
        assert ParseReaderNames(b'') == []
        assert ParseReaderNames(b'\x00') == []
        assert ParseReaderNames(b'\x00\x00') == []

    def test_parse_non_ascii_reader_name(self) -> None:
        """非 ASCII を含むリーダー名でもデコードに失敗しない"""

        assert ParseReaderNames('フェイクリーダー 00 00\x00\x00'.encode()) == ['フェイクリーダー 00 00']
        # 不正なバイト列が含まれていても例外にならない
        assert len(ParseReaderNames(b'Fake \xff\xfe Reader\x00\x00')) == 1


class TestNormalizeResultCode:
    """SCard* API の戻り値の正規化のテスト"""

    def test_normalize(self) -> None:
        # 32bit 環境で負値として返ってきた場合でも、符号なし 32bit の定数と比較できる
        assert NormalizeResultCode(SCARD_E_NO_SERVICE) == SCARD_E_NO_SERVICE
        assert NormalizeResultCode(SCARD_E_NO_SERVICE - 0x100000000) == SCARD_E_NO_SERVICE
        assert NormalizeResultCode(0) == SCARD_S_SUCCESS


class TestDetectCASCardsWithLibrary:
    """DetectCASCardsWithLibrary() のテスト (フェイクの libpcsclite ラッパーを使用)"""

    def test_detect_bcas_and_ccas(self) -> None:
        """B-CAS カードと C-CAS カードが 1 台ずつ挿さっている場合"""

        pcsc = FakePCSCLite(
            reader_names=['Fake Card Reader A 00 00', 'Fake Card Reader B 01 00'],
            transmit_results={
                'Fake Card Reader A 00 00': (SCARD_S_SUCCESS, BuildInitialSettingConditionsResponse(0x0005)),
                'Fake Card Reader B 01 00': (SCARD_S_SUCCESS, BuildInitialSettingConditionsResponse(0x0006)),
            },
            atrs={'Fake Card Reader A 00 00': '3B 00 00'},
        )
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert error is None
        assert len(cards) == 2
        assert cards[0].reader_name == 'Fake Card Reader A 00 00'
        assert cards[0].card_type == CardType.BCAS
        assert cards[0].ca_system_id == 0x0005
        assert cards[0].ca_system_name == 'B-CAS/A-CAS (ARIB 限定受信方式)'
        assert cards[0].card_id == '0102-0304-0506'
        assert cards[0].atr == '3B 00 00'
        assert cards[0].error is None
        assert cards[1].card_type == CardType.CCAS
        assert cards[1].ca_system_id == 0x0006
        assert cards[1].ca_system_name == 'C-CAS (松下 CATV 限定受信方式)'
        assert cards[1].atr is None

        # 送信した APDU は Initial Setting Conditions コマンドのみであること (スコープ境界の担保)
        assert pcsc.transmitted_commands == [INITIAL_SETTING_CONDITIONS_COMMAND] * 2
        # 接続したカードとコンテキストが確実に後始末されていること
        assert len(pcsc.disconnected_cards) == 2
        assert pcsc.released_contexts == [1000]

    def test_detect_unknown_ca_system_id(self) -> None:
        """未知の CA_system_id のカードは Other 扱いになり、表示名はフォールバックされる"""

        pcsc = FakePCSCLite(
            reader_names=['Fake Card Reader A 00 00'],
            transmit_results={'Fake Card Reader A 00 00': (SCARD_S_SUCCESS, BuildInitialSettingConditionsResponse(0x00FF))},
        )
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert error is None
        assert cards[0].card_type == CardType.OTHER
        assert cards[0].ca_system_id == 0x00FF
        assert cards[0].ca_system_name == 'Unknown (0x00FF)'

    def test_no_readers_available(self) -> None:
        """カードリーダーが 1 台も無い場合 (SCARD_E_NO_READERS_AVAILABLE)"""

        pcsc = FakePCSCLite(reader_names=[], list_readers_result=SCARD_E_NO_READERS_AVAILABLE)
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert cards == []
        assert error is not None
        assert 'カードリーダーが 1 台も接続されていません' in error
        # エラーで抜ける場合もコンテキストは解放されること
        assert pcsc.released_contexts == [1000]

    def test_no_readers_returned(self) -> None:
        """SCardListReaders() が成功しつつ 0 台を返した場合も同様に扱う"""

        cards, error = DetectCASCardsWithLibrary(FakePCSCLite(reader_names=[]))

        assert cards == []
        assert error is not None
        assert 'カードリーダーが 1 台も接続されていません' in error

    def test_pcscd_not_running(self) -> None:
        """pcscd が起動していない場合は例外を投げず、理由と対処のヒントを返す"""

        pcsc = FakePCSCLite(reader_names=[], establish_context_result=SCARD_E_NO_SERVICE)
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert cards == []
        assert error is not None
        assert 'pcscd' in error
        # コンテキストが確立できていないため、解放処理は呼ばれない
        assert pcsc.released_contexts == []

    def test_partial_failure(self) -> None:
        """1 台目でカードエラー・2 台目は成功する場合、2 台目の検出は継続される"""

        pcsc = FakePCSCLite(
            reader_names=['Fake Card Reader A 00 00', 'Fake Card Reader B 01 00'],
            connect_results={'Fake Card Reader A 00 00': SCARD_E_NO_SMARTCARD},
            transmit_results={'Fake Card Reader B 01 00': (SCARD_S_SUCCESS, BuildInitialSettingConditionsResponse(0x0005))},
        )
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert error is None
        assert len(cards) == 2
        assert cards[0].card_type == CardType.UNKNOWN
        assert cards[0].ca_system_id is None
        assert cards[0].error is not None
        assert 'カードが挿入されていません' in cards[0].error
        assert cards[1].card_type == CardType.BCAS
        assert cards[1].error is None
        # 接続に失敗したリーダーには APDU を送信しないこと
        assert pcsc.transmitted_commands == [INITIAL_SETTING_CONDITIONS_COMMAND]

    def test_transmit_failure(self) -> None:
        """APDU の送受信に失敗した場合 (収録中にカードが抜かれた等)"""

        pcsc = FakePCSCLite(
            reader_names=['Fake Card Reader A 00 00'],
            transmit_results={'Fake Card Reader A 00 00': (SCARD_W_REMOVED_CARD, b'')},
        )
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert error is None
        assert cards[0].card_type == CardType.UNKNOWN
        assert cards[0].error is not None
        assert 'カードが抜かれています' in cards[0].error
        # 送信に失敗してもカードは切断されること
        assert len(pcsc.disconnected_cards) == 1

    def test_unparsable_response(self) -> None:
        """CAS カード以外のカードなど、解析できない応答が返ってきた場合"""

        pcsc = FakePCSCLite(
            reader_names=['Fake Card Reader A 00 00'],
            transmit_results={'Fake Card Reader A 00 00': (SCARD_S_SUCCESS, b'\x6d\x00')},
        )
        cards, error = DetectCASCardsWithLibrary(pcsc)

        assert error is None
        assert cards[0].card_type == CardType.UNKNOWN
        assert cards[0].ca_system_id is None
        assert cards[0].card_id is None
        assert cards[0].error is not None


class TestDetectCASCards:
    """DetectCASCards() (libpcsclite のロードを含む) のテスト"""

    def test_library_load_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """libpcsclite がロードできない場合は例外を投げず (空リスト, 理由) を返す"""

        monkeypatch.setattr(PCSCLite, 'Load', classmethod(lambda cls: (None, 'libpcsclite.so.1 をロードできませんでした')))
        cards, error = DetectCASCards()

        assert cards == []
        assert error == 'libpcsclite.so.1 をロードできませんでした'

    def test_delegates_to_loaded_library(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """libpcsclite がロードできた場合は、そのラッパーを使って検出処理に委譲する"""

        # 実カードに APDU を送信しないよう、実 libpcsclite の代わりにフェイクをロードさせる
        pcsc = FakePCSCLite(
            reader_names=['Fake Card Reader A 00 00'],
            transmit_results={'Fake Card Reader A 00 00': (SCARD_S_SUCCESS, BuildInitialSettingConditionsResponse(0x0004))},
        )
        monkeypatch.setattr(PCSCLite, 'Load', classmethod(lambda cls: (pcsc, None)))
        cards, error = DetectCASCards()

        assert error is None
        assert len(cards) == 1
        assert cards[0].card_type == CardType.CCAS
        assert cards[0].ca_system_id == 0x0004

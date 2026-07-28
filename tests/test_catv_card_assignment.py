import shlex
import stat
import subprocess
from pathlib import Path

from isdb_scanner.catv.card_assignment import (
    MIRAKC_DECODE_FILTER_SCRIPT_NAME,
    MIRAKURUN_BCAS_DECODER_SCRIPT_NAME,
    MIRAKURUN_CCAS_DECODER_SCRIPT_NAME,
    CATVCardAssignmentReportFormatter,
    CollectCCASPhysicalChannels,
    DetermineDecodability,
    FindReaderNameForCardType,
    FormatDetectedCardsSummary,
    GetReaderNameForRequiredCard,
    WriteDecoderScripts,
)
from isdb_scanner.catv.cards import CardType, DetectedCard
from isdb_scanner.catv.constants import (
    CarrierType,
    CASInfo,
    CATVCarrierInfo,
    CATVMMTInfo,
    CATVServiceInfo,
    CATVTransportStreamInfo,
)


# テストで使うカードリーダー名・カード ID はすべて合成値
# (実在のカードリーダー製品名・カード ID・受信環境を特定できる情報は一切使わない)
FAKE_BCAS_READER_NAME = 'Synthetic Card Reader A 00 00'
FAKE_CCAS_READER_NAME = 'Synthetic Card Reader B 01 00'
FAKE_OTHER_READER_NAME = 'Synthetic Card Reader C 02 00'
# シェルのシングルクォートエスケープの検証用に、リーダー名にアポストロフィと空白を含めたケース
FAKE_QUOTE_HOSTILE_READER_NAME = "Synthetic 'Quote' Reader; rm -rf /"


def BuildBCASCard(reader_name: str = FAKE_BCAS_READER_NAME) -> DetectedCard:
    """テスト用に B-CAS カードの検出結果 (合成データ) を組み立てる"""

    return DetectedCard(
        reader_name=reader_name,
        ca_system_id=0x0005,
        ca_system_name='B-CAS/A-CAS (ARIB 限定受信方式)',
        card_type=CardType.BCAS,
        card_id='0000-0000-0001',
        atr='3B 00 00 00 00 00 00 00 00 00 00',
    )


def BuildCCASCard(reader_name: str = FAKE_CCAS_READER_NAME) -> DetectedCard:
    """テスト用に C-CAS カードの検出結果 (合成データ) を組み立てる"""

    return DetectedCard(
        reader_name=reader_name,
        ca_system_id=0x0006,
        ca_system_name='C-CAS (松下 CATV 限定受信方式)',
        card_type=CardType.CCAS,
        card_id='0000-0000-0002',
        atr='3B 00 00 00 00 00 00 00 00 00 01',
    )


def BuildSyntheticCASCarriers() -> list[CATVCarrierInfo]:
    """
    受信可能性判定のテスト用に、required_card が一通り揃った合成の CATVCarrierInfo 一覧を組み立てる
    - CATV_20: TSMF / 相対TS1 (required none) + 相対TS2 (required B-CAS)
    - CATV_21: SingleTS (required C-CAS)
    - CATV_22: SingleTS (required unknown)
    - CATV_C36: TLV (4K/8K MMT。A-CAS 相当だが CA_system_id では区別できないため常に unknown)
    - CATV_C62: Empty (受信不可。判定対象外)
    """

    return [
        CATVCarrierInfo(
            physical_channel='CATV_20',
            carrier_type=CarrierType.TSMF,
            transport_streams=[
                CATVTransportStreamInfo(
                    physical_channel='CATV_20',
                    tsmf_relative_ts_number=1,
                    transport_stream_id=0x1000,
                    network_name='合成ネットワーク1',
                    retransmission_source='Terrestrial',
                    cas=CASInfo(required_card='none', scramble_ratio=0.0),
                    services=[CATVServiceInfo(service_id=1024, service_name='合成サービス1')],
                ),
                CATVTransportStreamInfo(
                    physical_channel='CATV_20',
                    tsmf_relative_ts_number=2,
                    transport_stream_id=0x1001,
                    network_name='合成ネットワーク2',
                    retransmission_source='Terrestrial',
                    cas=CASInfo(ca_system_ids=[0x0005], required_card='B-CAS', scramble_ratio=0.9),
                    services=[CATVServiceInfo(service_id=1025, service_name='合成サービス2')],
                ),
            ],
        ),
        CATVCarrierInfo(
            physical_channel='CATV_21',
            carrier_type=CarrierType.SingleTS,
            transport_streams=[
                CATVTransportStreamInfo(
                    physical_channel='CATV_21',
                    transport_stream_id=0x2000,
                    network_name='合成自主放送',
                    retransmission_source='SelfBroadcast',
                    cas=CASInfo(ca_system_ids=[0x0006], required_card='C-CAS', scramble_ratio=0.9),
                    services=[CATVServiceInfo(service_id=1, service_name='合成専門チャンネル')],
                ),
            ],
        ),
        CATVCarrierInfo(
            physical_channel='CATV_22',
            carrier_type=CarrierType.SingleTS,
            transport_streams=[
                CATVTransportStreamInfo(
                    physical_channel='CATV_22',
                    transport_stream_id=0x3000,
                    network_name='Unknown',
                    retransmission_source='SelfBroadcast',
                    cas=CASInfo(required_card='unknown', scramble_ratio=0.9),
                    services=[],
                ),
            ],
        ),
        CATVCarrierInfo(
            physical_channel='CATV_C36',
            carrier_type=CarrierType.TLV,
            transport_streams=[],
            mmt=CATVMMTInfo(),
        ),
        CATVCarrierInfo(physical_channel='CATV_C62', carrier_type=CarrierType.Empty, transport_streams=[]),
    ]


class TestDetermineDecodability:
    """DetermineDecodability() の全分岐のテスト"""

    def test_none_is_always_yes(self):
        # スクランブルされていないチャンネルはカードの有無に関わらず受信できる
        assert DetermineDecodability('none', []) == 'yes'
        assert DetermineDecodability('none', [BuildBCASCard()]) == 'yes'

    def test_bcas_requires_bcas_card(self):
        assert DetermineDecodability('B-CAS', [BuildBCASCard()]) == 'yes'
        assert DetermineDecodability('B-CAS', [BuildBCASCard(), BuildCCASCard()]) == 'yes'
        assert DetermineDecodability('B-CAS', [BuildCCASCard()]) == 'no'
        assert DetermineDecodability('B-CAS', []) == 'no'

    def test_ccas_requires_ccas_card(self):
        # C-CAS カードが挿さっていれば yes (ただし「その局が発行した契約カードか」までは検証できない)
        assert DetermineDecodability('C-CAS', [BuildCCASCard()]) == 'yes'
        assert DetermineDecodability('C-CAS', [BuildBCASCard(), BuildCCASCard()]) == 'yes'
        assert DetermineDecodability('C-CAS', [BuildBCASCard()]) == 'no'
        assert DetermineDecodability('C-CAS', []) == 'no'

    def test_acas_is_always_unknown(self):
        # A-CAS の CA_system_id は B-CAS と同じ 0x0005 でリーダー側からは区別できないため、常に判定不能
        assert DetermineDecodability('A-CAS', []) == 'unknown'
        assert DetermineDecodability('A-CAS', [BuildBCASCard()]) == 'unknown'
        assert DetermineDecodability('A-CAS', [BuildBCASCard(), BuildCCASCard()]) == 'unknown'

    def test_unknown_is_always_unknown(self):
        assert DetermineDecodability('unknown', []) == 'unknown'
        assert DetermineDecodability('unknown', [BuildBCASCard(), BuildCCASCard()]) == 'unknown'

    def test_other_card_type_is_not_counted(self):
        # CardType.OTHER / UNKNOWN のカードは B-CAS/C-CAS のどちらとしてもカウントしない
        other_card = DetectedCard(reader_name=FAKE_OTHER_READER_NAME, ca_system_id=0x0009, card_type=CardType.OTHER)
        error_card = DetectedCard(reader_name=FAKE_OTHER_READER_NAME, error='カードが挿入されていません')
        assert DetermineDecodability('B-CAS', [other_card, error_card]) == 'no'
        assert DetermineDecodability('C-CAS', [other_card, error_card]) == 'no'


class TestReaderNameLookup:
    """カード種別 → 使用すべきカードリーダー名の解決のテスト"""

    def test_find_reader_name_for_card_type(self):
        cards = [BuildBCASCard(), BuildCCASCard()]
        assert FindReaderNameForCardType(cards, CardType.BCAS) == FAKE_BCAS_READER_NAME
        assert FindReaderNameForCardType(cards, CardType.CCAS) == FAKE_CCAS_READER_NAME
        assert FindReaderNameForCardType([], CardType.BCAS) is None

    def test_first_matching_reader_wins(self):
        # 同じ種別のカードが複数ある場合は検出順で最初のものを使う
        cards = [BuildBCASCard('Synthetic Card Reader A 00 00'), BuildBCASCard('Synthetic Card Reader A 00 01')]
        assert FindReaderNameForCardType(cards, CardType.BCAS) == 'Synthetic Card Reader A 00 00'

    def test_get_reader_name_for_required_card(self):
        cards = [BuildBCASCard(), BuildCCASCard()]
        assert GetReaderNameForRequiredCard('B-CAS', cards) == FAKE_BCAS_READER_NAME
        assert GetReaderNameForRequiredCard('C-CAS', cards) == FAKE_CCAS_READER_NAME
        # カード不要・判定不能なチャンネルには使用すべきリーダーが存在しない
        assert GetReaderNameForRequiredCard('none', cards) is None
        assert GetReaderNameForRequiredCard('A-CAS', cards) is None
        assert GetReaderNameForRequiredCard('unknown', cards) is None


class TestCollectCCASPhysicalChannels:
    """C-CAS が必要な物理チャンネル一覧の収集のテスト"""

    def test_only_ccas_channels_are_collected(self):
        assert CollectCCASPhysicalChannels(BuildSyntheticCASCarriers()) == ['CATV_21']

    def test_returns_sorted_unique_channels(self):
        carriers = BuildSyntheticCASCarriers()
        # 同じ物理チャンネルに C-CAS の TS が 2 本あっても重複しないこと・昇順にソートされること
        carriers[0].transport_streams[0].cas = CASInfo(required_card='C-CAS')
        carriers[0].transport_streams[1].cas = CASInfo(required_card='C-CAS')
        assert CollectCCASPhysicalChannels(carriers) == ['CATV_20', 'CATV_21']

    def test_no_ccas_channel_returns_empty(self):
        carriers = [
            CATVCarrierInfo(
                physical_channel='CATV_20',
                carrier_type=CarrierType.SingleTS,
                transport_streams=[
                    CATVTransportStreamInfo(physical_channel='CATV_20', cas=CASInfo(required_card='B-CAS')),
                ],
            )
        ]
        assert CollectCCASPhysicalChannels(carriers) == []


class TestCATVCardAssignmentReportFormatter:
    """受信可能性レポート (CATV.cards.txt) 生成のテスト"""

    def test_report_with_both_cards(self, tmp_path: Path):
        report = CATVCardAssignmentReportFormatter(
            tmp_path / 'CATV.cards.txt', BuildSyntheticCASCarriers(), [BuildBCASCard(), BuildCCASCard()]
        ).format()

        # 1. 検出したリーダー/カードの一覧
        assert FAKE_BCAS_READER_NAME in report
        assert FAKE_CCAS_READER_NAME in report
        assert '0x0005' in report
        assert '0x0006' in report
        assert '0000-0000-0001' in report

        # 2. 物理チャンネル×TS ごとの表
        lines = report.splitlines()
        none_row = next(line for line in lines if line.startswith('CATV_20') and '0x1000' in line)
        assert 'none' in none_row and 'yes' in none_row
        bcas_row = next(line for line in lines if line.startswith('CATV_20') and '0x1001' in line)
        assert 'B-CAS' in bcas_row and 'yes' in bcas_row and FAKE_BCAS_READER_NAME in bcas_row
        ccas_row = next(line for line in lines if line.startswith('CATV_21'))
        assert 'C-CAS' in ccas_row and 'yes' in ccas_row and FAKE_CCAS_READER_NAME in ccas_row
        unknown_row = next(line for line in lines if line.startswith('CATV_22'))
        assert 'unknown' in unknown_row
        # TLV キャリアは A-CAS 相当だが常に unknown
        tlv_row = next(line for line in lines if line.startswith('CATV_C36'))
        assert 'A-CAS' in tlv_row and 'unknown' in tlv_row
        # 受信できなかった Empty キャリアは判定対象外
        assert 'CATV_C62' not in report

        # 3. 注記 (C-CAS の契約カード前提 / A-CAS は判定不能 / カード 1 枚を複数チューナーで共有可能)
        assert '契約' in report
        assert 'A-CAS' in report
        assert 'SHARED' in report

    def test_report_without_any_card_includes_reason(self, tmp_path: Path):
        report = CATVCardAssignmentReportFormatter(
            tmp_path / 'CATV.cards.txt',
            BuildSyntheticCASCarriers(),
            [],
            'PC/SC サービス (pcscd) が起動していません',
        ).format()

        # 4. カードが 1 枚も検出できなかった場合は理由と --list-card-readers での確認を促す文が出る
        assert 'PC/SC サービス (pcscd) が起動していません' in report
        assert '--list-card-readers' in report
        # B-CAS/C-CAS が必要なチャンネルはすべて decodable=no になる
        lines = report.splitlines()
        assert 'no' in next(line for line in lines if line.startswith('CATV_20') and '0x1001' in line)
        assert 'no' in next(line for line in lines if line.startswith('CATV_21'))

    def test_report_with_bcas_only(self, tmp_path: Path):
        report = CATVCardAssignmentReportFormatter(tmp_path / 'CATV.cards.txt', BuildSyntheticCASCarriers(), [BuildBCASCard()]).format()

        lines = report.splitlines()
        bcas_row = next(line for line in lines if line.startswith('CATV_20') and '0x1001' in line)
        assert 'yes' in bcas_row and FAKE_BCAS_READER_NAME in bcas_row
        # C-CAS カードが無いため C-CAS チャンネルは受信できない
        ccas_row = next(line for line in lines if line.startswith('CATV_21'))
        assert 'no' in ccas_row
        assert FAKE_CCAS_READER_NAME not in report

    def test_report_with_ccas_only(self, tmp_path: Path):
        report = CATVCardAssignmentReportFormatter(tmp_path / 'CATV.cards.txt', BuildSyntheticCASCarriers(), [BuildCCASCard()]).format()

        lines = report.splitlines()
        assert 'no' in next(line for line in lines if line.startswith('CATV_20') and '0x1001' in line)
        ccas_row = next(line for line in lines if line.startswith('CATV_21'))
        assert 'yes' in ccas_row and FAKE_CCAS_READER_NAME in ccas_row

    def test_report_includes_card_error(self, tmp_path: Path):
        # 特定のリーダーでのみ取得に失敗した場合、その理由が一覧に出ること
        cards = [BuildBCASCard(), DetectedCard(reader_name=FAKE_OTHER_READER_NAME, error='カードが挿入されていません')]
        report = CATVCardAssignmentReportFormatter(tmp_path / 'CATV.cards.txt', BuildSyntheticCASCarriers(), cards).format()
        assert FAKE_OTHER_READER_NAME in report
        assert 'カードが挿入されていません' in report

    def test_report_with_no_receivable_carrier(self, tmp_path: Path):
        carriers = [CATVCarrierInfo(physical_channel='CATV_C62', carrier_type=CarrierType.Empty, transport_streams=[])]
        report = CATVCardAssignmentReportFormatter(tmp_path / 'CATV.cards.txt', carriers, [BuildBCASCard()]).format()
        assert '判定対象がありません' in report

    def test_save_writes_file(self, tmp_path: Path):
        save_path = tmp_path / 'CATV.cards.txt'
        formatted_str = CATVCardAssignmentReportFormatter(save_path, BuildSyntheticCASCarriers(), [BuildBCASCard(), BuildCCASCard()]).save()
        assert save_path.is_file()
        assert save_path.read_text(encoding='utf-8') == formatted_str
        assert formatted_str.endswith('\n')


class TestFormatDetectedCardsSummary:
    """コンソール表示用のカード検出要約のテスト"""

    def test_summary_lists_detected_cards(self):
        summary = FormatDetectedCardsSummary([BuildBCASCard(), BuildCCASCard()], None)
        assert summary == f'Detected cards: B-CAS ({FAKE_BCAS_READER_NAME}), C-CAS ({FAKE_CCAS_READER_NAME})'

    def test_summary_with_detection_error(self):
        summary = FormatDetectedCardsSummary([], 'カードリーダーが 1 台も接続されていません')
        assert summary == 'No CAS card detected: カードリーダーが 1 台も接続されていません'

    def test_summary_without_cas_card(self):
        # リーダー自体は見つかったが CAS カードが 1 枚も挿さっていなかった場合
        error_card = DetectedCard(reader_name=FAKE_OTHER_READER_NAME, error='カードが挿入されていません')
        assert FormatDetectedCardsSummary([error_card], None) == 'No CAS card detected in the connected card reader(s).'

    def test_summary_is_none_when_not_checked(self):
        # カード検出自体を行わなかった場合 (--no-check-cards) は何も表示しない
        assert FormatDetectedCardsSummary(None, None) is None


def GetExecLine(script_body: str) -> str:
    """スクリプト本文から `exec recisdb decode ...` の行を1つ取り出す"""

    return next(line.strip() for line in script_body.splitlines() if line.strip().startswith('exec recisdb decode'))


class TestWriteDecoderScripts:
    """デコーダースクリプト (Mirakurun ラッパー / mirakc decode-filter) 生成のテスト"""

    def test_scripts_are_generated_only_when_both_cards_exist(self, tmp_path: Path):
        carriers = BuildSyntheticCASCarriers()

        # B-CAS のみ / C-CAS のみ / カード無しでは生成しない (リーダーを選ぶ必要がそもそも無いため)
        assert WriteDecoderScripts(tmp_path, carriers, [BuildBCASCard()]) == []
        assert WriteDecoderScripts(tmp_path, carriers, [BuildCCASCard()]) == []
        assert WriteDecoderScripts(tmp_path, carriers, []) == []
        assert not (tmp_path / 'Mirakurun' / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME).exists()
        assert not (tmp_path / 'mirakc' / MIRAKC_DECODE_FILTER_SCRIPT_NAME).exists()

        # 両方揃ったときのみ 3 本とも生成される
        script_paths = WriteDecoderScripts(tmp_path, carriers, [BuildBCASCard(), BuildCCASCard()])
        assert script_paths == [
            tmp_path / 'Mirakurun' / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME,
            tmp_path / 'Mirakurun' / MIRAKURUN_CCAS_DECODER_SCRIPT_NAME,
            tmp_path / 'mirakc' / MIRAKC_DECODE_FILTER_SCRIPT_NAME,
        ]

    def test_scripts_are_executable(self, tmp_path: Path):
        script_paths = WriteDecoderScripts(tmp_path, BuildSyntheticCASCarriers(), [BuildBCASCard(), BuildCCASCard()])
        for script_path in script_paths:
            assert script_path.is_file()
            assert stat.S_IMODE(script_path.stat().st_mode) == 0o755

    def test_mirakurun_decoder_scripts_content(self, tmp_path: Path):
        WriteDecoderScripts(tmp_path, BuildSyntheticCASCarriers(), [BuildBCASCard(), BuildCCASCard()])
        bcas_script = (tmp_path / 'Mirakurun' / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME).read_text(encoding='utf-8')
        ccas_script = (tmp_path / 'Mirakurun' / MIRAKURUN_CCAS_DECODER_SCRIPT_NAME).read_text(encoding='utf-8')

        assert bcas_script.startswith('#!/bin/sh\n')
        # recisdb decode の引数形 (-i - で標準入力 / 位置引数 - で標準出力) と --card のリーダー名指定
        assert shlex.split(GetExecLine(bcas_script)) == [
            'exec', 'recisdb', 'decode', '--card', FAKE_BCAS_READER_NAME, '-i', '-', '-',
        ]  # fmt: skip
        assert shlex.split(GetExecLine(ccas_script)) == [
            'exec', 'recisdb', 'decode', '--card', FAKE_CCAS_READER_NAME, '-i', '-', '-',
        ]  # fmt: skip

        # 冒頭コメントに「Mirakurun の decoder には引数を渡せないためラッパが必要」「混在環境では recisdb が必要」の説明があること
        assert 'child_process.spawn' in bcas_script
        assert 'arib-b25-stream-test' in bcas_script
        # recisdb decode の引数形の根拠がコメントに書かれていること
        assert 'get_source()' in bcas_script
        assert 'get_output()' in bcas_script
        assert 'override_card_reader_name_pattern()' in bcas_script

    def test_reader_name_is_shell_escaped(self, tmp_path: Path):
        # リーダー名にシングルクォートやシェルのメタ文字が含まれていても、シェル上で元の文字列に戻ること
        cards = [BuildBCASCard(FAKE_QUOTE_HOSTILE_READER_NAME), BuildCCASCard()]
        script_paths = WriteDecoderScripts(tmp_path, BuildSyntheticCASCarriers(), cards)

        bcas_script = (tmp_path / 'Mirakurun' / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME).read_text(encoding='utf-8')
        assert shlex.split(GetExecLine(bcas_script)) == [
            'exec', 'recisdb', 'decode', '--card', FAKE_QUOTE_HOSTILE_READER_NAME, '-i', '-', '-',
        ]  # fmt: skip
        # 生の (エスケープされていない) リーダー名がそのままコマンド行に埋まっていないこと
        assert f"--card '{FAKE_QUOTE_HOSTILE_READER_NAME}'" not in bcas_script

        # 生成した全スクリプトが sh の構文として妥当であること (エスケープ漏れがあればここで落ちる)
        for script_path in script_paths:
            assert subprocess.run(['sh', '-n', str(script_path)], capture_output=True).returncode == 0

    def test_decode_filter_lists_only_ccas_channels(self, tmp_path: Path):
        script_path = WriteDecoderScripts(tmp_path, BuildSyntheticCASCarriers(), [BuildBCASCard(), BuildCCASCard()])[2]
        script = script_path.read_text(encoding='utf-8')

        assert script.startswith('#!/bin/sh\n')
        # case 分岐のパターン行には C-CAS が必要な物理チャンネルだけが列挙されること
        case_pattern_line = next(line.strip() for line in script.splitlines() if line.strip().endswith(')') and 'CATV_' in line)
        assert case_pattern_line == 'CATV_21)'
        assert 'CATV_20' not in script
        assert 'CATV_22' not in script
        assert 'CATV_C36' not in script

        # C-CAS 側は C-CAS リーダー、既定 (*) 側は B-CAS リーダーを使うこと
        exec_lines = [shlex.split(line.strip()) for line in script.splitlines() if line.strip().startswith('exec recisdb')]
        assert exec_lines == [
            ['exec', 'recisdb', 'decode', '--card', FAKE_CCAS_READER_NAME, '-i', '-', '-'],
            ['exec', 'recisdb', 'decode', '--card', FAKE_BCAS_READER_NAME, '-i', '-', '-'],
        ]

        # ヘッダーコメントに mirakc.yml への組み込み例 (filters.decode-filter.command) と Mustache 変数の実名があること
        assert 'filters:' in script
        assert 'decode-filter:' in script
        assert '{{{channel_name}}}' in script
        assert '{{{channel_type}}}' in script
        assert '{{{channel}}}' in script
        # --cas-as-sky に依存しない旨が書かれていること
        assert '--cas-as-sky' in script

    def test_decode_filter_without_any_ccas_channel(self, tmp_path: Path):
        # C-CAS が必要なチャンネルが 1 つも無い場合は case 分岐を生成しない (空の case は sh の構文エラーになるため)
        carriers = [
            CATVCarrierInfo(
                physical_channel='CATV_20',
                carrier_type=CarrierType.SingleTS,
                transport_streams=[
                    CATVTransportStreamInfo(physical_channel='CATV_20', cas=CASInfo(required_card='B-CAS')),
                ],
            )
        ]
        script_path = WriteDecoderScripts(tmp_path, carriers, [BuildBCASCard(), BuildCCASCard()])[2]
        script = script_path.read_text(encoding='utf-8')

        assert 'case "$CHANNEL" in' not in script
        assert shlex.split(GetExecLine(script)) == [
            'exec', 'recisdb', 'decode', '--card', FAKE_BCAS_READER_NAME, '-i', '-', '-',
        ]  # fmt: skip
        assert subprocess.run(['sh', '-n', str(script_path)], capture_output=True).returncode == 0

    def test_scripts_are_overwritten_on_regeneration(self, tmp_path: Path):
        # 2 回目の生成でリーダー名が更新されること (スクリプトは冪等に上書きされる)
        WriteDecoderScripts(tmp_path, BuildSyntheticCASCarriers(), [BuildBCASCard(), BuildCCASCard()])
        WriteDecoderScripts(tmp_path, BuildSyntheticCASCarriers(), [BuildBCASCard('Synthetic Card Reader A 00 09'), BuildCCASCard()])
        bcas_script = (tmp_path / 'Mirakurun' / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME).read_text(encoding='utf-8')
        assert 'Synthetic Card Reader A 00 09' in GetExecLine(bcas_script)
        assert FAKE_BCAS_READER_NAME not in GetExecLine(bcas_script)

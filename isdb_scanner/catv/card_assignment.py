from __future__ import annotations

import shlex
import stat
from pathlib import Path
from typing import Literal

from isdb_scanner.catv.cards import CardType, DetectedCard
from isdb_scanner.catv.constants import CarrierType, CATVCarrierInfo, RequiredCASCard


# CATV トラモジでは、地デジ/BS/CS の再送信チャンネルは元の B-CAS スクランブルのまま流れてくる一方、
# CATV 事業者の自主放送・専門チャンネルは事業者側で C-CAS に再スクランブルされて流れてくる
# そのため 1 台のホストに B-CAS カードと C-CAS カードの両方を挿し、チャンネルごとに使い分ける必要がある
# このモジュールは、スキャン結果 (どのチャンネルにどのカードが要るか) と cards.py のカード在庫 (どのリーダーに
# 何が挿さっているか) を突き合わせ、受信可能性レポート (CATV.cards.txt) とデコーダースクリプトを生成する
#
# 【CATV.json に decodable を入れない理由】
# 受信可能性はスキャン時点のカード接続状況に依存する値のため、CATV.json に混ぜると「カードを挿し替えただけ」で
# スキャン差分レポート (CATV.diff.txt) が偽陽性を出してしまう
# そのため CATV.json のスキーマは一切変更せず、この別ファイル (CATV.cards.txt) に隔離している


# 各チャンネルをこのホストのカードでデスクランブルできるかどうかの判定結果
Decodability = Literal['yes', 'no', 'unknown']

# 生成するデコーダースクリプトのファイル名
MIRAKURUN_BCAS_DECODER_SCRIPT_NAME = 'decoder-bcas.sh'
MIRAKURUN_CCAS_DECODER_SCRIPT_NAME = 'decoder-ccas.sh'
MIRAKC_DECODE_FILTER_SCRIPT_NAME = 'decode-filter.sh'

# 生成するスクリプトに付与するパーミッション (rwxr-xr-x)
SCRIPT_FILE_MODE = 0o755

# recisdb decode の引数形の根拠 (生成スクリプトのコメントにもそのまま埋め込む)
# 推測ではなく recisdb-rs (kazuki0824/recisdb-rs) のソースと README で確認済み:
# - recisdb-rs/src/context.rs の Commands::Decode
#     `-i` / `--input <file>` (必須) / `--card <リーダー名>` (任意) / 位置引数 <OUTPUT> (必須)
# - recisdb-rs/src/commands/utils.rs
#     get_source(): `if src == "-" { ... io::stdin().lock() ... }`  -> 入力 "-" は標準入力
#     get_output(): `Some(s) if s == "-" => Ok(Box::new(std::io::stdout().lock()))` -> 出力 "-" は標準出力
# - README の Usage: `recisdb decode [OPTIONS] --input <file> <OUTPUT>`
RECISDB_DECODE_ARGUMENT_REFERENCE_LINES = [
    '# recisdb decode の引数形は推測ではなく recisdb-rs のソース/README で確認済み:',
    '#   recisdb decode [OPTIONS] --input <file> <OUTPUT>   (README の Usage)',
    '#   - `-i` / `--input` は必須オプション (recisdb-rs/src/context.rs の Commands::Decode)。',
    '#     値が "-" のときは標準入力から読み込む',
    '#     (recisdb-rs/src/commands/utils.rs の get_source(): `if src == "-" { ... io::stdin().lock() ... }`)',
    '#   - <OUTPUT> は必須の位置引数。"-" のときは標準出力へ書き出す',
    '#     (同 get_output(): `Some(s) if s == "-" => Ok(Box::new(std::io::stdout().lock()))`)',
    '#   - `--card <リーダー名>` は default feature の prioritized_card_reader で有効になるオプション',
    '#     (README には未記載)。libaribb25 の override_card_reader_name_pattern() に渡され、',
    '#     リーダー名の「完全一致」で照合される (部分一致ではない) ため、',
    '#     PC/SC の SCardListReaders() が返す文字列をそのまま指定する必要がある',
]


def DetermineDecodability(required_card: RequiredCASCard, detected_cards: list[DetectedCard]) -> Decodability:
    """
    ある TS の受信に必要な CAS カード種別と、このホストに挿さっているカードの在庫を突き合わせ、
    デスクランブルできるかどうかを判定する

    Args:
        required_card (RequiredCASCard): 受信に必要な CAS カードの種別 ('none'/'B-CAS'/'C-CAS'/'A-CAS'/'unknown')
        detected_cards (list[DetectedCard]): 検出されたカードの一覧 (cards.py の DetectCASCards() の戻り値)

    Returns:
        Decodability: 'yes' (デスクランブル可能) / 'no' (必要なカードが無い) / 'unknown' (判定不能)
    """

    if required_card == 'none':
        # そもそもスクランブルされていないためカード不要
        return 'yes'
    if required_card == 'B-CAS':
        return 'yes' if HasCardType(detected_cards, CardType.BCAS) else 'no'
    if required_card == 'C-CAS':
        # C-CAS カードが挿さっていても、「その CATV 局が発行した契約カードか」まではカード側からは検証できない
        # (CA_system_id は方式を示すだけで、どの事業者が発行したカードかは分からない)
        # ここでは decodable = 'yes' としつつ、レポート側に契約カード前提である旨の注記を出す
        return 'yes' if HasCardType(detected_cards, CardType.CCAS) else 'no'
    if required_card == 'A-CAS':
        # A-CAS (4K/8K TLV/MMT) の CA_system_id は ARIB 限定受信方式 (0x0005) で B-CAS と同一のため、
        # カードリーダー側からは B-CAS カードと A-CAS カードを区別できない
        # (そもそも実際の A-CAS は受信機内蔵チップ (A-CAS チップ) として実装されていることも多く、
        #  PC/SC リーダーには現れないことがある)
        # そのため TLV キャリアの受信可能性は常に 'unknown' とする
        return 'unknown'
    # required_card == 'unknown': スクランブルされているが CA 記述子から方式を特定できなかった場合
    return 'unknown'


def HasCardType(detected_cards: list[DetectedCard], card_type: CardType) -> bool:
    """指定した種別のカードが 1 枚でも検出されているかを返す"""

    return FindReaderNameForCardType(detected_cards, card_type) is not None


def FindReaderNameForCardType(detected_cards: list[DetectedCard], card_type: CardType) -> str | None:
    """
    指定した種別のカードが挿さっているカードリーダーの名前を返す (見つからなければ None)
    複数見つかった場合は検出順で最初のものを使う (recisdb の --card はリーダー名の完全一致で 1 台を選ぶため)
    """

    for detected_card in detected_cards:
        if detected_card.card_type == card_type:
            return detected_card.reader_name
    return None


def GetReaderNameForRequiredCard(required_card: RequiredCASCard, detected_cards: list[DetectedCard]) -> str | None:
    """
    ある TS の受信に使うべきカードリーダーの名前を返す (カード不要・該当カード無し・判定不能の場合は None)
    ここで返る文字列はそのまま recisdb の `--card` に渡せる (リーダー名の完全一致で照合されるため)
    """

    if required_card == 'B-CAS':
        return FindReaderNameForCardType(detected_cards, CardType.BCAS)
    if required_card == 'C-CAS':
        return FindReaderNameForCardType(detected_cards, CardType.CCAS)
    return None


def CollectCCASPhysicalChannels(carriers: list[CATVCarrierInfo]) -> list[str]:
    """
    スキャン結果から、C-CAS カードが必要な TS が多重されている物理チャンネル名の一覧を返す (重複なし・昇順)
    ここで返る物理チャンネル名は Mirakurun/mirakc の channels 設定に出力される `channel` の値そのもので、
    mirakc の decode-filter に渡される Mustache 変数 {{{channel}}} と一致する
    """

    physical_channels = {
        carrier.physical_channel for carrier in carriers for ts_info in carrier.transport_streams if ts_info.cas.required_card == 'C-CAS'
    }
    return sorted(physical_channels)


def FormatDetectedCardsSummary(detected_cards: list[DetectedCard] | None, detection_error: str | None) -> str | None:
    """
    コンソール表示用に、検出したカードの要約を 1 行にまとめる (カード検出を行わなかった場合は None)
    戻り値は rich マークアップを含まないプレーンテキストのため、呼び出し側で escape() してから表示すること

    Args:
        detected_cards (list[DetectedCard] | None): 検出されたカードの一覧 (カード検出を行わなかった場合は None)
        detection_error (str | None): カード検出に失敗した理由 (成功時は None)

    Returns:
        str | None: 要約文字列 (カード検出を行わなかった場合は None)
    """

    if detected_cards is None:
        return None
    card_parts = [
        f'{detected_card.card_type.value} ({detected_card.reader_name})'
        for detected_card in detected_cards
        if detected_card.card_type in (CardType.BCAS, CardType.CCAS)
    ]
    if len(card_parts) > 0:
        return f'Detected cards: {", ".join(card_parts)}'
    if detection_error is not None:
        return f'No CAS card detected: {detection_error}'
    return 'No CAS card detected in the connected card reader(s).'


class CATVCardAssignmentReportFormatter:
    """
    スキャン結果と PC/SC カードの在庫を突き合わせ、チャンネルごとの受信可能性レポート (CATV.cards.txt) を生成する
    フォーマッター (既存の CATV フォーマッター群と同じ format()/save() のスタイルに揃えている)

    このレポートはスキャン時点のカード接続状況に依存するため、CATV.json には一切出力しない
    (CATV.json に入れるとカードを挿し替えただけで CATV.diff.txt が偽陽性を出してしまうため)
    """

    def __init__(
        self,
        save_file_path: Path,
        carriers: list[CATVCarrierInfo],
        detected_cards: list[DetectedCard],
        detection_error: str | None = None,
    ) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            carriers (list[CATVCarrierInfo]): スキャン結果の CATV キャリア情報のリスト
            detected_cards (list[DetectedCard]): 検出されたカードの一覧 (1 枚も検出できなかった場合は空リスト)
            detection_error (str | None): カード検出に失敗した理由 (成功時は None)
        """

        self._save_file_path = save_file_path
        self._carriers = carriers
        self._detected_cards = detected_cards
        self._detection_error = detection_error

    def format(self) -> str:
        """
        受信可能性レポートとしてフォーマットする

        Returns:
            str: フォーマットされた文字列 (末尾に改行を1つ含む)
        """

        lines = [
            'ISDBScanner CATV: CAS カード割り当てレポート',
            '',
            'このホストに接続されている CAS カードで、スキャンで見つかった各チャンネルをデスクランブルできるかを',
            '突き合わせた結果です。判定結果はスキャン時点のカード接続状況に依存するため、CATV.json には出力していません',
            '(CATV.json に含めると、カードを挿し替えただけでスキャン差分レポートが差分として検出してしまうため)。',
            '',
        ]
        lines.extend(self._BuildDetectedCardsSection())
        lines.append('')
        lines.extend(self._BuildDecodabilitySection())
        lines.append('')
        lines.extend(self._BuildNotesSection())
        return '\n'.join(lines) + '\n'

    def _BuildDetectedCardsSection(self) -> list[str]:
        """検出したカードリーダー/カードの一覧セクションを組み立てる"""

        lines = ['=' * 100, '検出したカードリーダー', '=' * 100]
        if len(self._detected_cards) == 0:
            lines.append('カードリーダー/カードを 1 枚も検出できませんでした。')
            if self._detection_error is not None:
                lines.append(f'理由: {self._detection_error}')
            lines.extend(
                [
                    '',
                    '`isdb-catv-scanner --list-card-readers` を実行すると、接続されている PC/SC カードリーダーと',
                    'それぞれに挿さっているカードの種別を単体で確認できます。まずはそちらで検出状況を確認してください。',
                ]
            )
            return lines

        for index, detected_card in enumerate(self._detected_cards):
            lines.append(f'Reader {index}: {detected_card.reader_name}')
            if detected_card.error is not None:
                lines.append(f'  エラー       : {detected_card.error}')
            else:
                lines.append(f'  Card Type    : {detected_card.card_type.value}')
                if detected_card.ca_system_id is not None:
                    lines.append(f'  CA System ID : 0x{detected_card.ca_system_id:04X} ({detected_card.ca_system_name})')
                if detected_card.card_id is not None:
                    lines.append(f'  Card ID      : {detected_card.card_id}')
            if detected_card.atr is not None:
                lines.append(f'  ATR          : {detected_card.atr}')
        return lines

    def _BuildDecodabilitySection(self) -> list[str]:
        """物理チャンネル×TS ごとの受信可能性テーブルを組み立てる"""

        lines = ['=' * 100, 'チャンネルごとの受信可能性', '=' * 100]

        # (物理チャンネル, TS の説明, required_card, decodable, 使用すべきリーダー名) の行を組み立てる
        rows: list[tuple[str, str, str, str, str]] = []
        for carrier in sorted(self._carriers, key=lambda carrier: carrier.physical_channel):
            if carrier.carrier_type == CarrierType.Empty:
                # 受信できなかったキャリアはそもそも判定対象にならない
                continue
            if carrier.carrier_type == CarrierType.TLV:
                # TLV (4K/8K MMT) キャリアは TS を持たないため、キャリア単位で 1 行にまとめる
                # 4K/8K 放送の限定受信方式は A-CAS 相当だが、CA_system_id が B-CAS と同一で区別できないため常に unknown
                rows.append(
                    (
                        carrier.physical_channel,
                        'TLV/MMT (4K/8K)',
                        'A-CAS',
                        DetermineDecodability('A-CAS', self._detected_cards),
                        '-',
                    )
                )
                continue
            for ts_info in sorted(carrier.transport_streams, key=lambda ts_info: ts_info.tsmf_relative_ts_number or 0):
                if ts_info.tsmf_relative_ts_number is not None:
                    ts_label = f'TSID={ts_info.transport_stream_id:#06x} (rel TS {ts_info.tsmf_relative_ts_number})'
                else:
                    ts_label = f'TSID={ts_info.transport_stream_id:#06x}'
                required_card = ts_info.cas.required_card
                reader_name = GetReaderNameForRequiredCard(required_card, self._detected_cards)
                rows.append(
                    (
                        carrier.physical_channel,
                        ts_label,
                        required_card,
                        DetermineDecodability(required_card, self._detected_cards),
                        reader_name if reader_name is not None else '-',
                    )
                )

        if len(rows) == 0:
            lines.append('受信できたチャンネルがないため、判定対象がありません。')
            return lines

        # 列幅は実際の値の最大長に合わせて動的に決める (リーダー名は最終列なので幅合わせ不要)
        headers = ('Physical Channel', 'Transport Stream', 'Required', 'Decodable', 'Reader')
        widths = [max(len(headers[column]), *(len(row[column]) for row in rows)) for column in range(4)]
        lines.append(
            f'{headers[0]:<{widths[0]}}  {headers[1]:<{widths[1]}}  {headers[2]:<{widths[2]}}  {headers[3]:<{widths[3]}}  {headers[4]}'
        )
        lines.append('  '.join('-' * width for width in widths) + '  ' + '-' * len(headers[4]))
        for row in rows:
            lines.append(f'{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]:<{widths[3]}}  {row[4]}')
        return lines

    def _BuildNotesSection(self) -> list[str]:
        """判定結果を読むうえでの注記セクションを組み立てる"""

        return [
            '=' * 100,
            '注記',
            '=' * 100,
            '* C-CAS: カードが物理的に挿さっていることまでは確認できますが、「契約している CATV 局が発行した',
            '  カードか」まではカード側からは検証できません (CA_system_id は限定受信方式を示すだけで、どの事業者が',
            '  発行したカードかは分かりません)。上表の decodable=yes は「その CATV 局が発行した契約済みの C-CAS',
            '  カードであること」を前提とした判定です。',
            '* A-CAS: 4K/8K (TLV/MMT) 放送の限定受信方式の CA_system_id は ARIB 限定受信方式 (0x0005) で B-CAS と',
            '  同一のため、カードリーダー側からは B-CAS カードと A-CAS カードを区別できません。加えて実際の A-CAS は',
            '  受信機内蔵のチップとして実装されていることも多く、PC/SC リーダーには現れないことがあります。',
            '  このため TLV キャリアの受信可能性は常に unknown としています。',
            '* unknown (required=unknown): スクランブルはされているものの、CA 記述子から限定受信方式を特定できな',
            '  かったチャンネルです。収録時間を延ばして再スキャンすると判定できることがあります。',
            '* カードの共有: pcscd の SHARED 接続により、1 枚のカードを複数のプロセスから同時に使えます',
            '  (libaribb25 は SCardBeginTransaction を使わず、ECM 処理も 1 APDU で完結するため、pcscd の',
            '  リーダー単位の mutex で十分に直列化されます)。B-CAS カード 1 枚で複数チューナーの同時録画が可能です。',
            '* リーダー名: 上表の Reader 列の文字列は PC/SC の SCardListReaders() が返す名前そのもので、',
            '  recisdb の `--card <リーダー名>` にそのまま渡せます (完全一致で照合されるため、一部だけを渡しても',
            '  一致しません)。arib-b25-stream-test は Linux ではリーダーを選択できないため、B-CAS/C-CAS が混在する',
            '  環境のデコーダーには recisdb を使ってください。',
        ]

    def save(self) -> str:
        """
        フォーマットを実行し、結果をファイルに保存する

        Returns:
            str: フォーマットされた文字列
        """

        formatted_str = self.format()
        with open(self._save_file_path, mode='w', encoding='utf-8') as f:
            f.write(formatted_str)

        return formatted_str


def _WriteScript(script_path: Path, script_body: str) -> Path:
    """スクリプトを書き出し、実行権限 (0o755) を付与する"""

    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_body, encoding='utf-8')
    script_path.chmod(SCRIPT_FILE_MODE)
    return script_path


def BuildMirakurunDecoderScript(card_label: str, reader_name: str) -> str:
    """
    Mirakurun の `decoder:` に指定するデコーダーラッパースクリプトの中身を組み立てる

    Args:
        card_label (str): カード種別の表示名 ('B-CAS' / 'C-CAS')
        reader_name (str): このスクリプトが使うカードリーダーの名前 (SCardListReaders() が返す文字列そのもの)

    Returns:
        str: シェルスクリプトの中身 (末尾に改行を1つ含む)
    """

    lines = [
        '#!/bin/sh',
        '# ISDBScanner CATV により自動生成: Mirakurun 用 ' + card_label + ' デコーダーラッパー',
        '#',
        '# Mirakurun の tuners.yml の `decoder:` は、設定された文字列を child_process.spawn(command) で',
        '# そのまま起動するだけで、追加の引数を渡す仕組みがない。このため「どのカードリーダーを使うか」を',
        '# 固定したラッパースクリプトを用意し、それを decoder に指定する必要がある。',
        '#',
        '# B-CAS カードしか挿さっていない環境であれば `decoder: arib-b25-stream-test` でも足りるが、',
        '# arib-b25-stream-test は Linux ではカードリーダーを選択できない',
        '# (リーダー選択用の ini 設定は Windows 専用で、Linux 版には CLI オプションも存在しない)。',
        '# このため B-CAS カードと C-CAS カードが混在する環境では、--card でリーダー名を指定できる',
        '# recisdb をデコーダーとして使う必要がある。',
        '#',
    ]
    lines.extend(RECISDB_DECODE_ARGUMENT_REFERENCE_LINES)
    lines.extend(
        [
            '#',
            '# Mirakurun の tuners.yml での指定例:',
            '#   - name: CATV Tuner (adapter0)',
            '#     types: ["GR", "BS", "CS"]',
            '#     command: dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 0 -P -t 0 -o - <channel>',
            f'#     decoder: /path/to/{MIRAKURUN_BCAS_DECODER_SCRIPT_NAME if card_label == "B-CAS" else MIRAKURUN_CCAS_DECODER_SCRIPT_NAME}',
            '#',
            f'# このスクリプトが使うカードリーダー ({card_label}): {reader_name}',
            '',
            # リーダー名にシングルクォートやスペースが含まれていても壊れないよう shlex.quote() でクォートする
            f'exec recisdb decode --card {shlex.quote(reader_name)} -i - -',
        ]
    )
    return '\n'.join(lines) + '\n'


def BuildMirakcDecodeFilterScript(bcas_reader_name: str, ccas_reader_name: str, ccas_physical_channels: list[str]) -> str:
    """
    mirakc の filters.decode-filter.command に指定する decode-filter スクリプトの中身を組み立てる

    Args:
        bcas_reader_name (str): B-CAS カードが挿さっているカードリーダーの名前
        ccas_reader_name (str): C-CAS カードが挿さっているカードリーダーの名前
        ccas_physical_channels (list[str]): C-CAS カードが必要な物理チャンネル名の一覧 (channels 設定の `channel` の値)

    Returns:
        str: シェルスクリプトの中身 (末尾に改行を1つ含む)
    """

    lines = [
        '#!/bin/sh',
        '# ISDBScanner CATV により自動生成: mirakc 用 decode-filter スクリプト',
        '#',
        '# mirakc の filters.decode-filter.command はチャンネルごとに Mustache でレンダリングされ、',
        '# テンプレート変数 channel_name / channel_type / channel を埋め込める',
        '# (mirakc の docs/config.md「Each Mustache template string defined in the `command` property will be',
        '#  rendered with the following template parameters」および decode-filter の対応表で確認済み)。',
        '# これにより「チャンネルごとに使うカードリーダーを変える」という分岐がフィルタ側で実現できる。',
        '#',
        '# mirakc はレンダリング結果をシェルに渡さず、shell_words で単語分割してそのまま exec する',
        '# (mirakc-core/src/command_util.rs の CommandBuilder::new())。',
        '# シングルクォートは shell_words が解釈してくれるため、値にスペースが含まれても下記の例のように',
        "# '{{{channel_name}}}' とクォートしておけば 1 引数として渡る。",
        '#',
        '# mirakc の config.yml への組み込み例:',
        '#   filters:',
        '#     decode-filter:',
        f"#       command: /path/to/{MIRAKC_DECODE_FILTER_SCRIPT_NAME} '{{{{{{channel_name}}}}}}' '{{{{{{channel_type}}}}}}' '{{{{{{channel}}}}}}'",
        '#',
        '# 分岐は --cas-as-sky (C-CAS が必要なチャンネルを type: SKY として出力するオプション) の指定有無に',
        '# 依存しないよう、channel_type ではなく「C-CAS が必要な物理チャンネル名の明示リスト」で行う。',
        '# 下記のリストは、スキャン結果のうち required_card == "C-CAS" の TS が多重されていた物理チャンネル。',
        '#',
        '# ※ mirakc は type と channel が同じ channels エントリをマージしてしまうため、TSMF 多重チャンネルは',
        '#   相対 TS ごとに channel をユニークな名前 (例: CATV_15#1) へ書き換えて使う必要がある',
        '#   (channels_catv.yml のヘッダーコメント参照)。書き換えた場合は、下記 case のパターンも',
        '#   その名前に合わせて追記すること (このスクリプトには $3 = channel の値がそのまま渡るため)。',
        '#',
    ]
    lines.extend(RECISDB_DECODE_ARGUMENT_REFERENCE_LINES)
    lines.extend(
        [
            '#',
            f'# B-CAS 用リーダー: {bcas_reader_name}',
            f'# C-CAS 用リーダー: {ccas_reader_name}',
            '',
            '# 位置引数: $1 = channel_name / $2 = channel_type / $3 = channel (物理チャンネル名)',
            '# 分岐に使うのは $3 のみだが、独自にカスタマイズしやすいよう channel_name / channel_type も受け取っている',
            'CHANNEL_NAME="$1"',
            'CHANNEL_TYPE="$2"',
            'CHANNEL="$3"',
            '',
        ]
    )

    if len(ccas_physical_channels) > 0:
        lines.append('case "$CHANNEL" in')
        # 物理チャンネル名はグロブとして解釈されないよう shlex.quote() を通す (英数字と _ のみの場合はそのまま)
        pattern = '|'.join(shlex.quote(physical_channel) for physical_channel in ccas_physical_channels)
        lines.extend(
            [
                f'    {pattern})',
                '        # CATV 事業者の自主放送・専門チャンネル (C-CAS で再スクランブルされている)',
                f'        exec recisdb decode --card {shlex.quote(ccas_reader_name)} -i - -',
                '        ;;',
                '    *)',
                '        # 地デジ/BS/CS の再送信チャンネル (元の B-CAS スクランブルのまま)',
                f'        exec recisdb decode --card {shlex.quote(bcas_reader_name)} -i - -',
                '        ;;',
                'esac',
            ]
        )
    else:
        lines.extend(
            [
                '# 今回のスキャン結果には C-CAS が必要なチャンネルが 1 つも無かったため、常に B-CAS 用リーダーを使う',
                '# (C-CAS チャンネルが見つかった状態で再スキャンすると、ここに case 分岐が生成される)',
                f'exec recisdb decode --card {shlex.quote(bcas_reader_name)} -i - -',
            ]
        )
    return '\n'.join(lines) + '\n'


def WriteDecoderScripts(
    output_dir: Path,
    carriers: list[CATVCarrierInfo],
    detected_cards: list[DetectedCard],
) -> list[Path]:
    """
    B-CAS カードと C-CAS カードの両方が検出された場合にのみ、カードを使い分けるためのデコーダースクリプトを生成する

    - Mirakurun/decoder-bcas.sh / Mirakurun/decoder-ccas.sh:
      Mirakurun の `decoder:` に指定するラッパー (Mirakurun は decoder に引数を渡せないため必要)
    - mirakc/decode-filter.sh:
      mirakc の filters.decode-filter.command に指定するスクリプト (チャンネルごとに使うリーダーを切り替える)

    片方のカードしか無い環境では、リーダーを選ぶ必要がそもそも無い (Mirakurun なら arib-b25-stream-test、
    mirakc なら既定の decode-filter 設定で足りる) ため、スクリプトは生成しない

    Args:
        output_dir (Path): 出力先ディレクトリ (この直下の Mirakurun/ と mirakc/ に生成する)
        carriers (list[CATVCarrierInfo]): スキャン結果の CATV キャリア情報のリスト
        detected_cards (list[DetectedCard]): 検出されたカードの一覧

    Returns:
        list[Path]: 生成したスクリプトのパスの一覧 (生成しなかった場合は空リスト)
    """

    bcas_reader_name = FindReaderNameForCardType(detected_cards, CardType.BCAS)
    ccas_reader_name = FindReaderNameForCardType(detected_cards, CardType.CCAS)
    if bcas_reader_name is None or ccas_reader_name is None:
        return []

    return [
        _WriteScript(
            output_dir / 'Mirakurun' / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME,
            BuildMirakurunDecoderScript('B-CAS', bcas_reader_name),
        ),
        _WriteScript(
            output_dir / 'Mirakurun' / MIRAKURUN_CCAS_DECODER_SCRIPT_NAME,
            BuildMirakurunDecoderScript('C-CAS', ccas_reader_name),
        ),
        _WriteScript(
            output_dir / 'mirakc' / MIRAKC_DECODE_FILTER_SCRIPT_NAME,
            BuildMirakcDecodeFilterScript(bcas_reader_name, ccas_reader_name, CollectCCASPhysicalChannels(carriers)),
        ),
    ]


def IsExecutable(script_path: Path) -> bool:
    """スクリプトに実行権限が付与されているかを返す (テストや動作確認用のヘルパー)"""

    return bool(script_path.stat().st_mode & stat.S_IXUSR)

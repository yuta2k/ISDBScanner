# ruff: noqa: UP013

import copy
import json
from io import StringIO
from pathlib import Path
from typing import NotRequired

from ruamel.yaml import YAML
from typing_extensions import TypedDict

from isdb_scanner.catv.card_assignment import (
    MIRAKC_DECODE_FILTER_SCRIPT_NAME,
    MIRAKURUN_BCAS_DECODER_SCRIPT_NAME,
    MIRAKURUN_CCAS_DECODER_SCRIPT_NAME,
    FindReaderNameForCardType,
)
from isdb_scanner.catv.cards import CardType, DetectedCard
from isdb_scanner.catv.constants import (
    CATV_FREQUENCY_TABLE,
    BuildDvbv5ConfEntryLines,
    CarrierType,
    CATVCarrierInfo,
    CATVTransportStreamInfo,
    PreferredSource,
)
from isdb_scanner.catv.tuner import CATVTuner
from isdb_scanner.constants import TransportStreamInfo, TransportStreamInfoList
from isdb_scanner.tuner import ISDBTuner


class CATVBaseFormatter:
    """
    CATV スキャン結果 (CATVCarrierInfo のリスト) 用フォーマッターの基底クラス
    既存の isdb_scanner/formatter.py の BaseFormatter は地上波/BS/CS の3リスト固定のコンストラクタ引数を取るため
    シグネチャが合わず、CATV ではこの独自の基底クラスを使う (出力スタイルは既存フォーマッターを参考にしている)
    """

    def __init__(self, save_file_path: Path, carriers: list[CATVCarrierInfo]) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            carriers (list[CATVCarrierInfo]): スキャン結果の CATV キャリア情報のリスト
        """

        self._save_file_path = save_file_path
        self._carriers = carriers

    def format(self) -> str:
        """フォーマットを実行する (サブクラスで実装する)"""

        raise NotImplementedError

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


def _DumpChannelsYaml(channels: list) -> str:
    """チャンネル一覧を Mirakurun/mirakc 向けの共通スタイル (インデント幅など) で YAML 文字列に変換する"""

    string_io = StringIO()
    yaml = YAML()
    yaml.width = 1000
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.dump(channels, string_io)
    string_io.seek(0)
    return string_io.getvalue()


# 全角英数字・記号 (U+FF01〜U+FF5E) → 対応する ASCII (U+0021〜U+007E) への変換テーブル
## 全角と半角の差は一律 0xFEE0 (U+FF01 - U+0021) なので、その分だけコードポイントをずらす
## 全角スペース (U+3000) のみ半角スペース (U+0020) へ個別に対応付ける
_FULLWIDTH_TO_HALFWIDTH_TABLE: dict[int, int] = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_TO_HALFWIDTH_TABLE[0x3000] = 0x0020


def NormalizeChannelName(name: str) -> str:
    """
    チャンネル名に含まれる全角英数字・記号 (U+FF01〜U+FF5E) を対応する ASCII に、全角スペース (U+3000) を半角スペースに正規化する
    (放送局側の SDT/NIT では英数字が全角で符号化されていることが多く、レコーダー UI 上で見づらいのを緩和するための任意処理)
    ひらがな・カタカナ・漢字などの全角文字はそのまま維持し、ASCII に1対1で対応する文字のみを変換する
    """

    return name.translate(_FULLWIDTH_TO_HALFWIDTH_TABLE)


def _BuildExcludedTLVChannelLines(carriers: list[CATVCarrierInfo], recorder_name: str) -> list[str]:
    """
    TLV (4K/8K MMT) キャリアを出力から除外した旨の注記コメント行を組み立てる (該当キャリアがなければ空リスト)
    MH-SDT からサービス名を取得できている場合は、どのチャンネルが除外されたのかを特定しやすいよう併記する
    """

    excluded_tlv_carriers = sorted(
        (carrier for carrier in carriers if carrier.carrier_type == CarrierType.TLV),
        key=lambda carrier: carrier.physical_channel,
    )
    if len(excluded_tlv_carriers) == 0:
        return []

    lines = [
        '#',
        f'# 以下の物理チャンネルは TLV/MMT (4K/8K) キャリアのため、TS ベースの {recorder_name} では扱えず除外しています:',
    ]
    for carrier in excluded_tlv_carriers:
        service_names = [
            service.service_name
            for service in (carrier.mmt.services if carrier.mmt is not None else [])
            if service.service_name != 'Unknown'
        ]
        if len(service_names) > 0:
            lines.append(f'#   {carrier.physical_channel}: {", ".join(service_names)}')
        else:
            lines.append(f'#   {carrier.physical_channel}')
    return lines


class CATVJSONFormatter(CATVBaseFormatter):
    """CATV キャリアのスキャン解析結果 (CATVCarrierInfo のリスト) を JSON データとして保存するフォーマッター"""

    def format(self) -> str:
        """
        JSON データとしてフォーマットする (物理チャンネル名をキーにした dict)

        Returns:
            str: フォーマットされた文字列
        """

        channels_dict = {carrier.physical_channel: carrier.model_dump(mode='json') for carrier in self._carriers}
        return json.dumps(channels_dict, indent=4, ensure_ascii=False)


class CATVDvbv5ConfFormatter(CATVBaseFormatter):
    """
    CATV キャリアのスキャン結果のうち、受信できた (Empty でない) キャリアのみを dvbv5 形式の conf ファイルとして保存するフォーマッター
    ここで出力される conf ファイルは dvbv5-zap 標準の書式で、
    将来 mirakc などから `dvbv5-zap -c <このファイル> ...` で直接選局できるようにすることを目的としている
    (CATVTuner が内部的に生成する全チャンネル分の conf とは異なり、こちらは実際にロックできたチャンネルのみを含む)
    """

    def format(self) -> str:
        """
        受信できたキャリアのみを dvbv5 形式の conf としてフォーマットする

        Returns:
            str: フォーマットされた文字列 (受信できたキャリアが1つもない場合は空文字列)
        """

        lines: list[str] = []
        for carrier in self._carriers:
            # Empty キャリア (受信不可、または NULL パケットのみ) は出力対象外
            if carrier.carrier_type == CarrierType.Empty:
                continue
            frequency = CATV_FREQUENCY_TABLE.get(carrier.physical_channel)
            if frequency is None:
                # 周波数プランに存在しない物理チャンネル名 (通常到達しないはずだが念のためスキップ)
                continue
            lines.extend(BuildDvbv5ConfEntryLines(carrier.physical_channel, frequency))

        if len(lines) == 0:
            return ''
        return '\n'.join(lines) + '\n'


# 映像サービスとみなすサービス種別 (既存 isdb_scanner/constants.py の ServiceInfo.isVideoServiceType() と同じ判定)
## 0x01: デジタルTVサービス / 0xA5: プロモーション映像サービス / 0xAD: 超高精細度4K専用TVサービス
_VIDEO_SERVICE_TYPES = (0x01, 0xA5, 0xAD)


def _BuildCATVChannelName(ts_info: CATVTransportStreamInfo) -> str:
    """
    CATV TS (TSMF の場合は分離後の相対 TS) 1本分の、Mirakurun/mirakc チャンネル名を組み立てる
    BS/CS の再送信では network_name はネットワーク名 (例: "ＢＳデジタル") で BS/CS の全 TS で共通のため、
    チャンネル名としては TSMF 分離後の SDT から得られる各 TS のサービス名 (映像サービス優先) を使う
    地上波の再送信であれば NIT から得られた network_name (TS 名 = 放送局名) が入っているはずなのでそれを使う
    CATV 事業者の自主放送などで network_name が得られなかった場合は、先頭サービスのサービス名で代用する
    それも取れない (サービスが1つも解析できなかった) 場合は、物理チャンネル名 (+ TSMF 相対 TS 番号) を機械的な名前として使う
    """

    if ts_info.retransmission_source in ('BS', 'CS'):
        named_services = [service for service in ts_info.services if service.service_name != 'Unknown']
        video_services = [service for service in named_services if service.service_type in _VIDEO_SERVICE_TYPES]
        if len(video_services) > 0:
            return video_services[0].service_name
        if len(named_services) > 0:
            return named_services[0].service_name
    if ts_info.network_name != 'Unknown':
        return ts_info.network_name
    if len(ts_info.services) > 0:
        return ts_info.services[0].service_name
    if ts_info.tsmf_relative_ts_number is not None:
        return f'{ts_info.physical_channel}#{ts_info.tsmf_relative_ts_number}'
    return ts_info.physical_channel


def _BuildCATVChannelType(ts_info: CATVTransportStreamInfo, cas_as_sky: bool) -> str:
    """
    CATV TS (TSMF の場合は分離後の相対 TS) 1本分の、Mirakurun/mirakc チャンネル type を組み立てる
    BS/CS の再送信 (トランスモジュレーション) は type: BS / type: CS として出力し、
    地上波再送信・自主放送・再送信元不明の TS は type: GR として出力する
    cas_as_sky が True の場合、C-CAS/A-CAS が必要な TS は再送信元に関わらず type: SKY (本来は SPHD 用の第4の type) として出力する
    """

    if cas_as_sky is True and ts_info.cas.required_card in ('C-CAS', 'A-CAS'):
        return 'SKY'
    if ts_info.retransmission_source == 'BS':
        return 'BS'
    if ts_info.retransmission_source == 'CS':
        return 'CS'
    return 'GR'


def _IterReceivableTransportStreams(
    carriers: list[CATVCarrierInfo],
    exclude_pay_tv: bool = False,
    bcas_only: bool = False,
) -> list[tuple[CATVCarrierInfo, CATVTransportStreamInfo]]:
    """
    レコーダー向け出力の対象になる (carrier_type が TSMF/SingleTS の) キャリアから、多重されている各 TS を
    (キャリア, TS情報) のペアの一覧として取り出す。物理チャンネル名昇順・TSMF相対TS番号昇順でソートする
    TLV (4K/8K MMT) キャリアは TS ベースの Mirakurun/mirakc では選局・視聴できないため対象外
    Empty (受信不可) キャリアはそもそも transport_streams が空なので自然に除外される

    bcas_only が True の場合、B-CAS カードで受信できない (C-CAS/A-CAS が必要、または CAS 種別を特定できない) TS を除外する
    exclude_pay_tv が True の場合、有料放送サービスを除外する。除外規則は既存 isdb_scanner/formatter.py の
    BaseFormatter (46-55 行付近) の複製:
    - CS 再送信はショップチャンネルと QVC 以外の全サービスが有料放送 (スカパー！) として運用されているため全サービスを除外する
    - それ以外は is_free でない (= 有料放送の) サービスを除外する
    - その結果 TS 内に独立データ放送 (service_type 0xC0) 以外のサービスが残らなかった TS は丸ごと除外する
    サービス一覧を書き換える場合は deepcopy した TS 情報を返すため、渡された carriers は破壊されない
    """

    pairs: list[tuple[CATVCarrierInfo, CATVTransportStreamInfo]] = []
    for carrier in carriers:
        if carrier.carrier_type not in (CarrierType.TSMF, CarrierType.SingleTS):
            continue
        for ts_info in carrier.transport_streams:
            # B-CAS で受信できない TS を除外
            if bcas_only is True and ts_info.cas.required_card not in ('none', 'B-CAS'):
                continue
            # 有料放送サービスを除外し、その結果映像/音声サービスが残らなかった TS を除外
            if exclude_pay_tv is True:
                ts_info = copy.deepcopy(ts_info)
                if ts_info.retransmission_source == 'CS':
                    ts_info.services = []
                else:
                    ts_info.services = [service for service in ts_info.services if service.is_free is True]
                if len([service for service in ts_info.services if service.service_type != 0xC0]) == 0:
                    continue
            pairs.append((carrier, ts_info))

    return sorted(
        pairs,
        key=lambda pair: (pair[0].physical_channel, pair[1].tsmf_relative_ts_number or 0),
    )


def _PrepareNativeTsInfos(
    tr_ts_infos: list[TransportStreamInfo],
    bs_ts_infos: list[TransportStreamInfo],
    cs_ts_infos: list[TransportStreamInfo],
    exclude_pay_tv: bool,
) -> list[TransportStreamInfo]:
    """
    ネイティブ (ISDB-T/ISDB-S 直結) 地上波/BS/CS の TS 情報をレコーダー向け YAML 出力用に前処理し、
    地上波 → BS → CS の順に結合した1つのリストとして返す (それぞれ物理チャンネル名昇順でソートする)

    有料放送の除外規則は既存の isdb_scanner/formatter.py の BaseFormatter (46-55 行付近) +
    Mirakurun/mirakc の TS スキップ規則 (346-357 行 / 636-648 行付近) の複製:
    - 地上波は実運用上有料放送は存在しないが念のため is_free でない (= 有料放送の) サービスを除外する。
      ただし TS 自体のスキップ (独立データ放送のみになった TS の除外) はネイティブ実装同様 BS/CS のみに適用し、
      地上波はサービスの有無に関わらず TS を残す (ネイティブ実装と同じ挙動)
    - BS は is_free でない (= 有料放送の) サービスを除外する
    - CS はショップチャンネルと QVC 以外の全サービスが有料放送 (スカパー！) として運用されている上、
      無料とはいえわざわざ通販チャンネルを見る人がいるとも思えないので全てのサービスを除外する
    - BS/CS はその結果 TS 内に独立データ放送 (service_type 0xC0) 以外のサービスが残らなかった TS を丸ごと除外する
      (有料放送の TS に無料独立データ放送が含まれる場合があるため (WOWOW など)、それらを除いてから判定する)

    既存 BaseFormatter は渡されたリストを in-place で破壊するが、こちらは複数フォーマッターに同じリストを
    渡しても安全なよう deepcopy してから処理する
    """

    tr_ts_infos = sorted(copy.deepcopy(tr_ts_infos), key=lambda x: x.physical_channel)
    bs_ts_infos = sorted(copy.deepcopy(bs_ts_infos), key=lambda x: x.physical_channel)
    cs_ts_infos = sorted(copy.deepcopy(cs_ts_infos), key=lambda x: x.physical_channel)
    if exclude_pay_tv is True:
        for tr_ts_info in tr_ts_infos:
            tr_ts_info.services = [service for service in tr_ts_info.services if service.is_free is True]
        for bs_ts_info in bs_ts_infos:
            bs_ts_info.services = [service for service in bs_ts_info.services if service.is_free is True]
        for cs_ts_info in cs_ts_infos:
            cs_ts_info.services = []
        # TS 自体のスキップ (0xC0 判定) はネイティブ実装同様 BS/CS のみに適用し、地上波はそのまま残す
        bs_ts_infos = [
            ts_info for ts_info in bs_ts_infos if len([s for s in ts_info.services if s.service_type != 0xC0]) > 0
        ]
        cs_ts_infos = [
            ts_info for ts_info in cs_ts_infos if len([s for s in ts_info.services if s.service_type != 0xC0]) > 0
        ]
    return tr_ts_infos + bs_ts_infos + cs_ts_infos


def _NativeBroadcastCategory(ts_info: TransportStreamInfo) -> str:
    """ネイティブ TS の放送種別 (broadcast_type) を、CATV 再送信元と対応付けるためのカテゴリ ('Terrestrial'/'BS'/'CS') に正規化する"""

    if ts_info.broadcast_type == 'Terrestrial':
        return 'Terrestrial'
    if ts_info.broadcast_type == 'BS':
        return 'BS'
    return 'CS'  # CS1 / CS2 はまとめて 'CS' 扱い


def _BuildDuplicateKeys(
    catv_pairs: list[tuple[CATVCarrierInfo, CATVTransportStreamInfo]],
    native_ts_infos: list[TransportStreamInfo],
) -> set[tuple[str, int]]:
    """
    CATV 再送信 TS とネイティブ TS が同一 TS を指している (重複している) 組を検出し、その (放送種別カテゴリ, TSID) の集合を返す
    再送信元が 'Terrestrial'/'BS'/'CS' の CATV 再送信 TS と、ネイティブ TS を (カテゴリ, transport_stream_id) の一致で突き合わせる
    ('Terrestrial'↔地上波, 'BS'↔BS, 'CS'↔CS1/CS2)
    """

    catv_keys = {
        (ts_info.retransmission_source, ts_info.transport_stream_id)
        for _, ts_info in catv_pairs
        if ts_info.retransmission_source in ('Terrestrial', 'BS', 'CS')
    }
    native_keys = {
        (_NativeBroadcastCategory(ts_info), ts_info.transport_stream_id) for ts_info in native_ts_infos
    }
    return catv_keys & native_keys


class NativeJSONFormatter:
    """
    ネイティブ (ISDB-T/ISDB-S 直結) 地上波/BS/CS のスキャン解析結果 (TransportStreamInfo のリスト) を JSON データとして保存するフォーマッター
    出力形式は既存の isdb_scanner/formatter.py の JSONFormatter が出力する Channels.json の
    "Terrestrial"/"BS"/"CS" キーの値 (TransportStreamInfoList の JSON 配列) と同一
    (Terrestrial.json / BS.json / CS.json のいずれの出力にも使える)
    既存の慣習 (JSON のみ常に全チャンネルを出力) に合わせ、有料放送のフィルタリングは行わない
    """

    def __init__(self, save_file_path: Path, ts_infos: list[TransportStreamInfo]) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            ts_infos (list[TransportStreamInfo]): スキャン結果の地上波・BS・CS いずれかの TS 情報のリスト
        """

        self._save_file_path = save_file_path
        self._ts_infos = ts_infos

    def format(self) -> str:
        """
        JSON データとしてフォーマットする

        Returns:
            str: フォーマットされた文字列
        """

        return json.dumps(TransportStreamInfoList(root=self._ts_infos).model_dump(mode='json'), indent=4, ensure_ascii=False)

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


# 後方互換のためのエイリアス (旧称。BS/CS 専用だった頃の名前で、地上波にも使えるよう NativeJSONFormatter へ改称した)
SatelliteJSONFormatter = NativeJSONFormatter


CATVMirakurunChannel = TypedDict(
    'CATVMirakurunChannel',
    {
        'name': str,
        'type': str,
        'channel': str,
        'tsmfRelTs': NotRequired[int],
        'isDisabled': bool,
    },
)


# ネイティブ地上波/BS/CS 用の Mirakurun チャンネルエントリ (CATV 用と異なり satellite キーで --tsid 等を指定する)
CATVSatelliteMirakurunChannel = TypedDict(
    'CATVSatelliteMirakurunChannel',
    {
        'name': str,
        'type': str,
        'channel': str,
        'satellite': str,
        'isDisabled': bool,
    },
)


class CATVMirakurunChannelsYmlFormatter(CATVBaseFormatter):
    """
    CATV キャリアのスキャン解析結果 (CATVCarrierInfo のリスト) から Mirakurun 用の channels.yml (CATV 分) を生成するフォーマッター

    Mirakurun は `tsmfRelTs` (1-15) キーをネイティブサポートしており (TSFilter.ts が TS 内で TSMF ヘッダを見て
    自前で分離する)、TSMF 分離用の外部フィルタ (isdb-tsmf-split) を挟む必要がない。そのためチューナー
    (tuners.yml) 側は dvbv5-zap で対象の物理チャンネルの全 PID をそのまま選局するだけでよく、本フォーマッターの
    出力ファイル冒頭にその旨と tuners.yml のエントリ例をコメントとして書き出す

    対象は carrier_type が TSMF/SingleTS の TS のみで、TLV (4K/8K MMT) キャリアと Empty (受信不可) キャリアは対象外
    (TLV は TS ベースの Mirakurun では扱えないため、除外した物理チャンネルをコメントで注記する)

    ネイティブ (ISDB-T/ISDB-S 直結) 地上波/BS/CS の TS 情報 (tr_ts_infos / bs_ts_infos / cs_ts_infos) が渡された場合は、
    CATV エントリの後に地上波/BS/CS のチャンネルエントリも統合して出力する (既存 MirakurunChannelsYmlFormatter の GR/BS/CS 分の移植)

    prefer が指定された場合、CATV 再送信 TS とネイティブ TS が同一 TS (放送種別カテゴリ, TSID が一致) で重複したとき、
    優先しない側のエントリを isDisabled: true にして出力する (エントリ自体は残す)
    normalize_names が True の場合、出力する name フィールドの全角英数字・記号を半角に正規化する
    """

    def __init__(
        self,
        save_file_path: Path,
        carriers: list[CATVCarrierInfo],
        tr_ts_infos: list[TransportStreamInfo] | None = None,
        bs_ts_infos: list[TransportStreamInfo] | None = None,
        cs_ts_infos: list[TransportStreamInfo] | None = None,
        exclude_pay_tv: bool = False,
        bcas_only: bool = False,
        cas_as_sky: bool = False,
        prefer: PreferredSource | None = None,
        normalize_names: bool = False,
    ) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            carriers (list[CATVCarrierInfo]): スキャン結果の CATV キャリア情報のリスト
            tr_ts_infos (list[TransportStreamInfo] | None): ネイティブ地上波のスキャン結果の TS 情報 (未指定なら地上波エントリを出力しない)
            bs_ts_infos (list[TransportStreamInfo] | None): ネイティブ BS のスキャン結果の TS 情報 (未指定なら BS エントリを出力しない)
            cs_ts_infos (list[TransportStreamInfo] | None): ネイティブ CS のスキャン結果の TS 情報 (未指定なら CS エントリを出力しない)
            exclude_pay_tv (bool): 有料放送チャンネルを出力から除外するか (CATV エントリ・ネイティブエントリの両方に適用される)
            bcas_only (bool): B-CAS カードで受信できない (C-CAS/A-CAS が必要、または CAS 種別不明の) CATV エントリを出力から除外するか
            cas_as_sky (bool): C-CAS/A-CAS が必要な CATV エントリを type: SKY として出力するか
            prefer (PreferredSource | None): CATV 再送信とネイティブで重複した TS のどちらを有効にするか (もう一方を isDisabled にする)
            normalize_names (bool): 出力する name フィールドの全角英数字・記号を半角に正規化するか
        """

        super().__init__(save_file_path, carriers)
        self._exclude_pay_tv = exclude_pay_tv
        self._bcas_only = bcas_only
        self._cas_as_sky = cas_as_sky
        self._prefer = prefer
        self._normalize_names = normalize_names
        self._native_ts_infos = _PrepareNativeTsInfos(tr_ts_infos or [], bs_ts_infos or [], cs_ts_infos or [], exclude_pay_tv)

    def _name(self, raw_name: str) -> str:
        """normalize_names が True の場合のみ name を半角へ正規化する"""

        return NormalizeChannelName(raw_name) if self._normalize_names else raw_name

    def format(self) -> str:
        """
        Mirakurun の channels.yml (CATV 分 + ネイティブ地上波/BS/CS 分) としてフォーマットする

        Returns:
            str: フォーマットされた文字列
        """

        header_lines = [
            '# ISDBScanner CATV: Mirakurun 用 channels.yml (CATV トランスモジュレーション分)',
            '#',
            '# Mirakurun は tsmfRelTs (1-15) キーをネイティブサポートしているため (TSFilter.ts が TS 内の',
            '# TSMF 多重フレームヘッダ (PID 0x002F) を見て自前で分離する)、TSMF 分離用の外部フィルタ',
            '# (isdb-tsmf-split) を挟む必要はない。チューナー側 (tuners.yml) は dvbv5-zap でこのキャリアの',
            '# 全 PID (-P) をそのまま選局するだけでよい。tuners.yml のエントリ例:',
            '#',
            '#   - name: CATV Tuner (adapter0)',
            '#     types:',
            '#       - GR',
            '#       - BS',
            '#       - CS',
            '#     command: dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 0 -P -t 0 -o - <channel>',
            '#     isDisabled: false',
            '#',
            '# BS/CS 再送信 (トランスモジュレーション) のチャンネルは type: BS / type: CS として出力されるため、',
            '# CATV チューナーの types にはこのファイルに現れる全 type (GR/BS/CS、--cas-as-sky 使用時は SKY も) を',
            '# 列挙すること。types に列挙しないと該当チャンネルの選局にこのチューナーが使われない',
            '#',
            '# `-t 0` は録画時間 0 秒 (dvbv5-zap 自身のタイムアウトに委ねず、Mirakurun 側がプロセスの生存期間を',
            '# 管理する) を意図した値だが、dvbv5-zap の `-t` はロックタイムアウトと録画時間を兼ねる特殊仕様のため、',
            '# 実運用に組み込む際は実機で `-t 0` の挙動 (無制限に出力し続けるか) を確認してから使うこと',
        ]
        header_lines.extend(_BuildExcludedTLVChannelLines(self._carriers, 'Mirakurun'))
        if len(self._native_ts_infos) > 0:
            header_lines.extend([
                '#',
                '# 末尾の channel が T** / BS**_* / CS** 形式のエントリはネイティブ (地上波/衛星アンテナ直結) 分で、recisdb で選局する。',
                '# satellite キーは本来 SPHD 向けの受信衛星指定用パラメータだが、BS/CS の TSID (--tsid) 指定用に転用している',
                '# (地上波では TSID 指定が不要なため satellite は半角スペース 1 個にしている)。',
                '# ISDB-S/ISDB-T チューナー側の tuners.yml のエントリ例:',
                '#',
                '#   - name: ISDB-S Tuner',
                '#     types:',
                '#       - BS',
                '#       - CS',
                '#     command: recisdb tune --device /dev/px4video0 --channel <channel><satellite>-',
                '#     isDisabled: false',
                '#',
                '# ※ PT1/PT2/PT3 (chardev) は TSID 選局に対応していないため、<satellite> を含めず',
                '#   `recisdb tune --device /dev/pt3video1 --channel <channel> -` のようなコマンドにすること',
                '# ※ CATV 再送信の地上波/BS/CS チャンネルとネイティブの地上波/BS/CS チャンネルは type が同じ (GR/BS/CS) になるため、',
                '#   Mirakurun はどちらのチューナー (dvbv5-zap / recisdb) で選局すべきかを type からは区別できない。',
                '#   両方を併用する場合は、どちらか一方のエントリを isDisabled: true にするなどの調整が必要',
            ])

        # CATV 再送信 TS の一覧を取り出し、prefer 指定時は重複した (放送種別カテゴリ, TSID) の集合を求めておく
        catv_pairs = _IterReceivableTransportStreams(self._carriers, self._exclude_pay_tv, self._bcas_only)
        duplicate_keys = (
            _BuildDuplicateKeys(catv_pairs, self._native_ts_infos) if self._prefer is not None else set()
        )
        # prefer 指定時、重複したエントリのうち優先しない側を isDisabled: true にする旨をヘッダーに追記する
        if self._prefer is not None and len(duplicate_keys) > 0:
            disabled_side = 'ネイティブ (recisdb)' if self._prefer == PreferredSource.CATV else 'CATV 再送信 (dvbv5-zap)'
            header_lines.extend([
                '#',
                f'# --prefer={self._prefer.value} が指定されたため、CATV 再送信とネイティブで重複した TS のうち',
                f'# {disabled_side} 側のエントリを isDisabled: true にして出力しています (エントリ自体は残しています)',
            ])

        channels: list[CATVMirakurunChannel | CATVSatelliteMirakurunChannel] = []
        for carrier, ts_info in catv_pairs:
            channel: CATVMirakurunChannel = {
                'name': self._name(_BuildCATVChannelName(ts_info)),
                # 再送信元に応じて BS/CS 再送信は type: BS / type: CS、それ以外は type: GR とする
                # (cas_as_sky 指定時は C-CAS/A-CAS が必要なチャンネルのみ type: SKY になる)
                'type': _BuildCATVChannelType(ts_info, self._cas_as_sky),
                'channel': carrier.physical_channel,
            }
            if ts_info.tsmf_relative_ts_number is not None:
                channel['tsmfRelTs'] = ts_info.tsmf_relative_ts_number
            # prefer='native' の場合、ネイティブ側と重複した CATV 再送信エントリを disable する
            channel['isDisabled'] = (
                self._prefer == PreferredSource.NATIVE
                and (ts_info.retransmission_source, ts_info.transport_stream_id) in duplicate_keys
            )
            channels.append(channel)

        # ネイティブ地上波/BS/CS のチャンネルエントリを追記 (既存 MirakurunChannelsYmlFormatter の GR/BS/CS 分 (345-387 行付近) の移植)
        for native_ts_info in self._native_ts_infos:
            # prefer='catv' の場合、CATV 再送信側と重複したネイティブエントリを disable する
            is_disabled = (
                self._prefer == PreferredSource.CATV
                and (_NativeBroadcastCategory(native_ts_info), native_ts_info.transport_stream_id) in duplicate_keys
            )
            if native_ts_info.broadcast_type == 'Terrestrial':
                # 地上波はネットワーク名 (= TS 名 = 放送局名) を name に、type: GR、channel は recisdb フォーマット (ex: T27) を使う
                native_channel: CATVSatelliteMirakurunChannel = {
                    'name': self._name(native_ts_info.network_name),
                    'type': 'GR',
                    'channel': native_ts_info.physical_channel_recisdb,
                    # 地上波では TSID 指定が不要だが、Mirakurun のプレースホルダーは単なる文字列置換で実装されており
                    # "satellite" が空だと条件分岐が成立せずチューナーコマンド内の <satellite> が置換されずに残ってしまうため、
                    # 空文字列ではなく半角スペース 1 個を入れている (詳細は isdb_scanner/formatter.py の MirakurunChannelsYmlFormatter を参照)
                    'satellite': ' ',
                    'isDisabled': is_disabled,
                }
            else:
                # BS・CS では追加の引数として --tsid (当該 TS の TSID) を指定し、当該トランスポンダで送出中の TS を明示的に TSID で選局する
                ## 実際に発行されるチューナーコマンドは --channel BS23_2 --tsid 18803 や --channel CS04 --tsid 28736 のようになる
                ## recisdb の V4L-DVB (DVBv5) 経路では ISDB-S のロックに TSID (DTV_STREAM_ID) の指定が必須で、
                ## BS は recisdb 内蔵の相対 TS テーブルで TSID を自動補完できるが、CS は補完テーブルが無いため --tsid の明示指定が必須
                native_channel = {
                    'name': self._name(native_ts_info.physical_channel),
                    'type': 'BS' if native_ts_info.broadcast_type == 'BS' else 'CS',
                    'channel': native_ts_info.physical_channel_recisdb,
                    # 本来は SPHD 向けの受信衛星指定用パラメータだが、mirakc における extra-args の代わりに TSID 指定用のパラメータに転用している
                    # 意図的に先頭と末尾に半角スペースを入れている (Mirakurun のプレースホルダー置換の仕様上の都合。
                    # 詳細は isdb_scanner/formatter.py の MirakurunChannelsYmlFormatter を参照)
                    'satellite': f' --tsid {native_ts_info.transport_stream_id} ',
                    'isDisabled': is_disabled,
                }
            channels.append(native_channel)

        return '\n'.join(header_lines) + '\n\n' + _DumpChannelsYaml(channels)


CATVMirakcChannel = TypedDict(
    'CATVMirakcChannel',
    {
        'name': str,
        'type': str,
        'channel': str,
        'extra-args': str,
        'disabled': bool,
    },
)


class CATVMirakcConfigYmlFormatter(CATVBaseFormatter):
    """
    CATV キャリアのスキャン解析結果 (CATVCarrierInfo のリスト) から mirakc 用の channels 設定断片 (CATV 分) を生成するフォーマッター

    mirakc は Mirakurun の tsmfRelTs のようなネイティブ TSMF 分離機能を持たないため、TSMF キャリアの各相対 TS を
    単一の TS として扱うには、選局コマンドの標準出力を `isdb-tsmf-split --rel-ts <N>` にパイプして分離する必要がある
    mirakc の tuner.command は Mustache テンプレートで channels 側の `channel` / `extra-args` を
    `{{{channel}}}` / `{{{extra_args}}}` に埋め込む方式 (既存 MirakcConfigYmlFormatter が BS の --tsid 指定に
    使っているのと同じ仕組み) のため、本フォーマッターでは TSMF の各エントリの extra-args に相対 TS 番号を
    設定し、tuners 側のコマンドテンプレートで isdb-tsmf-split に渡す運用を想定する
    (SingleTS のエントリは extra-args が空文字列になるため、isdb-tsmf-split を挟まずそのまま使われる想定)

    完全な mirakc の config.yml 全体ではなく、channels 部分の断片 + 運用方法の説明コメントのみを出力する
    (tuners 側の具体的な command テンプレートは環境 (アダプタ数・isdb-tsmf-split の組み込み方) によって変わるため、
    コメント内の例として示すに留める)

    ネイティブ (ISDB-T/ISDB-S 直結) 地上波/BS/CS の TS 情報 (tr_ts_infos / bs_ts_infos / cs_ts_infos) が渡された場合は、
    CATV エントリの後に地上波/BS/CS のチャンネルエントリも統合して出力する (既存 MirakcConfigYmlFormatter の GR/BS/CS 分の移植)
    CATV エントリの extra-args は TSMF 相対 TS 番号、ネイティブ BS/CS エントリの extra-args は `--tsid <TSID>` と意味が異なるため、
    CATV 再送信の BS/CS チャンネル (type: BS/CS だが extra-args は TSMF 相対 TS 番号) とネイティブ BS/CS を併用する場合は
    チューナーの振り分けに注意が必要 (ヘッダーコメント参照)

    prefer が指定された場合、CATV 再送信 TS とネイティブ TS が同一 TS (放送種別カテゴリ, TSID が一致) で重複したとき、
    優先しない側のエントリを disabled: true にして出力する (エントリ自体は残す)
    normalize_names が True の場合、出力する name フィールドの全角英数字・記号を半角に正規化する
    """

    def __init__(
        self,
        save_file_path: Path,
        carriers: list[CATVCarrierInfo],
        tr_ts_infos: list[TransportStreamInfo] | None = None,
        bs_ts_infos: list[TransportStreamInfo] | None = None,
        cs_ts_infos: list[TransportStreamInfo] | None = None,
        exclude_pay_tv: bool = False,
        bcas_only: bool = False,
        cas_as_sky: bool = False,
        prefer: PreferredSource | None = None,
        normalize_names: bool = False,
    ) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            carriers (list[CATVCarrierInfo]): スキャン結果の CATV キャリア情報のリスト
            tr_ts_infos (list[TransportStreamInfo] | None): ネイティブ地上波のスキャン結果の TS 情報 (未指定なら地上波エントリを出力しない)
            bs_ts_infos (list[TransportStreamInfo] | None): ネイティブ BS のスキャン結果の TS 情報 (未指定なら BS エントリを出力しない)
            cs_ts_infos (list[TransportStreamInfo] | None): ネイティブ CS のスキャン結果の TS 情報 (未指定なら CS エントリを出力しない)
            exclude_pay_tv (bool): 有料放送チャンネルを出力から除外するか (CATV エントリ・ネイティブエントリの両方に適用される)
            bcas_only (bool): B-CAS カードで受信できない (C-CAS/A-CAS が必要、または CAS 種別不明の) CATV エントリを出力から除外するか
            cas_as_sky (bool): C-CAS/A-CAS が必要な CATV エントリを type: SKY として出力するか
            prefer (PreferredSource | None): CATV 再送信とネイティブで重複した TS のどちらを有効にするか (もう一方を disabled にする)
            normalize_names (bool): 出力する name フィールドの全角英数字・記号を半角に正規化するか
        """

        super().__init__(save_file_path, carriers)
        self._exclude_pay_tv = exclude_pay_tv
        self._bcas_only = bcas_only
        self._cas_as_sky = cas_as_sky
        self._prefer = prefer
        self._normalize_names = normalize_names
        self._native_ts_infos = _PrepareNativeTsInfos(tr_ts_infos or [], bs_ts_infos or [], cs_ts_infos or [], exclude_pay_tv)

    def _name(self, raw_name: str) -> str:
        """normalize_names が True の場合のみ name を半角へ正規化する"""

        return NormalizeChannelName(raw_name) if self._normalize_names else raw_name

    def format(self) -> str:
        """
        mirakc の channels 設定断片 (CATV 分 + ネイティブ地上波/BS/CS 分) としてフォーマットする

        Returns:
            str: フォーマットされた文字列
        """

        header_lines = [
            '# ISDBScanner CATV: mirakc 用 channels 設定断片 (CATV トランスモジュレーション分)',
            '#',
            '# mirakc は Mirakurun の tsmfRelTs のようなネイティブ TSMF 分離機能を持たないため、TSMF キャリアの',
            '# 各相対 TS を単一の TS として扱うには、選局コマンドの標準出力を `isdb-tsmf-split --rel-ts <N>` に',
            '# パイプする必要がある。以下の channels 断片では、TSMF の各エントリの extra-args に相対 TS 番号',
            '# (1-15) を設定しているので、config.yml の tuners セクションのコマンドテンプレートで',
            '# {{{extra_args}}} を isdb-tsmf-split --rel-ts に渡すこと。tuners セクションのコマンド例:',
            '#',
            '#   tuners:',
            '#     - name: CATV Tuner (adapter0)',
            '#       types: ["GR", "BS", "CS"]',
            '#       command: >-',
            '#         dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 0 -P -t 0 -o - {{{channel}}}',
            '#         {{#extra_args}}| isdb-tsmf-split --rel-ts {{{extra_args}}}{{/extra_args}}',
            '#',
            '# ※ mirakc の Mustache 実装が上記のような条件セクション ({{#...}}...{{/...}}) 構文をサポートして',
            '#   いるかは未検証のため、実運用では SingleTS 用と TSMF 用でチューナー (アダプタ) 自体を分ける、',
            '#   あるいは `| isdb-tsmf-split --rel-ts N` を固定で埋め込んだチューナーを channel 数だけ用意する',
            '#   など、環境に応じた組み込み方を検討すること (extra-args の値自体は正しい相対 TS 番号を示す)',
            '#',
            '# BS/CS 再送信 (トランスモジュレーション) のチャンネルは type: BS / type: CS として出力されるため、',
            '# CATV チューナーの types にはこのファイルに現れる全 type (GR/BS/CS、--cas-as-sky 使用時は SKY も) を',
            '# 列挙すること。types に列挙しないと該当チャンネルの選局にこのチューナーが使われない',
        ]
        header_lines.extend(_BuildExcludedTLVChannelLines(self._carriers, 'mirakc'))
        if len(self._native_ts_infos) > 0:
            header_lines.extend([
                '#',
                '# 末尾の channel が T** / BS**_* / CS** 形式のエントリはネイティブ (地上波/衛星アンテナ直結) 分で、recisdb で選局する。',
                '# BS/CS エントリの extra-args は `--tsid <TSID>` で、CATV エントリの extra-args (TSMF 相対 TS 番号) とは意味が異なる',
                '# (地上波では TSID 指定が不要なため extra-args は空文字列にしている)。',
                '# ISDB-S/ISDB-T チューナー側の tuners セクションのエントリ例:',
                '#',
                '#   tuners:',
                '#     - name: ISDB-S Tuner',
                '#       types: ["BS", "CS"]',
                '#       command: recisdb tune --device /dev/px4video0 --channel {{{channel}}} {{{extra_args}}} -',
                '#',
                '# ※ PT1/PT2/PT3 (chardev) は TSID 選局に対応していないため、{{{extra_args}}} を含めず',
                '#   `recisdb tune --device /dev/pt3video1 --channel {{{channel}}} -` のようなコマンドにすること',
                '# ※ CATV 再送信の地上波/BS/CS チャンネルとネイティブの地上波/BS/CS チャンネルは type が同じ (GR/BS/CS) になるため、',
                '#   mirakc はどちらのチューナー (dvbv5-zap / recisdb) で選局すべきかを type からは区別できない。',
                '#   両方を併用する場合は、どちらか一方のエントリを disabled: true にするなどの調整が必要',
            ])

        # CATV 再送信 TS の一覧を取り出し、prefer 指定時は重複した (放送種別カテゴリ, TSID) の集合を求めておく
        catv_pairs = _IterReceivableTransportStreams(self._carriers, self._exclude_pay_tv, self._bcas_only)
        duplicate_keys = (
            _BuildDuplicateKeys(catv_pairs, self._native_ts_infos) if self._prefer is not None else set()
        )
        # prefer 指定時、重複したエントリのうち優先しない側を disabled: true にする旨をヘッダーに追記する
        if self._prefer is not None and len(duplicate_keys) > 0:
            disabled_side = 'ネイティブ (recisdb)' if self._prefer == PreferredSource.CATV else 'CATV 再送信 (dvbv5-zap)'
            header_lines.extend([
                '#',
                f'# --prefer={self._prefer.value} が指定されたため、CATV 再送信とネイティブで重複した TS のうち',
                f'# {disabled_side} 側のエントリを disabled: true にして出力しています (エントリ自体は残しています)',
            ])

        channels: list[CATVMirakcChannel] = []
        for carrier, ts_info in catv_pairs:
            extra_args = str(ts_info.tsmf_relative_ts_number) if ts_info.tsmf_relative_ts_number is not None else ''
            channel: CATVMirakcChannel = {
                'name': self._name(_BuildCATVChannelName(ts_info)),
                # 再送信元に応じて BS/CS 再送信は type: BS / type: CS、それ以外は type: GR とする
                # (cas_as_sky 指定時は C-CAS/A-CAS が必要なチャンネルのみ type: SKY になる)
                'type': _BuildCATVChannelType(ts_info, self._cas_as_sky),
                'channel': carrier.physical_channel,
                'extra-args': extra_args,
                # prefer='native' の場合、ネイティブ側と重複した CATV 再送信エントリを disable する
                'disabled': (
                    self._prefer == PreferredSource.NATIVE
                    and (ts_info.retransmission_source, ts_info.transport_stream_id) in duplicate_keys
                ),
            }
            channels.append(channel)

        # ネイティブ地上波/BS/CS のチャンネルエントリを追記 (既存 MirakcConfigYmlFormatter の GR/BS/CS 分 (636-677 行付近) の移植)
        for native_ts_info in self._native_ts_infos:
            # prefer='catv' の場合、CATV 再送信側と重複したネイティブエントリを disable する
            is_disabled = (
                self._prefer == PreferredSource.CATV
                and (_NativeBroadcastCategory(native_ts_info), native_ts_info.transport_stream_id) in duplicate_keys
            )
            if native_ts_info.broadcast_type == 'Terrestrial':
                # 地上波はネットワーク名 (= TS 名 = 放送局名) を name に、type: GR、channel は recisdb フォーマット (ex: T27) を使う。
                # 地上波では TSID 指定が不要なため extra-args は空文字列にする
                # (mirakc は Mustache テンプレートのため、空文字列でも Mirakurun のようにプレースホルダーが残ることはない)
                native_channel: CATVMirakcChannel = {
                    'name': self._name(native_ts_info.network_name),
                    'type': 'GR',
                    'channel': native_ts_info.physical_channel_recisdb,
                    'extra-args': '',
                    'disabled': is_disabled,
                }
            else:
                # BS・CS では追加の引数として --tsid (当該 TS の TSID) を指定し、当該トランスポンダで送出中の TS を明示的に TSID で選局する
                ## 実際に発行されるチューナーコマンドは --channel BS23_2 --tsid 18803 や --channel CS04 --tsid 28736 のようになる
                ## recisdb の V4L-DVB (DVBv5) 経路では ISDB-S のロックに TSID (DTV_STREAM_ID) の指定が必須で、
                ## BS は recisdb 内蔵の相対 TS テーブルで TSID を自動補完できるが、CS は補完テーブルが無いため --tsid の明示指定が必須
                native_channel = {
                    'name': self._name(native_ts_info.physical_channel),
                    'type': 'BS' if native_ts_info.broadcast_type == 'BS' else 'CS',
                    'channel': native_ts_info.physical_channel_recisdb,
                    'extra-args': f'--tsid {native_ts_info.transport_stream_id}',
                    'disabled': is_disabled,
                }
            channels.append(native_channel)

        return '\n'.join(header_lines) + '\n\n' + _DumpChannelsYaml(channels)


def GetEmittedCATVChannelTypes(
    carriers: list[CATVCarrierInfo],
    exclude_pay_tv: bool = False,
    bcas_only: bool = False,
    cas_as_sky: bool = False,
    prefer: PreferredSource | None = None,
    tr_ts_infos: list[TransportStreamInfo] | None = None,
    bs_ts_infos: list[TransportStreamInfo] | None = None,
    cs_ts_infos: list[TransportStreamInfo] | None = None,
) -> list[str]:
    """
    channels 側 (CATVMirakurunChannelsYmlFormatter / CATVMirakcConfigYmlFormatter) に実際に出力される
    CATV エントリの type 集合を、GR → BS → CS → SKY の順で返す (CATV チューナーの types に列挙するために使う)

    prefer / tr_ts_infos / bs_ts_infos / cs_ts_infos は引数として受け取るが、この関数の結果には影響しない:
    - これらはネイティブエントリの出力や CATV エントリの disable 有無 (prefer) にのみ関わる要素であり、
      CATV エントリの type 集合そのものは変えないため
    - prefer='native' で disable された CATV エントリも、チューナー側から見れば依然として選局し得る (disabled は
      レコーダー UI 上の扱いに過ぎない) ため、実装をシンプルに保つ意味でも disable の有無に関わらず type を含める
    (シグネチャ互換性・呼び出し側の見通しのため引数自体は受け取る)
    """

    emitted_types = {
        _BuildCATVChannelType(ts_info, cas_as_sky)
        for _, ts_info in _IterReceivableTransportStreams(carriers, exclude_pay_tv, bcas_only)
    }
    # GR → BS → CS → SKY の固定順で、実際に出力される type のみを返す
    return [channel_type for channel_type in ('GR', 'BS', 'CS', 'SKY') if channel_type in emitted_types]


CATVMirakurunTuner = TypedDict(
    'CATVMirakurunTuner',
    {
        'name': str,
        'types': list[str],
        'command': str,
        # CAS カードを検出できたときのみ出力する (未検出時・カード在庫未指定時は従来どおりキー自体を出力しない)
        'decoder': NotRequired[str],
        'isDisabled': bool,
    },
)


CATVMirakcTuner = TypedDict(
    'CATVMirakcTuner',
    {
        'name': str,
        'types': list[str],
        'command': str,
        'disabled': bool,
    },
)


def _BuildRecisdbTunerCommandMirakurun(tuner: ISDBTuner) -> str:
    """
    ISDB-T/ISDB-S チューナー用の recisdb 選局コマンド (Mirakurun 形式) を組み立てる
    (既存 isdb_scanner/formatter.py の MirakurunTunersYmlFormatter.get_tuner_command() (452-467 行付近) の移植)
    """

    # <satellite> は mirakc における {{{extra_args}}} の代わりとして使っている
    # TSID 選局に対応している ISDB-S 対応チューナー・ISDB-T/ISDB-S 両対応チューナーでは、
    # BS でのみ <satellite> が --tsid (BS チャンネルの TSID) に置換される
    if tuner.isTSIDSelectionSupported() is True and tuner.type in ('ISDB-S', 'ISDB-T/ISDB-S'):
        # Mirakurun のプレースホルダーは単なる文字列置換で実装されているが、"satellite" が空だと条件分岐が成立せず
        # チューナーコマンド内の <satellite> が置換されずに残ってしまうため、地上波や CS の場合は空文字列ではなく半角スペースを入れている
        # しかし生成されたチューナーコマンドに複数の連続するスペースが含まれるとコマンド実行に失敗するため、
        # "<channel>", "<satellite>", "-" (標準出力を表す) の間には敢えて半角スペースを入れないようにしている
        # ref: https://github.com/tsukumijima/ISDBScanner/issues/9
        return f'recisdb tune --device {tuner.device_path} --channel <channel><satellite>-'
    return f'recisdb tune --device {tuner.device_path} --channel <channel> -'


def _BuildRecisdbTunerCommandMirakc(tuner: ISDBTuner) -> str:
    """
    ISDB-T/ISDB-S チューナー用の recisdb 選局コマンド (mirakc 形式) を組み立てる
    (既存 isdb_scanner/formatter.py の MirakcConfigYmlFormatter.get_tuner_command() (679-688 行付近) の移植)
    """

    # TSID 選局に対応している ISDB-S 対応チューナー・ISDB-T/ISDB-S 両対応チューナーでは、
    # BS でのみ {{{extra_args}}} が --tsid (BS チャンネルの TSID) に置換される
    if tuner.isTSIDSelectionSupported() is True and tuner.type in ('ISDB-S', 'ISDB-T/ISDB-S'):
        return f'recisdb tune --device {tuner.device_path} --channel ' + '{{{channel}}} {{{extra_args}}} -'
    return f'recisdb tune --device {tuner.device_path} --channel ' + '{{{channel}}} -'


def _BuildMirakurunCardHeaderLines(detected_cards: list[DetectedCard], script_dir: Path) -> list[str]:
    """
    Mirakurun 用 tuners.yml のヘッダーコメントに追記する、CAS カード関連の説明行を組み立てる
    (カード在庫が渡された場合のみ呼ばれる。カード在庫が未指定 (None) のときは呼ばれず、従来どおりの出力になる)

    Args:
        detected_cards (list[DetectedCard]): 検出されたカードの一覧 (1 枚も検出できなかった場合は空リスト)
        script_dir (Path): デコーダーラッパースクリプトを生成したディレクトリ (tuners.yml と同じディレクトリ)

    Returns:
        list[str]: ヘッダーコメントに追記する行の一覧
    """

    bcas_reader_name = FindReaderNameForCardType(detected_cards, CardType.BCAS)
    ccas_reader_name = FindReaderNameForCardType(detected_cards, CardType.CCAS)

    if bcas_reader_name is None and ccas_reader_name is None:
        return [
            '#',
            '# CAS カードを 1 枚も検出できなかったため、CATV チューナーに decoder は指定していない。',
            '# 検出状況は CATV.cards.txt、または `isdb-catv-scanner --list-card-readers` で確認できる。',
        ]

    if bcas_reader_name is None or ccas_reader_name is None:
        # 片方のカードしか無い環境ではリーダーを選ぶ必要がないため、リーダー選択のできない arib-b25-stream-test で足りる
        detected_label = 'B-CAS' if bcas_reader_name is not None else 'C-CAS'
        return [
            '#',
            f'# {detected_label} カードのみを検出したため、CATV チューナーの decoder には arib-b25-stream-test を指定している。',
            '# arib-b25-stream-test は Linux ではカードリーダーを選択できない (リーダー選択用の ini 設定は Windows 専用で、',
            '# CLI オプションも存在しない) が、CAS カードが 1 種類しか挿さっていないため選択の必要がない。',
            '# B-CAS カードと C-CAS カードを併用する場合は、--card でリーダー名を指定できる recisdb が必要になる',
            '# (両方のカードを挿した状態で再スキャンすると、ラッパースクリプトが自動生成される)。',
        ]

    return [
        '#',
        '# B-CAS カードと C-CAS カードの両方を検出したため、CATV チューナーの decoder には自動生成した',
        f'# {MIRAKURUN_BCAS_DECODER_SCRIPT_NAME} (B-CAS 用リーダーを --card で固定した recisdb decode のラッパー) を指定している。',
        '#',
        '# Mirakurun の decoder はチューナー単位でしか指定できず、かつ child_process.spawn(command) で起動されるため',
        '# 引数を一切渡せない。このためチャンネルごとにカードを使い分けるには、次の 3 点セットが必要になる:',
        '#   1. ISDBScanner を --cas-as-sky 付きで実行し、C-CAS が必要なチャンネルを type: SKY として分離する',
        '#   2. types に SKY のみを持つ SKY 専用のチューナーエントリを別途立てる',
        f'#   3. その SKY 専用エントリの decoder に {MIRAKURUN_CCAS_DECODER_SCRIPT_NAME} を指定する',
        '#',
        '# SKY 専用エントリの記述例 (自動生成はしていないため、必要に応じて手動で追記すること):',
        '#',
        '#   - name: CATV Tuner (SKY / C-CAS)',
        '#     types:',
        '#       - SKY',
        '#     command: dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 1 -P -t 0 -o - <channel>',
        f'#     decoder: {script_dir / MIRAKURUN_CCAS_DECODER_SCRIPT_NAME}',
        '#     isDisabled: false',
        '#',
        '# ※ SKY 専用エントリを自動生成していないのは、アダプタを安全に割り当てられないため。',
        '#   Mirakurun は tuners.yml のエントリ間で物理アダプタの重複を一切チェックしない (Tuner.ts の _load() は',
        '#   設定エントリをそのまま push するだけ) ため、B-CAS 用エントリと SKY 用エントリで同じアダプタ番号 (-a N) を',
        '#   共有すると、同一アダプタに対して dvbv5-zap が二重起動しうる。',
        '#   SKY 専用エントリには必ず別のアダプタ (別の物理チューナー) を割り当てること。',
        '# ※ 一方でカード自体は共有できる: pcscd の SHARED 接続により 1 枚のカードを複数プロセスから同時に使えるため',
        '#   (libaribb25 は SCardBeginTransaction を使わず ECM 処理も 1 APDU で完結し、pcscd のリーダー単位の mutex で',
        '#   直列化される)、同じカードを複数のチューナーエントリからデコードに使っても問題ない。',
        '#   排他的に扱う必要があるのはカードではなくアダプタ (チューナー) の方。',
    ]


def _GetMirakurunDecoderCommand(detected_cards: list[DetectedCard], script_dir: Path) -> str | None:
    """
    CATV チューナーエントリの `decoder:` に出力する値を決める (出力しない場合は None)

    - B-CAS/C-CAS のどちらか一方のみ検出: リーダー選択が不要なため arib-b25-stream-test
      (既存 isdb_scanner/formatter.py の MirakurunTunersYmlFormatter が出力しているものと同じ)
    - 両方検出: リーダー名を --card で固定した recisdb decode のラッパースクリプト (B-CAS 用)
    - 1 枚も検出できなかった場合: None (従来どおり decoder キー自体を出力しない)
    """

    bcas_reader_name = FindReaderNameForCardType(detected_cards, CardType.BCAS)
    ccas_reader_name = FindReaderNameForCardType(detected_cards, CardType.CCAS)
    if bcas_reader_name is None and ccas_reader_name is None:
        return None
    if bcas_reader_name is None or ccas_reader_name is None:
        return 'arib-b25-stream-test'
    return str(script_dir / MIRAKURUN_BCAS_DECODER_SCRIPT_NAME)


def _BuildMirakcCardHeaderLines(detected_cards: list[DetectedCard], script_dir: Path) -> list[str]:
    """
    mirakc 用 tuners 設定断片のヘッダーコメントに追記する、CAS カード関連の説明行を組み立てる
    (カード在庫が渡された場合のみ呼ばれる。カード在庫が未指定 (None) のときは呼ばれず、従来どおりの出力になる)

    Args:
        detected_cards (list[DetectedCard]): 検出されたカードの一覧 (1 枚も検出できなかった場合は空リスト)
        script_dir (Path): decode-filter スクリプトを生成したディレクトリ (tuners 設定断片と同じディレクトリ)

    Returns:
        list[str]: ヘッダーコメントに追記する行の一覧
    """

    bcas_reader_name = FindReaderNameForCardType(detected_cards, CardType.BCAS)
    ccas_reader_name = FindReaderNameForCardType(detected_cards, CardType.CCAS)

    if bcas_reader_name is None and ccas_reader_name is None:
        return [
            '#',
            '# CAS カードを 1 枚も検出できなかったため、decode-filter の設定例は出力していない。',
            '# 検出状況は CATV.cards.txt、または `isdb-catv-scanner --list-card-readers` で確認できる。',
        ]

    if bcas_reader_name is None or ccas_reader_name is None:
        detected_label = 'B-CAS' if bcas_reader_name is not None else 'C-CAS'
        return [
            '#',
            f'# {detected_label} カードのみを検出した。CAS カードが 1 種類しか挿さっていない環境では使うリーダーを',
            '# 選ぶ必要がないため、mirakc 既定の filters.decode-filter.command の設定で足りる。',
            '# B-CAS カードと C-CAS カードを併用する場合は、チャンネルごとにリーダーを切り替える decode-filter が',
            '# 必要になる (両方のカードを挿した状態で再スキャンすると、そのスクリプトが自動生成される)。',
        ]

    decode_filter_path = script_dir / MIRAKC_DECODE_FILTER_SCRIPT_NAME
    return [
        '#',
        '# B-CAS カードと C-CAS カードの両方を検出したため、チャンネルごとに使うカードリーダーを切り替える',
        f'# decode-filter スクリプト ({MIRAKC_DECODE_FILTER_SCRIPT_NAME}) を自動生成している。',
        '# mirakc の filters.decode-filter.command はチャンネルごとに Mustache でレンダリングされ、',
        '# テンプレート変数 channel_name / channel_type / channel を利用できるため、フィルタ側で分岐できる。',
        '# config.yml への組み込み例:',
        '#',
        '#   filters:',
        '#     decode-filter:',
        f"#       command: {decode_filter_path} '{{{{{{channel_name}}}}}}' '{{{{{{channel_type}}}}}}' '{{{{{{channel}}}}}}'",
        '#',
        '# ※ mirakc はレンダリング結果をシェルに渡さず shell_words で単語分割して直接 exec するため',
        '#   (mirakc-core/src/command_util.rs の CommandBuilder::new())、値にスペースが含まれても壊れないよう',
        '#   上記のように各変数をシングルクォートで囲むこと (クォート自体は shell_words が解釈する)。',
        '# ※ スクリプト内の分岐は --cas-as-sky の指定有無に依存しないよう、channel_type ではなく',
        '#   「C-CAS が必要な物理チャンネル名の明示リスト」で行っている。',
        '# ※ pcscd の SHARED 接続により 1 枚のカードを複数プロセスから同時に使えるため、複数チューナーで同時に',
        '#   デコードしてもカードの取り合いにはならない。',
    ]


class CATVMirakurunTunersYmlFormatter:
    """
    検出したチューナー (CATV / ISDB-T / ISDB-S) から Mirakurun 用の tuners.yml を生成するフォーマッター

    CATV チューナーは dvbv5-zap で、ISDB-T/ISDB-S チューナーは recisdb で選局する。CATV チューナーの types には、
    channels 側 (channels_catv.yml) に実際に出力された CATV エントリの type 集合 (catv_channel_types) をそのまま列挙する
    (GetEmittedCATVChannelTypes() で得たもの)。types に列挙しない type のチャンネルはそのチューナーで選局されないため

    detected_cards (カード在庫) が渡された場合は、CATV チューナーエントリに `decoder:` を出力し、
    カード構成に応じた説明をヘッダーコメントに追記する (省略時 (None) の出力は従来と完全に同一)
    """

    def __init__(
        self,
        save_file_path: Path,
        catv_tuners: list[CATVTuner],
        isdbt_tuners: list[ISDBTuner],
        isdbs_tuners: list[ISDBTuner],
        dvbv5_conf_path: Path,
        catv_channel_types: list[str],
        detected_cards: list[DetectedCard] | None = None,
    ) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            catv_tuners (list[CATVTuner]): CATV (DVB-C ANNEX_A) 対応チューナーのリスト
            isdbt_tuners (list[ISDBTuner]): ネイティブ ISDB-T チューナーのリスト
            isdbs_tuners (list[ISDBTuner]): ネイティブ ISDB-S チューナーのリスト
            dvbv5_conf_path (Path): dvbv5-zap に渡す CATV 用 dvbv5 conf ファイルのパス
            catv_channel_types (list[str]): channels 側へ実際に出力された CATV エントリの type 集合 (GR/BS/CS/SKY)
            detected_cards (list[DetectedCard] | None): 検出された CAS カードの一覧
                (None = カード検出を行っていない。この場合は decoder もカード関連コメントも出力しない)
        """

        self._save_file_path = save_file_path
        self._catv_tuners = catv_tuners
        self._isdbt_tuners = isdbt_tuners
        self._isdbs_tuners = isdbs_tuners
        self._dvbv5_conf_path = dvbv5_conf_path
        self._catv_channel_types = catv_channel_types
        self._detected_cards = detected_cards
        # デコーダーラッパースクリプトは tuners.yml と同じディレクトリ (出力先の Mirakurun/) に生成される
        self._script_dir = save_file_path.parent

    def format(self) -> str:
        """
        Mirakurun の tuners.yml としてフォーマットする

        Returns:
            str: フォーマットされた文字列 (チューナーが1台も無い場合は説明コメントのみ)
        """

        header_lines = [
            '# ISDBScanner CATV: Mirakurun 用 tuners.yml',
            '#',
            '# CATV チューナーは dvbv5-zap で、ISDB-T/ISDB-S チューナーは recisdb で選局する。',
            '# CATV チューナーの types には channels_catv.yml に現れる全 type (GR/BS/CS、--cas-as-sky 使用時は SKY も) を',
            '# 列挙している (types に列挙しない type のチャンネルはこのチューナーで選局されない)。',
            '#',
            '# `-t 0` は録画時間 0 秒 (dvbv5-zap 自身のタイムアウトに委ねず、Mirakurun 側がプロセスの生存期間を',
            '# 管理する) を意図した値だが、dvbv5-zap の `-t` はロックタイムアウトと録画時間を兼ねる特殊仕様のため、',
            '# 実運用に組み込む際は実機で `-t 0` の挙動 (無制限に出力し続けるか) を確認してから使うこと',
        ]
        # カード在庫が渡されている場合のみ、CAS カード関連の説明をヘッダーに追記する (未指定時は従来どおりの出力)
        if self._detected_cards is not None:
            header_lines.extend(_BuildMirakurunCardHeaderLines(self._detected_cards, self._script_dir))
        decoder_command = (
            _GetMirakurunDecoderCommand(self._detected_cards, self._script_dir) if self._detected_cards is not None else None
        )

        tuners: list[CATVMirakurunTuner] = []
        for catv_tuner in self._catv_tuners:
            command = f'dvbv5-zap -c {self._dvbv5_conf_path} -a {catv_tuner.adapter_number} -P -t 0 -o - <channel>'
            catv_entry: CATVMirakurunTuner
            if decoder_command is not None:
                # 既存 isdb_scanner/formatter.py の MirakurunTunersYmlFormatter に倣い、decoder は command の直後に置く
                catv_entry = {
                    'name': f'{catv_tuner.name} (adapter{catv_tuner.adapter_number})',
                    'types': list(self._catv_channel_types),
                    'command': command,
                    'decoder': decoder_command,
                    'isDisabled': False,
                }
            else:
                catv_entry = {
                    'name': f'{catv_tuner.name} (adapter{catv_tuner.adapter_number})',
                    'types': list(self._catv_channel_types),
                    'command': command,
                    'isDisabled': False,
                }
            tuners.append(catv_entry)
        for isdbt_tuner in self._isdbt_tuners:
            tuners.append({
                'name': isdbt_tuner.name,
                'types': ['GR'],
                'command': _BuildRecisdbTunerCommandMirakurun(isdbt_tuner),
                'isDisabled': False,
            })
        for isdbs_tuner in self._isdbs_tuners:
            tuners.append({
                'name': isdbs_tuner.name,
                'types': ['BS', 'CS'],
                'command': _BuildRecisdbTunerCommandMirakurun(isdbs_tuner),
                'isDisabled': False,
            })

        if len(tuners) == 0:
            # チューナーが1台も無い場合は YAML 本体を出力せず、説明コメントのみ返す
            return '\n'.join(header_lines) + '\n'
        return '\n'.join(header_lines) + '\n\n' + _DumpChannelsYaml(tuners)

    def save(self) -> str:
        """フォーマットを実行し、結果をファイルに保存する"""

        formatted_str = self.format()
        with open(self._save_file_path, mode='w', encoding='utf-8') as f:
            f.write(formatted_str)
        return formatted_str


class CATVMirakcTunersYmlFormatter:
    """
    検出したチューナー (CATV / ISDB-T / ISDB-S) から mirakc 用の tuners 設定断片を生成するフォーマッター

    CATV チューナーは dvbv5-zap で、ISDB-T/ISDB-S チューナーは recisdb で選局する。
    mirakc は Mirakurun の tsmfRelTs のようなネイティブ TSMF 分離機能を持たないため、TSMF キャリアの相対 TS を
    単一 TS として扱うには、CATV チューナーの選局コマンドの標準出力を `isdb-tsmf-split --rel-ts {{{extra_args}}}` に
    パイプする必要がある (channels 側の extra-args に相対 TS 番号を出力している)。ここでは基本形の command のみを
    出力し、isdb-tsmf-split の組み込み方はヘッダーコメントの例として示すに留める

    detected_cards (カード在庫) が渡された場合は、ヘッダーコメントにカード構成に応じた decode-filter の
    組み込み例を追記する (省略時 (None) の出力は従来と完全に同一)

    各チューナーのリストが空ならそのセクションは出力せず、全チューナーが空なら説明コメントのみを返す
    """

    def __init__(
        self,
        save_file_path: Path,
        catv_tuners: list[CATVTuner],
        isdbt_tuners: list[ISDBTuner],
        isdbs_tuners: list[ISDBTuner],
        dvbv5_conf_path: Path,
        catv_channel_types: list[str],
        detected_cards: list[DetectedCard] | None = None,
    ) -> None:
        """
        Args:
            save_file_path (Path): 保存先のファイルパス
            catv_tuners (list[CATVTuner]): CATV (DVB-C ANNEX_A) 対応チューナーのリスト
            isdbt_tuners (list[ISDBTuner]): ネイティブ ISDB-T チューナーのリスト
            isdbs_tuners (list[ISDBTuner]): ネイティブ ISDB-S チューナーのリスト
            dvbv5_conf_path (Path): dvbv5-zap に渡す CATV 用 dvbv5 conf ファイルのパス
            catv_channel_types (list[str]): channels 側へ実際に出力された CATV エントリの type 集合 (GR/BS/CS/SKY)
            detected_cards (list[DetectedCard] | None): 検出された CAS カードの一覧
                (None = カード検出を行っていない。この場合はカード関連コメントを出力しない)
        """

        self._save_file_path = save_file_path
        self._catv_tuners = catv_tuners
        self._isdbt_tuners = isdbt_tuners
        self._isdbs_tuners = isdbs_tuners
        self._dvbv5_conf_path = dvbv5_conf_path
        self._catv_channel_types = catv_channel_types
        self._detected_cards = detected_cards
        # decode-filter スクリプトは tuners 設定断片と同じディレクトリ (出力先の mirakc/) に生成される
        self._script_dir = save_file_path.parent

    def format(self) -> str:
        """
        mirakc の tuners 設定断片としてフォーマットする

        Returns:
            str: フォーマットされた文字列 (チューナーが1台も無い場合は説明コメントのみ)
        """

        header_lines = [
            '# ISDBScanner CATV: mirakc 用 tuners 設定断片',
            '#',
            '# CATV チューナーは dvbv5-zap で、ISDB-T/ISDB-S チューナーは recisdb で選局する。',
            '# CATV チューナーの types には channels_catv.yml に現れる全 type (GR/BS/CS、--cas-as-sky 使用時は SKY も) を',
            '# 列挙している (types に列挙しない type のチャンネルはこのチューナーで選局されない)。',
            '#',
            '# mirakc は Mirakurun の tsmfRelTs のようなネイティブ TSMF 分離機能を持たないため、TSMF キャリアの',
            '# 各相対 TS を単一の TS として扱うには、CATV チューナーの選局コマンドの標準出力を',
            '# `isdb-tsmf-split --rel-ts {{{extra_args}}}` にパイプする必要がある。command の例:',
            '#',
            '#   command: >-',
            '#     dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 0 -P -t 0 -o - {{{channel}}}',
            '#     {{#extra_args}}| isdb-tsmf-split --rel-ts {{{extra_args}}}{{/extra_args}}',
            '#',
            '# ※ mirakc の Mustache 実装が上記のような条件セクション ({{#...}}...{{/...}}) 構文をサポートして',
            '#   いるかは未検証のため、実運用では SingleTS 用と TSMF 用でチューナー (アダプタ) 自体を分ける、',
            '#   あるいは `| isdb-tsmf-split --rel-ts N` を固定で埋め込んだチューナーを channel 数だけ用意する',
            '#   など、環境に応じた組み込み方を検討すること (以下の command は isdb-tsmf-split を挟まない基本形)',
            '#',
            '# `-t 0` は録画時間 0 秒 (dvbv5-zap 自身のタイムアウトに委ねず、mirakc 側がプロセスの生存期間を',
            '# 管理する) を意図した値だが、dvbv5-zap の `-t` はロックタイムアウトと録画時間を兼ねる特殊仕様のため、',
            '# 実運用に組み込む際は実機で `-t 0` の挙動 (無制限に出力し続けるか) を確認してから使うこと',
        ]
        # カード在庫が渡されている場合のみ、CAS カード関連の説明をヘッダーに追記する (未指定時は従来どおりの出力)
        if self._detected_cards is not None:
            header_lines.extend(_BuildMirakcCardHeaderLines(self._detected_cards, self._script_dir))

        tuners: list[CATVMirakcTuner] = []
        for catv_tuner in self._catv_tuners:
            tuners.append({
                'name': f'{catv_tuner.name} (adapter{catv_tuner.adapter_number})',
                'types': list(self._catv_channel_types),
                'command': (
                    f'dvbv5-zap -c {self._dvbv5_conf_path} -a {catv_tuner.adapter_number} -P -t 0 -o - '
                    + '{{{channel}}}'
                ),
                'disabled': False,
            })
        for isdbt_tuner in self._isdbt_tuners:
            tuners.append({
                'name': isdbt_tuner.name,
                'types': ['GR'],
                'command': _BuildRecisdbTunerCommandMirakc(isdbt_tuner),
                'disabled': False,
            })
        for isdbs_tuner in self._isdbs_tuners:
            tuners.append({
                'name': isdbs_tuner.name,
                'types': ['BS', 'CS'],
                'command': _BuildRecisdbTunerCommandMirakc(isdbs_tuner),
                'disabled': False,
            })

        if len(tuners) == 0:
            # チューナーが1台も無い場合は YAML 本体を出力せず、説明コメントのみ返す
            return '\n'.join(header_lines) + '\n'
        return '\n'.join(header_lines) + '\n\n' + _DumpChannelsYaml(tuners)

    def save(self) -> str:
        """フォーマットを実行し、結果をファイルに保存する"""

        formatted_str = self.format()
        with open(self._save_file_path, mode='w', encoding='utf-8') as f:
            f.write(formatted_str)
        return formatted_str

"""
CATV スキャン結果 (+ ネイティブ ISDB-T/ISDB-S 統合スキャン結果) を EDCB (EDCB-Wine + BonDriver_mirakc) 用の
ChSet4.txt / ChSet5.txt として出力するフォーマッター群

⚠️ 実機 (EDCB-Wine + BonDriver_mirakc) での検証は未了である。ここで出力する ChSet4.txt の空間 (space) /
   チューナー通し番号 (ch) の割り当ては、あくまで BonDriver_mirakc が参照する Mirakurun/mirakc のチャンネル定義
   (isdb_scanner/catv/formatter.py の CATVMirakurunChannelsYmlFormatter / CATVMirakcConfigYmlFormatter が
   出力する channels_catv.yml) との対応を前提とした実験的な出力であり、実際に EDCB で選局できるかは未確認である。

移植元は isdb_scanner/formatter.py の EDCBChSet4TxtFormatter / EDCBChSet5TxtFormatter で、
BOM (UTF-8 with BOM) / 改行コード (CRLF) / タブ区切りのフィールド順・意味・チューナー空間割り当て
(GR=0 / BS=1 / CS=2、CATV の SKY=3) / 有料放送除外規則 (独立データ放送 service_type 0xC0 の扱いを含む) を
そのまま踏襲している。フィルタ (exclude_pay_tv / bcas_only) ・type 判定・チャンネル名生成のロジックは
isdb_scanner/catv/formatter.py のヘルパー (_IterReceivableTransportStreams / _BuildCATVChannelName /
_BuildCATVChannelType / _PrepareNativeTsInfos など) を再利用し、二重実装しない。

prefer (PreferredSource) が指定された場合の扱いは Mirakurun/mirakc 用フォーマッターと異なる:
EDCB の ChSet4/ChSet5 には Mirakurun の isDisabled / mirakc の disabled に相当する「無効化したまま残す」概念が
無いため、CATV 再送信 TS とネイティブ TS が同一 TS (放送種別カテゴリ, TSID が一致) で重複したときは、
優先しない側のエントリを出力から完全に除外する (エントリ自体を残さない)。
"""

import csv
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from isdb_scanner.catv.constants import CATVCarrierInfo, PreferredSource
from isdb_scanner.catv.formatter import (
    _VIDEO_SERVICE_TYPES,
    NormalizeChannelName,
    _BuildCATVChannelName,
    _BuildCATVChannelType,
    _BuildDuplicateKeys,
    _IterReceivableTransportStreams,
    _NativeBroadcastCategory,
    _PrepareNativeTsInfos,
)
from isdb_scanner.constants import TransportStreamInfo


# CATV の type (_BuildCATVChannelType の結果) → EDCB のチューナー空間 (space) の対応表
## 移植元 EDCBChSet4TxtFormatter の GR=0 / BS=1 / CS=2 の割り当てを踏襲し、
## CATV 独自の SKY (--cas-as-sky 使用時に C-CAS/A-CAS が必要な TS に付与される、本来 SPHD 用の第4の type) を 3 に割り当てる
_TYPE_TO_SPACE: dict[str, int] = {'GR': 0, 'BS': 1, 'CS': 2, 'SKY': 3}

# ネイティブ TS の放送種別カテゴリ (_NativeBroadcastCategory の結果) → EDCB のチューナー空間 (space) の対応表
_NATIVE_CATEGORY_TO_SPACE: dict[str, int] = {'Terrestrial': 0, 'BS': 1, 'CS': 2}

# ネイティブ TS の ChSet4 の ch_name (物理チャンネル名) に付与する接頭辞 (移植元 EDCBChSet4TxtFormatter と同一)
_NATIVE_CATEGORY_TO_PREFIX: dict[str, str] = {'Terrestrial': 'Terrestrial', 'BS': 'BS', 'CS': 'CS'}

# ONID (network_id) が不明な CATV 自主放送 TS 用の代替 ONID (16bit の最大値)
_UNKNOWN_ONID = 0xFFFF


@dataclass
class _ServiceRow:
    """ChSet4/ChSet5 の1サービス (1行) 分の情報"""

    service_name: str
    service_id: int
    service_type: int
    partial_flag: int   # ワンセグ放送なら 1、それ以外は 0 (CATV は is_oneseg 情報を持たないため常に 0)
    is_video: bool      # 映像サービス種別かどうか (use_view_flag / epg_cap_flag / search_flag の判定に使う)


@dataclass
class _ChannelEntry:
    """
    ChSet4/ChSet5 の1チャンネル (= 1 TS) 分の情報
    space / ch (ChSet4 のみ) はチューナー空間ごとの通し番号で、実際の割り当ては format() 側で行う
    """

    space: int              # チューナー空間 (GR=0 / BS=1 / CS=2 / SKY=3)
    ch_name: str            # ChSet4 の物理チャンネル名。CATV は _BuildCATVChannelName の結果、ネイティブは "接頭辞:物理チャンネル"
    network_name: str       # ネットワーク名 (地上波: TS 名 / BS・CS: ネットワーク名)
    network_id: int         # ONID (CATV で network_id が None の場合は 0xFFFF)
    transport_stream_id: int
    remocon_id: int         # リモコンキー ID (CATV・リモコンキー ID を持たない TS では 0)
    services: list[_ServiceRow] = field(default_factory=list)


class _CATVEDCBBaseFormatter:
    """
    CATV (+ ネイティブ) スキャン結果を EDCB 用 ChSet4/ChSet5 として出力するフォーマッターの基底クラス
    エントリの組み立て (CATV エントリ・ネイティブエントリの列挙、フィルタ、prefer による除外) を共通化し、
    実際の書式化 (ChSet4 / ChSet5) はサブクラスの format() で行う
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
            cas_as_sky (bool): C-CAS/A-CAS が必要な CATV エントリを space: SKY (=3) として出力するか
            prefer (PreferredSource | None): CATV 再送信とネイティブで重複した TS のどちらを残すか (もう一方を出力から除外する)
            normalize_names (bool): 出力するチャンネル名・サービス名の全角英数字・記号を半角に正規化するか
        """

        self._save_file_path = save_file_path
        self._carriers = carriers
        self._exclude_pay_tv = exclude_pay_tv
        self._bcas_only = bcas_only
        self._cas_as_sky = cas_as_sky
        self._prefer = prefer
        self._normalize_names = normalize_names
        # ネイティブ地上波/BS/CS の TS 情報を前処理 (有料放送フィルタ + 物理チャンネル順ソート) しておく
        ## _PrepareNativeTsInfos は deepcopy してから処理するため、渡された tr/bs/cs のリストは破壊されない
        self._native_ts_infos = _PrepareNativeTsInfos(
            tr_ts_infos or [], bs_ts_infos or [], cs_ts_infos or [], exclude_pay_tv
        )

    def _name(self, raw_name: str) -> str:
        """normalize_names が True の場合のみ、チャンネル名・サービス名を半角へ正規化する"""

        return NormalizeChannelName(raw_name) if self._normalize_names else raw_name

    def _build_entries(self) -> list[_ChannelEntry]:
        """
        出力対象の全チャンネルエントリを、CATV 再送信 → ネイティブ地上波/BS/CS の順 (channels_catv.yml と同じ並び) で組み立てる
        prefer が指定された場合、CATV 再送信 TS とネイティブ TS が重複したとき、優先しない側のエントリを結果から除外する
        (EDCB には Mirakurun の isDisabled に相当する概念が無いため、無効化ではなく完全な除外で対応する)
        """

        # CATV 再送信 TS の一覧 (物理チャンネル名昇順・TSMF 相対 TS 番号昇順) を取り出す
        ## exclude_pay_tv / bcas_only によるフィルタは _IterReceivableTransportStreams が適用する
        catv_pairs = _IterReceivableTransportStreams(self._carriers, self._exclude_pay_tv, self._bcas_only)
        # prefer 指定時は、CATV 再送信とネイティブで重複した (放送種別カテゴリ, TSID) の集合を求めておく
        duplicate_keys = (
            _BuildDuplicateKeys(catv_pairs, self._native_ts_infos) if self._prefer is not None else set()
        )

        entries: list[_ChannelEntry] = []

        # CATV 再送信 TS のエントリを組み立てる
        for _, ts_info in catv_pairs:
            # prefer='native' の場合、ネイティブ側と重複した CATV 再送信エントリは出力から除外する
            if (
                self._prefer == PreferredSource.NATIVE
                and (ts_info.retransmission_source, ts_info.transport_stream_id) in duplicate_keys
            ):
                continue
            channel_type = _BuildCATVChannelType(ts_info, self._cas_as_sky)
            # サービスを service_id 昇順でソート (移植元 EDCBChSet4/ChSet5 と同じ)。元の services は破壊しない
            services = sorted(ts_info.services, key=lambda service: service.service_id)
            entries.append(
                _ChannelEntry(
                    space=_TYPE_TO_SPACE[channel_type],
                    # BonDriver_mirakc は Mirakurun/mirakc のチャンネル定義を参照するため、
                    # ch_name は channels_catv.yml の name (= _BuildCATVChannelName の結果) と一致させる
                    ch_name=self._name(_BuildCATVChannelName(ts_info)),
                    network_name=ts_info.network_name,
                    # CATV 自主放送 TS など network_id が None の場合は 0xFFFF を代替 ONID として使う
                    network_id=ts_info.network_id if ts_info.network_id is not None else _UNKNOWN_ONID,
                    transport_stream_id=ts_info.transport_stream_id,
                    remocon_id=0,  # CATV TS はリモコンキー ID を持たないため 0
                    services=[
                        _ServiceRow(
                            service_name=self._name(service.service_name),
                            service_id=service.service_id,
                            service_type=service.service_type,
                            # CATV のサービス情報は is_oneseg を持たないため partial_flag は常に 0
                            partial_flag=0,
                            is_video=service.service_type in _VIDEO_SERVICE_TYPES,
                        )
                        for service in services
                    ],
                )
            )

        # ネイティブ地上波/BS/CS TS のエントリを組み立てる (移植元 EDCBChSet4/ChSet5 と同じ扱い)
        for ts_info in self._native_ts_infos:
            category = _NativeBroadcastCategory(ts_info)
            # prefer='catv' の場合、CATV 再送信側と重複したネイティブエントリは出力から除外する
            if (
                self._prefer == PreferredSource.CATV
                and (category, ts_info.transport_stream_id) in duplicate_keys
            ):
                continue
            # 有料放送を除外する場合で、TS 内に独立データ放送 (0xC0) 以外のサービスが残らなかった TS はチャンネル自体を登録しない
            # (移植元 EDCBChSet4/ChSet5 の TS スキップ規則。_PrepareNativeTsInfos で BS/CS の空 TS は既に除去済みだが、
            #  地上波の空 TS は残るため、ここでネイティブ実装と同じ判定を行い ch の通し番号もネイティブと揃える)
            if self._exclude_pay_tv is True and len([s for s in ts_info.services if s.service_type != 0xC0]) == 0:
                continue
            prefix = _NATIVE_CATEGORY_TO_PREFIX[category]
            services = sorted(ts_info.services, key=lambda service: service.service_id)
            entries.append(
                _ChannelEntry(
                    space=_NATIVE_CATEGORY_TO_SPACE[category],
                    # ネイティブは移植元 EDCBChSet4 と同じく "接頭辞:物理チャンネル" (ex: "Terrestrial:T21") を ch_name に使う
                    ch_name=self._name(f'{prefix}:{ts_info.physical_channel}'),
                    network_name=ts_info.network_name,
                    network_id=ts_info.network_id,
                    transport_stream_id=ts_info.transport_stream_id,
                    remocon_id=ts_info.remote_control_key_id if ts_info.remote_control_key_id is not None else 0,
                    services=[
                        _ServiceRow(
                            service_name=self._name(service.service_name),
                            service_id=service.service_id,
                            service_type=service.service_type,
                            partial_flag=1 if service.is_oneseg else 0,
                            is_video=service.isVideoServiceType(),
                        )
                        for service in services
                    ],
                )
            )

        return entries

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


class CATVEDCBChSet4TxtFormatter(_CATVEDCBBaseFormatter):
    """
    CATV (+ ネイティブ ISDB-T/ISDB-S) スキャン結果を EDCB の ChSet4.txt として出力するフォーマッター
    移植元は isdb_scanner/formatter.py の EDCBChSet4TxtFormatter で、生成される ChSet4.txt は
    BonDriver_mirakc / BonDriver_Mirakurun (BonDriver_mirakc が参照する channels_catv.yml のチャンネル定義) 専用

    ⚠️ 実機検証は未了。space / ch の割り当ては channels_catv.yml との対応を前提とした実験的出力である
       (モジュール冒頭の注意書きを参照)。

    prefer 指定時は、CATV 再送信 TS とネイティブ TS が重複したとき優先しない側のエントリを出力から除外する
    (EDCB には isDisabled に相当する概念が無いため、無効化ではなく完全除外で対応する)
    """

    def format(self) -> str:
        """
        EDCB の ChSet4.txt としてフォーマットする

        Returns:
            str: フォーマットされた文字列 (UTF-8 with BOM 相当。先頭に BOM を付与し、改行は CRLF)
        """

        """
        ChSet4.txt のフォーマット (移植元 EDCBChSet4TxtFormatter と同一):
          ch_name, service_name, network_name, space, ch, network_id, transport_stream_id, service_id, service_type,
          partial_flag, use_view_flag, remocon_id
          space (チューナー空間) は GR=0 / BS=1 / CS=2、CATV の SKY (--cas-as-sky) は 3
          ch (物理チャンネルに対応する BonDriver の通し番号) は space ごとに 0 から通し番号を振る (エントリ = TS 単位でインクリメント)
          partial_flag は is_oneseg に対応する (CATV は情報を持たないため常に 0)
          use_view_flag は service_type が映像サービスの場合は 1、それ以外は 0
          remocon_id は remote_control_key_id に対応する (CATV・リモコンキー ID を持たない TS では 0)
        """

        entries = self._build_entries()

        # ヘッダーなし TSV (CRLF) に変換
        string_io = StringIO()
        writer = csv.writer(string_io, delimiter='\t', lineterminator='\r\n')
        # チューナー空間 (space) ごとの ch (通し番号) カウンタ。エントリ = TS 単位でインクリメントする
        ## サービスを持たないエントリ (例: 解析できたサービスが無い CATV SingleTS) でも ch は消費し、
        ## channels_catv.yml のチャンネル並びと ch の対応がずれないようにする (移植元 EDCBChSet4 と同じ挙動)
        space_ch_counters: dict[int, int] = {}
        for entry in entries:
            ch = space_ch_counters.get(entry.space, 0)
            for service in entry.services:
                writer.writerow(
                    [
                        entry.ch_name,
                        service.service_name,
                        entry.network_name,
                        entry.space,
                        ch,
                        entry.network_id,
                        entry.transport_stream_id,
                        service.service_id,
                        service.service_type,
                        service.partial_flag,
                        1 if service.is_video else 0,
                        entry.remocon_id,
                    ]
                )
            space_ch_counters[entry.space] = ch + 1

        # StringIO の先頭にシークする
        string_io.seek(0)

        # EDCB は UTF-8 with BOM でないと受け付けないため、先頭に BOM を付与する
        return '﻿' + string_io.getvalue()


class CATVEDCBChSet5TxtFormatter(_CATVEDCBBaseFormatter):
    """
    CATV (+ ネイティブ ISDB-T/ISDB-S) スキャン結果を EDCB の ChSet5.txt として出力するフォーマッター
    移植元は isdb_scanner/formatter.py の EDCBChSet5TxtFormatter で、ChSet5.txt は EDCB 全体で受信可能な
    チャンネル設定データ (各チューナー (BonDriver) に依存する情報は ChSet4.txt 側に書き込まれる)

    ⚠️ 実機検証は未了 (モジュール冒頭の注意書きを参照)。

    prefer 指定時は、CATV 再送信 TS とネイティブ TS が重複したとき優先しない側のエントリを出力から除外する
    (prefer 未指定時は CATV 再送信 TS とネイティブ TS の重複を除外しないため、同一 ONID/TSID/SID の行が
     重複して出力されうる。重複を避けたい場合は prefer を指定すること)
    """

    def format(self) -> str:
        """
        EDCB の ChSet5.txt としてフォーマットする

        Returns:
            str: フォーマットされた文字列 (UTF-8 with BOM 相当。先頭に BOM を付与し、改行は CRLF)
        """

        """
        ChSet5.txt のフォーマット (移植元 EDCBChSet5TxtFormatter と同一):
          service_name, network_name, network_id, transport_stream_id, service_id, service_type,
          partial_flag, epg_cap_flag, search_flag (未使用)
          partial_flag は is_oneseg に対応する (CATV は情報を持たないため常に 0)
          epg_cap_flag と search_flag (定義のみで未使用) は service_type が映像サービスの場合は 1、それ以外は 0
        """

        entries = self._build_entries()
        # network_id, transport_stream_id それぞれ昇順でソート (移植元 EDCBChSet5 と同じ。CATV の代替 ONID 0xFFFF は末尾側になる)
        entries = sorted(entries, key=lambda entry: (entry.network_id, entry.transport_stream_id))

        # ヘッダーなし TSV (CRLF) に変換
        string_io = StringIO()
        writer = csv.writer(string_io, delimiter='\t', lineterminator='\r\n')
        for entry in entries:
            for service in entry.services:
                epg_cap_flag = 1 if service.is_video else 0
                search_flag = 1 if service.is_video else 0
                writer.writerow(
                    [
                        service.service_name,
                        entry.network_name,
                        entry.network_id,
                        entry.transport_stream_id,
                        service.service_id,
                        service.service_type,
                        service.partial_flag,
                        epg_cap_flag,
                        search_flag,
                    ]
                )

        # StringIO の先頭にシークする
        string_io.seek(0)

        # EDCB は UTF-8 with BOM でないと受け付けないため、先頭に BOM を付与する
        return '﻿' + string_io.getvalue()

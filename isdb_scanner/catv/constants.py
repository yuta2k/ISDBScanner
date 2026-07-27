from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class PreferredSource(StrEnum):
    """
    CATV 再送信とネイティブ (ISDB-T/ISDB-S 直結) で同一 TS が重複したときに、どちらのエントリを優先 (有効なまま) にするか
    レコーダー (Mirakurun/mirakc) の設定生成時、重複したもう一方のエントリを isDisabled/disabled にして出力するために使う
    """

    # CATV 再送信 (dvbv5-zap) 側を優先し、重複したネイティブ (recisdb) 側のエントリを disable する
    CATV = 'catv'
    # ネイティブ (recisdb) 側を優先し、重複した CATV 再送信 (dvbv5-zap) 側のエントリを disable する
    NATIVE = 'native'


class CarrierType(StrEnum):
    """CATV トランスモジュレーション物理チャンネル (キャリア) の種別"""

    # TSMF (JCTEA STD-002) により複数 TS が多重されているキャリア
    TSMF = 'TSMF'
    # TSMF を使わず単一 TS がそのまま伝送されているキャリア
    SingleTS = 'SingleTS'
    # TLV/MMT (4K/8K 放送) が伝送されているキャリア
    TLV = 'TLV'
    # NULL パケットのみ・または何も受信できないキャリア
    Empty = 'Empty'


# 受信に必要な CAS カードの種別
# 'none': スクランブルなし (無料放送のみ) で CAS カード不要
# 'unknown': スクランブルされているが CA_system_id が空、または未知の値のためカード種別を特定できない
RequiredCASCard = Literal['none', 'B-CAS', 'C-CAS', 'A-CAS', 'unknown']

# 再送信元 (地上波・BS・CS の再送信か、CATV 事業者による自主放送か) の種別
RetransmissionSource = Literal['BS', 'Terrestrial', 'CS', 'SelfBroadcast', 'Unknown']

# CA_system_id (ARIB STD-B10 第2部 6.3.68 参照) からカード種別への対応表
# 0x0005 は ARIB STD-B25 で規定された ARIB 統一 CAS の CA_system_id で、地上波/BS/CS110 の B-CAS カードと
# 4K/8K 放送の A-CAS カードの双方で共通して使われている (TS キャリアか TLV/MMT キャリアかのコンテナ種別で B-CAS/A-CAS を区別する)
# 0x0006 は CATV 業界の C-CAS で使われていると実データ (複数の CATV ダンプで確認済み) から推測されるが、
# JCTEA STD-007 などの規格書による裏取りは未了のため、暫定的な対応とする
CA_SYSTEM_ID_NAMES: dict[int, str] = {
    0x0005: 'B-CAS',
    0x0006: 'C-CAS',
}


class CASInfo(BaseModel):
    """CAT/PMT/SDT の解析から得られる CAS (限定受信システム) 関連の情報"""

    # fmt: off
    ca_system_ids: list[int] = []               # CAT と PMT (第1ループ) の CA 記述子から得られた CA_system_id の和集合 (ソート済み)
    scramble_ratio: float = 0.0                  # TS パケットのうちスクランブルされているものの割合 (0.0-1.0)
    has_free_ca_mode_service: bool = False       # SDT で free_CA_mode (有料放送を示すフラグ) が立っているサービスが1つでもあるか
    required_card: RequiredCASCard = 'unknown'   # 受信に必要な CAS カードの種別
    # fmt: on


class CATVServiceInfo(BaseModel):
    """CATV TS (TSMF の場合は分離後の各相対 TS) に含まれるサービス (チャンネル) の情報"""

    # fmt: off
    service_id: int = -1            # サービス ID
    service_type: int = -1          # サービス種別
    service_name: str = 'Unknown'   # サービス名
    is_free: bool = True            # 無料放送かどうか (SDT の free_CA_mode == 0)
    # fmt: on


class CATVTransportStreamInfo(BaseModel):
    """
    CATV キャリアに多重されている TS (TSMF の場合は分離後の各相対 TS) の情報
    既存の TransportStreamInfo と異なり、ARIB 未規定の network_id を持つ CATV 自主放送 TS も扱えるよう computed_field は持たせない
    """

    # fmt: off
    physical_channel: str = 'Unknown'                        # 選局した物理チャンネル (ex: "CATV_15")
    tsmf_relative_ts_number: int | None = None               # TSMF の相対 TS 番号 (1-15) / TSMF でない (SingleTS の) 場合は None
    transport_stream_id: int = -1                            # トランスポートストリーム ID (PAT から取得)
    network_id: int | None = None                            # ネットワーク ID (NIT から自 TS の情報を取得できない場合は None)
    network_name: str = 'Unknown'                            # 地上波: TS 名 / BS/CS: ネットワーク名
    retransmission_source: RetransmissionSource = 'Unknown'  # 再送信元
    cas: CASInfo = CASInfo()                                 # CAS 関連情報
    services: list[CATVServiceInfo] = []                     # サービス一覧
    # fmt: on


class MMTAssetInfo(BaseModel):
    """MPT (MMT Package Table) のアセット (映像/音声/字幕/データ放送などのエレメンタリストリーム相当) 1つ分の情報"""

    # fmt: off
    asset_id_scheme: str = ''                          # asset_id() の scheme (4バイトの FourCC。印字不能な値は16進文字列にフォールバック)
    asset_type: str = ''                                # アセット種別 (4バイトの FourCC。ex: "hev1"=HEVC映像, "mp4a"=AAC音声, "aapp"=データ放送, "stpp"=字幕)
    packet_id: int | None = None                        # location_type=0x00 (同一データフロー) の場合の MMTP packet_id
    external_network_id: int | None = None              # location_type=0x03 (別放送網) の場合の network_id (8K マルチキャリア分散伝送の参照先)
    external_tsid: int | None = None                    # location_type=0x03 の場合の transport_stream_id (参照先キャリアの識別に使う)
    # fmt: on


class MMTServiceInfo(BaseModel):
    """MPT (MMT Package Table) 1つ分、すなわち MMT サービス (4K/8K のチャンネル) 1つ分の情報"""

    # fmt: off
    package_id: int = -1                  # package_id (MPT の package_id をそのまま整数化したもの)
    service_name: str = 'Unknown'         # サービス名 (MH-SDT などから取得できなかった場合は 'Unknown')
    assets: list[MMTAssetInfo] = []       # アセット一覧
    # fmt: on


class MMTExternalReferenceInfo(BaseModel):
    """MPT の location_type=0x03 (別放送網参照) から得られる、外部キャリアへの参照情報 (8K マルチキャリア分散伝送のグループ検出に使う)"""

    # fmt: off
    network_id: int = -1              # 参照先の network_id
    transport_stream_id: int = -1     # 参照先の transport_stream_id (TLV ストリームでは TSID として使われる)
    # fmt: on


class TLVStreamEntryInfo(BaseModel):
    """
    TLV-NIT のストリームループ 1 エントリ分の情報
    frequencies_hz が 2 つ以上あるエントリは、1 つの TLV ストリームを複数の物理キャリアに分散して伝送している
    (8K 放送のマルチキャリア分散伝送 / JLabs SPEC-034 相当) ことを示す
    """

    # fmt: off
    tlv_stream_id: int = -1           # TLV ストリーム ID
    service_ids: list[int] = []       # サービスリスト記述子 (tag=0x41) から得られたサービス ID の一覧
    frequencies_hz: list[int] = []    # 周波数リスト記述子 (tag=0xF3, JLabs 独自と推定) から得られた伝送周波数 (Hz) の一覧
    # fmt: on


class TLVNetworkInfo(BaseModel):
    """TLV-NIT (table_id=0x40, 自ネットワーク) から得られるネットワーク情報"""

    # fmt: off
    network_id: int | None = None                # ネットワーク ID (TLV-NIT が取得できなかった場合は None)
    network_name: str = 'Unknown'                # ネットワーク名 (ネットワーク名記述子 tag=0x40 から取得。UTF-8 で符号化されている)
    tlv_stream_ids: list[int] = []               # TLV ストリームループから得られた tlv_stream_id の一覧
    streams: list[TLVStreamEntryInfo] = []       # TLV ストリームループの詳細 (サービス ID / 伝送周波数)
    # fmt: on


class TLVCarrierGroupInfo(BaseModel):
    """
    TLV キャリアの TSMF 多重フレームヘッダから得られる、自キャリアの識別情報とキャリアグループ (複数 QAM 分散伝送) 情報
    group_carrier_count が 2 以上なら、8K 放送のように 1 つの TLV ストリームを複数キャリアに分散して伝送している
    (JLabs SPEC-034 の複数 QAM 変調方式相当) ことを示す
    実データでは 4K キャリアが (count=1, index=1)、8K の 3 キャリアが同一 group_id の
    (count=3, index=1/2/3) となることを全 TSMF ヘッダで確認済み
    """

    # fmt: off
    tlv_stream_id: int = -1         # 自キャリアで伝送されている TLV ストリームの ID (TLV-NIT の tlv_stream_id と対応)
    network_id: int = -1            # ネットワーク ID (TLV-NIT の network_id と対応)
    group_id: int = -1              # キャリアグループ番号
    group_carrier_count: int = 0    # グループを構成するキャリアの総数 (1 = 単一キャリアで完結)
    group_carrier_index: int = 0    # 自キャリアのグループ内での番号 (1 始まり)
    # fmt: on


class CATVMMTInfo(BaseModel):
    """TLV キャリア (4K/8K MMT 放送) の解析結果"""

    # fmt: off
    network: TLVNetworkInfo | None = None                         # TLV-NIT から得られるネットワーク情報 (取得できなかった場合は None)
    services: list[MMTServiceInfo] = []                           # MPT から得られたサービス (4K/8K チャンネル) 一覧
    carrier_group: TLVCarrierGroupInfo | None = None              # TSMF 多重フレームヘッダから得られる自キャリアの識別・グループ情報
    is_multi_carrier_partial: bool = False                        # 8K 等のマルチキャリア分散伝送の一部 (このキャリア単体では全データが揃わない)
                                                                   # であることを示す。TSMF ヘッダのキャリアグループ情報 (count >= 2)、
                                                                   # TLV-NIT の自サービスを含むストリームエントリの複数周波数、
                                                                   # MPT の location_type=0x03 (別放送網参照) のいずれかで判定する
    external_references: list[MMTExternalReferenceInfo] = []      # MPT の location_type=0x03 から得られた参照先 (network_id/TSID) 一覧
    multi_carrier_group: TLVStreamEntryInfo | None = None         # 自キャリアが属するマルチキャリアグループの TLV-NIT ストリームエントリ
                                                                   # (グループ全体の伝送周波数一覧を含む。TLV-NIT が取得できなければ None)
    # fmt: on


class CATVSignalStats(BaseModel):
    """
    選局中に DVBv5 ioctl (FE_GET_PROPERTY / DTV_STAT_*) から取得した信号品質統計
    フロントエンドドライバが対応していない項目や、統計取得自体に失敗した場合は None のままになる
    (dB 系の値は FE_SCALE_DECIBEL、% 系の値は FE_SCALE_RELATIVE でのみ取得できるため、
    ドライバによってどちらか一方しか埋まらないことがある)
    """

    # fmt: off
    signal_strength_dbm: float | None = None       # 信号強度 (dBm。DTV_STAT_SIGNAL_STRENGTH が FE_SCALE_DECIBEL の場合のみ)
    signal_strength_percent: float | None = None   # 信号強度 (0.0-100.0%。DTV_STAT_SIGNAL_STRENGTH が FE_SCALE_RELATIVE の場合のみ)
    cnr_db: float | None = None                     # C/N比 (dB。DTV_STAT_CNR が FE_SCALE_DECIBEL の場合のみ)
    cnr_percent: float | None = None                # C/N比 (0.0-100.0%。DTV_STAT_CNR が FE_SCALE_RELATIVE の場合のみ)
    error_rate: float | None = None                 # 訂正前ビット誤り率 (DTV_STAT_PRE_ERROR_BIT_COUNT / DTV_STAT_PRE_TOTAL_BIT_COUNT)
    # fmt: on


class CATVCarrierInfo(BaseModel):
    """CATV トランスモジュレーション物理チャンネル (キャリア) 1つ分の解析結果"""

    # fmt: off
    physical_channel: str = 'Unknown'                              # 選局した物理チャンネル (ex: "CATV_15")
    carrier_type: CarrierType = CarrierType.Empty                  # キャリア種別
    transport_streams: list[CATVTransportStreamInfo] = []          # 多重されている TS の一覧 (Empty/TLV キャリアでは空)
    mmt: CATVMMTInfo | None = None                                 # TLV キャリアの MMT 解析結果 (TLV キャリアでない場合は None)
    signal_stats: CATVSignalStats | None = None                    # 選局中に取得した信号品質統計 (未取得・取得失敗の場合は None)
    # fmt: on


# dvbv5-zap で選局する際の DELIVERY_SYSTEM / SYMBOL_RATE / MODULATION
# ISDB-C トランスモジュレーション (DVBC/ANNEX_A + QAM/AUTO + シンボルレート 5274000) の受信パラメータ
# ISDBC (SYS_ISDBC) は dvbv5 で選局できなかった経緯があるため使わない
CATV_DELIVERY_SYSTEM = 'DVBC/ANNEX_A'
CATV_MODULATION = 'QAM/AUTO'
CATV_SYMBOL_RATE = 5274000

# CATV トランスモジュレーションの物理チャンネル名 (dvbv5-zap の conf 上のチャンネル名) → 中心周波数 (Hz) の対応表
# 日本の CATV で一般的に使われる周波数プランに基づく標準的な値
# CATV_13-62 (UHF 帯相当) は 473-767MHz の 6MHz 刻み、CATV_C13-C63 (VHF/中間周波数帯) は 111-465MHz だが、
# CATV_C21→C22 のみ 159MHz→167MHz と 8MHz 刻みになっている点に注意 (C23 からは再び 225MHz を起点に 6MHz 刻み)
CATV_FREQUENCY_TABLE: dict[str, int] = {
    'CATV_13': 473_000_000,
    'CATV_14': 479_000_000,
    'CATV_15': 485_000_000,
    'CATV_16': 491_000_000,
    'CATV_17': 497_000_000,
    'CATV_18': 503_000_000,
    'CATV_19': 509_000_000,
    'CATV_20': 515_000_000,
    'CATV_21': 521_000_000,
    'CATV_22': 527_000_000,
    'CATV_23': 533_000_000,
    'CATV_24': 539_000_000,
    'CATV_25': 545_000_000,
    'CATV_26': 551_000_000,
    'CATV_27': 557_000_000,
    'CATV_28': 563_000_000,
    'CATV_29': 569_000_000,
    'CATV_30': 575_000_000,
    'CATV_31': 581_000_000,
    'CATV_32': 587_000_000,
    'CATV_33': 593_000_000,
    'CATV_34': 599_000_000,
    'CATV_35': 605_000_000,
    'CATV_36': 611_000_000,
    'CATV_37': 617_000_000,
    'CATV_38': 623_000_000,
    'CATV_39': 629_000_000,
    'CATV_40': 635_000_000,
    'CATV_41': 641_000_000,
    'CATV_42': 647_000_000,
    'CATV_43': 653_000_000,
    'CATV_44': 659_000_000,
    'CATV_45': 665_000_000,
    'CATV_46': 671_000_000,
    'CATV_47': 677_000_000,
    'CATV_48': 683_000_000,
    'CATV_49': 689_000_000,
    'CATV_50': 695_000_000,
    'CATV_51': 701_000_000,
    'CATV_52': 707_000_000,
    'CATV_53': 713_000_000,
    'CATV_54': 719_000_000,
    'CATV_55': 725_000_000,
    'CATV_56': 731_000_000,
    'CATV_57': 737_000_000,
    'CATV_58': 743_000_000,
    'CATV_59': 749_000_000,
    'CATV_60': 755_000_000,
    'CATV_61': 761_000_000,
    'CATV_62': 767_000_000,
    'CATV_C13': 111_000_000,
    'CATV_C14': 117_000_000,
    'CATV_C15': 123_000_000,
    'CATV_C16': 129_000_000,
    'CATV_C17': 135_000_000,
    'CATV_C18': 141_000_000,
    'CATV_C19': 147_000_000,
    'CATV_C20': 153_000_000,
    'CATV_C21': 159_000_000,
    'CATV_C22': 167_000_000,
    'CATV_C23': 225_000_000,
    'CATV_C24': 231_000_000,
    'CATV_C25': 237_000_000,
    'CATV_C26': 243_000_000,
    'CATV_C27': 249_000_000,
    'CATV_C28': 255_000_000,
    'CATV_C29': 261_000_000,
    'CATV_C30': 267_000_000,
    'CATV_C31': 273_000_000,
    'CATV_C32': 279_000_000,
    'CATV_C33': 285_000_000,
    'CATV_C34': 291_000_000,
    'CATV_C35': 297_000_000,
    'CATV_C36': 303_000_000,
    'CATV_C37': 309_000_000,
    'CATV_C38': 315_000_000,
    'CATV_C39': 321_000_000,
    'CATV_C40': 327_000_000,
    'CATV_C41': 333_000_000,
    'CATV_C42': 339_000_000,
    'CATV_C43': 345_000_000,
    'CATV_C44': 351_000_000,
    'CATV_C45': 357_000_000,
    'CATV_C46': 363_000_000,
    'CATV_C47': 369_000_000,
    'CATV_C48': 375_000_000,
    'CATV_C49': 381_000_000,
    'CATV_C50': 387_000_000,
    'CATV_C51': 393_000_000,
    'CATV_C52': 399_000_000,
    'CATV_C53': 405_000_000,
    'CATV_C54': 411_000_000,
    'CATV_C55': 417_000_000,
    'CATV_C56': 423_000_000,
    'CATV_C57': 429_000_000,
    'CATV_C58': 435_000_000,
    'CATV_C59': 441_000_000,
    'CATV_C60': 447_000_000,
    'CATV_C61': 453_000_000,
    'CATV_C62': 459_000_000,
    'CATV_C63': 465_000_000,
}


def BuildDvbv5ConfEntryLines(physical_channel: str, frequency: int) -> list[str]:
    """
    dvbv5 形式 conf ファイルの1チャンネル分のエントリ行を組み立てる
    (CATVTuner の内部 conf 生成と CATVDvbv5ConfFormatter の出力で共用し、書式が二重管理にならないようにする)
    """

    return [
        f'[{physical_channel}]',
        f'\tDELIVERY_SYSTEM = {CATV_DELIVERY_SYSTEM}',
        f'\tFREQUENCY = {frequency}',
        f'\tSYMBOL_RATE = {CATV_SYMBOL_RATE}',
        f'\tMODULATION = {CATV_MODULATION}',
    ]


class ChannelSummaryInfo(BaseModel):
    """スキャン差分レポートで、丸ごと追加/削除された物理チャンネル1件分の要約情報"""

    # fmt: off
    physical_channel: str = 'Unknown'      # 物理チャンネル (ex: "CATV_15")
    carrier_type: CarrierType = CarrierType.Empty  # キャリア種別
    transport_stream_count: int = 0        # 多重されている TS の数 (TLV/Empty キャリアでは 0)
    service_count: int = 0                 # 全 TS 合計のサービス数 (TLV キャリアでは MMT サービス数)
    # fmt: on


class TransportStreamDiffInfo(BaseModel):
    """スキャン差分レポートにおける、追加/削除された TS 1本分の情報 (TSID 単位)"""

    # fmt: off
    transport_stream_id: int = -1                # トランスポートストリーム ID
    tsmf_relative_ts_number: int | None = None   # TSMF の相対 TS 番号 (SingleTS の場合は None)
    network_name: str = 'Unknown'                # ネットワーク名 (地上波: TS 名 / BS・CS: ネットワーク名)
    service_count: int = 0                        # このTSに含まれるサービス数
    # fmt: on


class ServiceDiffInfo(BaseModel):
    """スキャン差分レポートにおける、追加/削除/改名されたサービス1件分の情報 (service_id 単位)"""

    # fmt: off
    transport_stream_id: int = -1               # このサービスが属する TS の TSID
    service_id: int = -1                        # サービス ID
    service_name: str = 'Unknown'                # 現在のサービス名 (削除の場合は削除される直前の名前)
    previous_service_name: str | None = None    # 改名の場合のみ、変更前のサービス名 (追加/削除では None)
    # fmt: on


class MMTServiceDiffInfo(BaseModel):
    """スキャン差分レポートにおける、追加/削除/改名された MMT サービス (TLV キャリアの 4K/8K チャンネル) 1件分の情報"""

    # fmt: off
    package_id: int = -1                        # MPT の package_id
    service_name: str = 'Unknown'                # 現在のサービス名 (削除の場合は削除される直前の名前)
    previous_service_name: str | None = None    # 改名の場合のみ、変更前のサービス名 (追加/削除では None)
    # fmt: on


class CASChangeInfo(BaseModel):
    """スキャン差分レポートにおける、TS 単位の CAS 種別 (required_card) 変化 1件分の情報"""

    # fmt: off
    transport_stream_id: int = -1
    previous_required_card: RequiredCASCard = 'unknown'
    current_required_card: RequiredCASCard = 'unknown'
    # fmt: on


class RetransmissionSourceChangeInfo(BaseModel):
    """スキャン差分レポートにおける、TS 単位の再送信元変化 1件分の情報"""

    # fmt: off
    transport_stream_id: int = -1
    previous_source: RetransmissionSource = 'Unknown'
    current_source: RetransmissionSource = 'Unknown'
    # fmt: on


class ChannelChangeInfo(BaseModel):
    """スキャン差分レポートにおける、内容が変化した物理チャンネル1件分の情報"""

    # fmt: off
    physical_channel: str = 'Unknown'
    previous_carrier_type: CarrierType | None = None    # キャリア種別が変化した場合のみ (変化前)
    current_carrier_type: CarrierType | None = None     # キャリア種別が変化した場合のみ (変化後)
    added_transport_streams: list[TransportStreamDiffInfo] = []
    removed_transport_streams: list[TransportStreamDiffInfo] = []
    added_services: list[ServiceDiffInfo] = []
    removed_services: list[ServiceDiffInfo] = []
    renamed_services: list[ServiceDiffInfo] = []
    added_mmt_services: list[MMTServiceDiffInfo] = []      # TLV キャリアの MMT サービス (package_id 単位) の増減・改名
    removed_mmt_services: list[MMTServiceDiffInfo] = []
    renamed_mmt_services: list[MMTServiceDiffInfo] = []
    cas_changes: list[CASChangeInfo] = []
    retransmission_source_changes: list[RetransmissionSourceChangeInfo] = []
    # fmt: on


class ScanDiff(BaseModel):
    """2回のスキャン結果 (CATV.json をパースした dict) を比較した差分レポート"""

    # fmt: off
    added_channels: list[ChannelSummaryInfo] = []      # 今回新たに追加された (前回は存在しなかった) 物理チャンネル
    removed_channels: list[ChannelSummaryInfo] = []    # 今回なくなった (前回は存在した) 物理チャンネル
    changed_channels: list[ChannelChangeInfo] = []     # 前回・今回とも存在するが、内容に変化があった物理チャンネル
    # fmt: on

    @property
    def has_changes(self) -> bool:
        """差分が1件でも存在するかどうか"""

        return len(self.added_channels) > 0 or len(self.removed_channels) > 0 or len(self.changed_channels) > 0

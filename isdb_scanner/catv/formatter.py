# ruff: noqa: UP013

import json
from io import StringIO
from pathlib import Path
from typing import NotRequired

from ruamel.yaml import YAML
from typing_extensions import TypedDict

from isdb_scanner.catv.constants import (
    CATV_DELIVERY_SYSTEM,
    CATV_FREQUENCY_TABLE,
    CATV_MODULATION,
    CATV_SYMBOL_RATE,
    CarrierType,
    CATVCarrierInfo,
    CATVTransportStreamInfo,
)


class CATVJSONFormatter:
    """
    CATV キャリアのスキャン解析結果 (CATVCarrierInfo のリスト) を JSON データとして保存するフォーマッター
    既存の isdb_scanner/formatter.py の BaseFormatter は地上波/BS/CS の3リスト固定のコンストラクタ引数を取るためシグネチャが合わず、
    CATV では継承せず独自の単純なクラスとして実装している (出力スタイルは既存 JSONFormatter を参考にしている)
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
        """
        JSON データとしてフォーマットする (物理チャンネル名をキーにした dict)

        Returns:
            str: フォーマットされた文字列
        """

        channels_dict = {carrier.physical_channel: carrier.model_dump(mode='json') for carrier in self._carriers}
        return json.dumps(channels_dict, indent=4, ensure_ascii=False)

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


class CATVDvbv5ConfFormatter:
    """
    CATV キャリアのスキャン結果のうち、受信できた (Empty でない) キャリアのみを dvbv5 形式の conf ファイルとして保存するフォーマッター
    ここで出力される conf ファイルは dvbv5-zap 標準の書式で、
    将来 mirakc などから `dvbv5-zap -c <このファイル> ...` で直接選局できるようにすることを目的としている
    (CATVTuner が内部的に生成する全チャンネル分の conf とは異なり、こちらは実際にロックできたチャンネルのみを含む)
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
            lines.append(f'[{carrier.physical_channel}]')
            lines.append(f'\tDELIVERY_SYSTEM = {CATV_DELIVERY_SYSTEM}')
            lines.append(f'\tFREQUENCY = {frequency}')
            lines.append(f'\tSYMBOL_RATE = {CATV_SYMBOL_RATE}')
            lines.append(f'\tMODULATION = {CATV_MODULATION}')

        if len(lines) == 0:
            return ''
        return '\n'.join(lines) + '\n'

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


def _BuildCATVChannelName(ts_info: CATVTransportStreamInfo) -> str:
    """
    CATV TS (TSMF の場合は分離後の相対 TS) 1本分の、Mirakurun/mirakc チャンネル名を組み立てる
    地上波/BS/CS の再送信であれば NIT から得られた network_name (TS 名/ネットワーク名) が入っているはずなのでそれを使う
    CATV 事業者の自主放送などで network_name が得られなかった場合は、先頭サービスのサービス名で代用する
    それも取れない (サービスが1つも解析できなかった) 場合は、物理チャンネル名 (+ TSMF 相対 TS 番号) を機械的な名前として使う
    """

    if ts_info.network_name != 'Unknown':
        return ts_info.network_name
    if len(ts_info.services) > 0:
        return ts_info.services[0].service_name
    if ts_info.tsmf_relative_ts_number is not None:
        return f'{ts_info.physical_channel}#{ts_info.tsmf_relative_ts_number}'
    return ts_info.physical_channel


def _IterReceivableTransportStreams(
    carriers: list[CATVCarrierInfo],
) -> list[tuple[CATVCarrierInfo, CATVTransportStreamInfo]]:
    """
    レコーダー向け出力の対象になる (carrier_type が TSMF/SingleTS の) キャリアから、多重されている各 TS を
    (キャリア, TS情報) のペアの一覧として取り出す。物理チャンネル名昇順・TSMF相対TS番号昇順でソートする
    TLV (4K/8K MMT) キャリアは TS ベースの Mirakurun/mirakc では選局・視聴できないため対象外
    Empty (受信不可) キャリアはそもそも transport_streams が空なので自然に除外される
    """

    pairs: list[tuple[CATVCarrierInfo, CATVTransportStreamInfo]] = []
    for carrier in carriers:
        if carrier.carrier_type not in (CarrierType.TSMF, CarrierType.SingleTS):
            continue
        for ts_info in carrier.transport_streams:
            pairs.append((carrier, ts_info))

    return sorted(
        pairs,
        key=lambda pair: (pair[0].physical_channel, pair[1].tsmf_relative_ts_number or 0),
    )


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


class CATVMirakurunChannelsYmlFormatter:
    """
    CATV キャリアのスキャン解析結果 (CATVCarrierInfo のリスト) から Mirakurun 用の channels.yml (CATV 分) を生成するフォーマッター

    Mirakurun は `tsmfRelTs` (1-15) キーをネイティブサポートしており (TSFilter.ts が TS 内で TSMF ヘッダを見て
    自前で分離する)、TSMF 分離用の外部フィルタ (isdb-tsmf-split) を挟む必要がない。そのためチューナー
    (tuners.yml) 側は dvbv5-zap で対象の物理チャンネルの全 PID をそのまま選局するだけでよく、本フォーマッターの
    出力ファイル冒頭にその旨と tuners.yml のエントリ例をコメントとして書き出す

    対象は carrier_type が TSMF/SingleTS の TS のみで、TLV (4K/8K MMT) キャリアと Empty (受信不可) キャリアは対象外
    (TLV は TS ベースの Mirakurun では扱えないため、除外した物理チャンネルをコメントで注記する)
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
        """
        Mirakurun の channels.yml (CATV 分) としてフォーマットする

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
            '#     command: dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 0 -P -t 0 -o - <channel>',
            '#     isDisabled: false',
            '#',
            '# `-t 0` は録画時間 0 秒 (dvbv5-zap 自身のタイムアウトに委ねず、Mirakurun 側がプロセスの生存期間を',
            '# 管理する) を意図した値だが、dvbv5-zap の `-t` はロックタイムアウトと録画時間を兼ねる特殊仕様のため、',
            '# 実運用に組み込む際は実機で `-t 0` の挙動 (無制限に出力し続けるか) を確認してから使うこと',
        ]

        excluded_tlv_channels = sorted(
            carrier.physical_channel for carrier in self._carriers if carrier.carrier_type == CarrierType.TLV
        )
        if len(excluded_tlv_channels) > 0:
            header_lines.append('#')
            header_lines.append(
                '# 以下の物理チャンネルは TLV/MMT (4K/8K) キャリアのため、TS ベースの Mirakurun では扱えず除外しています:'
            )
            header_lines.append(f'#   {", ".join(excluded_tlv_channels)}')

        channels: list[CATVMirakurunChannel] = []
        for carrier, ts_info in _IterReceivableTransportStreams(self._carriers):
            channel: CATVMirakurunChannel = {
                'name': _BuildCATVChannelName(ts_info),
                'type': 'GR',  # CATV トランスモジュレーションは Mirakurun 上 GR (地上波) 扱いにする
                'channel': carrier.physical_channel,
            }
            if ts_info.tsmf_relative_ts_number is not None:
                channel['tsmfRelTs'] = ts_info.tsmf_relative_ts_number
            channel['isDisabled'] = False
            channels.append(channel)

        string_io = StringIO()
        yaml = YAML()
        yaml.width = 1000
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        yaml.dump(channels, string_io)
        string_io.seek(0)

        return '\n'.join(header_lines) + '\n\n' + string_io.getvalue()

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


class CATVMirakcConfigYmlFormatter:
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
        """
        mirakc の channels 設定断片 (CATV 分) としてフォーマットする

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
            '#       types: ["GR"]',
            '#       command: >-',
            '#         dvbv5-zap -c <生成した dvbv5_channels_catv.conf> -a 0 -P -t 0 -o - {{{channel}}}',
            '#         {{#extra_args}}| isdb-tsmf-split --rel-ts {{{extra_args}}}{{/extra_args}}',
            '#',
            '# ※ mirakc の Mustache 実装が上記のような条件セクション ({{#...}}...{{/...}}) 構文をサポートして',
            '#   いるかは未検証のため、実運用では SingleTS 用と TSMF 用でチューナー (アダプタ) 自体を分ける、',
            '#   あるいは `| isdb-tsmf-split --rel-ts N` を固定で埋め込んだチューナーを channel 数だけ用意する',
            '#   など、環境に応じた組み込み方を検討すること (extra-args の値自体は正しい相対 TS 番号を示す)',
        ]

        excluded_tlv_channels = sorted(
            carrier.physical_channel for carrier in self._carriers if carrier.carrier_type == CarrierType.TLV
        )
        if len(excluded_tlv_channels) > 0:
            header_lines.append('#')
            header_lines.append(
                '# 以下の物理チャンネルは TLV/MMT (4K/8K) キャリアのため、TS ベースの mirakc では扱えず除外しています:'
            )
            header_lines.append(f'#   {", ".join(excluded_tlv_channels)}')

        channels: list[CATVMirakcChannel] = []
        for carrier, ts_info in _IterReceivableTransportStreams(self._carriers):
            extra_args = str(ts_info.tsmf_relative_ts_number) if ts_info.tsmf_relative_ts_number is not None else ''
            channel: CATVMirakcChannel = {
                'name': _BuildCATVChannelName(ts_info),
                'type': 'GR',  # CATV トランスモジュレーションは mirakc 上 GR (地上波) 扱いにする
                'channel': carrier.physical_channel,
                'extra-args': extra_args,
                'disabled': False,
            }
            channels.append(channel)

        string_io = StringIO()
        yaml = YAML()
        yaml.width = 1000
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        yaml.dump(channels, string_io)
        string_io.seek(0)

        return '\n'.join(header_lines) + '\n\n' + string_io.getvalue()

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

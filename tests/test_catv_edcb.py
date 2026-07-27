from pathlib import Path

from isdb_scanner.catv.constants import CASInfo, PreferredSource
from isdb_scanner.catv.edcb import CATVEDCBChSet4TxtFormatter, CATVEDCBChSet5TxtFormatter
from isdb_scanner.formatter import EDCBChSet4TxtFormatter, EDCBChSet5TxtFormatter

# 合成データビルダーはネイティブ/CATV フォーマッターのテストと共用する (tests/test_catv_formatter.py からそのまま流用)
from tests.test_catv_formatter import (
    BuildSyntheticBSTsInfos,
    BuildSyntheticCarriers,
    BuildSyntheticCSTsInfos,
    BuildSyntheticRetransmissionCarriers,
    BuildSyntheticTerrestrialTsInfos,
)


def ParseChSet(text: str) -> list[list[str]]:
    """ChSet4/ChSet5 の出力文字列 (先頭 BOM + CRLF 区切り TSV) を、フィールドのリストのリストにパースする"""

    # 先頭に UTF-8 BOM が付いていること
    assert text.startswith('﻿')
    body = text[1:]
    lines = body.split('\r\n')
    # csv.writer(lineterminator='\r\n') は各行末に CRLF を付けるため、末尾は必ず空文字列になる
    assert lines[-1] == ''
    return [line.split('\t') for line in lines if line != '']


class TestChSet4NativeEquivalence:
    """carriers を空にした場合、ネイティブ EDCBChSet4TxtFormatter とバイト単位で同一の出力になることのテスト"""

    def test_matches_native_formatter(self, tmp_path: Path):
        # exclude_pay_tv の有無いずれでも、ネイティブ実装と完全一致すること
        for exclude in (False, True):
            # ネイティブ EDCBChSet4TxtFormatter は入力リストを in-place で破壊するため、都度新しい合成データを渡す
            native_str = EDCBChSet4TxtFormatter(
                tmp_path / 'native.ChSet4.txt',
                BuildSyntheticTerrestrialTsInfos(),
                BuildSyntheticBSTsInfos(),
                BuildSyntheticCSTsInfos(),
                exclude,
            ).format()
            catv_str = CATVEDCBChSet4TxtFormatter(
                tmp_path / 'catv.ChSet4.txt',
                [],
                tr_ts_infos=BuildSyntheticTerrestrialTsInfos(),
                bs_ts_infos=BuildSyntheticBSTsInfos(),
                cs_ts_infos=BuildSyntheticCSTsInfos(),
                exclude_pay_tv=exclude,
            ).format()
            assert catv_str == native_str


class TestChSet4Format:
    """ChSet4.txt の書式 (フィールド数・区切り・エンコーディング・改行) のテスト"""

    def test_encoding_and_field_count(self):
        text = CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format()

        # 先頭 BOM・CRLF 区切り
        assert text.startswith('﻿')
        assert '\r\n' in text
        # LF 単独 (CRLF でない改行) が含まれないこと
        assert '\n' not in text.replace('\r\n', '')

        rows = ParseChSet(text)
        # ChSet4 は 12 フィールド
        assert all(len(row) == 12 for row in rows)

    def test_use_view_flag_and_partial_flag(self):
        # CATV は is_oneseg を持たないため partial_flag は常に 0、映像サービス (0x01) の use_view_flag は 1
        rows = ParseChSet(CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())

        video_row = next(row for row in rows if row[1] == 'ＢＳ朝日')  # service_type 0x01 (=1)
        assert video_row[8] == '1'  # service_type
        assert video_row[9] == '0'  # partial_flag (CATV は常に 0)
        assert video_row[10] == '1'  # use_view_flag (映像サービス)

        data_row = next(row for row in rows if row[1] == 'ＢＳ朝日データ')  # service_type 0xC0 (=192)
        assert data_row[8] == '192'
        assert data_row[10] == '0'  # use_view_flag (非映像サービス)


class TestChSet4SpaceAssignment:
    """CATV エントリのチューナー空間 (space) 割り当てのテスト (GR=0 / BS=1 / CS=2 / SKY=3)"""

    def test_bs_cs_gr_space_assignment(self):
        rows = ParseChSet(CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())

        # BS 再送信 → space 1
        assert all(row[3] == '1' for row in rows if row[0] == 'ＢＳ朝日')
        # CS 再送信 → space 2
        assert all(row[3] == '2' for row in rows if row[0] == 'スターチャンネル')
        # 自主放送 (GR) → space 0
        assert all(row[3] == '0' for row in rows if row[0] == 'コミュニティ有料')

    def test_cas_as_sky_maps_to_space3(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers(), cas_as_sky=True).format()
        )
        # C-CAS が必要な自主放送は SKY (space 3) になる
        assert all(row[3] == '3' for row in rows if row[0] == 'コミュニティ有料')
        # B-CAS で受信可能な BS 再送信の space は変わらない
        assert all(row[3] == '1' for row in rows if row[0] == 'ＢＳ朝日')

    def test_chname_matches_mirakurun_channel_name(self):
        # ch_name は _BuildCATVChannelName の結果 (channels_catv.yml の name) と一致すること
        rows = ParseChSet(CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())
        ch_names = {row[0] for row in rows}
        assert 'ＢＳ朝日' in ch_names  # network_name ではなく映像サービス名が使われる
        assert 'スターチャンネル' in ch_names
        assert 'コミュニティ有料' in ch_names  # 自主放送は network_name


class TestChSet4TsmfAndChNumbering:
    """TSMF 多重 TS の展開と、チューナー空間ごとの ch 通し番号のテスト"""

    def test_all_tsmf_ts_emitted_with_distinct_ch(self):
        rows = ParseChSet(CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticCarriers()).format())

        rel1 = next(row for row in rows if row[1] == 'サンプルサービス1')
        rel2 = next(row for row in rows if row[1] == '自主放送チャンネル')
        # TSMF の相対 TS はそれぞれ別チャンネル (ch) として展開される
        assert rel1[3] == '0' and rel1[4] == '0'  # space 0 / ch 0
        assert rel2[3] == '0' and rel2[4] == '1'  # space 0 / ch 1

    def test_empty_ts_consumes_ch_number(self):
        # サービスを持たない CATV_28 (SingleTS) も ch を1つ消費するため、後続のネイティブ地上波の ch がずれる
        # (CATV_15 rel1=ch0 / rel2=ch1 / CATV_28=ch2 (行なし) → ネイティブ T21=ch3)
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(
                Path('unused'), BuildSyntheticCarriers(), tr_ts_infos=BuildSyntheticTerrestrialTsInfos()
            ).format()
        )
        t21 = next(row for row in rows if row[0] == 'Terrestrial:T21')
        assert t21[3] == '0'  # space 0 (CATV GR と共有)
        assert t21[4] == '3'  # 先行する CATV GR エントリ3本 (空の CATV_28 含む) の後


class TestChSet4Filtering:
    """exclude_pay_tv / bcas_only による CATV エントリのフィルタリングのテスト"""

    def test_exclude_pay_tv(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers(), exclude_pay_tv=True).format()
        )
        ch_names = {row[0] for row in rows}
        # 無料 BS 再送信 (相対TS1) は残る
        assert 'ＢＳ朝日' in ch_names
        # 有料映像 + 無料独立データ放送のみの BS 再送信 (WOWOW 型) は丸ごと除外される
        assert all('ＷＯＷＯＷ' not in row[1] for row in rows)
        # CS 再送信は全サービス除外扱いで出力されない (space 2 が存在しない)
        assert all(row[3] != '2' for row in rows)
        # 有料自主放送も除外される
        assert 'コミュニティ有料' not in ch_names

    def test_exclude_pay_tv_does_not_mutate_carriers(self):
        carriers = BuildSyntheticRetransmissionCarriers()
        CATVEDCBChSet4TxtFormatter(Path('unused'), carriers, exclude_pay_tv=True).format()
        # 元の carriers の services が破壊されないこと
        assert len(carriers[0].transport_streams[1].services) == 2
        assert len(carriers[0].transport_streams[2].services) == 1

    def test_bcas_only(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers(), bcas_only=True).format()
        )
        # C-CAS が必要な自主放送は除外される
        assert all(row[0] != 'コミュニティ有料' for row in rows)

    def test_bcas_only_excludes_unknown_cas(self):
        carriers = BuildSyntheticRetransmissionCarriers()
        carriers[1].transport_streams[0].cas = CASInfo(required_card='unknown')
        rows = ParseChSet(CATVEDCBChSet4TxtFormatter(Path('unused'), carriers, bcas_only=True).format())
        assert all(row[0] != 'コミュニティ有料' for row in rows)


class TestChSet4Prefer:
    """prefer による重複 TS の除外 (EDCB は無効化ではなく完全除外) のテスト"""

    def test_prefer_none_keeps_both(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(
                Path('unused'),
                BuildSyntheticRetransmissionCarriers(),
                bs_ts_infos=BuildSyntheticBSTsInfos(),
                cs_ts_infos=BuildSyntheticCSTsInfos(),
            ).format()
        )
        ch_names = {row[0] for row in rows}
        # CATV 再送信・ネイティブの双方が出力される
        assert 'ＢＳ朝日' in ch_names
        assert 'BS:BS01/TS0' in ch_names
        assert 'CS:ND02' in ch_names

    def test_prefer_catv_excludes_duplicated_native(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(
                Path('unused'),
                BuildSyntheticRetransmissionCarriers(),
                bs_ts_infos=BuildSyntheticBSTsInfos(),
                cs_ts_infos=BuildSyntheticCSTsInfos(),
                prefer=PreferredSource.CATV,
            ).format()
        )
        ch_names = {row[0] for row in rows}
        # ネイティブ BS01/TS0 (TSID 16400) は CATV 再送信と重複するため除外される
        assert 'BS:BS01/TS0' not in ch_names
        # ネイティブ ND02 (TSID 24608) も重複するため除外される
        assert 'CS:ND02' not in ch_names
        # 重複しないネイティブ ND04 (TSID 28736) は残る
        assert 'CS:ND04' in ch_names
        # CATV 再送信側は残る
        assert 'ＢＳ朝日' in ch_names

    def test_prefer_native_excludes_duplicated_catv(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(
                Path('unused'),
                BuildSyntheticRetransmissionCarriers(),
                bs_ts_infos=BuildSyntheticBSTsInfos(),
                cs_ts_infos=BuildSyntheticCSTsInfos(),
                prefer=PreferredSource.NATIVE,
            ).format()
        )
        ch_names = {row[0] for row in rows}
        # CATV 再送信 BS (TSID 16400) は重複するため除外される
        assert 'ＢＳ朝日' not in ch_names
        # CATV 再送信 CS (TSID 24608) も重複するため除外される
        assert 'スターチャンネル' not in ch_names
        # ネイティブ側は残る
        assert 'BS:BS01/TS0' in ch_names
        # 重複しない自主放送は残る
        assert 'コミュニティ有料' in ch_names


class TestChSet4NormalizeNames:
    """normalize_names による全角→半角正規化のテスト"""

    def test_normalize_applies_to_ch_name_and_service_name(self):
        rows = ParseChSet(
            CATVEDCBChSet4TxtFormatter(
                Path('unused'), BuildSyntheticRetransmissionCarriers(), normalize_names=True
            ).format()
        )
        # ＢＳ朝日 → BS朝日 に正規化される (ch_name・service_name の両方)
        assert any(row[0] == 'BS朝日' for row in rows)
        assert any(row[1] == 'BS朝日' for row in rows)
        assert all(row[0] != 'ＢＳ朝日' for row in rows)

    def test_normalize_false_keeps_fullwidth(self):
        rows = ParseChSet(CATVEDCBChSet4TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())
        assert any(row[0] == 'ＢＳ朝日' for row in rows)


class TestChSet4Save:
    """save() のファイル書き出しのテスト"""

    def test_save_writes_file(self, tmp_path: Path):
        save_path = tmp_path / 'BonDriver_mirakc(BonDriver_mirakc).ChSet4.txt'
        formatted_str = CATVEDCBChSet4TxtFormatter(save_path, BuildSyntheticRetransmissionCarriers()).save()

        assert save_path.is_file()
        # CRLF が改行変換されずにそのまま書き込まれていることを確認するため、バイト列で比較する
        # (read_text は universal newlines で CRLF→LF に変換してしまうため使わない)
        assert save_path.read_bytes() == formatted_str.encode('utf-8')


class TestChSet5NativeEquivalence:
    """carriers を空にした場合、ネイティブ EDCBChSet5TxtFormatter とバイト単位で同一の出力になることのテスト"""

    def test_matches_native_formatter(self, tmp_path: Path):
        for exclude in (False, True):
            native_str = EDCBChSet5TxtFormatter(
                tmp_path / 'native.ChSet5.txt',
                BuildSyntheticTerrestrialTsInfos(),
                BuildSyntheticBSTsInfos(),
                BuildSyntheticCSTsInfos(),
                exclude,
            ).format()
            catv_str = CATVEDCBChSet5TxtFormatter(
                tmp_path / 'catv.ChSet5.txt',
                [],
                tr_ts_infos=BuildSyntheticTerrestrialTsInfos(),
                bs_ts_infos=BuildSyntheticBSTsInfos(),
                cs_ts_infos=BuildSyntheticCSTsInfos(),
                exclude_pay_tv=exclude,
            ).format()
            assert catv_str == native_str


class TestChSet5Format:
    """ChSet5.txt の書式・内容のテスト"""

    def test_encoding_and_field_count(self):
        text = CATVEDCBChSet5TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format()
        assert text.startswith('﻿')
        rows = ParseChSet(text)
        # ChSet5 は 9 フィールド
        assert all(len(row) == 9 for row in rows)

    def test_service_row_contents(self):
        rows = ParseChSet(CATVEDCBChSet5TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())

        # service_name, network_name, network_id, tsid, service_id, service_type, partial, epg_cap, search
        bs_asahi = next(row for row in rows if row[0] == 'ＢＳ朝日')
        assert bs_asahi[1] == 'ＢＳデジタル'  # network_name
        assert bs_asahi[2] == '4'  # network_id
        assert bs_asahi[3] == '16400'  # transport_stream_id
        assert bs_asahi[5] == '1'  # service_type (映像)
        assert bs_asahi[6] == '0'  # partial_flag (CATV は常に 0)
        assert bs_asahi[7] == '1'  # epg_cap_flag (映像サービス)
        assert bs_asahi[8] == '1'  # search_flag (映像サービス)

    def test_unknown_network_id_becomes_ffff(self):
        # network_id が None の CATV 自主放送 TS は ONID 0xFFFF (=65535) として出力される
        rows = ParseChSet(CATVEDCBChSet5TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())
        community = next(row for row in rows if row[0] == 'コミュニティ有料ch')
        assert community[2] == '65535'

    def test_sorted_by_network_id_then_tsid(self):
        rows = ParseChSet(CATVEDCBChSet5TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers()).format())
        # (network_id, transport_stream_id) の昇順 (代替 ONID 0xFFFF は末尾側) でソートされていること
        keys = [(int(row[2]), int(row[3])) for row in rows]
        assert keys == sorted(keys)

    def test_exclude_pay_tv(self):
        rows = ParseChSet(
            CATVEDCBChSet5TxtFormatter(Path('unused'), BuildSyntheticRetransmissionCarriers(), exclude_pay_tv=True).format()
        )
        service_names = {row[0] for row in rows}
        # 無料 BS 再送信は残る
        assert 'ＢＳ朝日' in service_names
        # CS 有料放送は除外される
        assert 'スターチャンネル' not in service_names
        # 有料自主放送も除外される
        assert 'コミュニティ有料ch' not in service_names


class TestChSet5Save:
    """save() のファイル書き出しのテスト"""

    def test_save_writes_file(self, tmp_path: Path):
        save_path = tmp_path / 'ChSet5.txt'
        formatted_str = CATVEDCBChSet5TxtFormatter(save_path, BuildSyntheticRetransmissionCarriers()).save()

        assert save_path.is_file()
        # CRLF が改行変換されずにそのまま書き込まれていることを確認するため、バイト列で比較する
        assert save_path.read_bytes() == formatted_str.encode('utf-8')

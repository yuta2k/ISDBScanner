from isdb_scanner.catv.analyzer import CATVCarrierAnalyzer
from isdb_scanner.catv.constants import CarrierType, RetransmissionSource
from isdb_scanner.catv.tsmf import NULL_PID, TS_PACKET_SIZE, TS_SYNC_BYTE, TSMF_SLOT_COUNT
from tests.test_cas import (
    BuildCATSection,
    BuildElementaryStreamPacket,
    BuildPATSection,
    BuildPMTSection,
    BuildSDTSection,
    BuildSectionPacket,
)
from tests.test_mmt import BuildTLVCells, BuildTLVPacket
from tests.test_tsmf import BuildTSMFHeaderPacket


def BuildSingleTSPackets(
    transport_stream_id: int = 0x1234,
    ca_system_ids: list[int] | None = None,
    scrambled_count: int = 120,
) -> bytearray:
    """テスト用に、PAT/CAT/PMT/SDT + ES パケットからなる単一 TS のパケット列を組み立てる (test_cas.py のビルダーを流用)"""

    ca_system_ids = ca_system_ids if ca_system_ids is not None else [0x0005]
    pmt_pid = 0x0100
    stream = bytearray()
    stream += BuildSectionPacket(0x0000, BuildPATSection(transport_stream_id, {1: pmt_pid}))
    stream += BuildSectionPacket(0x0001, BuildCATSection(ca_system_ids))
    stream += BuildSectionPacket(pmt_pid, BuildPMTSection(1, pmt_pid, ca_system_ids))
    stream += BuildSectionPacket(0x0011, BuildSDTSection(transport_stream_id, [(1, False)]))
    for i in range(scrambled_count):
        stream += BuildElementaryStreamPacket(0x0101, scrambled=True, continuity_counter=i)
    return stream


def DetermineRetransmissionSource(network_id: int | None, *, nit_analyzed: bool = True) -> RetransmissionSource:
    """テスト用に、private static メソッドの __determineRetransmissionSource を名前マングリング経由で呼び出す"""

    return CATVCarrierAnalyzer._CATVCarrierAnalyzer__determineRetransmissionSource(  # type: ignore[attr-defined]
        network_id, nit_analyzed=nit_analyzed
    )


def BuildNullPacket() -> bytes:
    """テスト用に NULL パケット (PID 0x1FFF) を生成する"""
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = (NULL_PID >> 8) & 0x1F
    packet[2] = NULL_PID & 0xFF
    packet[3] = 0x10
    return bytes(packet)


class TestCATVCarrierAnalyzerSynthetic:
    """合成データによる CATVCarrierAnalyzer の全体パイプラインのテスト (CI でも実行可能)"""

    def test_single_ts_carrier(self):
        # SingleTS: PAT の TSID・CAS 解析結果がそのまま CATVTransportStreamInfo に載ること
        ts_stream = BuildSingleTSPackets(transport_stream_id=0x1234, ca_system_ids=[0x0005])
        carrier_info = CATVCarrierAnalyzer(ts_stream, 'CATV_15').analyze()

        assert carrier_info.physical_channel == 'CATV_15'
        assert carrier_info.carrier_type == CarrierType.SingleTS
        assert len(carrier_info.transport_streams) == 1

        ts_info = carrier_info.transport_streams[0]
        assert ts_info.tsmf_relative_ts_number is None
        assert ts_info.transport_stream_id == 0x1234
        assert ts_info.cas.ca_system_ids == [0x0005]
        assert ts_info.cas.required_card == 'B-CAS'

    def test_tsmf_carrier(self):
        # TSMF: 多重フレームに載せた単一 TS が分離・解析され、相対 TS 番号が付与されること
        payload_packets = BuildSingleTSPackets(transport_stream_id=0x2345, ca_system_ids=[0x0006])
        packet_count = len(payload_packets) // TS_PACKET_SIZE
        table = [1] * TSMF_SLOT_COUNT
        stream = bytearray()
        frame_syncs = (0x1A86, 0x0579)
        frame_index = 0
        offset = 0
        while offset < packet_count:
            stream += BuildTSMFHeaderPacket(table, frame_syncs[frame_index % 2])
            for _ in range(TSMF_SLOT_COUNT):
                if offset < packet_count:
                    stream += payload_packets[offset * TS_PACKET_SIZE : (offset + 1) * TS_PACKET_SIZE]
                    offset += 1
                else:
                    stream += BuildNullPacket()
            frame_index += 1

        carrier_info = CATVCarrierAnalyzer(stream, 'CATV_16').analyze()

        assert carrier_info.carrier_type == CarrierType.TSMF
        assert len(carrier_info.transport_streams) == 1
        ts_info = carrier_info.transport_streams[0]
        assert ts_info.tsmf_relative_ts_number == 1
        assert ts_info.transport_stream_id == 0x2345
        assert ts_info.cas.ca_system_ids == [0x0006]
        assert ts_info.cas.required_card == 'C-CAS'

    def test_tlv_carrier(self):
        # TLV: MMT 解析結果 (mmt) が設定され、transport_streams は空のままになること
        # (detect_carrier_type の TLV 判定には最低 100 セル必要なため、十分な長さの TLV パケットを詰める)
        tlv_packets = BuildTLVPacket(0xFE, bytes(10000)) + BuildTLVPacket(0xFE, bytes(10000))
        ts_stream = bytearray(BuildTLVCells(tlv_packets))
        carrier_info = CATVCarrierAnalyzer(ts_stream, 'CATV_C36').analyze()

        assert carrier_info.carrier_type == CarrierType.TLV
        assert carrier_info.transport_streams == []
        assert carrier_info.mmt is not None

    def test_empty_carrier(self):
        # Empty: NULL パケットのみのキャリアでは TS 情報を含まないこと
        ts_stream = bytearray(b''.join(BuildNullPacket() for _ in range(200)))
        carrier_info = CATVCarrierAnalyzer(ts_stream, 'CATV_C62').analyze()

        assert carrier_info.carrier_type == CarrierType.Empty
        assert carrier_info.transport_streams == []


class TestDetermineRetransmissionSource:
    """ARIB STD-B10 付録N「ネットワーク識別の割当」に基づく再送信元判定のテスト"""

    def test_nit_not_analyzed_is_unknown(self):
        # NIT/SDT の解析自体が失敗している場合は判定材料がないため Unknown
        assert DetermineRetransmissionSource(0x7880, nit_analyzed=False) == 'Unknown'
        assert DetermineRetransmissionSource(None, nit_analyzed=False) == 'Unknown'

    def test_network_id_none_is_self_broadcast(self):
        # NIT は取得できたが自 TS のエントリがなかった場合は自主放送とみなす
        assert DetermineRetransmissionSource(None) == 'SelfBroadcast'

    def test_bs_and_cs(self):
        assert DetermineRetransmissionSource(0x0004) == 'BS'
        assert DetermineRetransmissionSource(0x0006) == 'CS'
        assert DetermineRetransmissionSource(0x0007) == 'CS'

    def test_catv_self_broadcast_range_boundaries(self):
        # 0x7C1F-0x7F5F は CATV 事業者の地デジ網内自主放送 (JCL SPEC-006/007)
        # この範囲は地上波の範囲 (0x7880-0x7FE8) に内包されているため、地上波より優先して判定される必要がある
        assert DetermineRetransmissionSource(0x7C1E) == 'Terrestrial'  # 範囲の直前は地上波再送信
        assert DetermineRetransmissionSource(0x7C1F) == 'SelfBroadcast'  # 範囲の下限
        assert DetermineRetransmissionSource(0x7F5F) == 'SelfBroadcast'  # 範囲の上限
        assert DetermineRetransmissionSource(0x7F60) == 'Terrestrial'  # 範囲の直後は地上波再送信

    def test_terrestrial_range_boundaries(self):
        # 0x7880-0x7FE8 は地上デジタルテレビジョン放送の再送信
        assert DetermineRetransmissionSource(0x7880) == 'Terrestrial'
        assert DetermineRetransmissionSource(0x7FE8) == 'Terrestrial'

    def test_catv_operator_network_ids(self):
        # 0xFFFC: デジアナ変換 (JCL SPEC-008) / 0xFFFD: JC-HITS トラモジ (JCL SPEC-005)
        # 0xFFFE: Digital broadcasting ReMUX (JCL SPEC-003/004) / 0xFFFF: 鹿児島ケーブルテレビ (独自規定)
        assert DetermineRetransmissionSource(0xFFFC) == 'SelfBroadcast'
        assert DetermineRetransmissionSource(0xFFFD) == 'SelfBroadcast'
        assert DetermineRetransmissionSource(0xFFFE) == 'SelfBroadcast'
        assert DetermineRetransmissionSource(0xFFFF) == 'SelfBroadcast'

    def test_unassigned_network_id_falls_back_to_self_broadcast(self):
        # 付録N のどの割当にも該当しない network_id は自主放送として扱う
        assert DetermineRetransmissionSource(0x0100) == 'SelfBroadcast'

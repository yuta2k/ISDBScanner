from isdb_scanner.catv.analyzer import CATVCarrierAnalyzer
from isdb_scanner.catv.constants import CarrierType
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

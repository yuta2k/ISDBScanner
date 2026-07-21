from __future__ import annotations

from isdb_scanner.analyzer import TransportStreamAnalyzer
from isdb_scanner.catv.cas import AnalyzeCAS, ExtractSDTFreeCAModeMap, ExtractTransportStreamId
from isdb_scanner.catv.constants import (
    CarrierType,
    CATVCarrierInfo,
    CATVServiceInfo,
    CATVTransportStreamInfo,
    RetransmissionSource,
)
from isdb_scanner.catv.mmt import ExtractTLVCarrierGroupInfo, ExtractTLVStream, MMTAnalyzer
from isdb_scanner.catv.tsmf import TS_PACKET_SIZE, TSMFDemultiplexer


# 解析対象として扱う最低パケット数
# TSMF 分離直後は空きスロットの残骸や誤検出でごく短いストリームが紛れ込むことがあるため、
# これ未満のパケット数しかないストリームは解析不能として無視する
MIN_ANALYZABLE_PACKET_COUNT = 100


class CATVCarrierAnalyzer:
    """
    CATV トランスモジュレーション物理チャンネル (1 キャリア分の受信データ) を解析するクラス
    キャリア種別 (TSMF/SingleTS/TLV/Empty) を判定した上で、TSMF/SingleTS キャリアについては
    多重されている各 TS を既存の TransportStreamAnalyzer (ariblib ベース) に投入し、CAS 解析・再送信元判定と合わせて返す
    """

    def __init__(self, ts_stream: bytearray, physical_channel: str) -> None:
        """
        CATVCarrierAnalyzer を初期化する

        Args:
            ts_stream (bytearray): チューナーから受信した TS ストリーム (1 キャリア分)
            physical_channel (str): 選局した物理チャンネル (ex: "CATV_15")
        """

        self.ts_stream = ts_stream
        self.physical_channel = physical_channel

    def analyze(self) -> CATVCarrierInfo:
        """
        CATV キャリアを解析する

        Returns:
            CATVCarrierInfo: キャリアの解析結果 (TLV/Empty キャリアでは transport_streams が空になる)
        """

        carrier_type = TSMFDemultiplexer.detect_carrier_type(self.ts_stream)
        carrier_info = CATVCarrierInfo(
            physical_channel=self.physical_channel,
            carrier_type=carrier_type,
            transport_streams=[],
        )

        if carrier_type == CarrierType.TLV:
            # 打ち切りパケット (8K マルチキャリア分散伝送では MPT が完全な形で届かないため、部分解析のサルベージ用に収集する)
            truncated_packets: list[bytes] = []
            tlv_stream = ExtractTLVStream(self.ts_stream, truncated_packets)
            # TSMF ヘッダから自キャリアの TLV ストリーム ID とキャリアグループ (8K 分散伝送) 情報を取得する
            carrier_group = ExtractTLVCarrierGroupInfo(self.ts_stream)
            carrier_info.mmt = MMTAnalyzer().analyze(tlv_stream, truncated_packets, carrier_group)
            return carrier_info

        # Empty キャリアには記録すべき TS 情報がない
        if carrier_type not in (CarrierType.TSMF, CarrierType.SingleTS):
            return carrier_info

        if carrier_type == CarrierType.TSMF:
            demuxed_streams = sorted(TSMFDemultiplexer.demux_all(self.ts_stream).items())
            streams: list[tuple[int | None, bytes]] = [
                (relative_ts_number, bytes(stream)) for relative_ts_number, stream in demuxed_streams
            ]
        else:
            # SingleTS: TSMF 多重されていないため、ストリーム全体がそのまま単一の TS になる
            streams = [(None, bytes(self.ts_stream))]

        for relative_ts_number, stream in streams:
            if len(stream) < TS_PACKET_SIZE * MIN_ANALYZABLE_PACKET_COUNT:
                continue
            ts_info = self.__analyzeTransportStream(stream, relative_ts_number)
            if ts_info is not None:
                carrier_info.transport_streams.append(ts_info)

        return carrier_info

    def __analyzeTransportStream(self, ts_stream: bytes, relative_ts_number: int | None) -> CATVTransportStreamInfo | None:
        """分離済みの単一 TS 1本を解析し、CATVTransportStreamInfo を組み立てる (解析不能なら None を返す)"""

        # NIT には同一ネットワークに属する他の TS の情報も含まれているため、「自 TS」の特定は NIT 経由ではなく、
        # 自 TS 自身の PAT (transport_stream_id) から直接行う方が確実
        transport_stream_id = ExtractTransportStreamId(ts_stream)
        if transport_stream_id is None:
            # PAT すら取得できない = 解析不能なストリーム (ノイズ・分離漏れなど)
            return None

        cas_info = AnalyzeCAS(ts_stream)
        free_ca_mode_map = ExtractSDTFreeCAModeMap(ts_stream)

        # 既存の TransportStreamAnalyzer (ariblib ベース) で NIT/SDT からネットワーク情報・サービス名などを取得する
        # ariblib は破損データや未対応の記述子を含む TS で例外を送出することがあるため、
        # 失敗した場合でも CAS 解析結果だけは返せるようにしておく
        network_id: int | None = None
        network_name = 'Unknown'
        services: list[CATVServiceInfo] = []
        analyze_succeeded = False
        try:
            # tuned_physical_channel には CATV の物理チャンネル名 (ex: "CATV_15") をそのまま渡す
            # "T" から始まらない文字列であれば、地上波専用の assert 分岐 (常に1TSのみ想定) には入らないため安全
            ts_infos = TransportStreamAnalyzer(bytearray(ts_stream), self.physical_channel).analyze()
            analyze_succeeded = True
        except Exception:
            ts_infos = []

        if analyze_succeeded:
            ts_info = next((info for info in ts_infos if info.transport_stream_id == transport_stream_id), None)
            if ts_info is not None:
                network_id = ts_info.network_id
                network_name = ts_info.network_name
                for service in ts_info.services:
                    services.append(
                        CATVServiceInfo(
                            service_id=service.service_id,
                            service_type=service.service_type,
                            service_name=service.service_name,
                            # is_free は cas.py で解析した SDT の free_CA_mode を優先し、取得できなければ
                            # ariblib (TransportStreamAnalyzer) 側の解析結果にフォールバックする
                            is_free=free_ca_mode_map.get(service.service_id, service.is_free),
                        )
                    )
                services.sort(key=lambda service_info: service_info.service_id)

        retransmission_source = self.__determineRetransmissionSource(network_id, nit_analyzed=analyze_succeeded)

        return CATVTransportStreamInfo(
            physical_channel=self.physical_channel,
            tsmf_relative_ts_number=relative_ts_number,
            transport_stream_id=transport_stream_id,
            network_id=network_id,
            network_name=network_name,
            retransmission_source=retransmission_source,
            cas=cas_info,
            services=services,
        )

    @staticmethod
    def __determineRetransmissionSource(network_id: int | None, *, nit_analyzed: bool) -> RetransmissionSource:
        """
        network_id (NIT から取得した自 TS のネットワーク ID) から再送信元を判定する
        ref: isdb_scanner/constants.py の TransportStreamInfo.broadcast_type (地上波/BS/CS の network_id 範囲判定ロジック)
        """

        if not nit_analyzed:
            # ariblib での NIT/SDT 解析自体が失敗しており、判定に必要な情報が一切得られていない
            return 'Unknown'
        if network_id is None:
            # NIT の解析自体は成功したが、自 TS の transport_stream_id が NIT に含まれていなかった
            # CATV 事業者が地上波/BS/CS のいずれの中継でもない独自のチャンネルを多重している (自主放送) 可能性が高い
            return 'SelfBroadcast'
        if network_id == 0x0004:
            return 'BS'
        if network_id in (0x0006, 0x0007):
            return 'CS'
        if 0x7880 <= network_id <= 0x7FE8:
            return 'Terrestrial'
        # 既知の範囲に当てはまらない network_id: NIT 自体は取得できているので、TS 自体は正常だが再送信元は特定できない
        return 'SelfBroadcast'

"""Сумісне перетворення торговельних вивантажень на повні TradeEdge."""
import math
from datetime import datetime

from models.schemas import TradeEdge
from parsers.source_common import read_input
from parsers.trade_sources import TradeSourceParser


class CustomsDataParser:
    CRITICAL_HS_PREFIXES = ("8542", "8526", "8471", "8517", "8457", "8458", "8459")

    def __init__(self, raw_filepath, source="panjiva", column_map=None):
        self.raw_filepath = raw_filepath
        self.parser = TradeSourceParser(source, column_map=column_map, hs_prefixes=self.CRITICAL_HS_PREFIXES)
        self.last_result = None

    def parse_records(self):
        content, format = read_input(self.raw_filepath)
        self.last_result = self.parser.parse(content, format)
        for record in self.last_result.records:
            data = record.data
            # Старий TradeEdge вимагає усі поля; неповні записи доступні в last_result.
            if any(data.get(field) is None for field in (
                "shipper", "consignee_inn", "hs_code", "shipment_date", "value_usd", "net_weight_kg"
            )):
                continue
            yield TradeEdge(source_exporter=data["shipper"], target_importer_inn=data["consignee_inn"],
                hs_code=data["hs_code"], product_desc=data["description"] or "", value_usd=data["value_usd"],
                weight_kg=data["net_weight_kg"], date=datetime.fromisoformat(data["shipment_date"]))

    def compute_edge_weights(self, edges):
        stats = {}
        for edge in edges:
            pair = (edge.source_exporter, edge.target_importer_inn)
            value, count = stats.get(pair, (0, 0))
            stats[pair] = (value + edge.value_usd, count + 1)
        return {pair: math.log1p(value) * math.sqrt(count) for pair, (value, count) in stats.items()}

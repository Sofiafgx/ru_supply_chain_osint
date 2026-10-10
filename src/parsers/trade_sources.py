"""ImportGenius, Panjiva і 52wmb: доступні таблиці та CSV/JSON-вивантаження."""
from __future__ import annotations

import re
from datetime import datetime

from models.source_records import ParseResult, SourceRecord
from parsers.source_common import (
    date, get, hs_code, inn, load_rows, number, record_id,
    source_url, soup_html, table_rows, text,
)


ALIASES = {
    "shipment_id": ("shipment_id", "bill_of_lading", "bill of lading number", "bill of lading", "b/l number", "提单号"),
    "shipper": ("source_exporter", "shipper_name", "shipper", "supplier", "supplier name", "exporter", "供应商", "发货人"),
    "consignee": ("consignee_name", "consignee", "importer", "buyer", "buyer name", "采购商", "收货人"),
    "consignee_inn": ("target_importer_inn", "consignee_inn", "importer_tax_id", "ИНН импортера", "ИНН получателя"),
    "hs_code": ("hs_code", "hs-code", "hscode", "hts code", "HS/HTS", "ТН ВЭД", "海关编码"),
    "description": ("product_desc", "goods_description", "description", "goods shipped", "product", "product description", "goods", "Описание товара", "产品描述"),
    "shipment_date": ("shipment_date", "arrival date", "date", "date of shipment", "Дата поставки", "交易日期", "日期"),
    "value_usd": ("value_usd", "customs_value_usd", "price_usd", "value of goods, cif (usd)", "value of goods, item cif (usd)", "total value usd", "金额美元"),
    "net_weight_kg": ("net_weight_kg", "net weight (kg)", "net weight kg", "净重"),
    "gross_weight_kg": ("gross_weight_kg", "gross weight (kg)", "gross weight kgs", "gross weight kg", "毛重"),
    "quantity": ("quantity", "shipment quantity", "数量"),
    "quantity_unit": ("quantity_unit", "shipment unit", "unit", "单位"),
    "origin_country": ("origin_country", "country of origin", "item origin", "原产国"),
}


class TradeSourceParser:
    def __init__(self, source, column_map=None, hs_prefixes=()):
        self.source = source
        # mapping: канонічне поле -> назва колонки у вашому вивантаженні.
        self.column_map = column_map or {}
        unknown = set(self.column_map) - set(ALIASES)
        if unknown:
            raise ValueError("Невідомі поля column_map: " + ", ".join(sorted(unknown)))
        self.hs_prefixes = hs_prefixes

    def parse(self, content, format="csv", url=None):
        result = ParseResult(source=self.source)
        if format == "html":
            soup = soup_html(content)
            rows = table_rows(soup)
            # Panjiva публікує один приклад накладної у парі label/value.
            for sample in soup.select(".sample_bol"):
                row = {}
                for label in sample.select(".label"):
                    value = label.find_next_sibling()
                    if value:
                        row[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)
                rows.append(row)
            result.warnings.append("HTML містить лише видимі записи; повноту історії постачань не підтверджено")
        else:
            rows = load_rows(content, format)
        for row in rows:
            values = {field: get(row, *((self.column_map[field],) if field in self.column_map else aliases))
                      for field, aliases in ALIASES.items()}
            data = {field: text(value) for field, value in values.items()}
            for field in ("value_usd", "net_weight_kg", "gross_weight_kg", "quantity"):
                data[field] = number(values[field])
            data["shipment_date"] = date(values["shipment_date"])
            data["hs_code"] = hs_code(values["hs_code"])
            data["consignee_inn"] = inn(values["consignee_inn"])
            if data["quantity_unit"] is None and text(values["quantity"]):
                unit_match = re.search(r"\d\s+([A-Za-z]+)$", str(values["quantity"]))
                if unit_match:
                    data["quantity_unit"] = unit_match.group(1)
            # Колонка Weight без підтвердженого типу/одиниць залишається сирим значенням.
            data["weight_raw"] = text(get(row, "weight", "weight_raw"))
            if data["description"] and re.fullmatch(r"[Xx\s]+", data["description"]):
                data["description"] = None
            # Статистика профілю або таблиця тарифів не є поставкою.
            if not (data["shipper"] or data["consignee"]) or not (data["shipment_date"] or data["shipment_id"]):
                result.rejected_rows += 1
                continue
            if self.hs_prefixes and not (data["hs_code"] or "").startswith(tuple(self.hs_prefixes)):
                continue
            warnings = []
            missing = [field for field in ("shipment_date", "hs_code", "consignee_inn", "value_usd") if data[field] is None]
            if missing:
                warnings.append("Відсутні або нерозпізнані поля: " + ", ".join(missing))
            if values["gross_weight_kg"] is not None and data["gross_weight_kg"] is None:
                warnings.append("Невалідна вага брутто")
            # Одна накладна може містити різні товарні рядки; номер B/L не є ключем рядка.
            result.records.append(SourceRecord(source=self.source, record_type="shipment", record_id=record_id(data),
                source_url=source_url(get(row, "source_url", "url"), url), data=data, warnings=warnings))
        if not result.records:
            result.warnings.append("Поставок не виявлено; перевірте колонки, фільтри або доступ до детальних даних")
        return result.deduplicate()


class ImportGeniusParser(TradeSourceParser):
    def __init__(self, **kwargs):
        super().__init__("importgenius", **kwargs)


class PanjivaParser(TradeSourceParser):
    def __init__(self, **kwargs):
        super().__init__("panjiva", **kwargs)


class Wmb52Parser(TradeSourceParser):
    def __init__(self, **kwargs):
        super().__init__("52wmb", **kwargs)


def replenishment_intervals(records, consignee_inn, hs_prefix="8542"):
    """Спостережувані інтервали, а не фактичний запас чи TTS."""
    dates = set()
    for record in records:
        data = record.data
        if record.record_type == "shipment" and data.get("consignee_inn") == consignee_inn and (
            data.get("hs_code") or ""
        ).startswith(hs_prefix) and data.get("shipment_date"):
            dates.add(datetime.fromisoformat(data["shipment_date"]).date())
    ordered = sorted(dates)
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:])]
    return {"consignee_inn": consignee_inn, "hs_prefix": hs_prefix, "shipment_days": len(ordered),
            "intervals_days": gaps, "mean_interval_days": sum(gaps) / len(gaps) if gaps else None,
            "note": "Інтервал між видимими поставками; для TTS потрібні запас і добове споживання"}


def estimate_tts(stock_units, daily_consumption_units):
    if stock_units < 0 or daily_consumption_units <= 0:
        raise ValueError("Запас має бути невід'ємним, а добове споживання — додатним")
    import math
    if not all(math.isfinite(x) for x in (stock_units, daily_consumption_units)):
        raise ValueError("Потрібні скінченні значення")
    return stock_units / daily_consumption_units

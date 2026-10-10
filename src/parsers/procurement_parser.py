"""Сумісний інтерфейс для перевірених контрактів ЄІС."""
from datetime import datetime
import logging

from models.schemas import TenderRecord
from parsers.registry_sources import ZakupkiParser
from parsers.source_common import SourceClient, SourceError, inn


class RussianProcurementParser:
    SEARCH_KEYWORDS = {
        "CNC_MACHINES": ["DMG MORI", "HAAS", "FANUC", "токарный", "обрабатывающий", "Siemens 840D", "станок"],
        "MATERIALS": ["углеволокно", "углеродная ткань", "эпоксидная смола", "препрег"],
        "ELECTRONICS": ["микросхема", "контроллер", "навигацион", "антенн", "Комета"],
    }

    def __init__(self, pool=None, api_key=""):
        self.pool = pool
        self.api_key = api_key

    async def scan_company_contracts(self, tax_id):
        if not inn(tax_id):
            raise ValueError("ІПН має містити 10 або 12 цифр")
        if self.pool is not None:
            pool = self.pool
            class PoolClient:
                async def request(self, method, url, **kwargs):
                    response = await pool.request_with_failover(method, url, **kwargs)
                    if response.is_error:
                        raise SourceError(f"HTTP {response.status_code}")
                    return response
            result = await ZakupkiParser().search(PoolClient(), tax_id)
        else:
            async with SourceClient() as client:
                result = await ZakupkiParser().search(client, tax_id)
        records = []
        for record in result.records:
            data = record.data
            if data["customer_inn"] != tax_id and tax_id not in data["supplier_inns"]:
                continue
            if any(data[field] is None for field in ("customer_inn", "price_rub", "sign_date")):
                logging.getLogger(__name__).warning("Пропущено неповний контракт %s", record.record_id)
                continue
            category = next((cat for cat, terms in self.SEARCH_KEYWORDS.items()
                             if any(term.casefold() in data["title"].casefold() for term in terms)), None)
            for supplier in data["supplier_inns"] or [None]:
                records.append(TenderRecord(tender_id=record.record_id, customer_inn=data["customer_inn"],
                    contractor_inn=supplier, title=data["title"], price_rub=data["price_rub"],
                    date=datetime.fromisoformat(data["sign_date"]), cnc_category=category))
        return records

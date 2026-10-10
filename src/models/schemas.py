from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class CompanyNode(BaseModel):
    inn: str = Field(..., description="ІПН компанії (10 або 12 цифр)")
    name: str
    country: str = "RUS"
    tier: int = Field(default=3, description="Рівень ланцюга постачання")

class TradeEdge(BaseModel):
    source_exporter: str
    target_importer_inn: str
    hs_code: str
    product_desc: str
    value_usd: float
    weight_kg: float
    date: datetime

class TenderRecord(BaseModel):
    tender_id: str
    customer_inn: str
    contractor_inn: Optional[str] = None
    title: str
    price_rub: float
    cnc_category: Optional[str] = None
    date: datetime

class BOMComponent(BaseModel):
    weapon_system: str
    component_name: str
    part_number: str
    manufacturer: str
    origin_country: str
    role: str
"""Нормалізовані записи з походженням і без вигаданих відсутніх значень."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    source: str
    record_type: Literal["company", "contract", "case", "shipment", "research"]
    record_id: str
    source_url: Optional[str] = None
    input_file: Optional[str] = None
    input_sha256: Optional[str] = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)


class ParseResult(BaseModel):
    source: str
    records: List[SourceRecord] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    rejected_rows: int = 0

    def deduplicate(self):
        unique = {}
        for record in self.records:
            unique.setdefault((record.record_type, record.record_id), record)
        self.records = list(unique.values())
        return self

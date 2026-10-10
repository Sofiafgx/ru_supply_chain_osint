"""Спільне читання, нормалізація та обмежений HTTP-клієнт."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


class SourceError(ValueError):
    pass


def text(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in ("", "nan", "null", "none", "n/a", "-", "—") else value


def number(value):
    value = text(value)
    if value is None:
        return None
    tokens = re.findall(r"[+-]?(?:\d[\d.,\s\u00a0]*\d|\d|[.,]\d+)(?:[eE][+-]?\d+)?", value)
    if len(tokens) != 1:
        return None
    value = tokens[0].replace("\u00a0", "").replace(" ", "").strip()
    if "," in value and "." in value:
        value = value.replace(",", "") if value.rfind(".") > value.rfind(",") else value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", "") if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", value) else value.replace(",", ".")
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except ValueError:
        return None


def date(value):
    value = text(value)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        for fmt in ("%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                pass
    return None


def inn(value):
    value = text(value)
    return value if value and re.fullmatch(r"\d{10}|\d{12}", value) else None


def hs_code(value):
    value = text(value)
    if value is None:
        return None
    # Не з'єднувати декілька кодів і не доповнювати пропущені нулі.
    value = re.sub(r"[.\s]", "", value)
    return value if re.fullmatch(r"\d{4,10}", value) else None


def record_id(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def key(value):
    return re.sub(r"[\W_]", "", str(value).casefold())


def get(row, *aliases):
    values = {key(k): v for k, v in row.items()}
    for alias in aliases:
        value = values.get(key(alias))
        if text(value) is not None:
            return value
    return None


def rows_from_json(payload, containers=("data", "results", "records", "items", "contracts", "shipments")):
    if isinstance(payload, list):
        if any(not isinstance(row, dict) for row in payload):
            raise SourceError("Очікується масив об'єктів")
        return payload
    if isinstance(payload, dict):
        for container in containers:
            if container in payload and isinstance(payload[container], (list, dict)):
                return rows_from_json(payload[container], containers)
        return [payload]
    raise SourceError("Непідтримувана структура JSON")


def load_rows(content, format):
    if format == "json":
        return rows_from_json(json.loads(content))
    if format == "jsonl":
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise SourceError("Рядок JSONL має бути об'єктом")
        return rows
    if format == "csv":
        sample = content[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(io.StringIO(content.lstrip("\ufeff")), dialect=dialect))
    raise SourceError(f"Непідтримуваний формат: {format}")


def soup_html(content):
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
    visible = soup.get_text(" ", strip=True).casefold()
    if any(marker in title for marker in ("just a moment", "access denied", "oh noes", "captcha", "доступ ограничен")) or (
        len(visible) < 15000 and any(marker in visible for marker in (
            "verify you are human", "подтвердите, что вы не робот", "проверка браузера",
            "protected by anubis", "access denied: error", "checking your browser"))
    ):
        raise SourceError("Сайт повернув антибот-перевірку; потрібна збережена сторінка або офіційний доступ")
    return soup


def table_rows(soup):
    rows = []
    for table in soup.select("table"):
        header_row = table.select_one("thead") or table.select_one("tr")
        if not header_row:
            continue
        headers = [cell.get_text(" ", strip=True) for cell in header_row.select("th, td")]
        for tr in table.select("tr"):
            if tr is header_row or tr.find_parent("thead") or tr.find("th"):
                continue
            cells = tr.find_all("td", recursive=False)
            if len(cells) == len(headers):
                row = dict(zip(headers, (cell.get_text(" ", strip=True) for cell in cells)))
                link = next((a for a in tr.select("a[href]") if re.search(
                    r"/Card/|/contracts?/|shipment|bill.?of.?lading|reestrNumber", a["href"], re.I)), None)
                if link:
                    row["source_url"] = link["href"]
                rows.append(row)
    return rows


def labelled_value(soup, *labels):
    for node in soup.find_all(["th", "dt", "span", "div", "p"]):
        label = node.get_text(" ", strip=True).rstrip(":").strip().casefold()
        if label not in {value.casefold() for value in labels}:
            continue
        sibling = node.find_next_sibling()
        if sibling:
            return sibling.get_text(" ", strip=True)
    return None


def source_url(value, base_url):
    if not text(value):
        return base_url
    resolved = urljoin(base_url or "", str(value))
    return resolved if urlparse(resolved).scheme in ("http", "https") else base_url


class SourceClient:
    """Один проксі за вибором, TLS, паузи, обмежені повтори тільки для тимчасових збоїв."""

    def __init__(self, *, proxy=None, interval=1.0, timeout=25.0, retries=2):
        self.interval = interval
        self.retries = retries
        self.last_request = 0.0
        self.request_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            proxy=proxy, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "SupplyChainOSINT/1.0 (public-data research)", "Accept-Language": "ru,en;q=0.8"},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method, url, **kwargs):
        for attempt in range(self.retries + 1):
            async with self.request_lock:
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    # Не включати URL із секретним API-ключем до повідомлень.
                    raise SourceError(f"Мережева помилка: {type(exc).__name__}") from None
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code in (401, 403, 429):
                raise SourceError(f"HTTP {response.status_code}: доступ обмежений; запити зупинено")
            if response.status_code >= 500 and attempt < self.retries:
                await asyncio.sleep(2 ** attempt)
                continue
            if response.is_error:
                raise SourceError(f"HTTP {response.status_code}")
            if "html" in response.headers.get("content-type", ""):
                soup_html(response.text)
            return response


def read_input(path):
    path = Path(path)
    format = path.suffix.lower().lstrip(".")
    if format == "htm":
        format = "html"
    return path.read_text(encoding="utf-8-sig"), format

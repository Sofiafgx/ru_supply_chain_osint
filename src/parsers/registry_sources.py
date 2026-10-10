"""Checko, ЄІС, Госзатраты і КАД: HTML та структуровані вивантаження."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from lxml import etree

from models.source_records import ParseResult, SourceRecord
from parsers.source_common import (
    SourceError, date, get, inn, labelled_value, load_rows, number,
    record_id, rows_from_json, source_url, soup_html, table_rows, text,
)


class CheckoParser:
    source = "checko"

    def parse(self, content, format="html", url=None):
        result = ParseResult(source=self.source)
        if format == "html":
            soup = soup_html(content)
            heading = soup.select_one("h1")
            corpus = soup.get_text(" ", strip=True)
            def identifier(label, length):
                alternatives = "|".join(r"\d{" + size + "}" for size in length.split(","))
                match = re.search(rf"\b{label}\s*:?\s*({alternatives})\b", corpus)
                return match.group(1) if match else None
            rows = [{"name": heading.get_text(" ", strip=True) if heading else None,
                     "inn": identifier("ИНН", "10,12"), "ogrn": identifier("ОГРН", "13,15"),
                     "kpp": identifier("КПП", "9"),
                     "address": labelled_value(soup, "Юридический адрес", "Адрес")}]
        else:
            payload = json.loads(content) if format == "json" else None
            if isinstance(payload, dict):
                meta = payload.get("meta") or {}
                if payload.get("error") or meta.get("status") == "error":
                    raise SourceError("Checko API повідомив про помилку доступу або запиту")
                if payload.get("data") == {} or payload.get("data") is None and "data" in payload:
                    return result
            rows = rows_from_json(payload) if payload is not None else load_rows(content, format)
        for row in rows:
            name = text(get(row, "name", "НаимСокр", "НаимПолн", "company_name", "ФИО"))
            tax_id = inn(get(row, "inn", "ИНН"))
            ogrn = text(get(row, "ogrn", "ОГРН"))
            if not name or not (tax_id or ogrn):
                result.rejected_rows += 1
                continue
            address = get(row, "address", "ЮрАдрес")
            status = get(row, "status", "Статус")
            data = {"name": name, "inn": tax_id, "ogrn": ogrn, "kpp": text(get(row, "kpp", "КПП")),
                    "address": address, "status": status, "registration_date": date(get(row, "ДатаРег", "registration_date"))}
            result.records.append(SourceRecord(source=self.source, record_type="company", record_id=tax_id or ogrn,
                                                source_url=url, data=data))
        if not result.records and format == "html":
            result.warnings.append("Профіль компанії не знайдено або структура сторінки змінилася")
        return result.deduplicate()

    async def fetch_company(self, client, tax_id, api_key):
        if not inn(tax_id):
            raise SourceError("ІПН має містити 10 або 12 цифр")
        if not api_key:
            raise SourceError("Для пошуку Checko за ІПН задайте CHECKO_API_KEY; HTML-профіль можна передати через --url")
        endpoint = "entrepreneur" if len(tax_id) == 12 else "company"
        url = f"https://api.checko.ru/v2/{endpoint}"
        response = await client.request("GET", url, params={"key": api_key, "inn": tax_id})
        return self.parse(response.text, "json", url)


def xml_contract_rows(content):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(content.encode(), parser)
    except etree.XMLSyntaxError:
        raise SourceError("Некоректний XML контрактів") from None
    def find(node, *names):
        for name in names:
            matches = node.xpath('.//*[local-name()=$name]/text()', name=name)
            if matches:
                return matches[0].strip()
        return None
    nodes = root.xpath('//*[local-name()="contract"]')
    if not nodes and etree.QName(root).localname == "contract":
        nodes = [root]
    rows = []
    for node in nodes:
        customers = node.xpath('.//*[local-name()="customer"]')
        suppliers = node.xpath('.//*[local-name()="supplier" or local-name()="supplierInfo"]')
        products = node.xpath('.//*[local-name()="product"]/*[local-name()="name"]/text()')
        rows.append({"regNum": find(node, "regNum", "regNumber", "reestrNumber"),
                     "subject": find(node, "subject") or "; ".join(products) or None,
                     "price": find(node, "price"), "signDate": find(node, "signDate"),
                     "customer": {"inn": find(customers[0], "INN", "inn") if customers else None},
                     "suppliers": [{"inn": find(s, "INN", "inn")} for s in suppliers]})
    return rows


class ContractParser:
    def __init__(self, source):
        self.source = source

    def parse(self, content, format="html", url=None):
        result = ParseResult(source=self.source)
        if format == "xml":
            rows = xml_contract_rows(content)
        elif format == "html":
            soup = soup_html(content)
            rows = table_rows(soup)
            for card in soup.select(".search-registry-entry-block"):
                link = card.select_one('a[href*="reestrNumber"], a[href*="regNumber"]')
                if not link:
                    continue
                params = parse_qs(urlparse(link["href"]).query)
                title = labelled_value(card, "Объект закупки", "Предмет контракта")
                title_node = card.select_one(".registry-entry__body-value")
                rows.append({"regNum": (params.get("reestrNumber") or params.get("regNumber") or [None])[0],
                             "title": title or (title_node.get_text(" ", strip=True) if title_node else None),
                             "price": labelled_value(card, "Цена контракта", "Начальная цена"),
                             "signDate": labelled_value(card, "Заключение контракта", "Дата заключения контракта"),
                             "source_url": link["href"]})
            if not rows:
                params = parse_qs(urlparse(url or "").query)
                number_id = labelled_value(soup, "Реестровый номер контракта", "Номер контракта")
                number_id = number_id or (params.get("reestrNumber") or [None])[0]
                if number_id:
                    rows = [{"regNum": number_id,
                             "title": labelled_value(soup, "Предмет контракта", "Объект закупки"),
                             "price": labelled_value(soup, "Цена контракта"),
                             "signDate": labelled_value(soup, "Дата заключения контракта"),
                             "customer_inn": labelled_value(soup, "ИНН заказчика"),
                             "supplier_inn": labelled_value(soup, "ИНН поставщика")}]
        else:
            rows = load_rows(content, format)
        for row in rows:
            identifier = text(get(row, "regNum", "regNumber", "reestrNumber", "contractNumber", "Номер контракта", "tender_id"))
            products = row.get("products") or []
            title = text(get(row, "title", "subject", "Предмет контракта", "Наименование объекта закупки", "product_name"))
            title = title or "; ".join(str(p.get("name")) for p in products if isinstance(p, dict) and p.get("name")) or None
            if not identifier or not title:
                result.rejected_rows += 1
                continue
            customer = row.get("customer") or {}
            suppliers = row.get("suppliers") or []
            customer_inn = inn(get(row, "customer_inn", "ИНН заказчика")) or (inn(get(customer, "inn", "ИНН")) if isinstance(customer, dict) else None)
            supplier_inns = [value for value in (inn(get(s, "inn", "ИНН")) for s in suppliers if isinstance(s, dict)) if value]
            supplier_inns.extend(value for value in (inn(v) for v in (row.get("supplier_inns") or []))
                                 if value and value not in supplier_inns)
            supplier_inn = inn(get(row, "supplier_inn", "contractor_inn", "ИНН поставщика"))
            if supplier_inn and supplier_inn not in supplier_inns:
                supplier_inns.append(supplier_inn)
            price = number(get(row, "amount_rur", "price", "price_rub", "Цена контракта", "contract_price"))
            currency = row.get("currency")
            currency_code = get(currency, "code", "name") if isinstance(currency, dict) else text(currency)
            explicit_rub = get(row, "amount_rur", "price_rub")
            if currency_code and str(currency_code).upper() not in ("RUB", "RUR", "643", "РОССИЙСКИЙ РУБЛЬ") and explicit_rub is None:
                price = None
            sign_date = date(get(row, "signDate", "date", "Дата заключения", "Дата заключения контракта", "sign_date"))
            supplier_names = list(row.get("supplier_names") or [])
            scalar_name = text(row.get("supplier_name"))
            if scalar_name and scalar_name not in supplier_names:
                supplier_names.append(scalar_name)
            for supplier in suppliers:
                if isinstance(supplier, dict):
                    name = text(get(supplier, "name", "fullName", "organizationName"))
                    if name and name not in supplier_names:
                        supplier_names.append(name)
            data = {"contract_number": identifier, "title": title, "customer_inn": customer_inn,
                    "customer_name": text(get(row, "customer_name")),
                    "supplier_inns": supplier_inns, "supplier_names": supplier_names,
                    "notif_num": text(row.get("notif_num")),
                    "purchase_code": text(row.get("purchase_code")),
                    "price_rub": price, "sign_date": sign_date, "currency_original": currency,
                    "amount_original": number(get(row, "amount", "price")),
                    "product_codes": row.get("product_codes") or [],
                    "product_names": row.get("product_names") or [], "law": text(row.get("fz")),
                    "source_execution_stage": row.get("current_stage")}
            data["source_text_fields"] = {key: row[key] for key in
                ("notice_title", "contract_subject", "supplier_clause", "description", "specification", "specifications")
                if row.get(key) is not None}
            missing = [field for field in ("customer_inn", "price_rub", "sign_date") if data[field] is None]
            origin = source_url(get(row, "source_url", "contractUrl"), url)
            if self.source == "clearspending" and text(row.get("fz")):
                origin = f"https://clearspending.ru/contracts/{row['fz']}/{identifier}"
            result.records.append(SourceRecord(source=self.source, record_type="contract", record_id=identifier,
                source_url=origin, data=data,
                warnings=["Відсутні поля: " + ", ".join(missing)] if missing else []))
        if not result.records and format == "html":
            result.warnings.append("Контрактів у HTML не виявлено: перевірте сторінку, доступ або JS-вивантаження")
        return result.deduplicate()


class ZakupkiParser(ContractParser):
    def __init__(self):
        super().__init__("zakupki")

    async def search(self, client, query, pages=1):
        result = ParseResult(source=self.source)
        for page in range(1, pages + 1):
            url = "https://zakupki.gov.ru/epz/contract/search/results.html"
            response = await client.request("GET", url, params={"searchString": query, "pageNumber": page,
                                                               "recordsPerPage": "_10", "searchResultView": "GENERAL"})
            parsed = self.parse(response.text, "html", str(response.url))
            result.records.extend(parsed.records)
            result.warnings.extend(parsed.warnings)
            result.rejected_rows += parsed.rejected_rows
            if not parsed.records:
                break
        return result.deduplicate()


class ClearSpendingParser(ContractParser):
    def __init__(self):
        super().__init__("clearspending")

    async def search(self, client, query, pages=1):
        # Публічна відповідь, яку використовує поточна сторінка /contracts.
        url = "https://clearspending.ru/api/gw/filtered-contracts/"
        result = ParseResult(source=self.source)
        for page in range(1, pages + 1):
            response = await client.request("GET", url, params={"search": query, "page": page, "page_size": 25})
            payload = response.json()
            if "data" not in payload or not isinstance(payload["data"], list):
                raise SourceError("Нерозпізнана відповідь пошуку Госзатрат")
            parsed = self.parse(response.text, "json", url)
            result.records.extend(parsed.records)
            result.rejected_rows += parsed.rejected_rows
            if not payload["data"] or page >= (payload.get("total_pages") or page + 1):
                break
        return result.deduplicate()


class KadParser:
    source = "kad"

    def parse(self, content, format="html", url=None):
        result = ParseResult(source=self.source)
        if format == "html":
            soup = soup_html(content)
            rows = []
            for tr in soup.select("tr"):
                link = tr.select_one('a[href*="/Card/"]')
                if not link:
                    continue
                # КАД дублює назви та адреси у прихованих підказках.
                for tooltip in tr.select('.js-rolloverHtml'):
                    tooltip.decompose()
                def cell(selector):
                    node = tr.select_one(selector)
                    return node.get_text(" ", strip=True) if node else None
                judge = tr.select_one('td.court .judge')
                court = judge.find_next_sibling() if judge else None
                rows.append({"CaseId": link["href"].rstrip("/").split("/")[-1],
                             "CaseNumber": link.get_text(" ", strip=True),
                             "CourtName": court.get_text(" ", strip=True) if court else cell("td.court"),
                             "Judge": cell("td.court .judge, td.judge"),
                             "Plaintiffs": cell("td.plaintiff"), "Respondents": cell("td.respondent"),
                             "Date": cell("td.num div[title] span, .date"), "source_url": link["href"]})
            if not rows:
                total = soup.select_one("#documentsTotalCount")
                if not total or total.get("value") != "0":
                    result.warnings.append("Результатів КАД не виявлено; потрібен HTML відповіді пошуку або JSON")
        else:
            rows = rows_from_json(json.loads(content), ("Result", "Items", "data", "cases")) if format == "json" else load_rows(content, format)
        for row in rows:
            case_number = text(get(row, "CaseNumber", "case_number", "Номер дела"))
            if not case_number:
                result.rejected_rows += 1
                continue
            identifier = text(get(row, "CaseId", "case_id")) or case_number
            data = {"case_number": case_number, "court": get(row, "CourtName", "Court", "Суд"),
                    "judge": get(row, "Judge", "Судья"), "registration_date": date(get(row, "Date", "registration_date")),
                    "plaintiffs": get(row, "Plaintiffs", "Истец"), "respondents": get(row, "Respondents", "Ответчик")}
            result.records.append(SourceRecord(source=self.source, record_type="case", record_id=identifier,
                source_url=source_url(get(row, "source_url"), url), data=data))
        return result.deduplicate()

    async def search(self, client, participant, pages=1):
        await client.request("GET", "https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F")
        result = ParseResult(source=self.source)
        url = "https://kad.arbitr.ru/Kad/SearchInstances"
        for page in range(1, pages + 1):
            response = await client.request("POST", url, json={"Page": page, "Count": 25,
                "Sides": [{"Name": participant, "Type": -1, "ExactMatch": False}],
                "Judges": [], "Courts": [], "CaseNumbers": [], "DateFrom": None, "DateTo": None,
                "WithVKSInstances": False}, headers={"x-date-format": "iso", "Referer": "https://kad.arbitr.ru/"})
            parsed = self.parse(response.text, "html", url)
            result.records.extend(parsed.records)
            result.warnings.extend(parsed.warnings)
            result.rejected_rows += parsed.rejected_rows
            if not parsed.records:
                break
        return result.deduplicate()

"""Evidence-preserving extraction from individual public notices and exports."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from parsers.source_common import SourceError, date, inn, soup_html

BRANDS = {
    "Haas": r"Haas|Хаас",
    "DMG Mori": r"DMG[\s-]*Mori|ДМГ[\s-]*Мори",
    "Fanuc": r"Fanuc|Fanuk|Fanuс|Фанук",
    "Siemens": r"Siemens|Сименс",
    "Doosan": r"Doosan|Дусан",
    "Sodick": r"Sodick|Содик",
}
# Only explicit model families; do not turn the next word into a model.
MODELS = {
    "Haas": r"(?:VF|VM|DT|DM|ST|SL|UMC|EC|DS|TL|TM|GR|VR)[\s-]*\d+[A-Za-z0-9/-]*(?:\s+THE)?",
    "DMG Mori": r"(?:DMC|DMU|DMF|CTX|NLX|NTX|CMX|CLX|NHX|NZX)[\s-]*(?:(?:gamma|beta|alpha)\s+)?\d+[A-Za-z0-9/-]*(?:\s+(?:V|TC|duoBLOCK|ecoline))?",
    "Fanuc": r"(?:Series\s+)?(?:\d{1,3}|[0OО])(?:[iIІ]|[iIІ]-[A-Za-z]+)[A-Za-z0-9-]*(?:\s+[A-Za-z]{1,3})?|A\d{2}[A-Za-z]-\d{4}-[A-Za-z0-9]+",
    "Siemens": r"(?:Sinumerik\s*)?(?:8[024]\d[A-Za-z]*|ONE)|Sinumerik",
    "Doosan": r"(?:Puma|Lynx|DNM|DNMV|DHP|DVC|DBM|NHM|NHP|VC|VM|MYNX)[\s-]*\d+[A-Za-z0-9/-]*",
    "Sodick": r"(?:ALC|ALN|AG|AQ|AP|AD|A|SL|VL|K)[\s-]*\d+[A-Za-z0-9/-]*",
}
CNC = re.compile(r"ЧПУ|CNC|станк|обрабатывающ\w*\s+центр|фрезер|токар|числов\w*\s+программ", re.I)
MAINTENANCE = re.compile(r"ремонт|техническ\w*\s+обслуж|\bТО\b|диагност|восстановлен\w*\s+работоспособ", re.I)
MONTHS = {v: i + 1 for i, v in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())}


def notice_date(value):
    if value is None:
        return None
    value = str(value).strip()
    aspnet = re.fullmatch(r"/Date\((\d+)(?:[+-]\d{4})?\)/", value)
    if aspnet:
        return datetime.fromtimestamp(int(aspnet[1])/1000, timezone(timedelta(hours=3))).date().isoformat()
    standard = date(value)
    if standard:
        return standard
    match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", value)
    if match:
        return date(match[1])
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", value):
        try:
            return datetime.strptime(value, "%d.%m.%y").date().isoformat()
        except ValueError:
            return None
    match = re.fullmatch(r"(\d{1,2})\s+(\w+)\s+(\d{4})(?:,?\s+\d{1,2}:\d{2})?", value)
    if match and match[2].lower() in MONTHS:
        try:
            return datetime(int(match[3]), MONTHS[match[2].lower()], int(match[1])).date().isoformat()
        except ValueError:
            return None
    return None


def equipment_mentions(body):
    result = []
    for brand, alias in BRANDS.items():
        pattern = re.compile(r"(?<!\w)(?:" + alias + r")(?!\w)", re.I)
        for match in pattern.finditer(body):
            suffix = body[match.end():]
            model_match = re.match(r"[\s:–—-]*(" + MODELS[brand] + r")(?!\w)", suffix, re.I)
            model = model_match[1].strip() if model_match else None
            end = match.end() + (model_match.end() if model_match else 0)
            result.append({"brand": brand, "model": model,
                           "equipment_type": "cnc_control_or_component" if brand in ("Fanuc", "Siemens") else "machine",
                           "raw_mention": body[match.start():end],
                           "context": body[max(0, match.start()-80):min(len(body), end+100)]})
    return list({(x["brand"], x["model"]): x for x in result}.values())


def normalize_notice(row, source):
    title = str(row.get("title") or "").strip()
    body = BeautifulSoup(str(row.get("description") or ""), "html.parser").get_text(" ", strip=True)
    content = title + "\n" + body
    url = row.get("source_url")
    if not title or not url or urlparse(url).scheme not in ("https", "http"):
        return None, "missing_title_or_direct_url"
    if source == "indparks":
        return None, "context_only_company_profile"
    if not CNC.search(content):
        return None, "no_cnc_context"
    mentions = equipment_mentions(content)
    if not mentions:
        return None, "no_target_brand"
    if source == "rostender" and (not MAINTENANCE.search(content) or re.match(r"\s*(поставка|закупка|приобретение)\b", title, re.I)):
        return None, "not_maintenance_service"
    record = {
        "company_tax_id": inn(row.get("company_tax_id")),
        "vacancy_or_tender_title": title,
        "equipment_brand_model": "; ".join(x["brand"] + (" " + x["model"] if x["model"] else "") for x in mentions),
        "posting_date": notice_date(row.get("posting_date")),
        "closure_date": notice_date(row.get("closure_date")),
        "activity_status_proxy": "equipment_maintenance" if source == "rostender" or MAINTENANCE.search(title) else "capacity_expansion",
        "source_url": url,
    }
    evidence = {
        "source": source, "source_url": url, "company_name": row.get("company_name"),
        "equipment_mentions": mentions, "posting_date_raw": row.get("posting_date"),
        "closure_date_raw": row.get("closure_date"),
        "application_deadline": notice_date(row.get("application_deadline")),
        "archived": row.get("archived"), "warnings": [],
        "verification_scope": row.get("verification_scope", "individual_notice"),
        "listing_url": row.get("listing_url"),
    }
    if not record["company_tax_id"]:
        evidence["warnings"].append("ІПН роботодавця/замовника не встановлено; ІПН порталу не використовується.")
    if not record["closure_date"]:
        evidence["warnings"].append("Дата закриття невідома; дедлайн заявок, validThrough та статус архіву не замінюють її.")
    if record["activity_status_proxy"] == "capacity_expansion":
        evidence["warnings"].append("Вакансія — слабкий проксі попиту на кадри, не доказ розширення потужностей.")
    return (record, evidence), None


def json_rows(payload, source, source_url=None):
    if isinstance(payload, list):
        return [r for item in payload for r in json_rows(item, source, source_url)]
    if not isinstance(payload, dict):
        raise SourceError("Очікується об'єкт або масив оголошень")
    if source == "trudvsem":
        if isinstance(payload.get("results"), dict):
            return json_rows(payload["results"].get("vacancies", []), source, source_url)
        if isinstance(payload.get("vacancy"), dict):
            return json_rows(payload["vacancy"], source, source_url)
        if "job-name" in payload:
            company = payload.get("company") or {}
            requirement = payload.get("requirement") or {}
            return [{"title": payload["job-name"],
                     "description": "\n".join(str(x or "") for x in
                         (payload.get("duty"), payload.get("requirements"),
                          requirement.get("qualification"), payload.get("qualification"))),
                     "company_name": company.get("name"), "company_tax_id": company.get("inn"),
                     "posting_date": payload.get("creation-date"),
                     "source_url": payload.get("vac_url") or source_url}]
    if "data" in payload and isinstance(payload["data"], list):
        return json_rows(payload["data"], source, source_url)
    if "items" in payload:
        return json_rows(payload["items"], source, source_url)
    if "VacancySend" in payload:
        v = payload["VacancySend"]
        return [{"title": v.get("Name"), "company_name": v.get("Company"),
                 "description": "\n".join(str(v.get(k) or "") for k in ("Responsibilities", "Requirements", "Skills", "WorkingСonditions")),
                 "posting_date": v.get("PublicationDate"),
                 "source_url": source_url or f"https://fkc-opk.ru/PublicResumeUser/AJGetVacancyMoreDetailed?id={v['id']}"}]
    if source == "hh" and "name" in payload:
        employer = payload.get("employer") or {}
        return [{"title": payload["name"], "description": payload.get("description"),
                 "company_name": employer.get("name"), "company_tax_id": employer.get("inn"),
                 "posting_date": payload.get("published_at"), "archived": payload.get("archived"),
                 "closure_date": payload.get("archived_at"), "source_url": payload.get("alternate_url") or source_url}]
    return [payload]


def html_rows(content, source, source_url):
    s = soup_html(content)
    if source == "indparks":
        return []
    if source == "rostender" and "/category/" in urlparse(source_url).path:
        rows = []
        for link in s.select('a.tender-info__link[href]'):
            card = link.find_parent(class_="tender-row__wrapper")
            if not card:
                continue
            published = card.select_one(".tender__date-start")
            deadline = card.select_one(".tender__date-end")
            href = link["href"]
            if not re.search(r"/\d{7,}-", href):
                continue
            rows.append({"title": link.get_text(" ", strip=True),
                         "description": "",
                         "source_url": "https://rostender.info" + href if href.startswith("/") else href,
                         "posting_date": published.get_text(" ", strip=True).replace("от", "").strip() if published else None,
                         "application_deadline": deadline.get_text(" ", strip=True) if deadline else None,
                         "verification_scope": "live_listing_card; detail_not_verified",
                         "listing_url": source_url})
        return rows
    jobs = []
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("@type") == "JobPosting" or "JobPosting" in (value.get("@type") or []):
                org = value.get("hiringOrganization") or {}
                if not isinstance(org, dict):
                    org = {}
                jobs.append({"title": value.get("title"), "description": value.get("description"),
                             "posting_date": value.get("datePosted"), "company_name": org.get("name"),
                             "source_url": value.get("url") or source_url})
            if "@graph" in value:
                visit(value["@graph"])
    for script in s.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
    if jobs:
        if source == "hh":
            # HH explicitly publishes archive dates in the individual page title.
            # This is actual archival evidence, unlike validThrough.
            page_title = s.title.get_text(" ", strip=True) if s.title else ""
            archive = re.search(r"в архиве\s+[cс]\s+(\d{1,2}\s+\w+\s+\d{4})", page_title, re.I)
            if archive:
                for job in jobs:
                    job["closure_date"] = archive[1]
                    job["archived"] = True
        return jobs
    h1 = s.find("h1")
    if not h1:
        return []
    if source == "cznelb":
        description = s.select_one(".vacancy-view__body, .vacancy-view__description, .vacancy-view__text")
        if not description:
            return []
        company = s.select_one(".vacancy-view__subtitle")
        return [{"title": h1.get_text(" ", strip=True), "description": description.get_text(" ", strip=True),
                 "company_name": company.get_text(" ", strip=True) if company else None, "source_url": source_url}]
    if source == "joblab" and re.search(r"/vacancy/\d+", urlparse(source_url).path):
        # Stop before recommendations, and only parse the individual vacancy.
        main = s.select_one("main") or s
        for node in main.select("script, style, nav, footer"):
            node.decompose()
        page_text = main.get_text(" ", strip=True).split("Похожие вакансии")[0]
        sections = []
        for tr in main.select("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) >= 2 and cells[0].get_text(" ", strip=True).rstrip(" *:") in ("Обязанности", "Требования", "Условия"):
                sections.append(cells[1].get_text(" ", strip=True))
        if not sections:
            return []
        pub = re.search(r"\b\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4}", page_text)
        return [{"title": h1.get_text(" ", strip=True), "description": "\n".join(sections),
                 "posting_date": pub[0] if pub else None, "source_url": source_url}]
    if source == "rostender" and re.search(r"/\d{7,}-", urlparse(source_url).path):
        # Individual notice only. Never use category/filter/sidebar brands.
        title = h1.get_text(" ", strip=True)
        body = s.select_one('[itemprop="description"], .tender-description')
        pub = s.select_one('[itemprop="datePublished"]')
        # The individual header uses 'от DD.MM.YY'; never select a footer date.
        header_text = s.get_text(" ", strip=True).split(title, 1)[0]
        pub_header = re.search(r"\bот\s+(\d{2}\.\d{2}\.\d{2,4})\b", header_text)
        return [{"title": title, "description": body.get_text(" ", strip=True) if body else "",
                 "posting_date": (pub.get("content") or pub.get_text(strip=True)) if pub else pub_header[1] if pub_header else None,
                 "source_url": source_url}]
    return []


def parse_notices(content, source, source_url=None, format="json"):
    if format == "html":
        rows = html_rows(content, source, source_url or "")
    else:
        payload = [json.loads(x) for x in content.splitlines() if x.strip()] if format == "jsonl" else json.loads(content)
        rows = json_rows(payload, source, source_url)
    records, evidence, rejected = [], [], {}
    seen = set()
    for row in rows:
        parsed, reason = normalize_notice(row, source)
        if parsed is None:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        record, proof = parsed
        identity = record["source_url"]
        if identity not in seen:
            records.append(record)
            evidence.append(proof)
            seen.add(identity)
    return {"records": records, "evidence": evidence, "rejected": rejected,
            "notices_examined": len(rows), "collected_at": datetime.now(timezone.utc).isoformat()}

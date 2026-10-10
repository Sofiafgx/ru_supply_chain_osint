"""Каталог звітів C4ADS і посилання на опубліковані матеріали."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from models.source_records import ParseResult, SourceRecord
from parsers.source_common import date, get, load_rows, record_id, source_url, soup_html, text


class C4ADSParser:
    source = "c4ads"

    def parse(self, content, format="html", url=None):
        result = ParseResult(source=self.source)
        if format != "html":
            rows = load_rows(content, format)
        else:
            soup = soup_html(content)
            rows = []
            is_detail = bool(re.search(r"/reports/[^/]+/?$", urlparse(url or "").path))
            for heading in ([] if is_detail else soup.select(".post-list-item-title")):
                link = heading.find_parent("a", href=True)
                if not link:
                    continue
                excerpt = link.select_one(".post-list-excerpt")
                rows.append({"title": heading.get_text(" ", strip=True), "source_url": link["href"],
                             "summary": excerpt.get_text(" ", strip=True) if excerpt else None})
            if is_detail:
                heading = soup.select_one("h1")
                main = heading.find_parent("article") if heading else None
                main = main or soup.select_one("main, article")
                published = soup.select_one('meta[property="article:published_time"], time[datetime]')
                published_text = soup.select_one(".publication-date")
                if heading and main:
                    assets = [source_url(a["href"], url) for a in main.select("a[href]")
                              if re.search(r"\.(pdf|csv|json|xlsx|zip)(\?|$)", a["href"], re.I)]
                    rows.append({"title": heading.get_text(" ", strip=True),
                                 "summary": main.get_text(" ", strip=True),
                                 "published_date": published.get("content", published.get("datetime")) if published else
                                     (published_text.get_text(" ", strip=True) if published_text else None),
                                 "assets": list(dict.fromkeys(assets))})
        for row in rows:
            title = text(get(row, "title", "name"))
            if not title:
                result.rejected_rows += 1
                continue
            report_url = source_url(get(row, "source_url", "url"), url)
            data = {"title": title, "summary": text(get(row, "summary", "description")),
                    "published_date": date(get(row, "published_date", "date")), "assets": row.get("assets") or []}
            result.records.append(SourceRecord(source=self.source, record_type="research",
                record_id=record_id({"url": report_url} if report_url else {"title": title}), source_url=report_url, data=data,
                warnings=["Дослідницька публікація; не є митною декларацією або підтвердженням запасів"]))
        if not result.records:
            result.warnings.append("Публікацій C4ADS не виявлено; перевірте URL або структуру сторінки")
        return result.deduplicate()

    async def search(self, client, query="", pages=1):
        result = ParseResult(source=self.source)
        for page in range(1, pages + 1):
            url = "https://c4ads.org/category/reports/" if page == 1 else f"https://c4ads.org/category/reports/page/{page}/"
            response = await client.request("GET", url)
            parsed = self.parse(response.text, "html", url)
            result.records.extend(record for record in parsed.records if not query or
                                  query.casefold() in (record.data["title"] + " " + (record.data["summary"] or "")).casefold())
            result.warnings.extend(parsed.warnings)
            if not parsed.records:
                break
        return result.deduplicate()

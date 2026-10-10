"""Відновлюваний збір: сирі відповіді, SQLite, контроль охоплення, CSV/JSONL."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pypdf import PdfReader
from pypdf.errors import PdfReadError, DependencyError

from models.source_records import SourceRecord
from parsers.c4ads_parser import C4ADSParser
from parsers.registry_sources import CheckoParser, ClearSpendingParser, KadParser, ZakupkiParser
from parsers.source_common import SourceClient, SourceError, record_id, source_url
from sources_cli import PARSERS, validate_url


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


QUERY_TERMS = {
    "станок": (r"\bстан(?:ок|к|оч)",),
    "обрабатывающий центр": (r"\bобрабатыва", r"\bцентр"),
    "чпу": (r"\bчпу\b",),
    "микросхема": (r"\bмикросхем",),
    "полупроводник": (r"\bполупроводник",),
    "микроконтроллер": (r"\bмикроконтроллер",),
    "углеволокно": (r"\bуглеволок",),
    "углеродная ткань": (r"\bуглеродн", r"\bткан"),
    "препрег": (r"\bпрепрег",),
    "эпоксидная смола": (r"\bэпоксидн", r"\bсмол"),
}

C4ADS_PDF_DOMAINS = {"c4ads.org", "carnegieendowment.org", "oceana.org", "conflictarm.com", "greenpeace.org", "securitycouncilreport.org", "thesentry.org"}


def validate_document_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not any(
        parsed.hostname == domain or parsed.hostname.endswith("." + domain) for domain in C4ADS_PDF_DOMAINS):
        raise SourceError("PDF поза перевіреними доменами посилань C4ADS")


def query_match(data, query):
    """Conservative text match; does not establish end use or completed delivery."""
    products = data.get("product_names") or []
    if isinstance(products, str):
        products = [products]
    fields = {"title": [data.get("title") or ""], "product_names": products}
    terms = QUERY_TERMS.get(query.casefold(), (re.escape(query.casefold()),))
    matched_fields = [name for name, values in fields.items()
                      if any(all(re.search(term, value.casefold()) for term in terms) for value in values)]
    return matched_fields


class CollectionArchive:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "collection.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                detail TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS responses (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), path TEXT NOT NULL,
                sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                source TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                json TEXT NOT NULL, PRIMARY KEY(source, kind, id)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                source TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES jobs(id), PRIMARY KEY(source, kind, record_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS source_status (
                source TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL
            );
        ''')
        self.normalize_research_ids()

    def normalize_research_ids(self):
        """Upgrade early snapshots whose identities also depended on card titles."""
        saved = self.db.execute("SELECT id,json FROM records WHERE source='c4ads' AND kind='research'").fetchall()
        if not saved:
            return
        groups = {}
        mappings = {}
        for row in saved:
            record = json.loads(row["json"])
            identifier = record_id({"url": record["source_url"]} if record.get("source_url") else {"title": record["data"]["title"]})
            mappings[row["id"]] = identifier
            score = (bool(record["data"].get("published_date")), len(record["data"].get("assets") or []), len(record["data"].get("summary") or ""))
            if identifier not in groups or score > groups[identifier][0]:
                record["record_id"] = identifier
                groups[identifier] = (score, record)
        if all(old == new for old, new in mappings.items()):
            return
        with self.db:
            self.db.execute("DELETE FROM records WHERE source='c4ads' AND kind='research'")
            for identifier, (_, record) in groups.items():
                self.db.execute("INSERT INTO records VALUES(?,?,?,?)", ("c4ads", "research", identifier, encoded(record)))
            for old, new in mappings.items():
                if old == new:
                    continue
                self.db.execute("INSERT OR IGNORE INTO evidence SELECT source,kind,?,job_id FROM evidence WHERE source='c4ads' AND kind='research' AND record_id=?", (new, old))
                self.db.execute("DELETE FROM evidence WHERE source='c4ads' AND kind='research' AND record_id=?", (old,))

    def enqueue(self, source, kind, payload):
        identifier = record_id({"source": source, "kind": kind, "payload": payload})
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(id,source,kind,payload,updated_at) VALUES(?,?,?,?,?)",
                            (identifier, source, kind, encoded(payload), now()))
        return identifier

    def next_job(self):
        return self.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()

    def raw(self, job, content, extension):
        digest = hashlib.sha256(content).hexdigest()
        path = self.directory / "raw" / job["source"] / (job["id"] + "." + extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO responses VALUES(?,?,?,?)",
                            (job["id"], str(path.resolve()), digest, now()))
        return path, digest

    def commit(self, job, records=(), status="done", detail=None):
        response = self.db.execute("SELECT * FROM responses WHERE job_id=?", (job["id"],)).fetchone()
        with self.db:
            for record in records:
                if response:
                    record.input_file = response["path"]
                    record.input_sha256 = response["sha256"]
                existing = self.db.execute("SELECT json FROM records WHERE source=? AND kind=? AND id=?",
                    (record.source, record.record_type, record.record_id)).fetchone()
                if not (job["kind"] == "catalog" and existing):
                    self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?)",
                                    (record.source, record.record_type, record.record_id, record.model_dump_json()))
                self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)",
                                (record.source, record.record_type, record.record_id, job["id"]))
            self.db.execute("UPDATE jobs SET status=?,detail=?,updated_at=? WHERE id=?",
                            (status, encoded(detail) if detail is not None else None, now(), job["id"]))

    def source_status(self, source, status, detail):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO source_status VALUES(?,?,?)", (source, status, detail))

    def counts(self):
        return {row["source"] + ":" + row["kind"]: row["n"] for row in self.db.execute(
            "SELECT source,kind,COUNT(*) n FROM records GROUP BY source,kind")}

    def export(self, plan):
        self.update_coverage_status()
        destination = self.directory / "exports_staging"
        destination.mkdir(exist_ok=True)
        for source_row in self.db.execute("SELECT DISTINCT source FROM records"):
            source = source_row[0]
            with (destination / (source + ".jsonl")).open("w", encoding="utf-8") as handle:
                for row in self.db.execute("SELECT json FROM records WHERE source=? ORDER BY kind,id", (source,)):
                    handle.write(row[0] + "\n")
        fields = ("contract_number", "title", "sign_date", "law", "customer_inn", "customer_name",
                  "supplier_inns", "supplier_names", "price_rub", "currency_original", "amount_original",
                  "product_codes", "product_names", "notif_num", "purchase_code", "source_url")
        query_evidence = {}
        for row in self.db.execute("SELECT e.record_id,j.payload FROM evidence e JOIN jobs j ON e.job_id=j.id WHERE e.kind='contract' AND j.kind='contracts'"):
            query = json.loads(row["payload"]).get("query")
            if query:
                query_evidence.setdefault(row["record_id"], set()).add(query)
        matched_contracts = set()
        match_fields = ("contract_number", "query", "matched_fields", "status")
        with (destination / "query_matches.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=match_fields); writer.writeheader()
            for row in self.db.execute("SELECT id,json FROM records WHERE kind='contract' ORDER BY id"):
                data = json.loads(row["json"])["data"]
                for query in sorted(query_evidence.get(row["id"], set())):
                    matches = query_match(data, query)
                    if matches:
                        matched_contracts.add(row["id"])
                    writer.writerow({"contract_number": data.get("contract_number"), "query": query,
                        "matched_fields": ";".join(matches), "status": "text_match" if matches else "unverified_search_hit"})
        with (destination / "contracts.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in self.db.execute("SELECT json FROM records WHERE kind='contract' ORDER BY id"):
                record = json.loads(row[0])
                data = {k: record["data"].get(k) for k in fields}
                data["source_url"] = record["source_url"]
                writer.writerow({k: encoded(v) if isinstance(v, (dict, list)) else v for k, v in data.items()})
        with (destination / "matched_contracts.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
            for row in self.db.execute("SELECT id,json FROM records WHERE kind='contract' ORDER BY id"):
                if row["id"] not in matched_contracts:
                    continue
                record = json.loads(row["json"])
                data = {k: record["data"].get(k) for k in fields}; data["source_url"] = record["source_url"]
                writer.writerow({k: encoded(v) if isinstance(v, (dict, list)) else v for k, v in data.items()})
        summary_fields = ("contract_number", "sign_date", "title", "title_truncated", "customer_inn", "customer_name",
                          "supplier_inns", "supplier_names", "price_rub", "law", "text_match", "notif_num", "purchase_code", "source_url")
        for filename, matched_only in (("contracts_summary.csv", False), ("matched_contracts_summary.csv", True)):
            with (destination / filename).open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=summary_fields); writer.writeheader()
                for row in self.db.execute("SELECT id,json FROM records WHERE kind='contract' ORDER BY id"):
                    if matched_only and row["id"] not in matched_contracts:
                        continue
                    record = json.loads(row["json"]); data = record["data"]
                    summary = {key: data.get(key) for key in summary_fields}
                    title = data.get("title") or ""
                    summary.update(title=title[:2000], title_truncated=len(title) > 2000, text_match=row["id"] in matched_contracts, source_url=record["source_url"])
                    writer.writerow({key: encoded(value) if isinstance(value, (dict, list)) else value for key, value in summary.items()})
        companies = {}
        for row in self.db.execute("SELECT id,json FROM records WHERE kind='contract'"):
            record = json.loads(row["json"]); data = record["data"]
            parties = [(data.get("customer_inn"), data.get("customer_name"), "customer")]
            supplier_inns, names = data.get("supplier_inns", []), data.get("supplier_names", [])
            # Без явного зв'язку ІПН/назва не зіставляємо два незалежні масиви за позицією.
            parties.extend((tax_id, names[0] if len(supplier_inns) == len(names) == 1 else None, "supplier")
                           for tax_id in supplier_inns)
            for tax_id, name, role in parties:
                if not tax_id:
                    continue
                company = companies.setdefault(tax_id, {"inn": tax_id, "names": set(), "roles": set(), "contracts": set(), "matched_contracts": set()})
                if name:
                    company["names"].add(name)
                company["roles"].add(role); company["contracts"].add(data["contract_number"])
                if row["id"] in matched_contracts:
                    company["matched_contracts"].add(data["contract_number"])
        with (destination / "companies.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle); writer.writerow(("inn", "names", "roles", "contract_count", "text_match_contract_count"))
            for tax_id, company in sorted(companies.items()):
                writer.writerow((tax_id, encoded(sorted(company["names"])), ";".join(sorted(company["roles"])), len(company["contracts"]), len(company["matched_contracts"])))
        documents = {}
        document_replacements = {}
        for row in self.db.execute("SELECT j.payload,j.detail,r.path FROM jobs j LEFT JOIN responses r ON j.id=r.job_id WHERE j.kind='pdf' AND j.status='done'"):
            payload, detail = json.loads(row["payload"]), json.loads(row["detail"] or "{}")
            documents[payload["url"]] = dict(detail, pdf_path=row["path"])
            if payload.get("replaces"):
                document_replacements[payload["replaces"]] = payload["url"]
        completed_documents = dict(documents)
        for original, replacement in document_replacements.items():
            documents[original] = dict(documents[replacement], resolved_url=replacement)
        pdf_assets = set()
        with (destination / "research.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle); writer.writerow(("title", "published_date", "url", "pdf_urls", "downloaded_documents"))
            for row in self.db.execute("SELECT json FROM records WHERE kind='research' ORDER BY id"):
                record = json.loads(row[0]); data = record["data"]
                assets = data.get("assets", [])
                pdf_assets.update(asset for asset in assets if urlparse(asset).path.lower().endswith(".pdf"))
                writer.writerow((data["title"], data.get("published_date"), record["source_url"], encoded(assets),
                                 encoded([documents[asset] for asset in assets if asset in documents])))
        final_exports = self.directory / "exports"
        final_exports.mkdir(exist_ok=True)
        for generated in destination.iterdir():
            generated.replace(final_exports / generated.name)
        destination.rmdir()
        jobs = [dict(row) for row in self.db.execute("SELECT source,kind,status,COUNT(*) n FROM jobs GROUP BY source,kind,status")]
        sources = [dict(row) for row in self.db.execute("SELECT * FROM source_status ORDER BY source")]
        incomplete = [dict(row) for row in self.db.execute("SELECT source,kind,payload,status,detail FROM jobs WHERE status NOT IN ('done','split','available','superseded')")]
        report = {"updated_at": now(), "scope": plan, "records": self.counts(), "companies": len(companies),
                  "jobs": jobs, "source_status": sources, "unresolved": incomplete,
                  "coverage_note": "Збір за вказаними запитами та періодом. Це не повний реєстр усіх контрактів/митних декларацій. Статус source probe не підтверджує повноту джерела."}
        quality = {"contracts": 0, "missing_customer_inn": 0, "missing_supplier_inn": 0,
                   "missing_price_rub": 0, "missing_sign_date": 0, "earliest_date": None, "latest_date": None}
        for row in self.db.execute("SELECT json FROM records WHERE kind='contract'"):
            data = json.loads(row[0])["data"]; quality["contracts"] += 1
            for name, field in (("missing_customer_inn", "customer_inn"), ("missing_supplier_inn", "supplier_inns"),
                                ("missing_price_rub", "price_rub"), ("missing_sign_date", "sign_date")):
                if data.get(field) is None or data.get(field) == []:
                    quality[name] += 1
            if data.get("sign_date"):
                quality["earliest_date"] = min(quality["earliest_date"] or data["sign_date"], data["sign_date"])
                quality["latest_date"] = max(quality["latest_date"] or data["sign_date"], data["sign_date"])
        report["quality"] = quality
        report["text_matches"] = {"contracts": len(matched_contracts), "unverified_search_hits": quality["contracts"] - len(matched_contracts),
            "method": "Усі словоформи запиту присутні у назві або в одній товарній позиції; правила QUERY_TERMS у collection_cli.py. Це текстова відповідність, не доказ кінцевого використання."}
        report["documents"] = {"discovered_pdf_urls": len(pdf_assets), "downloaded_and_processed": len(completed_documents),
            "with_text": sum(bool(value.get("characters")) for value in completed_documents.values()),
            "replacements": document_replacements,
            "missing_pdf_urls": sorted(pdf_assets - set(documents)),
            "requiring_ocr": sorted(url for url, value in completed_documents.items() if value.get("requires_ocr"))}
        audit_path = self.directory / "audit.json"
        if audit_path.exists():
            audit = json.loads(audit_path.read_text())
            snapshot = {"responses": self.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0],
                        "last_response": self.db.execute("SELECT MAX(fetched_at) FROM responses").fetchone()[0],
                        "last_job_update": self.db.execute("SELECT MAX(updated_at) FROM jobs").fetchone()[0]}
            if audit.get("records") == report["records"] and audit.get("snapshot") == snapshot:
                report["audit"] = audit
        (self.directory / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        pdf_count = len(completed_documents)
        clear_unresolved = self.db.execute("SELECT COUNT(*) FROM jobs WHERE source='clearspending' AND status NOT IN ('done','split')").fetchone()[0]
        text = (f"# Результат збору\n\nОновлено {report['updated_at']} (UTC).\n\n"
                f"Період контрактів: **{plan['start_date']} — {plan['end_date']}**. "
                "Пошук за наведеними ключовими словами; пошуковий збіг сам по собі не підтверджує військове застосування чи фактичне постачання.\n\n"
                f"Запити: {', '.join(plan['queries'])}.\n\n"
                f"Збережено **{quality['contracts']} контрактів**, **{len(companies)} ІПН учасників**, "
                f"**{self.counts().get('c4ads:research', 0)} публікацій C4ADS**, **{pdf_count} оброблених PDF** "
                f"(із текстом: **{report['documents']['with_text']}**).\n\n"
                f"Відомих PDF без успішної обробки: **{len(report['documents']['missing_pdf_urls'])}**; "
                f"потребують OCR: **{len(report['documents']['requiring_ocr'])}**.\n\n"
                f"У назві/товарних позиціях **{len(matched_contracts)} контрактів** знайдено текстову відповідність запитам. "
                f"Решта **{quality['contracts'] - len(matched_contracts)}** — пошукові збіги, які потребують перевірки первинних документів.\n\n"
                f"Незавершених/обмежених завдань Госзатрат: **{clear_unresolved}**.\n\n"
                "## Файли\n\n"
                "- [Компактна вибірка для перегляду CSV](exports/matched_contracts_summary.csv) — текстові збіги, без великих масивів товарних позицій. Назви довші за 2000 символів позначені як скорочені.\n"
                "- [Усі пошукові збіги, компактно CSV](exports/contracts_summary.csv).\n"
                "- [Контракти CSV](exports/contracts.csv) — ціни, дати, ІПН, назви, товарні коди, посилання.\n"
                "- [Контракти з текстовою відповідністю CSV](exports/matched_contracts.csv) — початкова вибірка для аналізу.\n"
                "- [Перевірка пошукових збігів CSV](exports/query_matches.csv) — запит, поля відповідності, статус.\n"
                "- [Компанії CSV](exports/companies.csv) — точні ІПН, відомі назви, ролі та кількість контрактів.\n"
                "- [Публікації та документи CSV](exports/research.csv) — дати, URL, шляхи PDF і витягнутого тексту.\n"
                "- [Контроль охоплення JSON](coverage.json) — черга, помилки, ліміти, джерела.\n\n"
                "## Якість даних\n\n"
                "| Поле | Контрактів без поля |\n|---|---:|\n"
                f"| ІПН замовника | {quality['missing_customer_inn']} |\n"
                f"| ІПН постачальника | {quality['missing_supplier_inn']} |\n"
                f"| Сума RUB | {quality['missing_price_rub']} |\n"
                f"| Дата підписання | {quality['missing_sign_date']} |\n\n"
                "## Інші джерела\n\n| Джерело | Статус | Причина |\n|---|---|---|\n")
        for entry in sources:
            text += f"| {entry['source']} | {entry['status']} | {entry['detail']} |\n"
        text += ("\nПовноту митної історії та фактичний TTS не встановлено: потрібні торговельні вивантаження, залишок запасів і споживання. "
                 "Сирі відповіді та SHA-256 збережені; кожен нормалізований запис має походження. "
                 "Незавершені завдання залишаються у SQLite для продовження.\n")
        if report.get("audit"):
            audit = report["audit"]
            text += (f"\n## Перевірка архіву\n\n[Детальний аудит](audit.json): "
                     f"аномалій пагінації — **{len(audit['pagination_issues'])}**, "
                     f"проблем сирих файлів/SHA-256 — **{len(audit['raw_file_issues'])}**; "
                     f"SQLite: **{audit['sqlite_integrity']}**. Перевірено **{audit['raw_responses_checked']}** відповідей.\n")
        (self.directory / "REPORT.md").write_text(text, encoding="utf-8")
        return report

    def close(self):
        self.db.close()

    def merge_source(self, other, source):
        """Copy evidence and local assets so the destination archive stays portable."""
        jobs = {row["id"]: dict(row) for row in other.db.execute("SELECT * FROM jobs WHERE source=?", (source,))}
        responses = []
        path_map = {}
        for row in other.db.execute("SELECT r.* FROM responses r JOIN jobs j ON r.job_id=j.id WHERE j.source=?", (source,)):
            response = dict(row); original = Path(response["path"])
            if hashlib.sha256(original.read_bytes()).hexdigest() != response["sha256"]:
                raise SourceError("SHA-256 джерела архіву не збігається")
            copied = self.directory / "raw" / source / original.name
            copied.parent.mkdir(parents=True, exist_ok=True)
            if copied.resolve() != original.resolve():
                shutil.copy2(original, copied)
            path_map[str(original.resolve())] = str(copied.resolve())
            response["path"] = str(copied.resolve()); responses.append(response)
            detail = json.loads(jobs[response["job_id"]]["detail"] or "null")
            if isinstance(detail, dict) and detail.get("text_path"):
                text_source = Path(detail["text_path"]); text_copy = copied.with_suffix(".txt")
                if text_source.resolve() != text_copy.resolve():
                    shutil.copy2(text_source, text_copy)
                detail["text_path"] = str(text_copy.resolve())
                jobs[response["job_id"]]["detail"] = encoded(detail)
        with self.db:
            for job in jobs.values():
                self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?)", tuple(job[name] for name in ("id", "source", "kind", "payload", "status", "detail", "updated_at")))
            for response in responses:
                self.db.execute("INSERT OR REPLACE INTO responses VALUES(?,?,?,?)", tuple(response[name] for name in ("job_id", "path", "sha256", "fetched_at")))
            for row in other.db.execute("SELECT * FROM records WHERE source=?", (source,)):
                record = json.loads(row["json"])
                record["input_file"] = path_map.get(record.get("input_file"), record.get("input_file"))
                self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?)", (source, row["kind"], row["id"], encoded(record)))
            for row in other.db.execute("SELECT * FROM evidence WHERE source=?", (source,)):
                self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)", tuple(row))

    def update_coverage_status(self):
        for row in self.db.execute("SELECT id,payload FROM jobs WHERE kind='pdf' AND status='done' AND json_extract(payload,'$.replaces') IS NOT NULL").fetchall():
            payload = json.loads(row["payload"])
            with self.db:
                self.db.execute("UPDATE jobs SET status='superseded',detail=?,updated_at=? WHERE kind='pdf' AND status='failed' AND json_extract(payload,'$.url')=?",
                    (encoded({"recovered_by": row["id"], "replacement_url": payload["url"], "reason": "Працююча копія того ж звіту з офіційного сайту"}), now(), payload["replaces"]))
        recovered = self.db.execute("SELECT id FROM jobs WHERE source='c4ads' AND kind='catalog' AND status='done' AND json_extract(payload,'$.url')='https://c4ads.org/category/reports/page/2/'").fetchone()
        if recovered:
            with self.db:
                self.db.execute("UPDATE jobs SET status='superseded',detail=?,updated_at=? WHERE source='c4ads' AND kind='catalog' AND status='failed' AND json_extract(payload,'$.url')='https://c4ads.org/reports/page/2/'",
                    (encoded({"original_error": "HTTP 404", "recovered_by": recovered["id"], "reason": "Використано робочу пагінацію офіційного каталогу"}), now()))
        for source in ("clearspending", "c4ads"):
            count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE source=?", (source,)).fetchone()[0]
            if not count:
                continue
            incomplete = self.db.execute("SELECT COUNT(*) FROM jobs WHERE source=? AND status NOT IN ('done','split','superseded')", (source,)).fetchone()[0]
            missing_pdfs = 0
            if source == "c4ads":
                assets = set()
                for row in self.db.execute("SELECT json FROM records WHERE source='c4ads'"):
                    assets.update(asset for asset in json.loads(row[0])["data"].get("assets", []) if urlparse(asset).path.lower().endswith(".pdf"))
                processed = set()
                for row in self.db.execute("SELECT payload FROM jobs WHERE source='c4ads' AND kind='pdf' AND status='done'"):
                    payload = json.loads(row[0]); processed.add(payload["url"])
                    if payload.get("replaces"):
                        processed.add(payload["replaces"])
                missing_pdfs = len(assets - processed)
            self.source_status(source, "partial" if incomplete or missing_pdfs else "collected_scope",
                f"Незавершених/обмежених завдань: {incomplete}; відомих PDF без обробки: {missing_pdfs}. Обсяг визначено планом; це не весь реєстр або всі дані організації.")


def contract_children(payload):
    start = date.fromisoformat(payload["start"]); end = date.fromisoformat(payload["end"])
    if start >= end:
        return []
    middle = start + (end - start) // 2
    return [dict(payload, start=start.isoformat(), end=middle.isoformat(), page=1),
            dict(payload, start=(middle + timedelta(days=1)).isoformat(), end=end.isoformat(), page=1)]


class EvidenceClient:
    def __init__(self, client, archive, job):
        self.client, self.archive, self.job = client, archive, job

    async def request(self, *args, **kwargs):
        response = await self.client.request(*args, **kwargs)
        extension = "json" if "json" in response.headers.get("content-type", "") else "html"
        self.archive.raw(self.job, response.content, extension)
        return response


class Collector:
    def __init__(self, archive, plan):
        self.archive, self.plan = archive, plan

    def seed(self):
        if self.plan.get("c4ads"):
            self.archive.enqueue("c4ads", "catalog", {"url": "https://c4ads.org/category/reports/"})
        for query in self.plan["queries"]:
            self.archive.enqueue("clearspending", "contracts", {"query": query, "start": self.plan["start_date"],
                "end": self.plan["end_date"], "page": 1})
        for source, url in self.plan.get("source_probes", {}).items():
            validate_url(source, url)
            self.archive.enqueue(source, "probe", {"url": url})
        for entry in self.plan.get("trade_urls", []):
            validate_url(entry["source"], entry["url"])
            self.archive.enqueue(entry["source"], "trade", entry)
        if not os.getenv("CHECKO_API_KEY"):
            self.archive.source_status("checko", "needs_credentials", "CHECKO_API_KEY не заданий; профілі за ІПН не зібрані")
        for tax_id in self.plan.get("company_inns", []):
            if os.getenv("CHECKO_API_KEY"):
                self.archive.enqueue("checko", "company", {"inn": tax_id})
            self.archive.enqueue("kad", "cases", {"participant": tax_id})
        if not self.plan.get("company_inns"):
            self.archive.source_status("kad", "needs_scope", "Потрібен список учасників/ІПН для збору справ")
        for source in ("panjiva", "importgenius", "52wmb"):
            self.archive.source_status(source, "needs_exports", "Повних торговельних вивантажень і цільових URL не надано")

    async def process(self, client, job):
        payload = json.loads(job["payload"])
        kind, source = job["kind"], job["source"]
        if kind == "contracts":
            await self.contracts(client, job, payload)
            return
        if kind == "company":
            result = await CheckoParser().fetch_company(EvidenceClient(client, self.archive, job), payload["inn"], os.environ["CHECKO_API_KEY"])
            self.archive.commit(job, result.records)
            return
        if kind == "cases":
            result = await KadParser().search(EvidenceClient(client, self.archive, job), payload["participant"])
            self.archive.commit(job, result.records, status="partial", detail="Отримано тільки першу сторінку справ")
            return
        url = payload["url"]
        if source == "c4ads" and kind == "pdf":
            validate_document_url(url)
        else:
            validate_url(source, url)
        extension = "pdf" if kind == "pdf" else "html"
        cached = self.archive.db.execute("SELECT * FROM responses WHERE job_id=?", (job["id"],)).fetchone() if kind == "pdf" else None
        content = Path(cached["path"]).read_bytes() if cached and Path(cached["path"]).exists() else None
        if content and content.startswith(b"%PDF-") and hashlib.sha256(content).hexdigest() == cached["sha256"]:
            path, digest = Path(cached["path"]), cached["sha256"]
        else:
            response = await client.request("GET", url)
            content = response.content
            path, digest = self.archive.raw(job, content, extension)
        if kind == "probe":
            self.archive.commit(job, status="available", detail="Сторінка доступна; записи ще не зібрані")
            return
        if kind == "pdf":
            if not content.lstrip().startswith(b"%PDF-"):
                raise SourceError("Посилання не повернуло PDF")
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted and not reader.decrypt(""):
                raise SourceError("PDF зашифровано; потрібна доступна копія")
            pages = [page.extract_text() or "" for page in reader.pages]
            text_path = path.with_suffix(".txt")
            text_path.write_text("\n\n".join(f"--- page {index + 1} ---\n{value}" for index, value in enumerate(pages)), encoding="utf-8")
            self.archive.commit(job, detail={"pages": len(pages), "characters": sum(map(len, pages)),
                "text_path": str(text_path.resolve()), "requires_ocr": not any(value.strip() for value in pages)})
            return
        result = (C4ADSParser() if source == "c4ads" else PARSERS[source]()).parse(response.text, "html", url)
        if source == "c4ads":
            if kind == "catalog":
                soup = BeautifulSoup(response.text, "html.parser")
                next_page = soup.select_one("a.next.page-numbers")
                if next_page:
                    self.archive.enqueue(source, "catalog", {"url": source_url(next_page["href"], url)})
                for record in result.records:
                    self.archive.enqueue(source, "detail", {"url": record.source_url})
            elif self.plan.get("download_pdfs"):
                if self.plan.get("follow_report_links"):
                    soup = BeautifulSoup(response.text, "html.parser")
                    for link in soup.select("a[href]"):
                        target = source_url(link["href"], url)
                        if not target:
                            continue
                        parsed_url = urlparse(target)
                        if parsed_url.hostname == "c4ads.org" and __import__("re").fullmatch(r"/reports/[^/]+/?", parsed_url.path):
                            canonical = parsed_url._replace(query="", fragment="").geturl()
                            self.archive.enqueue(source, "detail", {"url": canonical})
                for record in result.records:
                    for asset in record.data.get("assets", []):
                        if urlparse(asset).path.lower().endswith(".pdf"):
                            try:
                                validate_document_url(asset)
                            except SourceError:
                                continue
                            self.archive.enqueue(source, "pdf", {"url": asset})
        self.archive.commit(job, result.records, status="done" if result.records else "unparsed", detail=result.warnings)

    async def contracts(self, client, job, payload):
        url = "https://clearspending.ru/api/gw/filtered-contracts/"
        params = {"search": payload["query"], "sign_date_gte": payload["start"],
                  "sign_date_lte": (date.fromisoformat(payload["end"]) + timedelta(days=1)).isoformat(),
                  "sort": "-sign_date", "page_size": 50, "page": payload["page"]}
        response = await client.request("GET", url, params=params)
        self.archive.raw(job, response.content, "json")
        data = response.json()
        if not isinstance(data.get("data"), list) or not isinstance(data.get("count"), int):
            raise SourceError("Непідтримувана відповідь Госзатрат")
        if payload["page"] == 1 and data["count"] >= 500:
            children = contract_children(payload)
            if children:
                for child in children:
                    self.archive.enqueue("clearspending", "contracts", child)
                self.archive.commit(job, status="split", detail={"reported_count": data["count"], "reason": "Ліміт відповіді: поділ за датою"})
                return
        result = ClearSpendingParser().parse(response.text, "json", str(response.url))
        valid = [record for record in result.records if record.data.get("sign_date") and
                 payload["start"] <= record.data["sign_date"] <= payload["end"]]
        invalid_dates = len(result.records) - len(valid)
        last_page = payload["page"] >= data.get("total_pages", 0)
        if not data["data"] and not last_page:
            raise SourceError("Порожня сторінка до кінця пагінації; охоплення не підтверджено")
        if not last_page:
            self.archive.enqueue("clearspending", "contracts", dict(payload, page=payload["page"] + 1))
        capped = data["count"] >= 500 and payload["start"] == payload["end"]
        self.archive.commit(job, valid, status="capped" if capped else "done", detail={
            "reported_count": data["count"], "page": payload["page"], "total_pages": data.get("total_pages"),
            "parsed": len(valid), "outside_date_window": invalid_dates, "rejected_rows": result.rejected_rows})


async def run(plan, archive, max_requests, concurrency=3, export_every=250):
    collector = Collector(archive, plan); collector.seed()
    requests = 0
    async with SourceClient(proxy=os.getenv("OSINT_PROXY") or None, interval=1.0, timeout=18, retries=1) as client:
        while requests < max_requests:
            job = archive.next_job()
            if job is None:
                break
            client.interval = 10.0 if job["source"] == "c4ads" else 1.0
            # Only independent date windows/pages of ClearSpending share a batch.
            # Request starts are still serialized at one per second by SourceClient.
            batch = [job]
            if job["source"] == "clearspending":
                batch = archive.db.execute("SELECT * FROM jobs WHERE status='pending' AND source='clearspending' ORDER BY rowid LIMIT ?",
                    (min(concurrency, max_requests - requests),)).fetchall()
            async def process_one(item):
                try:
                    await collector.process(client, item)
                except (SourceError, ValueError, OSError, PdfReadError, DependencyError) as exc:
                    archive.commit(item, status="failed", detail=f"{type(exc).__name__}: {exc}")
                    if item["kind"] == "probe":
                        archive.source_status(item["source"], "access_failed", str(exc))
                    print(f"{item['source']}/{item['kind']}: {exc}", flush=True)
            previous = requests
            await asyncio.gather(*(process_one(item) for item in batch))
            requests += len(batch)
            if previous // 10 != requests // 10:
                print(encoded({"processed_jobs": requests, "records": archive.counts()}), flush=True)
            if previous // export_every != requests // export_every:
                archive.export(plan)
    return archive.export(plan)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path('config/collection_plan.json'))
    parser.add_argument("--directory", type=Path, default=Path("data/collection_20261008"))
    parser.add_argument("--max-requests", type=int, default=1000, help="Максимум завдань за запуск; прогрес зберігається")
    parser.add_argument("--concurrency", type=int, default=3, help="Незалежних запитів Госзатрат у роботі; початок не частіше разу на секунду")
    parser.add_argument("--export-every", type=int, default=250, help="Оновлення великих CSV через N завдань; SQLite зберігається після кожної сторінки")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    plan = json.loads(args.plan.read_text())
    date.fromisoformat(plan["start_date"]); date.fromisoformat(plan["end_date"])
    if plan["start_date"] > plan["end_date"] or args.max_requests < 1 or not 1 <= args.concurrency <= 5 or args.export_every < 1:
        parser.error("Некоректний період або --max-requests")
    stored_plan = args.directory / "plan.json"
    if stored_plan.exists() and json.loads(stored_plan.read_text()) != plan:
        parser.error("План відрізняється від збереженого: виберіть нову --directory для іншого обсягу")
    archive = CollectionArchive(args.directory)
    stored_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    try:
        if args.retry_failed:
            with archive.db:
                archive.db.execute("UPDATE jobs SET status='pending' WHERE status='failed'")
        report = archive.export(plan) if args.export_only else asyncio.run(run(plan, archive, args.max_requests, args.concurrency, args.export_every))
        print(encoded({"records": report["records"], "unresolved_jobs": len(report["unresolved"]),
                       "coverage": str((args.directory / 'coverage.json').resolve())}), flush=True)
    finally:
        archive.close()


if __name__ == "__main__":
    main()

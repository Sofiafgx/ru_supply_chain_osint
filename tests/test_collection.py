import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx

from collection_cli import CollectionArchive, Collector, contract_children, query_match
from models.source_records import SourceRecord
from parsers.c4ads_parser import C4ADSParser


PLAN = {"start_date": "2026-01-01", "end_date": "2026-01-10", "queries": ["example"], "c4ads": False}


class ArchiveTests(unittest.TestCase):
    def test_text_match_excludes_partial_compound_search_hits(self):
        self.assertEqual(query_match({"title": "Поставка двуокиси углерода"}, "углеродная ткань"), [])
        self.assertEqual(query_match({"title": "Поставка углеродной ткани"}, "углеродная ткань"), ["title"])
        self.assertEqual(query_match({"title": "Комплект", "product_names": ["Фрезерные станки с ЧПУ"]}, "станок"), ["product_names"])
        self.assertEqual(query_match({"title": "Остановка транспорта"}, "станок"), [])
        self.assertEqual(query_match({"product_names": ["Углеродный газ", "Хлопковая ткань"]}, "углеродная ткань"), [])

    def test_restart_deduplication_and_raw_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = CollectionArchive(temp)
            job_id = archive.enqueue("clearspending", "contracts", {"query": "example"})
            job = archive.next_job()
            path, digest = archive.raw(job, b'{"data": []}', "json")
            record = SourceRecord(source="clearspending", record_type="contract", record_id="1", data={"title": "example"})
            archive.commit(job, [record]); archive.close()
            archive = CollectionArchive(temp)
            self.assertEqual(archive.enqueue("clearspending", "contracts", {"query": "example"}), job_id)
            self.assertIsNone(archive.next_job())
            saved = json.loads(archive.db.execute("SELECT json FROM records").fetchone()[0])
            self.assertEqual(saved["input_file"], str(path.resolve()))
            self.assertEqual(saved["input_sha256"], digest)
            self.assertEqual(archive.counts(), {"clearspending:contract": 1})
            archive.close()

    def test_date_partition_covers_every_day_once(self):
        children = contract_children({"start": "2026-01-01", "end": "2026-01-10", "query": "x", "page": 1})
        self.assertEqual(children[0]["end"], "2026-01-05")
        self.assertEqual(children[1]["start"], "2026-01-06")
        self.assertEqual(children[1]["end"], "2026-01-10")
        self.assertEqual(contract_children(dict(children[0], end=children[0]["start"])), [])

    def test_export_does_not_match_supplier_names_by_position(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = CollectionArchive(temp)
            archive.enqueue("clearspending", "contracts", {})
            archive.commit(archive.next_job(), [SourceRecord(source="clearspending", record_type="contract", record_id="1",
                data={"contract_number": "1", "title": "Goods", "customer_inn": "0012345678", "customer_name": "Buyer",
                      "supplier_inns": ["0098765432", "0011111111"], "supplier_names": ["Supplier A", "Supplier B"]})])
            report = archive.export(PLAN)
            self.assertEqual(report["companies"], 3)
            content = (Path(temp) / "exports/companies.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("Supplier A", content)
            self.assertIn("Buyer", content)
            archive.close()

    def test_detail_excludes_related_reports(self):
        html = '''<main><article><h1>Actual Report</h1><div class="publication-date">May 29, 2025</div><p>Main text</p></article>
        <a href="/reports/related/"><h3 class="post-list-item-title">Related</h3></a></main>'''
        result = C4ADSParser().parse(html, url="https://c4ads.org/reports/actual/")
        self.assertEqual([record.data["title"] for record in result.records], ["Actual Report"])
        self.assertEqual(result.records[0].data["published_date"], "2025-05-29")
        self.assertNotIn("Related", result.records[0].data["summary"])

    def test_catalog_does_not_erase_detail_and_uses_stable_url_identity(self):
        detail = C4ADSParser().parse('<article><h1>Expanded title</h1><div class="publication-date">May 29, 2025</div><a href="/one.pdf">Download</a></article>',
            url="https://c4ads.org/reports/actual/").records[0]
        catalog = C4ADSParser().parse('<a href="/reports/actual/"><h3 class="post-list-item-title">Short title</h3></a>',
            url="https://c4ads.org/category/reports/").records[0]
        self.assertEqual(detail.record_id, catalog.record_id)
        with tempfile.TemporaryDirectory() as temp:
            archive = CollectionArchive(temp)
            archive.enqueue("c4ads", "detail", {"url": detail.source_url}); archive.commit(archive.next_job(), [detail])
            archive.enqueue("c4ads", "catalog", {"url": "https://c4ads.org/category/reports/"}); archive.commit(archive.next_job(), [catalog])
            saved = json.loads(archive.db.execute("SELECT json FROM records").fetchone()[0])
            self.assertEqual(saved["data"]["published_date"], "2025-05-29")
            self.assertEqual(saved["data"]["title"], "Expanded title")
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
            archive.close()

    def test_merged_archive_preserves_evidence_without_original_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            source = CollectionArchive(Path(temp) / "source"); target = CollectionArchive(Path(temp) / "target")
            source.enqueue("c4ads", "detail", {"url": "https://c4ads.org/reports/one/"})
            job = source.next_job(); path, digest = source.raw(job, b"<h1>Report</h1>", "html")
            source.commit(job, [SourceRecord(source="c4ads", record_type="research", record_id="one",
                source_url="https://c4ads.org/reports/one/", data={"title": "Report"})])
            target.merge_source(source, "c4ads"); source.close(); shutil.rmtree(Path(temp) / "source")
            record = json.loads(target.db.execute("SELECT json FROM records").fetchone()[0])
            self.assertTrue(Path(record["input_file"]).exists())
            self.assertEqual(hashlib.sha256(Path(record["input_file"]).read_bytes()).hexdigest(), digest)
            self.assertEqual(target.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)
            target.close()


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, payload):
        class Client:
            async def request(self, *args, **kwargs):
                self.params = kwargs.get("params")
                return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://clearspending.ru/api/gw/filtered-contracts/"))
        return Client()

    async def test_saturated_result_is_partitioned_not_claimed_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = CollectionArchive(temp)
            collector = Collector(archive, PLAN); collector.seed()
            job = archive.next_job()
            await collector.process(await self.response({"count": 500, "total_pages": 10, "data": []}), job)
            self.assertEqual(archive.db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0], "split")
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0], 2)
            self.assertEqual(archive.counts(), {})
            archive.close()

    async def test_daily_cap_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = CollectionArchive(temp)
            payload = {"start": "2026-01-01", "end": "2026-01-01", "query": "x", "page": 1}
            archive.enqueue("clearspending", "contracts", payload)
            await Collector(archive, PLAN).process(await self.response({"count": 500, "total_pages": 1,
                "data": [{"regnum": "1", "subject": "Goods", "sign_date": "2026-01-01"}]}), archive.next_job())
            report = archive.export(PLAN)
            self.assertEqual(report["unresolved"][0]["status"], "capped")
            archive.close()

    async def test_outside_dates_are_excluded_and_paging_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = CollectionArchive(temp); collector = Collector(archive, PLAN); collector.seed()
            client = await self.response({"count": 60, "total_pages": 2, "data": [
                {"regnum": "1", "subject": "Goods", "sign_date": "2026-01-02"},
                {"regnum": "2", "subject": "Other", "sign_date": "2026-01-11"}]})
            await collector.process(client, archive.next_job())
            self.assertEqual(client.params["sign_date_lte"], "2026-01-11")
            self.assertEqual(archive.counts(), {"clearspending:contract": 1})
            self.assertEqual(json.loads(archive.next_job()["payload"])["page"], 2)
            archive.close()


if __name__ == "__main__":
    unittest.main()

"""Синтетичні приклади; мережа не потрібна."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from parsers.c4ads_parser import C4ADSParser
from parsers.customs_parser import CustomsDataParser
from parsers.registry_sources import CheckoParser, ClearSpendingParser, KadParser, ZakupkiParser
from parsers.source_common import SourceClient, SourceError, date, hs_code, inn, number
from parsers.trade_sources import ImportGeniusParser, PanjivaParser, Wmb52Parser, estimate_tts, replenishment_intervals
from sources_cli import main, save_result, validate_url


SHIPMENT = {"shipper_name": "Example Supplier", "consignee_name": "Example Buyer",
            "consignee_inn": "0012345678", "hs_code": "8542.31", "goods_description": "Components",
            "customs_value_usd": "1,234.50", "net_weight_kg": "25.5", "shipment_date": "2026-09-01"}


class NormalizationTests(unittest.TestCase):
    def test_numbers(self):
        for raw, expected in (("1\u00a0234,50 руб.", 1234.5), ("$1,234.50", 1234.5),
                              ("1.234,50", 1234.5), ("$7,000", 7000), ("0", 0), ("1e3", 1000), ("100 EUR", 100)):
            with self.subTest(raw=raw):
                self.assertEqual(number(raw), expected)

    def test_invalid_numbers(self):
        for raw in (None, "", "NaN", "inf", "-1", "unknown", "N/A", "1 kg 2 kg"):
            self.assertIsNone(number(raw))

    def test_identifiers_and_dates(self):
        self.assertEqual(inn("0012345678"), "0012345678")
        self.assertIsNone(inn("1234567890.0"))
        self.assertEqual(hs_code("0101.21"), "010121")
        self.assertIsNone(hs_code("854231;854232"))
        self.assertEqual(date("01.09.2026"), "2026-09-01")
        self.assertEqual(date("2026-09-01T15:20:00Z"), "2026-09-01")
        self.assertIsNone(date("31.02.2026"))

    def test_domain_validation(self):
        validate_url("panjiva", "https://es.panjiva.com/example")
        for url in ("https://panjiva.com.evil.test", "http://panjiva.com", "https://panjiva.com/?key=secret"):
            with self.assertRaises(SourceError):
                validate_url("panjiva", url)


class RegistryTests(unittest.TestCase):
    def test_checko_api(self):
        result = CheckoParser().parse(json.dumps({"data": {"ИНН": "0012345678", "НаимСокр": "Example Co",
              "ОГРН": "1000000000001", "ЮрАдрес": {"АдресРФ": "Example"}}, "meta": {"status": "ok"}}), "json")
        self.assertEqual(result.records[0].record_type, "company")
        self.assertEqual(result.records[0].data["inn"], "0012345678")
        self.assertNotIn("price_rub", result.records[0].data)

    def test_checko_html(self):
        result = CheckoParser().parse('<h1>Example</h1><p>ИНН: 0012345678</p><p>ОГРН 1000000000001</p>')
        self.assertEqual(result.records[0].data["inn"], "0012345678")

    def test_checko_no_fallback(self):
        self.assertEqual(CheckoParser().parse("<h1>Example станок</h1>").records, [])

    def test_checko_api_error(self):
        with self.assertRaises(SourceError):
            CheckoParser().parse('{"meta":{"status":"error"},"data":{}}', "json")

    def test_clearspending_legacy(self):
        payload = {"contracts": {"data": [{"regNum": "test-1", "subject": "Example goods",
          "customer": {"inn": "0012345678"}, "suppliers": [{"inn": "0098765432"}],
          "price": "1200,50", "signDate": "2026-09-01"}]}}
        result = ClearSpendingParser().parse(json.dumps(payload), "json")
        self.assertEqual(result.records[0].data["supplier_inns"], ["0098765432"])
        self.assertEqual(result.records[0].data["price_rub"], 1200.5)

    def test_clearspending_current(self):
        result = ClearSpendingParser().parse(json.dumps({"data": [{"regnum": "test-2", "fz": "44",
           "subject": "Example goods", "customer_inn": "0012345678", "supplier_inns": ["0098765432"],
           "amount_rur": 100, "sign_date": "2026-09-01"}]}), "json")
        record = result.records[0]
        self.assertEqual(record.data["price_rub"], 100)
        self.assertEqual(record.data["supplier_inns"], ["0098765432"])
        self.assertEqual(record.source_url, "https://clearspending.ru/contracts/44/test-2")

    def test_zakupki_card(self):
        html = '''<div class="search-registry-entry-block">
        <a href="/epz/contract/contractCard/common-info.html?reestrNumber=123">№ 123</a>
        <div>Объект закупки</div><div>Example goods</div>
        <div>Цена контракта</div><div>1 234,50</div>
        <div>Заключение контракта</div><div>01.09.2026</div></div>'''
        record = ZakupkiParser().parse(html, url="https://zakupki.gov.ru").records[0]
        self.assertEqual(record.record_id, "123")
        self.assertEqual(record.data["price_rub"], 1234.5)
        self.assertIsNone(record.data["customer_inn"])

    def test_zakupki_xml(self):
        xml = '''<export xmlns="urn:sample"><contract><regNum>test-xml</regNum><subject>Goods</subject>
          <customer><INN>0012345678</INN></customer><suppliers><supplier><INN>0098765432</INN></supplier></suppliers>
          <price>100.00</price><signDate>2026-09-01</signDate></contract></export>'''
        record = ZakupkiParser().parse(xml, "xml").records[0]
        self.assertEqual(record.data["supplier_inns"], ["0098765432"])
        self.assertEqual(record.data["customer_inn"], "0012345678")

    def test_xml_invalid(self):
        with self.assertRaises(SourceError):
            ZakupkiParser().parse("<broken", "xml")

    def test_contract_missing_values(self):
        record = ClearSpendingParser().parse('[{"regNum":"1","subject":"Goods"}]', "json").records[0]
        self.assertIsNone(record.data["price_rub"])
        self.assertIsNone(record.data["sign_date"])

    def test_foreign_contract_currency_is_not_rubles(self):
        record = ClearSpendingParser().parse('[{"regNum":"1","subject":"Goods","price":100,"currency":"USD"}]', "json").records[0]
        self.assertIsNone(record.data["price_rub"])

    def test_kad_html(self):
        html = '''<table><tr><td><a href="/Card/example-id">А40-1/2026</a><span class="date">01.09.2026</span></td>
        <td class="court">Example court</td><td class="plaintiff">Example claimant</td>
        <td class="respondent">Example defendant</td></tr></table>'''
        record = KadParser().parse(html, url="https://kad.arbitr.ru").records[0]
        self.assertEqual(record.record_id, "example-id")
        self.assertEqual(record.data["registration_date"], "2026-09-01")
        self.assertEqual(record.source_url, "https://kad.arbitr.ru/Card/example-id")

    def test_kad_json(self):
        result = KadParser().parse('{"Result":{"Items":[{"CaseNumber":"А40-1/2026","CaseId":"id"}]}}', "json")
        self.assertEqual(len(result.records), 1)

    def test_kad_empty(self):
        result = KadParser().parse('<input id="documentsTotalCount" value="0">')
        self.assertEqual(result.records, [])
        self.assertEqual(result.warnings, [])

    def test_antibot(self):
        for parser in (CheckoParser(), ClearSpendingParser(), KadParser(), PanjivaParser(), C4ADSParser()):
            with self.assertRaises(SourceError):
                parser.parse("<html><title>Oh noes!</title>Protected by Anubis</html>", "html")


class TradeTests(unittest.TestCase):
    def test_all_three_providers_json(self):
        for parser in (ImportGeniusParser(), PanjivaParser(), Wmb52Parser()):
            record = parser.parse(json.dumps([SHIPMENT]), "json").records[0]
            self.assertEqual(record.data["consignee_inn"], "0012345678")
            self.assertEqual(record.data["hs_code"], "854231")
            self.assertEqual(record.data["value_usd"], 1234.5)

    def test_csv_bom_and_semicolons(self):
        csv = '\ufeffShipper;Consignee;HS Code;Shipment Date;Net Weight (kg)\nSupplier;Buyer;0101.21;01.09.2026;"1,25"\n'
        record = PanjivaParser().parse(csv).records[0]
        self.assertEqual(record.data["hs_code"], "010121")
        self.assertEqual(record.data["net_weight_kg"], 1.25)

    def test_importgenius_visible_table(self):
        html = '''<table><thead><tr><th>Bill of Lading</th><th>Supplier</th><th>Importer</th>
        <th>Arrival Date</th><th>Gross Weight KGS</th><th>Quantity</th></tr></thead>
        <tr><td>Example-BL</td><td>Supplier</td><td>Buyer</td><td>2026-09-01</td><td>120 Kgs</td><td>5 CTN</td></tr></table>'''
        record = ImportGeniusParser().parse(html, "html").records[0]
        self.assertEqual(record.data["gross_weight_kg"], 120)
        self.assertIsNone(record.data["net_weight_kg"])
        self.assertIsNone(record.data["hs_code"])
        self.assertEqual(record.data["quantity_unit"], "CTN")

    def test_supplier_link_is_not_shipment_provenance(self):
        html = '<table><tr><th>Date</th><th>Shipper</th></tr><tr><td>2026-09-01</td><td><a href="/suppliers/example">Supplier</a></td></tr></table>'
        record = ImportGeniusParser().parse(html, "html", "https://www.importgenius.com/importers/example").records[0]
        self.assertEqual(record.source_url, "https://www.importgenius.com/importers/example")

    def test_panjiva_unwrapped_thead_and_sample(self):
        html = '''<table><thead><th>Date</th><th>Shipper</th><th>Weight</th></thead>
        <tr><td>2026-09-01</td><td>Supplier</td><td>10 KG</td></tr></table>
        <div class="sample_bol"><div class="label">Shipment Date</div><div>2026-09-02</div>
        <div class="label">Shipper</div><div>Supplier</div><div class="label">HS Code</div><div>854231</div></div>'''
        result = PanjivaParser().parse(html, "html")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].data["weight_raw"], "10 KG")

    def test_wmb_chinese_columns(self):
        result = Wmb52Parser().parse('采购商,供应商,海关编码,交易日期\nBuyer,Supplier,854231,2026-09-01\n')
        self.assertEqual(result.records[0].data["consignee"], "Buyer")

    def test_custom_columns(self):
        record = Wmb52Parser(column_map={"shipper": "seller", "shipment_date": "day"}).parse('seller,day\nSupplier,2026-09-01\n').records[0]
        self.assertEqual(record.data["shipper"], "Supplier")

    def test_missing_tax_id_retained(self):
        row = dict(SHIPMENT, consignee_inn=None)
        record = PanjivaParser().parse(json.dumps([row]), "json").records[0]
        self.assertIsNone(record.data["consignee_inn"])
        self.assertTrue(record.warnings)

    def test_missing_date_not_invented(self):
        row = dict(SHIPMENT, shipment_date="bad", shipment_id="Example-BL")
        record = PanjivaParser().parse(json.dumps([row]), "json").records[0]
        self.assertIsNone(record.data["shipment_date"])

    def test_id_keeps_multiple_products_and_removes_duplicates(self):
        row = dict(SHIPMENT, shipment_id="Example-BL")
        rows = [row, row, dict(row, hs_code="854232", goods_description="Different components")]
        result = PanjivaParser().parse(json.dumps(rows), "json")
        self.assertEqual(len(result.records), 2)

    def test_optional_hs_filter(self):
        result = PanjivaParser(hs_prefixes=["8457"]).parse(json.dumps([SHIPMENT]), "json")
        self.assertEqual(result.records, [])

    def test_legacy_trade_edge(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "shipments.json"
            path.write_text(json.dumps([SHIPMENT, dict(SHIPMENT, shipment_date="bad")]))
            parser = CustomsDataParser(path)
            edges = list(parser.parse_records())
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0].weight_kg, 25.5)

    def test_replenishment_and_tts(self):
        rows = [SHIPMENT, dict(SHIPMENT, shipment_date="2026-09-01", shipper_name="Other"),
                dict(SHIPMENT, shipment_date="2026-09-11")]
        records = PanjivaParser().parse(json.dumps(rows), "json").records
        metrics = replenishment_intervals(records, "0012345678")
        self.assertEqual(metrics["mean_interval_days"], 10)
        self.assertIsNone(replenishment_intervals(records, "0098765432")["mean_interval_days"])
        self.assertEqual(estimate_tts(100, 5), 20)
        for stock, demand in ((-1, 5), (100, 0), (float("inf"), 5)):
            with self.assertRaises(ValueError):
                estimate_tts(stock, demand)


class ResearchTests(unittest.TestCase):
    def test_c4ads_archive(self):
        html = '<a href="https://c4ads.org/reports/example/"><h3 class="post-list-item-title">Example</h3><div class="post-list-excerpt">Description</div></a>'
        record = C4ADSParser().parse(html).records[0]
        self.assertEqual(record.record_type, "research")
        self.assertIsNone(record.data["published_date"])

    def test_c4ads_detail(self):
        html = '<meta property="article:published_time" content="2026-09-01T12:00:00Z"><main><h1>Example</h1><p>Text</p><a href="/example.csv">Data</a></main>'
        record = C4ADSParser().parse(html, url="https://c4ads.org/reports/example/").records[0]
        self.assertEqual(record.data["assets"], ["https://c4ads.org/example.csv"])
        self.assertEqual(record.data["published_date"], "2026-09-01")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_rotation_on_restricted_access(self):
        for status in (401, 403, 429):
            calls = []
            async with SourceClient(interval=0) as client:
                await client.client.aclose()
                client.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(status)))
                with self.assertRaises(SourceError):
                    await client.request("GET", "https://example.test")
            self.assertEqual(len(calls), 1)

    async def test_retry_temporary_failure(self):
        calls = []
        async with SourceClient(interval=0) as client:
            await client.client.aclose()
            client.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(503 if len(calls) == 1 else 200)))
            with patch("parsers.source_common.asyncio.sleep", return_value=None):
                result = await client.request("GET", "https://example.test")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 2)

    async def test_network_error_redacts_api_key(self):
        def fail(request):
            raise httpx.ConnectError("secret in URL", request=request)
        async with SourceClient(interval=0, retries=0) as client:
            await client.client.aclose()
            client.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
            with self.assertRaises(SourceError) as context:
                await client.request("GET", "https://example.test?key=secret")
        self.assertNotIn("secret", str(context.exception))


class CLITests(unittest.TestCase):
    def test_offline_jsonl_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.json"
            output = Path(temp) / "output.jsonl"
            source.write_text(json.dumps([SHIPMENT]))
            self.assertEqual(main(["panjiva", "--input", str(source), "--output", str(output)]), 0)
            row = json.loads(output.read_text())
            self.assertEqual(row["source"], "panjiva")
            self.assertIn("collected_at", row)
            self.assertEqual(row["input_file"], str(source.resolve()))
            self.assertEqual(len(row["input_sha256"]), 64)
            self.assertEqual(json.loads(output.with_suffix(".jsonl.report.json").read_text())["records"], 1)

    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "bad.json"
            output = Path(temp) / "output.jsonl"
            source.write_text("invalid json")
            output.write_text("previous data")
            self.assertEqual(main(["panjiva", "--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_text(), "previous data")

    def test_input_output_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.json"
            path.write_text(json.dumps([SHIPMENT]))
            self.assertEqual(main(["panjiva", "--input", str(path), "--output", str(path)]), 1)
            self.assertTrue(path.read_text().startswith("["))


if __name__ == "__main__":
    unittest.main()

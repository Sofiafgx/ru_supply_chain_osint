"""Controlled fixtures only; test inputs are never exported as collected evidence."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from osint_pipeline.adapters import parse
from osint_pipeline.pipeline import Sink, run, validate
from osint_pipeline.schema import FIELDS, normalize
from osint_pipeline.transport import Archive, CollectionError, Deferred, Transport, public_url, retry_after


def job(block, source, **kwargs):
    return dict(id='test', block=block, source=source, **kwargs)


def records(block, source, payload, **kwargs):
    return list(parse(json.dumps(payload), 'json', job(block, source, **kwargs), 'https://example.org/source'))


class ParsingTests(unittest.TestCase):
    def test_all_fields_and_nulls(self):
        for block in FIELDS:
            row = normalize(block, {})
            self.assertTrue(all(k in row and row[k] is None for k in FIELDS[block]))

    def test_zero_and_invalid(self):
        row = normalize(4, dict(hs_code='8542', units_qty=0, net_weight_kg=float('nan'), declaration_date='2024', importer_inn='123'))
        self.assertEqual(row['units_qty'], 0)
        self.assertIsNone(row['net_weight_kg'])
        self.assertIsNone(row['declaration_date'])
        self.assertIsNone(row['importer_inn'])
        self.assertIsNone(normalize(6, {'facility_coords': [91, 0]})['facility_coords'])

    def test_supplier_pairs_not_zipped(self):
        rows = records(1, 'clearspending', {'regNum': 'C1', 'supplier_inns': ['1234567890', '123456789012'], 'supplier_names': ['A', 'B']})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['supplier_name'] is None for row in rows))
        paired = records(1, 'clearspending', {'regNum': 'C1', 'suppliers': [{'inn': '1234567890', 'name': 'A'}]})[0]
        self.assertEqual(paired['supplier_name'], 'A')

    def test_unknown_currency_is_not_rubles(self):
        self.assertIsNone(records(1, 'clearspending', {'regNum': 'C1', 'price': 25, 'currency': 'USD'})[0]['price_rub'])
        self.assertEqual(records(1, 'clearspending', {'regNum': 'C1', 'price': 25, 'currency': 'RUB'})[0]['price_rub'], 25)

    def test_court_roles_are_explicit(self):
        row = records(1, 'kad', {'CaseNumber': 'A1', 'Plaintiffs': [{'Name': 'P', 'Inn': '1234567890'}], 'Respondents': [{'Name': 'R'}]})[0]
        self.assertEqual(row['customer_inn'], '1234567890')
        self.assertIsNone(row['supplier_inn'])
        self.assertEqual(row['record_kind'], 'court')

    def test_contract_xml_and_external_entity_disabled(self):
        xml = '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><root><contract><regNum>C1</regNum><subject>&secret;</subject><customer><INN>1234567890</INN><fullName>A</fullName></customer></contract></root>'
        row = list(parse(xml, 'xml', job(1, 'zakupki'), None))[0]
        self.assertEqual(row['customer_name'], 'A')
        self.assertIsNone(row['subject_text'])
        self.assertIsNone(row['supplier_inn'])

    def test_hh_exact_sentence_and_published_id(self):
        payload = {'id': '42', 'name': 'Оператор', 'description': '<p>Работа на Haas VF-2.</p><p>Без опыта.</p>', 'employer': {'name': 'A'}, 'published_at': '2025-01-02T10:00:00+03:00'}
        row = records(2, 'hh', payload)[0]
        self.assertEqual(row['text_fragment'], 'Работа на Haas VF-2.')
        self.assertEqual(row['record_id'], '42')
        self.assertEqual(row['cnc_model'], 'VF-2')
        self.assertIsNone(row['company_inn'])

    def test_unknown_cnc_model_remains_null(self):
        row = records(2, 'hh', {'name': 'Работа на Haas неизвестной модели', 'description': ''})[0]
        self.assertIsNone(row['cnc_model'])
        self.assertIsNone(row['record_id'])

    def test_arshin_owner_is_not_verifier(self):
        payload = {'result': {'vriInfo': {'miOwner': 'Owner', 'organization': {'inn': '1234567890'}, 'vrfDate': '2024-01-02', 'applicable': {}}, 'miInfo': {'singleMI': {'mitypeTitle': 'Instrument', 'manufactureNum': 'ABC'}}}}
        row = records(3, 'arshin', payload)[0]
        self.assertEqual(row['facility_name'], 'Owner')
        self.assertIsNone(row['facility_inn'])
        self.assertEqual(row['verification_result'], 'придатний')

    def test_comtrade_never_fabricates_shipments(self):
        row = records(4, 'comtrade', {'data': [{'cmdCode': '8542', 'period': 202501, 'primaryValue': 500, 'netWgt': 25, 'qty': 8}]})[0]
        for field in FIELDS[4]:
            if field != 'hs_code':
                self.assertIsNone(row[field], field)
        self.assertEqual(row['aggregate_value_usd'], 500)
        self.assertEqual(row['record_kind'], 'trade_aggregate')

    def test_quantity_requires_piece_unit(self):
        row = records(4, 'panjiva', {'shipment_id': 'B1', 'quantity': 20, 'quantity_unit': 'kg', 'hs_code': '8542'})[0]
        self.assertIsNone(row['units_qty'])
        row = records(4, 'panjiva', {'shipment_id': 'B1', 'quantity': 20, 'quantity_unit': 'pcs'})[0]
        self.assertEqual(row['units_qty'], 20)

    def test_patent_counts_do_not_imply_component_count(self):
        snippet = 'Устройство содержит 4 канала.'
        row = records(5, 'google_patents', {'patent_number': 'RU123456C1', 'claims': [snippet]})[0]
        self.assertEqual(row['raw_snippet_quote'], snippet)
        self.assertEqual(row['detected_architecture_channels'], 4)
        self.assertIsNone(row['calculated_component_multiplier'])

    def test_patent_component_quantity_requires_explicit_fact(self):
        base = {'patent_number': 'RU123456C1'}
        row = records(5, 'google_patents', dict(base, claims=['Устройство содержит 4 канала. Каждый канал содержит АЦП.']))[0]
        self.assertIsNone(row['calculated_component_multiplier'])
        row = records(5, 'google_patents', dict(base, claims=['Устройство содержит 4 канала. Каждый канал содержит 2 АЦП.']))[0]
        self.assertEqual(row['calculated_component_multiplier'], 2)

    def test_patent_xml_keeps_whitespace_in_quote(self):
        xml = '<patent><publication-reference><document-id><country>RU</country><doc-number>123456</doc-number><kind>C1</kind></document-id></publication-reference><claims><claim>Устройство  содержит 4 канала.</claim></claims></patent>'
        row = list(parse(xml, 'xml', job(5, 'fips'), None))[0]
        self.assertEqual(row['raw_snippet_quote'], 'Устройство  содержит 4 канала.')

    def test_trudvsem_keeps_source_id(self):
        payload = {'status': '200', 'results': {'vacancies': [{'vacancy': {'id': '42', 'job-name': 'Оператор Haas VF-2', 'company': {'inn': '1234567890'}}}]}}
        row = records(2, 'trudvsem', payload)[0]
        self.assertEqual(row['record_id'], '42')
        self.assertEqual(row['company_inn'], '1234567890')

    def test_rostender_filters_equipment_purchase(self):
        payload = {'title': 'Поставка станка Haas VF-2', 'description': 'Техническое обслуживание включено'}
        self.assertEqual(records(2, 'rostender', payload), [])

    def test_thermal_detection_never_proves_damage(self):
        payload = {'latitude': 50, 'longitude': 30, 'acq_date': '2025-01-02', 'acq_time': '30', 'confidence': 'h'}
        row = records(6, 'firms', payload)[0]
        self.assertEqual(row['t_event'], '2025-01-02T00:30:00Z')
        self.assertIsNone(row['damage_type'])
        self.assertIsNone(row['t_recovery'])
        self.assertIsNone(row['scene_id'])

    def test_missing_acquisition_time_never_becomes_midnight(self):
        row = records(6, 'firms', {'latitude': 50, 'longitude': 30, 'acq_date': '2025-01-02'})[0]
        self.assertIsNone(row['t_event'])

    def test_catalog_is_not_an_event(self):
        row = records(6, 'copernicus', {'value': [{'Id': 'ABC', 'ContentDate': {'Start': '2025-01-02T00:00:00Z'}}]})[0]
        self.assertIsNone(row['t_event'])
        self.assertEqual(row['observation_time'], '2025-01-02T00:00:00Z')

    def test_year_does_not_become_january_first(self):
        row = normalize(7, records(7, 'elibrary', {'record_id': '42', 'record_date': '2024', 'raw_quote': ' Точна  цитата. '})[0])
        self.assertIsNone(row['record_date'])
        self.assertEqual(row['raw_quote'], ' Точна  цитата. ')

    def test_selectors_are_record_local(self):
        html = '<footer>ИНН 9999999999</footer><article><b>A1</b><p> Точна  цитата. </p></article>'
        rows = list(parse(html, 'html', job(7, 'proverki', record_selector='article', selectors={'record_id': 'b', 'raw_quote': 'p'}), None))
        self.assertIsNone(rows[0]['company_inn'])
        self.assertEqual(rows[0]['raw_quote'], ' Точна  цитата. ')

    def test_bot_challenge_is_error(self):
        with self.assertRaises(ValueError):
            list(parse('<title>Access denied</title>', 'html', job(7, 'proverki'), None))

    def test_manifest_validation(self):
        with self.assertRaises(ValueError):
            validate({'jobs': [job(7, 'proverki', mode='file', path='a', collected_at='2024-01-01')]})
        with self.assertRaises(ValueError):
            validate({'jobs': [job(7, 'proverki', mode='file', path='a', column_map={'made_up_field': 'x'})]})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_rotation_archive_and_read_timeout(self):
        seen, options, sleeps = [], [], []
        statuses = iter([429, 499, 503, 200])
        async def sleep(delay):
            sleeps.append(delay)
        def factory(**kwargs):
            options.append(kwargs)
            proxy = kwargs['proxy']
            def handler(request):
                seen.append(proxy)
                return httpx.Response(next(statuses), headers={'Retry-After': '0'}, content=b'{"data": []}')
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        with tempfile.TemporaryDirectory() as temp:
            async with Transport(Archive(temp), proxies=['http://a:80', 'http://b:80'], attempts=4, interval=0, factory=factory, sleep=sleep) as client:
                response = await client.request('GET', 'https://example.org')
            self.assertEqual(seen, ['http://a:80', 'http://b:80', 'http://a:80', 'http://b:80'])
            self.assertEqual(options[0]['timeout'].read, 50)
            self.assertTrue(options[0]['verify'])
            audit = [json.loads(line) for line in (Path(temp) / 'responses.ndjson').read_text().splitlines()]
            self.assertEqual(len(audit), 4)
            self.assertEqual(response.extensions['evidence']['input_sha256'], hashlib.sha256(response.content).hexdigest())
            self.assertEqual(Path(audit[-1]['raw_file']).read_bytes(), response.content)

    async def test_long_retry_after_is_deferred(self):
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={'Retry-After': '3600'})))
        with tempfile.TemporaryDirectory() as temp:
            async with Transport(Archive(temp), factory=factory, interval=0) as client:
                with self.assertRaises(Deferred):
                    await client.request('GET', 'https://example.org')

    async def test_403_not_retried(self):
        calls = []
        def factory(**kwargs):
            def handler(request):
                calls.append(request)
                return httpx.Response(403)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as temp:
            async with Transport(Archive(temp), factory=factory, interval=0) as client:
                with self.assertRaises(CollectionError):
                    await client.request('GET', 'https://example.org')
            self.assertEqual(len(calls), 1)

    def test_secret_url_redaction(self):
        value = public_url('https://example.org/path/SECRET?key=SECRET', ['SECRET'])
        self.assertNotIn('SECRET', value)
        self.assertEqual(retry_after('-1'), 0)
        self.assertGreater(retry_after('Sat, 10 Oct 2099 12:00:00 GMT'), 60)

    async def test_local_run_preserves_historical_unknowns_and_streams_all_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.json').write_text(json.dumps([{'record_id': 'A', 'raw_quote': 'Quote', 'record_date': '2024'}]))
            (root / 'manifest.json').write_text(json.dumps({'jobs': [job(7, 'elibrary', mode='file', path='input.json')]}))
            report = await run(root / 'manifest.json', root / 'out')
            self.assertEqual(report['jobs'][0]['records'], 1)
            row = json.loads((root / 'out/block_7.ndjson').read_text())
            self.assertIsNone(row['collected_at'])
            self.assertIsNone(row['record_date'])
            self.assertIsNone(row['source_url'])
            for block in FIELDS:
                self.assertTrue((root / f'out/block_{block}.ndjson').exists())
            self.assertEqual(row['input_sha256'], hashlib.sha256((root / 'input.json').read_bytes()).hexdigest())

    async def test_intervals_exclude_unidentified_and_aggregate_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Archive(Path(temp) / 'raw')
            evidence = archive.save(b'original', 'https://example.org', None)
            sink = Sink(temp)
            try:
                for day in ('2025-01-01', '2025-01-04', '2025-01-04'):
                    sink.write(4, dict(importer_inn='1234567890', hs_code='8542', declaration_date=day, record_kind='shipment'), evidence, job(4, 'panjiva'))
                sink.write(4, dict(hs_code='8542', declaration_date='2025-01-02', record_kind='shipment'), evidence, job(4, 'panjiva'))
                sink.write(4, dict(importer_inn='1234567890', hs_code='8542', declaration_date='2025-01-03', record_kind='trade_aggregate'), evidence, job(4, 'comtrade'))
                sink.intervals()
                rows = [json.loads(line) for line in (Path(temp) / 'shipment_intervals.ndjson').read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['interval_days'], 3)
                self.assertEqual(len(rows[0]['evidence']), 2)
            finally:
                sink.close()

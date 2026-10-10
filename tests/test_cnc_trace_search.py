import json
import tempfile
import unittest
from pathlib import Path

import httpx
from cnc_trace_search import Collector, extract, normalize, screening


class CNCTraceTests(unittest.TestCase):
    def test_all_brands_models_and_sentence(self):
        text = ('Наладка DMG Mori DMU 50; Haas VF-3; Fanuc 0i-TF; Sodick AQ600L; '
                'Heidenhain TNC 640; Siemens 840D; Doosan Puma 2600SY; '
                'Mazak INTEGREX i-200; Hermle C 42 U; Studer S31.')
        hits = extract(text)
        self.assertEqual(len(hits), 10)
        self.assertTrue(all(h['cnc_model'] for h in hits))
        self.assertEqual(hits[1]['text_fragment'], 'Haas VF-3;')
        self.assertEqual(extract('Опыт Haas приветствуется.')[0]['cnc_model'], None)
        self.assertEqual(extract('HaasAutomation'), [])

    def test_generic_keyword(self):
        self.assertIsNone(extract('5-осевой фрезерный обрабатывающий центр.')[0]['cnc_brand'])

    def test_attribution_role_service_and_dates(self):
        row = {'company_tax_id': '2128700223', 'title': 'Оператор ЧПУ',
               'description': 'Работа на Haas VF-3.', 'posting_date': '2026-10-08',
               'source_url': 'https://hh.ru/vacancy/123'}
        result, _ = normalize(row, 'hh', ['2128700223'], [], 'now')
        self.assertEqual(result['detected_cnc_list'][0]['record_id'], '123')
        self.assertEqual(result['detected_cnc_list'][0]['record_date'], '2026-10-08')
        row['company_tax_id'] = None
        self.assertIsNone(normalize(row, 'hh', ['2128700223'], [], 'now')[0])
        row['company_tax_id'] = '2128700223'
        row['title'] = 'Менеджер'
        self.assertIsNone(normalize(row, 'hh', ['2128700223'], [], 'now')[0])
        row.update(title='Поставка запчастей для ремонта станка Haas VF-3', source_url='https://rostender.info/12345678-test')
        self.assertIsNone(normalize(row, 'rostender', ['2128700223'], [], 'now')[0])
        row['title'] = 'Техническое обслуживание станка Haas VF-3'
        self.assertIsNotNone(normalize(row, 'rostender', ['2128700223'], [], 'now')[0])

    def test_screening_needs_rule_and_model_match(self):
        hit = extract('Haas VF-3')[0]
        self.assertIsNone(screening(hit, [])['is_sanctioned_equipment'])
        rule = {'brand': 'Haas', 'model_regex': '^VF-', 'basis_url': 'https://example.org/rule'}
        self.assertTrue(screening(hit, [rule])['is_sanctioned_equipment'])
        self.assertIsNone(screening(extract('Haas ST-20')[0], [rule])['is_sanctioned_equipment'])

    def test_trudvsem_pagination_and_attribution(self):
        offsets = []
        def handler(request):
            offset = int(request.url.params['offset'])
            offsets.append(offset)
            items = [{'vacancy': {'id': str(i), 'job-name': 'Оператор ЧПУ', 'duty': 'Haas VF-3',
                       'vac_url': f'https://trudvsem.ru/vacancy/{i}'}} for i in range(100)] if offset == 0 else []
            return httpx.Response(200, json={'status': '200', 'results': {'vacancies': items}})
        with tempfile.TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            rows = Collector(client, Path(folder), 0, 3).trudvsem('2128700223')
        self.assertEqual(offsets, [0, 100])
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]['company_tax_id'], '2128700223')

    def test_rostender_ignores_footer_inn_and_brand(self):
        page = '<h1>Ремонт станка Haas VF-3 с ЧПУ</h1><footer>ИНН 2128700223 Siemens 840D</footer>'
        def handler(request):
            return httpx.Response(200, text=page)
        with tempfile.TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            c = Collector(client, Path(folder), 0, 1)
            self.assertEqual(c.rostender('2128700223', ['https://rostender.info/12345678-test']), [])
            self.assertEqual(c.report[0]['status'], 'customer_inn_not_verified')


if __name__ == '__main__':
    unittest.main()

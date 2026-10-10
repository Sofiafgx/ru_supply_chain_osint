import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import httpx
from cnc_signatures import Collector, classify, extract_signatures, parse_card, records_from_notice, validate_rules, visible_text


class SignatureTests(unittest.TestCase):
    def rule(self, **changes):
        return dict({'rule_id': 'test-only', 'brand': 'Haas', 'model_regex': 'VF-3',
            'jurisdiction': 'US', 'checked_at': '2026-10-10', 'basis_url': 'https://example.org/test-only',
            'is_sanctioned_equipment': True}, **changes)

    def test_exact_sentence_all_mentions(self):
        text = 'Опыт: Haas VF-3; Siemens 840D. Работа на обрабатывающем центре.'
        hits = extract_signatures(text)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0]['text_fragment'], 'Опыт: Haas VF-3; Siemens 840D.')
        self.assertEqual(hits[1]['cnc_model'], '840D')
        self.assertEqual(hits[2]['equipment_categories'], ['machining_center'])

    def test_html_preserves_internal_spaces(self):
        text = visible_text('<p>Работа на Haas <b>VF-3</b>;  наладка ЧПУ.</p>')
        self.assertEqual(text, 'Работа на Haas VF-3;  наладка ЧПУ.')

    def test_all_brands(self):
        hits = extract_signatures('DMG Mori DMU 50; Haas VF-3; Fanuc 0i-TF; Sodick AQ600L; Heidenhain TNC 640; Siemens 840D; Doosan Puma 2600; Mazak INTEGREX i-200; Hermle C 42 U; Studer S31.')
        self.assertEqual(len(hits), 10)
        self.assertTrue(all(h['cnc_model'] for h in hits))

    def test_unknown_ambiguous_conflicting_rules(self):
        hit = extract_signatures('Haas VF-3')[0]
        self.assertEqual(classify(hit, [self.rule()])[0], True)
        for rules in ([], [self.rule(ambiguous=True)], [self.rule(), self.rule(rule_id='other')],
                      [self.rule(model_regex='VF-30')], [self.rule(is_sanctioned_equipment=False)]):
            self.assertIsNone(classify(hit, rules)[0])
        self.assertIsNone(classify(extract_signatures('Haas')[0], [self.rule(model_regex='.*')])[0])
        validate_rules([self.rule()])
        with self.assertRaises(ValueError):
            validate_rules([self.rule(), self.rule()])

    def test_flat_schema_and_offline_cli(self):
        notice = {'company_inn': '2128700223', 'source_site': 'joblab', 'title': 'Инженер ЧПУ',
            'description': 'Haas VF-3; Siemens 840D.', 'source_url': 'https://joblab.ru/vacancy/123', 'record_date': '2026-10-10'}
        records, reason = records_from_notice(notice, 'joblab', ['2128700223'], [])
        self.assertIsNone(reason)
        self.assertEqual(len(records), 2)
        required = {'company_inn','source_site','record_id','record_date','text_fragment','cnc_brand','cnc_model','source_url','is_sanctioned_equipment'}
        self.assertTrue(required <= set(records[0]))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p/'input.json').write_text(json.dumps([notice]))
            subprocess.run(['.venv/bin/python','src/cnc_signatures.py','--offline',str(p/'input.json'),'--output',str(p/'out.json')],check=True,capture_output=True)
            self.assertEqual(len(json.loads((p/'out.json').read_text())), 2)

    def test_card_preserves_sentence_excludes_recommendations(self):
        html = '<h1>Оператор ЧПУ</h1><p class="vacancy-view__subtitle">Завод ИНН 2128700223</p><div class="vacancy-view__body">Работа на Haas <b>VF-3</b>;  наладка.</div><aside>Studer S31</aside>'
        def handler(request):
            return httpx.Response(200, text=html)
        with tempfile.TemporaryDirectory() as d, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            collector = Collector(client, Path(d), 0, 1)
            rows = collector.public_cards('cznelb', '2128700223', ['https://cznelb.ru/vacancy/123'])
        records, _ = records_from_notice(rows[0], 'cznelb', ['2128700223'], [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['text_fragment'], 'Работа на Haas VF-3;  наладка.')

    def test_portal_inn_rejected(self):
        html = '<h1>Оператор ЧПУ</h1><div class="vacancy-view__body">Haas VF-3</div><footer>ИНН 2128700223</footer>'
        with tempfile.TemporaryDirectory() as d, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,text=html))) as client:
            self.assertEqual(Collector(client, Path(d), 0, 1).public_cards('cznelb','2128700223',['https://cznelb.ru/vacancy/123']), [])


if __name__ == '__main__':
    unittest.main()

import csv
import json
from pathlib import Path
import tempfile
import unittest

from local_contract_evidence import filter_csv, parse_kad_html


class LocalEvidenceTests(unittest.TestCase):
    def test_or_filter_anonymous_and_invalid_counterparty(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root/'contracts.csv'
            base = dict(contract_number='001', customer_inn='7719830028', customer_name='',
                        supplier_inns='["6444009038"]', supplier_names='["Молот"]',
                        price_rub='100.10', title='Інструмент', sign_date='2022-03-03', product_names='[]')
            rows = [base, dict(base, contract_number='002', supplier_inns='[]'),
                    dict(base, contract_number='003', supplier_inns='["1234567890"]'),
                    dict(base, contract_number='004', customer_inn='6444009038', supplier_inns='["7719830028"]')]
            with source.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(base));writer.writeheader();writer.writerows(rows)
            report = filter_csv(source, root/'out', {'7719830028'})
            self.assertEqual(report['target_matches'], 4)
            self.assertEqual(report['valid_contract_edges'], 2)
            output = [json.loads(s) for s in (root/'out/contracts.ndjson').read_text().splitlines()]
            self.assertEqual([r['contract_id'] for r in output], ['001', '004'])
            self.assertIsNone(output[0]['customer_name'])
            self.assertEqual(set(output[0]), {'customer_inn','customer_name','supplier_inn','supplier_name','contract_id','sign_date','price_rub','product_description','input_sha256','collected_at','source_url'})

    def test_do_not_pair_multiple_parallel_supplier_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root/'contracts.csv'
            row = dict(customer_inn='1646019864', supplier_inns='["7719830028","6444009038"]',
                       supplier_names='["One","Two"]')
            with source.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row));writer.writeheader();writer.writerow(row)
            filter_csv(source, root/'out', {'1646019864'})
            self.assertTrue(all(json.loads(s)['supplier_name'] is None for s in (root/'out/contracts.ndjson').read_text().splitlines()))

    def test_court_roles_required_and_empty_search_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'case.html'
            html = ('<link rel="canonical" href="https://kad.arbitr.ru/Card/0dd8b95f-1a5c-4864-8e05-b4ece1195aac">'
                    '<div id="case-participants"><div data-role="plaintiff">7719830028</div>'
                    '<div data-role="respondent">6444009038</div></div>'
                    '<span class="case-number">А40-1/2026</span>')
            path.write_text(html)
            cases, audit = parse_kad_html(path, {('6444009038','7719830028')})
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].plaintiff_inn, '7719830028')
            path.write_text(html.replace('data-role="respondent"', 'data-role="plaintiff"'))
            self.assertEqual(parse_kad_html(path, {('6444009038','7719830028')})[0], [])
            path.write_text('<h2>Нет результатов</h2><p>По вашему запросу дел не найдено</p>')
            self.assertEqual(parse_kad_html(path, set())[1]['status'], 'saved_empty_search_not_case')


if __name__ == '__main__':
    unittest.main()

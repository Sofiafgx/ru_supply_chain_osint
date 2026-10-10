import hashlib
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kad_documents import discover_pdfs, extract_pdf, get, official_url, text_evidence
from parsers.registry_sources import ClearSpendingParser
from supplier_enrichment import audit, name_candidates, name_key, valid_inn
from supplier_text_extraction import mentions, text_values


class SupplierEvidenceTests(unittest.TestCase):
    def test_inn_checksum_and_leading_zero(self):
        self.assertTrue(valid_inn('0323410847'))
        self.assertFalse(valid_inn('0323410848'))
        self.assertFalse(valid_inn('323410847'))

    def test_ambiguous_name_is_not_resolved(self):
        index = {name_key('ООО «Пример»'): {'0323410847': [], '1650166051': []}}
        result = name_candidates(['ООО "ПРИМЕР"'], index)[0]
        self.assertEqual(result['status'], 'ambiguous_name')
        self.assertEqual(len(result['candidate_inns']), 2)
        self.assertNotEqual(name_key('АО Пример'), name_key('ООО Пример'))

    def test_scalar_supplier_name_and_uninterpreted_stage_retained(self):
        row = {'regnum': 'example', 'subject': 'Goods', 'supplier_name': 'ООО Пример',
               'current_stage': 'unknown-source-value'}
        data = ClearSpendingParser().parse(json.dumps({'data': [row]}), 'json').records[0].data
        self.assertEqual(data['supplier_names'], ['ООО Пример'])
        self.assertEqual(data['source_execution_stage'], 'unknown-source-value')
        self.assertNotIn('execution_status', data)

    def test_raw_recovery_and_name_match_never_assign_verified_inn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw.json'
            content = json.dumps({'data': [{'regnum': 'a', 'supplier_name': 'ООО Пример'},
                                          {'regnum': 'b', 'supplier_inns': ['0323410847']},
                                          {'regnum': 'c'}]}).encode()
            raw.write_bytes(content)
            db_path = root / 'archive.sqlite'
            with sqlite3.connect(db_path) as db:
                db.executescript('CREATE TABLE records(kind TEXT,json TEXT); CREATE TABLE jobs(id TEXT,source TEXT); CREATE TABLE responses(job_id TEXT,path TEXT,sha256 TEXT);')
                db.execute('INSERT INTO jobs VALUES (?,?)', ('job', 'clearspending'))
                db.execute('INSERT INTO responses VALUES (?,?,?)', ('job', str(raw), hashlib.sha256(content).hexdigest()))
                for identifier in ('a', 'b', 'c'):
                    record = {'record_id': identifier, 'data': {'supplier_inns': [], 'supplier_names': []}}
                    db.execute('INSERT INTO records VALUES (?,?)', ('contract', json.dumps(record)))
            directory_csv = root / 'directory.csv'
            directory_csv.write_text('inn,name\n0323410847,ООО Пример\n')
            summary = audit(db_path, [directory_csv], root / 'out')
            rows = [json.loads(line) for line in (root/'out/missing_suppliers.jsonl').read_text().splitlines()]
            self.assertEqual(summary['missing_supplier_identity'], 1)
            self.assertEqual(summary['verified_inns_written'], 0)
            self.assertTrue(all(row['supplier_tax_id'] is None for row in rows))
            self.assertEqual(rows[0]['name_candidates'][0]['status'], 'unique_name_candidate')
            self.assertEqual(rows[1]['raw_supplier_inns'], ['0323410847'])


class CourtDocumentTests(unittest.TestCase):
    def test_challenge_is_not_an_empty_case(self):
        with self.assertRaises(ValueError):
            discover_pdfs('<script src="/pravocaptcha.js"></script>', 'https://kad.arbitr.ru/Card/example')

    def test_captcha_library_does_not_hide_a_public_case_card(self):
        html='<script src="/pravocaptcha.js"></script><h1>А65-36865/2026</h1><h2>Участники дела</h2><a href="/Document/example.pdf">Решение</a>'
        self.assertEqual(discover_pdfs(html,'https://kad.arbitr.ru/Card/example'),['https://kad.arbitr.ru/Document/example.pdf'])

    def test_only_published_court_pdf_links(self):
        html = '<a href="/Document/example.pdf">Решение</a><a href="/Content/privacy.pdf">Политика</a>'
        self.assertEqual(discover_pdfs(html, 'https://kad.arbitr.ru/Card/example'), ['https://kad.arbitr.ru/Document/example.pdf'])
        with self.assertRaises(ValueError):
            official_url('https://kad.arbitr.ru.evil.test/example.pdf')

    def test_non_pdf_response_rejected(self):
        with self.assertRaises(ValueError):
            extract_pdf(b'<html>Challenge</html>', 'example', 'https://kad.arbitr.ru/Document/example.pdf', Path('unused'))

    def test_money_and_procedural_roles_remain_review_candidates(self):
        rows = text_evidence(['Истец: ООО Пример\nдоговор № ABC-22\nГоспошлина 1 200,00 руб.'])
        self.assertEqual({r['kind'] for r in rows}, {'party_role', 'contract_reference', 'money_mention'})
        self.assertTrue(all(r['page'] == 1 and r['review_status'] == 'candidate_requires_review' for r in rows))
        self.assertFalse(any('claim_amount' in row or 'supplier_tax_id' in row for row in rows))

    def test_redirect_destination_checked_before_request(self):
        client = Mock()
        client.get.return_value.is_redirect = True
        client.get.return_value.headers = {'location': 'https://example.com/file.pdf'}
        with self.assertRaises(ValueError):
            get(client, 'https://kad.arbitr.ru/Document/example.pdf')
        self.assertEqual(client.get.call_count, 1)


class SupplierTextTests(unittest.TestCase):
    def test_supplier_reference_is_only_a_candidate(self):
        result = mentions('Поставщик: ООО «Пример», ИНН: 0323410847')[0]
        self.assertEqual(result['status'], 'supplier_role_candidate')
        self.assertIsNone(result['supplier_tax_id'])

    def test_subcontractor_is_not_the_contract_supplier(self):
        result = mentions('Субподрядчик: ООО «Пример», ИНН 0323410847')[0]
        self.assertEqual(result['role_candidate'], 'subcontractor')
        self.assertNotEqual(result['status'], 'supplier_role_candidate')

    def test_customer_inn_excluded_even_with_supplier_word(self):
        result = mentions('Поставщик по требованию заказчика, ИНН 0323410847', '0323410847')[0]
        self.assertEqual(result['status'], 'customer_inn_mention')

    def test_invalid_proposed_replacements_flagged(self):
        for tax_id in ('5018077598', '2128700223', '6612001460'):
            result = mentions('ИНН '+tax_id)[0]
            self.assertFalse(result['checksum_valid'])
            self.assertEqual(result['status'], 'invalid_checksum')

    def test_unlabelled_long_number_is_not_extracted(self):
        self.assertEqual(mentions('Номер договора 0323410847; ИНН 0323410847000'), [])

    def test_nested_field_provenance(self):
        values = list(text_values({'clauses': ['ИНН 0323410847', 42]}, 'specification'))
        self.assertEqual(values, [('specification.clauses[0]', 'ИНН 0323410847')])


class MicrobatchTests(unittest.TestCase):
    def test_batch_preserves_named_inns_and_deduplicates_branches(self):
        from kad_microbatch import build_batch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'employers.csv'
            path.write_text('inn,name,match_method,sanctions_count\n0323410847,Company,inn,9\n0323410847,Branch,inn,9\n5018077598,Invalid,inn,10\n1650166051,Name match,name,9\n')
            plan = build_batch(path)
            inns = [r['inn'] for r in plan['targets']]
            self.assertEqual(len(inns), 6)
            self.assertEqual(len(set(inns)), 6)
            self.assertIn('5099000013', inns)
            self.assertNotIn('5018077598', inns)
            self.assertNotIn('1650166051', inns)

    def test_blocked_first_search_keeps_rest_pending(self):
        from kad_microbatch import run
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output/'microbatch.json').write_text(json.dumps({'period_from':'2022-01-01','period_to':'2026-10-08','targets':[{'inn':'1646019914','status':'pending'},{'inn':'1828020110','status':'pending'}]}))
            response = Mock(status_code=451, content=b'access denied', text='access denied', url='https://kad.arbitr.ru/Kad/SearchInstances')
            client = Mock()
            client.get.return_value = response
            client.post.return_value = response
            session = Mock()
            session.__enter__ = Mock(return_value=client)
            session.__exit__ = Mock(return_value=False)
            with patch('kad_microbatch.requests.Session', return_value=session), patch('kad_microbatch.time.sleep'):
                result = run(output)
            self.assertEqual(result['states'], {'access_blocked':1,'pending':1})
            self.assertEqual(result['pdf_documents'], 0)
            self.assertEqual(client.post.call_count, 1)


class OfflineArchiveTests(unittest.TestCase):
    def test_gzip_index_preserves_ids_and_unknown_execution_status(self):
        from offline_contracts import index_archive
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'contracts.jsonl.gz'
            record={'record_type':'contract','record_id':'example','source_url':'https://clearspending.ru/contracts/44/example',
                    'data':{'customer_inn':'0323410847','supplier_inns':['1650166051'],'sign_date':'2024-01-01','price_rub':100,'product_codes':['28.41']}}
            content=(json.dumps(record)+'\n').encode()
            with gzip.open(path,'wb') as handle:handle.write(content)
            result=index_archive(path,root/'out')
            self.assertEqual(result['decompressed_sha256'],hashlib.sha256(content).hexdigest())
            self.assertEqual(result['contracts'],1)
            self.assertEqual(result['distinct_party_inns'],2)
            self.assertEqual(result['network_requests'],0)
            with sqlite3.connect(result['database']) as db:
                self.assertIsNone(db.execute('SELECT execution_status FROM contracts').fetchone()[0])
                self.assertEqual(db.execute("SELECT tax_id FROM parties WHERE role='customer'").fetchone()[0],'0323410847')

    def test_existing_index_cannot_be_overwritten(self):
        from offline_contracts import index_archive
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'offline_contracts.sqlite').write_bytes(b'keep')
            with self.assertRaises(ValueError):index_archive(root/'not_needed.gz',root)
            self.assertEqual((root/'offline_contracts.sqlite').read_bytes(),b'keep')


if __name__ == '__main__':
    unittest.main()

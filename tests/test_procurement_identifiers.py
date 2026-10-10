import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backfill_procurement_identifiers import collect, run
from collection_cli import CollectionArchive
from models.source_records import SourceRecord
from parsers.registry_sources import ClearSpendingParser


class IdentifierTests(unittest.TestCase):
    def test_parser_and_normal_exports_preserve_identifiers(self):
        record = ClearSpendingParser().parse(json.dumps({'data': [{
            'regnum': 'one', 'subject': 'example', 'notif_num': '000123',
            'purchase_code': '000456', 'supplier_inns': []}]}), 'json').records[0]
        self.assertEqual(record.data['notif_num'], '000123')
        self.assertEqual(record.data['purchase_code'], '000456')
        with tempfile.TemporaryDirectory() as folder:
            archive = CollectionArchive(folder)
            try:
                archive.enqueue('clearspending', 'contracts', {})
                archive.commit(archive.next_job(), [record])
                archive.export({'start_date': '2026-01-01', 'end_date': '2026-01-02', 'queries': ['example']})
                for name in ('contracts.csv', 'contracts_summary.csv'):
                    with (Path(folder) / 'exports' / name).open(encoding='utf-8-sig') as handle:
                        row = next(csv.DictReader(handle))
                    self.assertEqual(row['notif_num'], '000123')
                    self.assertEqual(row['purchase_code'], '000456')
            finally:
                archive.close()

    def test_backfill_uses_exact_contract_and_rejects_changed_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / 'raw.json'
            raw.write_text(json.dumps({'data': [
                {'regnum': 'other', 'notif_num': 'WRONG'},
                {'regnum': 'one', 'notif_num': '000123', 'purchase_code': '000456'}]}))
            record = {'input_file': str(raw), 'input_sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
                      'data': {'contract_number': 'one', 'supplier_inns': [], 'supplier_names': []}}
            with sqlite3.connect(root / 'collection.sqlite') as db:
                db.execute('CREATE TABLE records(source TEXT,kind TEXT,id TEXT,json TEXT)')
                db.execute('INSERT INTO records VALUES(?,?,?,?)', ('clearspending', 'contract', 'one', json.dumps(record)))
            (root / 'exports').mkdir()
            (root / 'exports' / 'contracts.csv').write_text('contract_number,supplier_inns\none,[]\n')
            result = run(root, root / 'backup')
            self.assertEqual(result['filled_for_missing_supplier']['notif_num'], 1)
            with sqlite3.connect(root / 'collection.sqlite') as db:
                saved = json.loads(db.execute('SELECT json FROM records').fetchone()[0])
                self.assertEqual(saved['data']['notif_num'], '000123')
                self.assertEqual(saved['data']['supplier_inns'], [])
                raw.write_text('{}')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    collect(db, root)
            with (root / 'exports' / 'contracts.csv').open(encoding='utf-8-sig') as handle:
                self.assertEqual(next(csv.DictReader(handle))['purchase_code'], '000456')


if __name__ == '__main__':
    unittest.main()

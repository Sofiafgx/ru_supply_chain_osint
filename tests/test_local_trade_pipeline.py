import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_trade_pipeline import employer_index, load_local, normalized_name, resolve_importer, run


class LocalTradeTests(unittest.TestCase):
    def test_names_remain_distinct_and_invalid_provided_inn_not_replaced(self):
        self.assertEqual(normalized_name('АО «ВОТКИНСКИЙ ЗАВОД»'), normalized_name('Воткинский завод'))
        self.assertNotEqual(normalized_name('Купол'), normalized_name('Купол-ПРО'))
        index = {'воткинский завод': [{'inn': '1828020110'}]}
        row, audit = resolve_importer({'importer_name': 'АО "Воткинский завод"'}, index)
        self.assertEqual(row['importer_tax_id'], '1828020110')
        self.assertEqual(audit['method'], 'unique_exact_normalized_name')
        row, audit = resolve_importer({'importer_name': 'Воткинский завод', 'importer_tax_id': '1804002498'}, index)
        self.assertEqual(row['importer_tax_id'], '1804002498')
        self.assertEqual(audit['method'], 'invalid_provided_inn')
        index['воткинский завод'].append({'inn': '7735517743'})
        row, audit = resolve_importer({'importer_name': 'Воткинский завод'}, index)
        self.assertNotIn('importer_tax_id', row)
        self.assertEqual(audit['method'], 'ambiguous_or_invalid_dictionary_match')

    def test_offline_end_to_end_dates_exporters_and_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            (p/'employers.csv').write_text('name,inn,employer_id\nАО Воткинский завод,1828020110,1\n')
            plan = dict(target_country='RU', target_importer_inns=['1828020110'], hs_codes=['8542'],
                        date_range=dict(start_date='2022-01-01', end_date='2026-10-08'),
                        settings=dict(interval_grouping='importer_hs6'))
            (p/'plan.json').write_text(json.dumps(plan))
            rows = [dict(Buyer='Воткинский завод', HS='85423100', Date='2022-01-10', Exporter='A'),
                    dict(Buyer='АО «Воткинский завод»', HS='8542310000', Date='2022-01-01', Exporter='B')]
            (p/'input.json').write_text(json.dumps(rows))
            config = dict(input_file='input.json', output_dir='out', employers_file='employers.csv', plan_file='plan.json',
                          column_map=dict(importer_name='Buyer', hs_code='HS', shipment_date='Date', exporter_name='Exporter'),
                          defaults=dict(source='customs_gtd_dumps', import_country='RU', date_type='declaration'))
            (p/'config.json').write_text(json.dumps(config))
            manifest = run(p/'config.json')
            result = json.loads((p/'out/shipments.json').read_text())
            self.assertEqual([r['calculated_supply_interval_days'] for r in result], [9, None])
            self.assertEqual(manifest['name_fallback_rows'], 2)
            audit = json.loads((p/'out/row_audit.json').read_text())
            self.assertEqual(audit[0]['identity_resolution']['original_importer_name'], 'Воткинский завод')
            self.assertEqual(audit[0]['input_raw'], rows[0])

    def test_actual_dictionary_has_corrected_votkinsk(self):
        index = employer_index(Path(__file__).resolve().parents[1]/'database/employers_2026-10-09.csv')
        row, _ = resolve_importer({'importer_name': 'АО «Воткинский завод»'}, index)
        self.assertEqual(row['importer_tax_id'], '1828020110')

    def test_local_formats_preserve_strings(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            rows = [{'inn': '0012345678', 'hs': '854231', 'description': 'line 1\nline 2'}]
            for ext in ('json', 'jsonl'):
                f=p/('data.'+ext)
                f.write_text(json.dumps(rows) if ext=='json' else json.dumps(rows[0])+'\n')
                self.assertEqual(load_local(f, {}), rows)
            with (p/'data.csv').open('w', newline='') as handle:
                writer=csv.DictWriter(handle, fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            self.assertEqual(load_local(p/'data.csv', {}), rows)
            from openpyxl import Workbook
            workbook=Workbook();workbook.active.append(list(rows[0]));workbook.active.append(list(rows[0].values()))
            workbook.save(p/'data.xlsx');workbook.close()
            self.assertEqual(load_local(p/'data.xlsx', {}), rows)
            import pyarrow as arrow
            import pyarrow.parquet as parquet
            parquet.write_table(arrow.Table.from_pylist(rows), p/'data.parquet')
            self.assertEqual(load_local(p/'data.parquet', {}), rows)
            with sqlite3.connect(p/'data.db') as db:
                db.execute('CREATE TABLE customs (inn TEXT, hs TEXT, description TEXT)')
                db.execute('INSERT INTO customs VALUES (?,?,?)', tuple(rows[0].values()))
            self.assertEqual(load_local(p/'data.db', {'sqlite_table':'customs'}), rows)


if __name__ == '__main__':
    unittest.main()

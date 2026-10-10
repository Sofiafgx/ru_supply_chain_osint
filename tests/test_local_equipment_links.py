import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_equipment_links import build, mentions


class LocalEquipmentLinksTests(unittest.TestCase):
    def test_models_are_not_catalog_numbers(self):
        found = mentions('УЦИ ND780 Id.Nr. 220 010-01 Товарный знак HEIDENHAIN')
        self.assertEqual([(x[0], x[1]) for x in found], [('Heidenhain', 'ND780')])

    def test_shaving_products_do_not_become_machine_tools(self):
        self.assertEqual(mentions('Станок для бритья одноразовый'), [])

    def test_explicit_model_without_brand(self):
        self.assertEqual(mentions('Прецизионный плоскошлифовальный станок модели ОШ-550 исп. 31')[0][1],'ОШ-550')

    def test_batch_card_scoping_and_evidence_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rawdir = root/'data/collection_20261008/raw/clearspending'
            rawdir.mkdir(parents=True)
            rawpath = rawdir/'batch.json'
            cards = [dict(regnum='one',customer_inn='1111111111',customer_name='Завод Один',
                          sign_date='2022-01-11',subject='Приобретение датчиков',fz='223',
                          product_names=['ND780 Id.Nr. 220 010-01 HEIDENHAIN']),
                     dict(regnum='two',customer_inn='2222222222',customer_name='Завод Два',
                          sign_date='2022-02-12',subject='Ремонт станка HAAS VF-3',fz='223',product_names=[])]
            rawpath.write_text(json.dumps({'data':cards}))
            folder=root/'data/compliance_offline_20261009/processed'
            folder.mkdir(parents=True)
            con=sqlite3.connect(folder/'offline_contracts.sqlite')
            con.execute('CREATE TABLE contracts(contract_ref_id TEXT, input_file TEXT, customer_tax_id TEXT, source_url TEXT)')
            for row in cards:
                con.execute('INSERT INTO contracts VALUES(?,?,?,?)',(row['regnum'],str(rawpath),row['customer_inn'],'https://example.org/'+row['regnum']))
            con.commit();con.close()
            (folder.parent/'verified_tax_ids.json').write_text('[]')
            (root/'config').mkdir()
            (root/'config/russian_defence_entities.json').write_text('{"entities":[]}')
            (root/'config/facility_names.json').write_text('{"1111111111":["Завод Один"]}')
            for name in ('industrial_20261009','industrial_20261009_followup'):
                industrial=root/'data'/name
                (industrial/'raw').mkdir(parents=True)
                for filename in ('records.json','records.evidence.json'):
                    (industrial/filename).write_text('[]')
            out=root/'output'
            with contextlib.redirect_stdout(io.StringIO()):
                build(root,out)
            rows=[json.loads(line) for line in (out/'enterprise_equipment_links.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),2)
            byinn={r['company_inn']:r for r in rows}
            self.assertEqual(byinn['1111111111']['equipment_brand'],'Heidenhain')
            self.assertEqual(byinn['2222222222']['equipment_brand'],'Haas')
            self.assertTrue(byinn['1111111111']['is_target_company'])
            self.assertFalse(byinn['2222222222']['is_target_company'])
            for row in rows:
                self.assertFalse(row['verification_confirmed'])
                self.assertFalse(row['ownership_confirmed'])
                self.assertIsNone(row['vri_id'])
                self.assertIsNone(row['verification_date'])
                self.assertIsNone(row['equipment_serial_number'])


if __name__=='__main__':
    unittest.main()

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import httpx
from analytics.resilience import tts, myerson, loss_scenario, radar_candidate
from analytics.proxy_model import SupplyChainProxyModel
from collector_archive import archive_response
from evidence_pipeline import Ledger, run


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Ledger(self.root / 'db.sqlite3')
        self.a, self.b = '7719830028', '6444009038'
        self.meta = dict(source_url='https://example.org/contract', collected_at='2026-10-10T12:00:00+03:00')
        self.pilots = {self.a, self.b}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, kind, row, line=1, meta=None):
        return self.store.observe(kind, row, self.root/'input.jsonl', 'a'*64, line,
                                  self.meta if meta is None else meta, self.pilots)

    def contract(self, number):
        return dict(customer_inn=self.a, supplier_inn=self.b, contract_id=number, verified=True)

    def test_parallel_contracts_and_conflicting_versions_preserved(self):
        self.add('contract', self.contract('1'))
        self.add('contract', self.contract('2'), 2)
        self.add('contract', dict(self.contract('1'), price_rub=99), 3)
        self.add('contract', self.contract('1'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM contracts').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM contract_evidence').fetchone()[0], 3)
        self.assertEqual(self.store.db.execute('SELECT collected_at FROM observations LIMIT 1').fetchone()[0], '2026-10-10T09:00:00+00:00')

    def test_no_edges_without_numbers_roles_source_or_time(self):
        self.add('contract', self.contract(None))
        self.add('contract', dict(self.contract('2'), verified=False), 2)
        self.add('contract', self.contract('3'), 3, {})
        self.add('court', dict(party_a_inn=self.a, party_b_inn=self.b, case_number='case', verified=True), 4)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM graph_edges').fetchone()[0], 0)
        self.add('court', dict(plaintiff_inn=self.a, respondent_inn=self.b, case_number='case', verified=True), 5)
        self.assertEqual(self.store.db.execute('SELECT kind FROM graph_edges').fetchone()[0], 'court')

    def test_null_stock_and_norms_are_stored_as_null(self):
        self.add('inventory', dict(inn=self.a, S_safe=None, D_daily=5, verified=True, stock_unit='kg', demand_unit='kg'))
        self.add('consumption', dict(supplier_inn=self.a, r_kj=None, verified=True, unit='kg/unit'), 2)
        self.assertIsNone(self.store.db.execute('SELECT S_safe FROM inventory').fetchone()[0])
        self.assertIsNone(self.store.db.execute('SELECT r_kj FROM consumption').fetchone()[0])
        values = [json.loads(r[0]) for r in self.store.db.execute('SELECT result FROM metrics')]
        self.assertIsNone(values[0]['tts_days'])
        self.assertIsNone(values[1]['r_kj'])

    def test_invalid_manifest_rolls_back_all_observations(self):
        path=self.root/'input.jsonl';path.write_text('{"S_safe": null}\n')
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps(dict(inputs=[dict(kind='inventory',path='input.jsonl')], queries=[dict(query_id='q',source='s',category='bad')])))
        with self.assertRaises(ValueError):
            run(manifest,self.root/'db.sqlite3',self.root/'out')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0],0)

    def test_http_archive_preserves_bytes_and_appends(self):
        response=httpx.Response(200, content=b'{"x": 1}\n',request=httpx.Request('GET','https://example.org'))
        path=self.root/'raw.jsonl'
        audit=archive_response(path,response);archive_response(path,response)
        self.assertEqual(audit['input_sha256'],hashlib.sha256(response.content).hexdigest())
        self.assertEqual(len(path.read_text().splitlines()),2)

    def test_coverage_disjoint_and_pending_separate(self):
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps(dict(queries=[dict(query_id='a',source='s',category='found'),dict(query_id='b',source='s',category='no_result'),dict(query_id='c',source='s',category='technical_access_unavailable')],pending_queries=[dict(query_id='d')])) )
        report=run(manifest,self.root/'db.sqlite3',self.root/'out')
        self.assertEqual(list(report['counts'].values()),[1,1,1])
        self.assertFalse(report['query_scope_complete'])


class MetricTests(unittest.TestCase):
    def test_verified_units_zero_and_missing_stock(self):
        self.assertIsNone(tts(None,5,'kg','kg',True))
        self.assertIsNone(tts(100,5,'kg','units',True))
        self.assertIsNone(tts(100,0,'kg','kg',True))
        self.assertEqual(tts(0,5,'kg','kg',True),0)
        self.assertEqual(tts(100,5,'kg','kg',True),20)
        self.assertIsNone(SupplyChainProxyModel().compute_tts('x'))
        self.assertIsNone(SupplyChainProxyModel().compute_ttr('x'))

    def test_myerson_graph_restriction_and_efficiency(self):
        game=lambda s: 1.0 if len(s)==3 else 0.0
        result=myerson(['a','b','c'],[('a','b'),('b','c')],game)
        self.assertAlmostEqual(sum(result.values()),1)
        self.assertTrue(all(abs(v-1/3)<1e-10 for v in result.values()))
        self.assertEqual(myerson(['a','b','c'],[('a','b')],game),dict(a=0,b=0,c=0))
        # Endpoints need the middle player to communicate in the unanimity game.
        game=lambda s: float('a' in s and 'c' in s)
        self.assertTrue(all(abs(v-1/3)<1e-10 for v in myerson(['a','b','c'],[('a','b'),('b','c')],game).values()))

    def test_scenario_intervals_and_radar_bounds(self):
        result=loss_scenario([2,4],10,100,'units','explicit hypothetical interval')
        self.assertEqual(result['loss_interval'],[20,40])
        self.assertEqual(result['rei_interval'],[.2,.4])
        with self.assertRaises(ValueError): loss_scenario([4,2],10,100,'units','test')
        candidate=radar_candidate('2026-01-01T00:00:00Z','2026-01-05T00:00:00Z')
        self.assertFalse(candidate['confirms_production_recovery'])
        with self.assertRaises(ValueError): radar_candidate('2026-01-05','2026-01-01')


if __name__ == '__main__': unittest.main()

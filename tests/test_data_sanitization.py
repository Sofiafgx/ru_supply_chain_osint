import json
import tempfile
import unittest
from pathlib import Path
from data_sanitization import sanitize_record, audit
from analytics.proxy_model import SupplyChainProxyModel
from evidence_pipeline import Ledger


class SanitizationTests(unittest.TestCase):
    def fact(self, **changes):
        return dict(verified=True, source_url='https://source.example/document/1',
                    document_id='1', collected_at='2026-10-10T10:00:00Z', **changes)

    def proof(self, value, **changes):
        return self.fact(value=value, **changes)

    def test_nested_flags_and_upstream_paths_fail_closed(self):
        for change in [dict(is_synthetic=True), dict(is_demo=True),
                       dict(provenance={'input_file':'outputs/comtrade_smoke/x.jsonl'}),
                       dict(data={'is_demo':True})]:
            row, _, reasons = sanitize_record(self.fact(**change))
            self.assertIn('synthetic_demo_or_test_origin', reasons)
            self.assertFalse(row['is_eligible_for_calculation'])
        for path in ['x.synthetic.jsonl','outputs/patents/synthetic_demo.jsonl','osint_pipeline_smoke_abc/a.ndjson','examples/real.jsonl']:
            self.assertFalse(sanitize_record(self.fact(),path)[0]['is_eligible_for_calculation'])

    def test_static_values_are_removed_by_meaning_not_magnitude(self):
        row, count, _ = sanitize_record(self.fact(monthly_capacity=5500, tts_days=30,
                    ttr_months=9, nested={'ttr_months':1.5}, price_rub=550))
        self.assertEqual(count,4)
        self.assertIsNone(row['monthly_capacity'])
        self.assertIsNone(row['tts_days'])
        self.assertIsNone(row['nested']['ttr_months'])
        self.assertEqual(row['price_rub'],550)

    def test_manual_bom_nulls_quantities_and_never_imputes(self):
        original = self.fact(foreign_components=[dict(requires_manual_verification=True,
                                     units_per_system=4,r_kj=2,quantity=0)])
        row, count, reasons = sanitize_record(original)
        self.assertEqual(count,3)
        self.assertIn('pending_manual_verification',reasons)
        self.assertTrue(all(row['foreign_components'][0][k] is None for k in ('units_per_system','r_kj','quantity')))
        self.assertEqual(original['foreign_components'][0]['r_kj'],2)
        row, count, _ = sanitize_record(self.fact(units_per_system=None,S_safe=None))
        self.assertEqual(count,0)
        self.assertIsNone(row['units_per_system'])

    def test_missing_metric_wrappers_are_preserved_and_not_counted(self):
        row,count,_=sanitize_record(self.fact(metric_inputs={
            'TTR':{'value':None,'missing':['recovery_date'],'target_capacity_fraction':1.0},
            'TTS':{'value':30,'missing':['verified_stock']}}))
        self.assertEqual(count,1)
        self.assertEqual(row['metric_inputs']['TTR'],{'value':None,'missing':['recovery_date'],'target_capacity_fraction':1.0})
        self.assertIsNone(row['metric_inputs']['TTS']['value'])

    def test_evidence_is_bound_to_each_value_and_capacity_requires_bom(self):
        proof=self.proof(550,method='bottom_up_bom',inputs=[self.proof(1)])
        row,count,reasons=sanitize_record(self.fact(monthly_capacity=550,
                field_evidence={'monthly_capacity':proof}))
        self.assertEqual(count,0)
        self.assertFalse(reasons)
        self.assertEqual(row['monthly_capacity'],550)
        self.assertEqual(SupplyChainProxyModel(550,proof).base_capacity,550)
        self.assertIsNone(SupplyChainProxyModel(550).base_capacity)
        proof['value']=5500
        row,_,_=sanitize_record(self.fact(monthly_capacity=550,field_evidence={'monthly_capacity':proof}))
        self.assertIsNone(row['monthly_capacity'])

    def test_false_and_string_verification_do_not_pass(self):
        for verified in (False,'true',1,None):
            row=self.fact();row['verified']=verified
            self.assertFalse(sanitize_record(row)[0]['is_eligible_for_calculation'])
        row=self.fact(is_eligible_for_calculation=False)
        self.assertFalse(sanitize_record(row)[0]['is_eligible_for_calculation'])

    def test_graph_and_ledger_reject_demo_even_when_verified(self):
        edge=self.fact(kind='contract',contract_id='1',customer_inn='7719830028',
                       supplier_inn='6444009038',input_sha256='a'*64)
        model=SupplyChainProxyModel()
        model.build_network([edge,dict(edge,is_demo=True,contract_id='2')])
        self.assertEqual(model.graph.number_of_edges(),1)
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db.sqlite3')
            try:
                result=ledger.observe('contract',dict(edge,is_synthetic=True),Path(tmp)/'x.jsonl','a'*64,1,{},set())
                self.assertIsNone(result)
                self.assertEqual(ledger.db.execute('SELECT count(*) FROM observations').fetchone()[0],0)
            finally:ledger.db.close()

    def test_old_ledger_graph_rows_do_not_pass_without_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db.sqlite3';ledger=Ledger(path)
            try:
                a,b='7719830028','6444009038'
                edge=self.fact(contract_id='1',customer_inn=a,supplier_inn=b)
                ident=ledger.observe('contract',edge,Path(tmp)/'x.jsonl','a'*64,1,{}, {a,b})
                self.assertEqual(ledger.db.execute('SELECT count(*) FROM graph_edges').fetchone()[0],1)
                ledger.db.execute('UPDATE observations SET payload=? WHERE id=?',(json.dumps(edge),ident))
                self.assertEqual(ledger.db.execute('SELECT count(*) FROM graph_edges').fetchone()[0],0)
            finally:ledger.db.close()

    def test_documented_stock_and_demand_produce_tts_without_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db.sqlite3')
            try:
                row=self.fact(S_safe=100,D_daily=5,stock_unit='kg',demand_unit='kg',
                    field_evidence={'S_safe':self.proof(100),'D_daily':self.proof(5)})
                ledger.observe('inventory',row,Path(tmp)/'x.jsonl','a'*64,1,{},set())
                metric=json.loads(ledger.db.execute('SELECT result FROM metrics').fetchone()[0])
                self.assertEqual(metric['tts_days'],20)
                self.assertTrue(metric['is_eligible_for_calculation'])
                row['field_evidence']['D_daily']['value']=50
                ledger.observe('inventory',row,Path(tmp)/'x.jsonl','a'*64,2,{},set())
                self.assertIsNone(ledger.db.execute('SELECT D_daily FROM inventory ORDER BY rowid DESC').fetchone()[0])
            finally:ledger.db.close()

    def test_report_counts_and_repeatability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'x.jsonl').write_text('\n'.join(json.dumps(r) for r in [self.fact(),self.fact(is_demo=True),{'tts_days':30}]))
            output=root/'out'
            first=audit(root,output)
            second=audit(root,output)
            self.assertEqual(first['summary'],second['summary'])
            self.assertEqual(first['summary'],dict(total_records_inspected=3,synthetic_records_removed=1,
                hardcoded_constants_reset_to_null=1,eligible_records_passed=1))
            self.assertEqual(len(json.loads((output/'sanitization_report.json').read_text())['cleaned_dataset']),1)



if __name__=='__main__':unittest.main()

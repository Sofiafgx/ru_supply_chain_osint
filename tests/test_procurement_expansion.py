import csv
import json
from pathlib import Path
import tempfile
import unittest
from procurement_expansion import run

class ProcurementTests(unittest.TestCase):
    def test_anonymous_and_unverified_distributor_never_form_edges(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'contracts.csv'
            fields=['contract_number','customer_inn','customer_name','supplier_inns','supplier_names','sign_date','price_rub','title','product_names','source_url']
            with source.open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
                for number,suppliers in [('anonymous',[]),('identified',['7707083893'])]:
                    writer.writerow(dict(contract_number=number,customer_inn='7719830028',customer_name='Завод',supplier_inns=json.dumps(suppliers),supplier_names=json.dumps(['Дистрибьютор'] if suppliers else []),sign_date='2024-02-01',price_rub='123.45',title='Микросхемы',product_names='[]',source_url='https://example.org/contracts/'+number))
            output=root/'outputs/edges.ndjson'
            no_catalog=run(source,None,output,targets=['7719830028'])
            self.assertEqual(no_catalog['confirmed_edges'],0)
            self.assertEqual(no_catalog['incomplete_candidates'],1)
            catalog=root/'distributors.json'
            catalog.write_text(json.dumps([dict(inn='7707083893',sector='electronics',registration_source_url='https://example.org/registry/7707083893',sector_source_url='https://example.org/catalog/7707083893')]))
            verified=run(source,None,output,catalog,targets=['7719830028'])
            self.assertEqual(verified['unique_pairs'],1)
            self.assertEqual(verified['confirmed_contract_total_rub'],'123.45')
            row=json.loads(output.read_text())
            self.assertEqual(set(row),{'customer_inn','supplier_inn','contract_id','sign_date','price_rub','subject_text'})
            self.assertEqual(row['contract_id'],'identified')

if __name__=='__main__':unittest.main()

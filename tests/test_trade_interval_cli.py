import unittest
from trade_interval_cli import normalize


class TradeIntervalTests(unittest.TestCase):
    def row(self, day, **kwargs):
        return dict(source='analiz-ved', importer_tax_id='0012345678', exporter_name='Exporter',
                    hs_code='8542310000', shipment_date=day, date_type='declaration', **kwargs)

    def test_unsorted_same_day_and_distinct_exporters(self):
        a = self.row('2024-01-10', shipment_id='A')
        b = self.row('2024-01-01', shipment_id='B')
        c = self.row('2024-01-10', shipment_id='C')
        other = dict(self.row('2024-01-02'), exporter_name='Other')
        rows, audit, summary = normalize([a, b, c, a, other])
        self.assertEqual([r['calculated_supply_interval_days'] for r in rows], [9, None, 9, None])
        self.assertEqual(rows[0]['hs_code'], '854231')
        self.assertEqual(rows[0]['importer_tax_id'], '0012345678')
        self.assertEqual(len(summary), 2)

    def test_unknown_date_currency_weight_and_packages(self):
        raw = dict(self.row('2024-01-10'), date_type='arrival', weight_raw='10kg',
                   quantity='4', quantity_unit='cartons', declared_value_usd='200', value_currency='EUR')
        rows, _, summary = normalize([raw])
        for field in ('shipment_date', 'net_weight_kg', 'quantity_units', 'declared_value_usd', 'calculated_supply_interval_days'):
            self.assertIsNone(rows[0][field])
        self.assertEqual(summary, [])

    def test_explicit_units_and_customs_value(self):
        rows, _, _ = normalize([self.row('2024-01-10', quantity='4', quantity_unit='pcs',
            declared_value_usd='200', value_currency='USD', value_type='customs_declared')])
        self.assertEqual(rows[0]['quantity_units'], 4)
        self.assertEqual(rows[0]['declared_value_usd'], 200)

    def test_missing_identity_and_cross_source_are_not_linked(self):
        a = self.row('2024-01-01')
        b = dict(self.row('2024-01-10'), source='panjiva')
        c = dict(self.row('2024-01-20'), importer_tax_id=None)
        rows, _, _ = normalize([a, b, c])
        self.assertTrue(all(r['calculated_supply_interval_days'] is None for r in rows))

    def test_scoped_records_require_exact_inn_country_and_declaration_date(self):
        plan = dict(target_country='RU', target_importer_inns=['7735517743'], hs_codes=['8542'],
                    date_range=dict(start_date='2022-01-01', end_date='2026-10-08'))
        good = dict(self.row('2022-01-01'), importer_tax_id='7735517743', import_country='RU')
        end = dict(good, shipment_date='2026-10-08')
        wrong_country = dict(good, import_country='CN')
        no_inn = dict(good, importer_tax_id=None)
        late = dict(good, shipment_date='2026-10-09')
        rows, audit, _ = normalize([good, end, wrong_country, no_inn, late], plan)
        self.assertEqual(len(rows), 2)
        self.assertEqual([x['status'] for x in audit[2:]], ['excluded_scope', 'pending_scope', 'excluded_scope'])


if __name__ == '__main__':
    unittest.main()

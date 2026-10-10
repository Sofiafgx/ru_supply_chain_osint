import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import eis_ftp_contracts as eis

CUSTOMER = '7719830028'
SUPPLIER = '6444009038'


def contract(customer=CUSTOMER, supplier=SUPPLIER, extra=''):
    return (f'<contract><customer><inn>{customer}</inn><fullName>Замовник</fullName></customer>'
            f'<suppliers><supplier><inn>{supplier}</inn><fullName>Постачальник</fullName>'
            f'</supplier></suppliers>{extra}</contract>')


def parse(xml, law='44', targets=None):
    return list(eis.xml_records(io.BytesIO(xml.encode()), law, targets or eis.TARGETS, eis.PATHS))


class ContractsTests(unittest.TestCase):
    def test_checksum(self):
        self.assertEqual(eis.valid_inn(CUSTOMER), CUSTOMER)
        self.assertIsNone(eis.valid_inn('1646019864'))
        self.assertIsNone(eis.valid_inn('0000000000'))
        self.assertIsNone(eis.valid_inn('7719830029'))

    def test_namespaces_and_numeric_price(self):
        xml = contract(extra='<regNum>000123</regNum><price>1234.56</price>'
                       '<signDate>2026-10-09T17:00:00+03:00</signDate>'
                       '<products><product><name>Верстат</name></product></products>')
        xml = xml.replace('<contract>', '<contract xmlns="urn:test:v99">')
        row = json.loads(eis.dump_record(parse(xml)[0]))
        self.assertEqual(row['contract_id'], '000123')
        self.assertEqual(row['sign_date'], '2026-10-09')
        self.assertEqual(row['price_rub'], 1234.56)
        self.assertEqual(row['product_description'], 'Верстат')
        self.assertEqual(len(row), 8)

    def test_null_fields(self):
        row = parse(contract())[0]
        for key in ('contract_id', 'sign_date', 'price_rub', 'product_description'):
            self.assertIsNone(row[key])
        row = parse(contract().replace('<fullName>Постачальник</fullName>', ''))[0]
        self.assertIsNone(row['supplier_name'])

    def test_both_directions_and_invalid_opposite_party(self):
        self.assertEqual(len(parse(contract(), targets={CUSTOMER})), 1)
        self.assertEqual(len(parse(contract(), targets={SUPPLIER})), 1)
        self.assertEqual(parse(contract(customer='1234567890'), targets={SUPPLIER}), [])
        self.assertEqual(parse(contract(supplier='1234567890'), targets={CUSTOMER}), [])
        self.assertEqual(parse(contract(supplier='')), [])
        self.assertEqual(parse(contract(), targets={'1646019864'}), [])
        self.assertEqual(len(parse(contract(customer='1646019864'))), 1)
        self.assertEqual(len(parse(contract(supplier='1646019864'))), 1)

    def test_pair_filter_does_not_leak_other_suppliers(self):
        xml = contract().replace('</suppliers>',
                                 '<supplier><inn>7719830028</inn></supplier></suppliers>')
        rows = parse(xml, targets={SUPPLIER})
        self.assertEqual([r['supplier_inn'] for r in rows], [SUPPLIER])

    def test_223_and_foreign_currency(self):
        xml = (f'<contract><body><item><customerInfo><INN>{CUSTOMER}</INN></customerInfo>'
               f'<contractData><supplierInfo><legalEntity><inn>{SUPPLIER}</inn></legalEntity>'
               '</supplierInfo><contractRegNumber>223-1</contractRegNumber>'
               '<contractDate>2026-10-09</contractDate><price>100.10</price>'
               '<currency><code>643</code></currency><contractPositions>'
               '<contractPosition><name>Деталь</name></contractPosition>'
               '</contractPositions></contractData></item></body></contract>')
        row = parse(xml, '223')[0]
        self.assertEqual(row['contract_id'], '223-1')
        self.assertEqual(row['product_description'], 'Деталь')
        self.assertEqual(str(row['price_rub']), '100.10')
        self.assertIsNone(parse(xml.replace('643', '840'), '223')[0]['price_rub'])
        item = ET.fromstring(xml).find('body/item')
        second = ET.tostring(item, encoding='unicode').replace('223-1', '223-2')
        rows = parse(xml.replace('</body>', second + '</body>'), '223')
        self.assertEqual([r['contract_id'] for r in rows], ['223-1', '223-2'])

    def test_wrapper_with_many_contracts(self):
        rows = parse('<export>' + contract()*1000 + '</export>')
        self.assertEqual(len(rows), 1000)

    def test_dtd_and_unsupported_documents(self):
        with self.assertRaises(ValueError):
            parse('<!DOCTYPE contract [<!ENTITY x "bad">]>' + contract())
        with self.assertRaises(ValueError):
            parse('<purchaseNotice/>')
        reader = eis.SafeXML(io.BytesIO(b'<!DOCTYPE contract>'))
        reader.read(5)
        with self.assertRaises(ValueError):
            reader.read(5)

    def test_daily_dates(self):
        day = eis.archive_day('contract_R_2026100900_2026101000_001.xml.zip', '')
        self.assertEqual(day.isoformat(), '2026-10-09')
        self.assertIsNone(eis.archive_day('contract_R_2026090100_2026100100_001.xml.zip', ''))

    def test_discovery_keeps_every_latest_part(self):
        from types import SimpleNamespace
        args = SimpleNamespace(laws=['44'], root44='/fcs_regions', root223='/dish',
                               regions=['R'], remote_dirs=['contracts'], days=1)
        entries = [('contract_R_2026100900_2026101000_001.xml.zip', 'file', ''),
                   ('contract_R_2026100900_2026101000_002.xml.zip', 'file', ''),
                   ('contract_R_2026100800_2026100900_001.xml.zip', 'file', ''),
                   ('contract_R_2026090100_2026100100_001.xml.zip', 'file', '')]
        with patch.object(eis, 'ftp_session') as session, patch.object(eis, 'listing', return_value=entries):
            session.return_value.__enter__.return_value = object()
            jobs = eis.discover(args)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all('2026100900' in path for _, path in jobs))

    def test_local_cli_and_atomic_failure(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'good.zip'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('nested/one.xml', contract())
                zf.writestr('two.xml', contract())
            args = SimpleNamespace(local_zip=[archive], local_law='44', output=root/'out.ndjson',
                                   temp_dir=None, concurrency=2, targets=eis.TARGETS, max_xml_mib=1)
            asyncio.run(eis.run(args, eis.PATHS))
            saved = args.output.read_bytes()
            self.assertEqual(len(saved.splitlines()), 2)
            broken = root/'broken.zip'
            with zipfile.ZipFile(broken, 'w') as zf:
                zf.writestr('broken.xml', '<contract><customer>')
            args.local_zip = [archive, broken]
            with self.assertRaises(ET.ParseError):
                asyncio.run(eis.run(args, eis.PATHS))
            self.assertEqual(args.output.read_bytes(), saved)
            self.assertFalse(list(root.glob('.eis-*')))


if __name__ == '__main__':
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from patent_cli import main, parse_content, verify_pdf_ipc
from parsers.patent_parser import Patent, analyze, from_json, match_target, parse_html, parse_xml, records


def fixture(**overrides):
    row = {'patent_number': 'RU9999999C1', 'assignee_name': 'АО "ВНИИР-Прогресс"',
           'publication_date': '2025-01-02', 'device_title': 'СИНТЕТИЧНИЙ ПРИКЛАД', 'ipc': ['G01S 3/02'],
           'claims': ['Устройство содержит 4 приемных канала. Каждый приемный канал содержит АЦП и полосовой фильтр.']}
    row.update(overrides)
    return from_json(row)


class ArchitectureTests(unittest.TestCase):
    def test_one_component_per_channel(self):
        result, status = records(fixture())
        self.assertEqual(status, 'accepted')
        self.assertEqual(result[0]['calculated_component_multiplier']['ADC']['value'], 4)
        self.assertEqual(result[0]['detected_architecture_channels']['values'], [4])
        self.assertIn('Каждый приемный канал', result[0]['raw_snippet_quote'])

    def test_eight_and_twelve(self):
        for n in (8, 12):
            _, multiplier, _ = analyze(f'{n} приемных каналов. Каждый приемный канал содержит АЦП.')
            self.assertEqual(multiplier['ADC']['value'], n)

    def test_two_per_channel_and_symbolic(self):
        _, multiplier, _ = analyze('Число каналов N = 8. Каждый приемный канал содержит два АЦП.')
        self.assertEqual(multiplier['ADC']['value'], 16)
        self.assertEqual(multiplier['ADC']['r_kj'], '2*N')
        _, multiplier, _ = analyze('N приемных каналов. Каждый приемный канал содержит ПЛИС.')
        self.assertIsNone(multiplier['FPGA']['value'])
        self.assertEqual(multiplier['FPGA']['r_kj'], 'N')

    def test_count_alone_is_not_consumption(self):
        _, multiplier, reasons = analyze('Многоканальный блок содержит 8 каналов и АЦП.')
        self.assertIsNone(multiplier)
        self.assertIn('no_verified_per_channel_component_relation', reasons)

    def test_ambiguous_prior_art_shared_negation_ranges(self):
        snippets = [
            'Число каналов N=4, 8, 12. Каждый приемный канал содержит АЦП.',
            '4, 8 или 12 каналов. Каждый приемный канал содержит АЦП.',
            '4-, 8-, 12-канальный блок. Каждый приемный канал содержит АЦП.',
            '4 канала. Каждый приемный канал содержит два или три АЦП.',
            '4 канала. Каждый приемный канал содержит АЦП или ПЛИС.',
            'Известный прототип содержит 4 канала. Каждый приемный канал содержит АЦП.',
            '4 канала. Каждый приемный канал содержит АЦП и общий для всех каналов генератор тактовых импульсов.',
            '4 канала. Каждый приемный канал содержит АЦП, но не содержит ПЛИС.',
            'До 8 каналов. Каждый приемный канал содержит АЦП.',
            '4 приемных канала и 8 передающих каналов. Каждый приемный канал содержит АЦП.',
        ]
        for snippet in snippets:
            with self.subTest(snippet=snippet):
                self.assertIsNone(analyze(snippet)[1])

    def test_no_borrowed_count_from_other_claim(self):
        rows, _ = records(fixture(claims=['Многоканальный блок содержит 4 канала.', 'Каждый приемный канал содержит АЦП.']))
        self.assertTrue(all(r['calculated_component_multiplier'] is None for r in rows))

    def test_adjacent_description(self):
        rows, _ = records(fixture(claims=[], description=['Устройство содержит 8 приемных каналов.', 'Каждый приемный канал содержит АЦП.']))
        self.assertEqual(rows[0]['calculated_component_multiplier']['ADC']['value'], 8)

    def test_number_is_not_component_label_or_antenna_count(self):
        for snippet in ['Каждый приемный канал содержит АЦП 4 и фильтр 8.',
                        'N=8 антенн подключены к каналам. Каждый приемный канал содержит АЦП.']:
            self.assertIsNone(analyze(snippet)[1])

    def test_component_outside_clause(self):
        _, multiplier, _ = analyze('4 канала. Каждый приемный канал содержит АЦП, а блок обработки содержит ПЛИС.')
        self.assertEqual(set(multiplier), {'ADC'})

    def test_assignee_and_ipc_filters(self):
        for patent in [fixture(assignee_name='Другая организация', description='АО ВНИИР-Прогресс'),
                       fixture(ipc=['H04B 1/00']), fixture(ipc=[])]:
            self.assertEqual(records(patent)[0], [])
        for name in ['ОАО НИЦЭВТ', 'JSC VNIIR Progress', 'АО АНПП Темп-Авиа', 'АО НИИ КП']:
            self.assertIsNotNone(match_target(name))
        self.assertIsNone(match_target('НИИ КПИ'))


class SourceTests(unittest.TestCase):
    def test_google_metadata_does_not_include_citation_assignee(self):
        html = '''<meta name="citation_patent_number" content="RU:1234567:C1">
        <meta name="DC.title" content="Устройство"><meta name="DC.date" scheme="issue" content="2025-01-02">
        <meta name="DC.date" scheme="dateSubmitted" content="2020-01-01">
        <meta name="DC.contributor" scheme="assignee" content="Другая организация">
        <dd itemprop="assigneeOriginal">АО НИЦЭВТ</dd>
        <li itemprop="classifications"><span itemprop="Code">G06F12/00</span><meta itemprop="IsCPC" content="true"></li>
        <section itemprop="description"><div class="description-paragraph">АО НИЦЭВТ: многоканальный блок</div></section>'''
        patent = parse_html(html)
        self.assertEqual(patent.assignees, ['Другая организация'])
        self.assertEqual(patent.ipc, [])
        self.assertEqual(patent.cpc, ['G06F12/00'])
        self.assertEqual(patent.publication_date, '2025-01-02')
        self.assertEqual(records(patent)[0], [])

    def test_st36_and_namespaces(self):
        xml = '''<patent-document xmlns="urn:test"><bibliographic-data>
        <publication-reference><document-id><country>RU</country><doc-number>9999999</doc-number><kind>C1</kind><date>20250102</date></document-id></publication-reference>
        <parties><assignees><assignee><addressbook><orgname>АО НИИ КП</orgname></addressbook></assignee></assignees></parties>
        <invention-title>Устройство</invention-title><classification-ipcr><section>G</section><class>01</class><subclass>S</subclass></classification-ipcr>
        </bibliographic-data><claims><claim><claim-text>4 канала. Каждый приемный канал содержит АЦП.</claim-text></claim></claims></patent-document>'''
        patent = parse_xml(xml)
        self.assertEqual(patent.ipc, ['G01S'])
        self.assertEqual(patent.publication_date, '2025-01-02')
        self.assertEqual(len(records(patent)[0]), 1)
        self.assertEqual(len(parse_content(('<batch>' + xml + xml + '</batch>').encode(), 'batch.xml')), 2)

    def test_fips_inid(self):
        html = '''<html><body><p>(11) 9999999</p><p>(13) C1</p><p>(51) МПК G01S 3/02</p>
        <p>(45) Опубликовано 02.01.2025</p><p>(54) Устройство</p><p>(73) Патентообладатель: АО НИИ КП</p>
        <p>(57) Реферат</p><h2>Описание изобретения</h2><p>Многоканальный приемник.</p>
        <h2>Формула изобретения</h2><p>4 канала. Каждый приемный канал содержит АЦП.</p></body></html>'''
        patent = parse_html(html)
        self.assertEqual(patent.patent_number, 'RU9999999C1')
        self.assertEqual(records(patent)[0][-1]['calculated_component_multiplier']['ADC']['value'], 4)

    def test_cli_offline_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'input.json', Path(tmp) / 'result.jsonl'
            source.write_text(json.dumps({'patent_number': 'RU9999999C1', 'assignee_name': 'АО НИЦЭВТ',
                                          'publication_date': '2025-01-02', 'device_title': 'Синтетичний приклад',
                                          'ipc': ['G06F'], 'claims': '4 канала. Каждый приемный канал содержит АЦП.'}))
            self.assertEqual(main(['import', str(source), '--output', str(output), '--cache', tmp + '/cache']), 0)
            row = json.loads(output.read_text())
            self.assertEqual(row['calculated_component_multiplier']['ADC']['value'], 4)
            self.assertTrue(Path(str(output) + '.audit.json').exists())

    def test_captcha_is_error(self):
        with self.assertRaises(ValueError):
            parse_html('<html><title>Access denied</title></html>')

    def test_pdf_ipc_field_excludes_citations(self):
        p = fixture(ipc=[])
        p.pdf_url = 'https://patentimages.storage.googleapis.com/test.pdf'
        fake_client = SimpleNamespace(get=lambda url: (b'bytes', {'source_url': url}))
        fake_reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: '(51) МПК\nH04B 1/00\n(54) Устройство. Известен патент G01S 3/02')])
        with patch('pypdf.PdfReader', return_value=fake_reader):
            verify_pdf_ipc(p, fake_client)
        self.assertEqual(p.ipc, ['H04B1/00'])

    def test_discovery_prefixes_and_deduplication(self):
        from patent_cli import google_discover
        data = json.dumps({'results': {'total_num_pages': 1, 'total_num_results': 1,
                             'cluster': [{'result': [{'patent': {'publication_number': 'RU9999999C1'}}]}]}}).encode()
        fake_client = SimpleNamespace(get=lambda url: (data, {}))
        audit = []
        urls = google_discover(fake_client, [{'assignee': 'НИЦЭВТ', 'ipc_prefixes': ['G06F']}], 1, 5, audit)
        self.assertEqual(len(urls), 1)
        self.assertEqual(audit[0]['candidate_cpc_prefix'], 'G06F')
        self.assertIsNone(audit[1]['candidate_cpc_prefix'])


if __name__ == '__main__':
    unittest.main()

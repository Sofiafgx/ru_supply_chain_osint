import json
import tempfile
import unittest
from pathlib import Path

from collection_cli import CollectionArchive
from coverage_extension import ingest
from parsers.registry_sources import KadParser
from parsers.source_common import SourceError


class BrowserSnapshotTests(unittest.TestCase):
    def test_kad_current_markup_separates_court_judge_date_and_tooltip(self):
        html = '''<table><tr><td class="num"><div><div class="civil" title="12.01.2026 0:00:00"><span>12.01.2026</span></div>
        <a href="/Card/example">А56-1/2026</a></div></td><td class="court"><div><div class="judge">Example Judge</div><div>Example Court</div></div></td>
        <td class="plaintiff">Example Company<span class="js-rolloverHtml" style="display:none">Example Company Address INN</span></td>
        <td class="respondent">Other Company</td></tr></table>'''
        record = KadParser().parse(html, url='https://kad.arbitr.ru/').records[0]
        self.assertEqual(record.data['registration_date'], '2026-01-12')
        self.assertEqual(record.data['court'], 'Example Court')
        self.assertEqual(record.data['judge'], 'Example Judge')
        self.assertEqual(record.data['plaintiffs'], 'Example Company')

    def test_changed_input_is_not_silently_skipped_and_original_provenance_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); archive = p / 'archive'; file = p / 'cases.json'
            file.write_text(json.dumps([{'CaseId':'one','CaseNumber':'А40-1/2026','Date':'2026-01-01'}]))
            metadata = {'extent':'all_search_results','declared_count':1,'participant_inn':'0012345678'}
            self.assertEqual(ingest(archive,'kad',file,'https://kad.arbitr.ru/',metadata)['parsed'], 1)
            file.write_text(json.dumps([{'CaseId':'two','CaseNumber':'А40-2/2026','Date':'2026-01-02'}]))
            ingest(archive,'kad',file,'https://kad.arbitr.ru/',metadata)
            store = CollectionArchive(archive)
            self.assertEqual(store.counts(), {'kad:case':2})
            for (raw,) in store.db.execute('SELECT json FROM records'):
                record = json.loads(raw)
                self.assertTrue(Path(record['input_file']).exists())
                self.assertTrue(record['input_sha256'])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0], 2)
            store.close()

    def test_missing_results_or_count_mismatch_are_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); file = p / 'cases.json'; file.write_text('[]')
            with self.assertRaises(SourceError):
                ingest(p/'archive','kad',file,'https://kad.arbitr.ru/',{'extent':'all_search_results','declared_count':0})
            file.write_text('[{"CaseNumber":"А40-1/2026"}]')
            with self.assertRaises(SourceError):
                ingest(p/'archive','kad',file,'https://kad.arbitr.ru/',{'extent':'all_search_results','declared_count':2})
            store = CollectionArchive(p/'archive')
            self.assertEqual(store.counts(), {})
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0],2)
            store.close()


if __name__ == '__main__':
    unittest.main()

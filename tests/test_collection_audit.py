import json
import tempfile
import unittest
from pathlib import Path

from collection_cli import CollectionArchive
from collection_audit import audit
from models.source_records import SourceRecord


class AuditTests(unittest.TestCase):
    def fixture(self, directory, duplicate=False):
        plan = {'start_date': '2026-01-01', 'end_date': '2026-01-02', 'queries': ['станок']}
        (Path(directory) / 'plan.json').write_text(json.dumps(plan))
        archive = CollectionArchive(directory)
        for page in (1, 2):
            archive.enqueue('clearspending', 'contracts', {'query': 'станок', 'start': '2026-01-01', 'end': '2026-01-02', 'page': page})
            job = archive.next_job(); archive.raw(job, b'{"data": []}', 'json')
            identifier = '1' if duplicate else str(page)
            archive.commit(job, [SourceRecord(source='clearspending', record_type='contract', record_id=identifier,
                data={'contract_number': identifier, 'title': 'Станок'})], detail={'reported_count': 2, 'total_pages': 2,
                'parsed': 1, 'outside_date_window': 0, 'rejected_rows': 0})
        archive.close()

    def test_complete_pages_and_valid_raw_files_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory)
            result = audit(directory)
            self.assertEqual(result['pagination_issues'], [])
            self.assertEqual(result['raw_file_issues'], [])
            self.assertEqual(result['sqlite_integrity'], 'ok')

    def test_repeated_contract_across_successful_pages_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, duplicate=True)
            result = audit(directory)
            self.assertIn('duplicate_ids_across_pages', [issue['issue'] for issue in result['pagination_issues']])


if __name__ == '__main__':
    unittest.main()

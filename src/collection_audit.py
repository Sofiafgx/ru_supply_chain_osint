"""Offline checks of pagination, date partitions, duplicates and raw SHA-256."""
import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path


def audit(directory, verify_hashes=True):
    directory = Path(directory)
    plan = json.loads((directory / 'plan.json').read_text())
    db = sqlite3.connect(directory / 'collection.sqlite')
    db.row_factory = sqlite3.Row
    groups = defaultdict(dict)
    for row in db.execute("SELECT payload,status,detail FROM jobs WHERE source='clearspending' AND kind='contracts'"):
        payload = json.loads(row['payload'])
        groups[(payload['query'], payload['start'], payload['end'])][payload['page']] = dict(row)
    distinct = {}
    for row in db.execute("""SELECT json_extract(j.payload,'$.query') q,
        json_extract(j.payload,'$.start') s,json_extract(j.payload,'$.end') e,
        COUNT(DISTINCT ev.record_id) n FROM jobs j JOIN evidence ev ON j.id=ev.job_id
        WHERE j.source='clearspending' AND j.kind='contracts' GROUP BY q,s,e"""):
        distinct[(row['q'], row['s'], row['e'])] = row['n']
    issues = []
    intervals = defaultdict(list)
    leaf_count = 0
    for key, pages in groups.items():
        first = pages.get(1)
        if first and first['status'] == 'split':
            continue
        leaf_count += 1
        query, start, end = key
        intervals[query].append((date.fromisoformat(start), date.fromisoformat(end)))
        details = json.loads(first['detail'] or 'null') if first else None
        if not first or first['status'] != 'done' or not isinstance(details, dict):
            issues.append({'query': query, 'start': start, 'end': end, 'issue': 'unfinished_or_capped_window'})
            continue
        total_pages = details['total_pages']
        # Zero-result queries may declare zero pages but still return page 1.
        expected = set(range(1, max(1, total_pages) + 1))
        completed = {page for page, value in pages.items() if value['status'] == 'done'}
        if completed != expected or set(pages) != expected:
            issues.append({'query': query, 'start': start, 'end': end, 'issue': 'missing_or_extra_pages',
                           'expected_pages': sorted(expected), 'completed_pages': sorted(completed)})
            continue
        counts = [json.loads(value['detail']) for value in pages.values()]
        accepted = sum(value['parsed'] for value in counts)
        returned = sum(value['parsed'] + value['outside_date_window'] + value['rejected_rows'] for value in counts)
        if returned != details['reported_count']:
            issues.append({'query': query, 'start': start, 'end': end, 'issue': 'count_mismatch',
                           'declared': details['reported_count'], 'accounted': returned})
        if distinct.get(key, 0) != accepted:
            issues.append({'query': query, 'start': start, 'end': end, 'issue': 'duplicate_ids_across_pages',
                           'parsed': accepted, 'unique': distinct.get(key, 0)})
    for query in plan['queries']:
        next_date = date.fromisoformat(plan['start_date'])
        for start, end in sorted(intervals[query]):
            if start != next_date:
                issues.append({'query': query, 'issue': 'gap_or_overlap_in_date_partition',
                               'expected_start': next_date.isoformat(), 'actual_start': start.isoformat()})
            next_date = end + timedelta(days=1)
        if next_date != date.fromisoformat(plan['end_date']) + timedelta(days=1):
            issues.append({'query': query, 'issue': 'date_partition_not_finished'})
    integrity = db.execute('PRAGMA quick_check').fetchone()[0]
    foreign_keys = [tuple(row) for row in db.execute('PRAGMA foreign_key_check')]
    response_issues = []
    response_count = 0
    for row in db.execute('SELECT job_id,path,sha256 FROM responses'):
        response_count += 1
        path = Path(row['path'])
        if not path.exists():
            response_issues.append({'job_id': row['job_id'], 'issue': 'missing_raw_file'})
        elif verify_hashes and hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            response_issues.append({'job_id': row['job_id'], 'issue': 'sha256_mismatch'})
    counts = {row['source'] + ':' + row['kind']: row['n'] for row in db.execute('SELECT source,kind,COUNT(*) n FROM records GROUP BY source,kind')}
    result = {'audited_at': datetime.now(timezone.utc).isoformat(), 'records': counts,
              'date_windows': leaf_count, 'pagination_issues': issues,
              'raw_responses_checked': response_count, 'hashes_verified': verify_hashes,
              'raw_file_issues': response_issues, 'sqlite_integrity': integrity, 'foreign_key_issues': foreign_keys,
              'note': 'Checks the collected query results, not coverage of the entire source or accuracy of its claims.'}
    result['snapshot'] = {'responses': response_count,
                          'last_response': db.execute('SELECT MAX(fetched_at) FROM responses').fetchone()[0],
                          'last_job_update': db.execute('SELECT MAX(updated_at) FROM jobs').fetchone()[0]}
    db.close()
    (directory / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('data/collection_20261008'))
    parser.add_argument('--skip-hashes', action='store_true')
    args = parser.parse_args()
    result = audit(args.directory, not args.skip_hashes)
    print(json.dumps({key: result[key] for key in ('records', 'date_windows', 'raw_responses_checked', 'sqlite_integrity')}, ensure_ascii=False))
    print(json.dumps({'pagination_issues': len(result['pagination_issues']), 'raw_file_issues': len(result['raw_file_issues']),
                      'foreign_key_issues': len(result['foreign_key_issues'])}))


if __name__ == '__main__':
    main()

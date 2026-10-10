"""Restore procurement identifiers from each contract's original archived response."""
import argparse
import collections
import csv
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from parsers.source_common import text


FIELDS = ('notif_num', 'purchase_code')


def collect(db, archive_directory):
    groups = collections.defaultdict(list)
    for identifier, filename, digest, existing in db.execute("""
        SELECT id, json_extract(json,'$.input_file'),
        json_extract(json,'$.input_sha256'),
        json_object('notif_num',json_extract(json,'$.data.notif_num'),
                    'purchase_code',json_extract(json,'$.data.purchase_code'))
        FROM records WHERE source='clearspending' AND kind='contract'
    """):
        if not filename or not digest:
            raise ValueError(f'Missing provenance for contract {identifier}')
        path = Path(filename)
        if not path.is_absolute():
            path = archive_directory / path
        groups[path].append((identifier, digest, json.loads(existing)))
    restored = {}
    for path, wanted in groups.items():
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if any(digest != actual for _, digest, _ in wanted):
            raise ValueError(f'Archived response checksum mismatch: {path}')
        payload = json.loads(content)
        rows = payload if isinstance(payload, list) else payload.get('data')
        if not isinstance(rows, list):
            raise ValueError(f'Unexpected response shape: {path}')
        index = collections.defaultdict(list)
        for row in rows:
            identifier = str(row.get('regnum') or row.get('regNum') or '')
            index[identifier].append(row)
        for identifier, _, existing in wanted:
            candidates = index.get(identifier, [])
            if not candidates:
                raise ValueError(f'Contract {identifier} absent from its archived response')
            values = {tuple(text(row.get(k)) for k in FIELDS) for row in candidates}
            if len(values) != 1:
                raise ValueError(f'Conflicting archived identifiers for contract {identifier}')
            patch = dict(zip(FIELDS, values.pop()))
            for key in FIELDS:
                if existing.get(key) is not None and existing[key] != patch[key]:
                    raise ValueError(f'Existing {key} conflicts for contract {identifier}')
            restored[identifier] = patch
    return restored, len(groups)


def rewrite_csv(path, restored, backup_directory):
    temporary = path.with_name(path.name + '.identifiers.tmp')
    count = 0
    try:
        with path.open(encoding='utf-8-sig', newline='') as src, temporary.open('w', encoding='utf-8-sig', newline='') as dst:
            reader = csv.DictReader(src)
            columns = list(reader.fieldnames)
            columns.extend(k for k in FIELDS if k not in columns)
            writer = csv.DictWriter(dst, fieldnames=columns)
            writer.writeheader()
            for row in reader:
                identifier = row['contract_number']
                if identifier not in restored:
                    raise ValueError(f'CSV contract outside restored archive: {identifier}')
                for key, value in restored[identifier].items():
                    if row.get(key) and row[key] != (value or ''):
                        raise ValueError(f'CSV field conflict: {identifier}, {key}')
                    row[key] = value
                writer.writerow(row)
                count += 1
        backup_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_directory / path.name)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def run(archive_directory, output_directory, extra_csv=()):
    csv.field_size_limit(sys.maxsize)
    output_directory.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(archive_directory / 'collection.sqlite', timeout=60)
    try:
        restored, raw_count = collect(db, archive_directory)
        # Preserve a recoverable database snapshot before adding fields.
        with sqlite3.connect(output_directory / 'collection.before.sqlite') as backup:
            db.backup(backup)
        with db:
            for identifier, fields in restored.items():
                db.execute("""UPDATE records SET json=json_set(json,
                    '$.data.notif_num', ?, '$.data.purchase_code', ?)
                    WHERE source='clearspending' AND kind='contract' AND id=?""",
                    (fields['notif_num'], fields['purchase_code'], identifier))
        paths = [archive_directory / 'exports' / filename for filename in
                 ('contracts.csv', 'matched_contracts.csv', 'contracts_summary.csv', 'matched_contracts_summary.csv')]
        paths.extend(extra_csv)
        exports = {}
        for i, path in enumerate(dict.fromkeys(p.resolve() for p in paths)):
            if path.exists():
                exports[str(path)] = rewrite_csv(path, restored, output_directory / 'csv_before' / str(i))
        jsonl = archive_directory / 'exports' / 'clearspending.jsonl'
        if jsonl.exists():
            temporary = jsonl.with_name(jsonl.name + '.identifiers.tmp')
            try:
                with temporary.open('w', encoding='utf-8') as handle:
                    for blob, in db.execute("SELECT json FROM records WHERE source='clearspending' ORDER BY kind,id"):
                        handle.write(blob + '\n')
                shutil.copy2(jsonl, output_directory / 'clearspending.before.jsonl')
                temporary.replace(jsonl)
            finally:
                temporary.unlink(missing_ok=True)
        counts = {key: sum(value[key] is not None for value in restored.values()) for key in FIELDS}
        missing_counts = {key: db.execute("""SELECT count(*) FROM records
            WHERE source='clearspending' AND kind='contract'
            AND json_array_length(json_extract(json,'$.data.supplier_inns'))=0
            AND json_extract(json, ?) IS NOT NULL""", ('$.data.' + key,)).fetchone()[0] for key in FIELDS}
        audit = dict(completed_at=datetime.now(timezone.utc).isoformat(),
                     contracts=len(restored), raw_files_verified=raw_count,
                     filled_identifiers=counts, filled_for_missing_supplier=missing_counts,
                     csv_rows=exports, database_check=db.execute('PRAGMA quick_check').fetchone()[0],
                     scope='Only notif_num and purchase_code restored; supplier fields unchanged; no online requests',
                     backup_directory=str(output_directory.resolve()))
        (output_directory / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
        return audit
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=Path('data/collection_20261008'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--extra-csv', type=Path, nargs='*', default=[])
    args = parser.parse_args()
    print(json.dumps(run(args.archive, args.output, args.extra_csv), ensure_ascii=False, indent=2))

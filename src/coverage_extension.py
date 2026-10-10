"""Додаткові джерела: імпорт знімків браузера та авторизованих вивантажень."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from collection_cli import CollectionArchive, now
from parsers.source_common import SourceError
from sources_cli import PARSERS, validate_url


def ingest(directory, source, path, url, metadata=None):
    validate_url(source, url)
    path = Path(path)
    content = path.read_bytes()
    metadata = metadata or {}
    archive = CollectionArchive(directory)
    payload = {**metadata, "url": url, "input_name": path.name, "sha256": hashlib.sha256(content).hexdigest()}
    identifier = archive.enqueue(source, "authorized_input", payload)
    job = archive.db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
    if job["status"] == "done":
        archive.close()
        return {"status": "already_ingested"}
    try:
        result = PARSERS[source]().parse(content.decode("utf-8-sig"), path.suffix.lstrip('.'), url)
        if result.rejected_rows:
            raise SourceError(f"Нерозпізнаних рядків: {result.rejected_rows}; перевірте формат")
        if not result.records and not (source == "kad" and metadata.get("declared_count") == 0 and metadata.get("empty_search_confirmed") is True):
            raise SourceError("Записів немає; порожній або закритий документ не підтверджує відсутності даних")
        if source == "kad":
            for record in result.records:
                record.data["search_participant_inn"] = metadata.get("participant_inn")
                record.data["search_period_from"] = metadata.get("date_from")
                record.data["search_period_to"] = metadata.get("date_to")
                record.warnings.append("Участь компанії встановлена пошуком КАД; роль може бути третьою особою, а не позивачем/відповідачем")
            declared = metadata.get("declared_count")
            if declared is not None and metadata.get("extent") == "all_search_results" and declared != len(result.records):
                raise SourceError("Кількість у знімку не збігається із заявленим повним результатом")
        archive.raw(job, content, path.suffix.lstrip('.'))
        detail = {"parsed": len(result.records), "warnings": result.warnings, "metadata": metadata}
        archive.commit(job, result.records, status="done", detail=detail)
        archive.source_status(source, "partial", "Зібрано надану вибірку; охоплення всіх цільових компаній та всього джерела не встановлене")
        export_extension(archive)
        return {"source": source, "parsed": len(result.records), "total": archive.counts()}
    except Exception as exc:
        archive.commit(job, status="failed", detail={"error": str(exc)})
        raise
    finally:
        archive.close()


def export_extension(archive):
    destination = archive.directory / "exports"
    destination.mkdir(exist_ok=True)
    for source in PARSERS:
        rows = [json.loads(row[0]) for row in archive.db.execute("SELECT json FROM records WHERE source=? ORDER BY kind,id", (source,))]
        if not rows:
            continue
        (destination / (source + ".jsonl")).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        fields = sorted({key for row in rows for key in row['data']}) + ['record_id', 'source_url', 'warnings', 'input_file', 'input_sha256']
        with (destination / (source + ".csv")).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
            for row in rows:
                values = {**row['data'], **{key: row.get(key) for key in fields if key not in row['data']}}
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in values.items()})
    report = {'updated_at': now(), 'records': archive.counts(), 'sources': [dict(row) for row in archive.db.execute('SELECT * FROM source_status')],
              'jobs': [dict(row) for row in archive.db.execute('SELECT source,kind,status,detail,payload FROM jobs')],
              'note': 'Additional source collection. Does not replace or relabel the main archive.'}
    (archive.directory / 'coverage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('data/extension_20261008'))
    parser.add_argument('--source', choices=PARSERS, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--metadata', type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text()) if args.metadata else {}
    print(json.dumps(ingest(args.directory, args.source, args.input, args.url, metadata), ensure_ascii=False))


if __name__ == '__main__':
    main()

"""Rebuild the dated follow-up dataset from preserved, auditable snapshots."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

from parsers.industrial_labor import parse_notices


def build():
    base = Path(__file__).resolve().parents[1]
    prior = base / "data/industrial_20261009"
    output = base / "data/industrial_20261009_followup"
    raw = output / "raw"
    notices = [
        ("joblab", raw / "joblab_250.html", "https://joblab.ru/vacancy/13436122"),
        ("hh", raw / "hh_card.txt", "https://hh.ru/vacancy/136562166"),
        ("rostender", raw / "rostender_250.html", "https://rostender.info/category/tendery-frezernye-stanki"),
    ]
    for path in sorted(raw.glob("hh_target_*.html")):
        notices.append(("hh", path, "https://hh.ru/vacancy/" + path.stem.rsplit("_", 1)[1]))
    records, evidence, audit, rejected = {}, {}, [], Counter()
    for source, path, url in notices:
        if not path.exists():
            audit.append({"source": source, "url": url, "error": "snapshot_missing"})
            continue
        content = path.read_text()
        result = parse_notices(content, source, url, "html")
        rejected.update(result["rejected"])
        audit.append({"source": source, "url": url, "file": str(path),
                      "notices_examined": result["notices_examined"], "matched": len(result["records"]), "rejected": result["rejected"]})
        for record, proof in zip(result["records"], result["evidence"]):
            records[record["source_url"]] = record
            proof.update({"input_file": str(path), "input_sha256": hashlib.sha256(content.encode()).hexdigest(),
                          "collected_at": result["collected_at"]})
            evidence[record["source_url"]] = proof
    old_records = {r["source_url"]: r for r in json.loads((prior / "records.json").read_text())}
    old_evidence = {r["source_url"]: r for r in json.loads((prior / "records.evidence.json").read_text())}
    old_records.update(records)
    old_evidence.update(evidence)
    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    save("new_records.json", list(records.values()))
    save("new_records.evidence.json", list(evidence.values()))
    save("records.json", list(old_records.values()))
    save("records.evidence.json", list(old_evidence.values()))
    report = {"new_records": len(records), "combined_records": len(old_records),
              "new_by_source": dict(Counter(x["source"] for x in evidence.values())),
              "rejected": dict(rejected), "audit": audit,
              "publication_dates": sum(bool(r["posting_date"]) for r in old_records.values()),
              "closure_dates": sum(bool(r["closure_date"]) for r in old_records.values()),
              "company_tax_ids": sum(bool(r["company_tax_id"]) for r in old_records.values()),
              "coverage": "bounded follow-up, not exhaustive"}
    save("parse_report.json", report)
    # The park operator's INN is context, not an inferred employer/customer INN.
    park = raw / "indparks_250.html"
    if park.exists():
        soup = BeautifulSoup(park.read_text(), "html.parser")
        body = soup.get_text(" ", strip=True)
        import re
        match = re.search(r"ИНН управляющей компании\s+(\d{10})", body)
        save("company_context.json", [{"source_url": "https://indparks.ru/catalog/oez-alabuga/",
                                       "profile_title": (soup.h1 or soup.title).get_text(" ", strip=True) if (soup.h1 or soup.title) else None,
                                       "operator_tax_id": match[1] if match else None,
                                       "role": "industrial_park_operator; not vacancy_employer",
                                       "input_file": str(park), "input_sha256": hashlib.sha256(park.read_bytes()).hexdigest()}])
    print(json.dumps({k: report[k] for k in ("new_records", "combined_records", "new_by_source", "closure_dates")}, ensure_ascii=False))


if __name__ == "__main__":
    build()

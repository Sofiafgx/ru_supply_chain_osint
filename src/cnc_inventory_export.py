"""Export one row per evidenced CNC brand/model mention, without estimating TTR."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from parsers.source_common import inn

TARGET_BRANDS = {"DMG Mori", "Haas", "Fanuc", "Sodick", "Siemens"}
TARGET_SOURCES = {"rostender", "hh", "joblab", "cznelb", "trudvsem"}
FIELDS = ("company_inn", "cnc_brand", "cnc_model", "source_url")


def export_mentions(records, evidence):
    proofs = {p["source_url"]: p for p in evidence}
    rows, details, seen = [], [], set()
    for record in records:
        proof = proofs.get(record["source_url"])
        if proof is None:
            raise ValueError(f'Missing evidence for {record["source_url"]}')
        if proof["source"] not in TARGET_SOURCES:
            continue
        for mention in proof["equipment_mentions"]:
            if mention["brand"] not in TARGET_BRANDS:
                continue
            key = (record["source_url"], mention["brand"], mention["model"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({"company_inn": inn(record.get("company_tax_id")),
                         "cnc_brand": mention["brand"], "cnc_model": mention["model"],
                         "source_url": record["source_url"]})
            details.append({"row_number": len(rows), "source": proof["source"],
                            "company_name": proof.get("company_name"),
                            "posting_date": record.get("posting_date"),
                            "equipment_type": mention["equipment_type"],
                            "raw_mention": mention["raw_mention"], "context": mention["context"],
                            "verification_scope": proof.get("verification_scope", "individual_notice"),
                            "input_file": proof.get("input_file"),
                            "input_sha256": proof.get("input_sha256"),
                            "warnings": proof.get("warnings", []),
                            "ttr_days": None})
    return rows, details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True, help="Output JSON; also writes CSV and evidence")
    args = parser.parse_args()
    records, proofs = Path(args.records), Path(args.evidence)
    rows, details = export_mentions(json.loads(records.read_text()), json.loads(proofs.read_text()))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    output.with_suffix(".evidence.json").write_text(json.dumps({
        "input_records": str(records.resolve()),
        "input_records_sha256": hashlib.sha256(records.read_bytes()).hexdigest(),
        "input_evidence": str(proofs.resolve()),
        "input_evidence_sha256": hashlib.sha256(proofs.read_bytes()).hexdigest(),
        "records": details}, ensure_ascii=False, indent=2) + "\n")
    with output.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"mentions": len(rows), "notices": len({r["source_url"] for r in rows}),
                      "with_inn": sum(r["company_inn"] is not None for r in rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

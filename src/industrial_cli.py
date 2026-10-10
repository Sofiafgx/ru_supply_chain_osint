"""Collect individual public notices or parse preserved HTML/JSON snapshots."""
import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from parsers.industrial_labor import parse_notices
from parsers.source_common import SourceClient, SourceError
from parsers.industrial_transport import RotatingSourceClient

SOURCES = ("fkc", "hh", "joblab", "cznelb", "rostender", "trudvsem", "indparks")


async def run(args):
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = output.parent / (output.stem + "_raw")
    records, proofs, audit, rejected = {}, {}, [], Counter()

    def parse(content, source_url, format, input_file):
        try:
            result = parse_notices(content, args.source, source_url, format)
            rejected.update(result["rejected"])
            for record, proof in zip(result["records"], result["evidence"]):
                url = record["source_url"]
                records[url] = record
                proof.update({"input_file": str(Path(input_file).resolve()),
                              "input_sha256": hashlib.sha256(content.encode()).hexdigest()})
                proofs[url] = proof
            audit.append({"url": source_url, "input_file": str(input_file),
                          "notices_examined": result["notices_examined"], "records": len(result["records"])})
        except (SourceError, ValueError, TypeError, KeyError) as exc:
            audit.append({"url": source_url, "input_file": str(input_file), "error": str(exc)})

    for path in args.input:
        p = Path(path)
        parse(p.read_text(encoding="utf-8-sig"), args.source_url,
              "html" if p.suffix in (".html", ".htm") else "jsonl" if p.suffix == ".jsonl" else "json", p)

    transport = RotatingSourceClient(args.proxy_file, offset=args.proxy_offset, attempts=args.proxy_attempts,
                                    interval=args.interval) if args.proxy_file else SourceClient(interval=args.interval, timeout=20, retries=1)
    async with transport as client:
        async def fetch(url, method="GET", **kwargs):
            raw.mkdir(parents=True, exist_ok=True)
            try:
                response = await client.request(method, url, **kwargs)
                suffix = "json" if "json" in response.headers.get("content-type", "") else "html"
                path = raw / (hashlib.sha256(str(response.url).encode()).hexdigest()[:20] + "." + suffix)
                path.write_text(response.text, encoding="utf-8")
                return response, path, suffix
            except SourceError as exc:
                audit.append({"url": url, "error": str(exc)})
                return None

        for url in args.url:
            response = await fetch(url)
            if response:
                r, path, suffix = response
                parse(r.text, str(r.url), suffix, path)

        if args.discover and args.source == "fkc":
            response = await fetch("https://fkc-opk.ru/PublicResumeUser/AjAllGetVacancysList", "POST", json={})
            if response:
                r, path, _ = response
                rows = r.json().get("data", [])
                selected = [x for x in rows if re.search(r"ЧПУ|станк|мехобработ|фрезер|токар|наладчик", str(x[0]), re.I)]
                audit.append({"listing_url": str(r.url), "listing_file": str(path), "total_listed": len(rows),
                              "selected_by_title": len(selected), "limit": args.limit})
                for row in selected[:args.limit]:
                    url = f"https://fkc-opk.ru/PublicResumeUser/AJGetVacancyMoreDetailed?id={row[7]}"
                    response = await fetch(url)
                    if not response:
                        break
                    r, path, suffix = response
                    parse(r.text, str(r.url), suffix, path)

        if args.discover and args.source == "hh":
            remaining = args.limit
            for query in args.query or ["ЧПУ Haas", "ЧПУ DMG Mori", "ЧПУ Fanuc", "ЧПУ Siemens", "ЧПУ Doosan"]:
                page = 0
                while remaining > 0:
                    response = await fetch("https://api.hh.ru/vacancies", params={"text": query, "per_page": min(100, remaining), "page": page})
                    if not response:
                        remaining = 0
                        break
                    r, path, _ = response
                    payload = r.json()
                    audit.append({"listing_url": str(r.url), "listing_file": str(path), "found": payload.get("found"), "pages": payload.get("pages"), "page": page})
                    for item in payload.get("items", []):
                        response = await fetch(f"https://api.hh.ru/vacancies/{item['id']}")
                        if not response:
                            remaining = 0
                            break
                        r, path, suffix = response
                        parse(r.text, str(r.url), suffix, path)
                        remaining -= 1
                    page += 1
                    if page >= payload.get("pages", 0) or not payload.get("items"):
                        break

    if records or not any("error" in entry for entry in audit):
        output.write_text(json.dumps(list(records.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.with_suffix(".evidence.json").write_text(json.dumps(list(proofs.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"source": args.source, "collected_at": datetime.now(timezone.utc).isoformat(),
              "records": len(records), "rejected": dict(rejected), "audit": audit,
              "coverage": "bounded_sample; not complete historical coverage"}
    if args.proxy_file:
        report["transport_audit"] = transport.audit
    output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"records": len(records), "output": str(output.resolve()), "errors": sum("error" in x for x in audit)}, ensure_ascii=False))
    return 0 if records else 1 if any("error" in x for x in audit) else 2


def main():
    p = argparse.ArgumentParser(description="Вакансії ЧПУ та оголошення про ремонт обладнання")
    p.add_argument("source", choices=SOURCES)
    p.add_argument("--input", action="append", default=[])
    p.add_argument("--url", action="append", default=[])
    p.add_argument("--source-url", help="Пряме посилання на локально збережене оголошення")
    p.add_argument("--discover", action="store_true", help="Обмежений пошук через публічний список ФКЦ або API HH")
    p.add_argument("--query", action="append", help="Пошукові запити HH")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--interval", type=float, default=1)
    p.add_argument("--proxy-file", help="Explicitly enable bounded proxy rotation")
    p.add_argument("--proxy-offset", type=int, default=0)
    p.add_argument("--proxy-attempts", type=int, default=4)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    if not args.input and not args.url and not args.discover:
        p.error("Передайте --input, --url або --discover")
    if args.discover and args.source not in ("fkc", "hh"):
        p.error("Для цього джерела передайте прямі --url або локальні --input")
    if args.limit < 1 or args.interval < 0 or args.proxy_attempts < 1 or args.proxy_offset < 0:
        p.error("--limit має бути > 0; --interval має бути >= 0")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

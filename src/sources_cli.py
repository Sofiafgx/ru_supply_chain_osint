"""Єдиний запуск восьми джерел: python sources_cli.py --help."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from dotenv import load_dotenv

from parsers.c4ads_parser import C4ADSParser
from parsers.registry_sources import CheckoParser, ClearSpendingParser, KadParser, ZakupkiParser
from parsers.source_common import SourceClient, SourceError, read_input
from parsers.trade_sources import ImportGeniusParser, PanjivaParser, Wmb52Parser


PARSERS = {"zakupki": ZakupkiParser, "checko": CheckoParser, "clearspending": ClearSpendingParser,
           "kad": KadParser, "importgenius": ImportGeniusParser, "panjiva": PanjivaParser,
           "52wmb": Wmb52Parser, "c4ads": C4ADSParser}
DOMAINS = {"zakupki": "zakupki.gov.ru", "checko": "checko.ru", "clearspending": "clearspending.ru",
           "kad": "kad.arbitr.ru", "importgenius": "importgenius.com", "panjiva": "panjiva.com",
           "52wmb": "52wmb.com", "c4ads": "c4ads.org"}


def validate_url(source, url):
    parsed = urlparse(url)
    domain = DOMAINS[source]
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == domain or parsed.hostname.endswith("." + domain)
    ) or parsed.username or parsed.password:
        raise SourceError(f"Очікується HTTPS URL джерела {domain}")
    if any(name.lower() in ("key", "apikey", "api_key", "token") for name, _ in parse_qsl(parsed.query)):
        raise SourceError("API-ключі передавайте через .env, а не через URL")


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Парсери реєстрів, торговельних баз і досліджень C4ADS")
    parser.add_argument("source", choices=PARSERS)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="Локальний HTML/CSV/JSON/JSONL/XML у UTF-8")
    mode.add_argument("--url", help="Адреса доступної сторінки або JSON API")
    mode.add_argument("--query", help="Пошук: zakupki, clearspending, kad, c4ads; порожній рядок — каталог C4ADS")
    mode.add_argument("--inn", help="ІПН для Checko API (CHECKO_API_KEY)")
    parser.add_argument("--format", choices=("html", "csv", "json", "jsonl", "xml"), help="Перевизначити формат")
    parser.add_argument("--source-url", help="Початкова адреса для походження локального файлу")
    parser.add_argument("--output", type=Path, help="JSONL; за замовчуванням data/sources/<source>.jsonl")
    parser.add_argument("--pages", type=int, default=1, help="Максимум сторінок пошуку (1–100)")
    parser.add_argument("--proxy", help="Один власний HTTP/SOCKS-проксі; також OSINT_PROXY")
    parser.add_argument("--column-map", type=Path, help="JSON: канонічне поле -> колонка торговельного CSV")
    parser.add_argument("--hs-prefix", action="append", default=[], help="Фільтр HS, можна повторити; тільки торговельні джерела")
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 100:
        parser.error("--pages має бути від 1 до 100")
    trade = args.source in ("importgenius", "panjiva", "52wmb")
    if (args.column_map or args.hs_prefix) and not trade:
        parser.error("--column-map і --hs-prefix доступні лише для торговельних джерел")
    if args.inn is not None and args.source != "checko":
        parser.error("--inn доступний лише для Checko")
    if args.query is not None and args.source not in ("zakupki", "clearspending", "kad", "c4ads"):
        parser.error("Це джерело потребує --url або --input")
    if args.query == "" and args.source != "c4ads":
        parser.error("Пошуковий запит порожній")
    if args.pages != 1 and args.query is None:
        parser.error("--pages застосовується до --query")
    if any(not value.isdigit() or not 2 <= len(value) <= 10 for value in args.hs_prefix):
        parser.error("HS-префікс має містити 2–10 цифр")
    return args


async def collect(args):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    kwargs = {}
    if args.source in ("importgenius", "panjiva", "52wmb"):
        kwargs = {"hs_prefixes": args.hs_prefix,
                  "column_map": json.loads(args.column_map.read_text()) if args.column_map else None}
    parser = PARSERS[args.source](**kwargs)
    if args.source_url:
        validate_url(args.source, args.source_url)
    if args.input:
        content, format = read_input(args.input)
        result = parser.parse(content, args.format or format, args.source_url)
        digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
        for record in result.records:
            record.input_file = str(args.input.resolve())
            record.input_sha256 = digest
        return result
    async with SourceClient(proxy=args.proxy or os.getenv("OSINT_PROXY") or None,
                            interval=10 if args.source == "c4ads" else 1) as client:
        if args.inn is not None:
            return await parser.fetch_company(client, args.inn, os.getenv("CHECKO_API_KEY", ""))
        if args.query is not None:
            return await parser.search(client, args.query, args.pages)
        validate_url(args.source, args.url)
        params = {}
        if args.source == "clearspending" and urlparse(args.url).hostname == "newapi.clearspending.ru":
            api_key = os.getenv("CLEARSPENDING_API_KEY", "")
            if not api_key:
                raise SourceError("Задайте CLEARSPENDING_API_KEY для офіційного API")
            params["apikey"] = api_key
        response = await client.request("GET", args.url, **({"params": params} if params else {}))
        format = args.format or ("json" if "json" in response.headers.get("content-type", "") else "html")
        return parser.parse(response.text, format, args.url)


def save_result(result, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            for record in result.records:
                handle.write(record.model_dump_json() + "\n")
        temporary.replace(output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    report_path = output.with_suffix(output.suffix + ".report.json")
    report_path.write_text(json.dumps({"source": result.source, "records": len(result.records),
        "rejected_rows": result.rejected_rows, "warnings": result.warnings}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    args = arguments(argv)
    output = args.output or Path("data/sources") / (args.source + ".jsonl")
    try:
        if args.input and args.input.resolve() in (output.resolve(), output.with_suffix(output.suffix + ".report.json").resolve()):
            raise SourceError("Вхідний і вихідний файли мають відрізнятися")
        result = asyncio.run(collect(args))
        save_result(result, output)
    except (SourceError, ValueError, OSError) as exc:
        print(f"Помилка: {exc}", file=sys.stderr)
        return 1
    print(f"{result.source}: {len(result.records)} записів, відхилено {result.rejected_rows}; {output}")
    for warning in result.warnings:
        print(f"Увага: {warning}", file=sys.stderr)
    return 0 if result.records else 2


if __name__ == "__main__":
    raise SystemExit(main())

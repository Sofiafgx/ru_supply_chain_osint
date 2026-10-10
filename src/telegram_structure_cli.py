"""Нормалізація наявного Telegram-архіву без мережі й авторизації."""
import argparse
import json
from pathlib import Path

from analytics.telegram_structure import StructuredArchive

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Витяг дат, суб'єктів і обладнання з Telegram")
    parser.add_argument("--source", type=Path, action="append", help="SQLite веб- або MTProto-архів; можна повторювати")
    parser.add_argument("--output", type=Path, default=ROOT / "data/telegram_structured")
    args = parser.parse_args()
    sources = args.source or [ROOT / "data/telegram_public/archive.sqlite3"]
    if any(not source.is_file() for source in sources):
        parser.error("Архів джерела ще не створено")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = StructuredArchive(args.output / "structured.sqlite3")
    try:
        runs = [archive.process(source) for source in sources]
        summary = archive.export(args.output)
        print(json.dumps({"runs": runs, **{key: summary[key] for key in ("analyzed_posts", "industrial_posts", "event_candidates", "entity_mentions", "date_mentions")}}, ensure_ascii=False))
    finally:
        archive.close()


if __name__ == "__main__":
    main()

"""Окремий CLI; наявний main.py не змінюється."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Значення має бути більше нуля")
    return number


def arguments():
    parser = argparse.ArgumentParser(description="Архів повідомлень Telegram: SQLite, JSONL, CSV")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources", help="Показати всі задані адреси")
    collect = commands.add_parser("collect", help="Зібрати всю доступну історію або нові повідомлення")
    collect.add_argument("--channels-file", type=Path, default=ROOT / 'config/telegram_channels.txt')
    collect.add_argument("--limit", type=positive_integer, help="Ліміт на джерело за цей запуск; типово без ліміту")
    collect.add_argument("--rescan", action="store_true", help="Перечитати архів для оновлення старих повідомлень")
    collect.add_argument("--download-media", action="store_true", help="Також завантажити фото, відео, аудіо, документи")
    collect.add_argument("--collect-comments", action="store_true", help="Також зібрати доступні коментарі до архівованих дописів")
    collect.add_argument("--media-dir", type=Path, default=ROOT / "data/telegram/media")
    collect.add_argument("--export-format", choices=["jsonl", "csv"], help="Експортувати архів після збору")
    comments = commands.add_parser("comments", help="Зібрати коментарі до вже архівованих дописів через MTProto")
    comments.add_argument("--channels-file", type=Path, default=ROOT / 'config/telegram_channels.txt')
    comments.add_argument("--rescan", action="store_true", help="Перечитати коментарі для оновлення тексту та медіа")
    comments.add_argument("--download-media", action="store_true")
    comments.add_argument("--media-dir", type=Path, default=ROOT / "data/telegram/media")
    comments.add_argument("--export-format", choices=["jsonl", "csv"], default="jsonl")
    export = commands.add_parser("export", help="Експорт локального архіву без входу в Telegram")
    export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export.add_argument("--output", type=Path, required=True)
    commands.add_parser("status", help="Стан джерел та кількість збережених повідомлень")
    for command in (collect, comments, export, commands.choices["status"]):
        command.add_argument("--db", type=Path, default=ROOT / "data/telegram/archive.sqlite3")
    return parser.parse_args()


async def collect_archive(args, sources, archive, credentials):
    from telethon import TelegramClient
    from parsers.telegram_parser import TelegramParser

    api_id, api_hash, phone, session = credentials
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), api_id, api_hash, flood_sleep_threshold=0)
    try:
        # Номер, код і пароль 2FA вводяться лише локально у терміналі.
        if phone:
            await client.start(phone=phone)
        else:
            await client.start()
        if getattr(await client.get_me(), "bot", False):
            raise ValueError("Потрібна сесія користувача Telegram, а не BotFather-токен")
        collector = TelegramParser(
            client, archive, media_dir=args.media_dir if args.download_media else None,
        )
        failures = 0
        if args.command == "collect":
            failures = await collector.collect(sources, limit=args.limit, rescan=args.rescan)
        if args.command == "comments" or getattr(args, "collect_comments", False):
            from parsers.telegram_comments_parser import TelegramCommentParser
            comment_collector = TelegramCommentParser(client, archive, media_dir=args.media_dir if args.download_media else None)
            failures += await comment_collector.collect(sources, rescan=args.rescan)
            if args.export_format:
                comment_collector.comments.export(args.db.parent / f"comments.{args.export_format}", args.export_format)
        return failures
    finally:
        await client.disconnect()


def credentials_from_environment():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id.isdigit() or int(api_id) < 1 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise ValueError(
            "Заповніть TELEGRAM_API_ID і TELEGRAM_API_HASH у .env за прикладом .env.example. "
            "Реквізити: https://my.telegram.org/apps. Код входу вводьте лише в локальному терміналі."
        )
    session = Path(os.getenv("TELEGRAM_SESSION") or "data/telegram/account").expanduser()
    if not session.is_absolute():
        session = ROOT / session
    return int(api_id), api_hash, os.getenv("TELEGRAM_PHONE") or None, session


def main():
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        from parsers.telegram_parser import TelegramArchive, read_sources

        if args.command == "sources":
            sources = read_sources(ROOT / 'config/telegram_channels.txt')
            print("\n".join("@" + name for name in sources))
            print(f"Джерел: {len(sources)}")
            return 0
        credentials = credentials_from_environment() if args.command in ("collect", "comments") else None
        sources = read_sources(args.channels_file) if args.command in ("collect", "comments") else None
        if args.command != "collect" and not args.db.is_file():
            raise ValueError(f"Архів ще не створено: {args.db}. Спочатку виконайте collect.")
        archive = TelegramArchive(args.db)
        try:
            if args.command in ("collect", "comments"):
                failures = asyncio.run(collect_archive(args, sources, archive, credentials))
                if args.export_format and args.command == "collect":
                    target = args.db.parent / f"posts.{args.export_format}"
                    total = archive.export(target, args.export_format)
                    print(f"Експортовано {total} повідомлень: {target}")
                print(json.dumps(archive.report(), ensure_ascii=False, indent=2))
                return 1 if failures else 0
            if args.command == "export":
                total = archive.export(args.output, args.format)
                print(f"Експортовано {total} повідомлень: {args.output.resolve()}")
            else:
                print(json.dumps(archive.report(), ensure_ascii=False, indent=2))
        finally:
            archive.close()
    except ImportError as error:
        print(f"Немає залежності: {error}. Встановіть requirements.txt або telethon python-dotenv.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nЗбір зупинено. Збережені повідомлення залишились; наступний запуск продовжить збір.", file=sys.stderr)
        return 130
    except Exception as error:
        logging.error("%s: %s", type(error).__name__, error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

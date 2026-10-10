"""Збір публічних вебпрев'ю без API-ключів і входу в акаунт."""
import argparse
import asyncio
import logging
import signal
from pathlib import Path

import httpx

from parsers.telegram_parser import read_sources
from parsers.telegram_public_parser import PublicArchive, PublicTelegramParser

ROOT = Path(__file__).resolve().parents[1]


def load_sources(args):
    sources = read_sources(args.channels_file)
    if args.related_channels_file and args.related_channels_file.is_file():
        # Нові перевірені канали отримують пріоритет, початкові адреси зберігаються.
        combined = read_sources(args.related_channels_file) + sources
        sources = list({name.lower(): name for name in combined}.values())
    if args.priority_channels_file and args.priority_channels_file.is_file():
        available = {name.lower(): name for name in sources}
        priority = [available[name.lower()] for name in read_sources(args.priority_channels_file)
                    if name.lower() in available]
        combined = priority + sources
        sources = list({name.lower(): name for name in combined}.values())
    return sources


async def run(args):
    sources = load_sources(args)
    archive = PublicArchive(args.output / "archive.sqlite3")
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, task.cancel)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            collector = PublicTelegramParser(client, archive)
            await collector.collect(sources, args.output, max_rounds=args.max_rounds)
    except asyncio.CancelledError:
        logging.info("Збір зупинено; архів і курсори збережено")
    finally:
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="Telegram: публічний вебархів без авторизації")
    parser.add_argument("--channels-file", type=Path, default=ROOT / 'config/telegram_channels.txt')
    parser.add_argument("--related-channels-file", type=Path, default=ROOT / 'config/telegram_channels_related.txt')
    parser.add_argument("--priority-channels-file", type=Path, default=ROOT / 'config/telegram_channels_priority.txt')
    parser.add_argument("--output", type=Path, default=ROOT / "data/telegram_public")
    parser.add_argument("--max-rounds", type=int, help="Обмежити кількість проходів для короткої перевірки")
    args = parser.parse_args()
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error("--max-rounds має бути більше нуля")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

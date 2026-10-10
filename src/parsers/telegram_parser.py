"""Збір історії Telegram та відновлюваний локальний архів."""
from __future__ import annotations

import asyncio
import base64
import csv
import json
import logging
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, types, utils

LOG = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Не підтримується серіалізація {type(value).__name__}")


def as_json(value):
    return json.dumps(value, ensure_ascii=False, default=json_default)


def read_sources(path):
    sources = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        username = line.split("#", 1)[0].strip().lstrip("@")
        if not username:
            continue
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", username):
            raise ValueError(f"Некоректна адреса Telegram: {username!r}")
        if username.lower() not in seen:
            seen.add(username.lower())
            sources.append(username)
    if not sources:
        raise ValueError("Список джерел порожній")
    return sources


class TelegramArchive:
    """Курсор і повідомлення фіксуються в одній транзакції."""

    def __init__(self, path):
        path = Path(path)
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                username TEXT,
                title TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sources (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                channel_id INTEGER REFERENCES channels(channel_id),
                status TEXT NOT NULL,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                channel_id INTEGER NOT NULL REFERENCES channels(channel_id),
                message_id INTEGER NOT NULL,
                date TEXT,
                text TEXT NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY (channel_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS messages_date ON messages(date);
        """)

    def close(self):
        self.connection.close()

    def register(self, channel_id, username, title):
        with self.connection:
            self.connection.execute("""
                INSERT INTO channels(channel_id, username, title) VALUES (?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    username=excluded.username, title=excluded.title
            """, (channel_id, username, title))

    def status(self, username, status, channel_id=None, error=None):
        with self.connection:
            self.connection.execute("""
                INSERT INTO sources VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    channel_id=excluded.channel_id, status=excluded.status,
                    last_error=excluded.last_error, updated_at=excluded.updated_at
            """, (username, channel_id, status, error, utc_now()))

    def cursor(self, channel_id):
        row = self.connection.execute(
            "SELECT last_message_id FROM channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        return row[0] if row else 0

    def save(self, record):
        with self.connection:
            self.connection.execute("""
                INSERT INTO messages VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, message_id) DO UPDATE SET
                    date=excluded.date, text=excluded.text, record_json=excluded.record_json
            """, (record["channel_id"], record["message_id"], record["date"],
                  record["text"], as_json(record)))
            self.connection.execute("""
                UPDATE channels SET last_message_id=MAX(last_message_id, ?)
                WHERE channel_id=?
            """, (record["message_id"], record["channel_id"]))

    def record(self, channel_id, message_id):
        row = self.connection.execute(
            "SELECT record_json FROM messages WHERE channel_id=? AND message_id=?",
            (channel_id, message_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def report(self):
        rows = self.connection.execute("""
            SELECT s.username, s.status, s.last_error, s.updated_at,
                   c.title, c.last_message_id,
                   (SELECT COUNT(*) FROM messages m WHERE m.channel_id=s.channel_id) AS posts
            FROM sources s LEFT JOIN channels c USING(channel_id)
            ORDER BY s.username COLLATE NOCASE
        """)
        return [dict(row) for row in rows]

    def export(self, destination, format):
        """Потоковий експорт; попередній файл замінюється лише після успіху."""
        destination = Path(destination)
        if format not in {"jsonl", "csv"}:
            raise ValueError("Формат експорту має бути jsonl або csv")
        if destination.resolve() in {self.path, Path(str(self.path) + "-wal"),
                                      Path(str(self.path) + "-shm")}:
            raise ValueError("Файл експорту не може замінювати базу даних або її службові файли")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fields = ["channel_id", "channel_username", "channel_title", "message_id",
                  "date", "edit_date", "text", "url", "views", "forwards",
                  "reply_count", "grouped_id", "is_service", "links", "media",
                  "media_path", "reactions", "reply_to", "forward", "collected_at"]
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig" if format == "csv" else "utf-8",
                newline="", dir=destination.parent, delete=False,
            ) as output:
                temp_path = Path(output.name)
                writer = csv.DictWriter(output, fieldnames=fields) if format == "csv" else None
                if writer:
                    writer.writeheader()
                count = 0
                for row in self.connection.execute(
                    "SELECT record_json FROM messages ORDER BY channel_id, message_id"
                ):
                    if writer:
                        record = json.loads(row[0])
                        flat = {key: record.get(key) for key in fields}
                        for key, value in flat.items():
                            if isinstance(value, (dict, list)):
                                flat[key] = as_json(value)
                            elif isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                                # Telegram-текст не повинен виконуватись як формула в Excel.
                                flat[key] = "'" + value
                        writer.writerow(flat)
                    else:
                        output.write(row[0] + "\n")
                    count += 1
            temp_path.replace(destination)
            return count
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def message_record(message, entity):
    raw = message.to_dict()
    username = getattr(entity, "username", None)
    channel_id = utils.get_peer_id(entity)
    text = getattr(message, "message", None) or ""
    links = re.findall(r"https?://[^\s<>]+", text)
    for item in getattr(message, "entities", None) or []:
        if getattr(item, "url", None):
            links.append(item.url)
    file = getattr(message, "file", None)
    media = None
    if getattr(message, "media", None):
        attachment = getattr(message, "photo", None) or getattr(message, "document", None)
        media = {"type": type(message.media).__name__,
                 "id": getattr(attachment, "id", None),
                 "name": getattr(file, "name", None),
                 "mime_type": getattr(file, "mime_type", None),
                 "size": getattr(file, "size", None)}
    return {
        "channel_id": channel_id,
        "channel_username": username,
        "channel_title": entity.title,
        "message_id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "edit_date": message.edit_date.isoformat() if getattr(message, "edit_date", None) else None,
        "text": text,
        "url": f"https://t.me/{username}/{message.id}" if username else None,
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
        "reply_count": getattr(getattr(message, "replies", None), "replies", None),
        "grouped_id": getattr(message, "grouped_id", None),
        "is_service": isinstance(message, types.MessageService),
        "links": list(dict.fromkeys(links)),
        "media": media,
        "media_path": None,
        "reactions": raw.get("reactions"),
        "reply_to": raw.get("reply_to"),
        "forward": raw.get("fwd_from"),
        "raw": raw,
        "collected_at": utc_now(),
    }


class TelegramParser:
    def __init__(self, client, archive, media_dir=None, wait_time=1.0):
        self.client = client
        self.archive = archive
        self.media_dir = Path(media_dir).resolve() if media_dir else None
        self.wait_time = wait_time

    async def _wait(self, error):
        LOG.warning("Telegram просить зачекати %s с; збір продовжиться автоматично", error.seconds)
        await asyncio.sleep(error.seconds + 1)

    async def _call(self, operation):
        while True:
            try:
                return await operation()
            except errors.FloodWaitError as error:
                await self._wait(error)

    async def _media(self, message, channel_id):
        # Фото/документи, включно з відео й аудіо. WebPage не завантажуємо.
        if not (getattr(message, "photo", None) or getattr(message, "document", None)):
            return None
        folder = self.media_dir / str(channel_id)
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{message.id}-", dir=folder) as temporary:
            path = await self._call(lambda: self.client.download_media(
                message, file=str(Path(temporary) / str(message.id))
            ))
            if not path:
                raise OSError(f"Не вдалося завантажити вкладення повідомлення {message.id}")
            path = Path(path)
            destination = folder / f"{message.id}{path.suffix}"
            path.replace(destination)
            return str(destination)

    async def collect_source(self, username, limit=None, rescan=False):
        entity = await self._call(lambda: self.client.get_entity(username))
        if not isinstance(entity, (types.Channel, types.Chat)):
            kind = "bot" if getattr(entity, "bot", False) else "user"
            self.archive.status(username, f"skipped_{kind}", error="Адреса не є каналом або групою")
            LOG.warning("@%s: %s; архіву публікацій каналу немає", username, kind)
            return 0
        channel_id = utils.get_peer_id(entity)
        self.archive.register(channel_id, getattr(entity, "username", None), entity.title)
        self.archive.status(username, "collecting", channel_id)
        cursor = 0 if rescan else self.archive.cursor(channel_id)
        # Обмежуємо поточний прохід знімком верхньої межі історії.
        latest = await self._call(lambda: self.client.get_messages(entity, limit=1))
        maximum = latest[0].id + 1 if latest else 1
        count = 0
        while limit is None or count < limit:
            try:
                async for message in self.client.iter_messages(
                    entity, reverse=True, min_id=cursor, max_id=maximum,
                    limit=None if limit is None else limit - count, wait_time=self.wait_time,
                ):
                    record = message_record(message, entity)
                    old = self.archive.record(channel_id, message.id) if rescan else None
                    # Зберігаємо посилання на вже завантажене незмінене вкладення.
                    if old and old.get("media") == record["media"]:
                        old_path = old.get("media_path")
                        if old_path and Path(old_path).is_file():
                            record["media_path"] = old_path
                    if self.media_dir and not record["media_path"]:
                        record["media_path"] = await self._media(message, channel_id)
                    self.archive.save(record)
                    cursor = message.id
                    count += 1
                    if count % 100 == 0:
                        LOG.info("@%s: збережено %s повідомлень за цей прохід", username, count)
                break
            except errors.FloodWaitError as error:
                await self._wait(error)
        status = "limit_reached" if limit is not None and count >= limit else "complete"
        self.archive.status(username, status, channel_id)
        LOG.info("@%s: %s, збережено %s", username, status, count)
        return count

    async def collect(self, sources, limit=None, rescan=False):
        failures = 0
        for username in sources:
            LOG.info("Читання @%s", username)
            try:
                await self.collect_source(username, limit=limit, rescan=rescan)
            except (errors.RPCError, ValueError, OSError) as error:
                # Зберігаємо прив'язку до вже розпізнаного каналу й його курсор.
                row = self.archive.connection.execute(
                    "SELECT channel_id FROM sources WHERE username=?", (username,)
                ).fetchone()
                detail = f"{type(error).__name__}: {error}"
                self.archive.status(username, "error", row[0] if row else None, detail)
                LOG.error("@%s: %s; переходимо до наступного джерела", username, detail)
                failures += 1
        return failures

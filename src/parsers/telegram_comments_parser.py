"""Коментарі до архівованих дописів через MTProto, окремо від основної історії."""
from __future__ import annotations

import csv
import json
import logging
import tempfile
from pathlib import Path

from telethon import errors, utils

from parsers.telegram_parser import TelegramParser, as_json, message_record, utc_now

LOG = logging.getLogger(__name__)


class CommentArchive:
    def __init__(self, archive):
        self.archive = archive
        self.connection = archive.connection
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS comment_threads (
                parent_channel_id INTEGER NOT NULL,
                parent_message_id INTEGER NOT NULL,
                last_comment_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(parent_channel_id,parent_message_id)
            );
            CREATE TABLE IF NOT EXISTS comments (
                parent_channel_id INTEGER NOT NULL,
                parent_message_id INTEGER NOT NULL,
                discussion_channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY(parent_channel_id,parent_message_id,discussion_channel_id,message_id)
            );
        """)

    def cursor(self, channel_id, message_id, discussion_id=None):
        if discussion_id is not None:
            return self.connection.execute("SELECT COALESCE(MAX(message_id),0) FROM comments WHERE parent_channel_id=? AND parent_message_id=? AND discussion_channel_id=?",
                                           (channel_id, message_id, discussion_id)).fetchone()[0]
        row = self.connection.execute("SELECT last_comment_id FROM comment_threads WHERE parent_channel_id=? AND parent_message_id=?", (channel_id, message_id)).fetchone()
        return row[0] if row else 0

    def status(self, channel_id, message_id, status, error=None):
        with self.connection:
            self.connection.execute("INSERT INTO comment_threads(parent_channel_id,parent_message_id,status,error,updated_at) VALUES(?,?,?,?,?) "
                                    "ON CONFLICT(parent_channel_id,parent_message_id) DO UPDATE SET status=excluded.status,error=excluded.error,updated_at=excluded.updated_at",
                                    (channel_id, message_id, status, error, utc_now()))

    def save(self, record):
        parent = (record["parent_channel_id"], record["parent_message_id"])
        with self.connection:
            self.connection.execute("INSERT INTO comments VALUES(?,?,?,?,?) ON CONFLICT(parent_channel_id,parent_message_id,discussion_channel_id,message_id) "
                                    "DO UPDATE SET record_json=excluded.record_json",
                                    (*parent, record["channel_id"], record["message_id"], as_json(record)))
            self.connection.execute("INSERT INTO comment_threads VALUES(?,?,?,'collecting',NULL,?) "
                                    "ON CONFLICT(parent_channel_id,parent_message_id) DO UPDATE SET last_comment_id=MAX(last_comment_id,excluded.last_comment_id),updated_at=excluded.updated_at",
                                    (*parent, record["message_id"], utc_now()))

    def record(self, parent_id, parent_message_id, discussion_id, message_id):
        row = self.connection.execute("SELECT record_json FROM comments WHERE parent_channel_id=? AND parent_message_id=? AND discussion_channel_id=? AND message_id=?",
                                      (parent_id, parent_message_id, discussion_id, message_id)).fetchone()
        return json.loads(row[0]) if row else None

    def export(self, destination, format="jsonl"):
        destination = Path(destination)
        if format not in ("csv", "jsonl"):
            raise ValueError("Формат має бути csv або jsonl")
        if destination.resolve() in {self.archive.path, Path(str(self.archive.path) + "-wal"), Path(str(self.archive.path) + "-shm")}:
            raise ValueError("Експорт не може замінити архів")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fields = ["parent_channel_id", "parent_message_id", "parent_url", "channel_id", "message_id", "date", "edit_date", "text", "url", "reply_to", "media", "media_path", "collected_at"]
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if format == "csv" else "utf-8", newline="", dir=destination.parent, delete=False) as handle:
                temp = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=fields) if format == "csv" else None
                if writer:
                    writer.writeheader()
                total = 0
                for row in self.connection.execute("SELECT record_json FROM comments ORDER BY parent_channel_id,parent_message_id,message_id"):
                    if writer:
                        record = json.loads(row[0])
                        flat = {key: record.get(key) for key in fields}
                        for key, value in flat.items():
                            if isinstance(value, (dict, list)):
                                flat[key] = as_json(value)
                            elif isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                                flat[key] = "'" + value
                        writer.writerow(flat)
                    else:
                        handle.write(row[0] + "\n")
                    total += 1
            temp.replace(destination)
            return total
        finally:
            if temp:
                temp.unlink(missing_ok=True)


class TelegramCommentParser(TelegramParser):
    def __init__(self, client, archive, media_dir=None, wait_time=1.0):
        super().__init__(client, archive, media_dir=media_dir, wait_time=wait_time)
        self.comments = CommentArchive(archive)

    async def collect_thread(self, parent, entity, rescan=False):
        channel_id, post_id = parent["channel_id"], parent["message_id"]
        cursor = 0 if rescan else self.comments.cursor(channel_id, post_id)
        self.comments.status(channel_id, post_id, "collecting")
        latest = await self._call(lambda: self.client.get_messages(entity, reply_to=post_id, limit=1))
        maximum = latest[0].id + 1 if latest else 1
        if latest:
            group_id = utils.get_peer_id(latest[0].peer_id)
            cursor = 0 if rescan else self.comments.cursor(channel_id, post_id, group_id)
        discussion_entities = {}
        total = 0
        while True:
            try:
                async for message in self.client.iter_messages(entity, reply_to=post_id, reverse=True,
                                                               min_id=cursor, max_id=maximum, limit=None, wait_time=self.wait_time):
                    discussion_id = utils.get_peer_id(message.peer_id)
                    # Імена/телефони авторів не розгортаються в окремі профілі.
                    if discussion_id not in discussion_entities:
                        discussion_entities[discussion_id] = await self._call(lambda: self.client.get_entity(message.peer_id))
                    with_peer = discussion_entities[discussion_id]
                    record = message_record(message, with_peer)
                    record.update({"source_mode": "mtproto_comment", "parent_channel_id": channel_id,
                                   "parent_message_id": post_id, "parent_url": parent.get("url")})
                    if not record["url"] and str(discussion_id).startswith("-100"):
                        record["url"] = f"https://t.me/c/{utils.resolve_id(discussion_id)[0]}/{message.id}"
                    old = self.comments.record(channel_id, post_id, discussion_id, message.id)
                    if old and old.get("media") == record["media"] and old.get("media_path") and Path(old["media_path"]).is_file():
                        record["media_path"] = old["media_path"]
                    if self.media_dir and not record["media_path"]:
                        record["media_path"] = await self._media(message, discussion_id)
                    self.comments.save(record)
                    cursor = message.id
                    total += 1
                break
            except errors.FloodWaitError as error:
                await self._wait(error)
        self.comments.status(channel_id, post_id, "complete_pass")
        return total

    async def collect(self, sources, rescan=False):
        failures = 0
        for username in sources:
            row = self.archive.connection.execute("SELECT channel_id FROM sources WHERE username=?", (username,)).fetchone()
            if not row or row[0] is None:
                LOG.info("@%s: немає архівованих дописів для збору коментарів", username)
                continue
            try:
                entity = await self._call(lambda: self.client.get_entity(username))
            except (errors.RPCError, ValueError, OSError) as error:
                LOG.warning("@%s: коментарі недоступні: %s", username, error)
                failures += 1
                continue
            for post in self.archive.connection.execute("SELECT record_json FROM messages WHERE channel_id=? ORDER BY message_id", (row[0],)):
                parent = json.loads(post[0])
                if not (parent.get("raw", {}).get("replies") or {}).get("comments"):
                    continue
                try:
                    total = await self.collect_thread(parent, entity, rescan=rescan)
                    LOG.info("@%s/%s: збережено %s коментарів", username, parent["message_id"], total)
                except (errors.RPCError, ValueError, OSError) as error:
                    self.comments.status(parent["channel_id"], parent["message_id"], "unavailable", f"{type(error).__name__}: {error}")
                    LOG.warning("@%s/%s: коментарі недоступні: %s", username, parent["message_id"], error)
                    failures += 1
        return failures

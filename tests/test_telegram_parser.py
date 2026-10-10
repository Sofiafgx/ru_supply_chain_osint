import asyncio
import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import errors, types, utils

from parsers.telegram_parser import (
    TelegramArchive, TelegramParser, message_record, read_sources,
)

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def channel(identifier=123, username="ExampleChannel"):
    return types.Channel(identifier, "Канал", types.ChatPhotoEmpty(), NOW,
                         username=username, broadcast=True)


def message(identifier, text="Текст", photo=False):
    media = types.MessageMediaPhoto(
        photo=types.Photo(7, 8, b"ref", NOW, [], 2)
    ) if photo else None
    return types.Message(identifier, peer_id=types.PeerChannel(123), date=NOW,
                         message=text, media=media, views=10, forwards=2)


class FakeClient:
    def __init__(self, entity, messages):
        self.entity = entity
        self.messages = messages
        self.offsets = []
        self.history_error = None
        self.error_after = 1
        self.download_error = None

    async def get_entity(self, username):
        if username == "missing":
            raise ValueError("No such username")
        if username == "robot":
            return types.User(99, bot=True, username=username)
        return self.entity

    async def get_messages(self, entity, limit):
        return self.messages[-limit:][::-1]

    async def iter_messages(self, entity, **kwargs):
        self.offsets.append(kwargs["min_id"])
        count = 0
        for item in self.messages:
            if not kwargs["min_id"] < item.id < kwargs["max_id"]:
                continue
            if kwargs["limit"] is not None and count >= kwargs["limit"]:
                return
            yield item
            count += 1
            if self.history_error and count >= self.error_after:
                error, self.history_error = self.history_error, None
                raise error

    async def download_media(self, message, file):
        destination = Path(file + ".jpg")
        destination.write_bytes(b"test-photo")
        if self.download_error:
            error, self.download_error = self.download_error, None
            raise error
        return str(destination)


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.archive = TelegramArchive(self.folder / "archive.sqlite3")
        self.entity = channel()
        self.identifier = utils.get_peer_id(self.entity)
        self.archive.register(self.identifier, self.entity.username, self.entity.title)

    def tearDown(self):
        self.archive.close()
        self.temporary.cleanup()

    def rows(self):
        return self.archive.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


class ArchiveTests(ArchiveFixture):
    def test_all_requested_sources_and_case_insensitive_deduplication(self):
        original = read_sources(Path(__file__).resolve().parents[1] / 'config/telegram_channels.txt')
        self.assertEqual(len(original), 32)
        self.assertIn("DeepStateUA", original)
        self.assertIn("YouControlBot", original)
        path = self.folder / "channels.txt"
        path.write_text("@ExampleChannel\n@examplechannel # duplicate\n\n# comment\n")
        self.assertEqual(read_sources(path), ["ExampleChannel"])
        path.write_text("https://unrelated.example/path")
        with self.assertRaises(ValueError):
            read_sources(path)

    def test_upsert_refreshes_without_duplicates_and_preserves_high_cursor(self):
        self.archive.save(message_record(message(10), self.entity))
        self.archive.save(message_record(message(5), self.entity))
        self.archive.save(message_record(message(10, "Виправлено"), self.entity))
        self.assertEqual(self.rows(), 2)
        self.assertEqual(self.archive.cursor(self.identifier), 10)
        self.assertEqual(self.archive.record(self.identifier, 10)["text"], "Виправлено")

    def test_failed_transaction_never_skips_a_message(self):
        self.archive.connection.execute("""
            CREATE TRIGGER fail_cursor BEFORE UPDATE ON channels
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.archive.save(message_record(message(10), self.entity))
        self.assertEqual(self.rows(), 0)
        self.assertEqual(self.archive.cursor(self.identifier), 0)

    def test_utf8_exports_round_trip_media_only_and_hidden_links(self):
        item = message(1, "=HYPERLINK(\"test\") https://example.org/path")
        item.entities = [types.MessageEntityTextUrl(0, 4, "https://hidden.example/")]
        self.archive.save(message_record(item, self.entity))
        self.archive.save(message_record(message(2, "", photo=True), self.entity))
        json_path = self.folder / "posts.jsonl"
        csv_path = self.folder / "posts.csv"
        self.assertEqual(self.archive.export(json_path, "jsonl"), 2)
        self.assertEqual(self.archive.export(csv_path, "csv"), 2)
        records = [json.loads(line) for line in json_path.read_text().splitlines()]
        self.assertEqual(records[0]["links"], ["https://example.org/path", "https://hidden.example/"])
        self.assertEqual(records[1]["text"], "")
        self.assertEqual(records[1]["media"]["type"], "MessageMediaPhoto")
        self.assertIn("raw", records[0])
        with csv_path.open(encoding="utf-8-sig", newline="") as output:
            exported = list(csv.DictReader(output))
        self.assertEqual(exported[0]["channel_title"], "Канал")
        self.assertTrue(exported[0]["text"].startswith("'="))
        self.assertTrue(records[0]["text"].startswith("="))

    def test_failed_export_preserves_existing_file(self):
        self.archive.save(message_record(message(1), self.entity))
        destination = self.folder / "posts.csv"
        destination.write_text("previous export")
        with patch("parsers.telegram_parser.csv.DictWriter.writerow", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.archive.export(destination, "csv")
        self.assertEqual(destination.read_text(), "previous export")

    def test_export_cannot_replace_archive(self):
        self.archive.save(message_record(message(1), self.entity))
        with self.assertRaises(ValueError):
            self.archive.export(self.folder / "archive.sqlite3", "jsonl")
        self.assertEqual(self.rows(), 1)


class CollectionTests(ArchiveFixture):
    def run_async(self, operation):
        return asyncio.run(operation)

    def test_restarts_after_network_failure_from_committed_cursor(self):
        client = FakeClient(self.entity, [message(1), message(4), message(9)])
        client.history_error = OSError("connection lost")
        collector = TelegramParser(client, self.archive)
        self.assertEqual(self.run_async(collector.collect(["ExampleChannel"])), 1)
        self.assertEqual(self.archive.cursor(self.identifier), 1)
        self.assertEqual(self.archive.report()[0]["status"], "error")
        # Закриття і повторне відкриття перевіряє реальну стійкість курсора на диску.
        self.archive.close()
        self.archive = TelegramArchive(self.folder / "archive.sqlite3")
        collector = TelegramParser(client, self.archive)
        self.assertEqual(self.run_async(collector.collect(["ExampleChannel"])), 0)
        self.assertEqual(client.offsets, [0, 1])
        self.assertEqual(self.rows(), 3)
        self.assertEqual(self.archive.cursor(self.identifier), 9)

    def test_flood_wait_resumes_without_duplicates(self):
        client = FakeClient(self.entity, [message(1), message(2), message(3)])
        client.history_error = errors.FloodWaitError(request=None, capture=5)
        with patch("parsers.telegram_parser.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.run_async(TelegramParser(client, self.archive).collect(["ExampleChannel"]))
            sleep.assert_awaited_once_with(6)
        self.assertEqual(client.offsets, [0, 1])
        self.assertEqual(self.rows(), 3)

    def test_failed_source_and_bot_do_not_stop_other_sources(self):
        client = FakeClient(self.entity, [message(1)])
        failures = self.run_async(TelegramParser(client, self.archive).collect(
            ["missing", "robot", "ExampleChannel"]
        ))
        self.assertEqual(failures, 1)
        statuses = {row["username"]: row["status"] for row in self.archive.report()}
        self.assertEqual(statuses, {"missing": "error", "robot": "skipped_bot", "ExampleChannel": "complete"})
        self.assertEqual(self.rows(), 1)

    def test_limit_incremental_update_and_rescan(self):
        client = FakeClient(self.entity, [message(2), message(5), message(8)])
        collector = TelegramParser(client, self.archive)
        self.run_async(collector.collect(["ExampleChannel"], limit=2))
        self.assertEqual(self.archive.report()[0]["status"], "limit_reached")
        self.assertEqual(self.archive.cursor(self.identifier), 5)
        self.run_async(collector.collect(["ExampleChannel"]))
        self.assertEqual(self.rows(), 3)
        client.messages[0].message = "Редагування"
        self.run_async(collector.collect(["ExampleChannel"], rescan=True))
        self.assertEqual(self.rows(), 3)
        self.assertEqual(self.archive.record(self.identifier, 2)["text"], "Редагування")
        self.assertEqual(self.archive.cursor(self.identifier), 8)

    def test_media_failure_does_not_advance_cursor_and_can_be_retried(self):
        client = FakeClient(self.entity, [message(1, photo=True)])
        client.download_error = OSError("download interrupted")
        collector = TelegramParser(client, self.archive, media_dir=self.folder / "media")
        self.assertEqual(self.run_async(collector.collect(["ExampleChannel"])), 1)
        self.assertEqual(self.archive.cursor(self.identifier), 0)
        self.assertEqual(self.rows(), 0)
        self.assertEqual(list((self.folder / "media" / str(self.identifier)).iterdir()), [])
        self.assertEqual(self.run_async(collector.collect(["ExampleChannel"])), 0)
        record = self.archive.record(self.identifier, 1)
        self.assertEqual(Path(record["media_path"]).read_bytes(), b"test-photo")
        with patch.object(client, "download_media", side_effect=AssertionError("unnecessary redownload")):
            self.assertEqual(self.run_async(collector.collect(["ExampleChannel"], rescan=True)), 0)

    def test_media_can_be_added_by_rescan_after_text_collection(self):
        client = FakeClient(self.entity, [message(1, photo=True)])
        self.run_async(TelegramParser(client, self.archive).collect(["ExampleChannel"]))
        self.assertIsNone(self.archive.record(self.identifier, 1)["media_path"])
        collector = TelegramParser(client, self.archive, media_dir=self.folder / "media")
        self.run_async(collector.collect(["ExampleChannel"], rescan=True))
        self.assertTrue(Path(self.archive.record(self.identifier, 1)["media_path"]).is_file())
        self.assertEqual(self.rows(), 1)

    def test_source_alias_change_preserves_entity_cursor(self):
        client = FakeClient(self.entity, [message(1)])
        collector = TelegramParser(client, self.archive)
        self.run_async(collector.collect(["ExampleChannel"]))
        self.entity.username = "RenamedChannel"
        client.messages.append(message(2))
        self.run_async(collector.collect(["RenamedChannel"]))
        self.assertEqual(client.offsets, [0, 1])
        self.assertEqual(self.rows(), 2)


if __name__ == "__main__":
    unittest.main()

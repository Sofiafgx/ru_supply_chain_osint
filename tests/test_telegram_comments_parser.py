import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import errors, types, utils

from parsers.telegram_comments_parser import CommentArchive, TelegramCommentParser
from parsers.telegram_parser import TelegramArchive, message_record

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def channel(identifier, username=None, broadcast=False):
    return types.Channel(identifier, "Канал" if broadcast else "Обговорення", types.ChatPhotoEmpty(), NOW,
                         username=username, broadcast=broadcast, megagroup=not broadcast)


class FakeCommentClient:
    def __init__(self, parent, group, threads):
        self.parent = parent
        self.group = group
        self.threads = threads
        self.offsets = []
        self.error_after_first = None
        self.failed_threads = set()
        self.download_error = None

    async def get_entity(self, entity):
        return self.group if isinstance(entity, types.PeerChannel) else self.parent

    async def get_messages(self, entity, reply_to, limit):
        if reply_to in self.failed_threads:
            raise errors.ChannelPrivateError(request=None)
        return self.threads.get(reply_to, [])[-limit:][::-1]

    async def iter_messages(self, entity, **kwargs):
        self.offsets.append((kwargs["reply_to"], kwargs["min_id"]))
        for message in self.threads.get(kwargs["reply_to"], []):
            if kwargs["min_id"] < message.id < kwargs["max_id"]:
                yield message
                if self.error_after_first:
                    error, self.error_after_first = self.error_after_first, None
                    raise error

    async def download_media(self, message, file):
        if self.download_error:
            raise self.download_error
        path = Path(file + ".jpg")
        path.write_bytes(b"original")
        return str(path)


class CommentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.archive = TelegramArchive(self.folder / "archive.sqlite3")
        self.parent = channel(123, "ExampleChannel", broadcast=True)
        self.group = channel(456)
        self.parent_id = utils.get_peer_id(self.parent)
        self.archive.register(self.parent_id, self.parent.username, self.parent.title)
        self.archive.status(self.parent.username, "complete", self.parent_id)
        for post_id in (5, 8):
            message = types.Message(post_id, peer_id=types.PeerChannel(123), date=NOW, message="Допис",
                                    replies=types.MessageReplies(replies=2, replies_pts=1, comments=True, channel_id=456))
            self.archive.save(message_record(message, self.parent))

    def tearDown(self):
        self.archive.close()
        self.temporary.cleanup()

    def comment(self, identifier, photo=False):
        media = types.MessageMediaPhoto(photo=types.Photo(7, 8, b"ref", NOW, [], 2)) if photo else None
        return types.Message(identifier, peer_id=types.PeerChannel(456), date=NOW, message="Коментар", media=media)

    def test_comments_bound_to_post_separate_from_channel_cursor_and_resume_flood(self):
        client = FakeCommentClient(self.parent, self.group, {5: [self.comment(50), self.comment(51)]})
        client.error_after_first = errors.FloodWaitError(request=None, capture=2)
        collector = TelegramCommentParser(client, self.archive)
        with patch("parsers.telegram_parser.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 0)
            sleep.assert_awaited_once_with(3)
        self.assertEqual(client.offsets[:2], [(5, 0), (5, 50)])
        self.assertEqual(self.archive.cursor(self.parent_id), 8)
        self.assertEqual(collector.comments.cursor(self.parent_id, 5), 51)
        self.assertEqual(collector.comments.export(self.folder / "comments.jsonl"), 2)
        rows = [json.loads(line) for line in (self.folder / "comments.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["parent_url"], "https://t.me/ExampleChannel/5")
        self.assertEqual(rows[0]["channel_id"], utils.get_peer_id(self.group))
        self.assertEqual(rows[0]["url"], "https://t.me/c/456/50")
        client.threads[5].append(self.comment(52))
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 0)
        self.assertEqual(collector.comments.export(self.folder / "comments.csv", "csv"), 3)
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"], rescan=True)), 0)
        self.assertEqual(collector.comments.export(self.folder / "comments.jsonl"), 3)

    def test_unavailable_thread_does_not_stop_other_posts(self):
        client = FakeCommentClient(self.parent, self.group, {8: [self.comment(60)]})
        client.failed_threads.add(5)
        collector = TelegramCommentParser(client, self.archive)
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 1)
        self.assertEqual(collector.comments.cursor(self.parent_id, 8), 60)
        statuses = dict(self.archive.connection.execute("SELECT parent_message_id,status FROM comment_threads"))
        self.assertEqual(statuses, {5: "unavailable", 8: "complete_pass"})

    def test_media_failure_does_not_advance_comment_cursor(self):
        client = FakeCommentClient(self.parent, self.group, {5: [self.comment(50, photo=True)]})
        client.download_error = OSError("interrupted")
        collector = TelegramCommentParser(client, self.archive, media_dir=self.folder / "media")
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 1)
        self.assertEqual(collector.comments.cursor(self.parent_id, 5), 0)
        client.download_error = None
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 0)
        record = collector.comments.record(self.parent_id, 5, utils.get_peer_id(self.group), 50)
        self.assertEqual(Path(record["media_path"]).read_bytes(), b"original")
        self.assertEqual(collector.comments.cursor(self.parent_id, 5), 50)
        with self.assertRaises(ValueError):
            collector.comments.export(self.archive.path)

    def test_comment_and_cursor_transaction_rolls_back_together(self):
        comments = CommentArchive(self.archive)
        self.archive.connection.execute("CREATE TRIGGER reject_cursor BEFORE INSERT ON comment_threads BEGIN SELECT RAISE(ABORT, 'fail'); END")
        record = message_record(self.comment(50), self.group)
        record.update(parent_channel_id=self.parent_id, parent_message_id=5, parent_url=None)
        with self.assertRaises(sqlite3.IntegrityError):
            comments.save(record)
        self.assertEqual(self.archive.connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0)
        self.assertEqual(comments.cursor(self.parent_id, 5), 0)

    def test_new_discussion_group_uses_its_own_message_cursor(self):
        client = FakeCommentClient(self.parent, self.group, {5: [self.comment(50)]})
        collector = TelegramCommentParser(client, self.archive)
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 0)
        client.group = channel(789)
        client.threads[5] = [types.Message(2, peer_id=types.PeerChannel(789), date=NOW, message="Нова група")]
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 0)
        client.threads[5].append(types.Message(3, peer_id=types.PeerChannel(789), date=NOW, message="Новий коментар"))
        self.assertEqual(asyncio.run(collector.collect(["ExampleChannel"])), 0)
        self.assertEqual(collector.comments.cursor(self.parent_id, 5, utils.get_peer_id(client.group)), 3)
        self.assertEqual(collector.comments.export(self.folder / "comments.jsonl"), 3)


if __name__ == "__main__":
    unittest.main()

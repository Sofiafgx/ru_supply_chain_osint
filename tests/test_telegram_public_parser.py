import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from parsers.telegram_public_parser import PublicArchive, PublicTelegramParser, parse_public_page


def page(ids, before=None, text="Текст<br>Другий рядок"):
    posts = "".join(
        f'<div class="tgme_widget_message" data-post="Example/{identifier}">'
        f'<div class="tgme_widget_message_text">{text}<a href="https://example.org/">Посилання</a></div>'
        '<a class="tgme_widget_message_photo_wrap" style="background-image:url(\'https://cdn.example/photo.jpg\')"></a>'
        '<time datetime="2026-10-08T00:00:00+00:00"></time>'
        '<span class="tgme_widget_message_views">12.3K</span></div>' for identifier in ids
    )
    link = f'<a class="tme_messages_more" href="/s/Example?before={before}"></a>' if before else ""
    return f'<div class="tgme_channel_info_header_title">Канал</div><div class="tgme_channel_history">{posts}{link}</div>'


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.archive = PublicArchive(self.folder / "archive.sqlite3")
        self.archive.prepare(["Example"])

    def tearDown(self):
        self.archive.close()
        self.temporary.cleanup()

    def save(self, ids, before):
        records, cursor = parse_public_page(page(ids, before), "Example")
        self.archive.save_page("Example", records, cursor, self.archive.source("Example"))

    def test_text_links_and_media_and_pagination(self):
        records, before = parse_public_page(page([9, 10], 9), "Example")
        self.assertEqual(before, 9)
        self.assertEqual(records[0]["text"], "Текст\nДругий рядокПосилання")
        self.assertEqual(records[0]["links"], ["https://example.org/"])
        self.assertEqual(records[0]["media"][0]["url"], "https://cdn.example/photo.jpg")
        self.assertEqual(records[0]["views_display"], "12.3K")
        self.assertEqual(records[0]["source_mode"], "public_web")

    def test_foreign_pagination_is_not_followed(self):
        html = page([1], 1).replace('/s/Example?before=1', 'https://evil.example/s/Example?before=1')
        self.assertIsNone(parse_public_page(html, "Example")[1])

    def test_quoted_reply_is_not_used_as_main_post_text(self):
        html = '<div class="tgme_widget_message" data-post="Example/10">' \
               '<div class="tgme_widget_message_text js-message_reply_text">Чужа цитата</div>' \
               '<div class="tgme_widget_message_text js-message_text">Власний текст</div></div>'
        records, _ = parse_public_page(html, "Example")
        self.assertEqual(records[0]["text"], "Власний текст")
        self.assertEqual(records[0]["reply_preview"], "Чужа цитата")

    def test_resume_and_refresh_gap_survive_restart(self):
        self.save([100, 120], 100)
        self.save([1, 50], None)
        self.assertEqual(self.archive.source("Example")["status"], "web_history_exhausted")
        self.archive.prepare(["Example"])
        self.save([140, 160], 140)
        self.assertEqual(self.archive.source("Example")["refresh_stop"], 120)
        self.archive.close()
        self.archive = PublicArchive(self.folder / "archive.sqlite3")
        self.archive.prepare(["Example"])
        self.assertEqual(self.archive.source("Example")["refresh_before"], 140)
        self.assertEqual(self.archive.source("Example")["refresh_stop"], 120)
        self.save([110, 130], 110)
        self.assertEqual(self.archive.source("Example")["status"], "web_history_exhausted")
        self.assertEqual(self.archive.report()[0]["posts"], 8)

    def test_resaving_page_does_not_duplicate(self):
        self.save([10, 20], 10)
        records, before = parse_public_page(page([10, 20], 10, text="Оновлено"), "Example")
        self.archive.save_page("Example", records, before, self.archive.source("Example"))
        self.assertEqual(self.archive.report()[0]["posts"], 2)
        row = self.archive.db.execute("SELECT record_json FROM web_posts WHERE message_id=10").fetchone()
        self.assertEqual(json.loads(row[0])["text"], "ОновленоПосилання")

    def test_exports_and_progress_are_available_without_login(self):
        self.save([10, 20], 10)
        progress = self.archive.snapshot(self.folder)
        self.assertEqual(progress["posts"], 2)
        self.assertEqual(len((self.folder / "posts.jsonl").read_text().splitlines()), 2)
        self.assertTrue((self.folder / "posts.csv").is_file())
        self.assertEqual(json.loads((self.folder / "progress.json").read_text())["posts"], 2)
        self.assertTrue((self.folder / "channel_coverage.csv").is_file())
        self.assertTrue((self.folder / "Example.csv").is_file())

    def test_channel_export_excludes_other_channels(self):
        self.save([1, 2], None)
        other, cursor = parse_public_page(page([1]).replace("Example", "OtherChannel"), "OtherChannel")
        self.archive.prepare(["OtherChannel"])
        self.archive.save_page("OtherChannel", other, cursor, self.archive.source("OtherChannel"))
        self.archive.export(self.folder / "subset.jsonl", username="Example")
        rows = [json.loads(line) for line in (self.folder / "subset.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["channel_username"] for r in rows}, {"Example"})

    def test_oldest_web_page_is_not_claimed_complete_since_creation(self):
        self.save([100, 120], None)
        report = self.archive.report()[0]
        self.assertEqual(report["oldest_id"], 100)
        self.assertEqual(report["history_boundary"], "oldest_visible_page_reached")
        self.assertFalse(report["complete_since_creation_verified"])
        self.assertIsNone(report["creation_date"])

    def test_first_id_is_separate_from_creation_date_and_complete_archive(self):
        self.save([1, 20], None)
        report = self.archive.report()[0]
        self.assertEqual(report["history_boundary"], "first_id_reached")
        self.assertFalse(report["complete_since_creation_verified"])
        self.assertIsNone(report["creation_date"])

    def test_priority_list_keeps_every_original_and_related_source(self):
        from types import SimpleNamespace
        from telegram_public_cli import load_sources
        original = self.folder / "original.txt"
        related = self.folder / "related.txt"
        priority = self.folder / "priority.txt"
        original.write_text("@FirstChannel\n@SecondChannel\n")
        related.write_text("@RelatedChannel\n")
        priority.write_text("@SecondChannel\n@UnknownChannel\n")
        sources = load_sources(SimpleNamespace(channels_file=original,
                                               related_channels_file=related,
                                               priority_channels_file=priority))
        self.assertEqual(sources, ["SecondChannel", "RelatedChannel", "FirstChannel"])

    def test_stalled_pagination_is_error_and_keeps_cursor(self):
        self.save([10, 20], 10)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=page([10, 20], 10))
            )) as client:
                return await PublicTelegramParser(client, self.archive, delay=0).step("Example")
        self.assertFalse(asyncio.run(run()))
        self.assertEqual(self.archive.source("Example")["before_id"], 10)
        self.assertEqual(self.archive.source("Example")["status"], "error")

    def test_landing_page_does_not_falsely_finish_history(self):
        self.save([10, 20], 10)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text='<div class="tgme_page">Open Telegram</div>')
            )) as client:
                return await PublicTelegramParser(client, self.archive, delay=0).step("Example")
        self.assertFalse(asyncio.run(run()))
        self.assertFalse(self.archive.source("Example")["history_done"])
        self.assertEqual(self.archive.source("Example")["before_id"], 10)

    def test_inaccessible_source_is_reported_without_login(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text='<div class="tgme_page">Bot</div>')
            )) as client:
                return await PublicTelegramParser(client, self.archive, delay=0).step("Example")
        self.assertFalse(asyncio.run(run()))
        self.assertEqual(self.archive.source("Example")["status"], "web_preview_unavailable")

    def test_network_outage_is_retried_and_does_not_finish_history(self):
        self.save([10, 20], 10)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=page([1, 5]))
            )) as client:
                collector = PublicTelegramParser(client, self.archive, delay=0)
                from unittest.mock import AsyncMock, patch
                with patch.object(collector, "fetch", new_callable=AsyncMock,
                                  side_effect=httpx.ConnectError("network unavailable")) as fetch:
                    self.assertTrue(await collector.step("Example"))
                    self.assertEqual(self.archive.source("Example")["status"], "network_retry")
                    self.assertEqual(self.archive.source("Example")["before_id"], 10)
                    self.assertTrue(await collector.step("Example"))
                    self.assertEqual(fetch.await_count, 1)
                collector.retry_at["Example"] = 0
                self.assertTrue(await collector.step("Example"))
                self.assertEqual(self.archive.source("Example")["status"], "web_history_exhausted")
                self.assertNotIn("Example", collector.retry_at)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

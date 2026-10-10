import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from social_collection import SocialArchive, canonical, windows


class SocialArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.archive = SocialArchive(self.project / "collection")

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def post(self, identifier, **changes):
        value = {"platform": "X", "post_id": str(identifier), "account_handle": "@sample",
                 "author_id": "42", "timestamp": "2024-01-02T00:00:00Z", "text": "post " + str(identifier)}
        value.update(changes)
        return value

    def job(self):
        self.archive.plan_account({"platform": "X", "account_handle": "@sample", "account_id": "42",
                                   "created_at": "2024-01-01T00:00:00Z", "status": "pending"},
                                  "2024-02-01T00:00:00Z", self.archive.index_digest(), months=1)
        return self.archive.next_job()

    def page(self, posts, cursor=None, **changes):
        value = {"posts": posts, "next_cursor": cursor, "exhausted": cursor is None,
                 "coverage_kind": "full_archive"}
        value.update(changes)
        return value

    def count(self, table):
        return self.archive.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def current(self, job):
        return self.archive.db.execute("SELECT * FROM jobs WHERE task_id=?", (job["task_id"],)).fetchone()

    def test_historical_duplicate_streak_does_not_hide_older_new_post(self):
        duplicates = [self.post(i) for i in range(20)]
        with self.archive.db:
            for post in duplicates:
                self.archive.index_record(post, "existing")
        job = self.job()
        self.archive.commit_page(job, self.page(duplicates, "older"))
        current = self.current(job)
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["duplicate_streak"], 20)
        self.archive.commit_page(current, self.page([self.post("older-new")]))
        current = self.current(job)
        self.assertEqual(current["status"], "api_exhausted")
        self.assertEqual(current["new_posts"], 1)
        self.assertEqual(current["skipped_existing"], 20)
        self.assertEqual(self.count("posts"), 1)

    def test_identical_ids_on_different_platforms_are_distinct(self):
        with self.archive.db:
            self.assertEqual(self.archive.index_record(self.post("123"), "test", save=True), "NEW")
            self.assertEqual(self.archive.index_record(self.post("123", platform="Threads"), "test", save=True), "NEW")
        self.assertEqual(self.count("posts"), 2)
        self.assertTrue(self.archive.contains("X", "123"))
        self.assertTrue(self.archive.contains("Threads", "123"))

    def test_content_duplicate_with_new_id_indexes_id_without_saving_post(self):
        first = self.post("first", text="identical")
        second = self.post("second", text="identical")
        with self.archive.db:
            self.assertEqual(self.archive.index_record(first, "test", save=True), "NEW")
            self.assertEqual(self.archive.index_record(second, "test", save=True), "SKIPPED_EXISTING")
        self.assertTrue(self.archive.contains("X", "second"))
        self.assertEqual(self.count("posts"), 1)
        self.assertEqual(self.count("existing_ids"), 2)

    def test_md5_collision_requires_matching_canonical_content(self):
        class CollidingDigest:
            def hexdigest(self):
                return "0" * 32
        with patch("social_collection.hashlib.md5", return_value=CollidingDigest()):
            with self.archive.db:
                self.assertEqual(self.archive.index_record(self.post("one", text="first text"), "test", True), "NEW")
                self.assertEqual(self.archive.index_record(self.post("two", text="other text"), "test", True), "NEW")
                self.assertEqual(self.archive.index_record(self.post("three", text="first text"), "test", True), "SKIPPED_EXISTING")
        self.assertEqual(self.count("posts"), 2)
        canonical_texts = {json.loads(row[0])[2] for row in self.archive.db.execute("SELECT canonical FROM existing_content")}
        self.assertEqual(canonical_texts, {"first text", "other text"})

    def test_handle_only_export_deduplicates_live_post_with_author_id(self):
        exported = self.post("export-id", text="same content")
        del exported["author_id"]
        path = self.project / "posts.jsonl"
        path.write_text(json.dumps(exported) + "\n", encoding="utf-8")
        self.archive.import_posts(path)
        with self.archive.db:
            result = self.archive.index_record(self.post("live-id", text="same content"), "worker", save=True)
        self.assertEqual(result, "SKIPPED_EXISTING")
        self.assertTrue(self.archive.contains("X", "live-id"))
        self.assertEqual(self.count("posts"), 0)

    def test_normalization_equates_timezone_unicode_and_newlines(self):
        left = self.post("one", text="caf\u00e9\r\nline", timestamp="2024-01-02T02:00:00+02:00")
        right = self.post("two", text="cafe\u0301\nline", timestamp="2024-01-02T00:00:00.000000Z")
        self.assertEqual(canonical(left)[1], canonical(right)[1])

    def test_malformed_page_rolls_back_posts_ids_and_checkpoint(self):
        job = self.job()
        invalid = self.post("invalid")
        del invalid["text"]
        with self.assertRaises(ValueError):
            self.archive.commit_page(job, self.page([self.post("good"), invalid], "next"))
        self.assertEqual(self.count("posts"), 0)
        self.assertEqual(self.count("existing_ids"), 0)
        self.assertEqual(self.current(job)["pages"], 0)
        self.assertIsNone(self.current(job)["cursor"])
        self.archive.commit_page(job, self.page([self.post("good")], "next"))
        self.assertEqual(self.current(job)["pages"], 1)

    def test_cursor_recovery_replays_idempotently_and_resumes(self):
        job = self.job()
        self.archive.commit_page(job, self.page([self.post("one")], "expired"))
        self.archive.restart_cursor(self.current(job))
        replay = self.current(job)
        self.assertIsNone(replay["cursor"])
        self.assertEqual(replay["cursor_restarts"], 1)
        self.archive.commit_page(replay, self.page([self.post("one"), self.post("two")], "fresh"))
        self.archive.commit_page(self.current(job), self.page([self.post("three")]))
        self.assertEqual(self.count("posts"), 3)
        self.assertEqual(self.current(job)["skipped_existing"], 1)
        self.assertEqual(self.current(job)["status"], "api_exhausted")

    def test_half_open_bounds_with_fractional_seconds(self):
        job = self.job()
        posts = [self.post("before", timestamp="2023-12-31T23:59:59.999999Z"),
                 self.post("at-start", timestamp="2024-01-01T00:00:00Z"),
                 self.post("just-after-start", timestamp="2024-01-01T00:00:00.000001Z"),
                 self.post("before-end", timestamp="2024-01-31T23:59:59.999999Z"),
                 self.post("at-end", timestamp="2024-02-01T00:00:00Z"),
                 self.post("after-end", timestamp="2024-02-01T00:00:00.000001Z")]
        self.archive.commit_page(job, self.page(posts))
        identifiers = {r[0] for r in self.archive.db.execute("SELECT post_id FROM posts")}
        self.assertEqual(identifiers, {"at-start", "just-after-start", "before-end"})

    def test_month_windows_have_no_gaps_and_clip_final_end(self):
        ranges = list(windows("2024-01-31T12:00:00Z", "2024-05-02T13:00:00Z", months=1))
        parsed = [(datetime.fromisoformat(r["start"].replace("Z", "+00:00")),
                   datetime.fromisoformat(r["end"].replace("Z", "+00:00"))) for r in ranges]
        self.assertEqual(parsed[0][1].day, 29)
        self.assertEqual(parsed[-1][1], datetime.fromisoformat("2024-05-02T13:00:00+00:00"))
        self.assertTrue(all(start < end for start, end in parsed))
        self.assertTrue(all(parsed[i][1] == parsed[i + 1][0] for i in range(len(parsed) - 1)))

    def test_repeated_cursor_and_cycle_are_rejected_atomically(self):
        job = self.job()
        self.archive.commit_page(job, self.page([self.post("one")], "A"))
        with self.assertRaisesRegex(ValueError, "Repeated"):
            self.archive.commit_page(self.current(job), self.page([self.post("bad-repeat")], "A"))
        self.archive.commit_page(self.current(job), self.page([self.post("two")], "B"))
        with self.assertRaisesRegex(ValueError, "Repeated"):
            self.archive.commit_page(self.current(job), self.page([self.post("bad-cycle")], "A"))
        self.assertEqual(self.count("posts"), 2)
        self.assertEqual(self.current(job)["cursor"], "B")

    def test_two_worker_connections_reject_stale_page_checkpoint(self):
        job = self.job()
        other = SocialArchive(self.archive.directory)
        try:
            stale = other.next_job()
            self.archive.commit_page(job, self.page([self.post("winner")], "next"))
            with self.assertRaisesRegex(ValueError, "Stale"):
                other.commit_page(stale, self.page([self.post("loser")], "wrong"))
            self.assertFalse(self.archive.contains("X", "loser"))
            self.assertEqual(self.current(job)["cursor"], "next")
        finally:
            other.close()

    def test_stale_cursor_restart_cannot_reset_another_worker_progress(self):
        job = self.job()
        self.archive.commit_page(job, self.page([self.post("one")], "A"))
        stale = self.current(job)
        self.archive.commit_page(stale, self.page([self.post("two")], "B"))
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.archive.restart_cursor(stale)
        self.assertEqual(self.current(job)["cursor"], "B")
        self.assertEqual(self.current(job)["cursor_restarts"], 0)

    def test_cli_stale_worker_does_not_mark_concurrent_completed_job_failed(self):
        from social_cli import run
        job = self.job()
        archive = self.archive
        winner_page = self.page([self.post("winner")])
        stale_page = self.page([self.post("stale")])

        class RacingAdapter:
            def fetch_page(self, task, cursor):
                other = SocialArchive(archive.directory)
                try:
                    other.commit_page(other.next_job(), winner_page)
                finally:
                    other.close()
                return stale_page

        run(archive, {"X": RacingAdapter()}, max_pages=1, delay=0)
        self.assertEqual(self.current(job)["status"], "api_exhausted")
        self.assertTrue(archive.contains("X", "winner"))
        self.assertFalse(archive.contains("X", "stale"))

    def test_inventory_imports_db_and_exports_without_telegram_id_collisions(self):
        connection = sqlite3.connect(self.project / "legacy.sqlite3")
        connection.executescript("CREATE TABLE records(source TEXT, kind TEXT, id TEXT, json TEXT);"
                                 "CREATE TABLE web_posts(id TEXT);")
        connection.execute("INSERT INTO records VALUES(?,?,?,?)", ("twitter", "post", "db-x", json.dumps(self.post("db-x"))))
        connection.execute("INSERT INTO records VALUES(?,?,?,?)", ("Threads", "post", "db-threads", json.dumps({"data": self.post("db-threads", platform="Threads")})))
        connection.execute("INSERT INTO records VALUES(?,?,?,?)", ("telegram", "post", "tg-only", "{}"))
        connection.execute("INSERT INTO records VALUES(?,?,?,?)", ("X", "profile", "profile-only", "{}"))
        connection.execute("INSERT INTO web_posts VALUES('db-x')")
        connection.commit()
        connection.close()
        telegram_row = {"source_mode": "public_web", "channel_username": "example", "message_id": "tg-jsonl"}
        (self.project / "posts.jsonl").write_text(json.dumps(self.post("jsonl")) + "\n" + json.dumps(telegram_row) + "\n", encoding="utf-8")
        with (self.project / "posts.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, list(self.post("csv")))
            writer.writeheader()
            writer.writerow(self.post("csv"))
        report = self.archive.inventory(self.project)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["existing_id_count"], 4)
        self.assertFalse(self.archive.contains("X", "tg-only"))
        self.assertFalse(self.archive.contains("X", "tg-jsonl"))
        self.assertFalse(self.archive.contains("X", "profile-only"))
        self.assertEqual(self.count("posts"), 0)
        digest = self.archive.index_digest()
        self.assertEqual(self.archive.inventory(self.project)["existing_post_ids_hash"], digest)

    def test_inventory_preserves_other_archive_index_only_ids_and_content(self):
        other = SocialArchive(self.project / "older-archive")
        try:
            with other.db:
                other.index_record(self.post("known-only", text="known content"), "old-export")
        finally:
            other.close()
        report = self.archive.inventory(self.project)
        self.assertTrue(report["ready"], report)
        self.assertTrue(self.archive.contains("X", "known-only"))
        with self.archive.db:
            result = self.archive.index_record(self.post("new-id", text="known content"), "worker", save=True)
        self.assertEqual(result, "SKIPPED_EXISTING")
        self.assertEqual(self.count("posts"), 0)

    def test_inventory_uses_post_table_identity_columns_when_json_omits_them(self):
        connection = sqlite3.connect(self.project / "posts.sqlite")
        connection.execute("CREATE TABLE posts(platform TEXT, post_id TEXT, json TEXT)")
        content = self.post("column-id")
        del content["platform"]
        del content["post_id"]
        connection.execute("INSERT INTO posts VALUES(?,?,?)", ("X", "column-id", json.dumps(content)))
        connection.commit()
        connection.close()
        report = self.archive.inventory(self.project)
        self.assertTrue(report["ready"], report)
        self.assertTrue(self.archive.contains("X", "column-id"))

    def test_inventory_classifies_derived_telegram_ids_but_blocks_unknown_namespaces(self):
        path = self.project / "derived.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE filtered_posts(post_id TEXT, fingerprint TEXT, record_json TEXT)")
        connection.execute("INSERT INTO filtered_posts VALUES('public_web:example:1','digest',NULL)")
        connection.commit()
        self.assertTrue(self.archive.inventory(self.project)["ready"])
        connection.execute("INSERT INTO filtered_posts VALUES('twitter:example:2','digest',NULL)")
        connection.commit()
        connection.close()
        report = self.archive.inventory(self.project)
        self.assertFalse(report["ready"])
        self.assertEqual(report["unrecognized"][0]["table"], "filtered_posts")


if __name__ == "__main__":
    unittest.main()

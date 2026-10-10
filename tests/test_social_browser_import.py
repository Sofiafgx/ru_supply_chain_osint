import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from social_browser_import import export_browser, import_batch, setup
from social_collection import SocialArchive


class BrowserImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = SocialArchive(self.root / "archive")
        setup(self.archive)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def post(self, identifier="123", platform="X", **changes):
        value = {"platform": platform, "post_id": identifier, "account_handle": "@sample", "text": "original " + identifier,
                 "timestamp": "2024-01-02T00:00:00Z", "source_mode": "browser_dom", "text_complete": True,
                 "url": "https://x.com/sample/status/" + identifier if platform == "X" else "https://www.threads.net/@sample/post/" + identifier}
        value.update(changes)
        return value

    def batch(self, posts, name="batch.json", **changes):
        value = {"posts": posts, "observations": [{"platform": "X", "account_handle": "@sample", "url": "https://x.com/sample",
                 "status": "partial_capture", "detail": "One browser screen inspected"}], "checkpoint": {"next_url": "https://x.com/sample", "page": 1}}
        value.update(changes)
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def count(self, table):
        return self.archive.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_repeated_batch_and_duplicate_records_are_idempotent(self):
        path = self.batch([self.post(), self.post()])
        first = import_batch(self.archive, path)
        repeated = import_batch(self.archive, path)
        self.assertEqual((first["new_posts"], first["skipped_existing"]), (1, 1))
        self.assertTrue(repeated["already_imported"])
        self.assertEqual(self.count("posts"), 1)
        self.assertEqual(self.count("browser_batches"), 1)
        self.assertEqual(self.count("browser_observations"), 1)
        self.assertEqual(self.count("browser_checkpoints"), 1)
        third = import_batch(self.archive, self.batch([self.post()], name="other.json", checkpoint={"page": 2}))
        self.assertEqual((third["new_posts"], third["skipped_existing"]), (0, 1))

    def test_rejects_truncated_text_or_missing_publication_time(self):
        for change in ({"text_complete": False}, {"text_truncated": True}, {"is_repost": True}, {"timestamp": None},
                       {"timestamp": "2024-01-02"}, {"source_mode": "search_snippet"}, {"text": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                import_batch(self.archive, self.batch([self.post(**change)]))
        self.assertEqual(self.count("posts"), 0)
        self.assertEqual(self.count("browser_checkpoints"), 0)

    def test_permalink_rejects_wrong_author_id_or_unofficial_host(self):
        for url in ("https://x.com/other/status/123", "https://x.com/sample/status/999", "https://x.com.evil.test/sample/status/123",
                    "http://x.com/sample/status/123", "https://name@x.com/sample/status/123"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                import_batch(self.archive, self.batch([self.post(url=url)]))

    def test_platform_namespaces_and_threads_shortcodes(self):
        import_batch(self.archive, self.batch([self.post(), self.post(platform="Threads"), self.post(identifier="D_A-b1", platform="Threads")]))
        self.assertEqual(self.count("posts"), 3)
        self.assertTrue(self.archive.contains("X", "123"))
        self.assertTrue(self.archive.contains("Threads", "123"))
        self.assertTrue(self.archive.contains("Threads", "D_A-b1"))

    def test_requested_start_filters_exports_but_preserves_archive_index(self):
        import_batch(self.archive, self.batch([
            self.post("1", timestamp="2021-12-31T23:59:59Z"),
            self.post("2", timestamp="2022-01-01T00:00:00Z"),
            self.post("3", timestamp="2024-01-01T00:00:00Z")]))
        (self.archive.directory / "browser_scope.json").write_text(
            json.dumps({"start": "2022-01-01T00:00:00Z"}), encoding="utf-8")
        report = export_browser(self.archive)
        self.assertEqual(report["saved_browser_posts"], 2)
        self.assertEqual(report["accounts"][0]["saved_posts"], 2)
        rows = [json.loads(line) for line in (self.archive.directory / "browser_posts.jsonl").read_text().splitlines()]
        self.assertEqual([row["post_id"] for row in rows], ["2", "3"])
        self.assertEqual(self.count("posts"), 3)
        self.assertTrue(self.archive.contains("X", "1"))

    def test_existing_api_post_and_changed_id_content_are_skipped(self):
        old = self.post(source_mode="api", author_id="42")
        with self.archive.db:
            self.archive.index_record(old, "existing_api", save=True)
        receipt = import_batch(self.archive, self.batch([self.post(), self.post("456", text="original 123")]))
        self.assertEqual((receipt["new_posts"], receipt["skipped_existing"]), (0, 2))
        self.assertEqual(self.count("posts"), 1)
        self.assertEqual(export_browser(self.archive)["saved_browser_posts"], 0)

    def test_checkpoint_and_all_rows_rollback_on_database_error(self):
        original = self.archive.index_record
        calls = []

        def fail_second(record, origin, save=False):
            calls.append(record)
            if len(calls) == 2:
                raise RuntimeError("simulated write failure")
            return original(record, origin, save)

        with patch.object(self.archive, "index_record", side_effect=fail_second), self.assertRaises(RuntimeError):
            import_batch(self.archive, self.batch([self.post(), self.post("456")]))
        for table in ("posts", "existing_ids", "existing_content", "browser_batches", "browser_observations", "browser_checkpoints"):
            self.assertEqual(self.count(table), 0, table)

    def test_bad_checkpoint_and_bad_observation_cannot_commit_posts(self):
        for changes in ({"checkpoint": "invalid"}, {"observations": [{"platform": "X", "account_handle": "@sample", "url": "https://x.com/sample", "status": "complete", "detail": "claim"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                import_batch(self.archive, self.batch([self.post()], **changes))
        self.assertEqual(self.count("posts"), 0)

    def test_exports_browser_only_and_preserves_api_jobs_and_coverage(self):
        coverage = self.archive.directory / "coverage.json"
        coverage.write_bytes(b"original API coverage")
        self.archive.plan_account({"platform": "X", "account_handle": "@sample", "created_at": "2024-01-01T00:00:00Z"},
                                  "2024-02-01T00:00:00Z", self.archive.index_digest())
        before = [tuple(row) for row in self.archive.db.execute("SELECT * FROM jobs")]
        import_batch(self.archive, self.batch([self.post()]))
        import_batch(self.archive, self.batch([], name="second.json", checkpoint={"page": 2}, observations=[{
            "platform": "X", "account_handle": "@sample", "url": "https://x.com/sample", "status": "blocked_login", "detail": "Login wall observed"}]))
        report = export_browser(self.archive)
        self.assertEqual(report["latest_checkpoint"], {"page": 2})
        self.assertEqual(report["accounts"][0]["status"], "blocked_login")
        self.assertEqual(report["accounts"][0]["saved_posts"], 1)
        self.assertEqual(report["completeness"], "not_verified")
        self.assertEqual(coverage.read_bytes(), b"original API coverage")
        self.assertEqual([tuple(row) for row in self.archive.db.execute("SELECT * FROM jobs")], before)
        self.assertEqual(len((self.archive.directory / "browser_posts.jsonl").read_text().splitlines()), 1)
        self.assertTrue((self.archive.directory / "browser_posts.csv").exists())

    def test_streamed_exports_exclude_api_posts_and_group_browser_counts(self):
        with self.archive.db:
            self.archive.index_record(self.post("999", source_mode="api"), "api", save=True)
        import_batch(self.archive, self.batch([self.post(), self.post("456", account_handle="@SAMPLE")]))
        report = export_browser(self.archive)
        self.assertEqual(report["saved_browser_posts"], 2)
        self.assertEqual(report["accounts"][0]["saved_posts"], 2)
        jsonl = self.archive.directory / "browser_posts.jsonl"
        self.assertEqual({json.loads(line)["post_id"] for line in jsonl.read_text().splitlines()}, {"123", "456"})
        with (self.archive.directory / "browser_posts.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual({row["post_id"] for row in csv.DictReader(handle)}, {"123", "456"})

    def test_export_write_failure_preserves_previous_files_and_cleans_temporary_files(self):
        import_batch(self.archive, self.batch([self.post()]))
        export_browser(self.archive)
        paths = [self.archive.directory / name for name in ("browser_posts.jsonl", "browser_posts.csv")]
        before = [path.read_bytes() for path in paths]
        import_batch(self.archive, self.batch([self.post("456")], name="second.json"))
        with patch("social_browser_import.csv.DictWriter.writerow", side_effect=OSError("disk write failure")), self.assertRaises(OSError):
            export_browser(self.archive)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(list(self.archive.directory.glob(".browser_posts.*")), [])
        export_browser(self.archive)
        self.assertEqual(len(paths[0].read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()

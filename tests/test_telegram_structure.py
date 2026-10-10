import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analytics.telegram_structure import StructuredArchive, date_mentions, entity_mentions, extract_post


def post(text, identifier=1):
    return {"source_mode": "public_web", "channel_username": "ExampleChannel",
            "message_id": identifier, "text": text, "date": "2026-10-08T22:00:00+00:00",
            "url": f"https://t.me/ExampleChannel/{identifier}", "reply_preview": {"text": "ИНН 7707083893"}}


class ExtractionTests(unittest.TestCase):
    def test_dates_have_explicit_year_and_relative_kyiv_anchor(self):
        items = date_mentions("8 жовтня, 7 октября 2024; 31.02.2026; вчера; 2027-01-02", "2026-10-08T22:00:00+00:00")
        by_raw = {item["raw"]: item for item in items}
        self.assertIsNone(by_raw["8 жовтня"]["normalized_date"])
        self.assertEqual(by_raw["7 октября 2024"]["normalized_date"], "2024-10-07")
        self.assertEqual(by_raw["31.02.2026"]["warning"], "invalid_date")
        self.assertEqual(by_raw["вчера"]["normalized_date"], "2026-10-08")
        self.assertTrue(by_raw["2027-01-02"]["later_than_publication"])
        self.assertTrue(all(not item["event_date_verified"] for item in items))

    def test_labeled_ids_not_arbitrary_digits_and_no_inferred_hs(self):
        items = entity_mentions('ООО «Пример» ИНН 7707083893; ИНН 1234567890; заказ 7707083893; HS 8542; HS 12345; модель STM32F103; станки Haas')
        inns = [item for item in items if item["kind"] == "inn"]
        self.assertEqual(len(inns), 2)
        self.assertTrue(inns[0]["checksum_valid"])
        self.assertFalse(inns[1]["checksum_valid"])
        self.assertEqual([item["value"] for item in items if item["kind"] == "hs"], ["8542"])
        self.assertEqual([item["value"] for item in items if item["kind"] == "part_number"], ["STM32F103"])
        self.assertFalse(any(item["kind"] == "hs" for item in entity_mentions("микросхемы")))

    def test_unverified_claims_do_not_become_metrics_or_identity_links(self):
        record = extract_post(post('ООО «Пример» не восстановило производство 01.10.2026. Завод планирует выпуск 100 шт. в день.'))
        self.assertTrue(record["industrial_context"])
        events = record["event_candidates"]
        self.assertTrue(events[0]["negation_present"])
        self.assertTrue(events[1]["prospective_language"])
        self.assertIsNone(events[0]["event_date"])
        self.assertIsNone(events[0]["entity_id"])
        self.assertTrue(all(not item["metric_eligible"] for item in events))
        self.assertTrue(all(value["value"] is None for value in record["metric_inputs"].values()))
        self.assertFalse(any(item["kind"] == "inn" for item in record["entity_mentions"]))

    def test_date_in_other_sentence_not_linked_and_duplicates_hash(self):
        first = extract_post(post('01.10.2026 открыли музей. Завод возобновил производство.'))
        recovery = [item for item in first["event_candidates"] if "recovery" in item["labels"]][0]
        self.assertEqual(recovery["date_mentions"], [])
        second = extract_post(post(first["text"], 2))
        self.assertEqual(first["text_sha256"], second["text_sha256"])
        self.assertNotEqual(first["post_id"], second["post_id"])

    def test_damage_components_and_context_are_mentions_not_affiliation(self):
        record = extract_post(post('Завод у Росії пошкоджено. Мікросхема STM32F103 для ОПК: запас 100 шт.'))
        self.assertTrue(record["russia_mentioned"])
        self.assertTrue(record["defence_industry_mentioned"])
        self.assertTrue(any("disruption" in item["labels"] for item in record["event_candidates"]))
        self.assertTrue(any(item["kind"] == "component_designation" and item["value"] == "STM32F103" for item in record["entity_mentions"]))
        self.assertTrue(all(item["semantic_role"] is None for event in record["event_candidates"] for item in event["quantity_mentions"]))


class ArchiveTests(unittest.TestCase):
    def test_live_source_not_modified_repeat_is_idempotent_and_edit_removes_old_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "source.sqlite3"
            db = sqlite3.connect(source)
            db.execute("CREATE TABLE web_posts(username TEXT COLLATE NOCASE, message_id INTEGER, record_json TEXT, PRIMARY KEY(username,message_id))")
            for record in (post('Завод возобновил производство 01.10.2026.'), post('Привіт', 2)):
                db.execute("INSERT INTO web_posts VALUES(?,?,?)", (record["channel_username"], record["message_id"], json.dumps(record)))
            db.commit()
            archive = StructuredArchive(folder / "structured.sqlite3")
            try:
                self.assertEqual(archive.process(source, batch_size=1), {"checked": 2, "updated": 2})
                self.assertEqual(archive.process(source), {"checked": 2, "updated": 0})
                summary = archive.export(folder / "output")
                self.assertEqual(summary["industrial_posts"], 1)
                self.assertEqual(summary["event_candidates"], 1)
                self.assertEqual(summary["verified_metric_values"], 0)
                db.execute("UPDATE web_posts SET record_json=? WHERE message_id=1", (json.dumps(post('Виправлено: це музей.')),))
                db.commit()
                self.assertEqual(archive.process(source)["updated"], 1)
                self.assertEqual(archive.export(folder / "output")["industrial_posts"], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0], 2)
                with self.assertRaises(ValueError):
                    archive.process(archive.path)
            finally:
                archive.close()
                db.close()


if __name__ == "__main__":
    unittest.main()

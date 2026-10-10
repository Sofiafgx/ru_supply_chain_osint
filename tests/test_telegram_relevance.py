import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analytics.telegram_relevance import ContextClassifier, EntityRegistry, filter_post
from telegram_filter_cli import FilterArchive

ROOT = Path(__file__).resolve().parents[1]


class RelevanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = EntityRegistry(ROOT/'config/russian_defence_entities.json')
        cls.classifier = ContextClassifier()

    def classify(self, text):
        return filter_post({"text": text}, self.registry, self.classifier)

    def test_strong_names_labelled_identifiers_and_unicode_offsets(self):
        text = 'АТ «ВНІІР–Прогрес»; АО «Воткинский завод»; ІПН: 2130094170; ОГРН 1035003364021'
        matches, products = self.registry.match(text)
        self.assertEqual({m["entity_id"] for m in matches}, {"vniir_progress", "votkinsk_plant", "ktrv"})
        self.assertTrue(any(m["kind"] == "inn" for m in matches))
        self.assertTrue(any(m["kind"] == "ogrn" for m in matches))
        self.assertTrue(all(text[m["start"]:m["end"]] == m["raw"] for m in matches))
        self.assertTrue(all(not m["legal_identity_verified"] for m in matches))

    def test_confusable_entities_and_arbitrary_numbers_not_merged(self):
        for text in ('ВНИИРТ производит радары.', 'АО «Воткинский завод теплоизоляционных материалов» выпускает плиты.', 'Номер заказа 2130094170.'):
            with self.subTest(text=text):
                matches, _ = self.registry.match(text)
                self.assertFalse(any(m["kind"] in ("inn", "name") for m in matches))
                self.assertNotEqual(self.classify(text)["decision"], "entity_and_context_candidate")

    def test_positive_dependency_links_in_both_languages(self):
        for text in ('ВНИИР-Прогресс производит антенны Комета-М.', 'АО «Воткинский завод» поставил станки ЧПУ.', 'ВНІІР-Прогрес виробляє антени Комета-М.', 'VNIIR-Progress manufactures Kometa-M antennas.'):
            with self.subTest(text=text):
                result = self.classify(text)
                self.assertEqual(result["decision"], "entity_and_context_candidate")
                self.assertTrue(result["context"]["relations"])
                self.assertTrue(all(not item["relation_verified"] for item in result["context"]["relations"]))
                self.assertFalse(result["metric_eligible"])

    def test_other_subject_and_other_sentence_do_not_form_entity_action_link(self):
        for text in ('ВНИИР-Прогресс поздравил сотрудников, а Siemens поставил станки в Германию.',
                     'ВНИИР-Прогресс поздравил сотрудников. Siemens поставил станки в Германию.'):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text)["decision"], "entity_match_review")

    def test_negation_plans_advertising_and_identifier_conflict_need_review(self):
        texts = ['ВНИИР-Прогресс не производит микросхемы.',
                 'ВНИИР-Прогресс планирует производить микросхемы.',
                 'Реклама: ВНИИР-Прогресс производит микросхемы.',
                 'ВНИИР-Прогресс ИНН 7707083893 производит микросхемы.']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(self.classify(text)["decision"], "entity_match_review")

    def test_zone_product_and_unknown_defence_actor_not_assigned_to_legal_operator(self):
        for text in ('В ОЭЗ «Алабуга» собирают БПЛА.', 'Алабуга производит БПЛА.'):
            result = self.classify(text)
            self.assertEqual(result["decision"], "entity_match_review")
            self.assertTrue(all(not m["legal_identity_verified"] for m in result["entity_matches"]))
        result = self.classify('Российский оборонный завод производит микросхемы.')
        self.assertEqual(result["decision"], "context_only_review")
        self.assertTrue(result["context"]["relations"])
        result = self.classify('Антенны Комета-М обнаружены в аппарате.')
        self.assertEqual(result["decision"], "context_only_review")
        self.assertFalse(result["product_matches"][0]["manufacturer_assigned"])

    def test_dual_use_keywords_and_foreign_factory_not_sufficient(self):
        for text in ('Siemens производит станки ЧПУ в Германии.', 'Российский мебельный завод выпускает платы.', 'В США предприятие собирает БПЛА.'):
            self.assertEqual(self.classify(text)["decision"], "no_match")
        self.assertEqual(self.classify('VNIIR-Progress might manufacture Kometa-M antennas.')["decision"], "entity_match_review")
        self.assertEqual(self.classify('Воткинский завод производит газовое оборудование для гражданского рынка.')["decision"], "entity_match_review")

    def test_legacy_period_production_requires_review_without_keyword_history(self):
        for text in ('В 1964 году Воткинский завод выпускал ракеты.',
                     'У 1964 році Воткінський завод виробляв ракети.',
                     'In 1964 Votkinsk Plant manufactured missiles.',
                     'В СССР Воткинский завод выпускал ракеты.'):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text)["decision"], "entity_match_review")
        self.assertEqual(self.classify('В 2025 году Воткинский завод выпускал ракеты.')["decision"], "entity_and_context_candidate")

    def test_full_raw_archive_and_incremental_edit_remove_previous_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder/'source.sqlite3'
            db = sqlite3.connect(source)
            db.execute('CREATE TABLE web_posts(username TEXT COLLATE NOCASE, message_id INTEGER, record_json TEXT, PRIMARY KEY(username,message_id))')
            record = {"source_mode": "public_web", "channel_username": "Example", "message_id": 1,
                      "text": 'ВНИИР-Прогресс производит антенны Комета-М.', "date": "2026-10-09T00:00:00+00:00", "url": "https://t.me/Example/1"}
            db.execute('INSERT INTO web_posts VALUES(?,?,?)', ('Example', 1, json.dumps(record)))
            db.commit()
            archive = FilterArchive(folder/'filter.sqlite3')
            try:
                self.assertEqual(archive.process(source,self.registry,self.classifier), {"checked": 1, "updated": 1})
                self.assertEqual(archive.process(source,self.registry,self.classifier), {"checked": 1, "updated": 0})
                summary = archive.export(folder/'exports',self.registry,self.classifier,{"TTR": {"target_capacity_fraction": 1.0}})
                self.assertEqual(summary['decisions']['entity_and_context_candidate'],1)
                self.assertEqual(summary['verified_metric_values'],0)
                exported = json.loads((folder/'exports/entity_and_context_candidate.jsonl').read_text())
                self.assertEqual(exported['metric_inputs']['TTR']['target_capacity_fraction'],1.0)
                record['text'] = 'Виправлено: джерело відкликало повідомлення.'
                db.execute('UPDATE web_posts SET record_json=?', (json.dumps(record),));db.commit()
                self.assertEqual(archive.process(source,self.registry,self.classifier)['updated'],1)
                self.assertEqual(archive.export(folder/'exports',self.registry,self.classifier,{})['decisions']['entity_and_context_candidate'],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM web_posts').fetchone()[0],1)
                with self.assertRaises(ValueError):
                    archive.process(archive.path,self.registry,self.classifier)
            finally:
                archive.close();db.close()


if __name__ == "__main__":
    unittest.main()

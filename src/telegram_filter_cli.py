"""Filter the complete Telegram archive with registry matching and local NLP."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analytics.telegram_relevance import ContextClassifier, EntityRegistry, VERSION, filter_post
from analytics.telegram_structure import extract_post

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)
DECISIONS = ("entity_and_context_candidate", "entity_match_review", "context_only_review", "no_match")


class FilterArchive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS filtered_posts(
                post_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                decision TEXT NOT NULL,
                record_json TEXT
            );
            CREATE INDEX IF NOT EXISTS filtered_decision ON filtered_posts(decision);
        """)

    def close(self):
        self.connection.close()

    def process(self, path, registry, classifier, batch_size=500):
        path = Path(path).resolve()
        if path == self.path:
            raise ValueError("Результат не може замінити архів джерела")
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            names = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "web_posts" in names:
                table, column, cursor = "web_posts", "username", ("", 0)
            elif "messages" in names:
                table, column, cursor = "messages", "channel_id", (-9223372036854775808, 0)
            else:
                raise ValueError("Потрібен веб- або MTProto-архів Telegram")
            model_key = json.dumps(classifier.metadata, sort_keys=True)
            checked = updated = 0
            while True:
                rows = source.execute(f"SELECT {column},message_id,record_json FROM {table} WHERE ({column},message_id)>(?,?) ORDER BY {column},message_id LIMIT ?", (*cursor, batch_size)).fetchall()
                if not rows:
                    break
                with self.connection:
                    for channel, message_id, raw in rows:
                        record = json.loads(raw)
                        mode = record.get("source_mode") or "mtproto"
                        key = str(record.get("channel_id") or (record.get("channel_username") or "").lower())
                        post_id = f"{mode}:{key}:{message_id}"
                        fingerprint = hashlib.sha256((VERSION + registry.sha256 + model_key + raw).encode()).hexdigest()
                        checked += 1
                        old = self.connection.execute("SELECT fingerprint FROM filtered_posts WHERE post_id=?", (post_id,)).fetchone()
                        if old and old[0] == fingerprint:
                            continue
                        result = filter_post(record, registry, classifier)
                        decision = result["decision"]
                        enriched = None
                        if decision != "no_match":
                            enriched = extract_post(record)
                            enriched["relevance_filter"] = result
                        self.connection.execute("INSERT INTO filtered_posts VALUES(?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET fingerprint=excluded.fingerprint,decision=excluded.decision,record_json=excluded.record_json",
                                                (post_id, fingerprint, decision, json.dumps(enriched, ensure_ascii=False) if enriched else None))
                        updated += 1
                cursor = rows[-1][:2]
                if checked % 10000 == 0:
                    LOG.info("Перевірено %s дописів", checked)
            return {"checked": checked, "updated": updated}
        finally:
            source.close()

    def export(self, folder, registry, classifier, methodology):
        folder = Path(folder).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        fields = ["channel", "message_id", "source_url", "published_at", "decision", "matched_reference_subjects", "match_kinds", "reason", "blockers", "nlp_language", "relation_count", "text", "metric_eligible"]
        handles, temporary, writers = {}, [], {}
        counts, subjects = Counter(), Counter()
        examples = []
        unique_hashes = set()
        try:
            for decision in DECISIONS[:-1]:
                for suffix in ("csv", "jsonl"):
                    name = f"{decision}.{suffix}"
                    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if suffix == "csv" else "utf-8", newline="", dir=folder, delete=False)
                    handles[name] = handle
                    temporary.append((Path(handle.name), folder/name))
                    if suffix == "csv":
                        writers[name] = csv.DictWriter(handle, fieldnames=fields)
                        writers[name].writeheader()
            for decision, raw in self.connection.execute("SELECT decision,record_json FROM filtered_posts WHERE decision!='no_match' ORDER BY post_id"):
                record = json.loads(raw)
                result = record["relevance_filter"]
                counts[decision] += 1
                handles[f"{decision}.jsonl"].write(json.dumps(record, ensure_ascii=False) + "\n")
                matched = {item["reference_name"] for item in result["entity_matches"]}
                flat = {"channel": record["channel_username"], "message_id": record["message_id"],
                        "source_url": record["source_url"], "published_at": record["published_at"], "decision": decision,
                        "matched_reference_subjects": "; ".join(sorted(matched)),
                        "match_kinds": "; ".join(sorted({item["kind"] for item in result["entity_matches"]})),
                        "reason": result["reason"], "blockers": "; ".join(result["blockers"]),
                        "nlp_language": result["context"]["language"], "relation_count": len(result["context"]["relations"]),
                        "text": record["text"], "metric_eligible": False}
                for key, value in flat.items():
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                        flat[key] = "'" + value
                writers[f"{decision}.csv"].writerow(flat)
                if decision == "entity_and_context_candidate":
                    unique_hashes.add(record["text_sha256"])
                    subjects.update(matched)
                    if len(examples) < 6:
                        examples.append({"source_url": record["source_url"], "subjects": sorted(matched),
                                         "relations": result["context"]["relations"][:2]})
            for handle in handles.values():
                handle.close()
            for temp, target in temporary:
                temp.replace(target)
            totals = dict(self.connection.execute("SELECT decision,COUNT(*) FROM filtered_posts GROUP BY decision"))
            summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "checked_posts": sum(totals.values()),
                       "decisions": {decision: totals.get(decision, 0) for decision in DECISIONS},
                       "selected_unique_texts": len(unique_hashes), "selected_reference_subject_mentions": dict(subjects),
                       "reference_entity_count": len(registry.entities), "registry_sha256": registry.sha256,
                       "registry_scope": registry.data["scope"], "nlp_models": classifier.metadata,
                       "classifier_version": VERSION, "classifier_accuracy_measured": False,
                       "verified_metric_values": 0, "examples": examples,
                       "limitations": ["reference_set_not_complete", "no_match_is_not_verified_other_sector", "all_event_claims_unverified", "dependency_links_require_review", "ru_uk_en_models_only", "no_geographic_entity_merging", "crossposts_not_independent_confirmation"]}
            (folder/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder/"entity_reference.json").write_text(json.dumps(registry.data, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder/"methodology.json").write_text(json.dumps(methodology, ensure_ascii=False, indent=2), encoding="utf-8")
            write_report(folder, summary)
            return summary
        finally:
            for handle in handles.values():
                handle.close()
            for temp, target in temporary:
                temp.unlink(missing_ok=True)


def write_report(folder, summary):
    n = lambda value: f"{value:,}".replace(",", " ")
    stamp = datetime.fromisoformat(summary["generated_at"]).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
    counts = summary["decisions"]
    lines = ["# Відбір дописів про ВПК РФ", "", f"Знімок обробки: {stamp}, Київ. Перевірено {n(summary['checked_posts'])} дописів із повного локального архіву, включно з раніше не відібраними.", "",
             "Вебархів має обмеження Telegram і продовжує поповнюватися. Це обсяг перевірених збережених записів, а не підтвердження повної історії каналів від дня створення.", "",
             "| Група | Дописів | Що це означає |", "|---|---:|---|",
             f"| Збіг суб'єкта та виробничого контексту | {n(counts['entity_and_context_candidate'])} | Назва/ідентифікатор із довідника та синтаксичний зв'язок з дією й компонентом; твердження не перевірене |",
             f"| Згадка суб'єкта для перевірки | {n(counts['entity_match_review'])} | Назва є, але контекст неоднозначний, рекламний, історичний або зв'язок недостатній |",
             f"| Контекст без встановленого суб'єкта | {n(counts['context_only_review'])} | Можлива релевантність; потрібна ідентифікація та розширення довідника |",
             f"| Сигнал не виявлений | {n(counts['no_match'])} | Це не доведене віднесення до іншої країни/цивільного сектору |", "",
             f"В основній групі {n(summary['selected_unique_texts'])} різних текстів. Повтори дописів залишені з окремим походженням, а не вважаються незалежними підтвердженнями.", "",
             "## Файли", "",
             f"- [Збіг суб'єкта та контексту]({folder/'entity_and_context_candidate.csv'}).",
             f"- [Згадки суб'єктів для перевірки]({folder/'entity_match_review.csv'}).",
             f"- [Контекст без встановленого суб'єкта]({folder/'context_only_review.csv'}).",
             f"- [Довідник із джерелами]({folder/'entity_reference.json'}).",
             f"- [Погоджені умови метрик]({folder/'methodology.json'}).", "",
             "Кожна група також має JSONL: у ньому видно збіги, уривки речень і синтаксичні шляхи, за якими класифікатор пов'язав суб'єкта, дію й компонент.", "",
             "## Як виконано відбір", "",
             "Перший крок зіставляє явно підписані ІНН/ОГРН та задані варіанти назв. Зіставлення враховує лапки, дефіси, регістр і межі слів. "
             "ВНИИРТ не є ВНИИР-Прогресс. Місто Воткінськ, однойменний завод теплоізоляції, окремі резиденти Алабуги й дочірні підприємства холдингів не об'єднуються автоматично.", "",
             "Другий крок використовує місцеві мовні моделі spaCy для української, російської й англійської: леми, межі речень та дерево граматичних залежностей. "
             "Для основної групи потрібен короткий синтаксичний зв'язок суб'єкт — виробнича дія — компонент/обладнання. "
             "Сусідні речення та дії іншого явного підмета не зв'язуються. Заперечення, умовні плани, явний цивільний контекст і реклама переводять відповідний запис на перевірку.", "",
             "Це пояснюваний класифікатор на базі мовної моделі й правил; він не навчений і не виміряний на розміченій вибірці цього дослідження. "
             "Точність на Telegram-текстах поки не встановлена. Мова визначається евристично: змішані тексти й інші латинкові мови можуть помилково отримати англійську модель. "
             "Для текстів, яким не призначена мовна модель, граматичний висновок не робиться; наявні сигнали зберігаються для перевірки.", "",
             "## Охоплення довідника", "",
             "Початковий довідник містить чотири названі користувачем суб'єкти: оператор ОЕЗ «Алабуга», АО «Воткинский завод», «ВНИИР-Прогресс» і КТРВ. "
             "Ідентичність і оборонний зв'язок мають окремі посилання на першоджерела. Це не повний реєстр ВПК РФ. "
             "Позначення Комета-М і Герань-2 є сигналами номенклатури; вони самі по собі не визначають виробника конкретної партії. "
             "Широкі HS-коди, ЧПУ або бренд електроніки самі по собі не доводять військове призначення.", "",
             "## Метрики", "",
             "TTR потребує підтвердженого повернення до 100% визначеної доаварійної потужності того самого процесу. "
             "Для базового TTS буфер S_safe має бути доступним для споживання; добове споживання визначається узгодженою BOM і виробничою нормою. "
             "Мережевий TTS та REI потребують окремої моделі. У цьому відборі значення метрик не розраховані, усі твердження про події залишаються неперевіреними.", "",
             "[MIT: TTR/TTS і модель REI](https://news.mit.edu/2022/companies-use-mit-research-identify-respond-supply-chain-risks-0615).", ""]
    (folder/"report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Двокроковий відбір Telegram: довідник і контекстний NLP")
    parser.add_argument("--source", type=Path, default=ROOT/"data/telegram_public/archive.sqlite3")
    parser.add_argument("--registry", type=Path, default=ROOT/'config/russian_defence_entities.json')
    parser.add_argument("--output", type=Path, default=ROOT/"data/telegram_filtered")
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error("Архів джерела не знайдений")
    registry = EntityRegistry(args.registry)
    classifier = ContextClassifier()
    methodology = json.loads((ROOT/'config/research_methodology.json').read_text())
    archive = FilterArchive(args.output/"filtered.sqlite3")
    try:
        run = archive.process(args.source, registry, classifier)
        summary = archive.export(args.output, registry, classifier, methodology)
        print(json.dumps({"run": run, "checked_posts": summary["checked_posts"], "decisions": summary["decisions"]}, ensure_ascii=False))
    finally:
        archive.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    main()

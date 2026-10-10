"""Відтворюваний витяг згадок із Telegram; результати потребують перевірки."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "4"
KYIV = ZoneInfo("Europe/Kyiv")

MONTHS = {
    "января": 1, "січня": 1, "февраля": 2, "лютого": 2,
    "марта": 3, "березня": 3, "апреля": 4, "квітня": 4,
    "мая": 5, "травня": 5, "июня": 6, "червня": 6,
    "июля": 7, "липня": 7, "августа": 8, "серпня": 8,
    "сентября": 9, "вересня": 9, "октября": 10, "жовтня": 10,
    "ноября": 11, "листопада": 11, "декабря": 12, "грудня": 12,
}
DATE_PATTERNS = (
    ("iso_day", re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")),
    ("numeric_day", re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")),
    ("named_day", re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4})(?!\d))?\b", re.I)),
    ("relative_day", re.compile(r"\b(?:вчера|вчора|сегодня|сьогодні|позавчера|позавчора|завтра)\b", re.I)),
)

# Широкі класи є згадками, а не HS-кодами чи доведеними товарними позиціями.
EQUIPMENT = {
    "cnc": r"\b(?:ЧПУ|CNC)\b",
    "machine_tool": r"\b(?:станк\w*|верстат\w*|металлообработ\w*|металооброб\w*)",
    "electronics": r"\b(?:микроэлектрон\w*|мікроелектрон\w*|микросхем\w*|мікросхем\w*|микроконтроллер\w*|мікроконтролер\w*|полупроводник\w*|напівпровідник\w*|semiconductor\w*|microchip\w*)",
    "composites": r"\b(?:композит\w*|углеволокн\w*|вуглеволокн\w*|carbon\s+fiber\w*)",
    "industrial_equipment": r"\b(?:промышленн\w*\s+оборудован\w*|промислов\w*\s+обладнан\w*|industrial\s+equipment)",
}
EVENTS = {
    "disruption": r"\b(?:приостанов\w*|остановлен\w*|останов\w*\s+(?:производств\w*|работ\w*)|зупин\w*\s+(?:виробництв\w*|робот\w*)|поврежд\w*|пошкодж\w*|пожар\w*|пожеж\w*|дефицит\w*|дефіцит\w*|shortage\w*|disrupt\w*)",
    "recovery": r"\b(?:возобнов\w*|восстанов\w*|віднов\w*|resum\w*|restor\w*)",
    "delivery": r"\b(?:поставк\w*|поставил\w*|поставлен\w*|постачан\w*|постачил\w*|постачає\w*|получил\w*|отримал\w*|отгруз\w*|відвантаж\w*|deliver\w*|shipment\w*)",
    "production": r"\b(?:производств\w*|виробництв\w*|изготов\w*|виготов\w*|выпуск\w*|випуск\w*|production\w*|manufactur\w*)",
    "inventory": r"\b(?:запас\w*|остатк\w*|залишк\w*|stock\w*|inventory)",
    "consumption": r"\b(?:потреблен\w*|споживан\w*|расход\w*|consumption)",
}
EQUIPMENT = {key: re.compile(value, re.I) for key, value in EQUIPMENT.items()}
EVENTS = {key: re.compile(value, re.I) for key, value in EVENTS.items()}
INDUSTRY = re.compile(r"\b(?:завод\w*|предприят\w*|підприємств\w*|фабрик\w*|factory|factories|plant|supply\s+chain|ланцюг\w*\s+постачан\w*)", re.I)
LEGAL_NAME = re.compile(r'\b(?:ООО|АО|ПАО|ОАО|ЗАО|ФГУП|ТОВ|ПрАТ|АТ)\s*[«"“]([^»"”\n]{2,150})[»"”]', re.I)
FACILITY_NAME = re.compile(r'\b(?:завод\w*|предприят\w*|підприємств\w*|компан\w*|корпорац\w*)\s*[«"“]([^»"”\n]{2,150})[»"”]', re.I)
BRANDS = re.compile(r"\b(?:DMG\s+Mori|Haas|Fanuc|Siemens|Texas\s+Instruments|Analog\s+Devices|STMicroelectronics|Xilinx|Intel|AMD|NVIDIA|Ростех|Росэлектроника|Роселектроніка|ОАК)\b", re.I)
ID_PATTERNS = {
    "inn": re.compile(r"\b(?:ИНН|ІПН|INN)\s*[:№#-]?\s*(\d{10}|\d{12})(?!\d)", re.I),
    "ogrn": re.compile(r"\b(?:ОГРН|OGRN)\s*[:№#-]?\s*(\d{13}|\d{15})(?!\d)", re.I),
    "hs": re.compile(r"\b(?:HS(?:\s*code)?|HTS|ТН\s*ВЭД|ТН\s*ЗЕД)\s*[:№#-]?\s*(\d{4,10})(?!\d)", re.I),
    "part_number": re.compile(r"\b(?:артикул|part\s*(?:number|no\.?)|p/n|модель|model)\s*[:№#-]?\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9_./-]{1,45})", re.I),
    "component_designation": re.compile(r"\b(?:микросхем\w*|мікросхем\w*|микроконтроллер\w*|мікроконтролер\w*|подшипник\w*|підшипник\w*|верстат\w*|станок|станка)\s+([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9_./-]{1,45})", re.I),
}
NEGATION = re.compile(r"\b(?:не|нет|ні|not|no)\b", re.I)
PROSPECTIVE = re.compile(r"\b(?:планир\w*|плану\w*|ожида\w*|очіку\w*|намерен\w*|має\s+намір|будет|будуть|will|planned|expected)\b", re.I)
QUANTITY = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(?:(тыс\.?|тис\.?|млн|million|thousand)\s*)?(тонн\w*|т\b|кг\b|kg\b|шт\.?|штук\w*|одиниц\w*|одиниць|units?\b|%)(?:\s*(?:в|на|per|/)\s*(сутки|день|добу|day|месяц|місяць|month|год|рік|year))?", re.I)


def publication_day(value):
    try:
        stamp = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return stamp.astimezone(KYIV).date() if stamp.tzinfo else None
    except ValueError:
        return None


def date_mentions(text, published_at):
    result = []
    base = publication_day(published_at)
    for kind, pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            normalized, warning = None, None
            try:
                if kind == "iso_day":
                    normalized = date(*map(int, match.groups())).isoformat()
                elif kind == "numeric_day":
                    day, month, year = map(int, match.groups())
                    normalized = date(year, month, day).isoformat()
                elif kind == "named_day":
                    day, month, year = match.groups()
                    # Рік не виводимо з дати публікації: текст може описувати стару подію.
                    if year:
                        normalized = date(int(year), MONTHS[month.lower()], int(day)).isoformat()
                    else:
                        warning = "year_missing"
                else:
                    delta = {"вчера": -1, "вчора": -1, "позавчера": -2, "позавчора": -2,
                             "сегодня": 0, "сьогодні": 0, "завтра": 1}[match.group().lower()]
                    normalized = (base + timedelta(days=delta)).isoformat() if base else None
                    warning = "relative_to_publication_kyiv_requires_review"
            except ValueError:
                warning = "invalid_date"
            result.append({"kind": kind, "raw": match.group(), "normalized_date": normalized,
                           "start": match.start(), "end": match.end(), "warning": warning,
                           "later_than_publication": bool(normalized and base and date.fromisoformat(normalized) > base),
                           "event_date_verified": False})
    return sorted(result, key=lambda item: item["start"])


def inn_checksum(value):
    digits = list(map(int, value))
    if len(digits) == 10:
        return sum(a * b for a, b in zip(digits[:9], (2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10 == digits[9]
    if len(digits) == 12:
        first = sum(a * b for a, b in zip(digits[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
        second = sum(a * b for a, b in zip(digits[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
        return first == digits[10] and second == digits[11]
    return False


def entity_mentions(text):
    result = []
    for kind, pattern in (("organization_name", LEGAL_NAME), ("facility_name", FACILITY_NAME),
                          ("brand_or_organization", BRANDS)):
        for match in pattern.finditer(text):
            result.append({"kind": kind, "value": match.group(), "start": match.start(),
                           "end": match.end(), "identity_verified": False})
    for kind, pattern in ID_PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            # Для моделі потрібна хоча б цифра; довільний опис не стає артикулом.
            if kind in ("part_number", "component_designation") and not re.search(r"\d", value):
                continue
            if kind == "hs" and len(value) not in (4, 6, 8, 10):
                continue
            result.append({"kind": kind, "value": value, "raw": match.group(),
                           "start": match.start(), "end": match.end(),
                           "checksum_valid": inn_checksum(value) if kind == "inn" else None,
                           "identity_verified": False})
    for kind, pattern in EQUIPMENT.items():
        for match in pattern.finditer(text):
            result.append({"kind": "equipment_class", "category": kind, "value": match.group(),
                           "start": match.start(), "end": match.end(), "identity_verified": False})
    return sorted(result, key=lambda item: (item["start"], item["kind"]))


def clauses(text):
    split = re.compile(r"[\n!?;]+|(?<=[\w»])\.(?=\s+[A-ZА-ЯІЇЄ])")
    start = 0
    for match in split.finditer(text):
        if text[start:match.start()].strip():
            yield start, match.start(), text[start:match.start()]
        start = match.end()
    if text[start:].strip():
        yield start, len(text), text[start:]


def extract_post(record):
    text = record.get("text") or ""
    dates = date_mentions(text, record.get("date"))
    entities = entity_mentions(text)
    relevant = bool(INDUSTRY.search(text) or any(item["kind"] == "equipment_class" for item in entities))
    source_mode = record.get("source_mode") or "mtproto"
    key = str(record.get("channel_id") or (record.get("channel_username") or "").lower())
    post_id = f"{source_mode}:{key}:{record['message_id']}"
    candidates = []
    if relevant:
        for start, end, clause in clauses(text):
            labels = [kind for kind, pattern in EVENTS.items() if pattern.search(clause)]
            if not labels:
                continue
            context_entities = [item for item in entities if start <= item["start"] < end]
            # Події без промислового контексту в цьому реченні залишаємо лише як контекст допису.
            if not (INDUSTRY.search(clause) or context_entities):
                continue
            nearby_dates = [item for item in dates if start <= item["start"] < end]
            candidates.append({
                "candidate_id": f"{post_id}:{start}", "post_id": post_id,
                "labels": labels, "snippet": clause.strip(), "start": start, "end": end,
                "date_mentions": nearby_dates, "entity_mentions": context_entities,
                "quantity_mentions": [{"raw": item.group(), "number_raw": item.group(1),
                                       "scale_raw": item.group(2), "unit_raw": item.group(3),
                                       "period_raw": item.group(4), "semantic_role": None}
                                      for item in QUANTITY.finditer(clause)],
                "negation_present": bool(NEGATION.search(clause)),
                "prospective_language": bool(PROSPECTIVE.search(clause)),
                "event_date": None, "entity_id": None, "verification_status": "unverified_channel_claim",
                "metric_eligible": False,
            })
    warnings = ["rule_based_mentions_require_review", "no_automatic_entity_or_event_date_linking"]
    if record.get("quality_warning") or record.get("preview_warning"):
        warnings.append("source_text_may_be_incomplete")
    return {"post_id": post_id, "source_mode": source_mode,
            "channel_username": record.get("channel_username"), "channel_id": record.get("channel_id"),
            "message_id": record["message_id"], "source_url": record.get("url"),
            "published_at": record.get("date"), "source_collected_at": record.get("collected_at"),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text": text, "forwarded_from": record.get("forwarded_from") or record.get("forward"),
            "industrial_context": relevant, "date_mentions": dates, "entity_mentions": entities,
            "russia_mentioned": bool(re.search(r"\b(?:Росс\w*|Росі\w*|РФ)\b", text, re.I)),
            "defence_industry_mentioned": bool(re.search(r"\b(?:ВПК|ОПК|оборонно\w*|оборонн\w*|гособоронзаказ\w*)", text, re.I)),
            "event_candidates": candidates, "warnings": warnings, "extractor_version": VERSION,
            "metric_inputs": {
                "TTR": {"value": None, "target_capacity_fraction": 1.0,
                        "missing": ["verified_entity", "disruption_date", "pre_disruption_capacity_and_units", "recovered_capacity_and_units", "full_recovery_date", "same_disruption_link"]},
                "TTS": {"value": None, "scenario": "fixed_BOM_consumption_no_replenishment",
                        "missing": ["verified_component_and_BOM", "inventory_as_of_date", "available_S_safe", "BOM_daily_component_consumption", "matching_units"]},
                "REI": {"value": None, "missing": ["agreed_methodology", "verified_network", "loss_model", "verified_TTR_TTS_inputs"]},
            }}


class StructuredArchive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS analyzed_posts (
                post_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                relevant INTEGER NOT NULL, record_json TEXT
            );
        """)

    def close(self):
        self.connection.close()

    def process(self, source_path, batch_size=500):
        source_path = Path(source_path).resolve()
        if source_path == self.path:
            raise ValueError("Архів джерела і результати мають бути різними файлами")
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        try:
            names = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "web_posts" in names:
                table, column = "web_posts", "username"
                cursor = ("", 0)
            elif "messages" in names:
                table, column = "messages", "channel_id"
                cursor = (-9223372036854775808, 0)
            else:
                raise ValueError("Файл не є підтримуваним архівом Telegram")
            checked = updated = 0
            while True:
                rows = source.execute(f"SELECT {column}, message_id, record_json FROM {table} "
                                      f"WHERE ({column},message_id)>(?,?) ORDER BY {column},message_id LIMIT ?",
                                      (*cursor, batch_size)).fetchall()
                if not rows:
                    break
                with self.connection:
                    for channel_key, message_id, raw in rows:
                        record = json.loads(raw)
                        fingerprint = hashlib.sha256((VERSION + raw).encode()).hexdigest()
                        mode = record.get("source_mode") or "mtproto"
                        key = str(record.get("channel_id") or (record.get("channel_username") or "").lower())
                        post_id = f"{mode}:{key}:{message_id}"
                        previous = self.connection.execute("SELECT fingerprint FROM analyzed_posts WHERE post_id=?", (post_id,)).fetchone()
                        checked += 1
                        if previous and previous[0] == fingerprint:
                            continue
                        extracted = extract_post(record)
                        relevant = extracted["industrial_context"]
                        self.connection.execute("INSERT INTO analyzed_posts VALUES(?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
                                                "fingerprint=excluded.fingerprint,relevant=excluded.relevant,record_json=excluded.record_json",
                                                (post_id, fingerprint, relevant, json.dumps(extracted, ensure_ascii=False) if relevant else None))
                        updated += 1
                cursor = rows[-1][:2]
            return {"checked": checked, "updated": updated}
        finally:
            source.close()

    def records(self):
        for row in self.connection.execute("SELECT record_json FROM analyzed_posts WHERE relevant=1 ORDER BY post_id"):
            yield json.loads(row[0])

    def export(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        fields = {
            "events.csv": ["channel", "post_id", "source_url", "published_at", "candidate_id", "labels", "snippet", "date_mentions", "entity_mentions", "quantity_mentions", "negation_present", "prospective_language", "verification_status", "metric_eligible"],
            "entities.csv": ["channel", "post_id", "source_url", "published_at", "kind", "value", "category", "raw", "start", "end", "checksum_valid", "identity_verified"],
            "dates.csv": ["channel", "post_id", "source_url", "published_at", "kind", "raw", "normalized_date", "start", "end", "warning", "later_than_publication", "event_date_verified"],
        }
        temporary = []
        counts = {"industrial_posts": 0, "event_candidates": 0, "entity_mentions": 0, "date_mentions": 0}
        labels, by_channel, examples, hashes, entity_kinds = {}, {}, [], {}, {}
        review_counts = {"dates_without_normalized_day": 0, "dates_requiring_review": 0,
                         "negation_candidates": 0, "prospective_candidates": 0,
                         "russia_and_defence_context_posts": 0}
        try:
            handles, writers = {}, {}
            for name in ["structured_posts.jsonl"] + list(fields):
                handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if name.endswith("csv") else "utf-8", newline="", dir=folder, delete=False)
                temporary.append((Path(handle.name), folder / name))
                handles[name] = handle
                if name in fields:
                    writers[name] = csv.DictWriter(handle, fieldnames=fields[name], extrasaction="ignore")
                    writers[name].writeheader()
            for record in self.records():
                handles["structured_posts.jsonl"].write(json.dumps(record, ensure_ascii=False) + "\n")
                channel = record["channel_username"] or str(record["channel_id"])
                counts["industrial_posts"] += 1
                by_channel[channel] = by_channel.get(channel, 0) + 1
                hashes[record["text_sha256"]] = hashes.get(record["text_sha256"], 0) + 1
                if record.get("russia_mentioned") and record.get("defence_industry_mentioned"):
                    review_counts["russia_and_defence_context_posts"] += 1
                for item in record["entity_mentions"]:
                    entity_kinds[item["kind"]] = entity_kinds.get(item["kind"], 0) + 1
                for item in record["date_mentions"]:
                    review_counts["dates_without_normalized_day"] += not bool(item["normalized_date"])
                    review_counts["dates_requiring_review"] += bool(item["warning"])
                common = {"channel": channel, **{key: record[key] for key in ("post_id", "source_url", "published_at")}}
                for field, name, count_key in (("event_candidates", "events.csv", "event_candidates"),
                                                ("entity_mentions", "entities.csv", "entity_mentions"),
                                                ("date_mentions", "dates.csv", "date_mentions")):
                    for item in record[field]:
                        flat = {**common, **item}
                        for key, value in flat.items():
                            if isinstance(value, (dict, list)):
                                flat[key] = json.dumps(value, ensure_ascii=False)
                            elif isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                                flat[key] = "'" + value
                        writers[name].writerow(flat)
                        counts[count_key] += 1
                for event in record["event_candidates"]:
                    review_counts["negation_candidates"] += event["negation_present"]
                    review_counts["prospective_candidates"] += event["prospective_language"]
                    for label in event["labels"]:
                        labels[label] = labels.get(label, 0) + 1
                    if len(examples) < 8 and event["date_mentions"] and event["entity_mentions"]:
                        examples.append({**common, "snippet": event["snippet"][:600],
                                         "dates": event["date_mentions"], "labels": event["labels"]})
            for handle in handles.values():
                handle.close()
            for temp, target in temporary:
                temp.replace(target)
            summary = {"generated_at": datetime.now(timezone.utc).isoformat(),
                       "analyzed_posts": self.connection.execute("SELECT COUNT(*) FROM analyzed_posts").fetchone()[0],
                       **counts, "labels": labels, "by_channel": by_channel,
                       "entity_kinds": entity_kinds, "review_counts": review_counts,
                       "repeated_text_groups": sum(count > 1 for count in hashes.values()),
                       "verified_metric_values": 0, "examples": examples,
                       "limitations": ["rule_based_candidate_extraction_not_exhaustive", "all_claims_unverified", "no_automatic_causal_links", "raw_archive_preserved", "no_metric_values_from_missing_inputs", "crossposts_not_independent_confirmation"]}
            (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            write_report(folder, summary)
            return summary
        finally:
            for handle in locals().get("handles", {}).values():
                handle.close()
            for temp, target in temporary:
                temp.unlink(missing_ok=True)


def write_report(folder, summary):
    folder = Path(folder).resolve()
    number = lambda value: f"{value:,}".replace(",", " ")
    stamp = datetime.fromisoformat(summary["generated_at"]).astimezone(KYIV).strftime("%d.%m.%Y %H:%M")
    lines = ["# Огляд структурованих даних Telegram", "", f"Стан обробки: {stamp}, за київським часом.", "",
             f"Опрацьовано **{number(summary['analyzed_posts'])} дописів**. Це знімок локального архіву під час проходу; основний збір продовжується.", "",
             "| Що виділено | Кількість | Як читати |", "|---|---:|---|",
             f"| Дописи з промисловим контекстом | {number(summary['industrial_posts'])} | Широкий словниковий відбір, включає інші країни й історичні приклади |",
             f"| Кандидати на події | {number(summary['event_candidates'])} | Речення/абзаци зі згадкою події; твердження ще не перевірені |",
             f"| Згадки суб'єктів та обладнання | {number(summary['entity_mentions'])} | Згадки, а не кількість унікальних підприємств |",
             f"| Згадки дат | {number(summary['date_mentions'])} | Дата в тексті не обов'язково є датою події |",
             "| Підтверджені значення TTR/TTS/REI | 0 | Відсутні достатні перевірені входи |", "",
             f"Без однозначного календарного дня: {number(summary['review_counts']['dates_without_normalized_day'])} згадок дат. "
             f"Кандидати із запереченням: {number(summary['review_counts']['negation_candidates'])}; "
             f"із мовою планів/очікувань: {number(summary['review_counts']['prospective_candidates'])}.", "",
             "## Таблиці для перегляду", "",
             f"- [Події]({folder / 'events.csv'}) — уривок, категорії, дата публікації, згадані дати/суб'єкти та URL джерела.",
             f"- [Суб'єкти й обладнання]({folder / 'entities.csv'}) — явно зазначені назви, бренди, ідентифікатори, моделі та класи обладнання.",
             f"- [Дати]({folder / 'dates.csv'}) — точний текст дати, календарне значення або причина невизначеності.",
             f"- [Повні структуровані записи]({folder / 'structured_posts.jsonl'}) — текст і походження кожного відібраного допису.", "",
             "## Що потрібно для метрик", "",
             "- **TTR:** одна перевірена подія порушення роботи, однаковий суб'єкт/процес, дата порушення і дата повернення до нормального рівня роботи.",
             "- **TTS:** залишок конкретного матеріалу на визначену дату та добове споживання в однакових одиницях. Простий запас/споживання описує сценарій без нових надходжень; мережевий TTS потребує моделі.",
             "- **REI:** погоджена методика, модель ланцюга і втрат, перевірені часові параметри. Попередню формулу проєкту не застосовано.", "",
             "[MIT: визначення TTR/TTS та роль моделі REI](https://news.mit.edu/2022/companies-use-mit-research-identify-respond-supply-chain-risks-0615).", "",
             "## Приклади витягу", "",
             "Нижче — приклади роботи правил, не підтвердження подій або їхнього впливу. Дата публікації показана окремо від дати, згаданої в тексті.", ""]
    unique_examples = list({item["source_url"]: item for item in reversed(summary["examples"])}.values())
    for example in unique_examples[:4]:
        excerpt = " ".join(example["snippet"].split()[:15]).replace("\n", " ")
        mentioned = "; ".join(item["raw"] + " → " + (item["normalized_date"] or "рік/день не встановлено") for item in example["dates"][:2])
        lines.extend([f"- [{example['channel']}: допис-джерело]({example['source_url']}). Публікація: {example['published_at']}. "
                      f"Згадана дата: {mentioned}. Уривок: «{excerpt}…». Статус: потребує перевірки."])
    lines.extend(["", "## Межі результату", "",
                  "Відбір за словами не є вичерпним розпізнаванням. Згадка Росії або оборонної промисловості не доводить належності кожного суб'єкта до ВПК РФ. "
                  "Організації не об'єднуються за схожою назвою, а події не з'єднуються автоматично в причинний ланцюг. "
                  "Перепублікації не рахуються як незалежне підтвердження. Повторені тексти можна зіставити за контрольною сумою.", "",
                  "MTProto-збір історії та доступних медіа підготовлений; додано окремий збір коментарів із прив'язкою до допису. "
                  "На момент підготовки цього огляду авторизованої сесії технічного акаунта немає, тому коментарі й серверні файли ще не зібрані. "
                  "Коментарі не включені в цей структурований набір.", ""])
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")

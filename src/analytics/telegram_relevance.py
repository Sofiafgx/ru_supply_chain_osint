"""Two-stage, auditable relevance filtering; no event or capacity verification."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from analytics.telegram_structure import inn_checksum

VERSION = "4"
PRODUCT = re.compile(r"\b(?:плат[аыіи]\b|плат\b|микросхем\w*|мікросхем\w*|компонент\w*|комплектующ\w*|комплектуюч\w*|антенн\w*|антен\w*|станк\w*|станок|верстат\w*|ЧПУ|CNC|БПЛА|дрон\w*|беспилотник\w*|безпілотник\w*|ракет\w*|баллистическ\w*|балістичн\w*|боеприпас\w*|боєприпас\w*|оборудован\w*|обладнан\w*|подшипник\w*|підшипник\w*|двигател\w*|двигун\w*|сборочн\w*|складальн\w*|печатн\w*|друкован\w*|PCB|chip\w*|component\w*|missile\w*|drone\w*)", re.I)
PRODUCT = re.compile(PRODUCT.pattern + r"|\b(?:UAV\w*|antenna\w*|board\w*|machine\w*)\b", re.I)
ACTION_TEXT = re.compile(r"\b(?:производ\w*|вироб\w*|сборк\w*|собира\w*|склада\w*|збира\w*|постав\w*|постача\w*|изготов\w*|виготов\w*|выпуск\w*|випуск\w*|возобнов\w*|віднов\w*|восстанов\w*|manufactur\w*|assembl\w*|deliver\w*|produc\w*|suppl\w*)", re.I)
RUSSIA = re.compile(r"\b(?:российск\w*|російськ\w*|росси[яию]\w*|росії|РФ|russian|russia)\b", re.I)
DEFENCE = re.compile(r"\b(?:ВПК|ОПК|оборонн\w*|гособорон\w*|Минобороны|Міноборони|военн\w*|військов\w*|military|defence|defense)\b", re.I)
FACTORY = re.compile(r"\b(?:завод\w*|предприят\w*|підприємств\w*|корпорац\w*|компан\w*|company|plant|factory)\b", re.I)
ADVERT = re.compile(r"\b(?:erid|реклама|ваканси\w*|вакансі\w*|набор\s+сотрудник\w*|набір\s+працівник\w*|трудоустройств\w*|працевлаштуван\w*)", re.I)
UNCERTAIN = re.compile(r"\b(?:планир\w*|плану\w*|ожида\w*|очіку\w*|возможно|можливо|предполож\w*|ймовір\w*|якщо|если|гипотет\w*|hypothet\w*)", re.I)
UNCERTAIN = re.compile(UNCERTAIN.pattern + r"|\b(?:plan\w*|expect\w*|might|could|would|will|if)\b", re.I)
NEGATION = re.compile(r"\b(?:не|нет|ні|not|no)\b", re.I)
HISTORY = re.compile(r"\b(?:в\s+истории|в\s+історії|день\s+в\s+истории|історичн\w*|историческ\w*)", re.I)
LEGACY_STATE = re.compile(r"\b(?:СССР|СРСР|Soviet|USSR)\b", re.I)
DATED_YEAR = re.compile(r"\b((?:18|19|20)\d{2})\s*(?:г\.(?=\s|$)|год\w*\b|ро[кц]\w*\b|р\.(?=\s|$))|\b(?:in|year)\s+((?:18|19|20)\d{2})\b", re.I)
CIVILIAN = re.compile(r"\b(?:гражданск\w*|цивільн\w*|мебел\w*|мебл\w*|сельскохозяйств\w*|сільськогосподар\w*|теплоизоляц\w*|теплоізоляц\w*|газов\w*\s+оборудован\w*|газов\w*\s+обладнан\w*)", re.I)
CIVILIAN = re.compile(CIVILIAN.pattern + r"|\b(?:civilian|furniture|agricultur\w*)\b", re.I)
ACTION_LEMMAS = {
    "производить", "производство", "изготовить", "изготавливать", "изготовление", "выпускать", "выпустить", "выпуск",
    "собирать", "собрать", "сборка", "поставить", "поставлять", "поставка", "отгрузить", "отгрузка",
    "виробляти", "виробити", "виробництво", "виготовити", "виготовляти", "виготовлення", "випускати", "випуск",
    "складати", "складання", "збирати", "зібрати", "збирання", "постачати", "постачання", "постачити", "відвантажити",
    "восстановить", "восстановление", "возобновить", "відновити", "відновлення",
    "produce", "production", "manufacture", "manufacturing", "assemble", "assembly", "deliver", "delivery", "supply", "ship", "shipment", "restore", "resume",
}


def normalized_with_offsets(text):
    chars, offsets = [], []
    for index, character in enumerate(text):
        for char in unicodedata.normalize("NFKC", character).lower().replace("ё", "е"):
            if not char.isalnum():
                char = " "
            if char == " " and (not chars or chars[-1] == " "):
                continue
            chars.append(char)
            offsets.append(index)
    return "".join(chars), offsets


def normalize(text):
    return normalized_with_offsets(text)[0].strip()


class EntityRegistry:
    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.data = json.loads(raw)
        self.entities = {item["entity_id"]: item for item in self.data["entities"]}
        self.patterns = []
        for entity in self.entities.values():
            if entity.get("inn") and not inn_checksum(entity["inn"]):
                raise ValueError(f"Невалідна контрольна сума ІНН: {entity['entity_id']}")
            if not entity.get("identity_sources") or not entity.get("defence_sources"):
                raise ValueError("Кожний суб'єкт має мати джерела ідентичності та галузевої належності")
            for field, kind in (("aliases", "name"), ("context_aliases", "site_name"), ("ambiguous_aliases", "ambiguous_name")):
                for alias in entity.get(field, []):
                    self.patterns.append((entity, kind, re.compile(r"(?<!\w)" + re.escape(normalize(alias)) + r"(?!\w)")))
        self.product_patterns = [(item, re.compile(r"(?<!\w)" + re.escape(normalize(alias)) + r"(?!\w)"))
                                 for item in self.data.get("products", []) for alias in item["aliases"]]

    def match(self, text):
        normalized, offsets = normalized_with_offsets(text)
        matches, products = [], []
        for entity, kind, pattern in self.patterns:
            for match in pattern.finditer(normalized):
                tail = normalized[match.end():].lstrip()
                if any(tail.startswith(normalize(suffix)) for suffix in entity.get("blocked_suffixes", [])):
                    continue
                start, end = offsets[match.start()], offsets[match.end()-1] + 1
                matches.append({"entity_id": entity["entity_id"], "reference_name": entity["name"],
                                "kind": kind, "raw": text[start:end], "start": start, "end": end,
                                "reference_inn": entity.get("inn"), "reference_ogrn": entity.get("ogrn"),
                                "legal_identity_verified": False,
                                "identity_sources": entity["identity_sources"], "defence_sources": entity["defence_sources"]})
        for kind, label in (("inn", r"(?:ИНН|ІПН|INN)"), ("ogrn", r"(?:ОГРН|OGRN)")):
            for match in re.finditer(r"\b" + label + r"\s*[:№#-]?\s*(\d{10,15})(?!\d)", text, re.I):
                for entity in self.entities.values():
                    if match.group(1) == entity.get(kind):
                        matches.append({"entity_id": entity["entity_id"], "reference_name": entity["name"], "kind": kind,
                                        "raw": match.group(), "start": match.start(), "end": match.end(),
                                        "reference_inn": entity.get("inn"), "reference_ogrn": entity.get("ogrn"),
                                        "legal_identity_verified": False,
                                        "identity_sources": entity["identity_sources"], "defence_sources": entity["defence_sources"]})
        for item, pattern in self.product_patterns:
            for match in pattern.finditer(normalized):
                start, end = offsets[match.start()], offsets[match.end()-1] + 1
                products.append({"product_id": item["product_id"], "raw": text[start:end], "start": start, "end": end,
                                 "source_url": item["source_url"], "manufacturer_assigned": False})
        # Довгі назви мають перевагу над вкладеною короткою назвою того самого суб'єкта.
        order = {"inn": 0, "ogrn": 0, "name": 1, "site_name": 2, "ambiguous_name": 3}
        kept = []
        for item in sorted(matches, key=lambda m: (order[m["kind"]], -(m["end"]-m["start"]))):
            if not any(old["entity_id"] == item["entity_id"] and old["start"] <= item["start"] and old["end"] >= item["end"] for old in kept):
                kept.append(item)
        products = list({(m["product_id"], m["start"], m["end"]): m for m in products}.values())
        return sorted(kept, key=lambda m: m["start"]), products


def language(text):
    if re.search(r"[іїєґІЇЄҐ]|\b(?:виробництв\w*|підприємств\w*|постачан\w*)", text):
        return "uk"
    if re.search(r"[а-яА-ЯёЁ]", text):
        return "ru"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "unsupported"


def legacy_period(text):
    years = [int(first or second) for first, second in DATED_YEAR.findall(text)]
    return bool(LEGACY_STATE.search(text) or (years and max(years) < 1992))


def dependency_path(first, second, max_edges=3):
    """Короткий шлях по дереву; зберігаємо його як пояснення кандидата."""
    first_chain = [first] + list(first.ancestors)
    second_chain = [second] + list(second.ancestors)
    for left_index, left in enumerate(first_chain[:max_edges+1]):
        for right_index, right in enumerate(second_chain[:max_edges+1]):
            if left.i == right.i and left_index + right_index <= max_edges:
                return first_chain[:left_index+1] + list(reversed(second_chain[:right_index]))
    return None


class ContextClassifier:
    def __init__(self):
        import spacy
        self.models = {key: spacy.load(name, disable=["ner"]) for key, name in
                       (("ru", "ru_core_news_sm"), ("uk", "uk_core_news_sm"), ("en", "en_core_web_sm"))}
        self.metadata = {key: {"name": model.meta["name"], "version": model.meta["version"]} for key, model in self.models.items()}

    def classify(self, text, matches, products):
        lang = language(text)
        if lang == "unsupported":
            return {"language": lang, "relations": [], "warnings": ["language_model_unavailable"]}
        doc = self.models[lang](text)
        relations = []
        for sent in doc.sents:
            sentence_text = sent.text
            actions = [token for token in sent if token.lemma_.lower() in ACTION_LEMMAS]
            component_tokens = [(token, token.text) for token in sent if PRODUCT.fullmatch(token.text)]
            for item in products:
                if sent.start_char <= item["start"] < sent.end_char:
                    span = doc.char_span(item["start"], item["end"], alignment_mode="expand")
                    if span:
                        component_tokens.append((span.root, item["raw"]))
            local = [item for item in matches if sent.start_char <= item["start"] and item["end"] <= sent.end_char]
            anchors = []
            for item in local:
                span = doc.char_span(item["start"], item["end"], alignment_mode="expand")
                if span:
                    anchors.append((span.root, item))
            if not anchors and RUSSIA.search(sentence_text) and DEFENCE.search(sentence_text):
                anchors = [(token, None) for token in sent if FACTORY.fullmatch(token.text)]
            for anchor, item in anchors:
                for action in actions:
                    entity_path = dependency_path(anchor, action, max_edges=2)
                    if not entity_path:
                        continue
                    # Не переходимо через іншу дію: вона може мати іншого суб'єкта.
                    if any(token.i != action.i and token.pos_ in ("VERB", "AUX") for token in entity_path[1:]):
                        continue
                    for component, component_text in component_tokens:
                        path = dependency_path(component, action, max_edges=2)
                        if not path or component.i == action.i:
                            continue
                        if any(token.i != action.i and token.pos_ in ("VERB", "AUX") for token in path[1:]):
                            continue
                        # Чуже явне підметове речення не стає дією згаданого підприємства.
                        subjects = [child for child in action.children if child.dep_.startswith("nsubj")]
                        if action.pos_ == "VERB" and subjects and not any(child.i in {node.i for node in entity_path[:-1]} for child in subjects):
                            continue
                        relations.append({"entity_id": item["entity_id"] if item else None,
                                          "match_kind": item["kind"] if item else "unnamed_industrial_actor",
                                          "sentence": sentence_text, "start": sent.start_char, "end": sent.end_char,
                                          "action": action.text, "action_lemma": action.lemma_, "component": component_text,
                                          "entity_path": [{"text": token.text, "dep": token.dep_} for token in entity_path],
                                          "component_path": [{"text": token.text, "dep": token.dep_} for token in path],
                                          "negation_present": bool(NEGATION.search(sentence_text)),
                                          "prospective_or_conditional": bool(UNCERTAIN.search(sentence_text)),
                                          "civilian_or_mixed_context": bool(CIVILIAN.search(sentence_text)),
                                          "historical_or_legacy_state_context": legacy_period(sentence_text),
                                          "relation_verified": False})
        relations = list({(r["entity_id"], r["start"], r["action"], r["component"]): r for r in relations}.values())
        return {"language": lang, "relations": relations, "warnings": ["general_news_dependency_model_not_domain_validated"]}


def filter_post(record, registry, classifier):
    text = record.get("text") or ""
    matches, products = registry.match(text)
    generic_candidate = bool(RUSSIA.search(text) and DEFENCE.search(text) and FACTORY.search(text) and ACTION_TEXT.search(text) and PRODUCT.search(text))
    run_nlp = bool(matches or products or generic_candidate)
    context = classifier.classify(text, matches, products) if run_nlp else {"language": None, "relations": [], "warnings": []}
    blockers = []
    if ADVERT.search(text):
        blockers.append("advertisement_or_recruitment")
    if HISTORY.search(text):
        blockers.append("historical_context")
    if record.get("preview_warning") or record.get("quality_warning"):
        blockers.append("possibly_incomplete_source")
    # Прямий конфлікт реквізитів лишаємо для ручного розбору.
    for match in matches:
        if match["kind"] != "name":
            continue
        tail = text[match["end"]:match["end"]+100]
        labeled = re.match(r'[\s»"”.,:;)]*(?:ИНН|ІПН|INN)\s*[:№#-]?\s*(\d{10}|\d{12})(?!\d)', tail, re.I)
        if labeled and labeled.group(1) != match["reference_inn"]:
            blockers.append("name_inn_conflict")
    solid = [item for item in matches if item["kind"] in ("name", "inn", "ogrn")]
    relations = context["relations"]
    clean = [item for item in relations if item["match_kind"] in ("name", "inn", "ogrn") and not item["negation_present"] and not item["prospective_or_conditional"] and not item["civilian_or_mixed_context"] and not item["historical_or_legacy_state_context"]]
    if solid and clean and not blockers:
        decision = "entity_and_context_candidate"
        reason = "reference_entity_and_dependency_linked_industrial_action_component"
    elif matches:
        decision = "entity_match_review"
        reason = "reference_mention_without_sufficient_unambiguous_production_link"
    elif products or generic_candidate:
        decision = "context_only_review"
        reason = "potential_context_but_entity_identity_not_in_reference_set"
    else:
        decision = "no_match"
        reason = "no_signal_in_current_reference_and_context_rules_not_proof_of_other_sector"
    return {"filter_version": VERSION, "registry_sha256": registry.sha256,
            "decision": decision, "reason": reason, "entity_matches": matches, "product_matches": products,
            "context": context, "blockers": sorted(set(blockers)),
            "claim_verified": False, "metric_eligible": False,
            "classifier_accuracy_measured": False}

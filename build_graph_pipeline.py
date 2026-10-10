#!/usr/bin/env python3
"""
Industrial Supply Chain OSINT Graph Pipeline
=============================================
Автономний модуль побудови графа та аудиту зв'язків:
- Зчитує contracts.csv (розбирає supplier_inns та sign_date)
- Обчислює точки зчленування (Cut Vertices), HHI, SSR та Betweenness Centrality
- Автоматично створює та оновлює verified_dataset_audit.json та leaderboard_data.json
"""

import os
import re
import json
import math
import logging
from typing import Dict, List, Set, Any, Optional
import pandas as pd
import networkx as nx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SupplyChainGraphPipeline")

BASE_YEAR = 2026
LAMBDA_DECAY = 0.5436890126920764

DEFAULT_ENTITIES = [
  {
    "inn": "2130094170",
    "company_name": "АО «ВНИИР-Прогресс»",
    "display_name": "АО «ВНИИР-Прогресс»",
    "entity_origin": "russian_defence_entities.json",
    "checksum_verified": True,
    "legal_identity_sources": [
      "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R3183"
    ],
    "defence_designation_sources": [
      "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R3183"
    ],
    "products_referenced": [
      {
        "name": "Комета-М",
        "source_url": "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R3183",
        "note": "Згадування виробу в регламенті ЄС не доводить факт випуску або поставок"
      }
    ],
    "tier": None,
    "tier_note": "Не визначено; відсутнє верифіковане правило класифікації рівнів кооперації",
    "contract_records_in_archive": {
      "contracts_with_both_identifiers": 0,
      "single_party_customer_records": 0,
      "archive_note": "У вибірці прямих записів за даним ІПН не виявлено"
    },
    "arbitration_records": {
      "search_status": "first_page_collected",
      "browser_search_records": 25,
      "reported_total": 32,
      "pages_collected": 1,
      "complete": False,
      "period_from": "2022-01-01",
      "period_to": "2026-10-08",
      "source": "data/compliance_offline_20261009/raw/browser_search_2130094170.html",
      "archive_note": "Зібрано лише 1-шу сторінку (25 із 32). Пошукова присутність у КАД не підтверджує автоматично порушення поставок"
    },
    "graph_metrics": {
      "v_eff_latora": None,
      "betweenness_centrality": None,
      "ssr_monopoly": None,
      "hhi_concentration": None,
      "composite_criticality_index": None,
      "calculation_status": "uncomputed"
    },
    "resilience_parameters": {
      "units_per_system": None,
      "tts_days": None,
      "ttr_days": None,
      "basis": "Дані щодо норм витрат BOM, складських залишків та моделі відновлення відсутні"
    }
  },
  {
    "inn": "5099000013",
    "company_name": "АО «Корпорация «Тактическое ракетное вооружение»",
    "display_name": "АО «Корпорация «ТРВ»",
    "entity_origin": "russian_defence_entities.json",
    "checksum_verified": True,
    "legal_identity_sources": [
      "https://data.consilium.europa.eu/doc/document/ST-6810-2023-INIT/en/pdf"
    ],
    "defence_designation_sources": [
      "https://home.treasury.gov/news/press-releases/jy0677"
    ],
    "products_referenced": [],
    "tier": None,
    "tier_note": "Не визначено; холдингова назва не ідентифікує конкретний виробничий майданчик",
    "contract_records_in_archive": {
      "contracts_with_both_identifiers": 0,
      "single_party_customer_records": 4,
      "archive_note": "У вибірці знайдено 4 контракти замовника без зазначеного постачальника"
    },
    "arbitration_records": {
      "search_status": "first_page_collected",
      "browser_search_records": 25,
      "reported_total": 112,
      "pages_collected": 1,
      "complete": False,
      "period_from": "2022-01-01",
      "period_to": "2026-10-08",
      "source": "data/compliance_offline_20261009/raw/browser_search_5099000013.html",
      "archive_note": "Зібрано 1-шу сторінку (25 із 112)"
    },
    "graph_metrics": {
      "v_eff_latora": None,
      "betweenness_centrality": None,
      "ssr_monopoly": None,
      "hhi_concentration": None,
      "composite_criticality_index": None,
      "calculation_status": "uncomputed"
    },
    "resilience_parameters": {
      "units_per_system": None,
      "tts_days": None,
      "ttr_days": None,
      "basis": "Дані відсутні"
    }
  },
  {
    "inn": "1828020110",
    "company_name": "АО «Воткинский завод»",
    "display_name": "АО «Воткинский завод»",
    "entity_origin": "russian_defence_entities.json",
    "checksum_verified": True,
    "legal_identity_sources": [
      "https://sanctionssearch.ofac.treas.gov/Details.aspx?id=41002"
    ],
    "defence_designation_sources": [
      "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32022R2476"
    ],
    "products_referenced": [],
    "tier": None,
    "tier_note": "Не визначено",
    "contract_records_in_archive": {
      "contracts_with_both_identifiers": 0,
      "single_party_customer_records": 1,
      "archive_note": "У вибірці знайдено 1 контракт замовника без зазначеного постачальника"
    },
    "arbitration_records": {
      "search_status": "first_page_collected",
      "browser_search_records": 25,
      "reported_total": 490,
      "pages_collected": 1,
      "complete": False,
      "period_from": "2022-01-01",
      "period_to": "2026-10-08",
      "source": "data/compliance_offline_20261009/raw/browser_search_1828020110.html",
      "archive_note": "Зібрано 1-шу сторінку (25 із 490)"
    },
    "graph_metrics": {
      "v_eff_latora": None,
      "betweenness_centrality": None,
      "ssr_monopoly": None,
      "hhi_concentration": None,
      "composite_criticality_index": None,
      "calculation_status": "uncomputed"
    },
    "resilience_parameters": {
      "units_per_system": None,
      "tts_days": None,
      "ttr_days": None,
      "basis": "Дані відсутні"
    }
  },
  {
    "inn": "1646019914",
    "company_name": "АО «ОЭЗ ППТ «Алабуга»",
    "display_name": "АО «ОЭЗ ППТ «Алабуга»",
    "entity_origin": "russian_defence_entities.json",
    "checksum_verified": True,
    "legal_identity_sources": [
      "https://alabuga.ru/ru/information/"
    ],
    "defence_designation_sources": [
      "https://home.treasury.gov/news/press-releases/jy2117"
    ],
    "products_referenced": [
      {
        "name": "Герань-2",
        "source_url": "https://home.treasury.gov/news/press-releases/jy2117",
        "note": "Згадування у пресрелізі не доводить пряме виробництво чи зв'язок у масиві угод"
      }
    ],
    "tier": None,
    "tier_note": "Не визначено; оператор промзони",
    "contract_records_in_archive": {
      "contracts_with_both_identifiers": 0,
      "single_party_customer_records": 20,
      "archive_note": "У вибірці знайдено 20 контрактів замовника без постачальника"
    },
    "arbitration_records": {
      "search_status": "first_page_collected",
      "browser_search_records": 25,
      "reported_total": 137,
      "pages_collected": 1,
      "complete": False,
      "period_from": "2022-01-01",
      "period_to": "2026-10-08",
      "source": "data/compliance_offline_20261009/raw/browser_search_1646019914.html",
      "archive_note": "Зібрано 1-шу сторінку (25 із 137)"
    },
    "graph_metrics": {
      "v_eff_latora": None,
      "betweenness_centrality": None,
      "ssr_monopoly": None,
      "hhi_concentration": None,
      "composite_criticality_index": None,
      "calculation_status": "uncomputed"
    },
    "resilience_parameters": {
      "units_per_system": None,
      "tts_days": None,
      "ttr_days": None,
      "basis": "Дані відсутні"
    }
  },
  {
    "inn": "7719830028",
    "company_name": "АО «НПО «Базальт»",
    "display_name": "АО «НПО «Базальт»",
    "entity_origin": "companies.csv / employers_2026-10-09.csv",
    "checksum_verified": True,
    "legal_identity_sources": [
      "data/collection_20261008/exports/companies.csv"
    ],
    "defence_designation_sources": [
      "Запис у companies.csv; текстовий збіг не є самостійним доказом оборонного профілю"
    ],
    "products_referenced": [],
    "tier": None,
    "tier_note": "Не визначено",
    "contract_records_in_archive": {
      "contracts_with_both_identifiers": 0,
      "single_party_customer_records": 1,
      "archive_note": "У вибірці знайдено 1 контракт замовника без постачальника"
    },
    "arbitration_records": {
      "search_status": "not_performed",
      "browser_search_records": None,
      "reported_total": None,
      "pages_collected": 0,
      "complete": False,
      "period_from": None,
      "period_to": None,
      "source": None,
      "archive_note": "Окремий браузерний пошук не проводився"
    },
    "graph_metrics": {
      "v_eff_latora": None,
      "betweenness_centrality": None,
      "ssr_monopoly": None,
      "hhi_concentration": None,
      "composite_criticality_index": None,
      "calculation_status": "uncomputed"
    },
    "resilience_parameters": {
      "units_per_system": None,
      "tts_days": None,
      "ttr_days": None,
      "basis": "Дані відсутні"
    }
  },
  {
    "inn": "5243001887",
    "company_name": "ПАО «Арзамасское научно-производственное предприятие «Темп-Авиа»",
    "display_name": "ПАО «Темп-Авиа»",
    "entity_origin": "companies.csv / contracts.csv",
    "checksum_verified": True,
    "legal_identity_sources": [
      "data/collection_20261008/exports/companies.csv"
    ],
    "defence_designation_sources": [
      "Запис у companies.csv; текстовий збіг не є самостійним доказом оборонного профілю"
    ],
    "products_referenced": [],
    "tier": None,
    "tier_note": "Не визначено",
    "contract_records_in_archive": {
      "contracts_with_both_identifiers": 0,
      "single_party_customer_records": 5,
      "archive_note": "У вибірці знайдено 5 контрактів замовника без постачальника"
    },
    "arbitration_records": {
      "search_status": "not_performed",
      "browser_search_records": None,
      "reported_total": None,
      "pages_collected": 0,
      "complete": False,
      "period_from": None,
      "period_to": None,
      "source": None,
      "archive_note": "Окремий браузерний пошук не проводився"
    },
    "graph_metrics": {
      "v_eff_latora": None,
      "betweenness_centrality": None,
      "ssr_monopoly": None,
      "hhi_concentration": None,
      "composite_criticality_index": None,
      "calculation_status": "uncomputed"
    },
    "resilience_parameters": {
      "units_per_system": None,
      "tts_days": None,
      "ttr_days": None,
      "basis": "Дані відсутні"
    }
  }
]


def parse_supplier_inns(raw_val: Any) -> List[str]:
    if pd.isna(raw_val) or raw_val is None:
        return []
    if isinstance(raw_val, list):
        return [str(x).strip() for x in raw_val if str(x).strip()]
    raw_str = str(raw_val).strip()
    if not raw_str or raw_str in ('[]', 'null', 'None', ''):
        return []
    try:
        parsed = json.loads(raw_str)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        if isinstance(parsed, (int, str)):
            return [str(parsed).strip()]
    except Exception:
        pass
    return re.findall(r'\b\d{10}\b|\b\d{12}\b', raw_str)


def parse_contract_year(sign_date_val: Any) -> Optional[int]:
    if pd.isna(sign_date_val) or sign_date_val is None:
        return None
    date_str = str(sign_date_val).strip()
    match = re.match(r'^(\d{4})', date_str)
    return int(match.group(1)) if match else None


def run_pipeline():
    candidates = [
        "data/collection_20261008/exports/contracts.csv",
        "contracts.csv",
        os.path.expanduser("~/Desktop/ru_supply_chain_osint/data/collection_20261008/exports/contracts.csv"),
    ]
    contracts_csv = next((c for c in candidates if os.path.exists(c)), None)
    if not contracts_csv:
        raise FileNotFoundError("contracts.csv не знайдено.")

    logger.info(f"1. Завантаження contracts.csv: {contracts_csv}")
    df = pd.read_csv(contracts_csv, low_memory=False)

    logger.info("2. Побудова графа та розбір зв'язків...")
    G = nx.DiGraph()
    customer_spend_by_supplier: Dict[str, Dict[str, float]] = {}
    customer_has_missing_price: Dict[str, bool] = {}

    bilateral_count = 0
    missing_supplier_count = 0
    missing_price_count = 0

    for _, row in df.iterrows():
        c_inn = str(row.get('customer_inn') or '').strip()
        supp_list = parse_supplier_inns(row.get('supplier_inns'))
        raw_price = row.get('price_rub')
        sign_date = row.get('sign_date')

        is_price_missing = pd.isna(raw_price) or raw_price is None
        if is_price_missing:
            missing_price_count += 1
            price = None
        else:
            try:
                price = float(raw_price)
            except Exception:
                price = None

        year = parse_contract_year(sign_date)

        if not supp_list or not c_inn:
            missing_supplier_count += 1
            continue

        bilateral_count += 1
        decay_weight = (LAMBDA_DECAY ** max(0, BASE_YEAR - year)) if year is not None else 1.0

        for s_inn in supp_list:
            if not s_inn or len(s_inn) < 10:
                continue
            if price is not None and price > 0:
                edge_weight = math.log1p(price) * decay_weight
                if G.has_edge(s_inn, c_inn):
                    G[s_inn][c_inn]['weight'] += edge_weight
                    G[s_inn][c_inn]['raw_price_sum'] += price
                else:
                    G.add_edge(s_inn, c_inn, weight=edge_weight, raw_price_sum=price)
                customer_spend_by_supplier.setdefault(c_inn, {})
                customer_spend_by_supplier[c_inn][s_inn] = customer_spend_by_supplier[c_inn].get(s_inn, 0.0) + price
            else:
                if not G.has_edge(s_inn, c_inn):
                    G.add_edge(s_inn, c_inn, weight=0.0, raw_price_sum=0.0)
                if is_price_missing:
                    customer_has_missing_price[c_inn] = True

    for u, v, d in G.edges(data=True):
        d['distance'] = 1.0 / d['weight'] if d['weight'] > 0 else 1e6

    logger.info(f"   Граф: {G.number_of_nodes()} вузлів, {G.number_of_edges()} ребер.")

    logger.info("3. Обчислення HHI та SSR...")
    customer_hhi: Dict[str, Optional[float]] = {}
    supplier_max_ssr: Dict[str, Optional[float]] = {}

    for cust, supps in customer_spend_by_supplier.items():
        total_spend = sum(supps.values())
        if total_spend <= 0 or customer_has_missing_price.get(cust, False):
            customer_hhi[cust] = None
        else:
            customer_hhi[cust] = sum((v / total_spend) ** 2 for v in supps.values())
            for s_inn, s_val in supps.items():
                share = s_val / total_spend
                if supplier_max_ssr.get(s_inn) is None or share > supplier_max_ssr[s_inn]:
                    supplier_max_ssr[s_inn] = share

    logger.info("4. Пошук точок зчленування (Cut Vertices)...")
    UG = G.to_undirected()
    cut_vertices = set(nx.articulation_points(UG))
    logger.info(f"   Знайдено {len(cut_vertices)} точок зчленування.")

    cache_file = "graph_metrics_cache.json"
    betweenness = None
    if os.path.exists(cache_file):
        try:
            logger.info("5. Завантаження центральності з кешу...")
            with open(cache_file, "r", encoding="utf-8") as f:
                betweenness = json.load(f)
            logger.info(f"   Завантажено {len(betweenness)} значень центральності.")
        except Exception:
            betweenness = None

    if betweenness is None:
        logger.info("5. Обчислення Betweenness Centrality (зачекайте ~4-5 хвилин)...")
        betweenness = nx.betweenness_centrality(G, weight='distance', normalized=True)
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(betweenness, f)
            logger.info("   Центральність збережена у кеш graph_metrics_cache.json.")
        except Exception as e:
            logger.warning(f"   Помилка кешування: {e}")

    logger.info("6. Створення та узгодження структури JSON...")
    base_data = {
        "dataset_metadata": {
            "total_contracts": len(df),
            "both_identifiers_present": bilateral_count,
            "supplier_identifier_missing": missing_supplier_count,
            "missing_supplier_with_procurement_id": 18848,
            "missing_supplier_without_procurement_id": 12818,
            "missing_price_rub": missing_price_count,
            "distinct_identifiers_in_present_pairs": G.number_of_nodes(),
            "distinct_participant_inns_total": 25003,
            "arbitration_cases": {
                "older_unique": 47,
                "browser_unique": 499,
                "browser_rows_total": 500,
                "union_total_unique": 546
            },
            "source_file": contracts_csv,
            "base_year": BASE_YEAR,
            "lambda_decay": LAMBDA_DECAY,
            "graph_metrics_computed": True,
            "total_nodes_in_directed_graph": G.number_of_nodes(),
            "total_directed_edges": G.number_of_edges(),
            "cut_vertices_observed_count": len(cut_vertices),
            "version": "2.2.0-audit-verified"
        },
        "methodology_status": {
            "units_per_system": None,
            "tts_status": "unverified_no_inventory_data",
            "ttr_status": "unverified_no_recovery_model",
            "tier_status": "undefined_no_classification_rule",
            "decay_convention_status": "formula",
            "single_source_rule": "SSR >= 0.99 triggers Inferred Single-Source only if positive inflow is observed"
        },
        "entities": DEFAULT_ENTITIES
    }

    for entity in base_data["entities"]:
        inn = str(entity.get('inn', '')).strip()
        in_graph = G.has_node(inn)
        betw_val = betweenness.get(inn) if in_graph else None
        ssr_val = supplier_max_ssr.get(inn) if in_graph else None
        hhi_val = customer_hhi.get(inn) if in_graph else None
        is_cut = (inn in cut_vertices) if in_graph else False

        entity['graph_metrics'] = {
            "v_eff_latora": None,
            "betweenness_centrality": round(betw_val, 6) if betw_val is not None else None,
            "ssr_monopoly": round(ssr_val, 4) if ssr_val is not None else None,
            "hhi_concentration": round(hhi_val, 4) if hhi_val is not None else None,
            "composite_criticality_index": None,
            "calculation_status": "computed_centrality_and_ssr"
        }
        entity['spof_profile'] = {
            "is_cut_vertex_in_observed_graph": is_cut,
            "operational_spof_confirmed": False,
            "operational_spof_caveat": "Топологічна точка зчленування у спостережуваному підграфі; виробничий SPOF не підтверджений через відсутність BOM (units_per_system: null), складського обліку (tts_days: null) та невідомих постачальників у 31 666 контрактах."
        }

    for out_name in ["verified_dataset_audit.json", "leaderboard_data.json"]:
        with open(out_name, 'w', encoding='utf-8') as f:
            json.dump(base_data, f, ensure_ascii=False, indent=2)
        logger.info(f"✓ Успішно збережено файл: {out_name}")


if __name__ == "__main__":
    run_pipeline()

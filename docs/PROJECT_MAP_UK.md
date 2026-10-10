# Карта даних та інформації

## Де шукати за темою

| Тема | Входи / архіви | Результати / аналіз | Код у `src/` |
|---|---|---|---|
| Контракти й закупівлі | data/collection_20261008; data/c4ads_collection_20261008; data/compliance_b2b_20261009; data/compliance_remediation_20261009 | outputs/tier2_audit; outputs/procurement_identifiers_20261010; data/processed/procurement | collection_cli, contract_links_async, backfill_procurement_identifiers, eis_ftp_contracts |
| Судові справи та постачальники | data/compliance_offline_20261009; data/compliance_text_20261009 | outputs/local_contract_edges_20261010; outputs/tier2_contract_edges.ndjson | kad_documents, kad_microbatch, supplier_enrichment, supplier_text_extraction, local_contract_evidence |
| Компанії, вакансії, промисловість | data/imports/companies.csv; database/; data/industrial_20261009; data/industrial_20261009_followup | data/cnc_inventory_20261010; outputs/enterprise_equipment_20261010 | industrial_cli, industrial_followup_build, cnc_inventory_export, cnc_signatures, cnc_trace_search |
| Telegram | data/telegram; data/telegram_public | data/telegram_structured; data/telegram_filtered | telegram_cli, telegram_public_cli, telegram_structure_cli, telegram_filter_cli |
| X / Threads та інші соціальні джерела | data/imports/posts.csv; data/imports/Зібрані_дописи_X_Threads_2022_2026.xlsx | data/social_history | social_cli, social_collection, social_browser_import |
| Торгівля й митні дані | data/trade_target_scan_20261009; data/trade_intervals_20261009 | data/processed/trade; outputs/comtrade_only | comtrade_mirror_async, comtrade_report, trade_interval_cli, local_trade_pipeline |
| Обладнання / Аршин | data/processed/equipment | outputs/enterprise_equipment_20261010; outputs/evidence_pipeline | arshin_parser, local_equipment_links |
| Патенти | data/patent_cache* | outputs/patents; reports/patent_parser_validation.json | patent_cli, parsers/patent_parser.py |
| Супутникові й радарні матеріали | data/geospatial_recovery; config/radar_buildings.geojson | Див. каталоги й маніфести конкретного запуску; наявність коду не означає готовий TTR | satellite_ttr, sentinel1_radar_ttr, sentinel1_radar_gee |
| Компоненти та підсистеми | docs/research_notes | reports/navigation_control_components*; reports/weapon_subsystem_components* | Звіти з джерелами; автоматичний BOM-збирач не реалізовано |
| Якість, методика та готовність метрик | config/research_methodology.json; docs/WORK_PLAN.md | reports/audits; reports/metric_feasibility_20261010; reports/graph_aggregate_audit_20261010; outputs/sanitization_20261010 | data_sanitization, evidence_pipeline, evidence_snapshot, analytics/resilience.py |

## Як читати папку зі збором

- `raw/`, `responses.jsonl`, HTML, PDF і кеші — отримані матеріали джерела.
- SQLite — робочий архів або стан; призначення уточнюється в інструкції модуля.
- `exports/`, `records*.json`, CSV / JSONL — витяги й результати обробки; перевіряйте походження кожного запису.
- `coverage.json`, `progress.json`, `job.json`, `*.report.json` — обсяг збору, пропуски, помилки й повнота.
- `REPORT_UK.md`, `report.md`, `evidence*.md` — пояснення результату та обмежень.
- `-wal` і `-shm` біля SQLite можуть містити поточні транзакції; вони збережені разом із базами.

`data/` і `outputs/` містять історичні набори з власними назвами. Їх не об’єднано в одну «чисту» таблицю: це зберігає походження та різницю між запусками. [Покажчик даних](../data/README.md) і [покажчик результатів](../outputs/README.md) дають прямі посилання.

## З чого починати аналіз

1. Переглянути [готовність даних для метрик](../reports/audits/MATHEMATICAL_DATA_READINESS_UK_20261010.md).
2. Звірити [стан охоплення](../reports/status/coverage.json) і звіт конкретного запуску; цей файл є збереженим станом, не живим монітором.
3. Для зв’язків брати підтверджені записи та докази, а не лише назви компаній або текстові збіги.
4. Детальний [старий інвентар](../reports/audits/PROJECT_DATA_INVENTORY_UK_20261010.md) читати разом із [картою перенесень](../reports/maintenance/reorganization_20261010.json): він відображає структуру до впорядкування.

## Інструкції

Усі інструкції модулів — у [docs/guides/](guides/). Команди виконуються з кореня проєкту. Історичні звіти й посилання у сирих доказах не переписані; нові місця файлів наведені в журналі перенесення.

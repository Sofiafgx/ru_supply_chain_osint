# История изменений схемы

Новые записи — сверху. Каждое изменение схемы — новая миграция в `db/migrations/`; здесь — что и зачем.

## 2026-10-05 — 0003_supply_and_financials

- `company_edge`: новый `kind = 'supplier'` (`related_id` поставляет `company_id`), колонки `label` (что поставляется) и `evidence_url`.
- `company_financial`: выручка, прибыль, инвестиции с указанием уровня (юрлицо / группа) и источника.
- Данные — из атласа ВПК коллеги (`company-db load-vpk-atlas`): 38 связей поставок, 8 показателей. См. [data_notes.md](data_notes.md#атлас-впк-коллеги).
- `load-gur` теперь удаляет из `company_edge` только свои строки (`source` `profile`, `rostec_tree`), раньше удалял все рёбра загружаемых компаний.

## 2026-10-05 — 0002_vacancies

- Вакансии с сайтов (HH, trudvsem): `vacancy_source`, `region`, `employer_profile`, `employer_company_match`, `vacancy`, `vacancy_hh`, `vacancy_trudvsem`, `vacancy_discovery`, `vacancy_snapshot`, `vacancy_company_match`.
- Классификация ВПК: `classifier_run`, `vacancy_classification`, `employer_classification`, `classification_evidence`, `manual_review`.
- OpenSanctions: `opensanctions_entity`, `opensanctions_code`.
- `company.kpp`.
- Обоснование и решения: [proposals/vacancies.md](proposals/vacancies.md). Инструкции загрузки: [loading/](loading/).
- Применено к боевой БД; таблицы пусты.

## 2026-10-05 — журнал миграций

- `db/schema.sql` → `db/migrations/0001_initial.sql`; `company-db init` заменён на `company-db migrate`, журнал — `schema_migration`.
- Боевая БД, созданная до журнала, принята как есть: 0001 записана как применённая без повторного выполнения.

## 2026-10-05 — 0001_initial

- Создана схема из 21 таблицы: компании, санкции, граф компаний, вооружения и компоненты, модели БпЛА, оборудование заводов, справочники, происхождение (`source_record`).
- `tool_evidence.published_raw` — исходная дата доказательства, когда это не полная дата («2014-2015», «06.2023»).
- Применена к боевой БД (PostgreSQL 18, Railway) через `company-db init`; загружены данные ГУР через `company-db load-gur`.
- Миграций нет: БД создавалась с нуля.

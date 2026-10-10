# Загрузка trudvsem

Сначала прочитайте общие правила: [README.md](README.md).

БД — PostgreSQL, не SQLite. Ваш проект перенесён в общую схему почти целиком. Отличия:

| Ваш проект | Общая схема | Почему |
|---|---|---|
| `employer`, ключ `inn UNIQUE` | `employer_profile` (работодатель на сайте) + сопоставление с `company` | ИНН не уникален: филиалы делят ИНН головной компании (в ГУР — 19 ИНН на 64 компании), различаются по **КПП** — грузите его |
| `employer_external_id` | `employer_profile.external_id` | профиль и есть «работодатель на сайте» |
| `employer.name_norm` | нет | нормализацию для сопоставления делаем на стороне сопоставления |
| `source` (таблица) | `vacancy_source`, код `'trudvsem'` | |
| `vacancy` (одна таблица) | ядро `vacancy` + `vacancy_trudvsem` | поля, которых нет у HH, — в `vacancy_trudvsem` |
| `vacancy_raw` (gzip в БД) | `vacancy_snapshot` (путь + строка в файле) | сырые данные не храним в БД |
| `vacancy_fts` | пока нет | добавим миграцией, когда определимся с поиском |
| `sanctioned_org`, `sanctioned_code` | `opensanctions_entity`, `opensanctions_code` | |
| `level` 'точно'/'вероятно'/... | `level` + `raw_label` | общий словарь с HH |
| `crawl_run`, `crawl_task` | нет | состояние парсера остаётся у вас |
| `v_vpk` | нет | соберём представление после загрузки |

Ваши решения «контакты не храним», «прогоны с версией правил и санкций», «признаки построчно», `first_seen_at` / `last_seen_at` / `is_active` и `manual_review` — сохранены.

## region

По одной строке на субъект: `name` = `region.name`, `kladr_code` = `region.region_code` (13 цифр). Upsert по `kladr_code`. Ожидается 92 строки.

## employer_profile

Профиль — пара «работодатель на trudvsem»: `external_id` = `company.companycode`. Если у одного ИНН несколько `companycode` (разные КПП / филиалы) — это разные профили, так и надо.

| Колонка | Значение |
|---|---|
| `source` | `'trudvsem'` |
| `id_namespace` | `''` |
| `external_id` | `company.companycode`; если его нет — `inn` + `'/'` + `kpp` |
| `name` | `company.name` |
| `url` | `company.url` |
| `inn`, `ogrn`, `kpp` | `company.inn`, `company.ogrn`, `company.kpp` — строкой, без пробелов; пустые → `NULL` |
| `publisher_type` | `'agency'`, если `company.hr-agency` истинно или `source` вакансии — «Вакансия кадрового агентства» / «Аккредитованное частное агентство занятости»; иначе `'employer'`. «Вакансия интернет-ресурса» — `'unknown'` |
| `first_seen_at` / `last_seen_at` | min / max времени выгрузки вакансий профиля |

`company.email` не храним.

## vacancy

| Поле API | Колонка `vacancy` | Примечание |
|---|---|---|
| — | `source` = `'trudvsem'` | |
| `id` | `external_id` | UUID |
| `vac_url` | `url` | |
| — | `employer_profile_id` | FK |
| `company.name` | `employer_name` | |
| `job-name` | `title` | |
| `typicalPosition` | `profession` | |
| `code_profession` | `profession_code` | |
| `category.specialisation` | `specialisation` | |
| `region.region_code` | `region_id` | через `region.kladr_code` |
| — | `locality` | город из адреса, если надёжно выделяется; иначе `NULL` |
| `addresses.address[0]` | `address`, `lat`, `lng` | |
| `salary_min`, `salary_max` | `salary_from`, `salary_to` | `salary` (текст) не нужен |
| `currency` | `salary_currency` | |
| — | `salary_gross`, `salary_period` | `NULL` (API не говорит) |
| `employment` | `employment` | |
| `schedule` | `schedule` | |
| `requirement.experience` | `experience_years` | `experience` — исходный текст, если есть |
| `requirement.education` | `education` | |
| `duty` | `responsibilities` | |
| `requirements` | `requirements` | |
| `term.text` | `conditions` | |
| `skills` | `skills_raw` | склеить через `'; '`; массив — в `vacancy_trudvsem.skills` |
| — | `description` | `NULL` |
| `creation-date` | `published_at` | |
| `date_modify` | `modified_at` | |
| время выгрузки | `first_seen_at`, `last_seen_at` | |
| — | `is_active` | `true`; после полной выгрузки — см. общие правила |

## vacancy_trudvsem

| Поле API | Колонка |
|---|---|
| `source` | `origin` (как есть: «Вакансия работодателя», …) |
| `work_places` | `work_places` |
| `conditions` | `work_conditions` |
| `workPlaceType` | `work_place_type` |
| `qualification` | `qualification` |
| `hireDate` | `hire_date` |
| `medicalDocuments` | `medical_documents` |
| `social_protected` | `social_protected` |
| `benefit` | `benefits` |
| `shift` | `shift` |
| `skills` | `skills` (массив) |

Не храним `contact_person`, `contact_list`, `company.email`.

## vacancy_snapshot

`format = 'json'`, `path` — файл JSONL относительно корня хранилища (например, `vacancies.jsonl`), `locator` — номер строки (с 1), `fetched_at` — время выгрузки, `sha256` — хэш строки, `http_status = NULL`. Сообщите владельцу БД, где будет лежать файл; копию `vacancies.backup.jsonl` не загружайте.

## Классификация

На каждый прогон `trudvsem-vpk` — строка `classifier_run`:

- `classifier = 'trudvsem_vpk_rules'`;
- `rules_sha256` — хэш `config/vpk_rules.toml`;
- `params`:
  - `sanctions_version` — версия выгрузки OpenSanctions;
  - `level_map`: `{"точно": "confirmed", "вероятно": "likely", "нет": "no", "вне охвата": "out_of_scope"}`;
  - `category_map`, если меняете категории;
- текст правил целиком — в `note` или в `params.rules_snapshot`.

| Таблица | Что писать |
|---|---|
| `vacancy_classification` | все 480 305 вакансий, включая «нет» (иначе не посчитать долю). `raw_label` = ваш уровень, `score`, `category` (производство / НИИ/КБ / ремонт / военная служба) |
| `employer_classification` | ваш уровень работодателя, по строке на **каждый** профиль с этим ИНН |
| `classification_evidence` | каждый сработавший признак отдельной строкой: `signal`, `origin` (`own_text` / `employer_text` / `company_name` / `sanctions`), `weight`, `snippet` |

Признаки берите **из классификатора**, а не из `vpk_vacancies.csv`: там они склеены в одну строку `vpk_evidence`. Удобнее всего дать `trudvsem-vpk` режим записи прямо в БД.

## OpenSanctions

Только российские организации (9 991):

- `opensanctions_entity`:
  - `os_id` — id сущности FtM;
  - `name`;
  - `programs` — массив;
  - `defense_hint`;
  - `dataset_version` — дата выгрузки.
- `opensanctions_code`: строки `('inn', код, os_id)` и `('ogrn', код, os_id)` — 15 522 кода.

Upsert по `os_id`. При новой версии датасета обновляйте `dataset_version`.

## Сопоставление с предприятиями

Не нужно делать самим. После загрузки мы сопоставим `employer_profile` с `company` по ИНН (+КПП) со статусом `auto`: у ГУР ИНН есть у ~5,6 тыс. компаний. Если у вас есть свои гипотезы (холдинг, по названию), пишите их со статусом `candidate` и `method` / `evidence`.

## Объём и скорость

480 тыс. вакансий с текстами — это порядка 1–1,5 ГБ в БД (оценка). Грузите пачками по 5–10 тыс. строк, через `COPY` во временную таблицу или `executemany`. Сервер в другой сети, задержка заметная: строковые `INSERT` по одному займут часы.

## Проверка

Кроме общих запросов из [README.md](README.md):

```sql
SELECT count(*) FROM vacancy WHERE source = 'trudvsem';                    -- ~480 305
SELECT count(DISTINCT inn) FROM employer_profile WHERE source = 'trudvsem';  -- ~82 140
SELECT count(*) FROM region;                                               -- 92
SELECT raw_label, count(*) FROM vacancy_classification c
JOIN classifier_run r USING (run_id) WHERE r.classifier = 'trudvsem_vpk_rules'
GROUP BY 1;   -- точно 3 773, вероятно 7 097, вне охвата 21 781, нет 447 654
SELECT count(*) FROM opensanctions_entity;                                 -- 9 991
```

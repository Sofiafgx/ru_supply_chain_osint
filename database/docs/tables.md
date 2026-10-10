# Таблицы и колонки

Цифры «заполнено» — по боевой БД на 05.10.2026 (непустые значения / строк в таблице). Колонка «Источник» — поле JSONL из `data/gur-*.jsonl`, из которого загружается значение. `*` в именах означает тройку `_uk`, `_ru`, `_en`.

## Компании

### company — 7530

Одна строка на предприятие. Профиль одинаков во всех разделах портала; при загрузке записи из четырёх разделов сливаются по `gur_id` (первое непустое значение, порядок: Ростех, БпЛА, заводы, санкции).

| Колонка | Тип | Заполнено | Смысл | Источник |
|---|---|---|---|---|
| `company_id` | bigserial PK | 7530 | суррогатный ключ | — |
| `gur_id` | int UNIQUE | 7530 | id на портале ГУР, общий для всех разделов | `gur_id` |
| `inn` | text | 5581 | ИНН: РФ — 10/12 цифр после исправления ошибок ввода; иностранные — рег. номер как есть | `inn` |
| `ogrn` | text | 6393 | ОГРН (13) / ОГРНИП (15); у иностранных — местный рег. номер | `ogrn` |
| `inn_raw`, `ogrn_raw` | text | 5581 / 6393 | значения как на портале, до исправлений | `inn_raw`, `ogrn_raw` |
| `swift`, `bik`, `bank_license` | text | 18 | банковские реквизиты (только у банков) | одноимённые |
| `name_full_*` | text | 7530 / 7529 / 7529 | полное название; `name_full_uk` NOT NULL | `name_full_*` |
| `name_short_*` | text | 1843 / 2245 / 1841 | краткое название | `name_short_*` |
| `country_id` | int FK `country` | 7479 | страна регистрации | `country_*` |
| `address_*` | text | 1324 | адрес одной строкой | `address_*` |
| `postal_code` | text | 1258 | индекс, 6 цифр из начала адреса (РФ) | `postal_code` |
| `region`, `city` | text | 0 | зарезервировано: разбор адреса ещё не сделан | — |
| `description_*` | text | 1305 | «Підстави»: деятельность, лицензии, холдинг, строки кооперации; абзацы через `\n` | `description_*` |
| `products_*` | text[] | 10 | блок «Продукція» (на портале почти всегда пуст) | `products_*` |
| `license_vvt` | text | 31 | номер лицензии на ВВТ, выделен регуляркой из описания (`002271 ВВТ-ОП`) | `license_vvt` |
| `status_raw` | text | 14 | бейдж статуса, например `Ліквідовано 01.12.2025` | `status` |
| `liquidated_on` | date | 13 | дата ликвидации из бейджа (у одного бейджа даты нет) | `liquidated_at` |
| `website`, `website_domain` | text | 947 | сайт компании — эвристика: первая ссылка не с агрегатора/СМИ/архива | `website*` |
| `logo_url` | text | 7529 | логотип (og:image) | `logo_url` |
| `sanctions_count` | smallint | 7530 | число юрисдикций с санкциями, включая UA | `sanctions_count` |
| `sanctions_count_intl` | smallint | 7530 | то же без UA; совпадает с бейджем портала «застосовані / ще не застосовані санкції» | `sanctions_count_intl` |
| `rostec_tree_level` | smallint | 767 | глубина в дереве Ростеха (0 — госкорпорация) | `tree_level` |
| `created_at`, `updated_at` | timestamptz | 7530 | служебные | — |

Индексы: `inn`, `ogrn`, `country_id` (не уникальные).

### company_section — 8173

В каких разделах портала есть компания. 6997 компаний — в одном разделе, 532 — в 2–4.

| Колонка | Тип | Смысл |
|---|---|---|
| `company_id` | PK, FK | |
| `section` | PK, text | `rostec` (767), `uav/companies` (162), `tools/company` (219), `sanctions/companies` (7025) |
| `url_*` | text | URL профиля в этом разделе на трёх языках |

### company_link — 8216

Блоки «Посилання» (`kind = 'source'`, 4545) и «Архівні посилання» (`kind = 'archive'`, 3671) в порядке на странице (`position` с 0). Списки объединены по всем разделам компании.

| Колонка | Тип | Смысл |
|---|---|---|
| `company_id`, `kind`, `position` | PK | |
| `url` | text | сайт компании, новости, checko.ru, web.archive.org, archive.today, ... |

## Санкции

### company_sanction — 61 559

Строка на каждую юрисдикцию, показанную в профиле, — в том числе без санкций. Санкций (`is_sanctioned`) — 13 208.

| Колонка | Тип | Смысл | Источник |
|---|---|---|---|
| `company_id`, `jurisdiction` | PK | `jurisdiction` → `sanction_jurisdiction.code` | `sanctions[].jurisdiction` |
| `is_sanctioned` | boolean | есть ли санкции | `sanctions[].sanctioned` |
| `listed_on` | date | дата внесения (у всех санкций) | `sanctions[].date` |
| `doc_url` | text | ссылка на документ (OFAC, EUR-Lex, указ президента Украины, ...) | `sanctions[].doc_url` |

Санкций по юрисдикциям: US 5445, EU 1779, CH 1421, GB 1045, CA 960, UA 859, JP 820, AU 501, NZ 378. **Строка UA есть только у 1327 компаний** — см. [data_notes.md](data_notes.md#санкции-украины).

Индекс: `(jurisdiction, listed_on) WHERE is_sanctioned`.

### sanction_jurisdiction — 9

Справочник: `code` (US, CA, CH, EU, GB, AU, NZ, JP, UA), `name_uk`. Заполняется миграцией 0001.

## Граф компаний

### company_edge — 1809

Все связи между компаниями. Направление: `company_id` → `related_id`.

| `kind` | `source` | Строк | Смысл |
|---|---|---|---|
| `parent` | `rostec_tree` | 766 | `related_id` — прямой родитель в дереве на странице /rostec |
| `parent` | `profile` | 766 | `related_id` — следующее звено цепочки «В структурі Ростех» в профиле |
| `bank` | `profile` | 75 | `related_id` — банк, обслуживающий компанию |
| `related` | `profile` | 153 | «Повʼязані компанії» (раздел БпЛА) |
| `successor` | `profile` | 11 | `related_id` — правопреемник ликвидированной компании |
| `supplier` | `vpk_atlas` | 38 | `related_id` поставляет продукцию `company_id` (заказчику); см. [data_notes.md](data_notes.md#атлас-впк-коллеги) |

| Колонка | Тип | Смысл |
|---|---|---|
| `label` | text | что поставляется (только `supplier`; по-украински, как в атласе, несколько через `; `) |
| `evidence_url` | text | страница, где связь указана (только `supplier`: профиль ГУР поставщика или заказчика) |

`company-db load-gur` удаляет и вставляет заново только строки с `source` `profile` и `rostec_tree`; строки других источников переживают перезагрузку ГУР.

PK `(company_id, related_id, kind, source)`; петли запрещены. Две версии иерархии расходятся у 76 компаний — см. [data_notes.md](data_notes.md#иерархия-ростеха).

## Финансы

### company_financial — 8

Финансовые показатели из пресс-релизов и публикаций в СМИ (миграция 0003), не из аудированной отчётности. Сейчас: Ростех (группа, 2024–2025), ОДК-УМПО и ОДК-Авиадвигатель (юрлицо, 2024).

| Колонка | Тип | Смысл |
|---|---|---|
| `company_id`, `year`, `metric`, `scope`, `source` | PK | |
| `metric` | text | `revenue`, `consolidated_revenue`, `net_profit`, `investment` |
| `scope` | text | `legal_entity` или `consolidated_group`; показатели группы и юрлиц **не суммировать** |
| `amount` | numeric(20,2) | сумма в единицах валюты (рубли, не миллиарды) |
| `currency` | char(3) | `RUB` |
| `amount_as_published` | text | как в источнике: `26.238144 млрд RUB` |
| `accounting_standard` | text | `RAS` или NULL |
| `evidence_type` | text | `corporate_statement` (заявление компании) или `secondary_reporting` (пересказ отчётности в СМИ) |
| `source_url` | text | rostec.ru, kommersant.ru, aviaport.ru |
| `source` | text | загрузчик: `vpk_atlas` |

## Вооружения

### weapon — 32

Виды вооружений, в производстве которых участвуют компании («Участь у кооперації з виробництва озброєння»). Числового id на портале нет.

| Колонка | Тип | Смысл |
|---|---|---|
| `weapon_slug` | text PK | `rm-48u`, `zircon`, `page-su-57`, `page-pantsir` (`page-*` — лендинги) |
| `url` | text | страница вооружения на портале |
| `name_*` | text | «Ударна ракета РМ-48У» |
| `image_url` | text | 31 из 32 |

### component — 307

Компоненты (`/components/part/{id}`), которые компании поставляют для вооружений: `part_id` (PK), `url`, `name_*` (например, «Безплатформенна інерційна навігаційна система БИНС2М-С-03»).

### company_weapon — 825

Компания участвует в кооперации по вооружению: `(company_id, weapon_slug)`. Полный список берётся со страницы «Дивитися усі» (на профиле он обрезан).

### company_weapon_component — 615

Компания поставляет компонент для вооружения: `(company_id, weapon_slug, part_id)`, FK на `company_weapon` и `component`.

## БпЛА

### uav_model — 209

| Колонка | Тип | Заполнено | Смысл |
|---|---|---|---|
| `uav_model_id` | serial PK | 209 | суррогатный ключ |
| `gur_uav_id` | int UNIQUE | 203 | id страницы `/uav/{id}` |
| `slug` | text UNIQUE | 6 | лендинги без id: Оріон, Ланцет, Скальпель, Клин, Князь Олег, V2U |
| `url_*` | text | 209 / 203 / 203 | у лендингов только `url_uk` |
| `name_*` | text | 209 | «Велес» / «Veles» |
| `purpose_*` | text | 209 | назначение: «FPV-камікадзе», «Ударний / Розвідувальний», ... |
| `image_url` | text | 209 | |

CHECK: заполнен `gur_uav_id` или `slug`.

### uav_model_spec — 1233

Заявленные характеристики, «ключ–значение» (42 разных ключа). Есть только у моделей с `gur_uav_id`.

| Колонка | Тип | Смысл |
|---|---|---|
| `uav_model_id`, `spec_key` | PK | `spec_key` — английская подпись в snake_case |
| `position` | smallint | порядок строк на странице |
| `label_*` | text | подпись как на портале: «Дальність польоту» / «Flight range» |
| `value_*` | text | значение с единицами, свободный текст: «до 60 км/год», «up to 2.5 kg» |

Частые ключи: `flight_time` 138, `target_load_weight_max` 130, `flight_range` 108, `take_off_weight_max` 96, `maximum_speed` 88, `maximum_flight_altitude` 72, `speed` 66, `operating_temperature_range` 60, `wingspan` 39, `payload` 37, `engine` 33.

### company_uav_model — 402

Компания — производитель модели: `(company_id, uav_model_id)`. Объединение двух источников: карточки дронов в профилях компаний и список производителей на странице модели.

## Оборудование

### equipment_manufacturer — 460

Производители станков: `manufacturer_id` (id фильтра портала `/tools?f[manufacturer_id]=`), `name` (DMG Mori Seiki, Haas, KOVOSVIT MAS, Hermle, ...).

### tool — 1567

Иностранный станок на заводе. Каждый привязан к заводу.

| Колонка | Тип | Заполнено | Смысл | Источник |
|---|---|---|---|---|
| `tool_id` | int PK | 1567 | id `/tools/{id}` | `tool_id` |
| `plant_company_id` | bigint FK `company` | 1567 | завод, где стоит станок | `plant_gur_id` |
| `manufacturer_id` | int FK | 1567 | производитель | `manufacturer_id`, `manufacturer` |
| `url_*` | text | 1567 | | `url_*` |
| `title_*` | text | 1567 | заголовок страницы: «<производитель> <название>» | `title_*` |
| `name_*` | text | 1567 | наименование и маркировка: «Універсальний обробний центр DMU 50»; 98 — «Не ідентифіковано» | `name_*` |
| `cnc_model_*` | text | 126 | марка/модель системы ЧПУ | `cnc_model_*` |
| `extra_info_*` | text | 20 | дополнительная информация | `extra_info_*` |
| `plant_weapons_*` | text[] | 955 | вооружения, которые выпускает завод (как на карточке, без ссылок) | `weapons_*` |
| `quantity` | int | 1567 | количество (по умолчанию 1); сумма = 1655, это и есть «Верстатів» на портале | `quantity` |
| `serial` | text | 344 | серийный номер | `serial` |
| `published_on` | date | 1567 | дата публикации карточки | `published_at` |
| `photo_url` | text | 1566 | фото станка | `photo_url` |

Индексы: `plant_company_id`, `manufacturer_id`.

### tool_country — 1731

Страны производителя станка `(tool_id, country_id)`; у части станков их две (например, Германия и Япония).

### tool_evidence — 1602

Доказательства использования станка на заводе, по порядку на странице (`position`). 1554 станка имеют хотя бы одно, максимум 4.

| Колонка | Тип | Заполнено | Смысл |
|---|---|---|---|
| `tool_id`, `position` | PK | 1602 | |
| `evidence_type` | text | 1602 | выводится загрузчиком по ссылкам: `procurement` 1136, `video` 319, `leasing` 52, `other` 95 |
| `published_on` | date | 1555 | дата доказательства, если это полная дата |
| `published_raw` | text | 1569 | дата как на портале: в т.ч. «2014-2015», «06.2023», «-» |
| `author`, `author_url` | text | 366 | автор видео и ссылка (часто YouTube с таймкодом) |
| `timecode` | text | 304 | таймкод в видео |
| `leasing_term` | text | 31 | срок лизинга: «з 26.07.2024 - по 31.05.2026» |
| `video_urls` | text[] | 318 | встроенные видео (Google Drive и др.) |
| `source_urls` | text[] | 1285 | источник: zakupki.gov.ru, fedresurs.ru, clearspending.ru, archive.ph, ... |
| `notes` | text[] | 1 | текст без ссылки |

### tool_document — 3263

Документы к станку (`tool_id`, `position`, `title`, `url`), у 1065 станков. `url` — ссылка портала `download-controler?...`; сами файлы не скачаны.

### tool_photo — 156

Фото-доказательства: `(tool_id, position, url)`.

## Вакансии (миграция 0002, пока пусто)

Подробное соответствие полей источников колонкам — в [loading/hh.md](loading/hh.md) и [loading/trudvsem.md](loading/trudvsem.md); решения — в [proposals/vacancies.md](proposals/vacancies.md).

| Таблица | Ключ | Что хранит |
|---|---|---|
| `vacancy_source` | `code` | сайты-источники: `hh`, `trudvsem`; `raw_storage` — где лежат сырые файлы |
| `region` | `region_id`; `name`, `kladr_code` UNIQUE | субъекты РФ (из trudvsem) |
| `employer_profile` | `(source, id_namespace, external_id)` | работодатель на сайте: имя, URL, ИНН/ОГРН/КПП как показывает сайт, `publisher_type` (`employer` / `agency` / `unknown`), первое/последнее появление |
| `employer_company_match` | `(employer_profile_id, company_id)` | профиль ↔ юрлицо `company`: `status` (`auto` / `candidate` / `predicted` / `verified` / `rejected`), `method`, `confidence`, `evidence`; `verified` требует проверяющего, время и доказательства |
| `vacancy` | `vacancy_id`; `(source, external_id)` UNIQUE | общие поля вакансии: название, профессия, отрасль, `region_id` (субъект) + `locality` (город), адрес и координаты, зарплата (`from`/`to`/`currency`/`gross`/`period`), занятость, график, опыт, образование, тексты, `skills_raw`, даты публикации/изменения, `first_seen_at` / `last_seen_at` / `is_active` |
| `vacancy_hh` | `vacancy_id` | только HH: имя работодателя в деталях, статус разрешения работодателя, тип адреса, статус карточки, видимость |
| `vacancy_trudvsem` | `vacancy_id` | только trudvsem: тип источника (`origin`), рабочие места, класс условий труда, тип рабочего места, квалификация, дата найма, медкнижка, соцзащищённые, льготы, сменность, массив навыков |
| `vacancy_discovery` | `(vacancy_id, route)` | маршруты поиска, которыми найдена вакансия (HH) |
| `vacancy_snapshot` | `(vacancy_id, fetched_at)` | где лежат сырые данные: `format` (`html` / `json`), `path`, `locator` (строка в JSONL), `http_status`, `sha256` |
| `vacancy_company_match` | `(vacancy_id, company_id, method)` | вакансия ↔ фактический наниматель (агентство публикует за завод, NLP, ручная проверка) |

`company.kpp` (тоже 0002) — КПП юрлица; у данных ГУР пуст.

## Классификация ВПК (миграция 0002, пока пусто)

| Таблица | Ключ | Что хранит |
|---|---|---|
| `classifier_run` | `run_id` | прогон: `classifier` (`trudvsem_vpk_rules`, `hh_filter`, ...), версия, хэш правил, `params` (версия санкций, `level_map`), время |
| `vacancy_classification` | `(run_id, vacancy_id)` | `level` (`confirmed` / `likely` / `review` / `no` / `out_of_scope`), исходная `raw_label`, `score`, `category` |
| `employer_classification` | `(run_id, employer_profile_id)` | то же для работодателя |
| `classification_evidence` | — (FK на `vacancy_classification`) | сработавшие признаки построчно: `signal`, `origin`, `weight`, `snippet` |
| `manual_review` | `review_id` | ручной вердикт по вакансии или работодателю: `vpk` / `not_vpk` / `unclear`, кто и когда |

## OpenSanctions (миграция 0002, пока пусто)

| Таблица | Ключ | Что хранит |
|---|---|---|
| `opensanctions_entity` | `os_id` | организация из датасета OpenSanctions: имя, программы (`UA-SA1644`, `US-RUSHAR`, ...), оборонная тематика, версия датасета |
| `opensanctions_code` | `(kind, code, os_id)` | её ИНН и ОГРН; связь с `company` и `employer_profile` — запросом по коду |

Санкции ГУР (`company_sanction`) и OpenSanctions хранятся раздельно: разная гранулярность (юрисдикции против программ) и разные сущности.

## Служебные

`schema_migration (version, applied_at)` — журнал применённых миграций.

## Справочники и происхождение

### country — 117

| Колонка | Тип | Заполнено | Смысл |
|---|---|---|---|
| `country_id` | serial PK | 117 | |
| `gur_country_id` | int UNIQUE | 34 | id фильтра портала — есть только у стран производителей станков |
| `name_uk` | text UNIQUE | 117 | ключ сопоставления при загрузке: «російська федерація», «Китайська Народна Республіка», «ТОТ України» |
| `name_ru`, `name_en` | text | 117 | |
| `iso2` | char(2) | 0 | зарезервировано |

### source_record — 9949

Откуда взята каждая сущность. Перезаписывается при каждой загрузке ГУР (`source = 'gur'`).

| Колонка | Тип | Смысл |
|---|---|---|
| `source_record_id` | bigserial PK | |
| `source` | text | `gur` (дальше: `hh`, `egrul`, ...) |
| `section` | text | раздел портала: `rostec`, `uav/companies`, `tools/company`, `sanctions/companies`, `tools`, `uav` |
| `entity_type`, `entity_key` | text | `company` → `company_id`, `tool` → `tool_id`, `uav_model` → `uav_model_id` (строкой) |
| `url` | text | украинская страница сущности |
| `raw_html_path` | text | сохранённый HTML в `data/raw/...` (нет у 6 лендингов БпЛА) |
| `scraped_at` | timestamptz | время разбора |

Компания из нескольких разделов имеет несколько строк (по одной на раздел).

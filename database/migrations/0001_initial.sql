-- Enterprise database schema (PostgreSQL 15+).
-- Source of the first load: GUR "War & Sanctions" JSONL files in data/.
-- Documentation (columns, JSONL mapping, fill rates, data quirks, example queries): db/docs/.
--
-- Conventions
--   * company_id is a surrogate key: GUR ids are kept in company.gur_id, and future sources
--     (hh.ru, EGRUL, ...) may add companies that have no GUR id.
--   * INN/OGRN are NOT unique: branches share the parent's INN and OGRN (19 such INNs today).
--   * Translated text is stored as *_uk / *_ru / *_en columns (the portal is trilingual).
--   * Every edge between companies lives in company_edge, so graph queries need one table.

-- ---------------------------------------------------------------------------------------------
-- Reference tables
-- ---------------------------------------------------------------------------------------------

CREATE TABLE country (
    country_id      serial PRIMARY KEY,
    gur_country_id  int UNIQUE,              -- id in the portal's /tools filters (only for
                                             -- equipment manufacturer countries)
    name_uk         text NOT NULL UNIQUE,
    name_ru         text,
    name_en         text,
    iso2            char(2)                  -- not on the portal; fill later if needed
);

CREATE TABLE sanction_jurisdiction (
    code     text PRIMARY KEY,              -- US, CA, CH, EU, GB, AU, NZ, JP, UA
    name_uk  text NOT NULL
);

INSERT INTO sanction_jurisdiction (code, name_uk) VALUES
    ('US', 'США'), ('CA', 'Канада'), ('CH', 'Швейцарія'), ('EU', 'ЄС'),
    ('GB', 'Велика Британія'), ('AU', 'Австралія'), ('NZ', 'Нова Зеландія'),
    ('JP', 'Японія'), ('UA', 'Україна');

-- ---------------------------------------------------------------------------------------------
-- Companies
-- ---------------------------------------------------------------------------------------------

CREATE TABLE company (
    company_id            bigserial PRIMARY KEY,
    gur_id                int UNIQUE,        -- one id space across all portal sections

    -- identifiers (normalized; originals kept for audit)
    inn                   text,              -- RU: 10/12 digits; foreign: verbatim
    ogrn                  text,              -- RU: 13 (OGRN) / 15 (OGRNIP); foreign: reg. number
    inn_raw               text,
    ogrn_raw              text,
    swift                 text,
    bik                   text,
    bank_license          text,

    name_full_uk          text NOT NULL,
    name_full_ru          text,
    name_full_en          text,
    name_short_uk         text,
    name_short_ru         text,
    name_short_en         text,

    country_id            int REFERENCES country,
    address_uk            text,
    address_ru            text,
    address_en            text,
    postal_code           text,
    region                text,              -- not parsed yet (LLM / regex stage)
    city                  text,              -- not parsed yet

    description_uk        text,              -- «Підстави»: licence, holding, cooperation lines
    description_ru        text,
    description_en        text,
    products_uk           text[],            -- almost always empty on the portal
    products_ru           text[],
    products_en           text[],
    license_vvt           text,              -- e.g. '002271 ВВТ-ОП', regex from description

    status_raw            text,              -- e.g. 'Ліквідовано 01.12.2025'
    liquidated_on         date,

    website               text,              -- heuristic: first non-aggregator link
    website_domain        text,
    logo_url              text,

    sanctions_count       smallint NOT NULL DEFAULT 0,  -- all jurisdictions incl. UA
    sanctions_count_intl  smallint NOT NULL DEFAULT 0,  -- without UA (portal's badge)

    rostec_tree_level     smallint,          -- only for Rostec companies

    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX company_inn_idx  ON company (inn);
CREATE INDEX company_ogrn_idx ON company (ogrn);
CREATE INDEX company_country_idx ON company (country_id);

-- Portal sections a company appears in (532 companies are in 2-4 sections).
CREATE TABLE company_section (
    company_id  bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    section     text   NOT NULL CHECK (section IN (
                    'rostec', 'uav/companies', 'tools/company', 'sanctions/companies')),
    url_uk      text NOT NULL,
    url_ru      text,
    url_en      text,
    PRIMARY KEY (company_id, section)
);

-- One row per jurisdiction shown on the profile, including "not sanctioned" ones,
-- so absence of sanctions is explicit.
CREATE TABLE company_sanction (
    company_id     bigint  NOT NULL REFERENCES company ON DELETE CASCADE,
    jurisdiction   text    NOT NULL REFERENCES sanction_jurisdiction,
    is_sanctioned  boolean NOT NULL,
    listed_on      date,
    doc_url        text,
    PRIMARY KEY (company_id, jurisdiction)
);

CREATE INDEX company_sanction_listed_idx ON company_sanction (jurisdiction, listed_on)
    WHERE is_sanctioned;

-- All company-to-company relations.
--   parent     : company_id is a subsidiary of related_id
--                source 'rostec_tree' = embedded tree on /rostec (direct parent),
--                source 'profile'     = «В структурі Ростех» chain (consecutive pairs);
--                the two disagree for 76 companies, both are kept.
--   bank       : related_id is a bank serving company_id («Банки, які обслуговують»)
--   related    : «Повʼязані компанії» (UAV section)
--   successor  : related_id is the legal successor of a liquidated company_id
CREATE TABLE company_edge (
    company_id  bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    related_id  bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    kind        text   NOT NULL CHECK (kind IN ('parent', 'bank', 'related', 'successor')),
    source      text   NOT NULL DEFAULT 'profile',
    PRIMARY KEY (company_id, related_id, kind, source),
    CHECK (company_id <> related_id)
);

CREATE INDEX company_edge_related_idx ON company_edge (related_id, kind);

-- «Посилання» and «Архівні посилання», in page order.
CREATE TABLE company_link (
    company_id  bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    kind        text   NOT NULL CHECK (kind IN ('source', 'archive')),
    position    smallint NOT NULL,
    url         text   NOT NULL,
    PRIMARY KEY (company_id, kind, position)
);

-- ---------------------------------------------------------------------------------------------
-- Weapons and components (cooperation in weapons production)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE weapon (
    weapon_slug  text PRIMARY KEY,           -- 'rm-48u', 'zircon', 'page-su-57' (no numeric id)
    url          text NOT NULL,
    name_uk      text,
    name_ru      text,
    name_en      text,
    image_url    text
);

CREATE TABLE component (
    part_id  int PRIMARY KEY,                -- /components/part/{id}
    url      text NOT NULL,
    name_uk  text,
    name_ru  text,
    name_en  text
);

-- «Участь підприємства у кооперації з виробництва озброєння»
CREATE TABLE company_weapon (
    company_id   bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    weapon_slug  text   NOT NULL REFERENCES weapon,
    PRIMARY KEY (company_id, weapon_slug)
);

-- Components a company supplies for a given weapon (second weapon-card layout).
CREATE TABLE company_weapon_component (
    company_id   bigint NOT NULL,
    weapon_slug  text   NOT NULL,
    part_id      int    NOT NULL REFERENCES component,
    PRIMARY KEY (company_id, weapon_slug, part_id),
    FOREIGN KEY (company_id, weapon_slug) REFERENCES company_weapon ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------------------------
-- UAV models
-- ---------------------------------------------------------------------------------------------

CREATE TABLE uav_model (
    uav_model_id  serial PRIMARY KEY,
    gur_uav_id    int UNIQUE,                -- /uav/{id}
    slug          text UNIQUE,               -- landing pages /page-<slug> have no id
    url_uk        text NOT NULL,
    url_ru        text,
    url_en        text,
    name_uk       text NOT NULL,
    name_ru       text,
    name_en       text,
    purpose_uk    text,                      -- 'FPV-камікадзе', 'Ударний / Розвідувальний'
    purpose_ru    text,
    purpose_en    text,
    image_url     text,
    CHECK (gur_uav_id IS NOT NULL OR slug IS NOT NULL)
);

-- Declared specs; 42 distinct keys, so key-value rather than columns.
-- spec_key is the snake_cased English label ('take_off_weight_max', 'cruise_speed').
CREATE TABLE uav_model_spec (
    uav_model_id  int  NOT NULL REFERENCES uav_model ON DELETE CASCADE,
    spec_key      text NOT NULL,
    position      smallint NOT NULL,
    label_uk      text,
    label_ru      text,
    label_en      text,
    value_uk      text,                      -- free text with units: 'до 60 км/год'
    value_ru      text,
    value_en      text,
    PRIMARY KEY (uav_model_id, spec_key)
);

-- Manufacturers of a model (from the model page and from company profiles).
CREATE TABLE company_uav_model (
    company_id    bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    uav_model_id  int    NOT NULL REFERENCES uav_model ON DELETE CASCADE,
    PRIMARY KEY (company_id, uav_model_id)
);

-- ---------------------------------------------------------------------------------------------
-- Foreign equipment (machine tools) at plants
-- ---------------------------------------------------------------------------------------------

CREATE TABLE equipment_manufacturer (
    manufacturer_id  int PRIMARY KEY,        -- portal id from /tools?f[manufacturer_id]=
    name             text NOT NULL           -- 'DMG Mori Seiki', 'Haas', ...
);

CREATE TABLE tool (
    tool_id           int PRIMARY KEY,       -- /tools/{id}
    plant_company_id  bigint NOT NULL REFERENCES company,
    manufacturer_id   int REFERENCES equipment_manufacturer,
    url_uk            text NOT NULL,
    url_ru            text,
    url_en            text,
    title_uk          text,                  -- '<manufacturer> <name>' page heading
    title_ru          text,
    title_en          text,
    name_uk           text,                  -- 'Універсальний обробний центр DMU 50'
    name_ru           text,
    name_en           text,
    cnc_model_uk      text,
    cnc_model_ru      text,
    cnc_model_en      text,
    extra_info_uk     text,
    extra_info_ru     text,
    extra_info_en     text,
    plant_weapons_uk  text[],                -- weapons made at the plant, as shown on the card
    plant_weapons_ru  text[],
    plant_weapons_en  text[],
    quantity          int NOT NULL DEFAULT 1, -- portal totals (1655) are the sum of this
    serial            text,
    published_on      date,
    photo_url         text
);

CREATE INDEX tool_plant_idx ON tool (plant_company_id);
CREATE INDEX tool_manufacturer_idx ON tool (manufacturer_id);

CREATE TABLE tool_country (
    tool_id     int NOT NULL REFERENCES tool ON DELETE CASCADE,
    country_id  int NOT NULL REFERENCES country,
    PRIMARY KEY (tool_id, country_id)
);

-- Evidence that the tool is used at the plant; several per tool, in page order.
-- evidence_type is derived by the loader: 'video' (video_urls or YouTube/Rutube author link),
-- 'procurement' (zakupki.gov.ru, clearspending.ru), 'leasing' (fedresurs.ru or leasing_term),
-- otherwise 'other' — mirrors the portal's «Тип доказів» filter.
CREATE TABLE tool_evidence (
    tool_id        int      NOT NULL REFERENCES tool ON DELETE CASCADE,
    position       smallint NOT NULL,
    evidence_type  text     CHECK (evidence_type IN ('video', 'procurement', 'leasing', 'other')),
    published_on   date,                     -- NULL when the portal value is not a full date
    published_raw  text,                     -- original value: '2014-2015', '06.2023', ...
    author         text,
    author_url     text,
    timecode       text,
    leasing_term   text,                     -- 'з 26.07.2024 - по 31.05.2026'
    video_urls     text[],
    source_urls    text[],
    notes          text[],
    PRIMARY KEY (tool_id, position)
);

CREATE TABLE tool_document (
    tool_id   int      NOT NULL REFERENCES tool ON DELETE CASCADE,
    position  smallint NOT NULL,
    title     text,
    url       text     NOT NULL,             -- portal download link; files not downloaded
    PRIMARY KEY (tool_id, position)
);

CREATE TABLE tool_photo (
    tool_id   int      NOT NULL REFERENCES tool ON DELETE CASCADE,
    position  smallint NOT NULL,
    url       text     NOT NULL,
    PRIMARY KEY (tool_id, position)
);

-- ---------------------------------------------------------------------------------------------
-- Provenance
-- ---------------------------------------------------------------------------------------------

-- Where each entity came from; lets a record be re-parsed from the cached raw HTML.
CREATE TABLE source_record (
    source_record_id  bigserial PRIMARY KEY,
    source            text NOT NULL,         -- 'gur' (later: 'hh', 'egrul', ...)
    section           text,                  -- 'rostec', 'tools', ...
    entity_type       text NOT NULL CHECK (entity_type IN ('company', 'tool', 'uav_model')),
    entity_key        text NOT NULL,         -- company_id / tool_id / uav_model_id as text
    url               text,
    raw_html_path     text,
    scraped_at        timestamptz NOT NULL
);

CREATE INDEX source_record_entity_idx ON source_record (entity_type, entity_key);

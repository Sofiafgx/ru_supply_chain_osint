-- 0002: vacancies from job sites (HH, trudvsem), employer profiles, VPK classification,
-- OpenSanctions. Design and decisions: db/docs/proposals/vacancies.md.
-- Loading guides for the source owners: db/docs/loading/.

-- ---------------------------------------------------------------------------------------------
-- Companies: branch code (trudvsem gives INN + KPP; branches share the parent's INN)
-- ---------------------------------------------------------------------------------------------

ALTER TABLE company ADD COLUMN kpp text;

-- ---------------------------------------------------------------------------------------------
-- Sources and regions
-- ---------------------------------------------------------------------------------------------

CREATE TABLE vacancy_source (
    code         text PRIMARY KEY,           -- 'hh', 'trudvsem'
    name         text NOT NULL,
    base_url     text,
    raw_storage  text                        -- where raw files live (vacancy_snapshot.path is
                                             -- relative to it), e.g. a bucket or shared drive
);

INSERT INTO vacancy_source (code, name, base_url) VALUES
    ('hh', 'HeadHunter', 'https://hh.ru'),
    ('trudvsem', 'Работа в России', 'https://trudvsem.ru');

-- Subject of the Russian Federation (not a city).
CREATE TABLE region (
    region_id   serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,        -- 'Тульская область'
    kladr_code  text UNIQUE                  -- 13 digits (trudvsem region_code)
);

-- ---------------------------------------------------------------------------------------------
-- Employers as published by a site, and their legal entities
-- ---------------------------------------------------------------------------------------------

-- Requisites are what the site shows: a claim of the site, not a verified fact.
CREATE TABLE employer_profile (
    employer_profile_id  bigserial PRIMARY KEY,
    source               text NOT NULL REFERENCES vacancy_source,
    id_namespace         text NOT NULL DEFAULT '',   -- HH employer_id_namespace; '' otherwise
    external_id          text NOT NULL,              -- HH employer id / trudvsem companycode
    name                 text NOT NULL,
    url                  text,
    inn                  text,
    ogrn                 text,
    kpp                  text,
    publisher_type       text NOT NULL DEFAULT 'unknown'
                         CHECK (publisher_type IN ('employer', 'agency', 'unknown')),
    first_seen_at        timestamptz NOT NULL,
    last_seen_at         timestamptz NOT NULL,
    UNIQUE (source, id_namespace, external_id)
);

CREATE INDEX employer_profile_inn_idx ON employer_profile (inn, kpp);

-- Profile -> legal entity. An automatic INN match is 'auto', never 'verified'.
CREATE TABLE employer_company_match (
    employer_profile_id  bigint NOT NULL REFERENCES employer_profile ON DELETE CASCADE,
    company_id           bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    status               text   NOT NULL
                         CHECK (status IN ('auto', 'candidate', 'predicted', 'verified', 'rejected')),
    method               text   NOT NULL,     -- 'inn_kpp', 'inn', 'name_region', 'nlp', 'manual'
    confidence           real,                -- not a calibrated probability
    model_version        text,
    evidence             jsonb,
    reviewed_by          text,
    reviewed_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (employer_profile_id, company_id),
    CHECK (status <> 'verified'
           OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND evidence IS NOT NULL))
);

CREATE INDEX employer_company_match_company_idx ON employer_company_match (company_id);

-- ---------------------------------------------------------------------------------------------
-- Vacancies: common core + one 1:1 table per source for source-only fields
-- ---------------------------------------------------------------------------------------------

CREATE TABLE vacancy (
    vacancy_id           bigserial PRIMARY KEY,
    source               text NOT NULL REFERENCES vacancy_source,
    external_id          text NOT NULL,            -- HH id / trudvsem UUID
    url                  text NOT NULL,
    employer_profile_id  bigint REFERENCES employer_profile,   -- NULL: profile unknown
    employer_name        text,                     -- publisher name as shown on the card

    title                text NOT NULL,
    profession           text,
    profession_code      text,
    specialisation       text,                     -- industry by the site's classifier

    region_id            int REFERENCES region,    -- subject of the RF
    locality             text,                     -- city / settlement
    address              text,
    lat                  numeric(9,6),
    lng                  numeric(9,6),

    salary_from          numeric(18,2),
    salary_to            numeric(18,2),
    salary_currency      text,
    salary_gross         boolean,                  -- NULL = unknown
    salary_period        text,                     -- 'unspecified_by_source' is not 'month'

    employment           text,
    schedule             text,
    experience           text,                     -- as on the site
    experience_years     smallint,
    education            text,

    description          text,                     -- full text (HH)
    responsibilities     text,                     -- HH responsibilities / trudvsem duty
    requirements         text,
    conditions           text,                     -- HH conditions / trudvsem term.text
    skills_raw           text,                     -- as published; do not split on commas

    published_at         timestamptz,
    modified_at          timestamptz,
    first_seen_at        timestamptz NOT NULL,
    last_seen_at         timestamptz NOT NULL,
    is_active            boolean NOT NULL DEFAULT true,

    UNIQUE (source, external_id),
    CHECK (last_seen_at >= first_seen_at)
);

CREATE INDEX vacancy_employer_idx ON vacancy (employer_profile_id);
CREATE INDEX vacancy_region_idx   ON vacancy (region_id);
CREATE INDEX vacancy_modified_idx ON vacancy (modified_at);
CREATE INDEX vacancy_active_idx   ON vacancy (source, is_active);

CREATE TABLE vacancy_hh (
    vacancy_id                  bigint PRIMARY KEY REFERENCES vacancy ON DELETE CASCADE,
    employer_name_detail        text,
    employer_resolution_status  text,
    address_type                text,
    detail_status               text,
    source_visibility           text
);

CREATE TABLE vacancy_trudvsem (
    vacancy_id         bigint PRIMARY KEY REFERENCES vacancy ON DELETE CASCADE,
    origin             text,       -- API 'source': работодатель / интернет-ресурс / агентство / ...
    work_places        int,
    work_conditions    text,       -- class of working conditions: «Вредные», ...
    work_place_type    text,       -- обычное / квотируемое / спец. / для иностранцев
    qualification      text,
    hire_date          date,
    medical_documents  text,
    social_protected   text,
    benefits           text,
    shift              text,
    skills             text[]      -- original array
);

-- Every search route a vacancy was found through (HH). Dates are NULL when unknown.
CREATE TABLE vacancy_discovery (
    vacancy_id     bigint NOT NULL REFERENCES vacancy ON DELETE CASCADE,
    route          text   NOT NULL,
    first_seen_at  timestamptz,
    last_seen_at   timestamptz,
    PRIMARY KEY (vacancy_id, route)
);

-- Raw data stays in files; the database knows where and how to verify it.
CREATE TABLE vacancy_snapshot (
    vacancy_id   bigint      NOT NULL REFERENCES vacancy ON DELETE CASCADE,
    fetched_at   timestamptz NOT NULL,
    format       text        NOT NULL CHECK (format IN ('html', 'json')),
    path         text        NOT NULL,     -- relative to vacancy_source.raw_storage
    locator      text,                     -- line number / byte offset inside a JSONL file
    http_status  smallint,
    sha256       text,
    PRIMARY KEY (vacancy_id, fetched_at)
);

-- Vacancy -> the company that actually hires (agency posting for a plant, NLP, manual review).
CREATE TABLE vacancy_company_match (
    vacancy_id     bigint NOT NULL REFERENCES vacancy ON DELETE CASCADE,
    company_id     bigint NOT NULL REFERENCES company ON DELETE CASCADE,
    status         text   NOT NULL
                   CHECK (status IN ('candidate', 'predicted', 'verified', 'rejected')),
    method         text   NOT NULL,
    confidence     real,
    model_version  text,
    evidence       jsonb,
    reviewed_by    text,
    reviewed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (vacancy_id, company_id, method),
    CHECK (status <> 'verified'
           OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND evidence IS NOT NULL))
);

CREATE INDEX vacancy_company_match_company_idx ON vacancy_company_match (company_id);

-- ---------------------------------------------------------------------------------------------
-- VPK classification: runs, normalized levels + original labels, per-signal evidence
-- ---------------------------------------------------------------------------------------------

CREATE TABLE classifier_run (
    run_id        serial PRIMARY KEY,
    classifier    text NOT NULL,          -- 'trudvsem_vpk_rules', 'hh_filter', 'nlp_...'
    version       text,
    rules_sha256  text,
    params        jsonb,                  -- sanctions version, thresholds, level_map, ...
    started_at    timestamptz NOT NULL,
    note          text
);

CREATE TABLE vacancy_classification (
    run_id      int    NOT NULL REFERENCES classifier_run ON DELETE CASCADE,
    vacancy_id  bigint NOT NULL REFERENCES vacancy ON DELETE CASCADE,
    level       text   NOT NULL
                CHECK (level IN ('confirmed', 'likely', 'review', 'no', 'out_of_scope')),
    raw_label   text   NOT NULL,          -- 'точно', 'candidate_strong_signal', ...
    score       real,
    category    text,
    PRIMARY KEY (run_id, vacancy_id)
);

CREATE INDEX vacancy_classification_level_idx ON vacancy_classification (run_id, level);

CREATE TABLE employer_classification (
    run_id               int    NOT NULL REFERENCES classifier_run ON DELETE CASCADE,
    employer_profile_id  bigint NOT NULL REFERENCES employer_profile ON DELETE CASCADE,
    level                text   NOT NULL
                         CHECK (level IN ('confirmed', 'likely', 'review', 'no', 'out_of_scope')),
    raw_label            text   NOT NULL,
    score                real,
    category             text,
    PRIMARY KEY (run_id, employer_profile_id)
);

CREATE TABLE classification_evidence (
    run_id      int    NOT NULL,
    vacancy_id  bigint NOT NULL,
    signal      text   NOT NULL,          -- 'санкции', 'гособоронзаказ / военная приёмка', ...
    origin      text   NOT NULL,          -- own_text / employer_text / company_name / sanctions
    weight      real,
    snippet     text,
    FOREIGN KEY (run_id, vacancy_id) REFERENCES vacancy_classification ON DELETE CASCADE
);

CREATE INDEX classification_evidence_vacancy_idx ON classification_evidence (run_id, vacancy_id);

CREATE TABLE manual_review (
    review_id            bigserial PRIMARY KEY,
    vacancy_id           bigint REFERENCES vacancy ON DELETE CASCADE,
    employer_profile_id  bigint REFERENCES employer_profile ON DELETE CASCADE,
    verdict              text NOT NULL CHECK (verdict IN ('vpk', 'not_vpk', 'unclear')),
    comment              text,
    reviewed_by          text NOT NULL,
    reviewed_at          timestamptz NOT NULL,
    CHECK ((vacancy_id IS NULL) <> (employer_profile_id IS NULL))
);

-- ---------------------------------------------------------------------------------------------
-- OpenSanctions (separate from GUR company_sanction: programs, not jurisdictions)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE opensanctions_entity (
    os_id            text PRIMARY KEY,
    name             text   NOT NULL,
    programs         text[] NOT NULL,     -- 'UA-SA1644', 'US-RUSHAR', ...
    defense_hint     text,
    dataset_version  text   NOT NULL
);

CREATE TABLE opensanctions_code (
    kind   text NOT NULL CHECK (kind IN ('inn', 'ogrn')),
    code   text NOT NULL,
    os_id  text NOT NULL REFERENCES opensanctions_entity ON DELETE CASCADE,
    PRIMARY KEY (kind, code, os_id)
);

CREATE INDEX opensanctions_code_code_idx ON opensanctions_code (code);

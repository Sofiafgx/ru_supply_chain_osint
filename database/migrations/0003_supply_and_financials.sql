-- 0003: supply relations and financial figures from the colleague's VPK atlas
-- (other_vacancies/vpk-structured.json). Loader: company-db load-vpk-atlas.

-- ---------------------------------------------------------------------------------------------
-- company_edge: supplier edges with what is supplied and where it is stated
-- ---------------------------------------------------------------------------------------------

--   supplier   : related_id supplies company_id (company_id is the customer),
--                as stated in a GUR profile description; label = what is supplied
ALTER TABLE company_edge DROP CONSTRAINT company_edge_kind_check;
ALTER TABLE company_edge ADD CONSTRAINT company_edge_kind_check
    CHECK (kind IN ('parent', 'bank', 'related', 'successor', 'supplier'));

ALTER TABLE company_edge ADD COLUMN label        text;   -- e.g. 'Насоси та паливні фільтри'
ALTER TABLE company_edge ADD COLUMN evidence_url text;   -- page that states the relation

-- ---------------------------------------------------------------------------------------------
-- Financial figures (press releases and secondary reporting, not audited statements)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE company_financial (
    company_id           bigint   NOT NULL REFERENCES company ON DELETE CASCADE,
    year                 smallint NOT NULL,
    metric               text     NOT NULL CHECK (metric IN (
                             'revenue', 'consolidated_revenue', 'net_profit', 'investment')),
    scope                text     NOT NULL CHECK (scope IN ('legal_entity', 'consolidated_group')),
    amount               numeric(20, 2) NOT NULL,
    currency             char(3)  NOT NULL DEFAULT 'RUB',
    amount_as_published  text,               -- e.g. '26.238144 млрд RUB'
    accounting_standard  text,               -- 'RAS', ...
    evidence_type        text     NOT NULL CHECK (evidence_type IN (
                             'corporate_statement', 'secondary_reporting')),
    source_url           text     NOT NULL,
    source               text     NOT NULL,  -- loader that wrote the row: 'vpk_atlas'
    PRIMARY KEY (company_id, year, metric, scope, source)
);

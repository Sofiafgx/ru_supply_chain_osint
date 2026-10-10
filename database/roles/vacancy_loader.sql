-- Optional role for the vacancy parsers (HH, trudvsem): may write only vacancy tables,
-- reads the enterprise base. NOT applied automatically. Run once as the database owner:
--   psql "<owner dsn>" -v pw="'<strong password>'" -f db/roles/vacancy_loader.sql
-- and give each developer their own login (vacancy_loader_hh / vacancy_loader_trudvsem) if
-- you want separate audit; the grants below are the same.

CREATE ROLE vacancy_loader LOGIN PASSWORD :pw;

GRANT USAGE ON SCHEMA public TO vacancy_loader;

-- enterprise base (GUR) and reference tables: read only
GRANT SELECT ON ALL TABLES IN SCHEMA public TO vacancy_loader;

-- vacancy tables: read and write, no DDL
GRANT INSERT, UPDATE, DELETE ON
    vacancy, vacancy_hh, vacancy_trudvsem, vacancy_discovery, vacancy_snapshot,
    employer_profile, employer_company_match, vacancy_company_match,
    region, classifier_run, vacancy_classification, employer_classification,
    classification_evidence, manual_review, opensanctions_entity, opensanctions_code
TO vacancy_loader;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO vacancy_loader;

-- Load the grant tables from the staging CSVs written by build_grants_staging.py.
--
--   psql -d <db> -v staging_dir=output/grants/staging -f schema/load_grants.sql
--
-- Requires schema/grants_schema.sql to have been run once. Safe to re-run: every NIH_REPORTER
-- grant row and its xref rows are deleted and reloaded in one transaction, so a failed load
-- leaves the previous data in place.

\set ON_ERROR_STOP on
-- \copy doesn't expand psql variables, so move into the staging folder and use relative names.
\cd :staging_dir

BEGIN;

SELECT source_id AS nih_source_id FROM core.source WHERE source_code = 'NIH_REPORTER' \gset

-- ---------------------------------------------------------------- staging
CREATE TEMP TABLE stg_award (
    award_key TEXT PRIMARY KEY, core_project_num TEXT, project_title TEXT, activity_code TEXT,
    admin_ic_code TEXT, admin_ic_name TEXT, org_name TEXT, org_city TEXT, org_state TEXT,
    org_country TEXT, contact_pi_name TEXT, project_start_date DATE, project_end_date DATE,
    relevance_tier TEXT
) ON COMMIT DROP;
CREATE TEMP TABLE stg_funding (
    appl_id BIGINT PRIMARY KEY, award_key TEXT, fiscal_year SMALLINT, project_num TEXT,
    subproject_id INTEGER, project_title TEXT, abstract_text TEXT, award_amount NUMERIC(14, 2),
    award_notice_date DATE, project_detail_url TEXT
) ON COMMIT DROP;
CREATE TEMP TABLE stg_evidence (
    appl_id BIGINT, award_key TEXT, entity_type TEXT, entity_id BIGINT, matched_term TEXT,
    term_type TEXT, match_field TEXT, match_method TEXT, requires_context BOOLEAN
) ON COMMIT DROP;

\copy stg_award    FROM 'stg_grant_award.csv'    WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')
\copy stg_funding  FROM 'stg_grant_funding.csv'  WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')
\copy stg_evidence FROM 'stg_grant_evidence.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')

-- ---------------------------------------------------------------- clear the previous load
DELETE FROM core.xref
WHERE source_id = :nih_source_id AND entity_type IN ('grant_award', 'grant_funding');
DELETE FROM core.grant_award WHERE source_id = :nih_source_id;  -- cascades to the other grant tables

-- ---------------------------------------------------------------- grant_award
-- IDs are drawn from each table's own sequence up front so staging rows can be joined to them.
ALTER TABLE stg_award ADD COLUMN grant_award_id BIGINT;
UPDATE stg_award SET grant_award_id = nextval(pg_get_serial_sequence('core.grant_award', 'grant_award_id'));

INSERT INTO core.grant_award (
    grant_award_id, award_key, core_project_num, project_title, activity_code, admin_ic_code,
    admin_ic_name, org_name, org_city, org_state, org_country, contact_pi_name,
    project_start_date, project_end_date, relevance_tier, source_id
)
SELECT grant_award_id, award_key, core_project_num, project_title, activity_code, admin_ic_code,
       admin_ic_name, org_name, org_city, org_state, org_country, contact_pi_name,
       project_start_date, project_end_date, relevance_tier, :nih_source_id
FROM stg_award;

-- ---------------------------------------------------------------- grant_funding
ALTER TABLE stg_funding ADD COLUMN grant_funding_id BIGINT;
UPDATE stg_funding SET grant_funding_id = nextval(pg_get_serial_sequence('core.grant_funding', 'grant_funding_id'));

INSERT INTO core.grant_funding (
    grant_funding_id, grant_award_id, fiscal_year, project_num, subproject_id, project_title,
    abstract_text, award_amount, award_notice_date, project_detail_url, source_id
)
SELECT f.grant_funding_id, a.grant_award_id, f.fiscal_year, f.project_num, f.subproject_id,
       f.project_title, f.abstract_text, f.award_amount, f.award_notice_date,
       f.project_detail_url, :nih_source_id
FROM stg_funding f
JOIN stg_award a USING (award_key);

-- Totals and fiscal-year range, using the same rule as core.v_grant_award_total.
UPDATE core.grant_award ga
SET total_award_amount = t.total_award_amount,
    total_is_partial   = t.total_is_partial,
    first_fiscal_year  = y.first_fy,
    last_fiscal_year   = y.last_fy
FROM core.v_grant_award_total t,
     (SELECT grant_award_id, min(fiscal_year) AS first_fy, max(fiscal_year) AS last_fy
      FROM core.grant_funding GROUP BY grant_award_id) y
WHERE t.grant_award_id = ga.grant_award_id
  AND y.grant_award_id = ga.grant_award_id
  AND ga.source_id = :nih_source_id;

-- ---------------------------------------------------------------- grant_entity
CREATE TEMP TABLE stg_entity ON COMMIT DROP AS
SELECT DISTINCT award_key, entity_type, entity_id FROM stg_evidence;
ALTER TABLE stg_entity ADD COLUMN grant_entity_id BIGINT;
UPDATE stg_entity SET grant_entity_id = nextval(pg_get_serial_sequence('core.grant_entity', 'grant_entity_id'));

INSERT INTO core.grant_entity (grant_entity_id, grant_award_id, entity_type, entity_id, source_id)
SELECT e.grant_entity_id, a.grant_award_id, e.entity_type, e.entity_id, :nih_source_id
FROM stg_entity e
JOIN stg_award a USING (award_key);

-- ---------------------------------------------------------------- grant_entity_evidence
INSERT INTO core.grant_entity_evidence (
    grant_entity_id, grant_funding_id, matched_term, term_type, match_field, match_method, requires_context
)
SELECT e.grant_entity_id, f.grant_funding_id, s.matched_term, NULLIF(s.term_type, ''),
       s.match_field, s.match_method, s.requires_context
FROM stg_evidence s
JOIN stg_entity e USING (award_key, entity_type, entity_id)
JOIN stg_funding f USING (appl_id)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------- xref
INSERT INTO core.xref (entity_type, entity_id, source_id, xref_type, xref_value)
SELECT 'grant_award', grant_award_id, :nih_source_id, 'nih_core_project_num', core_project_num
FROM stg_award
UNION ALL
SELECT 'grant_funding', grant_funding_id, :nih_source_id, 'nih_appl_id', appl_id::TEXT
FROM stg_funding
UNION ALL
SELECT 'grant_funding', grant_funding_id, :nih_source_id, 'nih_project_num', project_num
FROM stg_funding
WHERE project_num IS NOT NULL;

COMMIT;

-- ---------------------------------------------------------------- report
SELECT 'grant_award' AS table_name, count(*) AS rows FROM core.grant_award WHERE source_id = :nih_source_id
UNION ALL SELECT 'grant_funding', count(*) FROM core.grant_funding WHERE source_id = :nih_source_id
UNION ALL SELECT 'grant_entity', count(*) FROM core.grant_entity WHERE source_id = :nih_source_id
UNION ALL SELECT 'grant_entity_evidence', count(*)
          FROM core.grant_entity_evidence ev JOIN core.grant_entity ge USING (grant_entity_id)
          WHERE ge.source_id = :nih_source_id
UNION ALL SELECT 'xref', count(*) FROM core.xref
          WHERE source_id = :nih_source_id AND entity_type IN ('grant_award', 'grant_funding');

-- Grants extension to the SerenQA centralized DB
-- Source data: NIH RePORTER (output/grants/grants_raw.csv, built by fetch_nih_grants.py)
--
--   grant_funding  --many-to-one-->  grant_award  --one-to-many-->  grant_entity  --one-to-many-->  grant_entity_evidence
--   (one row per RePORTER record)    (one grant)                    (grant <-> KG node)            (why the link exists)
--
-- Dialect: PostgreSQL, in the same `core` schema as core.source, core.xref, core.taxon, etc.
-- Source-specific IDs (appl_id, core_project_num, project_num) go in core.xref, following the
-- convention in "SerenQA Centralized DB relations".

-- ---------------------------------------------------------------------------
-- 1. Source row
-- ---------------------------------------------------------------------------
INSERT INTO core.source (source_code, source_name, source_url, description)
SELECT 'NIH_REPORTER',
       'NIH RePORTER',
       'https://reporter.nih.gov',
       'Federally funded research projects (NIH, CDC, FDA, VA) from the RePORTER v2 API'
WHERE NOT EXISTS (SELECT 1 FROM core.source WHERE source_code = 'NIH_REPORTER');

-- ---------------------------------------------------------------------------
-- 2. grant_award: one row per grant
-- ---------------------------------------------------------------------------
CREATE TABLE core.grant_award (
    grant_award_id      BIGSERIAL PRIMARY KEY,
    -- Grouping key used by the loader. Usually equals core_project_num. RePORTER truncates
    -- contract cores (N01/N44/Y01, e.g. '27220120000') to 11 characters, which merges different
    -- contracts, so for those this is the full contract number plus task order taken from project_num.
    award_key           TEXT NOT NULL UNIQUE,
    core_project_num    TEXT NOT NULL,              -- as RePORTER reports it (also in xref)
    project_title       TEXT,                       -- title from the earliest fiscal year; per-year titles are in grant_funding
    activity_code       TEXT,                       -- R01, U19, N01, ZIA, ...
    admin_ic_code       TEXT,                       -- e.g. 'AI'
    admin_ic_name       TEXT,                       -- e.g. 'NIAID', 'COTPER'; use this to filter out non-research awards
    org_name            TEXT,
    org_city            TEXT,
    org_state           TEXT,
    org_country         TEXT,
    contact_pi_name     TEXT,
    project_start_date  DATE,
    project_end_date    DATE,
    first_fiscal_year   SMALLINT,
    last_fiscal_year    SMALLINT,
    -- Sum of grant_funding.award_amount, computed the same way as v_grant_award_total below:
    -- parent rows only, because a multi-project parent (P01/U19/U54) already includes its
    -- subprojects. Fiscal years that have only subproject rows add those rows instead.
    total_award_amount  NUMERIC(14, 2),
    total_is_partial    BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE if any year had NULL amounts or subprojects only
    relevance_tier      TEXT CHECK (relevance_tier IN ('strong', 'moderate', 'weak')),
                        -- strong: filovirus word in title; moderate: 2+ abstract mentions; weak: 1 mention
    source_id           BIGINT NOT NULL REFERENCES core.source (source_id),
    CHECK (first_fiscal_year IS NULL OR last_fiscal_year IS NULL OR first_fiscal_year <= last_fiscal_year)
);

CREATE INDEX grant_award_core_project_num_idx ON core.grant_award (core_project_num);

-- ---------------------------------------------------------------------------
-- 3. grant_funding: one row per RePORTER record (one fiscal year, parent or subproject)
-- ---------------------------------------------------------------------------
CREATE TABLE core.grant_funding (
    grant_funding_id    BIGSERIAL PRIMARY KEY,
    grant_award_id      BIGINT NOT NULL REFERENCES core.grant_award (grant_award_id) ON DELETE CASCADE,
    fiscal_year         SMALLINT NOT NULL,
    project_num         TEXT,                       -- full yearly number, e.g. 5R01AI123456-03 (also in xref)
    subproject_id       INTEGER,                    -- NULL for the parent / single-project record
    project_title       TEXT,                       -- titles can change between years
    abstract_text       TEXT,
    award_amount        NUMERIC(14, 2),             -- NULL when RePORTER has no amount; never 0 as a stand-in
    award_notice_date   DATE,
    project_detail_url  TEXT,
    source_id           BIGINT NOT NULL REFERENCES core.source (source_id),
    CHECK (award_amount IS NULL OR award_amount >= 0)
);

CREATE INDEX grant_funding_award_year_idx ON core.grant_funding (grant_award_id, fiscal_year);

-- ---------------------------------------------------------------------------
-- 4. grant_entity: one row per (grant, KG node) pair
-- ---------------------------------------------------------------------------
-- entity_id points to a different table depending on entity_type, like core.xref, so the
-- database cannot enforce it as a foreign key. Run the orphan check at the bottom after each load.
CREATE TABLE core.grant_entity (
    grant_entity_id     BIGSERIAL PRIMARY KEY,
    grant_award_id      BIGINT NOT NULL REFERENCES core.grant_award (grant_award_id) ON DELETE CASCADE,
    entity_type         TEXT NOT NULL CHECK (entity_type IN
                            ('taxon', 'gene', 'protein', 'epitope', 'antibody', 'condition')),
    entity_id           BIGINT NOT NULL,            -- taxon -> taxon_id (NCBI taxid); others -> centralized PK
    source_id           BIGINT NOT NULL REFERENCES core.source (source_id),
    UNIQUE (grant_award_id, entity_type, entity_id)
);

CREATE INDEX grant_entity_entity_idx ON core.grant_entity (entity_type, entity_id);

-- ---------------------------------------------------------------------------
-- 5. grant_entity_evidence: every keyword match behind a grant_entity row
-- ---------------------------------------------------------------------------
-- One grant-entity pair is often reached by several terms ('Ebola virus', 'EBOV') and in several
-- years. Each match is a row here, and grant_entity keeps one row per pair.
CREATE TABLE core.grant_entity_evidence (
    grant_entity_evidence_id BIGSERIAL PRIMARY KEY,
    grant_entity_id     BIGINT NOT NULL REFERENCES core.grant_entity (grant_entity_id) ON DELETE CASCADE,
    grant_funding_id    BIGINT NOT NULL REFERENCES core.grant_funding (grant_funding_id) ON DELETE CASCADE,
    matched_term        TEXT NOT NULL,              -- search_term from kg_keywords.csv
    term_type           TEXT,                       -- term_type from kg_keywords.csv (db_name, db_alias, ...)
    match_field         TEXT NOT NULL CHECK (match_field IN ('title', 'abstract')),
    match_method        TEXT NOT NULL CHECK (match_method IN ('keyword', 'synonym', 'gene_label', 'manual')),
                        -- gene_label: protein reached through its associated gene label, not a verified synonym
    requires_context    BOOLEAN NOT NULL DEFAULT FALSE,  -- copied from kg_keywords.csv; these need review
    review_status       TEXT NOT NULL DEFAULT 'unreviewed'
                        CHECK (review_status IN ('unreviewed', 'accepted', 'rejected'))
);

-- 'VP40' and 'vp40' are the same match; dedupe case-insensitively.
CREATE UNIQUE INDEX grant_entity_evidence_uniq
    ON core.grant_entity_evidence (grant_entity_id, grant_funding_id, lower(matched_term), match_field);
CREATE INDEX grant_entity_evidence_funding_idx ON core.grant_entity_evidence (grant_funding_id);

-- ---------------------------------------------------------------------------
-- 6. Total funding rule (the loader fills grant_award.total_award_amount the same way)
-- ---------------------------------------------------------------------------
-- In the current data a parent's award_amount equals the sum of its subprojects
-- (e.g. P01AI120943 FY2016: parent 2,876,287 = subprojects 2,876,287), so summing every row
-- would count that money twice.
CREATE VIEW core.v_grant_award_total AS
WITH per_year AS (
    SELECT grant_award_id,
           fiscal_year,
           bool_or(subproject_id IS NULL)                                        AS has_parent,
           sum(award_amount) FILTER (WHERE subproject_id IS NULL)                AS parent_amount,
           sum(award_amount)                                                     AS all_amount,
           bool_or(award_amount IS NULL AND subproject_id IS NULL)               AS parent_amount_missing
    FROM core.grant_funding
    GROUP BY grant_award_id, fiscal_year
)
SELECT grant_award_id,
       sum(CASE WHEN has_parent THEN parent_amount ELSE all_amount END)          AS total_award_amount,
       bool_or(NOT has_parent OR parent_amount_missing)                          AS total_is_partial
FROM per_year
GROUP BY grant_award_id;

-- ---------------------------------------------------------------------------
-- 7. Source IDs in xref (examples of the rows the loader writes)
-- ---------------------------------------------------------------------------
--   entity_type     entity_id          xref_type              xref_value
--   grant_award     <grant_award_id>   nih_core_project_num   R01AI123456
--   grant_funding   <grant_funding_id> nih_appl_id            10001401
--   grant_funding   <grant_funding_id> nih_project_num        5R01AI123456-03

-- ---------------------------------------------------------------------------
-- 8. Orphan check: grant_entity rows whose entity_id doesn't exist (should return 0 rows)
-- ---------------------------------------------------------------------------
-- Adjust table and PK names to match the live DB (gene/antibody aren't in the relations doc yet).
--
-- SELECT ge.* FROM core.grant_entity ge
-- WHERE (ge.entity_type = 'taxon'     AND NOT EXISTS (SELECT 1 FROM core.taxon     t WHERE t.taxon_id     = ge.entity_id))
--    OR (ge.entity_type = 'protein'   AND NOT EXISTS (SELECT 1 FROM core.protein   p WHERE p.protein_id   = ge.entity_id))
--    OR (ge.entity_type = 'epitope'   AND NOT EXISTS (SELECT 1 FROM core.epitope   e WHERE e.epitope_id   = ge.entity_id))
--    OR (ge.entity_type = 'antibody'  AND NOT EXISTS (SELECT 1 FROM core.antibody  a WHERE a.antibody_id  = ge.entity_id))
--    OR (ge.entity_type = 'gene'      AND NOT EXISTS (SELECT 1 FROM core.gene      g WHERE g.gene_id      = ge.entity_id))
--    OR (ge.entity_type = 'condition' AND NOT EXISTS (SELECT 1 FROM core.condition c WHERE c.condition_id = ge.entity_id));

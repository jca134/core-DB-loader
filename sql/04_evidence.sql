-- Evidence layer: research grants, patents, and papers, plus the one
-- polymorphic table that links any of them to the entities already in core.*.
-- Run after 02_core.sql.
--
--   grant_funding --many-to-one--> grant_award ---\
--   (one RePORTER record)          (one grant)     \
--                                   core.patent ----+--> evidence_link --> any core entity
--                                   core.publication/     (+ evidence_link_support:
--                                                           why the link exists)
--
-- Design note (differs from overview/grants_schema/grants_schema.sql, which
-- proposed grant_entity + grant_entity_evidence): the linking half is
-- generalized across all three evidence types instead of duplicated per
-- source, following the polymorphic pattern of core.xref. Grants, patents and
-- papers all answer the same question -- "what existing KG entity does this
-- external document support, and how do we know?" -- so one link table keeps
-- the edges, and the confidence attached to them, in one place instead of
-- three. core.grant_award /
-- core.grant_funding below are unchanged from that proposal.

-- ===========================================================================
-- 1. Grants (NIH RePORTER)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS core.grant_award (
    grant_award_id      BIGSERIAL PRIMARY KEY,
    -- Grouping key used by the loader. Usually equals core_project_num. RePORTER
    -- truncates contract cores (N01/N44/Y01) to 11 characters, which merges distinct
    -- contracts, so for those this is the full contract number plus task order from
    -- project_num.
    award_key           TEXT NOT NULL UNIQUE,
    core_project_num    TEXT NOT NULL,            -- as RePORTER reports it (also in core.xref)
    project_title       TEXT,                     -- from the earliest fiscal year; per-year titles live in grant_funding
    activity_code       TEXT,                     -- R01, U19, N01, ZIA, ...
    admin_ic_code       TEXT,                     -- e.g. 'AI'
    admin_ic_name       TEXT,                     -- e.g. 'NIAID'
    org_name            TEXT,
    org_city            TEXT,
    org_state           TEXT,
    org_country         TEXT,
    contact_pi_name     TEXT,
    project_start_date  DATE,
    project_end_date    DATE,
    first_fiscal_year   SMALLINT,
    last_fiscal_year    SMALLINT,
    -- Parent rows only -- a multi-project parent (P01/U19/U54) already includes its
    -- subprojects, so summing every row double-counts. See core.v_grant_award_total.
    total_award_amount  NUMERIC(14, 2),
    total_is_partial    BOOLEAN NOT NULL DEFAULT FALSE,
    relevance_tier      TEXT CHECK (relevance_tier IN ('strong', 'moderate', 'weak')),
                        -- strong: filovirus term in title; moderate: 2+ abstract mentions; weak: 1
    source_id           INTEGER NOT NULL REFERENCES core.source(source_id),
    CHECK (first_fiscal_year IS NULL OR last_fiscal_year IS NULL OR first_fiscal_year <= last_fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_grant_award_core_project_num ON core.grant_award(core_project_num);

CREATE TABLE IF NOT EXISTS core.grant_funding (
    grant_funding_id    BIGSERIAL PRIMARY KEY,
    grant_award_id      BIGINT NOT NULL REFERENCES core.grant_award(grant_award_id) ON DELETE CASCADE,
    fiscal_year         SMALLINT NOT NULL,
    project_num         TEXT,                     -- full yearly number, e.g. 5R01AI123456-03 (also in core.xref)
    subproject_id       TEXT,                     -- NULL for the parent / single-project record
    project_title       TEXT,                     -- titles can change between years
    abstract_text       TEXT,
    award_amount        NUMERIC(14, 2),           -- NULL when RePORTER has no amount; never 0 as a stand-in
    award_notice_date   DATE,
    project_detail_url  TEXT,
    source_id           INTEGER NOT NULL REFERENCES core.source(source_id),
    CHECK (award_amount IS NULL OR award_amount >= 0)
);

CREATE INDEX IF NOT EXISTS idx_grant_funding_award_year ON core.grant_funding(grant_award_id, fiscal_year);

-- The loader fills grant_award.total_award_amount the same way this view computes it.
CREATE OR REPLACE VIEW core.v_grant_award_total AS
WITH per_year AS (
    SELECT grant_award_id,
           fiscal_year,
           bool_or(subproject_id IS NULL)                            AS has_parent,
           sum(award_amount) FILTER (WHERE subproject_id IS NULL)    AS parent_amount,
           sum(award_amount)                                         AS all_amount,
           bool_or(award_amount IS NULL AND subproject_id IS NULL)   AS parent_amount_missing
    FROM core.grant_funding
    GROUP BY grant_award_id, fiscal_year
)
SELECT grant_award_id,
       sum(CASE WHEN has_parent THEN parent_amount ELSE all_amount END) AS total_award_amount,
       bool_or(NOT has_parent OR parent_amount_missing)                 AS total_is_partial
FROM per_year
GROUP BY grant_award_id;

-- ===========================================================================
-- 2. Patents (USPTO search exports)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS core.patent (
    patent_id        BIGSERIAL PRIMARY KEY,
    document_id      TEXT NOT NULL UNIQUE,   -- as published, e.g. 'US 20250281602 A1'
    kind_code        TEXT,                   -- trailing kind, e.g. 'A1' (application) / 'B2' (granted)
    title            TEXT,
    family_id        TEXT,
    application_num  TEXT,
    date_published   DATE,
    filing_date      DATE,
    inventor         TEXT,
    assignee         TEXT,
    applicant_name   TEXT,
    -- CPC: inventive codes (CPCI) carry the subject matter; additional codes
    -- (CPCA) are indexing. Kept verbatim as the source's ';'-joined strings,
    -- and split into core.patent_cpc for querying.
    cpc_inventive    TEXT,
    cpc_additional   TEXT,
    page_count       INTEGER,
    source_id        INTEGER NOT NULL REFERENCES core.source(source_id)
);

-- Filled from each patent's Google Patents page (`filovirus fetch
-- patent-details`): the USPTO exports carry no abstract, and the expanded
-- search's patents had no dates. Added after the table, so existing
-- databases pick them up from a plain rerun of this file.
ALTER TABLE core.patent ADD COLUMN IF NOT EXISTS priority_date DATE;
ALTER TABLE core.patent ADD COLUMN IF NOT EXISTS abstract TEXT;

CREATE TABLE IF NOT EXISTS core.patent_cpc (
    patent_cpc_id BIGSERIAL PRIMARY KEY,
    patent_id     BIGINT NOT NULL REFERENCES core.patent(patent_id) ON DELETE CASCADE,
    cpc_code      TEXT NOT NULL,          -- e.g. 'C07K16/10'
    cpc_kind      TEXT NOT NULL CHECK (cpc_kind IN ('inventive', 'additional')),
    UNIQUE (patent_id, cpc_code, cpc_kind)
);

CREATE INDEX IF NOT EXISTS idx_patent_cpc_code ON core.patent_cpc(cpc_code);

-- ===========================================================================
-- 3. Papers: core.publication already exists (02_core.sql, ImmPort-populated).
--    Widened here rather than replaced, so the ImmPort study_publication links
--    keep working and a paper found by three routes stays one row.
-- ===========================================================================

ALTER TABLE core.publication ADD COLUMN IF NOT EXISTS url       TEXT;
ALTER TABLE core.publication ADD COLUMN IF NOT EXISTS source_id INTEGER REFERENCES core.source(source_id);
-- Filled for papers reached through a grant's citation list
-- (fetchers/cited_publications.py): the relevance filter reads them, and they are
-- the text any document-similarity comparison needs. NULL for the papers
-- workstream's own 300 until their abstracts are fetched too.
ALTER TABLE core.publication ADD COLUMN IF NOT EXISTS abstract   TEXT;
ALTER TABLE core.publication ADD COLUMN IF NOT EXISTS mesh_terms TEXT;   -- '; '-separated MeSH descriptor names

-- ===========================================================================
-- 4. evidence_link: one row per (evidence document, core entity) pair
-- ===========================================================================
-- Polymorphic on both sides, like core.xref: evidence_id points into
-- grant_award / patent / publication depending on evidence_type, and entity_id
-- into one of the core entity tables depending on entity_type. Neither side can
-- be a real FK, so run the orphan check at the bottom after each load.
CREATE TABLE IF NOT EXISTS core.evidence_link (
    evidence_link_id BIGSERIAL PRIMARY KEY,
    -- Names the table evidence_id points into, exactly as entity_type names the
    -- table entity_id points into. One rule for both discriminators.
    --
    -- These used to read 'grant' and 'paper'. That made the same thing two
    -- things: a paper was evidence_type 'paper' as a link's source but
    -- entity_type 'publication' as a grant's citation target, so the obvious
    -- query -- evidence_type = 'publication' -- returned nothing while 1,905
    -- paper-to-epitope links sat in the table.
    evidence_type    TEXT NOT NULL CHECK (evidence_type IN ('grant_award', 'patent', 'publication')),
    evidence_id      BIGINT NOT NULL,
    -- All three document types are valid targets too, so documents can link
    -- to each other directly: grant -> paper (cites) today, and grant <-> patent
    -- or paper <-> patent wherever a source or a similarity measure supplies
    -- one. Without this the only path between two documents was two hops
    -- through a shared entity -- for grant/patent pairs, almost always one of a
    -- handful of hub taxa, which connects 87% of all pairs and means nothing.
    -- Kept in step with the ALTER below, which applies it to existing tables.
    entity_type      TEXT NOT NULL CHECK (entity_type IN (
        'taxon', 'gene', 'protein', 'epitope', 'antibody', 'condition',
        'structure', 'isolate', 'sequence', 'feature', 'study',
        'publication', 'grant_award', 'patent',
        'symptom', 'assay_type'          -- sql/05_clinical_vocab.sql
    )),
    entity_id        TEXT NOT NULL,    -- TEXT because core PKs are a mix of BIGINT and accession strings (core.study)
    -- Vocabulary from the team's shared evidence format (Work.pdf B):
    --   funds     grant_award -> study
    --   claims    patent      -> antibody / protein / gene
    --   describes publication -> structure / epitope / antibody (exact-ID links)
    --   cites     grant_award -> publication  (RePORTER's own publication list)
    --             (also the relation for any future document -> document
    --              citation: patent -> paper, paper -> paper)
    --   related   anything reached by keyword rather than an exact identifier
    relation         TEXT NOT NULL CHECK (relation IN ('funds', 'claims', 'describes', 'cites', 'related')),
    -- How certain the link is: 1.0 for an exact identifier match, lower for
    -- keyword-derived ones.
    confidence       NUMERIC(3, 2) NOT NULL DEFAULT 1.00 CHECK (confidence > 0 AND confidence <= 1),
    source_id        INTEGER NOT NULL REFERENCES core.source(source_id),
    UNIQUE (evidence_type, evidence_id, entity_type, entity_id, relation)
);

-- CREATE TABLE IF NOT EXISTS leaves an existing table's CHECKs alone, so the
-- widened entity_type list is applied explicitly, as for core.xref below.
ALTER TABLE core.evidence_link DROP CONSTRAINT IF EXISTS evidence_link_entity_type_check;
ALTER TABLE core.evidence_link ADD CONSTRAINT evidence_link_entity_type_check CHECK (entity_type IN (
    'taxon', 'gene', 'protein', 'epitope', 'antibody', 'condition',
    'structure', 'isolate', 'sequence', 'feature', 'study',
    'publication', 'grant_award', 'patent',
    'symptom', 'assay_type'
));

-- Now that a document can be a target, it could be its own target. A paper
-- that "cites" itself is a loop: noise to any walk over these edges, and in a
-- row-normalised transition matrix it inflates the paper's stay probability.
ALTER TABLE core.evidence_link DROP CONSTRAINT IF EXISTS evidence_link_no_self_link;
ALTER TABLE core.evidence_link ADD CONSTRAINT evidence_link_no_self_link
    CHECK (NOT (evidence_type = entity_type AND evidence_id::text = entity_id));

CREATE INDEX IF NOT EXISTS idx_evidence_link_entity ON core.evidence_link(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_evidence_link_evidence ON core.evidence_link(evidence_type, evidence_id);

-- Every match behind an evidence_link row. One link is often reached by several
-- terms ('Ebola virus', 'EBOV') and, for grants, in several fiscal years; each
-- match is a row here while evidence_link keeps one row per pair.
CREATE TABLE IF NOT EXISTS core.evidence_link_support (
    evidence_link_support_id BIGSERIAL PRIMARY KEY,
    evidence_link_id  BIGINT NOT NULL REFERENCES core.evidence_link(evidence_link_id) ON DELETE CASCADE,
    -- Which specific record carried the match: a grant_funding row (one fiscal
    -- year's title/abstract), or NULL when the match is against the evidence
    -- document as a whole (a patent title, a paper's exact-ID link).
    grant_funding_id  BIGINT REFERENCES core.grant_funding(grant_funding_id) ON DELETE CASCADE,
    matched_term      TEXT,             -- search_term from kg_keywords.csv; NULL for exact-ID links
    term_type         TEXT,             -- term_type from kg_keywords.csv (db_name, db_alias, ...)
    match_field       TEXT NOT NULL CHECK (match_field IN ('title', 'abstract', 'identifier', 'query_scope')),
                      -- query_scope: the ebola/marburg search the patent file came from, not text in the row
    match_method      TEXT NOT NULL CHECK (match_method IN ('exact_id', 'keyword', 'synonym', 'gene_label', 'citation', 'manual')),
    requires_context  BOOLEAN NOT NULL DEFAULT FALSE,   -- copied from kg_keywords.csv; these need review
    review_status     TEXT NOT NULL DEFAULT 'unreviewed'
                      CHECK (review_status IN ('unreviewed', 'accepted', 'rejected'))
);

-- 'VP40' and 'vp40' are the same match; dedupe case-insensitively. COALESCE because
-- grant_funding_id and matched_term are NULL on exact-ID links, and NULL never
-- equals NULL in a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS evidence_link_support_uniq
    ON core.evidence_link_support (
        evidence_link_id, COALESCE(grant_funding_id, -1), lower(COALESCE(matched_term, '')), match_field
    );
CREATE INDEX IF NOT EXISTS idx_evidence_link_support_link ON core.evidence_link_support(evidence_link_id);
-- The other side of the same table: which support rows a given fiscal year's
-- grant record produced. Was a seq scan over ~20k rows.
CREATE INDEX IF NOT EXISTS idx_evidence_link_support_funding ON core.evidence_link_support(grant_funding_id);

-- ===========================================================================
-- 5. core.xref: widened for the new entity types' external identifiers
-- ===========================================================================
--   entity_type   xref_type              example xref_value
--   grant_award   nih_core_project_num   R01AI123456
--   grant_funding nih_appl_id            10001401
--   grant_funding nih_project_num        5R01AI123456-03
--   patent        uspto_document_id      US 20250281602 A1
--   patent        uspto_application_num  18/862481
--   publication   pubmed_id              28854372
ALTER TABLE core.xref DROP CONSTRAINT IF EXISTS xref_entity_type_check;
ALTER TABLE core.xref ADD CONSTRAINT xref_entity_type_check CHECK (entity_type IN (
    'isolate', 'sequence', 'feature', 'protein', 'structure',
    'epitope', 'antibody', 'alignment',
    'grant_award', 'grant_funding', 'patent', 'publication'
));

-- ===========================================================================
-- 6. Removed views
-- ===========================================================================
-- core.v_evidence_edge (a flat source/relation/target/weight list for a graph
-- consumer) is out of scope: this repo builds the database only, and reshaping
-- it for a consumer was removed with the rest of the graph projection.
-- Dropped here so existing databases lose it too.
DROP VIEW IF EXISTS core.v_evidence_edge;

-- core.v_evidence_cooccurrence (documents paired through a shared entity) was
-- removed 2026-11-03. Measured on the real data it connected 87% of all
-- grant/patent pairs, nearly all through a handful of hub taxa, and could never
-- connect a paper to a patent at all (they link to disjoint entity types).
-- Document-to-document edges now come from citations and a text-proximity
-- graph, stored directly in core.evidence_link. Dropped here so existing
-- databases lose it too.
DROP VIEW IF EXISTS core.v_evidence_cooccurrence;

-- ===========================================================================
-- 7. Orphan check: evidence_link rows whose entity_id doesn't exist.
--    Should return 0 rows; etl/evidence.py runs this and prints the count.
-- ===========================================================================
-- SELECT el.* FROM core.evidence_link el
-- WHERE (el.entity_type = 'taxon'     AND NOT EXISTS (SELECT 1 FROM core.taxon     t WHERE t.taxon_id::text     = el.entity_id))
--    OR (el.entity_type = 'gene'      AND NOT EXISTS (SELECT 1 FROM core.gene      g WHERE g.gene_id::text      = el.entity_id))
--    OR (el.entity_type = 'protein'   AND NOT EXISTS (SELECT 1 FROM core.protein   p WHERE p.protein_id::text   = el.entity_id))
--    OR (el.entity_type = 'epitope'   AND NOT EXISTS (SELECT 1 FROM core.epitope   e WHERE e.epitope_id::text   = el.entity_id))
--    OR (el.entity_type = 'antibody'  AND NOT EXISTS (SELECT 1 FROM core.antibody  a WHERE a.antibody_id::text  = el.entity_id))
--    OR (el.entity_type = 'condition' AND NOT EXISTS (SELECT 1 FROM core.condition c WHERE c.condition_id::text = el.entity_id))
--    OR (el.entity_type = 'structure' AND NOT EXISTS (SELECT 1 FROM core.structure s WHERE s.structure_id::text = el.entity_id))
--    OR (el.entity_type = 'isolate'   AND NOT EXISTS (SELECT 1 FROM core.isolate   i WHERE i.isolate_id::text   = el.entity_id))
--    OR (el.entity_type = 'sequence'  AND NOT EXISTS (SELECT 1 FROM core.sequence  q WHERE q.sequence_id::text  = el.entity_id))
--    OR (el.entity_type = 'feature'   AND NOT EXISTS (SELECT 1 FROM core.feature   f WHERE f.feature_id::text   = el.entity_id))
--    OR (el.entity_type = 'study'     AND NOT EXISTS (SELECT 1 FROM core.study     y WHERE y.study_accession    = el.entity_id))
--    OR (el.entity_type = 'publication' AND NOT EXISTS (SELECT 1 FROM core.publication u WHERE u.publication_id::text = el.entity_id))
--    OR (el.entity_type = 'grant_award' AND NOT EXISTS (SELECT 1 FROM core.grant_award g WHERE g.grant_award_id::text = el.entity_id))
--    OR (el.entity_type = 'patent'      AND NOT EXISTS (SELECT 1 FROM core.patent      p WHERE p.patent_id::text      = el.entity_id));

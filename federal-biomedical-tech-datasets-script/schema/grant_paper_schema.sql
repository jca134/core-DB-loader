-- Grant -> paper links: papers a grant's PI published after the award started
-- Source data: output/links/*.csv, built by link_grants_to_papers.py
--
--   grant_award  --one-to-many-->  grant_paper_link  <--many-to-one--  paper (pmid)
--
-- Dialect: PostgreSQL, same `core` schema as schema/grants_schema.sql (run that first).
-- Papers are keyed by PMID only: there is no core paper table yet, so pmid is not a foreign key.

-- ---------------------------------------------------------------------------
-- 1. grant_paper_link: one row per (grant, PI, paper) published after the award start
-- ---------------------------------------------------------------------------
-- Grant and paper attributes are copied in so the table can be used on its own (e.g. exported
-- for the paper-level KG); award_key joins back to core.grant_award for everything else.
CREATE TABLE core.grant_paper_link (
    grant_paper_link_id     BIGSERIAL PRIMARY KEY,
    award_key               TEXT NOT NULL REFERENCES core.grant_award (award_key) ON DELETE CASCADE,
    core_project_num        TEXT NOT NULL,
    grant_title             TEXT,
    activity_code           TEXT,
    admin_ic_name           TEXT,
    grant_org_name          TEXT,
    grant_relevance_tier    TEXT CHECK (grant_relevance_tier IN ('strong', 'moderate', 'weak')),
    award_start_date        DATE NOT NULL,
    award_end_date          DATE,
    award_date_source       TEXT NOT NULL CHECK (award_date_source IN
                                ('project_start_date', 'award_notice_date', 'fiscal_year_start')),
    pi_profile_id           BIGINT,                     -- RePORTER PI profile ID
    pi_name                 TEXT NOT NULL,
    pi_is_contact           BOOLEAN NOT NULL,
    pmid                    BIGINT NOT NULL,
    doi                     TEXT,
    pmcid                   TEXT,
    paper_title             TEXT,
    journal                 TEXT,
    pub_date                DATE NOT NULL,              -- partial dates padded to the first day; see precision
    pub_date_precision      TEXT NOT NULL CHECK (pub_date_precision IN ('day', 'month', 'year')),
    paper_year              SMALLINT,
    n_authors               INTEGER,
    cited_by_count          INTEGER,
    pubmed_url              TEXT,
    author_name             TEXT NOT NULL,              -- the PI as written on the paper
    author_position         INTEGER,
    is_last_author          BOOLEAN,                    -- last author is usually the lab head
    author_affiliation      TEXT,
    name_match              TEXT NOT NULL CHECK (name_match IN ('forename', 'initials')),
    affiliation_match       BOOLEAN,                    -- NULL when PubMed has no affiliation
    match_confidence        TEXT NOT NULL CHECK (match_confidence IN ('high', 'medium', 'low')),
                            -- high: paper cites the grant or affiliation = grantee org
                            -- medium: full first name only; low: initials only
    days_after_award_start  INTEGER NOT NULL CHECK (days_after_award_start > 0),
    published_during_award  BOOLEAN NOT NULL,
    grant_acknowledged      BOOLEAN NOT NULL,           -- paper lists this grant number in PubMed
    has_paper_abstract      BOOLEAN NOT NULL,
    similarity              NUMERIC(6, 4),              -- cosine of grant vs. paper text embeddings
    similarity_percentile   NUMERIC(5, 4),              -- share of all papers less similar to this grant
    is_best_grant_match     BOOLEAN NOT NULL,           -- this PI's grant closest to the paper (one per PI + paper)
    is_relevant             BOOLEAN NOT NULL,           -- acknowledged OR percentile >= the run threshold
    link_basis              TEXT,                       -- 'acknowledged', 'similarity', both, or ''
    UNIQUE (award_key, pi_name, pmid)
);

CREATE INDEX grant_paper_link_pmid_idx ON core.grant_paper_link (pmid);
CREATE INDEX grant_paper_link_pi_idx ON core.grant_paper_link (pi_profile_id);

-- ---------------------------------------------------------------------------
-- 2. grant_pi_paper_summary: one row per (grant, PI), papers before vs. after the award
-- ---------------------------------------------------------------------------
-- Counts cover only the papers in the input set (filovirus papers), not each PI's full record.
CREATE TABLE core.grant_pi_paper_summary (
    award_key                   TEXT NOT NULL REFERENCES core.grant_award (award_key) ON DELETE CASCADE,
    core_project_num            TEXT NOT NULL,
    pi_profile_id               BIGINT,
    pi_name                     TEXT NOT NULL,
    award_start_date            DATE,
    award_end_date              DATE,
    papers_before_award         INTEGER NOT NULL,
    relevant_papers_before_award INTEGER NOT NULL,      -- on-topic baseline (similarity only)
    papers_after_award          INTEGER NOT NULL,
    relevant_papers_after_award INTEGER NOT NULL,
    acknowledged_papers         INTEGER NOT NULL,
    first_relevant_year         SMALLINT,
    last_relevant_year          SMALLINT,
    PRIMARY KEY (award_key, pi_name)
);

-- ---------------------------------------------------------------------------
-- 3. Example: relevant, confidently attributed papers per grant
-- ---------------------------------------------------------------------------
-- SELECT award_key, pi_name, pmid, paper_title, pub_date, link_basis
-- FROM core.grant_paper_link
-- WHERE is_relevant AND is_best_grant_match AND match_confidence IN ('high', 'medium')
-- ORDER BY award_key, pub_date;

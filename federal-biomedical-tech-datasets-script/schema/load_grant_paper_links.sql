-- Load the grant -> paper link tables from the CSVs written by link_grants_to_papers.py.
--
--   psql -d <db> -v links_dir=output/links -f schema/load_grant_paper_links.sql
--
-- Requires schema/grants_schema.sql and schema/grant_paper_schema.sql to have been run, and the
-- grants loaded (schema/load_grants.sql), since award_key references core.grant_award.
-- Safe to re-run: both tables are emptied and reloaded in one transaction.

\set ON_ERROR_STOP on
-- \copy doesn't expand psql variables, so move into the links folder and use relative names.
\cd :links_dir

BEGIN;

TRUNCATE core.grant_paper_link, core.grant_pi_paper_summary RESTART IDENTITY;

\copy core.grant_paper_link (award_key, core_project_num, grant_title, activity_code, admin_ic_name, grant_org_name, grant_relevance_tier, award_start_date, award_end_date, award_date_source, pi_profile_id, pi_name, pi_is_contact, pmid, doi, pmcid, paper_title, journal, pub_date, pub_date_precision, paper_year, n_authors, cited_by_count, pubmed_url, author_name, author_position, is_last_author, author_affiliation, name_match, affiliation_match, match_confidence, days_after_award_start, published_during_award, grant_acknowledged, has_paper_abstract, similarity, similarity_percentile, is_best_grant_match, is_relevant, link_basis) FROM 'grant_paper_link.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')
\copy core.grant_pi_paper_summary (award_key, core_project_num, pi_profile_id, pi_name, award_start_date, award_end_date, papers_before_award, relevant_papers_before_award, papers_after_award, relevant_papers_after_award, acknowledged_papers, first_relevant_year, last_relevant_year) FROM 'grant_pi_paper_summary.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')

COMMIT;

-- ---------------------------------------------------------------- report
SELECT 'grant_paper_link' AS table_name, count(*) AS rows FROM core.grant_paper_link
UNION ALL SELECT 'grant_paper_link (relevant)', count(*) FROM core.grant_paper_link WHERE is_relevant
UNION ALL SELECT 'grant_pi_paper_summary', count(*) FROM core.grant_pi_paper_summary;

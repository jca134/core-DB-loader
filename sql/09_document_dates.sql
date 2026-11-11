-- One comparable date per document: when it became public. SerenQA's novelty
-- scoring and any "known before X" split need a single date per node, and the
-- sources each carry different ones. Idempotent.
--
--   grant        project start date; else the start of its first fiscal year
--                (NIH fiscal year N starts on 1 October of N-1)
--   patent       publication date; else filing date
--   publication  the day it went online (epub_date), if that is earlier than its
--                issue year; else 1 January of its issue year, with
--                date_precision 'year' -- no day is implied

-- When the paper went online, from PubMed's electronic ArticleDate (`filovirus
-- fetch paper-details`). Journals often publish online before the print issue
-- that `year` records -- sometimes years before.
ALTER TABLE core.publication ADD COLUMN IF NOT EXISTS epub_date DATE;

DROP VIEW IF EXISTS core.v_document_date;
CREATE VIEW core.v_document_date AS
SELECT 'grant_award'::text AS document_type, grant_award_id AS document_id,
       coalesce(project_start_date, make_date(first_fiscal_year - 1, 10, 1)) AS date,
       CASE WHEN project_start_date IS NOT NULL THEN 'day' ELSE 'fiscal_year' END AS date_precision,
       CASE WHEN project_start_date IS NOT NULL THEN 'project_start' ELSE 'first_fiscal_year' END AS date_basis
FROM core.grant_award
WHERE project_start_date IS NOT NULL OR first_fiscal_year IS NOT NULL
UNION ALL
SELECT 'patent', patent_id, coalesce(date_published, filing_date), 'day',
       CASE WHEN date_published IS NOT NULL THEN 'publication' ELSE 'filing' END
FROM core.patent
WHERE date_published IS NOT NULL OR filing_date IS NOT NULL
UNION ALL
SELECT 'publication', publication_id,
       -- CASE tests in order, so year is cast only once it is known to be a year.
       CASE WHEN year IS NULL OR year !~ '^\d{4}$' THEN epub_date
            WHEN epub_date < make_date(year::int, 1, 1) THEN epub_date
            ELSE make_date(year::int, 1, 1) END,
       CASE WHEN year IS NULL OR year !~ '^\d{4}$' THEN 'day'
            WHEN epub_date < make_date(year::int, 1, 1) THEN 'day'
            ELSE 'year' END,
       CASE WHEN year IS NULL OR year !~ '^\d{4}$' THEN 'online'
            WHEN epub_date < make_date(year::int, 1, 1) THEN 'online'
            ELSE 'issue_year' END
FROM core.publication
WHERE year ~ '^\d{4}$' OR epub_date IS NOT NULL;

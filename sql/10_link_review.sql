-- Human review of evidence links (accept / reject), from the query page's
-- review tab. Lives in ops.*, not core.*, because core.evidence_link is
-- rebuilt -- with new ids -- on every build. A review is keyed by what the link
-- connects, in natural ids that survive rebuilds:
--   'grant_award:R01AI123456|gene:GP|related'
-- and etl/evidence.py copies each decision back onto the rebuilt link's
-- core.evidence_link_support.review_status. Idempotent.

CREATE TABLE IF NOT EXISTS ops.link_review (
    link_key             TEXT PRIMARY KEY,
    decision             TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    confidence_at_review NUMERIC(3, 2) NOT NULL,
    note                 TEXT,
    reviewed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

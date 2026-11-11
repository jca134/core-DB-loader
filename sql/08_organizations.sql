-- Organizations: grant recipients and patent assignees as one node each,
-- however the sources spell them. Names are normalized and merged by
-- core_data_loader/curated/organizations.py; built by etl/evidence.py,
-- which owns grants and patents. Run after 04_evidence.sql. Idempotent.

CREATE TABLE IF NOT EXISTS core.organization (
    organization_id BIGSERIAL PRIMARY KEY,
    org_key         TEXT NOT NULL UNIQUE,     -- the normalized name: the identity
    name            TEXT NOT NULL,            -- display name
    kind            TEXT NOT NULL CHECK (kind IN ('university', 'company', 'government', 'hospital', 'nonprofit', 'other')),
    spellings       TEXT[] NOT NULL           -- every source spelling merged into it
);

CREATE TABLE IF NOT EXISTS core.grant_award_organization (
    grant_award_id  BIGINT NOT NULL REFERENCES core.grant_award(grant_award_id) ON DELETE CASCADE,
    organization_id BIGINT NOT NULL REFERENCES core.organization(organization_id) ON DELETE CASCADE,
    PRIMARY KEY (grant_award_id, organization_id)
);

CREATE TABLE IF NOT EXISTS core.patent_organization (
    patent_id       BIGINT NOT NULL REFERENCES core.patent(patent_id) ON DELETE CASCADE,
    organization_id BIGINT NOT NULL REFERENCES core.organization(organization_id) ON DELETE CASCADE,
    role            TEXT   NOT NULL CHECK (role IN ('assignee', 'applicant')),
    PRIMARY KEY (patent_id, organization_id, role)
);
CREATE INDEX IF NOT EXISTS idx_grant_award_organization_org ON core.grant_award_organization (organization_id);
CREATE INDEX IF NOT EXISTS idx_patent_organization_org ON core.patent_organization (organization_id);

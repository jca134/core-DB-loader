-- Ingestion tracking / provenance layer.
-- Run after 01_schemas.sql (independent of core.sql, but entity_provenance
-- references core.source).

CREATE TABLE IF NOT EXISTS ops.ingest_batch (
    batch_id     BIGSERIAL PRIMARY KEY,
    source_id    INTEGER REFERENCES core.source(source_id),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running',  -- 'running','succeeded','failed'
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS ops.ingest_file (
    file_id    BIGSERIAL PRIMARY KEY,
    batch_id   BIGINT NOT NULL REFERENCES ops.ingest_batch(batch_id),
    source_id  INTEGER REFERENCES core.source(source_id),
    file_path  TEXT NOT NULL,
    file_hash  TEXT,
    row_count  INTEGER,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row-level lineage: which raw row(s) a given core row was derived from.
-- Kept schema-agnostic (text columns) since a core entity is often
-- assembled from more than one raw table/source.
CREATE TABLE IF NOT EXISTS ops.entity_provenance (
    provenance_id BIGSERIAL PRIMARY KEY,
    core_table    TEXT NOT NULL,     -- e.g. 'isolate', 'epitope'
    core_pk       TEXT NOT NULL,
    source_id     INTEGER REFERENCES core.source(source_id),
    raw_table     TEXT NOT NULL,     -- e.g. 'bvbrc_genome'
    raw_pk        TEXT,
    ingest_file_id BIGINT REFERENCES ops.ingest_file(file_id),
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entity_provenance_core ON ops.entity_provenance(core_table, core_pk);
CREATE INDEX IF NOT EXISTS idx_ingest_file_batch ON ops.ingest_file(batch_id);

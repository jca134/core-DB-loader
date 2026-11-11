-- Filovirus medical countermeasures: vaccines, antibody products, antivirals.
-- Hand-curated in core_data_loader/curated/countermeasures.py with fixed ids;
-- seeded and linked by etl/countermeasure.py. Run after 06_gene_family.sql.
-- Idempotent.

CREATE TABLE IF NOT EXISTS core.countermeasure (
    countermeasure_id SMALLINT PRIMARY KEY,          -- fixed in the curated list
    name              TEXT NOT NULL UNIQUE,
    kind              TEXT NOT NULL CHECK (kind IN ('vaccine', 'antibody', 'small_molecule', 'sirna')),
    status            TEXT NOT NULL CHECK (status IN ('licensed', 'clinical', 'preclinical')),
    developer         TEXT,
    regulatory        TEXT,                          -- approvals, licensed products only
    names             TEXT[] NOT NULL                -- what grants/patents/papers call it
);

-- What it acts against. Taxa are species/genus roots; strains hang below them.
CREATE TABLE IF NOT EXISTS core.countermeasure_taxon (
    countermeasure_id SMALLINT NOT NULL REFERENCES core.countermeasure(countermeasure_id) ON DELETE CASCADE,
    taxon_id          INTEGER  NOT NULL REFERENCES core.taxon(taxon_id) ON DELETE CASCADE,
    PRIMARY KEY (countermeasure_id, taxon_id)
);
CREATE TABLE IF NOT EXISTS core.countermeasure_gene_family (
    countermeasure_id SMALLINT NOT NULL REFERENCES core.countermeasure(countermeasure_id) ON DELETE CASCADE,
    gene_family_id    SMALLINT NOT NULL REFERENCES core.gene_family(gene_family_id) ON DELETE CASCADE,
    PRIMARY KEY (countermeasure_id, gene_family_id)
);
-- Component antibodies (ZMapp = c13C6 + c2G4 + c4G7), resolved by name each build:
-- etl/bvbrc_hfv.py rebuilds core.antibody, which cascades these rows away.
CREATE TABLE IF NOT EXISTS core.countermeasure_antibody (
    countermeasure_id SMALLINT NOT NULL REFERENCES core.countermeasure(countermeasure_id) ON DELETE CASCADE,
    antibody_id       BIGINT   NOT NULL REFERENCES core.antibody(antibody_id) ON DELETE CASCADE,
    PRIMARY KEY (countermeasure_id, antibody_id)
);
-- Clinical studies that evaluated it (ImmPort SDY1373 = the rVSV-ZEBOV trial).
CREATE TABLE IF NOT EXISTS core.countermeasure_study (
    countermeasure_id SMALLINT NOT NULL REFERENCES core.countermeasure(countermeasure_id) ON DELETE CASCADE,
    study_accession   TEXT     NOT NULL REFERENCES core.study(study_accession) ON DELETE CASCADE,
    PRIMARY KEY (countermeasure_id, study_accession)
);
CREATE INDEX IF NOT EXISTS idx_countermeasure_antibody_ab ON core.countermeasure_antibody (antibody_id);
CREATE INDEX IF NOT EXISTS idx_countermeasure_taxon_taxon ON core.countermeasure_taxon (taxon_id);

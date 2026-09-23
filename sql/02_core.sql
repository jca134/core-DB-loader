-- Core (normalized, cross-source) layer for the filovirus integration database.
-- Populated by ETL out of `raw.*` — nothing in here is loaded directly from source files.
-- Run after 01_schemas.sql.

-- ===========================================================================
-- Provenance root: every cross-source entity carries a source_id.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS core.source (
    source_id       SERIAL PRIMARY KEY,
    source_code     TEXT NOT NULL UNIQUE,   -- e.g. 'bvbrc', 'ncbi', 'hfv_lanl', 'ucsc', 'immport_sdy1373'
    source_name     TEXT NOT NULL,
    source_url      TEXT,
    description     TEXT
);

-- One polymorphic table for every entity's external ids instead of a
-- per-entity accession column (genbank_accession, uniprotkb_accession, ...):
-- an entity can carry ids from more than one source, and a new source
-- shouldn't need a schema change to add its identifiers.
CREATE TABLE IF NOT EXISTS core.xref (
    xref_id     BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'isolate', 'sequence', 'feature', 'protein', 'structure',
        'epitope', 'antibody', 'alignment'
    )),
    entity_id   BIGINT NOT NULL,
    source_id   INTEGER NOT NULL REFERENCES core.source(source_id),
    xref_type   TEXT NOT NULL,   -- 'genbank_accession','bvbrc_genome_id','uniprotkb_accession','iedb_id', ...
    xref_value  TEXT NOT NULL,
    -- Only rejects re-recording the exact same fact twice for the same
    -- entity; two different entities are still free to share a value.
    UNIQUE (entity_type, entity_id, source_id, xref_type, xref_value)
);

CREATE INDEX IF NOT EXISTS idx_xref_entity ON core.xref(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS core.taxon (
    taxon_id        INTEGER PRIMARY KEY,    -- real NCBI taxon id, not a surrogate
    taxon_name      TEXT NOT NULL,
    taxon_rank      TEXT,
    genetic_code    INTEGER,
    lineage         TEXT,
    parent_id       INTEGER REFERENCES core.taxon(taxon_id),  -- matches bvbrc_taxonomy.parent_id
    source_id       INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.isolate (
    isolate_id          BIGSERIAL PRIMARY KEY,
    taxon_id            INTEGER REFERENCES core.taxon(taxon_id),
    primary_strain_name TEXT,
    lanl_strain_name    TEXT,               -- LANL/HFV nomenclature, e.g. EBOV/H.sap-tc/COD/76/Yambuku-Mayinga
    species             TEXT,
    country             TEXT,
    geo_location        TEXT,               -- finer-grained than country, e.g. NCBI's "USA: California"
    host                TEXT,
    tissue_specimen_source TEXT,
    collection_date     DATE,               -- only when the source gives a full date
    collection_year     INTEGER,            -- also set for year-only/year-month dates ("2014", "2014-08")
    genome_status       TEXT,               -- e.g. Complete / Partial
    source_id           INTEGER REFERENCES core.source(source_id),
    notes               TEXT,
    -- Patient-level facts for isolates tied to a documented human case (currently
    -- from LANL's hfv_ebola_annotation_web). One isolate = one case in that data,
    -- so these live here rather than in a separate table (see hfv_ebola_annotation_web).
    patient_outcome     TEXT,               -- LANL's single-letter code, e.g. 'd' (died), 's' (survived)
    patient_age         TEXT,               -- free text in source, e.g. '28 yr', '8 mo'
    patient_sex         TEXT,
    symptom_onset_date  DATE,
    death_date          DATE
);

-- Cell-level provenance for a merged BV-BRC+NCBI isolate row. isolate.source_id
-- above can only say which source the row is anchored to (BV-BRC, whenever a
-- BV-BRC genome exists) -- it can't say that e.g. country or collection_date
-- on that same row actually came from NCBI. One row per (isolate_id,
-- field_name) that had a non-null winning value; see pick_field() in
-- core_data_loader/common/accessions.py and its use in build_isolates_and_sequences().
CREATE TABLE IF NOT EXISTS core.isolate_field_source (
    isolate_id BIGINT NOT NULL REFERENCES core.isolate(isolate_id),
    field_name TEXT NOT NULL,
    source_id  INTEGER NOT NULL REFERENCES core.source(source_id),
    PRIMARY KEY (isolate_id, field_name)
);

CREATE TABLE IF NOT EXISTS core.sequence (
    sequence_id     BIGSERIAL PRIMARY KEY,
    isolate_id      BIGINT REFERENCES core.isolate(isolate_id),
    molecule_type   TEXT,                   -- e.g. ssRNA(-), genomic DNA
    sequence_type   TEXT,                   -- matches bvbrc_genome_sequence.sequence_type; hardcoded 'genomic' for ncbi rows
    segment         TEXT,
    length          INTEGER,
    sequence_text   TEXT,
    sequence_md5    TEXT,
    is_reference    BOOLEAN NOT NULL DEFAULT FALSE,
    release_date    DATE,                   -- submission/release date, distinct from isolate.collection_date
    source_id       INTEGER REFERENCES core.source(source_id)
);

-- Cell-level provenance for a merged BV-BRC+NCBI sequence row, same idea as
-- core.isolate_field_source above: sequence.source_id is always ctx.bvbrc_sid
-- on a merged row even when e.g. molecule_type or release_date came entirely
-- from NCBI's matched record. One row per (sequence_id, field_name) that had
-- a non-null winning value.
CREATE TABLE IF NOT EXISTS core.sequence_field_source (
    sequence_id BIGINT NOT NULL REFERENCES core.sequence(sequence_id),
    field_name  TEXT NOT NULL,
    source_id   INTEGER NOT NULL REFERENCES core.source(source_id),
    PRIMARY KEY (sequence_id, field_name)
);

CREATE TABLE IF NOT EXISTS core.feature (
    feature_id        BIGSERIAL PRIMARY KEY,
    sequence_id       BIGINT REFERENCES core.sequence(sequence_id),
    feature_type      TEXT,                 -- CDS, gene, mat_peptide, UTR, ...
    "start"           INTEGER,
    "end"             INTEGER,
    strand            TEXT,
    gene              TEXT,
    product           TEXT,
    annotation        TEXT,
    source_id         INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.protein (
    protein_id      BIGSERIAL PRIMARY KEY,
    feature_id      BIGINT REFERENCES core.feature(feature_id),
    sequence_text   TEXT,
    aa_length       INTEGER,
    product         TEXT,
    gene            TEXT,
    source_id       INTEGER REFERENCES core.source(source_id)
);

-- UCSC's single-reference eboVir3 gene model (from ucsc_ncbi_gene) — distinct
-- from core.feature.gene, which holds thousands of per-isolate BV-BRC/NCBI
-- annotations. This is one authoritative coordinate model per gene, useful as
-- a reference backbone (e.g. GP/sGP/ssGP share start/end but differ in
-- cds_end, reflecting Ebola's transcriptional RNA-editing site).
CREATE TABLE IF NOT EXISTS core.gene (
    gene_id       BIGSERIAL PRIMARY KEY,
    sequence_id   BIGINT REFERENCES core.sequence(sequence_id),
    gene_symbol   TEXT NOT NULL,
    strand        TEXT,
    start_pos     INTEGER,          -- txStart
    end_pos       INTEGER,          -- txEnd
    cds_start     INTEGER,
    cds_end       INTEGER,
    exon_count    INTEGER,
    description   TEXT,             -- from UCSC's geneDesc track
    source_id     INTEGER REFERENCES core.source(source_id)
);

-- Pfam domain hits along that same reference (from ucsc_ncbi_gene_pfam),
-- resolved to the core.gene they fall within where coordinates allow.
CREATE TABLE IF NOT EXISTS core.gene_domain (
    gene_domain_id BIGSERIAL PRIMARY KEY,
    gene_id        BIGINT REFERENCES core.gene(gene_id),
    pfam_name      TEXT NOT NULL,
    start_pos      INTEGER,
    end_pos        INTEGER,
    score          NUMERIC,         -- raw UCSC score (0-1000); low scores may be spurious HMM hits
    source_id      INTEGER REFERENCES core.source(source_id)
);

CREATE INDEX IF NOT EXISTS idx_gene_sequence ON core.gene(sequence_id);
CREATE INDEX IF NOT EXISTS idx_gene_domain_gene ON core.gene_domain(gene_id);

CREATE TABLE IF NOT EXISTS core.structure (
    structure_id    BIGSERIAL PRIMARY KEY,
    protein_id      BIGINT REFERENCES core.protein(protein_id),
    pdb_id          TEXT,
    resolution      NUMERIC,
    method          TEXT,
    title           TEXT,
    release_date    DATE,
    organism_name   TEXT,
    taxon_id        INTEGER REFERENCES core.taxon(taxon_id),
    source_id       INTEGER REFERENCES core.source(source_id),
    -- The only place in core.* with real amino-acid sequence text --
    -- core.protein.sequence_text is always null (genome_feature.csv only
    -- carries aa_length/md5, never the sequence itself; see build_features_and_proteins).
    -- protein_structure.csv does carry it, for the subset of proteins with a
    -- solved structure.
    sequence_text   TEXT
);

CREATE TABLE IF NOT EXISTS core.epitope (
    epitope_id       BIGSERIAL PRIMARY KEY,
    protein_id       BIGINT REFERENCES core.protein(protein_id),
    epitope_sequence TEXT,
    "start"          INTEGER,
    "end"            INTEGER,
    epitope_type     TEXT,
    host_species     TEXT,
    organism         TEXT,
    taxon_id         INTEGER REFERENCES core.taxon(taxon_id),  -- fallback when protein_id doesn't resolve, same idea as structure.taxon_id
    source_id        INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.epitope_assay (
    epitope_assay_id BIGSERIAL PRIMARY KEY,
    epitope_id       BIGINT NOT NULL REFERENCES core.epitope(epitope_id),
    assay_type       TEXT,
    mhc_allele       TEXT,
    host_species     TEXT,
    assay_outcome    TEXT,
    total_assays     INTEGER,
    pmid             TEXT,
    source_id        INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.antibody (
    antibody_id     BIGSERIAL PRIMARY KEY,
    name            TEXT,
    alias           TEXT,
    isotype         TEXT,
    isolation_host  TEXT,
    neutralizing    BOOLEAN,
    donor_outcome   TEXT,
    immunogen       TEXT,
    source_id       INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.antibody_epitope (
    antibody_epitope_id BIGSERIAL PRIMARY KEY,
    antibody_id         BIGINT NOT NULL REFERENCES core.antibody(antibody_id),
    epitope_id          BIGINT REFERENCES core.epitope(epitope_id),
    protein_id          BIGINT REFERENCES core.protein(protein_id),
    binding_comment     TEXT
);

CREATE TABLE IF NOT EXISTS core.alignment (
    alignment_id    BIGSERIAL PRIMARY KEY,
    name            TEXT,
    alignment_type  TEXT,
    method          TEXT,
    source_id       INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.alignment_member (
    alignment_member_id BIGSERIAL PRIMARY KEY,
    alignment_id        BIGINT NOT NULL REFERENCES core.alignment(alignment_id),
    sequence_id         BIGINT REFERENCES core.sequence(sequence_id),
    isolate_id          BIGINT REFERENCES core.isolate(isolate_id),
    strain_label        TEXT,
    aligned_sequence    TEXT,
    row_order           INTEGER
);

CREATE TABLE IF NOT EXISTS core.study (
    study_accession        TEXT PRIMARY KEY,
    brief_title            TEXT,
    official_title         TEXT,
    brief_description      TEXT,
    actual_start_date      DATE,
    actual_completion_date DATE,
    actual_enrollment      INTEGER,
    source_id              INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.study_arm (
    arm_accession   TEXT PRIMARY KEY,
    study_accession TEXT NOT NULL REFERENCES core.study(study_accession),
    name            TEXT,
    description     TEXT,
    type_reported   TEXT,
    type_preferred  TEXT
);

CREATE TABLE IF NOT EXISTS core.subject (
    subject_accession TEXT PRIMARY KEY,
    species           TEXT,
    gender            TEXT,
    race              TEXT,
    ethnicity         TEXT,
    strain            TEXT,
    source_id         INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.study_arm_subject (
    arm_accession     TEXT NOT NULL REFERENCES core.study_arm(arm_accession),
    subject_accession TEXT NOT NULL REFERENCES core.subject(subject_accession),
    age_event         TEXT,
    min_subject_age   NUMERIC,
    max_subject_age   NUMERIC,
    age_unit          TEXT,
    PRIMARY KEY (arm_accession, subject_accession)
);

CREATE TABLE IF NOT EXISTS core.biosample (
    biosample_accession       TEXT PRIMARY KEY,
    subject_accession         TEXT REFERENCES core.subject(subject_accession),
    study_accession           TEXT REFERENCES core.study(study_accession),
    name                      TEXT,
    type                      TEXT,
    subtype                   TEXT,
    study_time_collected      NUMERIC,
    study_time_collected_unit TEXT,
    study_time_t0_event       TEXT,
    source_id                 INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.experiment (
    experiment_accession   TEXT PRIMARY KEY,
    study_accession        TEXT REFERENCES core.study(study_accession),
    measurement_technique  TEXT,
    name                   TEXT,
    description            TEXT,
    source_id              INTEGER REFERENCES core.source(source_id)
);

CREATE TABLE IF NOT EXISTS core.experiment_sample (
    expsample_accession   TEXT PRIMARY KEY,
    experiment_accession  TEXT NOT NULL REFERENCES core.experiment(experiment_accession),
    biosample_accession   TEXT REFERENCES core.biosample(biosample_accession),
    name                  TEXT,
    result_schema         TEXT,
    repository_name       TEXT,   -- e.g. 'GEO' (from ImmPort's expsample_public_repository)
    repository_accession  TEXT    -- e.g. GEO GSM accession
);

CREATE TABLE IF NOT EXISTS core.protocol (
    protocol_accession TEXT PRIMARY KEY,
    name               TEXT,
    type               TEXT,
    description        TEXT
);

CREATE TABLE IF NOT EXISTS core.experiment_protocol (
    experiment_accession TEXT NOT NULL REFERENCES core.experiment(experiment_accession),
    protocol_accession   TEXT NOT NULL REFERENCES core.protocol(protocol_accession),
    PRIMARY KEY (experiment_accession, protocol_accession)
);

CREATE TABLE IF NOT EXISTS core.condition (
    condition_id         BIGSERIAL PRIMARY KEY,
    condition_reported   TEXT,
    condition_preferred  TEXT,
    ontology_id          TEXT
);

CREATE TABLE IF NOT EXISTS core.study_condition (
    study_accession TEXT NOT NULL REFERENCES core.study(study_accession),
    condition_id    BIGINT NOT NULL REFERENCES core.condition(condition_id),
    PRIMARY KEY (study_accession, condition_id)
);

-- Curated, hand-seeded knowledge linking a virus taxon to the disease(s) it
-- causes (e.g. Zaire ebolavirus 186538 -> Ebola hemorrhagic fever DOID:4325).
-- Unlike every other core.* table, this isn't derived from a raw.* source --
-- it's populated by core_data_loader.common.taxon_conditions after both
-- core.taxon and core.condition already have rows (see etl_taxon_condition.py).
CREATE TABLE IF NOT EXISTS core.taxon_condition (
    taxon_id     INTEGER NOT NULL REFERENCES core.taxon(taxon_id),
    condition_id BIGINT NOT NULL REFERENCES core.condition(condition_id),
    PRIMARY KEY (taxon_id, condition_id)
);

-- core.taxon_condition holds mappings at species rank only; genomes, epitopes
-- and structures mostly attach to taxa *below* the species (strains, the
-- legacy "Zaire ebolavirus" node, "Ebola virus", ...). This view gives every
-- descendant of a mapped taxon that taxon's conditions, so joining on
-- isolate/epitope/structure.taxon_id works directly. mapped_taxon_id is the
-- curated row the link was inherited from.
CREATE OR REPLACE VIEW core.taxon_condition_inherited AS
WITH RECURSIVE inherited AS (
    SELECT taxon_id, condition_id, taxon_id AS mapped_taxon_id
    FROM core.taxon_condition
  UNION
    SELECT t.taxon_id, i.condition_id, i.mapped_taxon_id
    FROM core.taxon t
    JOIN inherited i ON t.parent_id = i.taxon_id
)
SELECT taxon_id, condition_id, mapped_taxon_id FROM inherited;

CREATE TABLE IF NOT EXISTS core.treatment (
    treatment_accession TEXT PRIMARY KEY,
    name                TEXT,
    amount_value        TEXT,
    amount_unit         TEXT,
    duration_value      TEXT,
    duration_unit       TEXT,
    temperature_value   TEXT,
    temperature_unit    TEXT
);

CREATE TABLE IF NOT EXISTS core.biosample_treatment (
    biosample_accession TEXT NOT NULL REFERENCES core.biosample(biosample_accession),
    treatment_accession TEXT NOT NULL REFERENCES core.treatment(treatment_accession),
    PRIMARY KEY (biosample_accession, treatment_accession)
);

CREATE TABLE IF NOT EXISTS core.immune_exposure (
    exposure_accession            TEXT PRIMARY KEY,
    arm_accession                 TEXT NOT NULL REFERENCES core.study_arm(arm_accession),
    subject_accession             TEXT REFERENCES core.subject(subject_accession),
    exposure_process_reported     TEXT,
    exposure_process_preferred    TEXT,
    exposure_material_reported    TEXT,
    exposure_material_preferred   TEXT,
    exposure_material_ontology_id TEXT,  -- e.g. Vaccine Ontology id, 'VO:0004660'
    disease_reported              TEXT,
    disease_preferred             TEXT,
    disease_ontology_id           TEXT,  -- e.g. Disease Ontology id, 'DOID:4325'
    disease_stage_reported        TEXT,
    disease_stage_preferred       TEXT
);

CREATE TABLE IF NOT EXISTS core.publication (
    publication_id BIGSERIAL PRIMARY KEY,
    pubmed_id      TEXT UNIQUE,
    title          TEXT,
    authors        TEXT,
    journal        TEXT,
    year           TEXT,
    doi            TEXT
);

CREATE TABLE IF NOT EXISTS core.study_publication (
    study_accession TEXT NOT NULL REFERENCES core.study(study_accession),
    publication_id  BIGINT NOT NULL REFERENCES core.publication(publication_id),
    PRIMARY KEY (study_accession, publication_id)
);

CREATE INDEX IF NOT EXISTS idx_isolate_taxon ON core.isolate(taxon_id);
CREATE INDEX IF NOT EXISTS idx_sequence_isolate ON core.sequence(isolate_id);
CREATE INDEX IF NOT EXISTS idx_feature_sequence ON core.feature(sequence_id);
CREATE INDEX IF NOT EXISTS idx_protein_feature ON core.protein(feature_id);
CREATE INDEX IF NOT EXISTS idx_structure_protein ON core.structure(protein_id);
CREATE INDEX IF NOT EXISTS idx_epitope_protein ON core.epitope(protein_id);
CREATE INDEX IF NOT EXISTS idx_epitope_assay_epitope ON core.epitope_assay(epitope_id);
CREATE INDEX IF NOT EXISTS idx_antibody_epitope_antibody ON core.antibody_epitope(antibody_id);
-- Reverse-direction lookups. The indexes above cover each FK's "parent -> child"
-- side; these cover the traversals that actually get run: finding an entity by
-- its accession (xref_type/xref_value, not entity_id -- see resolve_sequence_id
-- in etl_ucsc.py), walking epitope -> antibody, and epitope -> taxon -> condition.
CREATE INDEX IF NOT EXISTS idx_xref_type_value ON core.xref(xref_type, xref_value);
CREATE INDEX IF NOT EXISTS idx_antibody_epitope_epitope ON core.antibody_epitope(epitope_id);
CREATE INDEX IF NOT EXISTS idx_epitope_taxon ON core.epitope(taxon_id);
CREATE INDEX IF NOT EXISTS idx_taxon_condition_condition ON core.taxon_condition(condition_id);

CREATE INDEX IF NOT EXISTS idx_biosample_subject ON core.biosample(subject_accession);
CREATE INDEX IF NOT EXISTS idx_biosample_study ON core.biosample(study_accession);
CREATE INDEX IF NOT EXISTS idx_experiment_study ON core.experiment(study_accession);

-- ===========================================================================
-- Clinical vocabulary: symptom and assay-type nodes
-- ===========================================================================
-- Before this, symptoms and assay types existed only as text inside other
-- columns (isolate patient_symptoms, experiment.measurement_technique,
-- epitope_assay.assay_type / assay_outcome). Text inside a column is not a
-- node: nothing can link to "fever", and no traversal can reach it. These
-- tables make each one a row with a stable ontology id, so isolates,
-- experiments, epitope assays -- and, through core.evidence_link, grants and
-- papers -- can all point at the same node.
--
-- Both vocabularies are curated, not inferred: seeded from
-- core_data_loader/common/clinical_vocab.py (same pattern as core.source and
-- core.taxon_condition), and limited to the terms the loaded data actually
-- uses. Ids are upserted on name, so they stay stable across reruns; the
-- ontology id (HPO / OBI) is the identifier to hand to anyone outside this DB.
--
-- The link tables are rebuilt by etl_clinical_vocab.py and hold FKs to rows
-- that other ETLs truncate (core.isolate, core.experiment, core.epitope_assay),
-- so those truncates CASCADE them away. run_all.py runs etl_clinical_vocab
-- after all three, and sql/checks.sql flags an emptied link table.

CREATE TABLE IF NOT EXISTS core.symptom (
    symptom_id        BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,          -- HPO's label, e.g. 'Fever'
    hpo_id            TEXT UNIQUE,                   -- e.g. 'HP:0001945'
    -- Only where the child is a kind of the parent by definition
    -- (Bloody diarrhea is a Diarrhea), never "often seen together".
    parent_symptom_id BIGINT REFERENCES core.symptom(symptom_id),
    -- Other names for retrieval: 'pyrexia', 'sore throat', and the source's own
    -- spellings ('diarrhorea'). Names, not nodes -- they never get an id.
    synonyms          TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS core.assay_type (
    assay_type_id        BIGSERIAL PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE,
    -- OBI id. NULL where OBI has no term at this granularity (it has
    -- intracellular cytokine staining only per cytokine), rather than borrowing
    -- a near-miss id that would claim a narrower or different assay.
    obi_id               TEXT UNIQUE,
    parent_assay_type_id BIGINT REFERENCES core.assay_type(assay_type_id),
    synonyms             TEXT[] NOT NULL DEFAULT '{}'
);

-- An isolate's patient presented with this symptom (LANL annotation).
CREATE TABLE IF NOT EXISTS core.isolate_symptom (
    isolate_id BIGINT NOT NULL REFERENCES core.isolate(isolate_id) ON DELETE CASCADE,
    symptom_id BIGINT NOT NULL REFERENCES core.symptom(symptom_id) ON DELETE CASCADE,
    PRIMARY KEY (isolate_id, symptom_id)
);
CREATE INDEX IF NOT EXISTS idx_isolate_symptom_symptom ON core.isolate_symptom(symptom_id);

-- An ImmPort experiment used this assay (from measurement_technique).
CREATE TABLE IF NOT EXISTS core.experiment_assay_type (
    experiment_accession TEXT NOT NULL REFERENCES core.experiment(experiment_accession) ON DELETE CASCADE,
    assay_type_id        BIGINT NOT NULL REFERENCES core.assay_type(assay_type_id) ON DELETE CASCADE,
    PRIMARY KEY (experiment_accession, assay_type_id)
);
CREATE INDEX IF NOT EXISTS idx_experiment_assay_type_assay ON core.experiment_assay_type(assay_type_id);

-- An epitope assay record was this kind of assay. One record can carry
-- several: its IEDB category (T cell / B cell / MHC binding) and, for LANL CTL
-- rows, each method it names ("IFNg ICC, lytic activity, EliSpot" is three).
CREATE TABLE IF NOT EXISTS core.epitope_assay_assay_type (
    epitope_assay_id BIGINT NOT NULL REFERENCES core.epitope_assay(epitope_assay_id) ON DELETE CASCADE,
    assay_type_id    BIGINT NOT NULL REFERENCES core.assay_type(assay_type_id) ON DELETE CASCADE,
    PRIMARY KEY (epitope_assay_id, assay_type_id)
);
CREATE INDEX IF NOT EXISTS idx_epitope_assay_assay_type_assay ON core.epitope_assay_assay_type(assay_type_id);

-- Grants and papers link to these nodes through core.evidence_link, whose
-- entity_type list (sql/04_evidence.sql) already includes 'symptom' and
-- 'assay_type'. Kept there, not repeated here, so there is one list to edit.

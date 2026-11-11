-- Filovirus gene families: the virus-neutral concept "the GP gene of a
-- filovirus", which links proteins from every filovirus to the Ebola
-- reference gene model without claiming they are Ebola genes. Seeded and
-- assigned by etl/gene_family.py from core_data_loader/curated/gene_families.py.
-- Run after 02_core.sql. Idempotent.

CREATE TABLE IF NOT EXISTS core.gene_family (
    -- Fixed in the curated list, not generated: stable across rebuilds.
    gene_family_id SMALLINT PRIMARY KEY,
    symbol         TEXT NOT NULL UNIQUE,   -- 'NP', 'VP35', 'VP40', 'GP', 'VP30', 'VP24', 'L'
    name           TEXT NOT NULL
);

ALTER TABLE core.gene
    ADD COLUMN IF NOT EXISTS gene_family_id SMALLINT REFERENCES core.gene_family(gene_family_id);

ALTER TABLE core.protein
    ADD COLUMN IF NOT EXISTS gene_family_id SMALLINT REFERENCES core.gene_family(gene_family_id);
-- Which name the family was read from: the protein's gene label, or, when
-- that is missing or unrecognised, its product name.
ALTER TABLE core.protein
    ADD COLUMN IF NOT EXISTS gene_family_basis TEXT;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'protein_gene_family_basis_check') THEN
    ALTER TABLE core.protein ADD CONSTRAINT protein_gene_family_basis_check
        CHECK (gene_family_basis IN ('gene', 'product')
               AND gene_family_id IS NOT NULL
               OR gene_family_basis IS NULL AND gene_family_id IS NULL);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_protein_gene_family ON core.protein (gene_family_id);
CREATE INDEX IF NOT EXISTS idx_gene_gene_family ON core.gene (gene_family_id);

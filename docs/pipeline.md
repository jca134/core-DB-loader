# The build pipeline

```
data/ ──load──▶ raw.*  ──build──▶ core.*  ──export──▶ Neo4j ──serve──▶ query page
  ▲                                 │
  └── fetch (network, run once)     └── check (sql/checks.sql)
```

- **`raw.*`** is a verbatim mirror of the source files. Every column is text,
  nothing is cleaned, and each loader replaces only its own tables.
- **`core.*`** is the normalized, cross-source model (`sql/02_core.sql`,
  `04_evidence.sql`, `05_clinical_vocab.sql`). It is rebuilt from `raw.*` by
  `filovirus build`. Nothing in it is edited by hand.
- **`ops.*`** records lineage: the ingest batches and files (`ops.ingest_batch`,
  `ops.ingest_file`), and which raw row each core row came from
  (`ops.entity_provenance`).
- **Neo4j** is a read-only copy of `core.*` for graph queries. See
  [graph.md](graph.md).

## Schema

`filovirus init-db` applies `sql/01_schemas.sql` … `06_gene_family.sql` in
order. Every file is idempotent, so it is safe to run again after pulling a
schema change.

## Build order

`filovirus build` (`etl/pipeline.py`) runs the steps below in this order. **The
order is load-bearing.** Getting it wrong raises no error; it leaves a
database that looks fine and is quietly wrong.

| # | Step | Builds | Must come after |
| --- | --- | --- | --- |
| 1 | `etl/bvbrc_hfv.py` | taxa, isolates, sequences, features, proteins, structures, epitopes, antibodies | -- |
| 2 | `etl/immport.py` | studies, subjects, biosamples, experiments, conditions | -- |
| 3 | `etl/ucsc.py` | the reference gene model (`core.gene`, `core.gene_domain`) | 1, which truncates `core.sequence` and cascades `core.gene` away |
| 4 | `etl/gene_family.py` | filovirus gene families; assigns every gene and protein to one | 1 and 3, which rebuild `core.protein` and `core.gene` |
| 5 | `etl/taxon_condition.py` | curated taxon → disease (`core.taxon_condition`) | 2, which truncates `core.condition` and cascades it away |
| 6 | `etl/antibody_target.py` | curated antibody → gene symbol | 1, which rebuilds `core.antibody_epitope` |
| 7 | `etl/clinical_vocab.py` | symptom and assay-type nodes and their links | 1 and 2, which cascade its link tables away |
| 8 | `etl/evidence.py` | grants, patents, papers, and every evidence link | everything: it stores surrogate keys the other steps regenerate |

Run a single step with `python -m core_data_loader.etl.<step>`, then **always
rerun `etl.evidence` afterwards**. It stores resolved primary keys for genes,
epitopes, structures, antibodies, isolates, features and conditions, and
those keys are regenerated on every run. `filovirus build` does all of this
for you.

After the steps, the build:

1. **Resynchronises every `BIGSERIAL` sequence.** The ETL assigns ids itself,
   so sequences would otherwise sit at 1, and the next ordinary `INSERT` would
   collide on the primary key.
2. **Runs `sql/checks.sql`** and exits non-zero if any check fails. The checks
   cover orphaned links, emptied curated tables, duplicates, raw→core row
   counts, and lineage gaps. Each row's `what_it_means` names the defect and
   the fix. Run them on their own with `filovirus check`.

## Curated knowledge

Mappings no source provides are written by hand in `core_data_loader/curated/`
and reviewed like code:

| File | Provides |
| --- | --- |
| `taxon_conditions.py` | species-rank taxon → disease (DOID), plus `CURATED_CONDITIONS`, which seeds filovirus diseases that ImmPort doesn't supply (Marburg). Query through the `core.taxon_condition_inherited` view, which carries each mapping down to every strain |
| `antibody_targets.py` | LANL antibody product label → gene symbol, which is what makes "which antibodies target GP" answerable |
| `gene_families.py` | the seven filovirus gene families (NP, VP35, VP40, GP, VP30, VP24, L) and the names that map a protein onto one, from its gene label or else its product name. `core.gene` is Ebola's reference model only; the family is the virus-neutral bridge, so a Marburg GP protein is linked to "GP" without being called an Ebola gene |
| `clinical_vocab.py` | the symptom (HPO) and assay-type (OBI) vocabularies and their synonyms. Outside this database, refer to these nodes by their HPO/OBI ids, never the surrogate ids |

## Known data traps

These are written down because each one once cost a silent row loss:

- **Raw dates.** `safe_date` returns `None` on any format it doesn't know, so
  a new date format nulls a whole column without an error. Add the format to
  `common/db_utils.py`.
- **Taxonomy names.** NCBI renamed the filovirus genera: Ebolavirus became
  Orthoebolavirus, and Marburgvirus became Orthomarburgvirus. Sources link to
  both names, so anything that filters by taxon must walk `CHILD_OF` from
  both roots.
- **Row counts.** A matching count proves nothing on its own. When changing
  the ETL, build a copy with the change and compare its contents with the
  current database (`filovirus compare`, below).

## Comparing two builds

`filovirus compare OTHER_DB` reads every `core.*` table, plus the lineage
table, from both databases and reports each table as `same` or `DIFF`, with
sample rows that exist on one side only. Two kinds of id legitimately differ
between correct builds, so they are compared by natural key instead: source
ids by `source_code`, and publication ids by PMID (including links, xrefs and
lineage that point at a paper). Everything else is compared as is.

```bash
createdb -T filovirus filovirus_test
FILOVIRUS_DB_URL=postgresql+psycopg2://$USER@localhost:5432/filovirus_test uv run filovirus build
uv run filovirus compare filovirus_test     # every DIFF should be one you meant
```

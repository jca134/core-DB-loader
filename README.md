# core-data-loader

Loads filovirus (Ebola/Marburg) data from BV-BRC, HFV/LANL, ImmPort, NCBI, and
UCSC into a Postgres database, then transforms it from `raw.*` mirrors into a
shared `core.*` schema. An evidence layer adds NIH grants, USPTO patents, and
research papers, each linked to the entities already in the graph.

The database is the domain knowledge graph for the **SerenQA** serendipity-aware
KGQA algorithm — see `overview/README.md` for the full project orientation,
including which schema decisions that algorithm drives.

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — manages
  the virtual environment and dependencies for this project
- **PostgreSQL** — running locally, or reachable over the network

## Setup

1. Install dependencies (uv creates `.venv` and installs everything from
   `pyproject.toml`/`uv.lock` automatically):

   ```bash
   uv sync
   ```

2. Create the database and its schemas/tables:

   ```bash
   createdb filovirus
   psql -d filovirus -f sql/01_schemas.sql -f sql/02_core.sql \
                     -f sql/03_ops.sql \
                     -f sql/04_evidence.sql \
                     -f sql/05_clinical_vocab.sql
   ```

3. Point the scripts at your database. By default they connect to
   `postgresql+psycopg2://$(whoami)@localhost:5432/filovirus`. If your setup
   differs (different user, password, host, or database name), set:

   ```bash
   export FILOVIRUS_DB_URL="postgresql+psycopg2://user:password@host:5432/filovirus"
   ```

4. Add source data. `data/` is git-ignored, so you'll need to place the raw
   downloads yourself, one subfolder per source. Put the data folder under the project root:

   ```
   data/bvbrc/     data/hfv/     data/immport/     data/ncbi/     data/ucsc/
   data/grants/    data/patents/ data/papers/
   ```

   The papers bundle ships as a zip; unzip it in place:

   ```bash
   cd data/papers && unzip 'papers_iedb_rcsb_update (1) (1).zip'
   ```

## Usage

Run the loaders you need (each mirrors its source files verbatim into
`raw.*`, replacing the table if it already exists):

```bash
uv run python -m core_data_loader.loaders.load_bvbrc
uv run python -m core_data_loader.loaders.load_hfv
uv run python -m core_data_loader.loaders.load_immport
uv run python -m core_data_loader.loaders.load_ncbi
uv run python -m core_data_loader.loaders.load_ucsc
uv run python -m core_data_loader.loaders.load_grants
uv run python -m core_data_loader.loaders.load_patents
uv run python -m core_data_loader.loaders.load_papers
```

Then populate `core.*` from `raw.*`. One command runs every ETL in the required
order and verifies the result:

```bash
uv run python -m core_data_loader.etl.run_all
```

The order is a correctness requirement, not a convention — `etl_bvbrc_hfv`
truncates `core.sequence`, which cascades into `core.gene`; `etl_immport`
cascades into `core.taxon_condition`; `etl_evidence` resolves links to keys
every earlier script regenerates. Getting it wrong raises nothing and leaves a
database that looks fine. `run_all` also resynchronises the `BIGSERIAL`
sequences afterwards and then runs `sql/checks.sql`, exiting non-zero if any
check fails.

To run the steps individually instead, use the order below.

Then run the matching ETL script to populate `core.*` from `raw.*`. Run them
in this order: `etl_bvbrc_hfv` rebuilds `core.sequence`, which empties
`core.gene`/`core.gene_domain` (FK cascade), so `etl_ucsc` has to come after it.

```bash
uv run python -m core_data_loader.etl.etl_bvbrc_hfv
uv run python -m core_data_loader.etl.etl_immport
uv run python -m core_data_loader.etl.etl_ucsc
```

Then, once both `etl_bvbrc_hfv` and `etl_immport` have run, seed the curated
taxon-to-disease mapping (`core.taxon_condition`, e.g. Zaire ebolavirus ->
Ebola hemorrhagic fever):

```bash
uv run python -m core_data_loader.etl.etl_taxon_condition
uv run python -m core_data_loader.etl.etl_antibody_target
```

`etl_antibody_target` fills `core.antibody_epitope.target_gene_symbol` from LANL's
product label, which is what makes "which antibodies target GP" answerable —
`protein_id` is null on every row, because a label like "glycoprotein" cannot
identify one of the 32,004 accession-keyed `core.protein` rows.

Finally, build the evidence layer — grants, patents, papers, and the links from
each to entities already in `core.*`:

```bash
uv run python -m core_data_loader.etl.etl_evidence
```

**`etl_evidence` must run last, and must be rerun whenever any other ETL
reruns.** It stores resolved primary keys, and most of those are surrogate keys
that `etl_bvbrc_hfv` (epitope/structure/antibody), `etl_ucsc` (gene) and
`etl_immport` (condition) regenerate on every run — the same standing
requirement as `etl_taxon_condition`. The script ends with an orphan check;
if it reports anything other than zero dangling rows, something upstream moved.

Grant-to-paper edges need one extra, network-dependent step. It queries the NIH
RePORTER publications API (free, no key) and writes a CSV that `load_grants`
picks up automatically on later runs:

```bash
uv run python -m core_data_loader.loaders.fetch_grant_publications
```

That list cites ~20k papers, almost none of them in the papers bundle. A
second fetch pulls their PubMed records (title, abstract, MeSH); `etl_evidence`
then adds the filovirus ones (~1,270: a filovirus term in the title or MeSH,
or twice in the abstract) to `core.publication`, so their citations become
edges instead of being dropped. Run it after `fetch_grant_publications`:

```bash
uv run python -m core_data_loader.loaders.fetch_cited_publications
```

Papers need one too. PubMed supplies the titles and years the papers bundle
doesn't carry; without it every paper node is labelled `PMID 28854372` and has
no date, which keeps papers out of any temporal analysis:

```bash
uv run python -m core_data_loader.loaders.fetch_publication_metadata
```

Evidence links are not all equally strong. A link built from an exact
identifier carries `confidence = 1.0`; one built from a keyword in a grant
abstract or patent title carries less, and every link records the term and
field that produced it in `core.evidence_link_support`.

Documents link to each other directly in `core.evidence_link` (a grant citing a
paper, for instance), not through a shared entity: pairing documents that
mention the same taxon connected 87% of all grant/patent pairs and told you
nothing.

Symptoms and assay types are nodes too (`core.symptom`, `core.assay_type`),
seeded from the curated lists in `common/clinical_vocab.py` with HPO / OBI ids,
and linked to isolates, ImmPort experiments and epitope assays by
`etl_clinical_vocab`. Outside this DB, refer to them by their HPO/OBI ids,
not the surrogate ids.

After any ETL run, verify the database with the data-quality checks. Every
count the report prints should be 0:

```bash
psql -d filovirus -f sql/checks.sql
```

Mappings are made at species rank. To find the conditions for an isolate,
epitope, or structure, join on the `core.taxon_condition_inherited` view,
which carries each mapping down to every descendant taxon (strains, legacy
species names, and so on).

## Graph database (Neo4j)

The same data can be queried as a graph in Neo4j, using Cypher -- the query
language SerenQA's retrieval task generates. Postgres stays the source of
truth: the export wipes Neo4j and rebuilds it from `core.*`, so rerun it after
every `run_all`.

One-time setup (macOS):

```bash
brew install neo4j
neo4j-admin dbms set-initial-password filovirus   # before the first start
brew services start neo4j
```

Then export:

```bash
uv run python -m core_data_loader.export.export_neo4j
```

It fails if any node or relationship count differs from the Postgres rows it
came from. Override the connection with `NEO4J_URI`, `NEO4J_USER` and
`NEO4J_PASSWORD`.

Query it in Neo4j Browser at <http://localhost:7474> (user `neo4j`). Nodes are
the core tables in CamelCase (`Taxon`, `Grant`, `Patent`, `Publication`, ...)
keyed on the same primary keys; evidence links are `RELATED`, `CITES`,
`CLAIMS` and `DESCRIBES` relationships carrying `confidence` and
`matched_terms`. External ids are node properties (`xref_genbank_accession`,
`xref_ncbi_protein_id`, ...).

```cypher
// papers cited by both a patent and a grant
MATCH (p:Patent)-[:CITES]->(paper:Publication)<-[:CITES]-(g:Grant)
RETURN p, paper, g LIMIT 25
```

Raw sequence strings and lineage tables stay in Postgres only.

## Project layout

| Path | Purpose |
| --- | --- |
| `core_data_loader/common/db_utils.py` | DB engine + shared value-parsing helpers, no schema knowledge |
| `core_data_loader/common/sources.py` | Catalog of external datasets; upserts `core.source` rows |
| `core_data_loader/common/taxon_conditions.py` | Curated taxon-to-disease mappings; upserts `core.taxon_condition` rows |
| `core_data_loader/common/antibody_targets.py` | Curated LANL-product-to-gene mappings; fills `antibody_epitope.target_gene_id` |
| `core_data_loader/common/kg_keywords.py` | Shared KG search vocabulary + the text matcher behind keyword evidence links |
| `core_data_loader/common/raw_common.py` | Shared helpers for `loaders/` (raw ingestion) |
| `core_data_loader/common/etl_common.py` | Shared helpers for `etl/` (raw → core transforms) |
| `core_data_loader/loaders/` | One script per source; loads raw files into `raw.<table>` |
| `core_data_loader/etl/` | Transforms `raw.*` tables into `core.*` |
| `core_data_loader/export/export_neo4j.py` | Rebuilds the Neo4j graph from `core.*` |
| `core_data_loader/profiling/profile_bvbrc.py` | Profiles `raw.bvbrc_*` columns, writes `profile/bvbrc_profile.csv` |
| `sql/` | Schema DDL — run once against a fresh database |
| `overview/` | Project orientation, the SerenQA paper, and team planning docs (git-ignored) |

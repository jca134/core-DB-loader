# core-data-loader

Loads filovirus (Ebola/Marburg) data from BV-BRC, HFV/LANL, ImmPort, NCBI, and
UCSC into a Postgres database, then transforms it from `raw.*` mirrors into a
shared `core.*` schema.

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
   psql -d filovirus -f sql/01_schemas.sql -f sql/02_core.sql -f sql/03_ops.sql
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
```

Then run the matching ETL script to populate `core.*` from `raw.*`:

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
```

## Project layout

| Path | Purpose |
| --- | --- |
| `core_data_loader/common/db_utils.py` | DB engine + shared value-parsing helpers, no schema knowledge |
| `core_data_loader/common/sources.py` | Catalog of external datasets; upserts `core.source` rows |
| `core_data_loader/common/taxon_conditions.py` | Curated taxon-to-disease mappings; upserts `core.taxon_condition` rows |
| `core_data_loader/common/raw_common.py` | Shared helpers for `loaders/` (raw ingestion) |
| `core_data_loader/common/etl_common.py` | Shared helpers for `etl/` (raw → core transforms) |
| `core_data_loader/loaders/` | One script per source; loads raw files into `raw.<table>` |
| `core_data_loader/etl/` | Transforms `raw.*` tables into `core.*` |
| `core_data_loader/profiling/profile_bvbrc.py` | Profiles `raw.bvbrc_*` columns, writes `profile/bvbrc_profile.csv` |
| `sql/` | Schema DDL — run once against a fresh database |

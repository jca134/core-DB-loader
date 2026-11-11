# Filovirus knowledge graph

Builds a knowledge graph of Ebola and Marburg viruses, and lets you query it.
It combines:
- genomes, proteins, structures, epitopes and antibodies from BV-BRC, NCBI,
  LANL and UCSC
- clinical studies from ImmPort
- the NIH grants, USPTO patents and papers about them, linked to everything
  above

It is the domain knowledge graph for **SerenQA**, a serendipity-aware
question-answering algorithm. That is why links are explicit, typed and carry
a confidence score.

```
data/ ──load──▶ Postgres raw.* ──build──▶ Postgres core.* ──export──▶ Neo4j ──serve──▶ query page
  ▲                                            │
  └── fetch (network lookups, run once)        └── check (data-quality checks)
```

Everything runs through one command, `filovirus`. Postgres is the source of
truth, and Neo4j is a read-only copy for graph queries.

## Query the graph

If everything is already set up:

```bash
brew services start neo4j   # if it isn't running
uv run filovirus serve      # then open http://localhost:8765
```

- **Pick a question (free).** Ready-made questions with dropdowns, such as
  "Which antibodies target [GP]?" or "Which NIH grants study [Marburg
  viruses]?". Each runs a fixed, checked query and shows it. Results download
  as CSV.
- **Ask in your own words (optional).** Paste an Anthropic API key into the
  page once and Claude answers any question, showing the queries it ran. Each
  question costs a few cents. Get a key at
  <https://console.anthropic.com/settings/keys>.

To write Cypher yourself, use Neo4j Browser at <http://localhost:7474>
(user `neo4j`, password `filovirus`).

## First-time setup

You need **Python 3.11+**, **[uv](https://docs.astral.sh/uv/)**, **PostgreSQL**
and, for the graph, **Neo4j** (`brew install neo4j`).

```bash
uv sync                                           # install dependencies and the `filovirus` command
createdb filovirus
uv run filovirus init-db                          # create the schemas and tables (safe to rerun)

neo4j-admin dbms set-initial-password filovirus   # once, before Neo4j's first start
brew services start neo4j
```

Put the source files under `data/`, one folder per source. Which files go
where is in [docs/data-sources.md](docs/data-sources.md). Then build
everything:

```bash
uv run filovirus rebuild    # load data/ -> build the database -> export to Neo4j  (~2 min)
```

Settings come from environment variables. The defaults fit a local setup:

| Variable | Default |
| --- | --- |
| `FILOVIRUS_DB_URL` | `postgresql+psycopg2://$USER@localhost:5432/filovirus` |
| `FILOVIRUS_DATA_DIR` | `./data` |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `neo4j` / `filovirus` |
| `NCBI_EMAIL` | unset. The contact address sent with PubMed requests by `filovirus fetch` |
| `ANTHROPIC_API_KEY` | unset. Optional; a key can also be pasted into the query page |

## Commands

Run them as `uv run filovirus ...`, or as plain `filovirus ...` once the
virtualenv is activated.

| Command | Does |
| --- | --- |
| `init-db` | create the Postgres schemas and tables; idempotent |
| `load [SOURCE ...]` | source files in `data/` → `raw.*` (all sources by default) |
| `fetch NAME ...` | NIH RePORTER / PubMed lookups that add CSVs to `data/`. Run once and keep the files ([details](docs/data-sources.md#written-by-filovirus-fetch-network-free-no-key)) |
| `build` | `raw.*` → `core.*` in the required order, then the data-quality checks |
| `check` | only the data-quality checks; every failure count should be 0 |
| `compare OTHER_DB` | compare this database's contents with another build, table by table |
| `export` | rebuild the Neo4j graph from `core.*` |
| `serve` | the query page at <http://localhost:8765> |
| `ask "QUESTION"` | one plain-English question in the terminal (needs an API key) |
| `rebuild` | `load` + `build` + `export`: the usual refresh after data changes |

After changing anything in `data/`, run `filovirus rebuild`. The build order is
load-bearing; [docs/pipeline.md](docs/pipeline.md) explains why.

## Documentation

| | |
| --- | --- |
| [docs/data-sources.md](docs/data-sources.md) | which file goes where, required vs optional, and what each fetcher adds |
| [docs/pipeline.md](docs/pipeline.md) | raw → core → Neo4j, the build order and why it matters, the checks, curated mappings, known data traps |
| [docs/evidence-layer.md](docs/evidence-layer.md) | how grants, patents and papers link to the graph, confidence scores, what was deliberately left out |
| [docs/graph.md](docs/graph.md) | the Neo4j model, the query page, adding ready-made questions, the API key and safety |

`overview/` (git-ignored, shared separately) holds the team's planning
documents and the SerenQA paper.

## Project layout

```
core_data_loader/
├── cli.py          the `filovirus` command
├── config.py       every setting and path, from the environment
├── common/         DB engine, value parsing, raw/ETL helpers, source catalog, keyword matcher
├── curated/        hand-curated mappings: taxon→disease, antibody→gene, gene families, symptom/assay vocabularies
├── loaders/        one per source: data/ → raw.*            (filovirus load)
├── fetchers/       NIH RePORTER / PubMed lookups → data/     (filovirus fetch)
├── etl/            raw.* → core.*, run in order by pipeline.py   (filovirus build)
└── graph/          Neo4j export, query page, ready-made questions, Claude questions
sql/                schema DDL (01–06) and the data-quality checks (checks.sql)
tests/              unit tests: pure logic only, no database needed
docs/               the documentation above
```

## Development

```bash
uv run pytest           # unit tests; no database needed
uv run ruff check .     # lint
uv run filovirus check  # data-quality checks against the live database
```

CI (`.github/workflows/ci.yml`) runs the lint and unit tests on every push.

Before merging an ETL change, build a throwaway copy with the change and
compare its contents with the real database. Matching row counts alone have
hidden real bugs here before.

```bash
createdb -T filovirus filovirus_test
FILOVIRUS_DB_URL=postgresql+psycopg2://$USER@localhost:5432/filovirus_test uv run filovirus build
uv run filovirus compare filovirus_test   # every DIFF should be a change you meant
dropdb filovirus_test
```

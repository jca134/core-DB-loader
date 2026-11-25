# Filovirus knowledge graph

Builds a knowledge graph of Ebola and Marburg viruses, and lets you explore and
query it. It combines:
- genomes, proteins, structures, epitopes and antibodies from BV-BRC, NCBI,
  LANL and UCSC
- clinical studies from ImmPort
- the NIH grants, USPTO patents and papers about them, linked to everything
  above

It is the domain knowledge graph for **SerenQA**, a serendipity-aware
question-answering algorithm. That is why links are explicit, typed and carry
a confidence score.

```
data/ ──load──▶ Postgres raw.* ──build──▶ Postgres core.* ──export──▶ Neo4j ──serve──▶ dashboard + query page
  ▲                                            │
  └── fetch (network lookups, run once)        └── check (data-quality checks)
```

Everything runs through one command, `filovirus`. Postgres is the source of
truth, and Neo4j is a read-only copy for graph queries.

## Setup

You need **Python 3.11+**, **[uv](https://docs.astral.sh/uv/)**, **PostgreSQL**
and **Neo4j** (`brew install neo4j`).

1. Install, and create the database:
   ```bash
   uv sync                                           # dependencies and the `filovirus` command
   createdb filovirus
   uv run filovirus init-db                          # schemas and tables (safe to rerun)
   ```
2. Start Neo4j:
   ```bash
   neo4j-admin dbms set-initial-password filovirus   # once, before Neo4j's first start
   brew services start neo4j
   ```
3. Put the source files under `data/`, one folder per source.
   [docs/data-sources.md](docs/data-sources.md) lists which file goes where.
4. Build everything:
   ```bash
   uv run filovirus rebuild    # load data/ -> build Postgres -> export to Neo4j  (~2 min)
   ```

Rerun `filovirus rebuild` after anything in `data/` changes, or just
`filovirus export` if only the database changed: the graph in Neo4j is a copy.

Settings come from environment variables. The defaults fit a local setup:

| Variable | Default |
| --- | --- |
| `FILOVIRUS_DB_URL` | `postgresql+psycopg2://$USER@localhost:5432/filovirus` |
| `FILOVIRUS_DATA_DIR` | `./data` |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `neo4j` / `filovirus` |
| `NCBI_EMAIL` | unset. The contact address sent with PubMed requests by `filovirus fetch` |
| `ANTHROPIC_API_KEY` | unset. Optional; a key can also be pasted into the query page |

## Dashboard: explore the full graph

Once it's set up:

```bash
brew services start neo4j   # if it isn't running
uv run filovirus serve      # leave this terminal open; Ctrl-C stops it
```

Then open **<http://localhost:8765/explore>**. It reads the **whole** graph live
from Neo4j (189,897 nodes, genome-annotation features included). The line under
the search box reads "Live from Neo4j: the full graph, …" when you're on the
live version.

- **Search** for any virus, protein, gene, epitope, antibody, countermeasure,
  grant, paper or patent, and see everything within 1–3 steps of it.
- **Steps out** sets how far to walk. **Links followed per record** limits each
  record past the start to its best links (ranked by edge confidence, as
  SerenQA's neighbor scoring does); the default follows all of them. Use a limit
  when two steps runs through a hub and reaches most of the graph.
- **The drawing** starts by type: one circle per record type, sized by count,
  with line thickness showing how many links join two types. Double-click a
  circle to show its records one by one; select a node to fade everything not
  linked to it; **Explore from here** moves into a smaller subgraph (a trail
  leads back). **Every record** switches to one dot per record.
- **Export** the subgraph as JSON, CSV, GraphML, or a Cypher query that returns
  it in Neo4j Browser.
- **The other tabs** summarize the grants, papers and patents and the links
  between them.

The first search takes about 7 seconds while the server indexes every node;
after that, searches are instant and subgraphs load in a second or two. After a
new `filovirus export`, the search picks up the new graph by itself; restart
`serve` to refresh the totals shown on the page.

## Query page

`filovirus serve` also runs the query page at <http://localhost:8765>:

- **Pick a question (free).** Ready-made questions with dropdowns, such as
  "Which antibodies target [GP]?" or "Which NIH grants study [Marburg
  viruses]?". Each runs a fixed, checked query and shows it. Results download
  as CSV.
- **Ask in your own words (optional).** Paste an Anthropic API key into the
  page once and Claude answers any question, showing the queries it ran. Each
  question costs a few cents. Get a key at
  <https://console.anthropic.com/settings/keys>.
- **Review links (free).** Mark keyword-based links right or wrong; their
  confidence becomes a measured precision (`filovirus review-stats`).

To write Cypher yourself, open Neo4j Browser at <http://localhost:7474> (user
`neo4j`, password `filovirus`). `CALL db.schema.visualization();` draws every
node type with the relationships between them; for real data, try
`MATCH (g:Gene {gene_symbol: 'GP'})<-[r]-(n) RETURN g, r, n LIMIT 50`.

## Published dashboard (static copy)

The same page can also be built as one self-contained file,
`dashboard/output/research_dashboard.html` (git-ignored), which opens in any
browser and is published as a Claude artifact. To fit the artifact's 16 MB
limit, it embeds a trimmed copy of the graph: the 136k genome-annotation
features are dropped (each protein links straight to its sequence instead) and
properties are trimmed. The Cypher export adds those features back.

```bash
uv run python dashboard/build_dashboard.py   # the whole page; works from a fresh clone
uv run python dashboard/build_explorer.py    # graph only, into an already built page
```

- The document tabs are built from input files that are in git (`data/` and
  `federal-biomedical-tech-datasets-script/output/`; the root `.gitignore`
  lists the exceptions).
- The graph is read from Neo4j when it's running, and otherwise from
  `dashboard/snapshot/graph.json.gz`, a checked-in copy. A build that reads
  Neo4j rewrites that file; commit it when the graph has changed.
- `build_explorer.py [page.html]` replaces only the graph inside an existing
  page (by default `dashboard/output/research_dashboard.html`), for example
  after the database changes or on a downloaded copy of the artifact.

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
| `graph-report` | the Neo4j graph's hubs, isolated nodes and degree per node type |
| `review-stats` | precision of human-reviewed links, per confidence level (review on the query page) |
| `export` | rebuild the Neo4j graph from `core.*` (~20 s) |
| `serve` | the dashboard with the full live graph at <http://localhost:8765/explore>, and the query page at <http://localhost:8765> |
| `ask "QUESTION"` | one plain-English question in the terminal (needs an API key) |
| `rebuild` | `load` + `build` + `export`: the usual refresh after data changes |

The build order is load-bearing; [docs/pipeline.md](docs/pipeline.md) explains
why.

## Documentation

| | |
| --- | --- |
| [docs/data-sources.md](docs/data-sources.md) | which file goes where, required vs optional, and what each fetcher adds |
| [docs/pipeline.md](docs/pipeline.md) | raw → core → Neo4j, the build order and why it matters, the checks, curated mappings, known data traps |
| [docs/evidence-layer.md](docs/evidence-layer.md) | how grants, patents and papers link to the graph, confidence scores, what was deliberately left out |
| [docs/graph.md](docs/graph.md) | the Neo4j model, hubs and the explorer's link limit, the query page, adding ready-made questions, the API key and safety |

`overview/` (git-ignored, shared separately) holds the team's planning
documents and the SerenQA paper.

## Project layout

```
core_data_loader/
├── cli.py          the `filovirus` command
├── config.py       every setting and path, from the environment
├── common/         DB engine, value parsing, raw/ETL helpers, source catalog, keyword matcher
├── curated/        hand-curated knowledge: taxon→disease, antibody→gene, gene families, countermeasures,
│                   organization names, symptom/assay vocabularies
├── loaders/        one per source: data/ → raw.*            (filovirus load)
├── fetchers/       NIH RePORTER / PubMed lookups → data/     (filovirus fetch)
├── etl/            raw.* → core.*, run in order by pipeline.py   (filovirus build)
└── graph/          Neo4j export, live explorer and web server, query page, ready-made questions
dashboard/          research dashboard: template, builders (build_dashboard.py, build_explorer.py), graph snapshot
sql/                schema DDL (01–10) and the data-quality checks (checks.sql)
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

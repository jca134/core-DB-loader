# The graph: Neo4j and the query page

Postgres is the source of truth. `filovirus export` wipes Neo4j and rebuilds
it from `core.*` in about 20 seconds, so rerun it after every build
(`filovirus rebuild` does). Nothing is ever edited in Neo4j.

Neo4j is used because SerenQA's benchmark knowledge graph, CKG, is a Neo4j
database, and its retrieval task is natural language → Cypher.

## Setup (macOS)

```bash
brew install neo4j
neo4j-admin dbms set-initial-password filovirus   # before the first start
brew services start neo4j                         # starts now and at login
```

The connection comes from `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD`
(defaults: `bolt://localhost:7687`, `neo4j`, `filovirus`). Change the
password anywhere the database is reachable by anyone else.

## Model

- **Nodes.** One per core row, labelled in CamelCase (`Taxon`, `Gene`,
  `Grant`, `Patent`, `Publication`, ...) and keyed on the same primary key
  column as in Postgres (`taxon_id`, `grant_award_id`, ...). Every node has a
  display `name`.
- **External ids** are list properties: `xref_genbank_accession`,
  `xref_ncbi_protein_id`, `xref_pubmed_id`, ...
- **Foreign keys and join tables** become relationships: `CHILD_OF`,
  `OF_TAXON`, `ON_PROTEIN`, `BINDS`, `TARGETS`, `CAUSES`, ... `CAUSES` is
  already inherited down to every strain.
- **Evidence links** become `RELATED`, `CITES`, `CLAIMS` and `DESCRIBES`
  relationships, carrying `confidence`, `matched_terms` and `support_count`.
- **Left out:** raw sequence strings and the lineage tables stay in Postgres.

The export compares every node and relationship count with the Postgres rows
it came from, and fails on any difference.

```cypher
// papers cited by both a patent and a grant
MATCH (p:Patent)-[:CITES]->(paper:Publication)<-[:CITES]-(g:Grant)
RETURN p, paper, g LIMIT 25
```

Write Cypher yourself in Neo4j Browser: <http://localhost:7474>.

## The query page

`filovirus serve` serves <http://localhost:8765>, on this computer only.

**Pick a question (free).** The ready-made questions in `graph/templates.py`
each have dropdown blanks. Every one is a fixed, reviewed Cypher query, and
the dropdown values reach Neo4j only as query parameters. Picking "Ebola
viruses" or "Marburg viruses" searches both genus names (see
[pipeline.md](pipeline.md#known-data-traps)). Results download as CSV.

To add a question, append to `TEMPLATES`. Use `$name` for each `{name}` blank,
and alias each returned column with its header. A column named `Link` is
rendered as a link.

**Ask in your own words (optional).** Claude (`graph/ask.py`) turns the
question into Cypher, runs it, reads the rows, refines the query if needed,
and answers. The page shows every query it ran. The same works from the
terminal with `filovirus ask "..."`.

- **Key:** `ANTHROPIC_API_KEY` if set; otherwise one pasted into the page. A
  pasted key is checked with Anthropic first, then saved owner-only in
  `~/.config/filovirus-graph/`, never inside the repo.
- **Cost:** a few cents per question.
- **Graph description:** `filovirus ask --show-schema` prints what Claude is
  given. It is generated from the live graph, so it stays current.

## Safety

- **Read-only queries.** Every query runs in a read-only Neo4j transaction
  with a 30 s limit, so a generated `DELETE` is rejected.
- **Local only.** The page answers only `localhost` requests, and refuses
  cross-site and non-JSON POSTs, so another website open in the same browser
  can't spend your key or read results.

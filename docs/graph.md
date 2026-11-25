# The graph: Neo4j and the dashboard

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
- **Gene families.** `Gene` holds Ebola's reference genes only. To cross
  viruses, go through `GeneFamily`: every protein of every filovirus, and
  every `Gene`, has `IN_FAMILY` to one of seven families. That bridge is how a
  Marburg epitope reaches the grants and patents about GP:
  `(:Epitope)-[:ON_PROTEIN]->(:Protein)-[:IN_FAMILY]->(:GeneFamily)<-[:IN_FAMILY]-(:Gene)<-[:RELATED]-(:Grant)`.
- **Evidence links** become `RELATED`, `CITES`, `CLAIMS`, `DESCRIBES` and
  `FUNDS` relationships, carrying `confidence`, `matched_terms` and
  `support_count`.
- **Countermeasures** (`Countermeasure`): `TARGETS` taxa, `ACTS_ON` a gene
  family, `CONTAINS` antibodies, `TESTED_IN` studies.
- **Organizations** (`Organization`): `(:Grant)-[:AWARDED_TO]->`,
  `(:Patent)-[:ASSIGNED_TO {role}]->`.
- **Dates.** Every `Grant`, `Patent` and `Publication` has `date` (when it
  became public, from `core.v_document_date`), `date_precision`
  (`day` / `fiscal_year` / `year`) and `date_basis`. A paper is dated by the
  earlier of its online date and its print issue year (`date_basis` `online`
  or `issue_year`): journals often publish online first, sometimes years first.
- **Left out:** raw sequence strings and the lineage tables stay in Postgres.

The export compares every node and relationship count with the Postgres rows
it came from, and fails on any difference.

```cypher
// papers cited by both a patent and a grant
MATCH (p:Patent)-[:CITES]->(paper:Publication)<-[:CITES]-(g:Grant)
RETURN p, paper, g LIMIT 25
```

Write Cypher yourself in Neo4j Browser: <http://localhost:7474>. To see the
whole model at a glance, with every label and relationship type, run
`CALL db.schema.visualization();`.

## The dashboard

`filovirus serve` serves <http://localhost:8765/explore>, on this computer
only. It is the research dashboard (`dashboard/`), with its graph explorer
reading Neo4j live through `graph/explore.py`: search any record, see and export
everything within 1–3 steps of it, across every node, Feature nodes included.
Search runs over an
in-memory list of every node, built on the first search (about 7 s) and rebuilt
when the node count changes, i.e. after `filovirus export`; subgraphs are
walked in Neo4j one step at a time (about 2 s for 30,000 nodes). Picking
"Ebola viruses" or "Marburg viruses" starts from both genus names (see
[pipeline.md](pipeline.md#known-data-traps)).

## Plain-English questions

`filovirus ask "..."` (`graph/ask.py`): Claude turns the question into Cypher,
runs it, reads the rows, refines the query if needed, and answers, printing
every query it ran.

- **Key:** `ANTHROPIC_API_KEY`, or a key saved earlier (by the old query page)
  owner-only in `~/.config/filovirus-graph/`, never inside the repo.
- **Cost:** a few cents per question.
- **Graph description:** `filovirus ask --show-schema` prints what Claude is
  given. It is generated from the live graph, so it stays current.

## Graph shape

`filovirus graph-report` prints, per node type, the node count, isolated
nodes, and mean and largest degree, then the biggest hubs. The gene-family
nodes are the largest by design: every protein of every filovirus points to
one, which makes them the bridges between viruses.

SerenQA doesn't remove hubs. Its 3-hop transition matrix is row-normalized, so
a node with thousands of links passes only a small share of probability to
each neighbor, and Novelty and Surprise are computed from those
probabilities. Where a hub has too many neighbors to show the LLM, it ranks
them by edge confidence and passes on the best ones (Appendix D.3, "Neighbor
Scoring"), which is what `confidence` on `RELATED`, `CITES`, `CLAIMS` and
`DESCRIBES` is for. So the graph keeps every hub and every link.

The subgraph explorer (`filovirus serve`, /explore)
applies that neighbor scoring to what it shows. The starting record passes on
all its links; every record reached after it passes on only its best N links
not already walked ("Links followed per record": all by default, or the best 5 to 250), ranked
by edge confidence and then by the neighbor's degree. Only inferred links
(`RELATED`, `CLAIMS`) have confidences below 1; a curated link with no
`confidence` is a recorded fact and counts as 1.0. Without it, two steps
from a protein reach 3,700-9,400 nodes through its gene family; with 25, 29.
Records that were limited are marked in the table. This is a viewing limit
only; it doesn't change the graph or anything scored from it.

## Safety

- **Read-only queries.** Every query runs in a read-only Neo4j transaction
  with a 30 s limit, so a generated `DELETE` is rejected.
- **Local only.** The dashboard server answers only `localhost` requests,
  and refuses cross-site and non-JSON POSTs, so another website open in the
  same browser can't read the graph through it.

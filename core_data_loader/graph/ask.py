"""
Ask the filovirus knowledge graph a question in plain English.

    filovirus ask "Which antibodies target GP?"
    filovirus serve          # the query page, http://localhost:8765

Claude translates the question into Cypher, runs it against the Neo4j copy of
the database (`filovirus export`), reads the rows, refines
the query if they don't answer the question, and replies -- showing every
Cypher query it ran, so an answer can always be checked by hand in Neo4j
Browser. This is the knowledge-retrieval task from SerenQA (NL -> Cypher).

SAFETY. Queries run in READ transactions: Neo4j rejects any write, so a
generated `DELETE` fails instead of damaging the graph (and the graph is a
rebuildable copy of Postgres anyway). Each query has a time limit and its
result is capped before it goes back to the model.

Needs an Anthropic API key: ANTHROPIC_API_KEY, or one saved from the query
page (see api_key.py). The page's ready-made questions need no key at all.
"""

import argparse
import json
import sys
from datetime import date, datetime

import anthropic
from anthropic import beta_tool
from neo4j import READ_ACCESS, GraphDatabase, unit_of_work
from neo4j.exceptions import Neo4jError

from core_data_loader.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from core_data_loader.graph import api_key

MODEL = "claude-opus-5-5"
MAX_ROWS = 50            # rows of a result the model sees
MAX_CELL_CHARS = 300     # abstracts are long; the model needs the gist only
QUERY_TIMEOUT_S = 30
MAX_ITERATIONS = 8       # Cypher attempts per question before giving up

GUIDE = """\
You answer questions about a filovirus (Ebola / Marburg) knowledge graph stored
in Neo4j, by writing Cypher and calling run_cypher. Ground every factual claim
in rows you actually retrieved; never answer from general knowledge alone. If
the graph doesn't contain the answer, say so plainly.

Conventions in this graph:
- Every node has a display `name`; keys are the Postgres primary keys
  (taxon_id, gene_id, grant_award_id, patent_id, publication_id, ...).
- Match names case-insensitively and loosely: toLower(n.name) CONTAINS 'marburg'.
  Taxon names are formal ("Orthomarburgvirus marburgense"), so prefer CONTAINS on
  a distinctive word, and check which taxa matched.
- Strains and legacy names hang under species via (:Taxon)-[:CHILD_OF]->(:Taxon).
  (:Taxon)-[:CAUSES]->(:Condition) is already inherited down to every strain.
- Evidence relationships RELATED / CITES / CLAIMS / DESCRIBES run from a document
  (Grant, Patent, Publication) to what it is about. They carry `confidence`
  (1.0 = exact identifier, 0.7 title keyword, 0.5 abstract keyword, lower =
  ambiguous term) and `matched_terms`. Mention confidence when it is below 0.7.
- External ids are list properties: xref_genbank_accession, xref_ncbi_protein_id,
  xref_pubmed_id, xref_nih_core_project_num, ... -- query with 'X' IN n.xref_...
- (:Gene) is the Ebola reference gene model only (NP, VP35, VP40, GP, sGP, ssGP,
  VP30, VP24, L). For any other filovirus, go through (:GeneFamily): every
  protein of every filovirus and every Gene has [:IN_FAMILY] to one of seven
  families (symbol 'NP', 'VP35', 'VP40', 'GP', 'VP30', 'VP24', 'L'). Which virus
  a protein belongs to: (p:Protein)-[:ENCODED_BY]->(:Feature)-[:ON_SEQUENCE]->
  (:Sequence)-[:FROM_ISOLATE]->(:Isolate)-[:OF_TAXON]->(:Taxon), or via an
  epitope's or structure's own [:OF_TAXON].
- Always LIMIT exploratory queries; count(*) before listing large sets.

When you answer: lead with the direct answer, then the supporting rows (as a
short list or table), then caveats. Keep it concise.

Graph schema (generated from the live database):
"""


def _clean(value):
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "..."
    if isinstance(value, list):
        return [_clean(v) for v in value[:20]]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if hasattr(value, "iso_format"):          # neo4j temporal types
        return value.iso_format()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "element_id"):          # a whole node / relationship
        return _clean(dict(value))
    return value


class Graph:
    """Read-only access to the Neo4j graph."""

    def __init__(self):
        self.driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        self.driver.verify_connectivity()

    def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        @unit_of_work(timeout=QUERY_TIMEOUT_S)
        def work(tx):
            result = tx.run(cypher, params or {})
            return [r.data() for r in result]

        with self.driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

    def schema(self) -> str:
        """Labels with their properties and counts, and every relationship pattern."""
        lines = ["Nodes:"]
        for row in self.run(
            "CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName "
            "WITH nodeLabels[0] AS label, collect(propertyName) AS props "
            "RETURN label, props ORDER BY label"
        ):
            count = self.run(f"MATCH (n:`{row['label']}`) RETURN count(n) AS c")[0]["c"]
            lines.append(f"  ({row['label']}) x{count}: {', '.join(sorted(p for p in row['props'] if p))}")
        lines.append("Relationships:")
        for row in self.run(
            "MATCH (a)-[r]->(b) WITH labels(a)[0] AS a, type(r) AS t, labels(b)[0] AS b, r "
            "WITH a, t, b, count(*) AS c, collect(keys(r))[0] AS props "
            "RETURN a, t, b, c, props ORDER BY a, t, b"
        ):
            props = f" {{{', '.join(sorted(row['props']))}}}" if row["props"] else ""
            lines.append(f"  ({row['a']})-[:{row['t']}{props}]->({row['b']}) x{row['c']}")
        return "\n".join(lines)

    def close(self):
        self.driver.close()


def ask(question: str, graph: Graph, client: anthropic.Anthropic, system: str) -> dict:
    """Answer one question. Returns the answer text and every query run, with its outcome."""
    queries = []

    @beta_tool
    def run_cypher(cypher: str) -> str:
        """Run a read-only Cypher query against the filovirus knowledge graph and
        return the result rows as JSON (at most 50 rows; long text is truncated).
        Errors are returned as text so the query can be fixed and retried.

        Args:
            cypher: One Cypher read query. Include a LIMIT when listing rows.
        """
        try:
            rows = graph.run(cypher)
        except Neo4jError as e:
            queries.append({"cypher": cypher, "error": e.message})
            return f"Cypher error: {e.message}"
        queries.append({"cypher": cypher, "rows": len(rows)})
        payload = {"row_count": len(rows), "rows": [_clean(r) for r in rows[:MAX_ROWS]]}
        if len(rows) > MAX_ROWS:
            payload["note"] = f"showing {MAX_ROWS} of {len(rows)} rows"
        return json.dumps(payload, default=str)

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        max_iterations=MAX_ITERATIONS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[run_cypher],
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": question}],
    )
    final = runner.until_done()

    if final.stop_reason == "refusal":
        answer = "The model declined to answer this question."
    else:
        answer = "\n".join(b.text for b in final.content if b.type == "text").strip()
        if final.stop_reason == "tool_use":   # ran out of iterations mid-search
            answer = (answer + "\n\n" if answer else "") + \
                f"(Stopped after {MAX_ITERATIONS} query attempts without a final answer.)"
    return {"question": question, "answer": answer, "queries": queries}


# ---------------------------------------------------------------------------
# Command line (the query page is graph/web.py)
# ---------------------------------------------------------------------------

def schema_prompt() -> str:
    """The system prompt Claude is given: the guide plus the live graph's schema."""
    graph = Graph()
    try:
        return GUIDE + graph.schema()
    finally:
        graph.close()


def ask_cli(question: str) -> int:
    """Answer one question in the terminal. Returns an exit code."""
    key, _source = api_key.current_key()
    if not key:
        print("No Anthropic API key. Either add one on the query page (`filovirus serve`), or run\n"
              "  export ANTHROPIC_API_KEY='sk-ant-api03-...'\n"
              "with your key from https://console.anthropic.com -> API Keys.", file=sys.stderr)
        return 1
    client, problem = api_key.check_key(key, MODEL)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    graph = Graph()
    try:
        result = ask(question, graph, client, GUIDE + graph.schema())
    finally:
        graph.close()
    print(result["answer"])
    print("\nCypher run:")
    for q in result["queries"]:
        outcome = f"error: {q['error']}" if "error" in q else f"{q['rows']} rows"
        print(f"  [{outcome}]\n    " + q["cypher"].replace("\n", "\n    "))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("question", nargs="*", help="the question to ask")
    parser.add_argument("--show-schema", action="store_true", help="print the schema Claude sees and exit")
    args = parser.parse_args()
    if args.show_schema:
        print(schema_prompt())
        return 0
    if not args.question:
        parser.print_help()
        return 1
    return ask_cli(" ".join(args.question))


if __name__ == "__main__":
    sys.exit(main())

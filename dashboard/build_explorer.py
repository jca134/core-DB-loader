#!/usr/bin/env python3
"""Add the knowledge graph to the research dashboard, for its "Explore the graph" tab.

The tab searches for any virus, protein, gene, epitope, antibody, grant, paper
or patent, pulls out the subgraph around it, and exports that subgraph (JSON,
CSV, GraphML, or a Cypher query that returns the same subgraph in Neo4j
Browser). Everything runs in the page, so the graph is embedded in it.

The graph comes straight from Neo4j (`filovirus export`), so run that first.
Two things are trimmed to keep the page under the 16 MB artifact limit:

  * Feature nodes (136k genome annotations) are dropped. Each
    (Protein)-[:ENCODED_BY]->(Feature)-[:ON_SEQUENCE]->(Sequence) path becomes
    one (Protein)-[:ENCODED_ON]->(Sequence) link; the protein keeps its
    feature_id, so the Cypher export still matches the real path. Features a
    paper DESCRIBES are kept.
  * Properties are cut to a short per-label list, and long text is truncated.

build_dashboard.py calls graph_data() itself. Run this script on its own to
refresh the graph in an already built page (for example a copy of the
published artifact) without rebuilding the document tabs:

  python dashboard/build_explorer.py [PAGE.html]   # default: dashboard/output/research_dashboard.html
"""

import gzip
import json
import re
import sys
from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from core_data_loader.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER  # noqa: E402
from core_data_loader.graph.explore import LABELS, meta, node_row, short  # noqa: E402

OUT = HERE / "output" / "research_dashboard.html"
# Checked in, so the page can be rebuilt without Neo4j; every read from Neo4j refreshes it.
SNAPSHOT = HERE / "snapshot" / "graph.json.gz"
# The template's placeholder line; a built page has the JSON in its place.
GRAPH_LINE = re.compile(r"^(?:let|const) KG = .*$", re.M)
def write_snapshot(path, data):
    """Gzipped JSON with a fixed mtime, so an unchanged data set gives an unchanged file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
        gz.write(raw)


def read_snapshot(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def graph_data() -> dict:
    """The graph as compact JSON-ready lists, read from Neo4j."""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    labels = LABELS
    nodes, n_idx = [], {}          # element id -> index
    rels, r_idx = [], {}
    edges, eprops = [], {}

    def rel(t):
        if t not in r_idx:
            r_idx[t] = len(rels)
            rels.append(t)
        return r_idx[t]

    with driver.session() as s:
        described = {r["id"] for r in s.run(
            "MATCH (:Publication)-[:DESCRIBES]->(f:Feature) RETURN DISTINCT elementId(f) AS id")}
        for label in labels:
            for r in s.run(f"MATCH (n:{label}) RETURN elementId(n) AS id, properties(n) AS p"):
                if label == "Feature" and r["id"] not in described:
                    continue
                n_idx[r["id"]] = len(nodes)
                nodes.append(node_row(label, r["p"]))

        for r in s.run("MATCH (a)-[e]->(b) WHERE NOT (a:Protein AND b:Feature) "
                       "RETURN elementId(a) AS a, type(e) AS t, elementId(b) AS b, "
                       "e.confidence AS c, e.matched_terms AS m, e.support_count AS sc"):
            a, b = n_idx.get(r["a"]), n_idx.get(r["b"])
            if a is None or b is None:
                continue
            if r["c"] is not None or r["m"]:
                eprops[len(edges) // 3] = [r["c"], short(r["m"]) or "", r["sc"]]
            edges += [a, b, rel(r["t"])]
        enc = rel("ENCODED_ON")
        for r in s.run("MATCH (p:Protein)-[:ENCODED_BY]->(:Feature)-[:ON_SEQUENCE]->(q:Sequence) "
                       "RETURN elementId(p) AS a, elementId(q) AS b"):
            edges += [n_idx[r["a"]], n_idx[r["b"]], enc]

        neo_counts = {r["l"]: r["c"] for r in s.run(
            "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c")}
        neo_rels = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    driver.close()

    m = meta(neo_counts, neo_rels)
    return {"built": m["built"], "labels": m["labels"], "keys": m["keys"],
            "rels": rels, "nodes": nodes, "edges": edges, "eprops": eprops,
            "viruses": m["viruses"], "neo4j": m["neo4j"]}


def embed(html: str, data: dict) -> str:
    """Put the graph into a dashboard page (template or already built)."""
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    if not GRAPH_LINE.search(html):
        raise SystemExit("No `let KG = ...;` line in the page: is it built from dashboard.template.html?")
    return GRAPH_LINE.sub(lambda _: f"let KG = {blob};", html, count=1)


def summary(data: dict) -> str:
    by_label = Counter(data["labels"][n[0]] for n in data["nodes"])
    neo = data["neo4j"]
    return (f"  graph: {len(data['nodes']):,} nodes, {len(data['edges']) // 3:,} links "
            f"(Neo4j: {sum(neo['nodes'].values()):,} nodes, {neo['relationships']:,} relationships; "
            f"features kept: {by_label['Feature']:,} of {neo['nodes'].get('Feature', 0):,})")


def main():
    page = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    if not page.exists():
        raise SystemExit(f"{page} doesn't exist. Build it first: python dashboard/build_dashboard.py")
    data = graph_data()
    write_snapshot(SNAPSHOT, data)
    html = embed(page.read_text(encoding="utf-8"), data)
    page.write_text(html, encoding="utf-8")
    print(f"Wrote {page} ({len(html.encode()) / 2**20:.1f} MB)")
    print(summary(data))
    print(f"  saved {SNAPSHOT.relative_to(REPO)} (commit it so anyone can rebuild the page)")


if __name__ == "__main__":
    main()

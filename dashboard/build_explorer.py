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
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from core_data_loader.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER  # noqa: E402
from core_data_loader.graph.export import LABEL_KEY  # noqa: E402
from core_data_loader.graph.templates import VIRUSES  # noqa: E402

OUT = HERE / "output" / "research_dashboard.html"
SNAPSHOT = HERE / "snapshot" / "graph.json.gz"   # checked in; see build_dashboard.py
# The template's placeholder line; a built page has the JSON in its place.
GRAPH_LINE = re.compile(r"^const KG = .*$", re.M)
MAX_TEXT = 160

# Properties shown in the detail panel, per label (key and name are always kept).
PROPS = {
    "Taxon": ["taxon_rank", "lineage"],
    "Isolate": ["species", "host", "genome_status", "xref_bvbrc_genome_id"],
    "Sequence": ["length", "is_reference", "release_date", "xref_genbank_accession"],
    "Feature": ["feature_type", "start", "end", "annotation"],
    "Protein": ["aa_length", "xref_ncbi_protein_id", "feature_id"],
    "Gene": ["description", "start_pos", "end_pos", "strand"],
    "GeneFamily": ["symbol"],
    "GeneDomain": ["pfam_name", "start_pos", "end_pos"],
    "Structure": ["title", "method", "resolution", "organism_name", "release_date"],
    "Epitope": ["epitope_type", "organism", "host_species", "start", "end", "xref_bvbrc_epitope_id"],
    "EpitopeAssay": ["assay_type", "assay_outcome", "host_species"],
    "Antibody": ["isotype", "immunogen", "isolation_host", "donor_outcome"],
    "AssayType": ["obi_id", "synonyms"],
    "Symptom": ["hpo_id", "synonyms"],
    "Condition": ["ontology_id"],
    "Study": ["official_title", "actual_start_date", "actual_enrollment"],
    "StudyArm": ["description"],
    "Subject": ["species", "gender"],
    "ImmuneExposure": ["exposure_process_preferred", "exposure_material_ontology_id"],
    "Biosample": ["type", "study_time_collected", "study_time_collected_unit"],
    "Experiment": ["measurement_technique", "description"],
    "ExperimentSample": ["repository_name", "repository_accession"],
    "Protocol": ["type", "description"],
    "Alignment": ["method", "alignment_type"],
    "Publication": ["date", "journal", "authors", "pubmed_id", "doi", "url"],
    "Grant": ["date", "award_key", "contact_pi_name", "admin_ic_name", "activity_code",
              "first_fiscal_year", "last_fiscal_year", "relevance_tier"],
    "GrantFunding": ["fiscal_year", "project_detail_url"],
    "Patent": ["date", "document_id", "applicant_name", "inventor", "priority_date", "kind_code"],
    "Countermeasure": ["kind", "status", "developer", "regulatory", "names"],
    "Organization": ["kind", "org_key"],
}
# Extra searchable ids, per label, beyond name and key.
SEARCH = {
    "Taxon": ["taxon_name"], "Isolate": ["xref_bvbrc_genome_id", "xref_ncbi_assembly"],
    "Sequence": ["xref_genbank_accession"], "Protein": ["xref_ncbi_protein_id"],
    "Gene": ["gene_symbol"], "GeneFamily": ["symbol"], "Structure": ["pdb_id"],
    "Epitope": ["xref_bvbrc_epitope_id"], "AssayType": ["obi_id", "synonyms"],
    "Symptom": ["hpo_id", "synonyms"], "Condition": ["ontology_id", "condition_reported"],
    "Study": ["study_accession"], "Publication": ["pubmed_id", "doi"],
    "Grant": ["award_key", "core_project_num", "contact_pi_name"], "GrantFunding": ["project_num"],
    "Patent": ["document_id", "application_num", "family_id"],
    "Countermeasure": ["names"], "Organization": ["spellings"],
}


def write_snapshot(path, data):
    """Gzipped JSON with a fixed mtime, so an unchanged data set gives an unchanged file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
        gz.write(raw)


def read_snapshot(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def short(v):
    if hasattr(v, "iso_format"):          # neo4j.time.Date / DateTime
        v = v.iso_format()[:10]
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v)
    if isinstance(v, str) and len(v) > MAX_TEXT:
        v = v[:MAX_TEXT - 1] + "…"
    return v


def graph_data() -> dict:
    """The graph as compact JSON-ready lists, read from Neo4j."""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    labels = [l for l in LABEL_KEY]
    l_idx = {l: i for i, l in enumerate(labels)}
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
            key = LABEL_KEY[label]
            want = PROPS.get(label, [])
            extra = SEARCH.get(label, [])
            for r in s.run(f"MATCH (n:{label}) RETURN elementId(n) AS id, properties(n) AS p"):
                if label == "Feature" and r["id"] not in described:
                    continue
                p = r["p"]
                props = {k: short(p[k]) for k in want if p.get(k) not in (None, "", [])}
                aka = set()
                for k in extra:
                    v = p.get(k)
                    for x in (v if isinstance(v, list) else [v]):
                        if x not in (None, ""):
                            aka.add(str(x))
                aka.discard(p.get("name") or "")
                aka -= {x for v in props.values() for x in str(v).split("; ")}
                name = p.get("name") or str(p[key])
                if label == "Sequence" and p.get("xref_genbank_accession"):
                    name = f'{p["xref_genbank_accession"][0]} {name}'.strip()
                n_idx[r["id"]] = len(nodes)
                nodes.append([l_idx[label], short(p[key]), short(name),
                              props or 0, " | ".join(sorted(aka))])

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

    return {
        "built": date.today().isoformat(), "labels": labels, "keys": [LABEL_KEY[l] for l in labels],
        "rels": rels, "nodes": nodes, "edges": edges, "eprops": eprops,
        "viruses": [{"key": k, "label": label, "taxa": ids} for k, label, ids in VIRUSES],
        "neo4j": {"nodes": neo_counts, "relationships": neo_rels},
    }


def embed(html: str, data: dict) -> str:
    """Put the graph into a dashboard page (template or already built)."""
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    if not GRAPH_LINE.search(html):
        raise SystemExit("No `const KG = ...;` line in the page: is it built from dashboard.template.html?")
    return GRAPH_LINE.sub(lambda _: f"const KG = {blob};", html, count=1)


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

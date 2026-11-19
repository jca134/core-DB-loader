"""
The graph explorer's data: the record format shared by the dashboard (which
embeds a trimmed copy of the graph, dashboard/build_explorer.py) and the live
explorer behind `filovirus serve` at /explore (LiveExplorer below), which reads
the whole graph from Neo4j on every request, Feature nodes included.

A record is a compact list: [label index, key, display name, {shown properties}
or 0, "searchable ids | ..."]. Labels are indexed in LABEL_KEY order, so both
sources agree on what index means what.
"""

import importlib.util
import json
import re
import sys
import threading
from datetime import date

from core_data_loader.config import PROJECT_ROOT as ROOT
from core_data_loader.graph.export import LABEL_KEY
from core_data_loader.graph.templates import VIRUSES

LABELS = list(LABEL_KEY)
LABEL_INDEX = {label: i for i, label in enumerate(LABELS)}
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


def short(v):
    if hasattr(v, "iso_format"):          # neo4j.time.Date / DateTime
        v = v.iso_format()[:10]
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v)
    if isinstance(v, str) and len(v) > MAX_TEXT:
        v = v[:MAX_TEXT - 1] + "…"
    return v


def node_row(label: str, p: dict) -> list:
    """One node's record: what the explorer shows and searches."""
    key = LABEL_KEY[label]
    props = {k: short(p[k]) for k in PROPS.get(label, []) if p.get(k) not in (None, "", [])}
    aka = set()
    for k in SEARCH.get(label, []):
        v = p.get(k)
        for x in (v if isinstance(v, list) else [v]):
            if x not in (None, ""):
                aka.add(str(x))
    aka.discard(p.get("name") or "")
    aka -= {x for v in props.values() for x in str(v).split("; ")}
    name = p.get("name") or str(p[key])
    if label == "Sequence" and p.get("xref_genbank_accession"):
        name = f'{p["xref_genbank_accession"][0]} {name}'.strip()
    return [LABEL_INDEX[label], short(p[key]), short(name), props or 0, " | ".join(sorted(aka))]


def meta(neo_counts: dict, neo_rels: int) -> dict:
    """What every copy of the graph carries besides its nodes and edges."""
    return {
        "built": date.today().isoformat(), "labels": LABELS, "keys": [LABEL_KEY[l] for l in LABELS],
        "viruses": [{"key": k, "label": label, "taxa": ids} for k, label, ids in VIRUSES],
        "neo4j": {"nodes": neo_counts, "relationships": neo_rels},
    }


# --- the live explorer -----------------------------------------------------------

TEMPLATE = ROOT / "dashboard" / "dashboard.template.html"
GRAPH_LINE = re.compile(r"^(?:let|const) KG = .*$", re.M)
BATCH = 5000
# Search ranking, as in the page: records people look up by name before bulk records.
PRIORITY = {"Taxon": 0, "GeneFamily": 0, "Gene": 1, "Countermeasure": 1, "Antibody": 1, "Condition": 1,
            "Symptom": 1, "Structure": 2, "Epitope": 2, "Grant": 2, "Publication": 2, "Patent": 2,
            "Organization": 2, "AssayType": 2, "Study": 2, "GeneDomain": 3, "Isolate": 4, "Protein": 5,
            "Sequence": 5, "EpitopeAssay": 5, "GrantFunding": 5, "Feature": 5}
_BOUNDARY = set(" |(-:,/")


class LiveExplorer:
    """Search and subgraphs over the whole Neo4j graph, for the page at /explore.

    Search runs over an in-memory list of every node (element id, record, search
    text, degree), rebuilt whenever the node count changes, i.e. after each
    `filovirus export`. Subgraphs are walked in Neo4j, one step at a time."""

    def __init__(self, graph):
        self.graph = graph
        self._lock = threading.Lock()
        self._index = None      # (node count, entries, element id -> entry, taxon key -> element id)
        self._page = None

    # --- search index ---
    def _entries(self):
        count = self.graph.run("MATCH (n) RETURN count(n) AS c")[0]["c"]
        with self._lock:
            if self._index is None or self._index[0] != count:
                entries, by_id, taxa = [], {}, {}
                for label in LABELS:
                    for r in self.graph.run(f"MATCH (n:{label}) RETURN elementId(n) AS id, properties(n) AS p, "
                                            "COUNT { (n)--() } AS deg"):
                        row = node_row(label, r["p"])
                        hay = " | ".join([str(row[2]), str(row[1]), row[4]] +
                                         [str(v) for v in (row[3] or {}).values()]).lower()
                        e = (r["id"], row, hay, r["deg"])
                        entries.append(e)
                        by_id[r["id"]] = e
                        if label == "Taxon":
                            taxa[r["p"]["taxon_id"]] = r["id"]
                self._index = (count, entries, by_id, taxa)
            return self._index

    def search(self, body):
        q = str(body.get("q") or "").strip().lower()
        if not q:
            return {"results": []}
        _, entries, _, _ = self._entries()
        hits = []
        for e in entries:
            p = e[2].find(q)
            if p < 0:
                continue
            row = e[1]
            name = str(row[2]).lower()
            if name == q or str(row[1]).lower() == q or f" | {row[4].lower()} | ".find(f" | {q} | ") >= 0:
                tier = 0
            elif name.startswith(q):
                tier = 1
            elif p == 0 or e[2][p - 1] in _BOUNDARY:
                tier = 2
            else:
                tier = 3
            pri = PRIORITY.get(LABELS[row[0]], 3)
            hits.append(((40 if pri >= 4 else 0) + tier * 10 + pri, -e[3], e))
        hits.sort(key=lambda h: (h[0], h[1]))
        limit = max(1, min(int(body.get("limit") or 14), 50))
        return {"results": [{"id": e[0], "node": e[1], "deg": e[3]} for _, _, e in hits[:limit]]}

    # --- subgraph ---
    def _batched(self, cypher, ids, **params):
        ids = list(ids)
        for k in range(0, len(ids), BATCH):
            yield from self.graph.run(cypher, {"ids": ids[k:k + BATCH], **params})

    def subgraph(self, body):
        """Everything within `hops` steps of the starting records, with every link between them."""
        _, _, by_id, taxa = self._entries()
        hops = max(1, min(int(body.get("hops") or 2), 3))
        starts = [i for i in body.get("ids") or [] if i in by_id]
        starts += [taxa[t] for t in body.get("taxa") or [] if t in taxa]
        if not starts:
            return {"error": "That record isn't in the graph any more. Search for it again."}
        roots = list(dict.fromkeys(starts))
        if body.get("subtaxa"):
            rows = self._batched("UNWIND $ids AS id MATCH (t:Taxon) WHERE elementId(t) = id "
                                 "MATCH (c:Taxon)-[:CHILD_OF*1..]->(t) RETURN DISTINCT elementId(c) AS c", roots)
            roots += [r["c"] for r in rows if r["c"] not in roots]
        hop = {i: 0 for i in roots}
        frontier = roots
        for d in range(1, hops + 1):
            nxt = []
            for r in self._batched("UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id "
                                   "MATCH (n)--(m) RETURN DISTINCT elementId(m) AS m", frontier):
                if r["m"] not in hop:
                    hop[r["m"]] = d
                    nxt.append(r["m"])
            frontier = nxt
        ids = list(hop)
        index = {i: k for k, i in enumerate(ids)}
        rels, r_idx, edges, eprops = [], {}, [], {}
        for r in self._batched("UNWIND $ids AS id MATCH (a)-[e]->(b) WHERE elementId(a) = id "
                               "RETURN id AS a, type(e) AS t, elementId(b) AS b, e.confidence AS c, "
                               "e.matched_terms AS m, e.support_count AS sc", ids):
            b = index.get(r["b"])
            if b is None:
                continue
            if r["t"] not in r_idx:
                r_idx[r["t"]] = len(rels)
                rels.append(r["t"])
            if r["c"] is not None or r["m"]:
                eprops[len(edges) // 3] = [r["c"], short(r["m"]) or "", r["sc"]]
            edges += [index[r["a"]], b, r_idx[r["t"]]]
        return {
            "ids": ids, "nodes": [by_id[i][1] for i in ids], "deg": [by_id[i][3] for i in ids],
            "edges": edges, "eprops": eprops, "rels": rels,
            "starts": [index[i] for i in dict.fromkeys(starts)],
        }

    # --- the page ---
    def page(self) -> bytes:
        """The dashboard page, with the explorer switched to this server."""
        with self._lock:
            if self._page is None:
                counts = {r["l"]: r["c"] for r in self.graph.run(
                    "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c")}
                rels = self.graph.run("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
                live = {**meta(counts, rels), "live": True, "rels": [], "nodes": [], "edges": [], "eprops": {}}
                html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", _documents_json())
                blob = json.dumps(live, separators=(",", ":")).replace("</", "<\\/")
                self._page = GRAPH_LINE.sub(lambda _: f"let KG = {blob};", html, count=1).encode("utf-8")
            return self._page


def _documents_json() -> str:
    """The document tabs' data, built by dashboard/build_dashboard.py from the tracked inputs."""
    path = ROOT / "dashboard" / "build_dashboard.py"
    spec = importlib.util.spec_from_file_location("build_dashboard", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_dashboard", module)
    spec.loader.exec_module(module)
    data = module.document_data()
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")

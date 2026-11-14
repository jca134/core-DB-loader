#!/usr/bin/env python3
"""Measure how accurate the entity edges from build_entity_edges.py are.

Two checks, because only papers have an independent answer key:

  mesh    Papers. PubMed indexers tag every paper with MeSH headings and substance names
          ("Ebolavirus", "Hemorrhagic Fever, Ebola", "VP35 protein, filovirus") without
          seeing our matcher. For each entity that has a MeSH counterpart, reports
            supported  share of our paper edges that MeSH also tags  (precision proxy)
            found      share of MeSH tags our edges also have        (recall proxy)
          MeSH tags a paper's main topics, so an unsupported edge is not always wrong (a
          passing mention) and a missed tag is not always a miss (the term may only be in
          the full text). Disagreements go to mesh_disagreements.csv for a look.
          Papers PubMed has not indexed yet (no MeSH at all) are left out.

  sample  Grants. No independent labels exist, so this draws a seeded random sample of
          grant edges, --per-type from each entity type, with the sentence that produced
          each one, into review/grant_edge_review.csv. Fill the `correct` column with y/n.

  score   Reads the filled review sheet and reports precision per entity type with a 95%
          Wilson interval.

Usage:

  python check_accuracy.py mesh             # fetches MeSH once, caches output/papers/paper_mesh.csv
  python check_accuracy.py sample --per-type 20
  python check_accuracy.py score
"""

import argparse
import csv
import math
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from build_entity_edges import (
    FACETS, HERE, TAXON_NODE, load_papers, load_vocabulary, match_text, read_csv, write_csv,
)

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "federal-biomedical-tech-datasets-script"
OUT_DIR = HERE / "output" / "entity_links"
REVIEW_PATH = HERE / "review" / "grant_edge_review.csv"
MESH_CACHE = HERE / "output" / "papers" / "paper_mesh.csv"

FILO = r"(ebola|marburg|filovir)"
# (entity_type, entity id or genus id) -> pattern over "descriptors | substances".
# Only entities with an unambiguous MeSH counterpart are checked. Indexers often tag a
# filovirus protein with the generic descriptor ("Viral Envelope Proteins") rather than the
# specific substance; every paper here is a filovirus paper, so a generic descriptor that
# only one filovirus protein fits counts too (envelope -> GP, matrix -> VP40, nucleoprotein
# -> NP, RdRp -> L). VP35/VP30/VP24 share "Viral Regulatory and Accessory Proteins", so
# they need the specific substance.
MESH_MAP = {
    ("taxon", "3044781"): r"^(Ebolavirus|Hemorrhagic Fever, Ebola|Ebola Vaccines)$",
    ("taxon", "3044783"): r"^(Marburgvirus|Marburg Virus Disease)$",
    ("taxon", "11266"): r"^(Filoviridae|Filoviridae Infections)$",
    ("gene", "GP"): rf"^(((envelope|transmembrane) glycoprotein|GP protein|glycoprotein),? .*{FILO}|Viral Envelope Proteins)$",
    ("gene", "sGP"): rf"^(secreted glycoprotein|sGP).*{FILO}",
    ("gene", "NP"): rf"^((nucleocapsid protein|nucleoprotein|NP protein), .*{FILO}|Nucleoproteins)$",
    ("gene", "VP35"): r"VP35",
    ("gene", "VP40"): r"VP40|^Viral Matrix Proteins$",
    ("gene", "VP24"): r"VP24",
    ("gene", "VP30"): r"VP30",
    ("gene", "L"): rf"^((L protein|polymerase L).*{FILO}|RNA-Dependent RNA Polymerase)$",
    ("condition", "DOID:4325"): r"^Hemorrhagic Fever, Ebola$",
    ("condition", "DOID:4327"): r"^Marburg Virus Disease$",
    ("condition", "MONDO:0018087"): r"^Hemorrhagic Fevers, Viral$",
    ("symptom", "HP:0001945"): r"^Fever$",
    ("symptom", "HP:0001892"): r"^Hemorrhage$",
    ("symptom", "HP:0002014"): r"^Diarrhea$",
    ("symptom", "HP:0002013"): r"^Vomiting$",
    ("symptom", "HP:0002315"): r"^Headache$",
    ("symptom", "HP:0005268"): r"^Abortion, Spontaneous$",
    ("assay_type", "OBI:0000661"): r"^Enzyme-Linked Immunosorbent Assay$",
    ("assay_type", "OBI:0002692"): r"Polymerase Chain Reaction$",
    ("assay_type", "OBI:0003796"): r"^Neutralization Tests$",
    ("assay_type", "OBI:0600031"): r"^Enzyme-Linked Immunospot Assay$",
    ("assay_type", "OBI:0001463"): r"^(Oligonucleotide Array Sequence Analysis|Gene Expression Profiling)$",
}
MESH_RE = {k: re.compile(v, re.IGNORECASE) for k, v in MESH_MAP.items()}

REVIEW_COLUMNS = ["correct", "source_type", "source_id", "entity_type", "entity_name", "entity_id",
                  "matched_keyword", "match_field", "confidence", "context", "title", "url", "note"]


def check_key(edge):
    """Edge -> MESH_MAP key. Taxa are checked at genus (family stays family)."""
    if edge["matched_entity_type"] == "taxon":
        return ("taxon", TAXON_NODE[edge["matched_entity_id"]][2])
    return (edge["matched_entity_type"], edge["matched_entity_id"])


# --- mesh ---------------------------------------------------------------------------------

def fetch_mesh(pmids):
    rows = []
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i + 200]
        data = urllib.parse.urlencode({"db": "pubmed", "retmode": "xml", "id": ",".join(batch), "tool": TOOL}).encode()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(EFETCH, data=data), timeout=120) as r:
                    root = ET.fromstring(r.read())
                break
            except Exception as e:  # network or parse; retry, then give up on the batch
                if attempt == 2:
                    print(f"  batch {i}: failed ({e})")
                    root = None
                time.sleep(2 * (attempt + 1))
        for art in (root.findall("PubmedArticle") if root is not None else []):
            rows.append({
                "pmid": art.findtext("MedlineCitation/PMID", ""),
                "mesh": "|".join(d.text or "" for d in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName")),
                "substances": "|".join(s.text or "" for s in art.findall(".//ChemicalList/Chemical/NameOfSubstance")),
            })
        print(f"  MeSH {min(i + 200, len(pmids))}/{len(pmids)}", flush=True)
        time.sleep(0.4)
    return rows


def run_mesh(args):
    edges = read_csv(OUT_DIR / "paper_entity_edges.csv")
    papers = load_papers(HERE / "output" / "papers")
    cached = {r["pmid"]: r for r in read_csv(MESH_CACHE)} if MESH_CACHE.exists() else {}
    todo = sorted(set(papers) - set(cached), key=int)
    if todo:
        print(f"Fetching MeSH for {len(todo)} papers")
        for r in fetch_mesh(todo):
            cached[r["pmid"]] = r
        write_csv(MESH_CACHE, sorted(cached.values(), key=lambda r: int(r["pmid"])), ["pmid", "mesh", "substances"])

    indexed = {pmid for pmid, r in cached.items() if r["mesh"] and pmid in papers}
    tags = defaultdict(set)      # pmid -> MESH_MAP keys MeSH supports
    for pmid in indexed:
        terms = [t for t in (cached[pmid]["mesh"] + "|" + cached[pmid]["substances"]).split("|") if t]
        for key, pattern in MESH_RE.items():
            if any(pattern.search(t) for t in terms):
                tags[pmid].add(key)
    ours = defaultdict(set)
    names = {}
    for e in edges:
        key = check_key(e)
        if e["source_id"] in indexed and key in MESH_RE:
            ours[e["source_id"]].add(key)
            names.setdefault(key, e["entity_name"] if e["matched_entity_type"] != "taxon"
                             else {"3044781": "Ebola viruses", "3044783": "Marburg viruses", "11266": "Filoviruses"}[key[1]])
    for key in MESH_RE:
        names.setdefault(key, key[1])

    per = defaultdict(lambda: Counter())
    disagreements = []
    for pmid in indexed:
        for key in ours[pmid] | tags[pmid]:
            c = per[key]
            if key in ours[pmid] and key in tags[pmid]:
                c["both"] += 1
            elif key in ours[pmid]:
                c["ours_only"] += 1
                disagreements.append({"pmid": pmid, "entity_type": key[0], "entity": names[key], "kind": "edge, no MeSH tag",
                                      "title": papers[pmid]["title"]})
            else:
                c["mesh_only"] += 1
                disagreements.append({"pmid": pmid, "entity_type": key[0], "entity": names[key], "kind": "MeSH tag, no edge",
                                      "title": papers[pmid]["title"]})

    lines = [f"MeSH agreement for paper edges: {len(indexed)} of {len(papers)} papers are MeSH-indexed.",
             "supported = our edges MeSH also tags (precision proxy); found = MeSH tags we also have (recall proxy).", ""]
    header = f"{'entity':<34}{'edges':>7}{'supported':>11}{'tags':>7}{'found':>8}"
    for facet in FACETS:
        keys = [k for k in MESH_RE if k[0] == facet]
        tot = Counter()
        rows = []
        for k in keys:
            c = per[k]
            tot.update(c)
            rows.append(fmt_row(names[k], c))
        lines += [facet.upper(), header] + rows + [fmt_row("  all " + facet, tot), ""]
    report = "\n".join(lines)
    (OUT_DIR / "accuracy_mesh.txt").write_text(report + "\n", encoding="utf-8")
    write_csv(OUT_DIR / "mesh_disagreements.csv", sorted(disagreements, key=lambda r: (r["entity_type"], r["entity"], r["kind"])),
              ["pmid", "entity_type", "entity", "kind", "title"])
    print(report)
    print(f"Wrote accuracy_mesh.txt and mesh_disagreements.csv ({len(disagreements)} rows) to {OUT_DIR.relative_to(HERE)}")


def fmt_row(name, c):
    edges, tags = c["both"] + c["ours_only"], c["both"] + c["mesh_only"]
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "-"
    return f"{name:<34}{edges:>7}{pct(c['both'], edges):>11}{tags:>7}{pct(c['both'], tags):>8}"


# --- sample / score -------------------------------------------------------------------------

def context(text, start, length, width=110):
    a, b = max(0, start - width), min(len(text), start + length + width)
    snippet = text[a:start] + "[[" + text[start:start + length] + "]]" + text[start + length:b]
    return ("…" if a else "") + " ".join(snippet.split()) + ("…" if b < len(text) else "")


def run_sample(args):
    if REVIEW_PATH.exists() and not args.force:
        sys.exit(f"{REVIEW_PATH.relative_to(HERE)} exists and may hold your labels; pass --force to overwrite it.")
    targets, _ = load_vocabulary(HERE / "Context" / "Context" / "kg_keywords.csv", HERE / "inputs" / "kg_clinical_terms.csv")
    edges = read_csv(OUT_DIR / "grant_entity_edges.csv")
    texts = defaultdict(lambda: {"title": [], "abstract": []})
    for r in read_csv(HERE / "output" / "grants" / "staging" / "stg_grant_funding.csv"):
        for field, col in (("title", "project_title"), ("abstract", "abstract_text")):
            if r[col] and r[col] not in texts[r["award_key"]][field]:
                texts[r["award_key"]][field].append(r[col])

    rng = random.Random(args.seed)
    rows = []
    for facet in FACETS:
        pool = [e for e in edges if e["matched_entity_type"] == facet]
        for e in rng.sample(pool, min(args.per_type, len(pool))):
            ctx = ""
            for field in ("title", "abstract"):
                for text in texts[e["source_id"]][field]:
                    hit = next((h for h in match_text(text, field, targets)
                                if h["entity_type"] == facet and h["entity_id"] == e["matched_entity_id"]), None)
                    if hit:
                        ctx = context(text, hit["start"], len(hit["matched"]))
                        break
                if ctx:
                    break
            rows.append({"correct": "", "source_type": "grant", "source_id": e["source_id"], "entity_type": facet,
                         "entity_name": e["entity_name"], "entity_id": e["matched_entity_id"],
                         "matched_keyword": e["matched_keyword"], "match_field": e["match_field"],
                         "confidence": e["confidence"], "context": ctx, "title": e["title"], "url": e["url"], "note": ""})
    REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_csv(REVIEW_PATH, rows, REVIEW_COLUMNS)
    print(f"Wrote {len(rows)} grant edges to {REVIEW_PATH.relative_to(HERE)} "
          f"({', '.join(f'{f} {sum(r['entity_type'] == f for r in rows)}' for f in FACETS)}).")
    print("Mark `correct` y if the [[bracketed]] text really refers to that entity, n if not, then run: "
          "python check_accuracy.py score")


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def run_score(args):
    rows = read_csv(REVIEW_PATH)
    labelled = [r for r in rows if r["correct"].strip().lower()[:1] in ("y", "n")]
    if not labelled:
        sys.exit(f"No rows labelled yet in {REVIEW_PATH.relative_to(HERE)} (put y or n in `correct`).")
    lines = [f"Grant edge precision from manual review: {len(labelled)} of {len(rows)} sampled edges labelled.", "",
             f"{'entity type':<14}{'labelled':>9}{'correct':>9}{'precision':>11}   95% CI"]
    groups = defaultdict(list)
    for r in labelled:
        groups[r["entity_type"]].append(r["correct"].strip().lower().startswith("y"))
    for facet in FACETS + ["all"]:
        vals = [v for g in groups.values() for v in g] if facet == "all" else groups.get(facet, [])
        if not vals:
            continue
        k, n = sum(vals), len(vals)
        lo, hi = wilson(k, n)
        lines.append(f"{facet:<14}{n:>9}{k:>9}{100 * k / n:>10.0f}%   {100 * lo:.0f}–{100 * hi:.0f}%")
    lines += ["", "Per-type samples are equal-sized, so 'all' is an unweighted average across types,",
              "not the precision of the full edge set (taxon edges dominate the real counts)."]
    wrong = [r for r in labelled if r["correct"].strip().lower().startswith("n")]
    if wrong:
        lines += ["", "Marked wrong:"] + [f"  {r['entity_type']}: {r['entity_name']} via '{r['matched_keyword']}' in {r['source_id']}"
                                          + (f" ({r['note']})" if r["note"] else "") for r in wrong]
    report = "\n".join(lines)
    (OUT_DIR / "accuracy_grants.txt").write_text(report + "\n", encoding="utf-8")
    print(report)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mesh", help="paper edges vs PubMed MeSH (automatic)")
    s = sub.add_parser("sample", help="draw grant edges for manual review")
    s.add_argument("--per-type", type=int, default=20)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--force", action="store_true", help="overwrite an existing review sheet")
    sub.add_parser("score", help="precision from the filled review sheet")
    return parser.parse_args()


def main():
    args = parse_args()
    {"mesh": run_mesh, "sample": run_sample, "score": run_score}[args.cmd](args)


if __name__ == "__main__":
    main()

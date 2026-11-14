#!/usr/bin/env python3
"""Link grants and papers to upper-level KG entities, then compare each grant to its papers.

Upper-level entities are the clinical KG's factual layer: filoviruses (taxon), proteins
(gene), conditions, symptoms and assay types. The vocabulary is the KG's own export:

  Context/Context/kg_keywords.csv   taxon, gene, protein (antibody rows are not used here)
  inputs/kg_clinical_terms.csv      symptom (HPO), assay_type (OBI), condition (DOID/MONDO)

Matching follows core-DB-loader's common/kg_keywords.py so these edges line up with
core.evidence_link: protein labels resolve to the 9 gene-level nodes, bare "Ebola" /
"Marburg" / "filovirus" resolve to genus / family, and a title hit outranks an abstract
hit. It runs without the database, so IDs are the stable external ones (NCBI taxon,
gene symbol, HPO, OBI, DOID/MONDO), never core surrogate keys. On top of that:

  Longest span wins.  "Ebola hemorrhagic fever" is a condition, not the symptom "fever";
                      "polymerase chain reaction" is an assay, not the L polymerase.
                      Taxon names may overlap a condition name ("Ebola" in "Ebola virus
                      disease") because the two are different facts.
  Context.            Generic protein words (GP, NP, L, N, glycoprotein, polymerase, ...)
                      count only when a filovirus name is in the same sentence. Short
                      ambiguous acronyms (EVD, PCR, ICS, ...) must match in upper case.
  One taxon node.     Legacy and virus-name taxon IDs collapse to the current ICTV species
                      or genus, so "Ebola virus" and "Zaire ebolavirus" are one entity.

Papers are the papers workstream's set plus the filovirus papers RePORTER lists under a
grant (fetch_grant_publications.py). Divergence. For each grant -> paper pair where the
paper acknowledges the grant (in PubMed, or in RePORTER's publication list), the
grant's entity set (title + abstract, every fiscal year) is compared with the paper's
(title + abstract), facet by facet. Taxa are compared at genus level (Ebola vs Marburg vs
filovirus-wide), since a paper naming the species of an Ebola grant has not changed topic.

Outputs, written to --out-dir (default output/entity_links):

  grant_entity_edges.csv     one row per (grant, entity), shared evidence format + extras
  paper_entity_edges.csv     one row per (paper, entity), same columns
  grant_paper_divergence.csv one row per acknowledged (grant, paper) pair
  divergence_entity_flow.csv one row per (pair, entity): shared / grant_only / paper_only
  summary.txt

Usage:

  python build_entity_edges.py
  python build_entity_edges.py --all-links    # also compare PI-authored, non-acknowledged pairs
"""

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

EDGE_COLUMNS = [
    "evidence_id", "source_type", "title", "date", "url", "matched_entity_type",
    "matched_entity_id", "matched_keyword", "match_method", "relation",
    "source_id", "entity_name", "match_field", "confidence", "requires_context",
]
PAIR_COLUMNS = [
    "award_key", "pmid", "grant_title", "paper_title", "paper_year", "link_basis",
    "grant_relevance_tier", "pattern", "core_pattern", "jaccard", "n_shared", "n_grant_only", "n_paper_only",
    "shared", "grant_only", "paper_only",
    "taxon_pattern", "gene_pattern", "condition_pattern", "symptom_pattern", "assay_type_pattern",
]
FLOW_COLUMNS = ["award_key", "pmid", "entity_type", "entity_id", "entity_name", "side"]

FACETS = ["taxon", "gene", "condition", "symptom", "assay_type"]

# Same as core-DB-loader common/kg_keywords.py.
CONFIDENCE = {"title": 0.70, "abstract": 0.50}
AMBIGUOUS_PENALTY = 0.5
SUPPLEMENTAL_TERMS = [
    ("Ebola", "taxon", "186536", "common_name"),
    ("Marburg", "taxon", "186537", "common_name"),
    ("filovirus", "taxon", "11266", "common_name"),
    ("filoviruses", "taxon", "11266", "common_name"),
    ("filoviral", "taxon", "11266", "common_name"),
    ("Filoviridae", "taxon", "11266", "db_name"),
]

# NCBI taxon id -> (current ICTV node id, name, genus id). Legacy species and virus-name
# nodes collapse onto the species they retrieve evidence for (see kg_keywords_README.md).
EBOLA_GENUS, MARBURG_GENUS, FAMILY = "3044781", "3044783", "11266"
TAXON_NODE = {}
for ids, node, name, genus in [
    (("1570291", "186538", "3052462"), "3052462", "Orthoebolavirus zairense", EBOLA_GENUS),
    (("186540", "3052460"), "3052460", "Orthoebolavirus sudanense", EBOLA_GENUS),
    (("186539", "3052459"), "3052459", "Orthoebolavirus restonense", EBOLA_GENUS),
    (("565995", "3052458"), "3052458", "Orthoebolavirus bundibugyoense", EBOLA_GENUS),
    (("3052461",), "3052461", "Orthoebolavirus taiense", EBOLA_GENUS),
    (("2010960", "3052457"), "3052457", "Orthoebolavirus bombaliense", EBOLA_GENUS),
    (("11269", "3052505"), "3052505", "Orthomarburgvirus marburgense", MARBURG_GENUS),
    (("186536", "3044781"), EBOLA_GENUS, "Orthoebolavirus", EBOLA_GENUS),
    (("186537", "3044783"), MARBURG_GENUS, "Orthomarburgvirus", MARBURG_GENUS),
    (("11266",), FAMILY, "Filoviridae", FAMILY),
]:
    for raw in ids:
        TAXON_NODE[raw] = (node, name, genus)
GENUS_NAME = {EBOLA_GENUS: "Orthoebolavirus", MARBURG_GENUS: "Orthomarburgvirus", FAMILY: "Filoviridae"}

# Protein labels that also name other viruses' proteins (audit issue #1). Filovirus-specific
# labels (VP24/30/35/40, sGP, ssGP, "viral protein 40", ...) need no context.
GENERIC_PROTEIN_RE = re.compile(
    r"^(gp|np|n|l|glycoprotein|envelope glycoprotein|secreted glycoprotein|polymerase|"
    r"nucleoprotein|nucleocapsid protein|rna-dependent rna polymerase|rna-directed rna polymerase)$",
    re.IGNORECASE,
)
# A lone "L" or "N" is a gene only when it is written as one ("L protein", "N gene").
SINGLE_LETTER_FOLLOW = r"(?=[\s-]+(?:protein|gene|polymerase|mRNA|ORF)\b)"
# ...and the filovirus name must be this close (characters, ~10 words), not just in the
# sentence: "the HPIV3 polymerase" in a sentence about Ebola GP is not the L protein.
FILOVIRUS_WINDOW = 80
# Clinical terms that are ordinary words in other senses.
CONTEXT_REQUIRED = {"tetramer": re.compile(r"\b(MHC|HLA)\b")}
# "fever" in a disease name (Lassa fever, viral hemorrhagic fever) is not the symptom.
DISEASE_FEVER_RE = re.compile(
    r"(hemorrhagic|haemorrhagic|lassa|yellow|dengue|scarlet|typhoid|rheumatic|valley|"
    r"crimean-congo|q|marburg|ebola)[\s-]+$", re.IGNORECASE,
)

SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
FIELD_RANK = {"title": 0, "abstract": 1}


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def term_pattern(term, case_sensitive):
    """Whole-token match; a hyphen separates tokens (kg_keywords.py's rule)."""
    flags = 0 if case_sensitive else re.IGNORECASE
    follow = SINGLE_LETTER_FOLLOW if len(term) == 1 else ""
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z]){follow}", flags)


def load_vocabulary(keywords_path, clinical_path):
    """One target per (search term, resolved entity). Gene/protein labels -> gene symbol."""
    keywords = [r for r in read_csv(keywords_path) if r["scope"] == "include"]
    gene_symbols = {r["search_term"].lower(): r["search_term"]
                    for r in keywords if r["entity_type"] == "gene"}
    # canonical protein label -> gene symbol, through the db_gene_label sibling row.
    gene_by_canonical = {}
    for r in keywords:
        if r["term_type"] == "db_gene_label" and r["search_term"].lower() in gene_symbols:
            gene_by_canonical.setdefault(r["canonical_name"].lower(), gene_symbols[r["search_term"].lower()])

    targets = {}

    def add(term, entity_type, entity_id, name, term_type, needs_context):
        # Ambiguous acronyms and single letters match in the case the KG writes them.
        case_sensitive = needs_context and (len(term) <= 4) and term.isupper() or len(term) == 1
        generic = entity_type == "gene" and bool(GENERIC_PROTEIN_RE.match(term))
        key = (term.lower() if not case_sensitive else term, entity_type, entity_id)
        if key in targets:
            return
        targets[key] = {
            "term": term, "entity_type": entity_type, "entity_id": entity_id, "entity_name": name,
            "term_type": term_type, "requires_context": needs_context or generic,
            "needs_filovirus": generic, "pattern": term_pattern(term, case_sensitive),
        }

    for r in keywords:
        needs_context = r["requires_context"].lower() == "true"
        if r["entity_type"] == "taxon":
            for raw in r["entity_ids"].split("|"):
                if raw in TAXON_NODE:
                    node, name, _ = TAXON_NODE[raw]
                    add(r["search_term"], "taxon", node, name, r["term_type"], needs_context)
        elif r["entity_type"] in ("gene", "protein"):
            symbol = gene_symbols.get(r["search_term"].lower()) or gene_by_canonical.get(r["canonical_name"].lower())
            if symbol:
                add(r["search_term"], "gene", symbol, symbol, r["term_type"], needs_context)
    for term, entity_type, entity_id, term_type in SUPPLEMENTAL_TERMS:
        node, name, _ = TAXON_NODE[entity_id]
        add(term, entity_type, node, name, term_type, False)

    no_id = set()
    for r in read_csv(clinical_path):
        if r["scope"] != "include":
            continue
        # Entities without an ontology id keep a readable placeholder so they still count.
        entity_id = r["entity_ids"] or f"unmapped:{r['canonical_name']}"
        if not r["entity_ids"]:
            no_id.add(r["canonical_name"])
        add(r["search_term"], r["entity_type"], entity_id, r["canonical_name"], r["term_type"],
            r["requires_context"].lower() == "true")
    return list(targets.values()), sorted(no_id)


def match_method(term_type):
    if term_type == "db_gene_label":
        return "gene_label"
    if term_type.endswith("alias") or term_type in ("synonym", "common_or_legacy_name", "common_name"):
        return "synonym"
    return "keyword"


def match_text(text, field, targets):
    """Accepted hits in one field: longest non-overlapping spans, context rules applied."""
    if not text:
        return []
    candidates = []
    for t in targets:
        for m in t["pattern"].finditer(text):
            candidates.append((m.start(), m.end(), t))
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
    accepted, spans = [], []
    for start, end, t in candidates:
        overlaps = [s for s in spans if s[0] < end and start < s[1]]
        # Same span claimed by two entities of one term (e.g. GP vs sGP labels) -> keep both;
        # a taxon may sit inside a condition name; anything else inside a longer span is dropped.
        if any(not ((s[0], s[1]) == (start, end) or {s[2], t["entity_type"]} == {"taxon", "condition"})
               for s in overlaps):
            continue
        spans.append((start, end, t["entity_type"]))
        accepted.append((start, end, t))

    sentences = [(m.start(), m.end()) for m in re.finditer(r"[^.!?;\n]+[.!?;]?", text)]
    taxon_spans = [(s, e) for s, e, t in accepted if t["entity_type"] == "taxon"]

    def sentence_of(pos):
        return next(((a, b) for a, b in sentences if a <= pos < b), (0, len(text)))

    hits = []
    for start, end, t in accepted:
        a, b = sentence_of(start)
        if t["needs_filovirus"] and not any(
                a <= s < b and (abs(s - start) <= FILOVIRUS_WINDOW or abs(e - end) <= FILOVIRUS_WINDOW)
                for s, e in taxon_spans):
            continue
        if t["entity_name"] == "Fever" and DISEASE_FEVER_RE.search(text[max(0, start - 20):start]):
            continue
        rule = CONTEXT_REQUIRED.get(t["term"].lower())
        if rule and not rule.search(text[a:b]):
            continue
        confidence = CONFIDENCE[field] * (AMBIGUOUS_PENALTY if t["requires_context"] else 1)
        hits.append({**t, "match_field": field, "confidence": round(confidence, 2),
                     "matched": text[start:end], "start": start})
    return hits


def collapse(source_type, source_id, title, date, url, hits):
    """One edge per entity: strongest field, every distinct keyword that produced it."""
    by_entity = defaultdict(list)
    for h in hits:
        by_entity[(h["entity_type"], h["entity_id"])].append(h)
    edges = []
    for (entity_type, entity_id), group in sorted(by_entity.items()):
        best = max(group, key=lambda h: (h["confidence"], -FIELD_RANK[h["match_field"]]))
        keywords = sorted({h["term"] for h in group}, key=str.lower)
        edges.append({
            "evidence_id": f"{source_type}:{source_id}:{entity_type}:{entity_id}",
            "source_type": source_type, "title": title, "date": date, "url": url,
            "matched_entity_type": entity_type, "matched_entity_id": entity_id,
            "matched_keyword": ";".join(keywords), "match_method": match_method(best["term_type"]),
            "relation": "related", "source_id": source_id, "entity_name": best["entity_name"],
            "match_field": best["match_field"], "confidence": best["confidence"],
            "requires_context": str(all(h["requires_context"] for h in group)).lower(),
        })
    return edges


def grant_edges(funding_path, award_path, targets):
    awards = {r["award_key"]: r for r in read_csv(award_path)}
    records = defaultdict(list)
    for r in read_csv(funding_path):
        records[r["award_key"]].append(r)
    edges = []
    for award_key, rows in sorted(records.items()):
        rows.sort(key=lambda r: (r["fiscal_year"], r["appl_id"]))
        hits = []
        for field, column in (("title", "project_title"), ("abstract", "abstract_text")):
            for text in dict.fromkeys(r[column] for r in rows if r[column]):
                hits += match_text(text, field, targets)
        award = awards.get(award_key, {})
        edges += collapse("grant", award_key, award.get("project_title") or rows[-1]["project_title"],
                          award.get("project_start_date") or rows[0]["award_notice_date"] or rows[0]["fiscal_year"],
                          rows[0]["project_detail_url"], hits)
    return edges


def load_papers(papers_dir):
    """pmid -> paper: the papers workstream's set plus the filovirus papers grants cite."""
    abstracts = {r["pmid"]: r["abstract"] for r in read_csv(papers_dir / "paper_abstracts.csv")}
    papers = {}
    cited = papers_dir / "cited_papers.csv"
    for p in (read_csv(cited) if cited.exists() else []):
        if p["is_filovirus"] == "true":
            papers[p["pmid"]] = {"pmid": p["pmid"], "title": p["title"], "abstract": p["abstract"],
                                 "date": p["pub_date"] or p["year"], "year": p["year"], "journal": p["journal"]}
    for p in read_csv(papers_dir / "filovirus_papers.csv"):
        papers[p["pmid"]] = {"pmid": p["pmid"], "title": p["title"], "date": p["pub_date"] or p["year"],
                             "abstract": abstracts.get(p["pmid"]) or papers.get(p["pmid"], {}).get("abstract", ""),
                             "year": p["year"], "journal": p["journal"]}
    return papers


def paper_edges(papers, targets):
    edges = []
    for pmid, p in sorted(papers.items(), key=lambda kv: int(kv[0])):
        hits = match_text(p["title"], "title", targets) + match_text(p["abstract"], "abstract", targets)
        edges += collapse("paper", pmid, p["title"], p["date"], f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", hits)
    return edges


def comparison_keys(edges):
    """source_id -> {(facet, id): name}. Taxa compared at genus level."""
    keys = defaultdict(dict)
    for e in edges:
        entity_type, entity_id = e["matched_entity_type"], e["matched_entity_id"]
        if entity_type == "taxon":
            genus = TAXON_NODE[entity_id][2]
            keys[e["source_id"]][("taxon", genus)] = GENUS_NAME[genus]
        else:
            keys[e["source_id"]][(entity_type, entity_id)] = e["entity_name"]
    # "Filovirus" beside a named genus is framing, not a third topic; it counts only alone.
    for k in keys.values():
        if ("taxon", EBOLA_GENUS) in k or ("taxon", MARBURG_GENUS) in k:
            k.pop(("taxon", FAMILY), None)
    return keys


def pattern(grant, paper):
    if not grant and not paper:
        return "none"
    if not paper:
        return "paper_has_none"
    if not grant:
        return "grant_has_none"
    if grant == paper:
        return "same"
    if paper < grant:
        return "narrowed"
    if paper > grant:
        return "expanded"
    return "shifted" if grant & paper else "disjoint"


def candidate_pairs(links_path, reporter_path, award_path, papers, all_links):
    """(award_key, pmid) -> pair facts, from PubMed acknowledgments and RePORTER's publication list.

    link_basis records every route: 'pubmed_ack' (the paper's PubMed grant list names the
    award), 'reporter' (RePORTER lists the paper under the grant), and, with --all-links,
    'pi_author' (a PI wrote it and it is on topic, but neither source ties it to the grant).
    """
    awards = {r["award_key"]: r for r in read_csv(award_path)}
    by_core = defaultdict(list)
    for a in awards.values():
        by_core[a["core_project_num"]].append(a["award_key"])
    pairs = {}

    def add(award_key, pmid, basis):
        if pmid not in papers or award_key not in awards:
            return
        pair = pairs.setdefault((award_key, pmid), {"award_key": award_key, "pmid": pmid, "basis": set()})
        pair["basis"].add(basis)

    for r in read_csv(links_path):
        if "acknowledged" in r["link_basis"]:
            add(r["award_key"], r["pmid"], "pubmed_ack")
        elif all_links and r["link_basis"]:
            add(r["award_key"], r["pmid"], "pi_author")
    if reporter_path.exists():
        for r in read_csv(reporter_path):
            for award_key in by_core.get(r["core_project_num"], ()):
                add(award_key, r["pmid"], "reporter")
    rows = []
    for (award_key, pmid), pair in sorted(pairs.items()):
        a, p = awards[award_key], papers[pmid]
        rows.append({"award_key": award_key, "pmid": pmid, "grant_title": a["project_title"],
                     "paper_title": p["title"], "paper_year": p["year"],
                     "link_basis": "+".join(sorted(pair["basis"])),
                     "grant_relevance_tier": a["relevance_tier"]})
    return rows


def compare(candidates, grant_keys, paper_keys):
    pairs, flow = [], []
    for r in candidates:
        g, p = grant_keys.get(r["award_key"], {}), paper_keys.get(r["pmid"], {})
        gs, ps = set(g), set(p)
        names = {**g, **p}
        label = lambda ks: "; ".join(sorted(f"{k[0]}:{names[k]}" for k in ks))
        union = gs | ps
        row = {
            **r, "pattern": pattern(gs, ps),
            "jaccard": round(len(gs & ps) / len(union), 3) if union else "",
            "n_shared": len(gs & ps), "n_grant_only": len(gs - ps), "n_paper_only": len(ps - gs),
            "shared": label(gs & ps), "grant_only": label(gs - ps), "paper_only": label(ps - gs),
        }
        core = {"taxon", "gene"}
        row["core_pattern"] = pattern({k for k in gs if k[0] in core}, {k for k in ps if k[0] in core})
        for facet in FACETS:
            row[f"{facet}_pattern"] = pattern({k for k in gs if k[0] == facet}, {k for k in ps if k[0] == facet})
        pairs.append(row)
        for side, ks in (("shared", gs & ps), ("grant_only", gs - ps), ("paper_only", ps - gs)):
            for k in sorted(ks):
                flow.append({"award_key": r["award_key"], "pmid": r["pmid"], "entity_type": k[0],
                             "entity_id": k[1], "entity_name": names[k], "side": side})
    return pairs, flow


def summarize(path, grants, papers, pairs, flow, no_id):
    lines = []
    for label, edges in (("Grant", grants), ("Paper", papers)):
        sources = {e["source_id"] for e in edges}
        lines.append(f"{label} -> entity edges: {len(edges)} across {len(sources)} {label.lower()}s")
        by_type = Counter(e["matched_entity_type"] for e in edges)
        for t in FACETS:
            lines.append(f"  {t:<11} {by_type[t]:>6} edges  "
                         f"{len({e['matched_entity_id'] for e in edges if e['matched_entity_type'] == t}):>3} entities")
    lines.append("")
    lines.append(f"Grant -> paper pairs compared: {len(pairs)} "
                 f"({len({p['award_key'] for p in pairs})} grants, {len({p['pmid'] for p in pairs})} papers)")
    for name, n in Counter(p["pattern"] for p in pairs).most_common():
        lines.append(f"  {name:<15} {n:>4}")
    lines.append("  core (filovirus + protein only): " + ", ".join(
        f"{k} {v}" for k, v in Counter(p["core_pattern"] for p in pairs).most_common()))
    lines.append("  by link basis: " + ", ".join(f"{k} {v}" for k, v in Counter(p["link_basis"] for p in pairs).most_common()))
    lines.append("")
    lines.append("By facet (pairs where both sides name that facet are the informative ones):")
    for facet in FACETS:
        c = Counter(p[f"{facet}_pattern"] for p in pairs)
        lines.append(f"  {facet:<11} " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    lines.append("")
    lines.append("Entities most often in the paper but not the grant:")
    for (t, n), c in Counter((f["entity_type"], f["entity_name"]) for f in flow if f["side"] == "paper_only").most_common(12):
        lines.append(f"  {c:>4}  {t}: {n}")
    lines.append("Entities most often in the grant but not the paper:")
    for (t, n), c in Counter((f["entity_type"], f["entity_name"]) for f in flow if f["side"] == "grant_only").most_common(12):
        lines.append(f"  {c:>4}  {t}: {n}")
    if no_id:
        lines.append("")
        lines.append(f"Matched but no ontology id yet (entity_id 'unmapped:<name>'): {', '.join(no_id)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keywords", type=Path, default=HERE / "Context" / "Context" / "kg_keywords.csv")
    parser.add_argument("--clinical", type=Path, default=HERE / "inputs" / "kg_clinical_terms.csv")
    parser.add_argument("--staging-dir", type=Path, default=HERE / "output" / "grants" / "staging")
    parser.add_argument("--papers-dir", type=Path, default=HERE / "output" / "papers")
    parser.add_argument("--links", type=Path, default=HERE / "output" / "links" / "grant_paper_link.csv")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output" / "entity_links")
    parser.add_argument("--all-links", action="store_true",
                        help="compare every relevant grant-paper pair, not only acknowledged ones")
    return parser.parse_args()


def main():
    args = parse_args()
    targets, no_id = load_vocabulary(args.keywords, args.clinical)
    grants = grant_edges(args.staging_dir / "stg_grant_funding.csv", args.staging_dir / "stg_grant_award.csv", targets)
    paper_set = load_papers(args.papers_dir)
    papers = paper_edges(paper_set, targets)
    candidates = candidate_pairs(args.links, args.papers_dir / "reporter_grant_publications.csv",
                                 args.staging_dir / "stg_grant_award.csv", paper_set, args.all_links)
    pairs, flow = compare(candidates, comparison_keys(grants), comparison_keys(papers))
    write_csv(args.out_dir / "grant_entity_edges.csv", grants, EDGE_COLUMNS)
    write_csv(args.out_dir / "paper_entity_edges.csv", papers, EDGE_COLUMNS)
    write_csv(args.out_dir / "grant_paper_divergence.csv", pairs, PAIR_COLUMNS)
    write_csv(args.out_dir / "divergence_entity_flow.csv", flow, FLOW_COLUMNS)
    summarize(args.out_dir / "summary.txt", grants, papers, pairs, flow, no_id)


if __name__ == "__main__":
    main()

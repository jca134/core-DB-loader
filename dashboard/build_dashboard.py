#!/usr/bin/env python3
"""Build the research dashboard (dashboard/output/research_dashboard.html).

One page. The first tab, "Explore the graph", searches the whole knowledge graph
and exports the subgraph around any record; its data comes from Neo4j through
build_explorer.py (see there). The other tabs cover each document collection
(grants, papers, patents) and each pair of them. Their inputs:

  data/      the shared source folder (docs/data-sources.md; FILOVIRUS_DATA_DIR
             overrides it): RePORTER publication lists, the PubMed records of
             grant-cited and patent-cited papers, the papers bundle, the patent
             files, and the fetched edge files (patent-details, paper-details,
             paper-citations)
  federal-biomedical-tech-datasets-script/output/
             what only the grants workstream writes: the award staging tables,
             each grant's and paper's virus/protein matches, the topic
             comparison of each grant-paper pair, and the PI-authored links
  patent_pipeline/data/processed/
             the patent pipeline's review of its virus/protein matches and the
             expanded inventor list

The rules for which papers count and how grant numbers resolve are imported
from the loader (core_data_loader.etl.evidence), so the dashboard and the
knowledge graph agree. Virus and protein names on papers no workstream has
matched yet are found with the grants workstream's own matcher
(build_entity_edges.py) against data/kg_keywords.csv.

Every source keeps its own virus and protein matching. This script only maps the
names onto one shared list (Ebola / Marburg / filovirus family, and the nine
filovirus proteins), so one filter can apply to every tab.

The data is embedded in dashboard.template.html in place of /*__DATA__*/null, the
same way federal-biomedical-tech-datasets-script/build_links_page.py works.

Usage:

  python dashboard/build_dashboard.py
"""

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

csv.field_size_limit(10**8)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = Path(os.environ.get("FILOVIRUS_DATA_DIR") or REPO / "data")
GRANTS_WS = REPO / "federal-biomedical-tech-datasets-script"
GRANTS_OUT = GRANTS_WS / "output"
PATENT_WS = REPO / "patent_pipeline" / "data" / "processed"
TEMPLATE = HERE / "dashboard.template.html"
OUT = HERE / "output" / "research_dashboard.html"

sys.path[:0] = [str(REPO), str(GRANTS_WS), str(HERE)]
import build_entity_edges as matcher  # noqa: E402

from core_data_loader.etl.evidence import (  # noqa: E402
    AWARD_SERIAL_RE,
    CONTRACT_IN_TEXT_RE,
    contract_targets,
    is_filovirus_paper,
    normalize_document_id,
    parse_grant_numbers,
)

# Shared filter vocabulary: index 0-2 viruses, 3-11 proteins.
VIRUSES = ["Ebola viruses", "Marburg viruses", "Filovirus family"]
PROTEINS = ["NP", "VP35", "VP40", "GP", "sGP", "ssGP", "VP30", "VP24", "L"]
ENTS = [["virus", v] for v in VIRUSES] + [["protein", p] for p in PROTEINS]
PROTEIN_IDX = {p: 3 + i for i, p in enumerate(PROTEINS)}

# Patent protein names (patent_entity_links_review.csv) -> gene symbol.
PATENT_PROTEIN = {
    "glycoprotein": "GP", "envelope glycoprotein": "GP", "gp": "GP", "sgp": "sGP", "ssgp": "ssGP",
    "nucleoprotein": "NP", "np": "NP", "polymerase": "L", "rna-dependent rna polymerase": "L",
}

# A link from a grant to a paper that cites it, by the topic pattern build_entity_edges.py
# gives the pair (viruses by genus + proteins). Codes match the template.
PATTERN = {"same": "s", "narrowed": "s", "expanded": "a", "shifted": "a", "disjoint": "n", "paper_has_none": "e"}
PUBMED_ACK, REPORTER, PI_AUTHOR = 1, 2, 4
# Where a paper entered the set. Codes match the template.
CORE, VIA_GRANT, VIA_PATENT = 1, 2, 3

unmapped = Counter()


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def virus_idx(name):
    n = name.lower()
    if "marburg" in n or "ravn" in n:
        return 1
    if "filovir" in n:
        return 2
    if any(w in n for w in ("ebola", "bundibugyo", "sudan", "reston", "tai forest", "bombali")):
        return 0
    unmapped[("virus", name)] += 1
    return None


def protein_idx(name):
    if name in PROTEIN_IDX:
        return PROTEIN_IDX[name]
    n = name.lower().strip()
    if n in PATENT_PROTEIN:
        return PROTEIN_IDX[PATENT_PROTEIN[n]]
    m = re.search(r"(?:vp|protein)\s*(24|30|35|40)\b", n)
    if m:
        return PROTEIN_IDX["VP" + m.group(1)]
    unmapped[("protein", name)] += 1
    return None


def ent_idx(kind, name):
    return virus_idx(name) if kind == "taxon" else protein_idx(name)


def year_of(s):
    m = re.match(r"(\d{4})", s or "")
    return int(m.group(1)) if m else None


def money(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


MOJIBAKE = {"â�\x80�\x99": "’", "�\x80\x93": "–", "�\x80\x94": "—", "�\x80\x99": "’"}


def clean_text(s):
    """Repair the double-encoded dashes and quotes in some patent citation text; drop control bytes."""
    s = (s or "").strip()
    for bad, good in MOJIBAKE.items():
        s = s.replace(bad, good)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", s)


def compact_patent(pid):
    return re.sub(r"\s+", "", pid or "").upper()


def patent_key(pid):
    return compact_patent(normalize_document_id(pid) or pid)


# ---------------------------------------------------------------------------
# Grant numbers -> awards (the loader's GrantNumbers, on the award staging table)
# ---------------------------------------------------------------------------

class GrantNumbers:
    """
    Same rules as core_data_loader.etl.evidence.GrantNumbers: a number with its
    activity code ("R43 AI066487") names that award; a bare serial ("AI066487")
    names every award of the NIH project; contracts match on their number.
    """

    def __init__(self, awards):
        self.by_core, self.by_serial, self.contracts = defaultdict(set), defaultdict(set), []
        for a in awards:
            core, key = a["core_project_num"], a["award_key"]
            m = AWARD_SERIAL_RE.match(core or "")
            if m:
                self.by_core[core].add(key)
                self.by_serial[m.group(2)].add(key)
            else:
                self.contracts.append((re.sub(r"[^0-9A-Z]", "", key.upper()), key, a["project_title"].strip().lower()))

    def match(self, text_):
        targets = {}
        for activity, serial in parse_grant_numbers(text_):
            exact = self.by_core.get(f"{activity}{serial}") if activity else None
            for key in exact or self.by_serial.get(serial, ()):
                targets.setdefault(key, f"{activity or ''}{serial}")
        for m in CONTRACT_IN_TEXT_RE.finditer((text_ or "").upper()):
            for key in contract_targets(m.group(1) or m.group(2), self.contracts):
                targets.setdefault(key, m.group(0).strip())
        return targets


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def build_grants():
    staging = GRANTS_OUT / "grants" / "staging"
    awards = read_csv(staging / "stg_grant_award.csv")
    funding = defaultdict(list)
    for r in read_csv(staging / "stg_grant_funding.csv"):
        funding[r["award_key"]].append(r)
    ents = defaultdict(set)
    for r in read_csv(DATA / "grants" / "grant_entity_edges.csv"):
        if r["matched_entity_type"] in ("taxon", "gene"):
            i = ent_idx(r["matched_entity_type"], r["entity_name"])
            if i is not None:
                ents[r["source_id"]].add(i)

    grants, index = [], {}
    for a in awards:
        rows = funding.get(a["award_key"], [])
        # A multi-project grant lists the parent award and each subproject; the parent's amount
        # already includes them. Same rule as fetch_nih_grants.py / build_grants_staging.py.
        parents = [r for r in rows if not r["subproject_id"]] or rows
        by_year = defaultdict(float)
        for r in parents:
            by_year[int(r["fiscal_year"])] += money(r["award_amount"])
        years = sorted(int(r["fiscal_year"]) for r in rows)
        url = next((r["project_detail_url"] for r in parents if r["project_detail_url"]), "")
        index[a["award_key"]] = len(grants)
        grants.append({
            "k": a["award_key"], "c": a["core_project_num"], "t": a["project_title"].strip(),
            "a": a["activity_code"], "ic": a["admin_ic_name"], "o": a["org_name"].strip(),
            "st": a["org_state"], "co": a["org_country"],
            "pi": a["contact_pi_name"].strip(), "s": a["project_start_date"], "e": a["project_end_date"],
            "y0": year_of(a["project_start_date"]) or (years[0] if years else None),
            "y1": year_of(a["project_end_date"]) or (years[-1] if years else None),
            "r": a["relevance_tier"], "u": url, "sd": 1 if a["project_start_date"] else 0,
            "fy": [[y, round(v)] for y, v in sorted(by_year.items())],
            "n": sorted(ents.get(a["award_key"], ())),
        })
    return grants, index, awards


class Papers:
    """The paper set, in the order papers are added: core set, grant-cited, patent-cited."""

    def __init__(self):
        self.rows, self.index, self.text = [], {}, {}

    def add(self, pmid, origin, title, journal, pub_date, year, abstract=""):
        if not pmid or pmid in self.index:
            return False
        self.index[pmid] = len(self.rows)
        self.rows.append({"id": pmid, "t": (title or "").strip() or f"PMID {pmid}", "j": journal or "",
                          "d": pub_date or year or "", "y": year_of(year) or year_of(pub_date),
                          "o": origin, "core": 1 if origin == CORE else 0, "n": []})
        self.text[pmid] = (title or "", abstract or "")
        return True


def build_papers():
    papers = Papers()
    ws_papers = {r["pmid"]: r for r in read_csv(GRANTS_OUT / "papers" / "filovirus_papers.csv")}
    abstracts = {r["pmid"]: r["abstract"] for r in read_csv(GRANTS_OUT / "papers" / "paper_abstracts.csv")}
    dates = {r["pmid"]: r["epub_date"] for r in read_csv(DATA / "papers" / "paper_dates.csv")}

    # Core set: the papers bundle (unique_papers_v2.csv), titled from publication_metadata.csv.
    bundle_dir = DATA / "papers" / "papers_iedb_rcsb_update" / "output_v2"
    bundle = read_csv(bundle_dir / "unique_papers_v2.csv")
    meta = {r["pmid"]: r for r in read_csv(DATA / "papers" / "publication_metadata.csv")}
    for b in bundle:
        pmid = b["pmid"]
        m, w = meta.get(pmid, {}), ws_papers.get(pmid, {})
        if papers.add(pmid, CORE, m.get("title") or w.get("title"), m.get("journal") or w.get("journal"),
                      w.get("pub_date") or m.get("year"), m.get("year") or w.get("year"), abstracts.get(pmid, "")):
            papers.rows[-1].update(cb=int(w.get("cited_by_count") or 0), src=b["cited_by_sources"])

    # Grant-cited: the PubMed records of papers RePORTER lists under a grant, kept when they are
    # about filoviruses (the loader's is_filovirus_paper, the same rule build_cited_papers uses).
    grant_cited = 0
    for r in read_csv(DATA / "grants" / "cited_publications.csv"):
        if is_filovirus_paper(r["title"], r["mesh_terms"], r["abstract"]):
            grant_cited += papers.add(r["pmid"], VIA_GRANT, r["title"], r["journal"],
                                      dates.get(r["pmid"]) or r["year"], r["year"], r["abstract"])

    # Patent-cited: every reference a patent makes that resolved to PubMed, filovirus or not
    # (the team's 2026-11-10 decision, as in build_patent_cited_papers).
    patent_cited = 0
    for r in read_csv(DATA / "patents" / "patent_cited_publications.csv"):
        if r["pmid"]:
            patent_cited += papers.add(r["pmid"], VIA_PATENT, r["title"], r["journal"], r["year"], r["year"], r["abstract"])

    # Viruses and proteins: the grants workstream's matches where it made them, the same
    # matcher (build_entity_edges.py) for papers it hasn't seen.
    ents, matched = defaultdict(set), set()
    for r in read_csv(GRANTS_OUT / "entity_links" / "paper_entity_edges.csv"):
        matched.add(r["source_id"])
        if r["matched_entity_type"] in ("taxon", "gene"):
            i = ent_idx(r["matched_entity_type"], r["entity_name"])
            if i is not None:
                ents[r["source_id"]].add(i)
    targets, _ = matcher.load_vocabulary(DATA / "kg_keywords.csv", GRANTS_WS / "inputs" / "kg_clinical_terms.csv")
    targets = [t for t in targets if t["entity_type"] in ("taxon", "gene")]
    rematched = 0
    for p in papers.rows:
        pmid = p["id"]
        if pmid in matched:
            continue
        title, abstract = papers.text[pmid]
        hits = matcher.match_text(title, "title", targets) + matcher.match_text(abstract, "abstract", targets)
        for h in hits:
            i = ent_idx(h["entity_type"], h["entity_name"])
            if i is not None:
                ents[pmid].add(i)
        rematched += 1
    for p in papers.rows:
        p["n"] = sorted(ents.get(p["id"], ()))
    print(f"  papers: {sum(p['core'] for p in papers.rows)} core, {grant_cited} grant-cited, "
          f"{patent_cited} patent-cited; {rematched} matched here for viruses/proteins")
    return papers


def federal_statement(text_):
    """The federal-support sentence of a government-interest section, or '' when it says there is none."""
    s = " ".join(re.sub(r"^.*?itemscope>", "", text_ or "").split())
    if not s or re.search(r"\bnot applicable\b|\bN\.\s?A\.", s, re.IGNORECASE):
        return ""
    s = re.split(r"\b(?:BACKGROUND|FIELD OF THE INVENTION|TECHNICAL FIELD|CROSS[- ]REFERENCE|SEQUENCE LISTING)\b", s)[0]
    end = r"(?=\s+(?:The|Accordingly,? the)\s+(?:U\.S\.\s+|United States\s+)?government (?:has|may have)|$)"
    # From "This invention was made with government support ..." to the "government has certain rights" boilerplate.
    m = re.search(r"\b(?:This|The|Part of the)\s+(?:invention|work|materials|research)\b.{0,120}?"
                  r"\b(?:support|supported|funds?|funding|grant|licensed)\b.*?" + end, s, re.IGNORECASE)
    if m:
        out = m.group(0)
    else:   # "Government support under NIH Grant Nos. ..." with no lead-in
        out = re.search(r".*?" + end, re.sub(r"^(?:[A-Z][A-Z\-]+\s+){1,10}", "", s), re.IGNORECASE).group(0)
    out = out.strip()
    return out if len(out) <= 320 else out[:317].rsplit(" ", 1)[0] + "…"


def build_patents():
    ev = read_csv(DATA / "patents" / "patent_evidence_expanded.csv")
    inventors = {compact_patent(r["patent_id"]): r["inventor_name"]
                 for r in read_csv(PATENT_WS / "patent_inventors_expanded.csv")}
    details = {patent_key(r["document_id"]): r for r in read_csv(DATA / "patents" / "patent_details.csv")}
    ents, review = defaultdict(set), defaultdict(lambda: [0, 0])
    for r in read_csv(PATENT_WS / "patent_entity_links_review.csv"):
        pid = compact_patent(r["patent_id"])
        i = ent_idx(r["entity_type"], r["canonical_name"])
        if i is not None:
            ents[pid].add(i)
        review[pid][0 if r["review_status"] == "likely_valid" else 1] += 1

    patents, index = [], {}
    for r in ev:
        pid = compact_patent(r["patent_id"])
        d = details.get(pid, {})
        for kw in (r["matched_keyword"] or "").split("|"):
            if kw.strip():
                i = virus_idx(kw.strip())
                if i is not None:
                    ents[pid].add(i)
        kind_code = r["patent_id"].split()[-1] if " " in r["patent_id"] else ""
        cpc = r["cpc_filter"].replace(" ", "")
        if r["match_method"].startswith("broader"):
            found = "broad"
        elif "A61K39/12" in cpc and "C07K16/10" in cpc:
            found = "both"
        elif "C07K16/10" in cpc:
            found = "ab"
        else:
            found = "vax"
        filed = r["filing_date"] or d.get("filing_date", "")
        index[pid] = len(patents)
        patents.append({
            "id": r["patent_id"], "t": r["title"].strip(), "f": filed, "p": r["date"] or d.get("publication_date", ""),
            "y": year_of(filed), "g": 1 if kind_code.startswith("B") else 0,
            "asg": r["assignee"].strip(), "inv": inventors.get(pid, "") + (" et al." if "et al" in r["inventor"] else ""),
            "how": found, "fam": r["family_id"], "u": r["url"], "n": sorted(ents.get(pid, ())), "rv": review.get(pid, [0, 0]),
            "gi": federal_statement(d.get("government_interest", "")),
        })
    return patents, index, details


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def document_data() -> dict:
    """The document tabs' data, built from the workstreams' files."""
    print(f"Reading {DATA}")
    grants, g_idx, awards = build_grants()
    papers = build_papers()
    p_idx, P = papers.index, papers.rows
    patents, t_idx, details = build_patents()
    numbers = GrantNumbers(awards)
    dropped = Counter()

    # Grant -> paper. Cited, three routes: RePORTER lists the paper under the grant
    # (grant_publications.csv), or the paper's PubMed record lists the grant (its GrantList,
    # paper_grants.csv, and the grants workstream's pubmed_ack rows). PI-written: a grant PI
    # wrote it after the award started and it is on the grant's topic (grant_paper_link.csv).
    pairs = {}

    def link(award_key, pmid, route, pattern=None):
        if award_key not in g_idx or pmid not in p_idx:
            return False
        pair = pairs.setdefault((award_key, pmid), [0, ""])
        pair[0] |= route
        if pattern is not None:
            pair[1] = pattern
        return True

    by_core = defaultdict(set)
    for a in awards:
        by_core[a["core_project_num"]].add(a["award_key"])
    for r in read_csv(DATA / "grants" / "grant_publications.csv"):
        keys = by_core.get(r["core_project_num"], ())
        # RePORTER lists a contract's publications under its truncated core number, which the
        # staging step splits into several awards; that names no single award (as in the loader).
        if len(keys) == 1 and r["pmid"] in p_idx:
            link(next(iter(keys)), r["pmid"], REPORTER)
    for r in read_csv(DATA / "grants" / "grant_paper_divergence.csv"):
        route = PUBMED_ACK if "pubmed_ack" in r["link_basis"] else 0
        if not link(r["award_key"], r["pmid"], route, PATTERN.get(r["core_pattern"], "e")):
            dropped["grant-paper (divergence file)"] += 1
    for r in read_csv(DATA / "papers" / "paper_grants.csv"):
        if r["pmid"] not in p_idx:
            continue
        grant_id = r["grant_id"].strip()
        if r["acronym"] and re.fullmatch(r"\d{5,6}", grant_id):
            grant_id = r["acronym"] + grant_id
        for key in numbers.match(grant_id):
            link(key, r["pmid"], PUBMED_ACK)
    for r in read_csv(GRANTS_OUT / "links" / "grant_paper_link.csv"):
        if r["is_relevant"] == "true" and not link(r["award_key"], r["pmid"], PI_AUTHOR):
            dropped["grant-paper PI"] += 1
    gp = [[g_idx[g], p_idx[p], routes, pat] for (g, p), (routes, pat) in sorted(pairs.items()) if routes]

    # Patent -> paper. A cited paper in the set by PMID: the patent pipeline's matches to the
    # core set, and every free-text reference fetch patent-citations resolved to PubMed.
    # Unresolved references stay as citation text ("outside literature").
    tp, seen = [], set()
    for r in read_csv(DATA / "patents" / "patent_team_paper_edges.csv"):
        pid, pmid = compact_patent(r["patent_id"]), r["pmid"]
        if pid not in t_idx or pmid not in p_idx:
            dropped["patent-paper team"] += 1
            continue
        seen.add((pid, pmid))
        tp.append([t_idx[pid], p_idx[pmid], r["match_type"]])
    resolved = {(compact_patent(r["patent_id"]), r["external_paper_id"]): r["pmid"]
                for r in read_csv(DATA / "patents" / "patent_cited_publications.csv") if r["pmid"]}
    tx = []
    for r in read_csv(DATA / "patents" / "patent_external_literature_edges.csv"):
        pid = compact_patent(r["patent_id"])
        if pid not in t_idx:
            dropped["patent-paper external"] += 1
            continue
        pmid = resolved.get((pid, r["external_paper_id"]))
        if pmid in p_idx:
            if (pid, pmid) not in seen:
                seen.add((pid, pmid))
                tp.append([t_idx[pid], p_idx[pmid], "resolved reference"])
        else:
            tx.append([t_idx[pid], clean_text(r["citation_text"]), year_of(r["paper_year"])])

    # Grant -> patent: the grant numbers in the patent's government-interest statement.
    gt = []
    for pid, d in details.items():
        if pid in t_idx and d["government_interest"]:
            for key, number in sorted(numbers.match(d["government_interest"]).items()):
                if key in g_idx:
                    gt.append([g_idx[key], t_idx[pid], number])

    # Paper -> paper (PubMed reference lists) and patent -> patent (Google Patents citation
    # tables, read from both ends), when both documents are in the set.
    pc = sorted({(p_idx[r["pmid"]], p_idx[r["cited_pmid"]])
                 for r in read_csv(DATA / "papers" / "paper_citations.csv")
                 if r["pmid"] in p_idx and r["cited_pmid"] in p_idx and r["pmid"] != r["cited_pmid"]})
    tc = set()
    for pid, d in details.items():
        if pid not in t_idx:
            continue
        for n in filter(None, d["cited_patents"].split(";")):
            if patent_key(n) in t_idx:
                tc.add((t_idx[pid], t_idx[patent_key(n)]))
        for n in filter(None, d["cited_by_patents"].split(";")):
            if patent_key(n) in t_idx:
                tc.add((t_idx[patent_key(n)], t_idx[pid]))
    tc = sorted((a, b) for a, b in tc if a != b)

    data = {
        "built": date.today().isoformat(), "ents": ENTS,
        "grants": grants, "papers": P, "patents": patents,
        "gp": gp, "tp": tp, "tx": tx, "gt": gt, "pc": [list(x) for x in pc], "tc": [list(x) for x in tc],
    }
    print(f"  {len(grants)} grants, {len(P)} papers, {len(patents)} patents")
    print(f"  grant-paper {len(gp)}, patent-paper {len(tp)} in set + {len(tx)} outside, grant-patent {len(gt)} "
          f"({len({l[1] for l in gt})} patents, {sum(1 for t in patents if t['gi'])} with a federal-support statement)")
    print(f"  paper-paper citations {len(pc)}, patent-patent citations {len(tc)}")
    if dropped:
        print("  dropped (an end is not in the dataset):", dict(dropped))
    if unmapped:
        print("  names not mapped to a virus or protein:", dict(unmapped.most_common(15)), file=sys.stderr)
    return data


def main():
    import build_explorer

    data = document_data()

    # Explore tab: from Neo4j when it's running (and the checked-in snapshot is
    # refreshed), otherwise from that snapshot, so a fresh clone needs no Neo4j.
    try:
        graph = build_explorer.graph_data()
        build_explorer.write_snapshot(build_explorer.SNAPSHOT, graph)
        print(f"  saved {build_explorer.SNAPSHOT.relative_to(REPO)} (commit it so anyone can rebuild the page)")
    except Exception as e:  # noqa: BLE001 -- any Neo4j failure means: use the snapshot
        graph = build_explorer.read_snapshot(build_explorer.SNAPSHOT)
        print(f"Graph: Neo4j not reachable ({type(e).__name__}), so using "
              f"{build_explorer.SNAPSHOT.relative_to(REPO)} (built {graph['built']})")

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    html = build_explorer.embed(html, graph)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(REPO)} ({len(html.encode()) / 2**20:.1f} MB)")
    print(build_explorer.summary(graph))


if __name__ == "__main__":
    main()

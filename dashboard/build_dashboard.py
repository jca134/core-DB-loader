#!/usr/bin/env python3
"""Build the research dashboard (dashboard/output/research_dashboard.html).

One page with a tab for each collection (grants, papers, patents) and for each
pair of them, filled from the CSVs the workstreams already wrote:

  grants   federal-biomedical-tech-datasets-script/output/   (fetch_nih_grants.py,
           build_grants_staging.py, build_entity_edges.py, link_grants_to_papers.py)
  papers   the same folder: output/papers/ (the papers workstream's 240 filovirus papers,
           plus the filovirus papers NIH RePORTER or PubMed tie to a grant)
  patents  patent_pipeline/data/processed/

Every source keeps its own virus and protein matching. This script only maps the
names onto one shared list (Ebola / Marburg / filovirus family, and the nine
filovirus proteins), so one filter can apply to every tab.

Grant -> patent links are not built yet: none of the inputs records which grant
funded a patent. That tab shows the indirect route instead (a grant's paper that
a patent cites).

The data is embedded in dashboard.template.html in place of /*__DATA__*/null, the
same way federal-biomedical-tech-datasets-script/build_links_page.py works.

Usage:

  python dashboard/build_dashboard.py
"""

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

csv.field_size_limit(10**8)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
GRANTS_OUT = REPO / "federal-biomedical-tech-datasets-script" / "output"
PATENTS = REPO / "patent_pipeline" / "data" / "processed"
TEMPLATE = HERE / "dashboard.template.html"
OUT = HERE / "output" / "research_dashboard.html"

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


def compact_patent(pid):
    return re.sub(r"\s+", "", pid or "").upper()


def build_grants():
    awards = read_csv(GRANTS_OUT / "grants" / "staging" / "stg_grant_award.csv")
    funding = defaultdict(list)
    for r in read_csv(GRANTS_OUT / "grants" / "staging" / "stg_grant_funding.csv"):
        funding[r["award_key"]].append(r)
    ents = defaultdict(set)
    for r in read_csv(GRANTS_OUT / "entity_links" / "grant_entity_edges.csv"):
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
    return grants, index


def build_papers():
    team = read_csv(GRANTS_OUT / "papers" / "filovirus_papers.csv")
    cited = read_csv(GRANTS_OUT / "papers" / "cited_papers.csv")
    papers, index = [], {}

    def add(pmid, t, j, d, y):
        index[pmid] = len(papers)
        papers.append({"id": pmid, "t": t.strip(), "j": j, "d": d, "y": y, "core": 0, "n": []})

    for p in team:
        add(p["pmid"], p["title"], p["journal"], p["pub_date"] or p["year"], year_of(p["year"]))
        papers[-1].update(core=1, cb=int(p["cited_by_count"] or 0), src=p["source_databases"])
    for p in cited:
        if p["is_filovirus"] == "true" and p["pmid"] not in index:
            add(p["pmid"], p["title"], p["journal"], p["pub_date"] or p["year"], year_of(p["year"]))

    ents = defaultdict(set)
    for r in read_csv(GRANTS_OUT / "entity_links" / "paper_entity_edges.csv"):
        if r["matched_entity_type"] in ("taxon", "gene"):
            i = ent_idx(r["matched_entity_type"], r["entity_name"])
            if i is not None:
                ents[r["source_id"]].add(i)
    for p in papers:
        p["n"] = sorted(ents.get(p["id"], ()))
    return papers, index


def build_patents():
    ev = read_csv(PATENTS / "patent_evidence_expanded.csv")
    inventors = {compact_patent(r["patent_id"]): r["inventor_name"]
                 for r in read_csv(PATENTS / "patent_inventors_expanded.csv")}
    ents, review = defaultdict(set), defaultdict(lambda: [0, 0])
    for r in read_csv(PATENTS / "patent_entity_links_review.csv"):
        pid = compact_patent(r["patent_id"])
        i = ent_idx(r["entity_type"], r["canonical_name"])
        if i is not None:
            ents[pid].add(i)
        review[pid][0 if r["review_status"] == "likely_valid" else 1] += 1

    patents, index = [], {}
    for r in ev:
        pid = compact_patent(r["patent_id"])
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
        index[pid] = len(patents)
        patents.append({
            "id": r["patent_id"], "t": r["title"].strip(), "f": r["filing_date"], "p": r["date"],
            "y": year_of(r["filing_date"]), "g": 1 if kind_code.startswith("B") else 0,
            "asg": r["assignee"].strip(), "inv": inventors.get(pid, "") + (" et al." if "et al" in r["inventor"] else ""),
            "how": found, "fam": r["family_id"], "u": r["url"], "n": sorted(ents.get(pid, ())), "rv": review.get(pid, [0, 0]),
        })
    return patents, index


def main():
    grants, g_idx = build_grants()
    papers, p_idx = build_papers()
    patents, t_idx = build_patents()
    dropped = Counter()

    # Grant -> paper. Cited: the paper's PubMed record lists the grant, or RePORTER lists the
    # paper under the grant (grant_paper_divergence.csv). PI-authored: a grant PI wrote it after
    # the award started and it is on the grant's topic (is_relevant in grant_paper_link.csv).
    pairs = {}
    for r in read_csv(GRANTS_OUT / "entity_links" / "grant_paper_divergence.csv"):
        if r["award_key"] not in g_idx or r["pmid"] not in p_idx:
            dropped["grant-paper cited"] += 1
            continue
        routes = (PUBMED_ACK if "pubmed_ack" in r["link_basis"] else 0) | (REPORTER if "reporter" in r["link_basis"] else 0)
        pairs[(r["award_key"], r["pmid"])] = [routes, PATTERN.get(r["core_pattern"], "e")]
    for r in read_csv(GRANTS_OUT / "links" / "grant_paper_link.csv"):
        if r["is_relevant"] != "true":
            continue
        if r["award_key"] not in g_idx or r["pmid"] not in p_idx:
            dropped["grant-paper PI"] += 1
            continue
        pair = pairs.setdefault((r["award_key"], r["pmid"]), [0, ""])
        pair[0] |= PI_AUTHOR
    gp = [[g_idx[g], p_idx[p], routes, pat] for (g, p), (routes, pat) in sorted(pairs.items())]

    # Patent -> paper. Team: the cited paper is in the paper set (by PMID). External: anything
    # else in the patent's non-patent literature, kept as citation text.
    tp = []
    for r in read_csv(PATENTS / "master_patent_team_paper_edges.csv"):
        pid, pmid = compact_patent(r["patent_id"]), r["pmid"]
        if pid not in t_idx or pmid not in p_idx:
            dropped["patent-paper team"] += 1
            continue
        tp.append([t_idx[pid], p_idx[pmid], r["match_type"]])
    tx = []
    for r in read_csv(PATENTS / "master_patent_external_literature_edges.csv"):
        pid = compact_patent(r["patent_id"])
        if pid not in t_idx:
            dropped["patent-paper external"] += 1
            continue
        tx.append([t_idx[pid], r["citation_text"].strip(), year_of(r["paper_year"])])

    data = {
        "built": date.today().isoformat(), "ents": ENTS,
        "grants": grants, "papers": papers, "patents": patents,
        "gp": gp, "tp": tp, "tx": tx,
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")

    print(f"Wrote {OUT.relative_to(REPO)} ({len(html) / 1024:.0f} KB)")
    print(f"  {len(grants)} grants, {len(papers)} papers ({sum(p['core'] for p in papers)} in the core set), {len(patents)} patents")
    print(f"  grant-paper {len(gp)}, patent-paper {len(tp)} team + {len(tx)} external")
    if dropped:
        print("  dropped (an end is not in the dataset):", dict(dropped))
    if unmapped:
        print("  names not mapped to a virus or protein:", dict(unmapped.most_common(15)), file=sys.stderr)


if __name__ == "__main__":
    main()

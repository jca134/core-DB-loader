#!/usr/bin/env python3
"""Build the grant -> paper links page (output/links/grant_paper_links.html).

Joins the two kinds of grant -> paper link into one dataset and embeds it in
viz/grant_paper_links.template.html:

  cited      the paper acknowledges the grant: PubMed's grant list or RePORTER's
             publication list (build_entity_edges.py, grant_paper_divergence.csv)
  PI-authored  a grant PI wrote the paper after the award started
             (link_grants_to_papers.py, grant_paper_link.csv)

Each grant and paper carries its upper-level entities at the level the divergence
comparison uses (taxa by genus), so the page can show what a paper shares with its
grant and what it adds.

Run build_entity_edges.py first. Usage:

  python build_links_page.py
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

from build_entity_edges import comparison_keys, pattern

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "viz" / "grant_paper_links.template.html"
OUT = HERE / "output" / "links" / "grant_paper_links.html"

# Route bits, matching the page's script.
PUBMED_ACK, REPORTER, PI_AUTHOR = 1, 2, 4
CONF = {"high": "h", "medium": "m", "low": "l"}
TAXON_LABEL = {"Orthoebolavirus": "Ebola viruses", "Orthomarburgvirus": "Marburg viruses",
               "Filoviridae": "Filoviruses"}


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    links_dir, entity_dir, papers_dir = HERE / "output" / "links", HERE / "output" / "entity_links", HERE / "output" / "papers"
    awards = {r["award_key"]: r for r in read_csv(HERE / "output" / "grants" / "staging" / "stg_grant_award.csv")}
    grant_rows = read_csv(entity_dir / "grant_entity_edges.csv")
    grant_keys = comparison_keys(grant_rows)
    paper_rows = read_csv(entity_dir / "paper_entity_edges.csv")
    paper_keys = comparison_keys(paper_rows)
    cited = read_csv(entity_dir / "grant_paper_divergence.csv")
    pi_links = [r for r in read_csv(links_dir / "grant_paper_link.csv")]

    # Paper facts: the papers workstream's file wins, cited_papers.csv fills the rest.
    papers = {}
    for p in read_csv(papers_dir / "cited_papers.csv"):
        if p["is_filovirus"] == "true":
            papers[p["pmid"]] = {"t": p["title"], "j": p["journal"], "d": p["pub_date"] or p["year"], "y": p["year"]}
    for p in read_csv(papers_dir / "filovirus_papers.csv"):
        papers[p["pmid"]] = {"t": p["title"], "j": p["journal"], "d": p["pub_date"] or p["year"], "y": p["year"]}

    entities, ent_index = [], {}

    def ent_ids(keys):
        out = []
        for key, name in sorted(keys.items()):
            if key not in ent_index:
                ent_index[key] = len(entities)
                entities.append([key[0], TAXON_LABEL.get(name, name)])
            out.append(ent_index[key])
        return out

    pairs = defaultdict(lambda: {"routes": 0, "pi": None})
    for r in cited:
        pair = pairs[(r["award_key"], r["pmid"])]
        pair["routes"] |= (PUBMED_ACK if "pubmed_ack" in r["link_basis"] else 0) | (REPORTER if "reporter" in r["link_basis"] else 0)
        pair["core"], pair["full"] = r["core_pattern"], r["pattern"]
    for r in pi_links:
        if r["pmid"] not in papers:
            continue
        pair = pairs[(r["award_key"], r["pmid"])]
        pair["routes"] |= PI_AUTHOR
        if r["grant_acknowledged"] == "true":
            pair["routes"] |= PUBMED_ACK
        # One PI per pair: the strongest author match, as the old page showed per link.
        candidate = [r["pi_name"], r["author_name"], CONF.get(r["match_confidence"], "l"),
                     r["is_relevant"] == "true", round(float(r["similarity_percentile"] or 0), 3),
                     int(r["days_after_award_start"] or 0)]
        if pair["pi"] is None or "hml".index(candidate[2]) < "hml".index(pair["pi"][2]):
            pair["pi"] = candidate

    grant_list, grant_index = [], {}
    links = []
    for (award_key, pmid), pair in sorted(pairs.items()):
        if award_key not in awards or pmid not in papers:
            continue
        if award_key not in grant_index:
            a = awards[award_key]
            grant_index[award_key] = len(grant_list)
            grant_list.append({"k": award_key, "c": a["core_project_num"], "t": a["project_title"],
                               "a": a["activity_code"], "o": a["org_name"], "s": a["project_start_date"],
                               "e": a["project_end_date"], "r": a["relevance_tier"],
                               "pi": a["contact_pi_name"], "n": ent_ids(grant_keys.get(award_key, {}))})
        g, p = grant_keys.get(award_key, {}), paper_keys.get(pmid, {})
        core = {"taxon", "gene"}
        core_pat = pair.get("core") or pattern({k for k in g if k[0] in core}, {k for k in p if k[0] in core})
        links.append([grant_index[award_key], pmid, pair["routes"], core_pat, pair["pi"]])

    used = {l[1] for l in links}
    for pmid in list(papers):
        if pmid in used:
            papers[pmid]["n"] = ent_ids(paper_keys.get(pmid, {}))
        else:
            del papers[pmid]

    # Coverage over every grant and paper, not just the linked ones: entity -> [grants, papers].
    coverage = defaultdict(lambda: [0, 0])
    for side, keys in ((0, grant_keys), (1, paper_keys)):
        for source_keys in keys.values():
            for e in ent_ids(source_keys):
                coverage[e][side] += 1
    data = {"ents": entities, "grants": grant_list, "papers": papers, "links": links,
            "cov": {str(e): v for e, v in coverage.items()},
            "totals": [len(awards), len(paper_keys), len(grant_rows)]}
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(HERE)}: {len(grant_list)} grants, {len(papers)} papers, "
          f"{len(links)} links, {len(entities)} entities, {len(html) / 1024:.0f} KB")


if __name__ == "__main__":
    main()

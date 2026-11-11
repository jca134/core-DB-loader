"""
Builds the evidence layer -- grants, patents, papers, and the links from each
to entities already in core.* -- out of raw.nih_reporter_grants,
raw.uspto_patents and raw.papers_*.

Run LAST, after every other ETL script. Links are resolved to live primary
keys, and most of those keys are surrogate: etl.bvbrc_hfv regenerates
core.epitope/structure/antibody ids, etl.ucsc regenerates core.gene ids, and
etl.immport regenerates core.condition ids. Those scripts truncate with
RESTART IDENTITY, so a rerun over unchanged source data reproduces the same
ids harmlessly -- but any upstream data change shifts them, and the links this
script wrote then point at the wrong rows. Rerun it after any upstream ETL
(same standing requirement as etl.taxon_condition).

The orphan check at the end only catches links whose target id no longer
exists. Shifted-but-still-valid ids pass it silently, so a clean orphan check
is not evidence that the links are correct -- only that none are dangling.

Two kinds of link are produced, and they are not equally trustworthy:

  exact identifier  confidence 1.0. The papers workstream carries each source's
                    own id (BV-BRC genome_id, IEDB epitope id, PDB id, ImmPort
                    accession), so the link is a lookup, not a guess.
  keyword           confidence < 1.0. A term from kg_keywords.csv occurring in a
                    grant abstract or patent title. Real signal, but it says the
                    document mentions the entity, not that it is about it --
                    which is why every one carries a support row naming the term
                    and the field it was found in, and why terms kg_keywords
                    flags as ambiguous are halved again.

core.publication is shared with etl.immport (which truncates it), so papers are
upserted on pubmed_id rather than truncated and rebuilt -- a paper found by
three routes stays one row, per the team's dedup requirement.
"""

import json
import re

from sqlalchemy import text

from core_data_loader.common.db_utils import clean_text
from core_data_loader.common.etl_common import (
    add_provenance,
    add_xref,
    blank_to_none,
    engine,
    ensure_sources,
    raw_table_exists,
    read_raw,
    safe_date,
    safe_float,
    safe_int,
    truncate,
    write_provenance,
    write_xref,
)
from core_data_loader.common.kg_keywords import (
    AMBIGUOUS_PENALTY,
    CONFIDENCE,
    KeywordMatcher,
)
from core_data_loader.common.link_keys import apply_reviews

# Owned outright -- replaced wholesale on every run. core.publication is NOT
# here: etl.immport writes lineage for it too, so it is passed as a shared
# table scoped to this script's own source_id instead.
PROVENANCE_TABLES_OWNED = ["grant_award", "grant_funding", "patent", "evidence_link"]
XREF_ENTITY_TYPES_OWNED = ["grant_award", "grant_funding", "patent", "publication"]

GRANTS_TABLE = "nih_reporter_grants"
GRANT_PUBS_TABLE = "nih_reporter_grant_publications"
CITED_PUBS_TABLE = "nih_reporter_cited_publications"
GRANT_ENTITY_EDGES_TABLE = "nih_reporter_grant_entity_edges"
GRANT_DIVERGENCE_TABLE = "nih_reporter_grant_paper_divergence"
PATENTS_TABLE = "uspto_patents"
PATENTS_EXPANDED_TABLE = "uspto_patents_expanded"
PATENT_LINKS_TABLE = "uspto_patent_entity_links"
PATENT_TEAM_PAPERS_TABLE = "uspto_patent_team_papers"
PATENT_CITED_PUBS_TABLE = "uspto_patent_cited_publications"
PATENT_DETAILS_TABLE = "uspto_patent_details"
PAPER_CITATIONS_TABLE = "papers_citations"
PAPERS_TABLE = "papers_unique"
PAPER_REFS_TABLE = "papers_references"
PAPER_META_TABLE = "papers_metadata"

# RePORTER truncates contract core numbers to 11 characters, merging distinct
# contracts under one core_project_num (26120150000 covers five unrelated task
# orders). For these, the award is keyed off project_num instead.
CONTRACT_ACTIVITY_CODES = {"N01", "N02", "N43", "N44", "Y01"}

# Terms whose presence in a grant title makes it unambiguously filovirus work.
# Used only for relevance_tier, not for entity linking -- linking goes through
# kg_keywords.
# Hyphens separate tokens here, matching _word_boundary_pattern in
# common/kg_keywords.py -- otherwise "Ebola-virus vaccines" would not count.
FILOVIRUS_TITLE_RE = re.compile(
    r"(?<![0-9A-Za-z])(filovir\w*|ebola\w*|marburg\w*|sudan virus|bundibugyo|"
    r"ravn virus|EBOV|SUDV|MARV|BDBV|TAFV|RESTV)(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# master_references_v2.csv's entity_type -> the core entity it resolves to.
# 'source_note' is excluded: those 14 rows cite nomenclature papers for the LANL
# spreadsheet itself, not for any entity in it.
PAPER_ENTITY_TYPES = {
    "genome": "isolate",
    "epitope": "epitope",
    "structure": "structure",
    "antibody": "antibody",
    "feature": "feature",
    "study": "study",
}


# ---------------------------------------------------------------------------
# Link accumulation
# ---------------------------------------------------------------------------

class LinkBuilder:
    """
    Collects (evidence, entity) links and the support rows behind them.

    One evidence_link per (evidence, entity, relation) regardless of how many
    terms reached it -- each term becomes a support row, and the link keeps the
    highest confidence any single match earned.
    """

    def __init__(self):
        self.links = {}

    def add(self, evidence_type, evidence_id, entity_type, entity_id, relation,
            confidence, source_id, support):
        if evidence_id is None or entity_id is None:
            return
        key = (evidence_type, evidence_id, entity_type, str(entity_id), relation)
        link = self.links.get(key)
        if link is None:
            link = self.links[key] = {"confidence": confidence, "source_id": source_id, "support": []}
        link["confidence"] = max(link["confidence"], confidence)
        if support is not None:
            link["support"].append(support)

    def write(self, conn, provenance_rows):
        link_rows = 0
        support_rows = 0
        for (ev_type, ev_id, ent_type, ent_id, relation), link in self.links.items():
            link_id = conn.execute(
                text(
                    """
                    INSERT INTO core.evidence_link
                        (evidence_type, evidence_id, entity_type, entity_id,
                         relation, confidence, source_id)
                    VALUES (:evidence_type, :evidence_id, :entity_type, :entity_id,
                            :relation, :confidence, :source_id)
                    RETURNING evidence_link_id
                    """
                ),
                {
                    "evidence_type": ev_type, "evidence_id": ev_id,
                    "entity_type": ent_type, "entity_id": ent_id,
                    "relation": relation, "confidence": link["confidence"],
                    "source_id": link["source_id"],
                },
            ).scalar()
            link_rows += 1
            add_provenance(provenance_rows, "evidence_link", link_id, link["source_id"],
                            f"{ev_type}_evidence", f"{ev_type}:{ev_id}")

            for s in link["support"]:
                # ON CONFLICT: the unique index collapses case variants of the
                # same term in the same field ('VP40' / 'vp40').
                result = conn.execute(
                    text(
                        """
                        INSERT INTO core.evidence_link_support
                            (evidence_link_id, grant_funding_id, matched_term, term_type,
                             match_field, match_method, requires_context)
                        VALUES (:evidence_link_id, :grant_funding_id, :matched_term, :term_type,
                                :match_field, :match_method, :requires_context)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"evidence_link_id": link_id, **s},
                )
                support_rows += result.rowcount
        return link_rows, support_rows


def organism_context_established(hits: list[dict]) -> bool:
    """
    Whether a document's own hits name an organism, which decides if its
    ambiguous-term hits are trustworthy enough to keep.

    kg_keywords marks terms whose names are ambiguous out of context with
    `requires_context`, and its README is explicit that those "need organism
    context in retrieval". A bare "L" or "N" is a filovirus gene symbol and
    also an ordinary letter: in a grant that elsewhere says "Ebola virus" the
    polymerase reading is plausible, and in "HUMAN ANTIBODIES TO HANTAVIRUS SIN
    NOMBRE" it is simply wrong. Without this rule the single letters L and N
    alone produced 110 links, nearly all of them noise.

    Context is established by any taxon hit on the document, including a
    patent's query_scope link -- a patent returned by a filovirus CPC search is
    about filoviruses whatever its title happens to say.
    """
    return any(h["entity_type"] == "taxon" for h in hits)


def keep_hit(hit: dict, has_context: bool) -> bool:
    """An ambiguous-term hit survives only with organism context; others always do."""
    return has_context or not hit["requires_context"]


def _support(match_field, match_method, matched_term=None, term_type=None,
             requires_context=False, grant_funding_id=None):
    return {
        "grant_funding_id": grant_funding_id,
        "matched_term": matched_term,
        "term_type": term_type,
        "match_field": match_field,
        "match_method": match_method,
        "requires_context": requires_context,
    }


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def award_key(core_project_num: str, project_num: str, activity_code: str) -> str:
    """
    The grouping key for one grant across its fiscal years.

    Normally core_project_num. For contract activity codes it is project_num
    with its trailing segment (the per-record counter) dropped, which keeps
    distinct task orders apart -- 261201500003I-0-26100013-1 and
    261201500003I-0-26100033-1 are different contracts that RePORTER reports
    under the single core number 26120150000. Erring toward splitting: merging
    two unrelated contracts into one award would silently sum their budgets.
    """
    if activity_code in CONTRACT_ACTIVITY_CODES and project_num:
        head, sep, _tail = project_num.rpartition("-")
        return head if sep else project_num
    return core_project_num


def relevance_tier(titles, abstracts: list) -> str | None:
    """
    strong: a filovirus term in any of the award's titles; moderate: 2+ abstract
    mentions; weak: 1.

    Every fiscal year's title is checked, not just the first. RePORTER titles
    change between years -- a project renamed to name Ebola in 2016 is strongly
    filovirus-related even if its 2010 title was generic -- and ignoring the
    later ones mis-tiers 33 of the 732 awards in the current data.
    """
    if isinstance(titles, str):
        titles = [titles]
    if any(t and FILOVIRUS_TITLE_RE.search(t) for t in titles):
        return "strong"
    mentions = sum(len(FILOVIRUS_TITLE_RE.findall(a or "")) for a in abstracts)
    if mentions >= 2:
        return "moderate"
    return "weak" if mentions == 1 else None


def contact_pi(row) -> str | None:
    """
    contact_pi_name, falling back to the is_contact_pi entry of the embedded
    principal_investigators JSON when RePORTER leaves the flat column blank.
    """
    name = blank_to_none(getattr(row, "contact_pi_name", None))
    if name:
        return name
    try:
        pis = json.loads(getattr(row, "principal_investigators", "") or "[]")
    except (ValueError, TypeError):
        return None
    for pi in pis:
        if isinstance(pi, dict) and pi.get("is_contact_pi"):
            return blank_to_none(pi.get("full_name"))
    return None


def build_grants(conn, matcher, links, xref_rows, provenance_rows, sid):
    if not raw_table_exists(GRANTS_TABLE):
        print("core.grant_award: raw.nih_reporter_grants absent, skipped")
        return {}, {}

    raw = read_raw(GRANTS_TABLE).fillna("")
    # Clean the text once, here, so every use downstream -- stored columns,
    # relevance tiers, keyword matching -- sees decoded text ('β', not '&#946;').
    for column in ("project_title", "abstract_text"):
        raw[column] = raw[column].map(lambda v: clean_text(v) or "")
    raw["award_key_"] = [
        award_key(r.core_project_num, r.project_num, r.activity_code)
        for r in raw.itertuples(index=False)
    ]
    # Earliest fiscal year first, so the award's headline fields come from the
    # record that opened it rather than whichever row pandas happened to see.
    raw["fy_"] = raw["fiscal_year"].map(safe_int)
    raw = raw.sort_values("fy_", kind="stable")

    award_ids = {}
    funding_ids = {}
    award_by_appl = {}
    dropped_no_context = 0
    for key, group in raw.groupby("award_key_", sort=False):
        award_hits = []
        first = group.iloc[0]
        years = [y for y in group["fy_"] if y is not None]
        abstracts = [a for a in group["abstract_text"] if a]

        award_id = conn.execute(
            text(
                """
                INSERT INTO core.grant_award
                    (award_key, core_project_num, project_title, activity_code,
                     admin_ic_code, admin_ic_name, org_name, org_city, org_state,
                     org_country, contact_pi_name, project_start_date, project_end_date,
                     first_fiscal_year, last_fiscal_year, relevance_tier, source_id)
                VALUES
                    (:award_key, :core_project_num, :project_title, :activity_code,
                     :admin_ic_code, :admin_ic_name, :org_name, :org_city, :org_state,
                     :org_country, :contact_pi_name, :project_start_date, :project_end_date,
                     :first_fiscal_year, :last_fiscal_year, :relevance_tier, :source_id)
                RETURNING grant_award_id
                """
            ),
            {
                "award_key": key,
                "core_project_num": first["core_project_num"],
                "project_title": blank_to_none(first["project_title"]),
                "activity_code": blank_to_none(first["activity_code"]),
                "admin_ic_code": blank_to_none(first["agency_ic_admin_code"]),
                "admin_ic_name": blank_to_none(first["agency_ic_admin_abbreviation"]),
                "org_name": blank_to_none(first["organization_org_name"]),
                "org_city": blank_to_none(first["organization_org_city"]),
                "org_state": blank_to_none(first["organization_org_state"]),
                "org_country": blank_to_none(first["organization_org_country"]),
                "contact_pi_name": contact_pi(first),
                "project_start_date": safe_date(first["project_start_date"]),
                "project_end_date": safe_date(first["project_end_date"]),
                "first_fiscal_year": min(years) if years else None,
                "last_fiscal_year": max(years) if years else None,
                "relevance_tier": relevance_tier(list(group["project_title"]), abstracts),
                "source_id": sid,
            },
        ).scalar()
        award_ids[key] = award_id
        add_xref(xref_rows, "grant_award", award_id, sid, "nih_core_project_num",
                 first["core_project_num"])
        add_provenance(provenance_rows, "grant_award", award_id, sid, GRANTS_TABLE, key)

        for r in group.itertuples(index=False):
            funding_id = conn.execute(
                text(
                    """
                    INSERT INTO core.grant_funding
                        (grant_award_id, fiscal_year, project_num, subproject_id,
                         project_title, abstract_text, award_amount, award_notice_date,
                         project_detail_url, source_id)
                    VALUES
                        (:grant_award_id, :fiscal_year, :project_num, :subproject_id,
                         :project_title, :abstract_text, :award_amount, :award_notice_date,
                         :project_detail_url, :source_id)
                    RETURNING grant_funding_id
                    """
                ),
                {
                    "grant_award_id": award_id,
                    "fiscal_year": r.fy_,
                    "project_num": blank_to_none(r.project_num),
                    "subproject_id": blank_to_none(r.subproject_id),
                    "project_title": blank_to_none(r.project_title),
                    "abstract_text": blank_to_none(r.abstract_text),
                    "award_amount": safe_float(r.award_amount),
                    "award_notice_date": safe_date(r.award_notice_date),
                    "project_detail_url": blank_to_none(r.project_detail_url),
                    "source_id": sid,
                },
            ).scalar()
            funding_ids[r.appl_id] = funding_id
            award_by_appl[r.appl_id] = award_id
            add_xref(xref_rows, "grant_funding", funding_id, sid, "nih_appl_id", r.appl_id)
            add_xref(xref_rows, "grant_funding", funding_id, sid, "nih_project_num", r.project_num)
            add_provenance(provenance_rows, "grant_funding", funding_id, sid, GRANTS_TABLE, r.appl_id)

            # Keyword hits are buffered per award, not added per fiscal year:
            # the organism-context rule is a property of the whole grant, and a
            # grant that names "Ebola virus" in one year's abstract has
            # established context for an ambiguous term found in another's.
            for field, value in (("title", r.project_title), ("abstract", r.abstract_text)):
                for hit in matcher.match(value, field):
                    award_hits.append((hit, funding_id))

        has_context = organism_context_established([h for h, _ in award_hits])
        for hit, funding_id in award_hits:
            if not keep_hit(hit, has_context):
                dropped_no_context += 1
                continue
            # The support row still names the fiscal year the term was found in,
            # so a term appearing in only one year stays attributable to it.
            links.add(
                "grant_award", award_id, hit["entity_type"], hit["entity_id"], "related",
                hit["confidence"], sid,
                _support(hit["match_field"], hit["match_method"], hit["matched_term"],
                         hit["term_type"], hit["requires_context"], funding_id),
            )

    # total_award_amount from the same rule as core.v_grant_award_total: parent
    # rows only, because a P01/U19 parent already includes its subprojects.
    conn.execute(
        text(
            """
            UPDATE core.grant_award a
            SET total_award_amount = v.total_award_amount,
                total_is_partial  = coalesce(v.total_is_partial, FALSE)
            FROM core.v_grant_award_total v
            WHERE v.grant_award_id = a.grant_award_id
            """
        )
    )

    print(f"core.grant_award: {len(award_ids)} rows")
    print(f"core.grant_funding: {len(funding_ids)} rows")
    if dropped_no_context:
        print(f"  {dropped_no_context} ambiguous-term hits dropped "
              "(grant names no organism; see organism_context_established)")
    return award_ids, award_by_appl


def is_filovirus_paper(title: str, mesh_terms: str, abstract: str) -> bool:
    """
    Whether a grant-cited paper is about filoviruses: a filovirus term in the
    title or MeSH headings, or 2+ mentions in the abstract.

    One abstract mention is not enough. On the current data that rule would add
    170 papers, and they are mostly passing mentions ("blocked replication of
    other RNA viruses, including Ebola virus"). Two mentions is the same
    threshold relevance_tier() uses for a 'moderate' grant.
    """
    if FILOVIRUS_TITLE_RE.search(title or "") or FILOVIRUS_TITLE_RE.search(mesh_terms or ""):
        return True
    return len(FILOVIRUS_TITLE_RE.findall(abstract or "")) >= 2


UPSERT_CITED_PUBLICATION_SQL = text(
    """
    INSERT INTO core.publication
        (pubmed_id, title, abstract, mesh_terms, authors, journal, year, doi, url, source_id)
    VALUES (:pubmed_id, :title, :abstract, :mesh_terms, :authors, :journal, :year, :doi, :url, :source_id)
    ON CONFLICT (pubmed_id) DO UPDATE
        SET title      = COALESCE(core.publication.title, EXCLUDED.title),
            abstract   = COALESCE(core.publication.abstract, EXCLUDED.abstract),
            mesh_terms = COALESCE(core.publication.mesh_terms, EXCLUDED.mesh_terms),
            authors    = COALESCE(core.publication.authors, EXCLUDED.authors),
            journal    = COALESCE(core.publication.journal, EXCLUDED.journal),
            year       = COALESCE(core.publication.year, EXCLUDED.year),
            doi        = COALESCE(core.publication.doi, EXCLUDED.doi),
            url        = COALESCE(core.publication.url, EXCLUDED.url),
            -- A paper both a grant and a patent cite belongs to the grant
            -- route, which runs first; one left owned by the patent route on
            -- an earlier run is handed over, or the patent route's cleanup
            -- could delete a paper a grant still cites.
            source_id  = CASE WHEN core.publication.source_id = :yield_sid
                              THEN EXCLUDED.source_id
                              ELSE COALESCE(core.publication.source_id, EXCLUDED.source_id) END
    RETURNING publication_id
    """
)


def _upsert_cited_paper(conn, r, sid, yield_sid=None) -> int:
    pmid = blank_to_none(r.pmid)
    return conn.execute(
        UPSERT_CITED_PUBLICATION_SQL,
        {
            "pubmed_id": pmid,
            "title": clean_text(r.title),
            "abstract": clean_text(r.abstract),
            "mesh_terms": blank_to_none(r.mesh_terms),
            "authors": blank_to_none(r.authors),
            "journal": blank_to_none(r.journal),
            "year": blank_to_none(r.year),
            "doi": blank_to_none(r.doi),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "source_id": sid,
            "yield_sid": yield_sid,
        },
    ).scalar()


def _delete_stale_cited_papers(conn, sid, pmids) -> int:
    """
    Rows a cited-paper route added on an earlier run that it no longer keeps.
    evidence_link was truncated at the start of the run, so nothing points at
    them yet; study_publication is ImmPort's and is left alone.
    """
    return conn.execute(
        text(
            """
            DELETE FROM core.publication p
            WHERE p.source_id = :sid
              AND NOT (p.pubmed_id = ANY(:pmids))
              AND NOT EXISTS (SELECT 1 FROM core.study_publication sp
                              WHERE sp.publication_id = p.publication_id)
            """
        ),
        {"sid": sid, "pmids": list(pmids)},
    ).rowcount


def build_cited_papers(conn, xref_rows, provenance_rows, sid, bundle_pmids=(), patent_sid=None):
    """
    core.publication rows for the filovirus papers grants cite that the papers
    workstream didn't reach. Without them build_grant_publication_links drops
    every citation outside the original 300 (22,431 of 22,667).

    These get source_id = nih_reporter, not pubmed: the source records how a
    paper entered the KG, and these entered through a grant. It also keeps the
    'raw->core: papers' check in sql/checks.sql an exact count of the bundle.

    Upserted like build_papers, since a cited paper can already exist (ImmPort,
    or the papers bundle -- fetchers/cited_publications.py excludes the bundle,
    but a rerun against a newer bundle may overlap). Rows this function added
    on an earlier run that no longer pass the filter are deleted, so tightening
    is_filovirus_paper actually removes papers rather than leaving them behind.
    """
    if not raw_table_exists(CITED_PUBS_TABLE):
        print(f"cited papers: raw.{CITED_PUBS_TABLE} absent, skipped "
              "(run fetchers/cited_publications.py)")
        return

    raw = read_raw(CITED_PUBS_TABLE).fillna("")
    kept = raw[[is_filovirus_paper(r.title, r.mesh_terms, r.abstract)
                for r in raw.itertuples(index=False)]]

    pmids = []
    for r in kept.itertuples(index=False):
        pmid = blank_to_none(r.pmid)
        # A bundle paper is already a core.publication row owned by
        # build_papers; claiming it here would duplicate its lineage.
        if pmid is None or pmid in bundle_pmids:
            continue
        publication_id = _upsert_cited_paper(conn, r, sid, yield_sid=patent_sid)
        pmids.append(pmid)
        add_xref(xref_rows, "publication", publication_id, sid, "pubmed_id", pmid)
        add_provenance(provenance_rows, "publication", publication_id, sid, CITED_PUBS_TABLE, pmid)

    removed = _delete_stale_cited_papers(conn, sid, pmids)
    print(f"cited papers: {len(pmids)} of {len(raw)} grant-cited papers kept as filovirus papers"
          + (f", {removed} no longer passing removed" if removed else ""))
    return set(pmids)


def build_patent_cited_papers(conn, xref_rows, provenance_rows, sid, claimed_pmids=()):
    """
    core.publication rows for every paper a patent cites, resolved from its
    free-text references by fetchers/patent_cited_publications.py. No relevance
    filter -- see the comment below for why this differs from grants.

    source_id = uspto: the paper entered the KG through a patent. Papers the
    bundle or a grant already brought in (claimed_pmids) are left to those
    routes -- they still get their cites edge, from
    build_patent_publication_links, which resolves against all of
    core.publication.
    """
    if not raw_table_exists(PATENT_CITED_PUBS_TABLE):
        print(f"patent-cited papers: raw.{PATENT_CITED_PUBS_TABLE} absent, skipped "
              "(run fetchers/patent_cited_publications.py)")
        return
    raw = read_raw(PATENT_CITED_PUBS_TABLE).fillna("")
    resolved = raw[raw["pmid"] != ""].drop_duplicates("pmid")
    # Every resolved reference is kept, filovirus or not -- unlike grant
    # citations (is_filovirus_paper). The team decided 2026-11-10: a patent's
    # references are a short, examiner-vetted list (154 across 39 patents, not
    # a P51 centre's thousands), and the off-topic ones are the prior art the
    # invention builds on (HIV gp41 fusion peptides behind a filovirus fusion
    # inhibitor) -- real provenance, even though those papers link to nothing
    # else in this KG.
    kept = resolved

    pmids = []
    for r in kept.itertuples(index=False):
        if r.pmid in claimed_pmids:
            continue
        publication_id = _upsert_cited_paper(conn, r, sid)
        pmids.append(r.pmid)
        add_xref(xref_rows, "publication", publication_id, sid, "pubmed_id", r.pmid)
        add_provenance(provenance_rows, "publication", publication_id, sid,
                       PATENT_CITED_PUBS_TABLE, r.pmid)

    removed = _delete_stale_cited_papers(conn, sid, pmids)
    print(f"patent-cited papers: {raw['citation_text'].nunique()} references, "
          f"{resolved['pmid'].nunique()} resolved to PubMed, "
          f"{len(pmids)} new to core.publication"
          + (f", {removed} no longer passing removed" if removed else ""))


def build_patent_publication_links(conn, links, patent_ids, sid):
    """
    patent -> paper 'cites' edges: the patents workstream's own PMID-resolved
    matches, plus every reference fetchers/patent_cited_publications.py resolved.
    A citation is dropped, not guessed, when the paper is not in
    core.publication (off-topic, or never resolved).
    """
    pub_ids = dict(conn.execute(text("SELECT pubmed_id, publication_id FROM core.publication")).fetchall())
    rows = []
    if raw_table_exists(PATENT_TEAM_PAPERS_TABLE):
        for r in read_raw(PATENT_TEAM_PAPERS_TABLE).fillna("").itertuples(index=False):
            rows.append((r.patent_id, r.pmid, r.citation_text or r.team_paper_title,
                         f"patents_workstream:{r.match_type}"))
    if raw_table_exists(PATENT_CITED_PUBS_TABLE):
        for r in read_raw(PATENT_CITED_PUBS_TABLE).fillna("").itertuples(index=False):
            if r.pmid:
                rows.append((r.patent_id, r.pmid, r.citation_text, f"pubmed_resolved:{r.resolve_method}"))
    if not rows:
        print("patent -> paper links: no citation tables, skipped")
        return

    linked = unmatched_patent = unmatched_pub = 0
    for patent_doc, pmid, citation, how in rows:
        patent_id = patent_ids.get(normalize_document_id(patent_doc))
        publication_id = pub_ids.get(blank_to_none(pmid))
        if patent_id is None:
            unmatched_patent += 1
            continue
        if publication_id is None:
            unmatched_pub += 1
            continue
        links.add("patent", patent_id, "publication", publication_id, "cites",
                  CONFIDENCE["identifier"], sid,
                  _support("identifier", "citation", matched_term=clean_text(citation),
                           term_type=how))
        linked += 1
    print(f"patent -> paper: {linked} citations linked "
          f"({unmatched_patent} unmatched patent, {unmatched_pub} paper not in core.publication)")


def build_grant_publication_links(conn, links, award_ids, sid):
    """
    grant -> paper edges from RePORTER's own publication list. This is the only
    direct document-to-document edge in the evidence layer; everything else
    connects to an entity, not to another document.
    """
    if not raw_table_exists(GRANT_PUBS_TABLE):
        print("grant -> paper links: raw.nih_reporter_grant_publications absent, skipped "
              "(run fetchers/grant_publications.py)")
        return
    raw = read_raw(GRANT_PUBS_TABLE).fillna("")
    pub_ids = dict(conn.execute(text("SELECT pubmed_id, publication_id FROM core.publication")).fetchall())

    linked = unmatched_award = unmatched_pub = 0
    for r in raw.itertuples(index=False):
        award_id = award_ids.get(r.core_project_num)
        if award_id is None:
            # RePORTER lists publications for every year of a contract under the
            # truncated core number, which award_key() deliberately splits.
            unmatched_award += 1
            continue
        publication_id = pub_ids.get(str(r.pmid))
        if publication_id is None:
            # Cited, but neither reached from the KG nor kept by
            # build_cited_papers (off-topic, or the fetch hasn't been run).
            unmatched_pub += 1
            continue
        links.add("grant_award", award_id, "publication", publication_id, "cites",
                  CONFIDENCE["identifier"], sid,
                  _support("identifier", "citation"))
        linked += 1

    print(f"grant -> paper: {linked} links "
          f"({unmatched_award} unmatched award, {unmatched_pub} paper not in core.publication)")


# "Fever" inside a disease name is not the symptom: "Ebola hemorrhagic fever",
# "yellow fever virus". Unmasked, every grant about VHF links to HP:0001945.
# Masked out of the text before a symptom keyword is checked. 'ha?emorr?hagic'
# because abstracts misspell it ("Crimean Congo Hemorhagic Fever").
DISEASE_NAME_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:viral\s+)?(?:ha?emorr?hagic|yellow|dengue|lassa|rift\s+valley|"
    r"crimean[\s-]+congo|scarlet|typhoid|rheumatic|hay|q|marburg|ebola)\s+fevers?"
    r"(?:\s+virus(?:es)?)?(?![0-9A-Za-z])",
    re.IGNORECASE,
)
CLINICAL_ENTITY_TYPES = ("condition", "symptom", "assay_type")


def build_grant_clinical_links(conn, links, award_by_appl, sid):
    """
    grant -> condition / symptom / assay_type keyword links from a teammate's
    grant_entity_edges.csv. etl.evidence's own matcher covers taxa and genes
    only, so these are the file's new information; its taxon and gene rows
    duplicate what build_grants computes (apart from taxonomy-version genus
    ids) and are ignored.

    Not taken on trust. Each row is re-checked against the grant text stored
    in core.grant_funding: a row survives only if one of its keywords occurs
    as a whole word in the field it claims, and for symptoms only outside a
    disease name (DISEASE_NAME_RE). The support row names the fiscal year the
    term was found in. Entities resolve by ontology id (DOID, HP, OBI), never
    by the file's ids; conditions not in core.condition are dropped.

    Awards resolve through the RePORTER appl_id in each row's URL, because the
    file keys contracts differently from award_key(). The appl_id -> award map
    comes from build_grants in this same run, NOT from core.xref: xref rows are
    only rewritten at the end of the run, so reading them here would see the
    previous run's ids -- or, on a fresh database, none at all (which silently
    dropped all 349 hits).
    """
    if not raw_table_exists(GRANT_ENTITY_EDGES_TABLE):
        return
    raw = read_raw(GRANT_ENTITY_EDGES_TABLE).fillna("")
    raw = raw[raw["matched_entity_type"].isin(CLINICAL_ENTITY_TYPES)]

    funding_text = {}
    for award_id, funding_id, title, abstract in conn.execute(text(
        "SELECT grant_award_id, grant_funding_id, project_title, abstract_text "
        "FROM core.grant_funding ORDER BY fiscal_year"
    )):
        funding_text.setdefault(award_id, []).append((funding_id, title or "", abstract or ""))

    entity_ids = {
        "condition": dict(conn.execute(text(
            "SELECT ontology_id, min(condition_id) FROM core.condition "
            "WHERE ontology_id IS NOT NULL GROUP BY 1")).fetchall()),
        "symptom": dict(conn.execute(text("SELECT hpo_id, symptom_id FROM core.symptom")).fetchall()),
        "assay_type": dict(conn.execute(text(
            "SELECT obi_id, assay_type_id FROM core.assay_type WHERE obi_id IS NOT NULL")).fetchall()),
    }
    assay_by_name = {n.lower(): i for i, n in conn.execute(
        text("SELECT assay_type_id, name FROM core.assay_type"))}

    kept = 0
    dropped = {}
    for r in raw.itertuples(index=False):
        appl = re.search(r"project-details/(\d+)", r.url)
        award_id = award_by_appl.get(appl.group(1)) if appl else None
        entity_id = entity_ids[r.matched_entity_type].get(r.matched_entity_id)
        if entity_id is None and r.matched_entity_type == "assay_type":
            entity_id = assay_by_name.get(r.entity_name.lower())
        if award_id is None or entity_id is None:
            reason = "award not in core.grant_award" if award_id is None else \
                f"{r.matched_entity_type} {r.matched_entity_id} not in core"
            dropped[reason] = dropped.get(reason, 0) + 1
            continue

        found = None
        for term in filter(None, (t.strip() for t in r.matched_keyword.split(";"))):
            pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z])", re.IGNORECASE)
            for funding_id, title, abstract in funding_text.get(award_id, []):
                value = title if r.match_field == "title" else abstract
                if r.matched_entity_type == "symptom":
                    value = DISEASE_NAME_RE.sub(" ", value)
                if pattern.search(value):
                    found = (term, funding_id)
                    break
            if found:
                break
        if found is None:
            reason = f"{r.matched_entity_type} keyword not in grant text"
            dropped[reason] = dropped.get(reason, 0) + 1
            continue

        requires_context = r.requires_context.lower() == "true"
        confidence = CONFIDENCE[r.match_field] * (AMBIGUOUS_PENALTY if requires_context else 1)
        links.add("grant_award", award_id, r.matched_entity_type, entity_id, "related",
                  round(confidence, 2), sid,
                  _support(r.match_field, r.match_method if r.match_method in ("keyword", "synonym") else "keyword",
                           found[0], "teammate_grant_entity_edges", requires_context, found[1]))
        kept += 1

    print(f"grant -> condition/symptom/assay_type: {kept} of {len(raw)} hits kept")
    for reason, count in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"  ! dropped {count}: {reason}")


def build_grant_ack_links(conn, links, award_ids, sid):
    """
    grant -> paper 'cites' edges from the paper side: the grant numbers a
    PubMed record acknowledges (link_basis 'pubmed_ack' in the teammate's
    grant_paper_divergence.csv). Most duplicate a RePORTER edge and only add a
    support row; the rest are citations RePORTER does not list. The file's
    divergence statistics are analysis output and are not stored.
    """
    if not raw_table_exists(GRANT_DIVERGENCE_TABLE):
        return
    raw = read_raw(GRANT_DIVERGENCE_TABLE).fillna("")
    raw = raw[raw["link_basis"].str.contains("pubmed_ack")]
    pub_ids = dict(conn.execute(text("SELECT pubmed_id, publication_id FROM core.publication")).fetchall())
    linked = skipped = 0
    for r in raw.itertuples(index=False):
        award_id, publication_id = award_ids.get(r.award_key), pub_ids.get(r.pmid)
        if award_id is None or publication_id is None:
            skipped += 1
            continue
        links.add("grant_award", award_id, "publication", publication_id, "cites",
                  CONFIDENCE["identifier"], sid,
                  _support("identifier", "citation", term_type="pubmed_grant_acknowledgement"))
        linked += 1
    print(f"grant -> paper (PubMed acknowledgements): {linked} links, {skipped} unresolved")


# ---------------------------------------------------------------------------
# Same-type document references: paper -> paper, patent -> patent, grant -> grant
# ---------------------------------------------------------------------------

# An NIH core project number, e.g. R01AI123456 / U19 AI109762.
CORE_PROJECT_RE = re.compile(r"\b([A-Z]\d{2})\s?([A-Z]{2})\s?(\d{6})\b")
# The institute + serial part of a core project number (AI102347 in
# R43AI102347): the same project across its funding stages -- R43 -> R44
# (small-business phase I -> II), R56 -> R01, R21 -> R33.
PROJECT_SERIAL_RE = re.compile(r"^[A-Z]\d{2}([A-Z]{2}\d{6})$")
# Preprint servers, as PubMed names their journal.
PREPRINT_JOURNAL_RE = re.compile(r"biorxiv|medrxiv|research square|arxiv|ssrn|preprints", re.IGNORECASE)


def build_document_citations(conn, links, patent_ids, source_ids):
    """
    Documents that reference another document of the same kind, as direct
    edges -- only when both ends are in the graph, never guessed:

      paper  -cites->   paper   its PubMed reference list (fetch paper-citations)
      patent -cites->   patent  its Google Patents citation tables, read from both
                                ends: A's "cites" and B's "cited by" (fetch
                                patent-details)
      patent -related-> patent  the same patent family: an application and the
                                patent later granted for it
      grant  -cites->   grant   a grant's own text naming another grant's core
                                project number
      grant  -related-> grant   the same NIH project at another funding stage:
                                same institute + serial, different activity
                                code (R43AI102347 -> R44AI102347), earlier ->
                                later
      paper  -related-> paper   a preprint and its published version: the same
                                title, one in a preprint server's "journal"
    """
    counts = {}

    def add(kind, etype, eid, tid, relation, sid, field, term, term_type):
        if eid is None or tid is None or eid == tid:
            return
        links.add(etype, eid, etype, tid, relation, CONFIDENCE["identifier"], sid,
                  _support(field, "exact_id", term, term_type))
        counts[kind] = counts.get(kind, 0) + 1

    if raw_table_exists(PAPER_CITATIONS_TABLE):
        pub_ids = dict(conn.execute(text("SELECT pubmed_id, publication_id FROM core.publication")).fetchall())
        for r in read_raw(PAPER_CITATIONS_TABLE).itertuples(index=False):
            add("paper cites paper", "publication", pub_ids.get(r.pmid), pub_ids.get(r.cited_pmid),
                "cites", source_ids["pubmed"], "identifier", f"PMID {r.cited_pmid}", "pubmed_reference_list")

    if raw_table_exists(PATENT_DETAILS_TABLE):
        details = read_raw(PATENT_DETAILS_TABLE).fillna("")
        if "cited_patents" in details.columns:
            for r in details.itertuples(index=False):
                this = patent_ids.get(normalize_document_id(r.document_id))
                for number in filter(None, r.cited_patents.split(";")):
                    add("patent cites patent", "patent", this, patent_ids.get(normalize_document_id(number)),
                        "cites", source_ids["uspto"], "identifier", number, "google_patents_citation")
                for number in filter(None, r.cited_by_patents.split(";")):
                    add("patent cites patent", "patent", patent_ids.get(normalize_document_id(number)), this,
                        "cites", source_ids["uspto"], "identifier", r.document_id, "google_patents_citation")

    # Same family: the application (A*) points at the granted patent (B*).
    families = {}
    for patent_id, family_id, kind in conn.execute(text(
            "SELECT patent_id, family_id, kind_code FROM core.patent WHERE family_id IS NOT NULL")):
        families.setdefault(family_id, []).append((kind or "", patent_id))
    for family_id, members in families.items():
        applications = [p for k, p in members if k.startswith("A")]
        granted = [p for k, p in members if k.startswith("B")]
        for a in applications:
            for g in granted:
                add("patent same family", "patent", a, g, "related", source_ids["uspto"],
                    "identifier", f"family {family_id}", "same_patent_family")

    # A grant naming another grant's core project number in its own text.
    by_core = {}
    for core, award_id in conn.execute(text("SELECT core_project_num, grant_award_id FROM core.grant_award")):
        by_core.setdefault(core, set()).add(award_id)
    for award_id, funding_id, title, abstract in conn.execute(text(
            "SELECT grant_award_id, grant_funding_id, project_title, abstract_text FROM core.grant_funding")):
        for field, value in (("title", title or ""), ("abstract", abstract or "")):
            for m in CORE_PROJECT_RE.finditer(value):
                targets = by_core.get("".join(m.groups()), set())
                if len(targets) == 1:   # a truncated contract number can mean several awards
                    target = next(iter(targets))
                    if target != award_id:
                        links.add("grant_award", award_id, "grant_award", target, "cites",
                                  CONFIDENCE["identifier"], source_ids["nih_reporter"],
                                  _support(field, "exact_id", m.group(0), "grant_number_in_text", False, funding_id))
                        counts["grant cites grant"] = counts.get("grant cites grant", 0) + 1

    # One NIH project across funding stages.
    stages = {}
    for award_id, core, first_year in conn.execute(text(
            "SELECT grant_award_id, core_project_num, first_fiscal_year FROM core.grant_award")):
        m = PROJECT_SERIAL_RE.match(core or "")
        if m:
            stages.setdefault(m.group(1), []).append((first_year or 0, core, award_id))
    for serial, awards in stages.items():
        awards.sort()
        for (_, core_a, a), (_, core_b, b) in zip(awards, awards[1:]):
            if core_a != core_b:
                add("grant same project", "grant_award", a, b, "related", source_ids["nih_reporter"],
                    "identifier", f"{core_a} -> {core_b}", "same_nih_project_serial")

    # A preprint and its published version.
    by_title = {}
    for publication_id, title, journal in conn.execute(text(
            "SELECT publication_id, title, journal FROM core.publication WHERE title IS NOT NULL")):
        key = " ".join(re.sub(r"[^0-9a-z]+", " ", title.lower()).split())
        by_title.setdefault(key, []).append((publication_id, bool(PREPRINT_JOURNAL_RE.search(journal or ""))))
    for papers in by_title.values():
        preprints = [p for p, is_pre in papers if is_pre]
        published = [p for p, is_pre in papers if not is_pre]
        for pre in preprints:
            for pub in published:
                add("preprint of paper", "publication", pre, pub, "related", source_ids["pubmed"],
                    "identifier", "identical title", "preprint_of_published")

    print("same-type document links: " + (", ".join(f"{v} {k}" for k, v in counts.items()) or "none"))


# ---------------------------------------------------------------------------
# Grant -> patent: the patent's federal-support statement
# ---------------------------------------------------------------------------

# An NIH research grant inside free text: optional type/activity prefix, the
# institute code, the serial: "AI066487", "R43 AI066487-01", "U19 AI-109762".
# Patents also write the serial without its leading zero ("AI 51292" is
# AI051292) and misprint the institute ("A1057153" for AI057153).
GRANT_IN_TEXT_RE = re.compile(r"(?<![A-Z0-9])(?:\d?[A-Z]\d{2}\s?)?([A-Z][A-Z1])\s?-?\s?(\d{5,6})(?!\d)")


def grant_serial(match) -> str:
    """'AI051292' from a GRANT_IN_TEXT_RE match: institute fixed, serial zero-padded."""
    institute = "AI" if match.group(1) == "A1" else match.group(1)
    return institute + match.group(2).zfill(6)
# NIH contracts: "HHSN272201400058C", "HHSN-261-2015-00003I", "75N93020C00012".
CONTRACT_IN_TEXT_RE = re.compile(r"HHSN[\s-]?([\d\s-]{9,16})[A-Z]?|(75N9\d{4}[A-Z]\d{5})")


def build_grant_patent_links(conn, links, patent_ids, sid):
    """
    grant -funds-> patent, from the patent's own statement of government
    interest ("made with government support under AI066487 awarded by NIH"),
    fetched with `filovirus fetch patent-details`. Only exact numbers of
    grants in the graph link; DoD, DARPA and other agencies' numbers have no
    grant node to point at and are left alone.
    """
    if not raw_table_exists(PATENT_DETAILS_TABLE):
        return
    details = read_raw(PATENT_DETAILS_TABLE).fillna("")
    if "government_interest" not in details.columns:
        print("grant -> patent: no government-interest statements fetched, skipped")
        return

    by_serial, contracts = {}, []
    for award_id, core, award_key in conn.execute(text(
            "SELECT grant_award_id, core_project_num, award_key FROM core.grant_award")):
        m = PROJECT_SERIAL_RE.match(core or "")
        if m:
            by_serial.setdefault(m.group(1), set()).add(award_id)
        else:
            contracts.append((re.sub(r"[^0-9A-Z]", "", (award_key or "").upper()), award_id))
    institutes = {serial[:2] for serial in by_serial}

    statements = linked = 0
    for r in details.itertuples(index=False):
        statement = r.government_interest
        patent_id = patent_ids.get(normalize_document_id(r.document_id))
        if not statement or patent_id is None:
            continue
        statements += 1
        targets = {}
        for m in GRANT_IN_TEXT_RE.finditer(statement):
            serial = grant_serial(m)
            if serial[:2] in institutes:
                for award_id in by_serial.get(serial, ()):
                    targets[award_id] = m.group(0).strip()
        for m in CONTRACT_IN_TEXT_RE.finditer(statement):
            number = re.sub(r"[^0-9A-Z]", "", (m.group(1) or m.group(2)).upper())
            for key, award_id in contracts:
                if len(number) >= 9 and key.startswith(number[:12]):
                    targets[award_id] = m.group(0).strip()
        for award_id, number in targets.items():
            links.add("grant_award", award_id, "patent", patent_id, "funds", CONFIDENCE["identifier"], sid,
                      _support("identifier", "exact_id", number, "patent_government_interest"))
            linked += 1
    print(f"grant -> patent: {statements} patents state federal support; {linked} links to grants in the graph")


# ---------------------------------------------------------------------------
# Countermeasures named in documents
# ---------------------------------------------------------------------------

def build_countermeasure_links(conn, links, source_ids):
    """
    grant / patent / paper -related-> countermeasure, where the document's
    title or abstract names it (curated/countermeasures.py `names`, matched as
    whole words, case-insensitively). Product names are specific -- "ZMapp",
    "REGN-EB3", "remdesivir" -- so no ambiguity penalty applies.
    """
    patterns = [
        (cid, re.compile(r"(?<![0-9A-Za-z])(" + "|".join(re.escape(n) for n in names) + r")(?![0-9A-Za-z])",
                         re.IGNORECASE))
        for cid, names in conn.execute(text("SELECT countermeasure_id, names FROM core.countermeasure"))
    ]
    if not patterns:
        return
    counts = {}

    def scan(etype, eid, sid, fields, funding_id=None):
        for field, value in fields:
            for cid, pattern in patterns:
                m = pattern.search(value or "")
                if m:
                    links.add(etype, eid, "countermeasure", cid, "related", CONFIDENCE[field], sid,
                              _support(field, "keyword", m.group(1), "countermeasure_name", False, funding_id))
                    counts[etype] = counts.get(etype, 0) + 1

    for award_id, funding_id, title, abstract in conn.execute(text(
            "SELECT grant_award_id, grant_funding_id, project_title, abstract_text FROM core.grant_funding")):
        scan("grant_award", award_id, source_ids["nih_reporter"], (("title", title), ("abstract", abstract)), funding_id)
    for patent_id, title, abstract in conn.execute(text("SELECT patent_id, title, abstract FROM core.patent")):
        scan("patent", patent_id, source_ids["uspto"], (("title", title), ("abstract", abstract)))
    for pub_id, title, abstract in conn.execute(text("SELECT publication_id, title, abstract FROM core.publication")):
        scan("publication", pub_id, source_ids["pubmed"], (("title", title), ("abstract", abstract)))
    print("document -> countermeasure: " + ", ".join(f"{v} {k} mentions" for k, v in counts.items()))


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------

def build_organizations(conn):
    """
    core.organization from grant recipients and patent assignees/applicants,
    merged across spellings by curated/organizations.py, and the
    grant/patent -> organization link tables.
    """
    from core_data_loader.curated.organizations import display_name, kind_of, normalize, split_names

    spellings, grant_rows, patent_rows = {}, [], []
    for award_id, org in conn.execute(text("SELECT grant_award_id, org_name FROM core.grant_award")):
        for name in split_names(org):
            key = normalize(name)
            spellings.setdefault(key, []).append(name)
            grant_rows.append((award_id, key))
    for patent_id, assignee, applicant in conn.execute(text(
            "SELECT patent_id, assignee, applicant_name FROM core.patent")):
        for role, field in (("assignee", assignee), ("applicant", applicant)):
            for name in split_names(field):
                key = normalize(name)
                spellings.setdefault(key, []).append(name)
                patent_rows.append((patent_id, key, role))

    ids = {}
    for key in sorted(spellings):
        ids[key] = conn.execute(text(
            "INSERT INTO core.organization (org_key, name, kind, spellings) VALUES (:k, :n, :kind, :s) "
            "RETURNING organization_id"),
            {"k": key, "n": display_name(key, spellings[key]), "kind": kind_of(key),
             "s": sorted(set(spellings[key]))}).scalar()
    for award_id, key in set(grant_rows):
        conn.execute(text("INSERT INTO core.grant_award_organization VALUES (:a, :o) ON CONFLICT DO NOTHING"),
                     {"a": award_id, "o": ids[key]})
    for patent_id, key, role in set(patent_rows):
        conn.execute(text("INSERT INTO core.patent_organization VALUES (:p, :o, :r) ON CONFLICT DO NOTHING"),
                     {"p": patent_id, "o": ids[key], "r": role})
    grant_keys = {k for _, k in grant_rows}
    patent_keys = {k for _, k, _ in patent_rows}
    print(f"core.organization: {len(ids)} rows ({len(grant_keys)} grant recipients, "
          f"{len(patent_keys)} patent owners, {len(grant_keys & patent_keys)} both)")


# ---------------------------------------------------------------------------
# Patents
# ---------------------------------------------------------------------------

KIND_CODE_RE = re.compile(r"\s([A-Z]\d?)$")

# The organism each export was searched for, as an NCBI taxon id. Species rank,
# matching the convention in curated/taxon_conditions.py, so the link reaches
# strain-level taxa through core.taxon_condition_inherited's tree.
QUERY_ORGANISM_TAXA = {
    "ebola": 186536,    # Ebolavirus (genus) -- the search was not species-specific
    "marburg": 186537,  # Marburgvirus (genus)
}


DOCUMENT_ID_RE = re.compile(r"^\s*([A-Z]{2})\s*(\d+)\s*([A-Z]\d?)\s*$")


def normalize_document_id(value: str) -> str | None:
    """
    'US20180291088A1' and 'US 20180291088 A1' are the same patent; the
    patents workstream's files use both. core.patent stores the spaced form
    the USPTO export uses.
    """
    m = DOCUMENT_ID_RE.match(value or "")
    return f"{m.group(1)} {m.group(2)} {m.group(3)}" if m else blank_to_none(value)


def _family_id(value: str) -> str | None:
    """The expanded file's family_id went through a float ('78828615.0')."""
    value = blank_to_none(value)
    return value[:-2] if value and value.endswith(".0") else value


def _patent_groups():
    """
    (document_id, rows) for every patent, from the four CPC exports and, when
    present, the expanded file's additional patents. The expanded file repeats
    the 72 exported patents with identical fields; for those the export stays
    authoritative because only it carries query_organism. Expanded rows are
    renamed onto the export's column names so one insert path serves both;
    raw_table_ / raw_pk_ keep each row's real origin for its lineage row.
    """
    raw = read_raw(PATENTS_TABLE).fillna("")
    raw["raw_table_"], raw["raw_pk_"] = PATENTS_TABLE, raw["document_id"]
    groups = list(raw.groupby("document_id", sort=False))
    if not raw_table_exists(PATENTS_EXPANDED_TABLE):
        return groups, 0

    seen = {doc for doc, _ in groups}
    expanded = read_raw(PATENTS_EXPANDED_TABLE).fillna("")
    expanded["raw_table_"], expanded["raw_pk_"] = PATENTS_EXPANDED_TABLE, expanded["patent_id"]
    expanded = expanded.rename(columns={"patent_id": "document_id", "date": "date_published"})
    expanded["document_id"] = expanded["document_id"].map(normalize_document_id)
    expanded["family_id"] = expanded["family_id"].map(lambda v: _family_id(v) or "")
    # No query_organism: these came from an abstract search, not a per-organism
    # CPC export, so they get no query_scope taxon link.
    expanded["query_organism"] = ""
    added = expanded[~expanded["document_id"].isin(seen)]
    return groups + list(added.groupby("document_id", sort=False)), added["document_id"].nunique()


def _patent_abstract_hits(matcher):
    """
    document_id -> keyword hits in the patent's abstract, from the patents
    workstream's entity-link file. The abstracts were not supplied, so each
    matched keyword is re-run through the shared matcher: that resolves it to
    a live entity exactly as a title hit would be, and drops any keyword the
    vocabulary does not carry. Title rows are ignored -- build_patents matches
    titles itself.
    """
    if not raw_table_exists(PATENT_LINKS_TABLE):
        return {}
    hits = {}
    for r in read_raw(PATENT_LINKS_TABLE).fillna("").itertuples(index=False):
        if r.match_location != "abstract":
            continue
        doc = normalize_document_id(r.patent_id)
        hits.setdefault(doc, []).extend(matcher.match(r.matched_keyword, "abstract"))
    return hits


def _patent_details() -> dict:
    """
    document_id -> abstract and dates from each patent's Google Patents page
    (`filovirus fetch patent-details`). Empty when the file wasn't fetched.
    """
    if not raw_table_exists(PATENT_DETAILS_TABLE):
        return {}
    return {normalize_document_id(r["document_id"]): r
            for r in read_raw(PATENT_DETAILS_TABLE).fillna("").to_dict("records")}


def build_patents(conn, matcher, links, xref_rows, provenance_rows, sid):
    if not raw_table_exists(PATENTS_TABLE):
        print("core.patent: raw.uspto_patents absent, skipped")
        return {}

    taxon_ids = {row[0] for row in conn.execute(text("SELECT taxon_id FROM core.taxon"))}
    groups, expanded_count = _patent_groups()
    abstract_hits = _patent_abstract_hits(matcher)
    details = _patent_details()
    filled_dates = 0

    patent_ids = {}
    patent_count = cpc_count = dropped_no_context = 0
    for document_id, group in groups:
        first = group.iloc[0]
        kind = KIND_CODE_RE.search(document_id)
        # The USPTO export is authoritative; the patent's own page only fills
        # what the export left blank (the expanded search had no dates at all).
        detail = details.get(document_id, {})
        date_published = safe_date(first["date_published"])
        if date_published is None and safe_date(detail.get("publication_date")):
            date_published = safe_date(detail["publication_date"])
            filled_dates += 1
        abstract = clean_text(detail.get("abstract"))

        patent_id = conn.execute(
            text(
                """
                INSERT INTO core.patent
                    (document_id, kind_code, title, family_id, application_num,
                     date_published, filing_date, priority_date, inventor, assignee, applicant_name,
                     cpc_inventive, cpc_additional, page_count, abstract, source_id)
                VALUES
                    (:document_id, :kind_code, :title, :family_id, :application_num,
                     :date_published, :filing_date, :priority_date, :inventor, :assignee, :applicant_name,
                     :cpc_inventive, :cpc_additional, :page_count, :abstract, :source_id)
                RETURNING patent_id
                """
            ),
            {
                "document_id": document_id,
                "kind_code": kind.group(1) if kind else None,
                "title": clean_text(first["title"]),
                "family_id": blank_to_none(first["family_id"]),
                "application_num": blank_to_none(first["application_number"]),
                "date_published": date_published,
                "filing_date": safe_date(first["filing_date"]) or safe_date(detail.get("filing_date")),
                "priority_date": safe_date(detail.get("priority_date")),
                "inventor": blank_to_none(first["inventor"]),
                "assignee": blank_to_none(first["assignee"]),
                "applicant_name": blank_to_none(first["applicant_name"]),
                "cpc_inventive": blank_to_none(first["cpci"]),
                "cpc_additional": blank_to_none(first["cpca"]),
                "page_count": safe_int(first["pages"]),
                "abstract": abstract,
                "source_id": sid,
            },
        ).scalar()
        patent_count += 1
        patent_ids[document_id] = patent_id
        add_xref(xref_rows, "patent", patent_id, sid, "uspto_document_id", document_id)
        add_xref(xref_rows, "patent", patent_id, sid, "uspto_application_num", first["application_number"])
        add_provenance(provenance_rows, "patent", patent_id, sid, first["raw_table_"], first["raw_pk_"])
        if detail:
            add_provenance(provenance_rows, "patent", patent_id, sid, PATENT_DETAILS_TABLE, detail["document_id"])

        for column, kind_label in (("cpci", "inventive"), ("cpca", "additional")):
            for code in str(first[column]).split(";"):
                code = blank_to_none(code)
                if not code:
                    continue
                cpc_count += conn.execute(
                    text(
                        """
                        INSERT INTO core.patent_cpc (patent_id, cpc_code, cpc_kind)
                        VALUES (:patent_id, :cpc_code, :cpc_kind)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"patent_id": patent_id, "cpc_code": code, "cpc_kind": kind_label},
                ).rowcount

        # Which search the row came from. Weaker than a title match and recorded
        # as match_field='query_scope', because it is a property of the query
        # that found the patent, not of any text in the record. Added before the
        # title hits so it counts as organism context for them.
        patent_hits = []
        for organism in sorted(set(group["query_organism"])):
            taxon_id = QUERY_ORGANISM_TAXA.get(organism)
            if taxon_id in taxon_ids:
                patent_hits.append({
                    "entity_type": "taxon", "entity_id": str(taxon_id),
                    "matched_term": organism, "term_type": "query_organism",
                    "requires_context": True, "match_field": "query_scope",
                    "match_method": "manual", "confidence": CONFIDENCE["query_scope"],
                })

        # Title keywords. 'claims' rather than 'related' for the entities a
        # patent can actually claim -- an antibody or a gene product named in the
        # title is the subject matter, not an incidental mention.
        patent_hits.extend(matcher.match(first["title"], "title"))
        patent_hits.extend(abstract_hits.get(document_id, []))
        if abstract:
            patent_hits.extend(matcher.match(abstract, "abstract"))

        has_context = organism_context_established(patent_hits)
        for hit in patent_hits:
            if not keep_hit(hit, has_context):
                dropped_no_context += 1
                continue
            relation = ("claims" if hit["entity_type"] in ("antibody", "gene", "protein")
                        else "related")
            links.add("patent", patent_id, hit["entity_type"], hit["entity_id"], relation,
                      hit["confidence"], sid,
                      _support(hit["match_field"], hit["match_method"], hit["matched_term"],
                               hit["term_type"], hit["requires_context"]))

    if details:
        print(f"  patent details: {sum(1 for d in details.values() if d.get('abstract'))} abstracts, "
              f"{filled_dates} publication dates filled from Google Patents")
    print(f"core.patent: {patent_count} rows ({expanded_count} from the expanded search), "
          f"core.patent_cpc: {cpc_count} rows")
    if dropped_no_context:
        print(f"  {dropped_no_context} ambiguous-term hits dropped (patent names no organism)")
    return patent_ids


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------

def build_papers(conn, links, xref_rows, provenance_rows, sid, cited_sid=None, patent_sid=None):
    if not raw_table_exists(PAPERS_TABLE):
        print("core.publication: raw.papers_unique absent, skipped")
        return {}

    raw = read_raw(PAPERS_TABLE).fillna("")

    # Bibliographic metadata, if fetchers/publication_metadata.py has been run.
    # Without it a paper has no title (so its graph node is labelled "PMID
    # 28854372", which tells an LLM nothing) and no year (so it is invisible to
    # every temporal split).
    metadata = {}
    if raw_table_exists(PAPER_META_TABLE):
        for r in read_raw(PAPER_META_TABLE).fillna("").itertuples(index=False):
            metadata[blank_to_none(r.pmid)] = r
        print(f"  publication metadata available for {len(metadata)} papers")
    else:
        print("  (no raw.papers_metadata; papers will have no title or year -- "
              "run fetchers/publication_metadata.py)")

    publication_ids = {}
    inserted = 0
    for r in raw.itertuples(index=False):
        pmid = blank_to_none(r.pmid)
        if pmid is None:
            continue
        # Upsert, not truncate: core.publication is shared with etl.immport, and
        # a paper reached by a PDB citation, an IEDB reference and a keyword
        # search must stay one row. COALESCE keeps whatever ImmPort already
        # supplied (title, authors, journal) rather than blanking it.
        meta = metadata.get(pmid)
        publication_id = conn.execute(
            text(
                """
                INSERT INTO core.publication
                    (pubmed_id, title, authors, journal, year, doi, url, source_id)
                VALUES (:pubmed_id, :title, :authors, :journal, :year, :doi, :url, :source_id)
                ON CONFLICT (pubmed_id) DO UPDATE
                    SET title     = COALESCE(core.publication.title, EXCLUDED.title),
                        authors   = COALESCE(core.publication.authors, EXCLUDED.authors),
                        journal   = COALESCE(core.publication.journal, EXCLUDED.journal),
                        year      = COALESCE(core.publication.year, EXCLUDED.year),
                        doi       = COALESCE(core.publication.doi, EXCLUDED.doi),
                        url       = COALESCE(core.publication.url, EXCLUDED.url),
                        -- A paper that first entered as a grant citation and is
                        -- now in the bundle belongs to the bundle; otherwise
                        -- build_cited_papers would treat it as its own and could
                        -- delete it. Any other existing source (ImmPort) is kept.
                        source_id = CASE WHEN core.publication.source_id IN (:cited_sid, :patent_sid)
                                         THEN EXCLUDED.source_id
                                         ELSE COALESCE(core.publication.source_id, EXCLUDED.source_id) END
                RETURNING publication_id
                """
            ),
            {
                "pubmed_id": pmid,
                "title": clean_text(meta.title) if meta is not None else None,
                "authors": blank_to_none(meta.authors) if meta is not None else None,
                "journal": blank_to_none(meta.journal) if meta is not None else None,
                "year": blank_to_none(meta.year) if meta is not None else None,
                # The papers file's own DOI wins when present; PubMed's fills gaps.
                "doi": blank_to_none(r.doi) or (blank_to_none(meta.doi) if meta is not None else None),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source_id": sid,
                "cited_sid": cited_sid,
                "patent_sid": patent_sid,
            },
        ).scalar()
        publication_ids[pmid] = publication_id
        inserted += 1
        add_xref(xref_rows, "publication", publication_id, sid, "pubmed_id", pmid)
        add_provenance(provenance_rows, "publication", publication_id, sid, PAPERS_TABLE, pmid)

    print(f"core.publication: {inserted} papers upserted")
    return publication_ids


class EntityResolver:
    """
    Maps each source's own identifier, as the papers workstream reports it, to a
    live core primary key. Every lookup is by a stable natural key -- an
    accession, a PDB id, a name -- never by a core surrogate id, since those are
    regenerated by the upstream ETLs on every run.
    """

    def __init__(self, conn):
        self.conn = conn
        self.isolate_by_genome_id = self._xref_map("isolate", "bvbrc_genome_id")
        self.epitope_by_iedb = self._xref_map("epitope", "iedb_id")
        self.epitope_by_bvbrc = self._xref_map("epitope", "bvbrc_epitope_id")
        self.epitope_by_sequence = dict(conn.execute(text(
            "SELECT upper(epitope_sequence), min(epitope_id) FROM core.epitope "
            "WHERE epitope_sequence IS NOT NULL GROUP BY 1"
        )).fetchall())
        self.structure_by_pdb = dict(conn.execute(text(
            "SELECT upper(pdb_id), min(structure_id) FROM core.structure "
            "WHERE pdb_id IS NOT NULL GROUP BY 1"
        )).fetchall())
        self.study_accessions = {
            row[0] for row in conn.execute(text("SELECT study_accession FROM core.study"))
        }
        self.antibody_by_name = {}
        for aid, name, alias in conn.execute(text("SELECT antibody_id, name, alias FROM core.antibody")):
            for label in [name] + (alias.split(",") if alias else []):
                label = blank_to_none(label)
                if label:
                    self.antibody_by_name.setdefault(label.lower(), aid)
        # LANL's EBOV_features rows land in core.feature with the feature label
        # as `product`; BV-BRC/NCBI features have the same column but never
        # these labels, so the lookup stays unambiguous.
        self.feature_by_product = dict(conn.execute(text(
            "SELECT lower(product), min(feature_id) FROM core.feature "
            "WHERE product IS NOT NULL GROUP BY 1"
        )).fetchall())

    def _xref_map(self, entity_type, xref_type):
        return dict(self.conn.execute(
            text(
                "SELECT xref_value, min(entity_id) FROM core.xref "
                "WHERE entity_type = :e AND xref_type = :x GROUP BY 1"
            ),
            {"e": entity_type, "x": xref_type},
        ).fetchall())

    def resolve(self, source_entity_type, source_entity):
        value = blank_to_none(source_entity)
        if value is None:
            return None, None
        entity_type = PAPER_ENTITY_TYPES.get(source_entity_type)
        if entity_type is None:
            return None, None

        if entity_type == "isolate":
            return entity_type, self.isolate_by_genome_id.get(value)
        if entity_type == "epitope":
            # Numeric ids are IEDB's (BV-BRC mirrors the same numbers); the
            # handful of non-numeric values are LANL peptide sequences.
            if value.isdigit():
                return entity_type, (self.epitope_by_iedb.get(value)
                                     or self.epitope_by_bvbrc.get(value))
            return entity_type, self.epitope_by_sequence.get(value.upper())
        if entity_type == "structure":
            return entity_type, self.structure_by_pdb.get(value.upper())
        if entity_type == "antibody":
            return entity_type, self.antibody_by_name.get(value.lower())
        if entity_type == "feature":
            return entity_type, self.feature_by_product.get(value.lower())
        if entity_type == "study":
            return entity_type, value if value in self.study_accessions else None
        return None, None


# The papers file attributes each link to the source it was found in; those map
# onto core.source so an IEDB-derived link isn't credited to BV-BRC.
PAPER_LINK_SOURCES = {
    "BV-BRC": "bvbrc",
    "IEDB": "iedb",
    "LANL HFV": "hfv_lanl",
    "PDB (via BV-BRC)": "bvbrc",
    "RCSB PDB": "rcsb_pdb",
    "ImmPort": "immport_sdy1373",
}

# grant/patent links are 'related'; a paper that reports a structure or epitope
# describes it. 'study' keeps the ImmPort wording.
PAPER_RELATION = {"study": "describes", "isolate": "related"}


def build_paper_links(conn, links, publication_ids, source_ids, default_sid):
    if not raw_table_exists(PAPER_REFS_TABLE):
        print("paper links: raw.papers_references absent, skipped")
        return

    raw = read_raw(PAPER_REFS_TABLE).fillna("")
    resolver = EntityResolver(conn)

    linked = 0
    skipped = {}
    for r in raw.itertuples(index=False):
        publication_id = publication_ids.get(blank_to_none(r.pmid))
        if publication_id is None:
            skipped["paper not in unique_papers"] = skipped.get("paper not in unique_papers", 0) + 1
            continue
        if r.entity_type not in PAPER_ENTITY_TYPES:
            # 'source_note': cites the spreadsheet, not an entity in it.
            skipped[f"entity_type={r.entity_type}"] = skipped.get(f"entity_type={r.entity_type}", 0) + 1
            continue

        entity_type, entity_id = resolver.resolve(r.entity_type, r.source_entity)
        if entity_id is None:
            key = f"unresolved {r.entity_type}"
            skipped[key] = skipped.get(key, 0) + 1
            continue

        sid = source_ids.get(PAPER_LINK_SOURCES.get(r.source, ""), default_sid)
        links.add("publication", publication_id, entity_type, entity_id,
                  PAPER_RELATION.get(entity_type, "describes"),
                  CONFIDENCE["identifier"], sid,
                  _support("identifier", "exact_id", matched_term=r.source_entity,
                           term_type=r.source))
        linked += 1

    print(f"paper links: {linked} resolved")
    for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  ! skipped {count}: {reason}")


# ---------------------------------------------------------------------------

ORPHAN_CHECK_SQL = """
SELECT entity_type, count(*) FROM core.evidence_link el
WHERE (entity_type = 'taxon'     AND NOT EXISTS (SELECT 1 FROM core.taxon     t WHERE t.taxon_id::text     = el.entity_id))
   OR (entity_type = 'gene'      AND NOT EXISTS (SELECT 1 FROM core.gene      g WHERE g.gene_id::text      = el.entity_id))
   OR (entity_type = 'protein'   AND NOT EXISTS (SELECT 1 FROM core.protein   p WHERE p.protein_id::text   = el.entity_id))
   OR (entity_type = 'epitope'   AND NOT EXISTS (SELECT 1 FROM core.epitope   e WHERE e.epitope_id::text   = el.entity_id))
   OR (entity_type = 'antibody'  AND NOT EXISTS (SELECT 1 FROM core.antibody  a WHERE a.antibody_id::text  = el.entity_id))
   OR (entity_type = 'condition' AND NOT EXISTS (SELECT 1 FROM core.condition c WHERE c.condition_id::text = el.entity_id))
   OR (entity_type = 'structure' AND NOT EXISTS (SELECT 1 FROM core.structure s WHERE s.structure_id::text = el.entity_id))
   OR (entity_type = 'isolate'   AND NOT EXISTS (SELECT 1 FROM core.isolate   i WHERE i.isolate_id::text   = el.entity_id))
   OR (entity_type = 'countermeasure' AND NOT EXISTS (SELECT 1 FROM core.countermeasure c WHERE c.countermeasure_id::text = el.entity_id))
   OR (entity_type = 'sequence'  AND NOT EXISTS (SELECT 1 FROM core.sequence  q WHERE q.sequence_id::text  = el.entity_id))
   OR (entity_type = 'feature'   AND NOT EXISTS (SELECT 1 FROM core.feature   f WHERE f.feature_id::text   = el.entity_id))
   OR (entity_type = 'study'     AND NOT EXISTS (SELECT 1 FROM core.study     y WHERE y.study_accession    = el.entity_id))
   OR (entity_type = 'publication' AND NOT EXISTS (SELECT 1 FROM core.publication u WHERE u.publication_id::text = el.entity_id))
   OR (entity_type = 'grant_award' AND NOT EXISTS (SELECT 1 FROM core.grant_award g WHERE g.grant_award_id::text = el.entity_id))
   OR (entity_type = 'patent'      AND NOT EXISTS (SELECT 1 FROM core.patent      p WHERE p.patent_id::text      = el.entity_id))
   OR (entity_type = 'symptom'     AND NOT EXISTS (SELECT 1 FROM core.symptom     s WHERE s.symptom_id::text     = el.entity_id))
   OR (entity_type = 'assay_type'  AND NOT EXISTS (SELECT 1 FROM core.assay_type  a WHERE a.assay_type_id::text  = el.entity_id))
GROUP BY entity_type
"""


def main():
    source_ids = ensure_sources()
    xref_rows = []
    provenance_rows = []
    links = LinkBuilder()

    # core.publication is NOT truncated -- it is shared with etl.immport and
    # papers are upserted onto it. Everything else here is owned outright.
    truncate(
        "core.evidence_link_support", "core.evidence_link",
        "core.grant_award_organization", "core.patent_organization", "core.organization",
        "core.patent_cpc", "core.patent",
        "core.grant_funding", "core.grant_award",
    )

    matcher = KeywordMatcher()
    print(f"kg_keywords: {len(matcher.terms)} terms resolved to live core entities")

    with engine.begin() as conn:
        award_ids, award_by_appl = build_grants(conn, matcher, links, xref_rows, provenance_rows,
                                                 source_ids["nih_reporter"])
        build_grant_clinical_links(conn, links, award_by_appl, source_ids["nih_reporter"])
        patent_ids = build_patents(conn, matcher, links, xref_rows, provenance_rows, source_ids["uspto"])
        publication_ids = build_papers(conn, links, xref_rows, provenance_rows,
                                        source_ids["pubmed"], source_ids["nih_reporter"],
                                        source_ids["uspto"])
        build_paper_links(conn, links, publication_ids, source_ids, source_ids["pubmed"])
        # Before the citation links: those resolve PMIDs against core.publication.
        grant_cited = build_cited_papers(conn, xref_rows, provenance_rows, source_ids["nih_reporter"],
                                         bundle_pmids=set(publication_ids),
                                         patent_sid=source_ids["uspto"])
        build_patent_cited_papers(conn, xref_rows, provenance_rows, source_ids["uspto"],
                                  claimed_pmids=set(publication_ids) | (grant_cited or set()))
        build_grant_publication_links(conn, links, award_ids, source_ids["nih_reporter"])
        build_grant_ack_links(conn, links, award_ids, source_ids["nih_reporter"])
        build_patent_publication_links(conn, links, patent_ids, source_ids["uspto"])
        build_document_citations(conn, links, patent_ids, source_ids)
        build_grant_patent_links(conn, links, patent_ids, source_ids["nih_reporter"])
        build_countermeasure_links(conn, links, source_ids)
        build_organizations(conn)

        link_rows, support_rows = links.write(conn, provenance_rows)
        print(f"core.evidence_link: {link_rows} rows")
        print(f"core.evidence_link_support: {support_rows} rows")

        reviewed = apply_reviews(conn)
        if reviewed:
            print(f"  {reviewed} links carry a saved human review (ops.link_review)")

        orphans = conn.execute(text(ORPHAN_CHECK_SQL)).fetchall()
        # A publication with no edge at all is invisible to any graph query.
        # Currently 1: a paper whose only reference was a 'source_note' citing
        # the LANL spreadsheet itself rather than anything in it.
        isolated_papers = conn.execute(text(
            """
            SELECT count(*) FROM core.publication p
            WHERE NOT EXISTS (SELECT 1 FROM core.evidence_link el
                              WHERE el.evidence_type = 'publication' AND el.evidence_id = p.publication_id)
              AND NOT EXISTS (SELECT 1 FROM core.evidence_link el
                              WHERE el.entity_type = 'publication' AND el.entity_id = p.publication_id::text)
              AND NOT EXISTS (SELECT 1 FROM core.study_publication sp
                              WHERE sp.publication_id = p.publication_id)
            """
        )).scalar()

    write_xref(xref_rows, XREF_ENTITY_TYPES_OWNED)
    # core.publication is shared with etl.immport, so this script replaces only
    # the lineage rows it is responsible for -- pubmed (the papers bundle),
    # nih_reporter and uspto (papers that entered through a grant's or a
    # patent's citations).
    write_provenance(provenance_rows, PROVENANCE_TABLES_OWNED,
                      shared_tables={"publication": [source_ids["pubmed"],
                                                     source_ids["nih_reporter"],
                                                     source_ids["uspto"]]})

    if orphans:
        for entity_type, count in orphans:
            print(f"  ! {count} evidence_link rows point at a missing {entity_type}")
    else:
        print("orphan check: 0 dangling evidence_link rows")

    if isolated_papers:
        print(f"  {isolated_papers} publications carry no link of any kind "
              "(only cited as a source_note)")

    print("Finished evidence ETL.")


if __name__ == "__main__":
    main()

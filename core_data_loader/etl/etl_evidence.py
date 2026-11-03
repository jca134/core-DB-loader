"""
Builds the evidence layer -- grants, patents, papers, and the links from each
to entities already in core.* -- out of raw.nih_reporter_grants,
raw.uspto_patents and raw.papers_*.

Run LAST, after every other ETL script. Links are resolved to live primary
keys, and most of those keys are surrogate: etl_bvbrc_hfv regenerates
core.epitope/structure/antibody ids, etl_ucsc regenerates core.gene ids, and
etl_immport regenerates core.condition ids. Those scripts truncate with
RESTART IDENTITY, so a rerun over unchanged source data reproduces the same
ids harmlessly -- but any upstream data change shifts them, and the links this
script wrote then point at the wrong rows. Rerun it after any upstream ETL
(same standing requirement as etl_taxon_condition).

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

core.publication is shared with etl_immport (which truncates it), so papers are
upserted on pubmed_id rather than truncated and rebuilt -- a paper found by
three routes stays one row, per the team's dedup requirement.
"""

import json
import re

from sqlalchemy import text

from core_data_loader.common.etl_common import (
    engine, ensure_sources, truncate, read_raw, raw_table_exists,
    safe_int, safe_float, safe_date, blank_to_none,
    add_provenance, write_provenance, add_xref, write_xref,
)
from core_data_loader.common.kg_keywords import KeywordMatcher, CONFIDENCE

# Owned outright -- replaced wholesale on every run. core.publication is NOT
# here: etl_immport writes lineage for it too, so it is passed as a shared
# table scoped to this script's own source_id instead.
PROVENANCE_TABLES_OWNED = ["grant_award", "grant_funding", "patent", "evidence_link"]
XREF_ENTITY_TYPES_OWNED = ["grant_award", "grant_funding", "patent", "publication"]

GRANTS_TABLE = "nih_reporter_grants"
GRANT_PUBS_TABLE = "nih_reporter_grant_publications"
PATENTS_TABLE = "uspto_patents"
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
        return {}

    raw = read_raw(GRANTS_TABLE).fillna("")
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
    return award_ids


def build_grant_publication_links(conn, links, award_ids, sid):
    """
    grant -> paper edges from RePORTER's own publication list. This is the only
    direct document-to-document edge in the evidence layer; everything else
    connects through a shared entity (see core.v_evidence_cooccurrence).
    """
    if not raw_table_exists(GRANT_PUBS_TABLE):
        print("grant -> paper links: raw.nih_reporter_grant_publications absent, skipped "
              "(run fetch_grant_publications.py)")
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
            # The paper is cited by the grant but isn't one of the 300 papers
            # reached from the KG, so there is no core.publication row to point at.
            unmatched_pub += 1
            continue
        links.add("grant_award", award_id, "publication", publication_id, "cites",
                  CONFIDENCE["identifier"], sid,
                  _support("identifier", "citation"))
        linked += 1

    print(f"grant -> paper: {linked} links "
          f"({unmatched_award} unmatched award, {unmatched_pub} paper not in core.publication)")


# ---------------------------------------------------------------------------
# Patents
# ---------------------------------------------------------------------------

KIND_CODE_RE = re.compile(r"\s([A-Z]\d?)$")

# The organism each export was searched for, as an NCBI taxon id. Species rank,
# matching the convention in common/taxon_conditions.py, so the link reaches
# strain-level taxa through core.taxon_condition_inherited's tree.
QUERY_ORGANISM_TAXA = {
    "ebola": 186536,    # Ebolavirus (genus) -- the search was not species-specific
    "marburg": 186537,  # Marburgvirus (genus)
}


def build_patents(conn, matcher, links, xref_rows, provenance_rows, sid):
    if not raw_table_exists(PATENTS_TABLE):
        print("core.patent: raw.uspto_patents absent, skipped")
        return

    raw = read_raw(PATENTS_TABLE).fillna("")
    taxon_ids = {row[0] for row in conn.execute(text("SELECT taxon_id FROM core.taxon"))}

    patent_count = cpc_count = dropped_no_context = 0
    for document_id, group in raw.groupby("document_id", sort=False):
        first = group.iloc[0]
        kind = KIND_CODE_RE.search(document_id)

        patent_id = conn.execute(
            text(
                """
                INSERT INTO core.patent
                    (document_id, kind_code, title, family_id, application_num,
                     date_published, filing_date, inventor, assignee, applicant_name,
                     cpc_inventive, cpc_additional, page_count, source_id)
                VALUES
                    (:document_id, :kind_code, :title, :family_id, :application_num,
                     :date_published, :filing_date, :inventor, :assignee, :applicant_name,
                     :cpc_inventive, :cpc_additional, :page_count, :source_id)
                RETURNING patent_id
                """
            ),
            {
                "document_id": document_id,
                "kind_code": kind.group(1) if kind else None,
                "title": blank_to_none(first["title"]),
                "family_id": blank_to_none(first["family_id"]),
                "application_num": blank_to_none(first["application_number"]),
                "date_published": safe_date(first["date_published"]),
                "filing_date": safe_date(first["filing_date"]),
                "inventor": blank_to_none(first["inventor"]),
                "assignee": blank_to_none(first["assignee"]),
                "applicant_name": blank_to_none(first["applicant_name"]),
                "cpc_inventive": blank_to_none(first["cpci"]),
                "cpc_additional": blank_to_none(first["cpca"]),
                "page_count": safe_int(first["pages"]),
                "source_id": sid,
            },
        ).scalar()
        patent_count += 1
        add_xref(xref_rows, "patent", patent_id, sid, "uspto_document_id", document_id)
        add_xref(xref_rows, "patent", patent_id, sid, "uspto_application_num", first["application_number"])
        add_provenance(provenance_rows, "patent", patent_id, sid, PATENTS_TABLE, document_id)

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

    print(f"core.patent: {patent_count} rows, core.patent_cpc: {cpc_count} rows")
    if dropped_no_context:
        print(f"  {dropped_no_context} ambiguous-term hits dropped (patent names no organism)")


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------

def build_papers(conn, links, xref_rows, provenance_rows, sid):
    if not raw_table_exists(PAPERS_TABLE):
        print("core.publication: raw.papers_unique absent, skipped")
        return {}

    raw = read_raw(PAPERS_TABLE).fillna("")

    # Bibliographic metadata, if fetch_publication_metadata.py has been run.
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
              "run fetch_publication_metadata.py)")

    publication_ids = {}
    inserted = 0
    for r in raw.itertuples(index=False):
        pmid = blank_to_none(r.pmid)
        if pmid is None:
            continue
        # Upsert, not truncate: core.publication is shared with etl_immport, and
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
                        source_id = COALESCE(core.publication.source_id, EXCLUDED.source_id)
                RETURNING publication_id
                """
            ),
            {
                "pubmed_id": pmid,
                "title": blank_to_none(meta.title) if meta is not None else None,
                "authors": blank_to_none(meta.authors) if meta is not None else None,
                "journal": blank_to_none(meta.journal) if meta is not None else None,
                "year": blank_to_none(meta.year) if meta is not None else None,
                # The papers file's own DOI wins when present; PubMed's fills gaps.
                "doi": blank_to_none(r.doi) or (blank_to_none(meta.doi) if meta is not None else None),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source_id": sid,
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
   OR (entity_type = 'feature'   AND NOT EXISTS (SELECT 1 FROM core.feature   f WHERE f.feature_id::text   = el.entity_id))
   OR (entity_type = 'study'     AND NOT EXISTS (SELECT 1 FROM core.study     y WHERE y.study_accession    = el.entity_id))
   OR (entity_type = 'publication' AND NOT EXISTS (SELECT 1 FROM core.publication u WHERE u.publication_id::text = el.entity_id))
GROUP BY entity_type
"""


def main():
    source_ids = ensure_sources()
    xref_rows = []
    provenance_rows = []
    links = LinkBuilder()

    # core.publication is NOT truncated -- it is shared with etl_immport and
    # papers are upserted onto it. Everything else here is owned outright.
    truncate(
        "core.evidence_link_support", "core.evidence_link",
        "core.patent_cpc", "core.patent",
        "core.grant_funding", "core.grant_award",
    )

    matcher = KeywordMatcher()
    print(f"kg_keywords: {len(matcher.terms)} terms resolved to live core entities")

    with engine.begin() as conn:
        award_ids = build_grants(conn, matcher, links, xref_rows, provenance_rows,
                                  source_ids["nih_reporter"])
        build_patents(conn, matcher, links, xref_rows, provenance_rows, source_ids["uspto"])
        publication_ids = build_papers(conn, links, xref_rows, provenance_rows,
                                        source_ids["pubmed"])
        build_paper_links(conn, links, publication_ids, source_ids, source_ids["pubmed"])
        build_grant_publication_links(conn, links, award_ids, source_ids["nih_reporter"])

        link_rows, support_rows = links.write(conn, provenance_rows)
        print(f"core.evidence_link: {link_rows} rows")
        print(f"core.evidence_link_support: {support_rows} rows")

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
    # core.publication is shared with etl_immport, so this script replaces only
    # the lineage rows it is responsible for -- the ones attributed to pubmed.
    write_provenance(provenance_rows, PROVENANCE_TABLES_OWNED,
                      shared_tables={"publication": [source_ids["pubmed"]]})

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

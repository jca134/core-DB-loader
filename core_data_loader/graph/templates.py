"""
Ready-made questions for the query page: free, no AI involved.

Each template is a fixed, reviewed Cypher query with blanks the user fills
from a dropdown (or, for `text`, a search box). Values reach Neo4j only as
query parameters, never pasted into the Cypher, and dropdown values are
checked against the dropdown's own options before a query runs.

To add a question: append to TEMPLATES, use $<param name> in the Cypher,
alias every returned column with the header the page should show, and add a
case to tests/test_query_templates.py's live-graph check if it needs one.
A column named `Link` is rendered as a clickable link.
"""

# Viruses as a researcher names them, not as the taxonomy does. Each entry
# lists every taxon the graph files that virus under: NCBI renamed the genera
# (Ebolavirus -> Orthoebolavirus, Marburgvirus -> Orthomarburgvirus) and the
# sources link to both, so picking one id would silently miss records.
# Templates walk CHILD_OF down from these roots, so strains are included.
VIRUSES = [
    ("filoviridae", "All filoviruses (Filoviridae)", [11266]),
    ("ebola_all", "Ebola viruses, all species (Orthoebolavirus)", [3044781, 186536]),
    ("zaire", "Ebola virus (Zaire ebolavirus)", [3052462]),
    ("sudan", "Sudan virus", [3052460]),
    ("bundibugyo", "Bundibugyo virus", [3052458]),
    ("tai_forest", "Taï Forest virus", [3052461]),
    ("reston", "Reston virus", [3052459]),
    ("bombali", "Bombali virus", [3052457]),
    ("marburg_all", "Marburg viruses (Orthomarburgvirus)", [3044783, 186537]),
    ("lloviu", "Lloviu virus (Cuevavirus)", [1513236]),
    ("mengla", "Mengla / Dehong viruses (Dianlovirus)", [2733240]),
]
VIRUS_IDS = {key: ids for key, _label, ids in VIRUSES}

# Dropdown sources for each parameter type. 'virus' is the curated list above.
OPTION_QUERIES = {
    "gene": "MATCH (g:Gene) RETURN g.gene_symbol AS value, g.gene_symbol AS label ORDER BY label",
    "gene_family": "MATCH (f:GeneFamily) RETURN f.symbol AS value, f.name AS label ORDER BY f.gene_family_id",
    "countermeasure": "MATCH (c:Countermeasure) RETURN c.countermeasure_id AS value, c.name AS label ORDER BY label",
    # Ontology-backed only: ImmPort also files a vaccine trial name as a "condition".
    "condition": "MATCH (c:Condition) WHERE c.ontology_id IS NOT NULL "
                 "RETURN c.condition_id AS value, c.name AS label ORDER BY label",
    "symptom": "MATCH (s:Symptom) RETURN s.symptom_id AS value, s.name AS label ORDER BY label",
    "assay_type": "MATCH (a:AssayType) RETURN a.assay_type_id AS value, a.name AS label ORDER BY label",
}

# Every taxon at or below the chosen virus's roots.
_SUBTREE = """MATCH (t:Taxon)-[:CHILD_OF*0..]->(root:Taxon) WHERE root.taxon_id IN $virus
WITH collect(DISTINCT t) AS taxa
"""

_PATENT_LINK = "'https://patents.google.com/patent/' + replace(p.document_id, ' ', '')"

TEMPLATES = [
    # --- by virus -------------------------------------------------------------
    {
        "id": "grants_for_virus", "group": "Viruses",
        "question": "Which NIH grants study {virus}?",
        "params": [{"name": "virus", "type": "virus"}],
        "cypher": _SUBTREE + """MATCH (g:Grant)-[r:RELATED]->(t) WHERE t IN taxa
WITH g, max(r.confidence) AS conf
OPTIONAL MATCH (f:GrantFunding)-[:FUNDING_OF]->(g)
WITH g, conf, max(f.project_detail_url) AS link
RETURN g.project_title AS Grant, g.org_name AS Institution,
       g.first_fiscal_year AS `From`, g.last_fiscal_year AS `To`,
       g.total_award_amount AS `Total awarded ($)`, conf AS Confidence, link AS Link
ORDER BY conf DESC, g.last_fiscal_year DESC LIMIT 200""",
    },
    {
        "id": "patents_for_virus", "group": "Viruses",
        "question": "Which patents are about {virus}?",
        "params": [{"name": "virus", "type": "virus"}],
        "cypher": _SUBTREE + f"""MATCH (p:Patent)-[r:RELATED]->(t) WHERE t IN taxa
WITH p, max(r.confidence) AS conf
RETURN p.title AS Patent, p.document_id AS Number, p.assignee AS Assignee,
       coalesce(p.date_published, p.filing_date) AS Date, conf AS Confidence,
       {_PATENT_LINK} AS Link
ORDER BY conf DESC, Date DESC LIMIT 200""",
    },
    {
        "id": "papers_for_virus", "group": "Viruses",
        "question": "Which papers describe {virus} isolates, epitopes or structures?",
        "params": [{"name": "virus", "type": "virus"}],
        "cypher": _SUBTREE + """MATCH (pub:Publication)-[:RELATED|DESCRIBES]->(x)-[:OF_TAXON]->(t) WHERE t IN taxa
WITH pub, count(DISTINCT x) AS items
RETURN pub.title AS Paper, pub.journal AS Journal, pub.year AS Year,
       items AS `Isolates / epitopes / structures`, pub.url AS Link
ORDER BY items DESC LIMIT 200""",
    },
    {
        "id": "diseases_for_virus", "group": "Viruses",
        "question": "Which diseases does {virus} cause?",
        "params": [{"name": "virus", "type": "virus"}],
        "cypher": _SUBTREE + """MATCH (t)-[:CAUSES]->(c:Condition) WHERE t IN taxa
RETURN c.name AS Disease, c.ontology_id AS `Ontology id`, count(DISTINCT t) AS `Taxa (incl. strains)`
ORDER BY Disease""",
    },
    {
        "id": "isolates_by_country", "group": "Viruses",
        "question": "Where were {virus} isolates collected?",
        "params": [{"name": "virus", "type": "virus"}],
        "cypher": _SUBTREE + """MATCH (i:Isolate)-[:OF_TAXON]->(t) WHERE t IN taxa
RETURN coalesce(i.country, '(not recorded)') AS Country, count(*) AS Isolates,
       min(i.collection_year) AS `First year`, max(i.collection_year) AS `Last year`
ORDER BY Isolates DESC""",
    },
    {
        "id": "countermeasures_for_virus", "group": "Viruses",
        "question": "Which vaccines, antibodies and antivirals act against {virus}?",
        "params": [{"name": "virus", "type": "virus"}],
        "cypher": _SUBTREE + """MATCH (c:Countermeasure)-[:TARGETS]->(t) WHERE t IN taxa
WITH DISTINCT c
OPTIONAL MATCH (d)-[:RELATED]->(c)
RETURN c.name AS Countermeasure, c.kind AS Kind, c.status AS Status, c.regulatory AS Approved,
       c.developer AS Developer, count(DISTINCT d) AS `Grants, patents & papers naming it`
ORDER BY CASE c.status WHEN 'licensed' THEN 0 WHEN 'clinical' THEN 1 ELSE 2 END, Countermeasure""",
    },
    # --- countermeasures ---------------------------------------------------------
    {
        "id": "documents_for_countermeasure", "group": "Countermeasures and organizations",
        "question": "Which grants, patents and papers name {countermeasure}?",
        "params": [{"name": "countermeasure", "type": "countermeasure"}],
        "cypher": """MATCH (c:Countermeasure {countermeasure_id: $countermeasure})<-[r:RELATED]-(d)
RETURN CASE WHEN d:Grant THEN 'Grant' WHEN d:Patent THEN 'Patent' ELSE 'Paper' END AS Type,
       coalesce(d.project_title, d.title) AS Title, d.date AS Date, r.matched_terms AS `Named as`,
       CASE WHEN d:Patent THEN 'https://patents.google.com/patent/' + replace(d.document_id, ' ', '')
            WHEN d:Publication THEN d.url END AS Link
ORDER BY Date DESC LIMIT 200""",
    },
    {
        "id": "countermeasure_lineage", "group": "Countermeasures and organizations",
        "question": "What antibodies, trials and gene targets is each countermeasure built on?",
        "params": [],
        "cypher": """MATCH (c:Countermeasure)
OPTIONAL MATCH (c)-[:ACTS_ON]->(f:GeneFamily)
OPTIONAL MATCH (c)-[:CONTAINS]->(a:Antibody)
OPTIONAL MATCH (c)-[:TESTED_IN]->(s:Study)
RETURN c.name AS Countermeasure, c.status AS Status, f.symbol AS `Acts on`,
       collect(DISTINCT a.name) AS `Component antibodies`, collect(DISTINCT s.study_accession) AS `ImmPort trials`
ORDER BY Countermeasure""",
    },
    {
        "id": "grants_that_led_to_patents", "group": "Countermeasures and organizations",
        "question": "Which NIH grants are credited on patents (the patent's government-interest statement)?",
        "params": [],
        "cypher": """MATCH (g:Grant)-[:FUNDS]->(p:Patent)
RETURN g.project_title AS Grant, g.core_project_num AS `Grant number`, p.title AS Patent,
       p.document_id AS `Patent number`, 'https://patents.google.com/patent/' + replace(p.document_id, ' ', '') AS Link
ORDER BY `Grant number`""",
    },
    {
        "id": "organizations_with_grants_and_patents", "group": "Countermeasures and organizations",
        "question": "Which organizations both received NIH grants and own patents?",
        "params": [],
        "cypher": """MATCH (o:Organization)<-[:AWARDED_TO]-(g:Grant)
WITH o, count(DISTINCT g) AS grants
MATCH (o)<-[:ASSIGNED_TO]-(p:Patent)
RETURN o.name AS Organization, o.kind AS Kind, grants AS Grants, count(DISTINCT p) AS Patents
ORDER BY Patents DESC, Grants DESC""",
    },
    {
        "id": "organization_portfolio", "group": "Countermeasures and organizations",
        "question": "What grants and patents does an organization named {text} hold?",
        "params": [{"name": "text", "type": "text"}],
        "cypher": f"""MATCH (o:Organization) WHERE toLower(o.name) CONTAINS toLower($text)
   OR any(s IN o.spellings WHERE toLower(s) CONTAINS toLower($text))
CALL (o) {{
  MATCH (o)<-[:AWARDED_TO]-(g:Grant)
  RETURN 'Grant' AS Type, g.project_title AS Title, g.date AS Date, null AS Link
  UNION ALL
  MATCH (o)<-[:ASSIGNED_TO]-(p:Patent)
  RETURN 'Patent' AS Type, p.title AS Title, p.date AS Date, {_PATENT_LINK} AS Link
}}
RETURN o.name AS Organization, Type, Title, Date, Link ORDER BY Organization, Date DESC LIMIT 200""",
    },
    # --- by gene --------------------------------------------------------------
    {
        "id": "antibodies_for_gene", "group": "Genes and antibodies",
        "question": "Which antibodies target {gene}?",
        "params": [{"name": "gene", "type": "gene"}],
        "cypher": """MATCH (a:Antibody)-[:TARGETS]->(g:Gene {gene_symbol: $gene})
RETURN a.name AS Antibody, a.isotype AS Isotype, a.neutralizing AS Neutralizing,
       a.isolation_host AS Host, a.immunogen AS Immunogen
ORDER BY Antibody""",
    },
    {
        "id": "papers_on_antibodies_for_gene", "group": "Genes and antibodies",
        "question": "Which papers describe antibodies against {gene}?",
        "params": [{"name": "gene", "type": "gene"}],
        "cypher": """MATCH (pub:Publication)-[:DESCRIBES]->(a:Antibody)-[:TARGETS]->(:Gene {gene_symbol: $gene})
RETURN pub.title AS Paper, pub.year AS Year, collect(DISTINCT a.name) AS Antibodies, pub.url AS Link
ORDER BY Year DESC LIMIT 200""",
    },
    {
        "id": "grants_for_gene", "group": "Genes and antibodies",
        "question": "Which NIH grants study the {gene} gene?",
        "params": [{"name": "gene", "type": "gene"}],
        "cypher": """MATCH (g:Grant)-[r:RELATED]->(:Gene {gene_symbol: $gene})
OPTIONAL MATCH (f:GrantFunding)-[:FUNDING_OF]->(g)
WITH g, r, max(f.project_detail_url) AS link
RETURN g.project_title AS Grant, g.org_name AS Institution, g.last_fiscal_year AS `Last year`,
       r.confidence AS Confidence, r.matched_terms AS `Matched terms`, link AS Link
ORDER BY Confidence DESC, `Last year` DESC LIMIT 200""",
    },
    {
        "id": "patents_claiming_gene", "group": "Genes and antibodies",
        "question": "Which patents claim the {gene} gene product?",
        "params": [{"name": "gene", "type": "gene"}],
        "cypher": f"""MATCH (p:Patent)-[r:CLAIMS]->(:Gene {{gene_symbol: $gene}})
RETURN p.title AS Patent, p.document_id AS Number, p.assignee AS Assignee,
       coalesce(p.date_published, p.filing_date) AS Date, r.confidence AS Confidence,
       {_PATENT_LINK} AS Link
ORDER BY Date DESC""",
    },
    {
        "id": "epitopes_by_virus_for_family", "group": "Genes and antibodies",
        "question": "Which viruses have mapped epitopes on their {gene_family} protein?",
        "params": [{"name": "gene_family", "type": "gene_family"}],
        "cypher": """MATCH (f:GeneFamily {symbol: $gene_family})<-[:IN_FAMILY]-(p:Protein)<-[:ON_PROTEIN]-(e:Epitope)
MATCH (e)-[:OF_TAXON]->(t:Taxon)
RETURN t.name AS Virus, count(DISTINCT e) AS Epitopes, count(DISTINCT p) AS Proteins
ORDER BY Epitopes DESC""",
    },
    {
        "id": "documents_for_family", "group": "Genes and antibodies",
        "question": "Which grants, patents and papers concern the {gene_family} gene, in any filovirus?",
        "params": [{"name": "gene_family", "type": "gene_family"}],
        "cypher": f"""MATCH (f:GeneFamily {{symbol: $gene_family}})
CALL (f) {{
  MATCH (g:Grant)-[r:RELATED]->(:Gene)-[:IN_FAMILY]->(f)
  RETURN 'Grant' AS Type, g.project_title AS Title, g.last_fiscal_year AS Year, max(r.confidence) AS Confidence,
         null AS Link
  UNION ALL
  MATCH (p:Patent)-[r:CLAIMS|RELATED]->(:Gene)-[:IN_FAMILY]->(f)
  RETURN 'Patent' AS Type, p.title AS Title, coalesce(p.date_published, p.filing_date).year AS Year,
         max(r.confidence) AS Confidence, {_PATENT_LINK} AS Link
  UNION ALL
  MATCH (pub:Publication)-[r:DESCRIBES]->(x)-[:ON_PROTEIN|OF_PROTEIN]->(:Protein)-[:IN_FAMILY]->(f)
  RETURN 'Paper' AS Type, pub.title AS Title, toInteger(pub.year) AS Year, max(r.confidence) AS Confidence,
         pub.url AS Link
}}
RETURN Type, Title, Year, Confidence, Link ORDER BY Confidence DESC, Year DESC LIMIT 200""",
    },
    # --- clinical -------------------------------------------------------------
    {
        "id": "grants_for_condition", "group": "Diseases, symptoms and assays",
        "question": "Which NIH grants mention {condition}?",
        "params": [{"name": "condition", "type": "condition"}],
        "cypher": """MATCH (g:Grant)-[r:RELATED]->(c:Condition {condition_id: $condition})
OPTIONAL MATCH (f:GrantFunding)-[:FUNDING_OF]->(g)
WITH g, r, max(f.project_detail_url) AS link
RETURN g.project_title AS Grant, g.org_name AS Institution, g.last_fiscal_year AS `Last year`,
       r.confidence AS Confidence, r.matched_terms AS `Matched terms`, link AS Link
ORDER BY Confidence DESC, `Last year` DESC LIMIT 200""",
    },
    {
        "id": "grants_for_symptom", "group": "Diseases, symptoms and assays",
        "question": "Which NIH grants mention the symptom {symptom}?",
        "params": [{"name": "symptom", "type": "symptom"}],
        "cypher": """MATCH (g:Grant)-[r:RELATED]->(:Symptom {symptom_id: $symptom})
OPTIONAL MATCH (f:GrantFunding)-[:FUNDING_OF]->(g)
WITH g, r, max(f.project_detail_url) AS link
RETURN g.project_title AS Grant, g.org_name AS Institution, g.last_fiscal_year AS `Last year`,
       r.confidence AS Confidence, r.matched_terms AS `Matched terms`, link AS Link
ORDER BY Confidence DESC, `Last year` DESC LIMIT 200""",
    },
    {
        "id": "grants_for_assay", "group": "Diseases, symptoms and assays",
        "question": "Which NIH grants use {assay_type}?",
        "params": [{"name": "assay_type", "type": "assay_type"}],
        "cypher": """MATCH (g:Grant)-[r:RELATED]->(:AssayType {assay_type_id: $assay_type})
OPTIONAL MATCH (f:GrantFunding)-[:FUNDING_OF]->(g)
WITH g, r, max(f.project_detail_url) AS link
RETURN g.project_title AS Grant, g.org_name AS Institution, g.last_fiscal_year AS `Last year`,
       r.confidence AS Confidence, r.matched_terms AS `Matched terms`, link AS Link
ORDER BY Confidence DESC, `Last year` DESC LIMIT 200""",
    },
    {
        "id": "isolates_with_symptom", "group": "Diseases, symptoms and assays",
        "question": "Which virus isolates were reported with {symptom}?",
        "params": [{"name": "symptom", "type": "symptom"}],
        "cypher": """MATCH (i:Isolate)-[:HAS_SYMPTOM]->(:Symptom {symptom_id: $symptom})
MATCH (i)-[:OF_TAXON]->(t:Taxon)
RETURN i.name AS Isolate, t.name AS Virus, i.country AS Country,
       i.collection_year AS Year, i.patient_outcome AS Outcome
ORDER BY Year""",
    },
    # --- connections ----------------------------------------------------------
    {
        "id": "shared_citations", "group": "Connections",
        "question": "Which papers are cited by both a patent and an NIH grant?",
        "params": [],
        "cypher": """MATCH (p:Patent)-[:CITES]->(pub:Publication)<-[:CITES]-(g:Grant)
RETURN pub.title AS Paper, pub.year AS Year,
       collect(DISTINCT p.document_id) AS Patents, count(DISTINCT g) AS Grants, pub.url AS Link
ORDER BY Grants DESC""",
    },
    {
        "id": "most_cited_papers", "group": "Connections",
        "question": "Which papers are cited most by other papers, grants and patents in the graph?",
        "params": [],
        "cypher": """MATCH (pub:Publication)<-[:CITES]-(d)
WITH pub, count(DISTINCT CASE WHEN d:Publication THEN d END) AS papers,
     count(DISTINCT CASE WHEN d:Grant THEN d END) AS grants,
     count(DISTINCT CASE WHEN d:Patent THEN d END) AS patents
RETURN pub.title AS Paper, pub.year AS Year, papers AS `Cited by papers`, grants AS `Cited by grants`,
       patents AS `Cited by patents`, pub.url AS Link
ORDER BY papers + grants + patents DESC LIMIT 100""",
    },
    {
        "id": "patent_citation_pairs", "group": "Connections",
        "question": "Which patents cite, or share a family with, other patents in the graph?",
        "params": [],
        "cypher": """MATCH (a:Patent)-[r:CITES|RELATED]->(b:Patent)
RETURN a.title AS Patent, a.document_id AS Number, CASE type(r) WHEN 'CITES' THEN 'cites' ELSE 'same family as' END AS Relation,
       b.title AS `Other patent`, b.document_id AS `Other number`,
       'https://patents.google.com/patent/' + replace(a.document_id, ' ', '') AS Link
ORDER BY Relation, Number""",
    },
    {
        "id": "top_genes_by_grants", "group": "Connections",
        "question": "Which genes are studied by the most NIH grants?",
        "params": [],
        "cypher": """MATCH (g:Grant)-[:RELATED]->(gene:Gene)
RETURN gene.gene_symbol AS Gene, count(DISTINCT g) AS Grants
ORDER BY Grants DESC""",
    },
    {
        "id": "search_titles", "group": "Connections",
        "question": "Find grants, patents and papers with {text} in the title",
        "params": [{"name": "text", "type": "text"}],
        "cypher": f"""CALL () {{
  MATCH (g:Grant) WHERE toLower(g.project_title) CONTAINS toLower($text)
  RETURN 'Grant' AS Type, g.project_title AS Title, g.last_fiscal_year AS Year, null AS Link
  UNION ALL
  MATCH (p:Patent) WHERE toLower(p.title) CONTAINS toLower($text)
  RETURN 'Patent' AS Type, p.title AS Title, coalesce(p.date_published, p.filing_date).year AS Year,
         {_PATENT_LINK} AS Link
  UNION ALL
  MATCH (pub:Publication) WHERE toLower(pub.title) CONTAINS toLower($text)
  RETURN 'Paper' AS Type, pub.title AS Title, toInteger(pub.year) AS Year, pub.url AS Link
}}
RETURN Type, Title, Year, Link ORDER BY Year DESC LIMIT 200""",
    },
]
TEMPLATES_BY_ID = {t["id"]: t for t in TEMPLATES}

MIN_TEXT_CHARS = 3


def resolve_params(template: dict, values: dict, options: dict) -> dict:
    """
    Turn the page's choices into Cypher parameters, rejecting anything that
    isn't one of the dropdown's own options. Raises ValueError with a message
    fit to show the user.
    """
    out = {}
    for p in template["params"]:
        name, kind = p["name"], p["type"]
        value = values.get(name)
        if kind == "virus":
            if value not in VIRUS_IDS:
                raise ValueError("Pick a virus from the list.")
            out[name] = VIRUS_IDS[value]
        elif kind == "text":
            value = (value or "").strip()
            if len(value) < MIN_TEXT_CHARS:
                raise ValueError(f"Type at least {MIN_TEXT_CHARS} characters to search for.")
            out[name] = value
        else:
            allowed = {str(o["value"]): o["value"] for o in options.get(kind, [])}
            if str(value) not in allowed:
                raise ValueError(f"Pick a {kind.replace('_', ' ')} from the list.")
            out[name] = allowed[str(value)]
    return out

# The evidence layer

NIH grants, USPTO patents and research papers are documents. Each one links
to the entities it is about, in one polymorphic table: `core.evidence_link`
(`sql/04_evidence.sql`), built by `etl/evidence.py`.

| Relation | From → to | Built from |
| --- | --- | --- |
| `related` | grant / patent → taxon, gene, condition, symptom, assay type, antibody | keywords in titles and abstracts (`kg_keywords.csv`), plus the CPC search a patent came from. Patent abstracts come from `filovirus fetch patent-details` |
| `claims` | patent → gene | a gene product named in the patent title |
| `describes` / `related` | paper → epitope, structure, antibody, feature, isolate, study | the papers bundle's exact source identifiers |
| `cites` | grant / patent → paper | NIH RePORTER and PubMed grant acknowledgements; patents' resolved references |
| `funds` | grant → patent | the patent's statement of government interest ("made with government support under AI066487"), matched to the grant's institute + serial or contract number |
| `related` | grant / patent / paper → countermeasure | the document's title or abstract naming a curated countermeasure (`curated/countermeasures.py`) |

## Confidence

Every link has a `confidence`, and one or more `core.evidence_link_support`
rows naming the term, field and method that produced it:

| Evidence | Confidence |
| --- | --- |
| exact identifier (PMID, PDB id, IEDB id, ...) | 1.0 |
| keyword in a title | 0.7 |
| keyword in an abstract | 0.5 |
| the CPC search that found a patent | 0.4 |
| a term the vocabulary flags as ambiguous (e.g. "GP") | halved |

An ambiguous term only counts when the same document also names an organism,
for example "GP" in a grant that never mentions a filovirus. This is the
organism-context rule in `organism_context_established`.

## Documents link directly

Documents link to each other with direct edges, not through a shared entity.
Pairing every two documents that mention the same taxon connected 87% of all
grant/patent pairs and told you nothing.

| Edge | Source | Kept when |
| --- | --- | --- |
| grant `cites` paper | NIH RePORTER publication lists, plus the paper's own PubMed grant list (`fetch paper-grants`): a GrantID naming the activity code links that award, a bare serial links every award of the NIH project | the paper is in `core.publication` |
| patent `cites` paper | the patent's references cited, resolved on PubMed | always (all resolved papers are kept) |
| paper `cites` paper | the citing paper's PubMed reference list (`fetch paper-citations`); only papers with full text in PubMed Central have one | both papers are in `core.publication` |
| patent `cites` patent | the Google Patents citation tables, read from both ends: A's "cites" and B's "cited by" (`fetch patent-details`) | both patents are in `core.patent` |
| patent `related` patent | the same patent family: an application points at the patent later granted for it | both are in `core.patent` |
| grant `cites` grant | the grant's own title or abstract naming another grant's core project number | that number names exactly one award |

No edge is guessed: if a reference's target is not in the graph, it is
dropped. Contract numbers (patents' funding statements, papers' grant lists)
link only when every task order they match is the same project: an umbrella
contract like the Frederick National Lab's covers unrelated projects, so it
links nothing.

## Organizations

Grant recipients and patent owners are `core.organization` nodes, joined to
their documents by `grant_award_organization` and `patent_organization`
(assignee or applicant). One organization merges every spelling of its name
(`curated/organizations.py`), so "The United States of America as represented
by the Secretary of the Army" and "US ARMY MEDICAL RESEARCH AND MATERIEL
COMMAND" are one node. People listed as patent applicants are not
organizations and are skipped.

## Reviewing links

The query page's **Review links** tab shows one keyword-based grant or patent
link at a time, with the text that produced it, to mark right or wrong.
Reviews live in `ops.link_review`, keyed by what the link connects in natural
ids (`grant_award:R01AI123456|gene:GP|related`), so they survive rebuilds;
`etl/evidence.py` copies each decision onto the rebuilt link's
`review_status`. `filovirus review-stats` reports precision per confidence
level.

## Papers

`core.publication` holds one row per PMID, however many routes found the
paper. `source_id` records the route that brought it in:

- `pubmed`: the papers bundle, 300 papers.
- `nih_reporter`: papers grants cite. Only the filovirus ones are kept, about
  1,270: a filovirus term in the title or MeSH headings, or twice in the
  abstract (`is_filovirus_paper`). Big centre grants cite thousands of
  unrelated papers.
- `uspto`: papers patents cite. All resolved references are kept, with no
  filovirus filter, because a patent's short reference list is the prior art
  it builds on. Team decision, 2026-11-10.

Each route deletes only its own stale rows, and a paper several routes reach
belongs to the earliest one.

## Workstream files

Teammates' link files are never loaded on trust:

- **Patent abstract hits.** Each keyword is re-run through the shared matcher,
  because the file's entity ids are stale.
- **Grant clinical hits.** Each condition, symptom or assay-type hit is
  re-checked against the stored grant text. For symptoms, disease names like
  "Lassa fever" are masked first (`DISEASE_NAME_RE`), so they don't count as
  the symptom "fever".
- **Deliberately not loaded:** viral hemorrhagic fever (MONDO:0018087), which
  spans non-filoviruses; taxon and gene rows that duplicate our own links; and
  the divergence statistics, which are analysis output.

See [data-sources.md](data-sources.md) for the files themselves.

## Rerun rule

The evidence step stores surrogate keys that the other steps regenerate, so
it must run last, and again after any other step. `filovirus build` does this.
Its closing orphan check catches dangling links, but not links that have
shifted onto a different but still valid row. That is why the rule exists.

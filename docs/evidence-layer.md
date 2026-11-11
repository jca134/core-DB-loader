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

Documents link to each other with direct `cites` edges, not through a shared
entity. Pairing every two documents that mention the same taxon connected 87%
of all grant/patent pairs and told you nothing.

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

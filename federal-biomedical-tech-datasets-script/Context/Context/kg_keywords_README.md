# KG keyword and synonym file

`kg_keywords.csv` is a UTF-8 CSV exported from the local `filovirus` database on 2026-09-25. It contains 772 rows: 120 taxon terms, 9 gene terms, 167 antibody terms, and 476 protein terms. Database access was read-only.

## Use

Filter `scope` to `include` for the default search vocabulary (231 rows). `review` rows require identity or scope review. `exclude` rows are retained only for an auditable record of exclusions. Deduplicate search terms when retrieving evidence, but retain all candidate node IDs for later review.

The assumed case scope is the Ebola and Marburg groups, including Reston and Bombali. Other filovirus groups are excluded or held for review. This is a project-scope choice, not a claim about host restriction or relevance to every study. Bat association alone is not a reason to exclude an Ebola or Marburg taxon. The project team can change scope flags.

## Columns

- `entity_type`: existing KG table (`taxon`, `gene`, `antibody`, `protein`).
- `canonical_name`: original database name or product label; not a claim that the database spelling is the latest accepted taxonomy.
- `search_term`: one search term per row.
- `term_type`: original name, recorded antibody alias, taxon common/legacy name, abbreviation, protein product label, or associated gene label.
- `scope`: `include`, `review`, or `exclude`.
- `requires_context`: ambiguous names need organism context in retrieval and manual review.
- `entity_ids`: original primary keys, separated by `|` when several protein nodes share the same label and gene annotation. Split these before integration. Shared labels do not establish biological identity.
- `source`: database table/column or supporting taxonomy URL.
- `notes`: scope and interpretation details.

## Mapping rules and limitations

Current species labels present in the database receive common names, abbreviations, and historical search names. For example, `Orthoebolavirus zairense` has `Ebola virus`, `EBOV`, and `Zaire ebolavirus`. Virus names and species names are different categories; these are retrieval associations. Marburg virus and Ravn virus remain distinct viruses even though their names retrieve evidence associated with the same species node.

Taxonomy reference: https://ictv.global/report/chapter/filoviridae/filoviridae

Historical nomenclature reference: https://ictv.global/report_9th/RNAneg/Mononegavirales/Filoviridae

Antibody aliases come only from `core.antibody.alias`; comma-separated aliases are split. Internal `polyclonal_Becquart_*` and `antiserum_FS*` names are excluded from default searches. Other names are retained without asserting external validation or equivalence between modified antibody forms.

Protein names and associated gene labels come from the database. No new protein equivalences are inferred. Protein scope is checked through `protein → feature → sequence → isolate → taxon`, using descendants of the current and legacy Ebola/Marburg genera. Unknown organism links and nonstandard product annotations are held for review. Blank product labels are omitted. Only ordinary, recognizable protein labels with in-scope organism links are included by default; other descriptions remain review entries. Preserve the distinction between GP, sGP, and ssGP.

The file contains names and identifiers only. Keyword matches should create candidate `related` links, not automatically imply that a grant funds a study or that a patent claims a specific entity. Exact identifiers and source text should support stronger links.

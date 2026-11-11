# Data sources

Source files are not versioned. Put them under `data/` (or point
`FILOVIRUS_DATA_DIR` somewhere else), one folder per source.
`filovirus load` mirrors each one verbatim into `raw.*`: every column is
stored as text, and each table is replaced on every run. Each load is recorded
in `ops.ingest_batch` / `ops.ingest_file`, with the path, md5 and row count.

```
data/
├── kg_keywords.csv            search vocabulary for keyword evidence links (+ kg_keywords_README.md)
├── bvbrc/*.csv                BV-BRC Filoviridae exports
├── ncbi/sequences.csv         NCBI Virus sequence catalog
├── hfv/                       LANL Hemorrhagic Fever Viruses database
├── immport/SDY*_ALL_DATA/     ImmPort study exports (tab-delimited)
├── ucsc/database/             UCSC eboVir3 tables (<table>.sql + <table>.txt.gz)
├── grants/                    NIH RePORTER grants (+ fetched / companion files)
├── patents/                   USPTO search exports (+ companion files)
└── papers/                    the papers workstream bundle (unzip in place)
```

## Required files

| Source | `filovirus load ...` | Files | Into |
| --- | --- | --- | --- |
| BV-BRC | `bvbrc` | every `bvbrc/*.csv` | `raw.bvbrc_<file>` |
| NCBI | `ncbi` | `ncbi/sequences.csv` | `raw.ncbi_sequences` |
| LANL HFV | `hfv` | `F15AG1.csv`, `F15OG1.csv`, `F15RG1.csv`, `EbolaCTL.xlsx`, `EBOV_numbering.xlsx`, `EbolaAntibody.xlsx`, `EBOV_features.xlsx`, `ebola-annotation-web.xlsx` | `raw.hfv_*` |
| ImmPort | `immport` | `SDY*_ALL_DATA/*_Tab/Tab/*.txt`: the study, subject, biosample, experiment, condition and exposure tables | `raw.immport_<study>_<table>` |
| UCSC | `ucsc` | `database/<table>.sql` and `<table>.txt.gz` for the tables listed in `loaders/ucsc.py` | `raw.ucsc_<table>` |
| NIH RePORTER | `grants` | `grants/grants_raw.csv` | `raw.nih_reporter_grants` |
| USPTO | `patents` | `patents/{ebola,marburg}_{A61K39-12,C07K16-10}_raw.csv` | `raw.uspto_patents` |
| Papers | `papers` | `papers/papers_iedb_rcsb_update/output_v2/` -- unzip `papers_iedb_rcsb_update (1) (1).zip` in `data/papers/` | `raw.papers_*` |

`kg_keywords.csv` is the KG search vocabulary that turns words in grant and
patent text into evidence links. It is exported by hand, as described in
`kg_keywords_README.md`. The evidence step looks for it in `data/` first, then
in the older `overview/Context/` and `exports/` locations.

## Optional files

Each of these is picked up if it exists and skipped with a message if it
doesn't. Whatever depends on a missing file is skipped too, never guessed.

### Written by `filovirus fetch` (network, free, no key)

Run these once and keep the CSVs. Everything after them works offline.

| `filovirus fetch ...` | Writes | Adds |
| --- | --- | --- |
| `grant-publications` | `grants/grant_publications.csv` | the papers NIH RePORTER lists for each grant (grant → paper edges) |
| `cited-publications` | `grants/cited_publications.csv` | PubMed records for those cited papers; the build keeps the ~1,270 filovirus ones. Run after `grant-publications` |
| `publication-metadata` | `papers/publication_metadata.csv` | titles and years for the papers bundle. Without it, papers are labelled `PMID 123` and undated |
| `patent-citations` | `patents/patent_cited_publications.csv` | patents' free-text references, resolved to PubMed and verified. All resolved papers are kept |

Set `NCBI_EMAIL` before running the PubMed fetchers. NCBI asks automated
clients to send a contact address with their requests.

### From the patents and grants workstreams

| File | Adds |
| --- | --- |
| `patents/patent_evidence_expanded.csv` | 100 more patents from a broader abstract search. They have no publication date, CPC codes or page count |
| `patents/patent_entity_links.csv` | keyword hits in patent abstracts, re-resolved through the shared matcher |
| `patents/patent_team_paper_edges.csv` | patent → paper citations already resolved to PMIDs |
| `patents/patent_external_literature_edges.csv` | patents' free-text references; the input to `fetch patent-citations` |
| `grants/grant_entity_edges.csv` | grant → condition / symptom / assay-type hits. Each is re-checked against the stored grant text, and the taxon/gene rows are ignored |
| `grants/grant_paper_divergence.csv` | only its `pubmed_ack` rows are used: grant → paper citations taken from the paper's PubMed grant list |

`data/additions/` holds the original copies of the 2026-11 workstream files.
The loaders do not read it.

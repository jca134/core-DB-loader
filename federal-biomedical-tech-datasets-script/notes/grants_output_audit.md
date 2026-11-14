# Grants output audit — 2026-10-04

Audit of `output/grants/` produced by `fetch_nih_grants.py` (include scope, all fiscal years).
Run totals: 681 grants, 5,717 evidence links, 2,426 raw RePORTER records.

## Reproducibility

- Ran the script 3 times. Every run returned 681 grants and 5,717 links.
- `grant_evidence.csv`, `query_log.csv` and `reporter_records.jsonl` were byte-identical across all runs.
- `grants.csv` differed only in the order of keywords that differ by case (`GP;gp` vs `gp;GP`), because Python set order changes between runs. **Fixed:** case ties are now broken deterministically, and the evidence sort uses `evidence_id` as a final tiebreaker.
- Results will change on a later date as RePORTER adds new fiscal-year records. That is expected.

## What's clean

- No duplicate `evidence_id`s, no exact duplicate evidence rows, and no duplicate grants by core project number.
- No duplicated raw records. Each `(appl_id, subproject_id)` appears once.
- All 5,717 URLs are well-formed `https://reporter.nih.gov/project-details/<appl_id>` links.
- Each grant has exactly one title in the evidence file.
- No encoding damage: no mojibake, and "Taï Forest" round-trips correctly.

## Issues, most serious first

### 1. Generic gene/protein terms create false links (high)

The filovirus-context check passes if a filovirus word appears anywhere in the abstract. It does not require the word to be near the matched term. So `NP`, `GP`, `polymerase`, `glycoprotein` and `nucleoprotein` match other viruses' proteins whenever the abstract mentions Ebola once in passing.

- 42 grants matched only through gene/protein terms. 28 of them mention a filovirus exactly once.
- Examples:
  - `K23AI171263` (SARS-CoV-2 neuro-PASC): "NP" is the SARS-CoV-2 nucleocapsid. Ebola appears only in the PI's background.
  - `R01AI181618` (HIV-1 membrane fusion): matched on "envelope glycoprotein".
  - RSV and Borna virus polymerase grants: matched on "polymerase".
- For 11 of the 60 grants matched on `polymerase`, every mention is "polymerase chain reaction". That's 66 bad evidence rows.
- Protein rows are 59% of all links (3,356), so this is the biggest quality problem.
- **Proposed fix:** require the filovirus word within the same sentence or about 10 words of the term, and ignore "polymerase chain reaction".

### 2. Contract numbers are cut short, so different contracts get merged (high, few rows)

For contracts (N01/N44/Y01), RePORTER's `core_project_num` is truncated to 11 characters (e.g. `27220120000`, `75N93019C00`). Different task orders, and sometimes different contracts, end up under one "grant".

- There are 32 nonstandard core numbers. At least 5 of them merge records with different titles. For example:
  - `26120150000` mixes "IGF::OT::IGF HIV CLINICAL RESEARCH IN WEST AFRICA" with an Ebola contract.
  - `27220120000` merges Task X8, X14 and X15.
- **Proposed fix:** for nonstandard cores, group by the full contract number plus task order, taken from `project_num` (e.g. `272201200003I` + `27200015`).

### 3. Weakly related grants (medium)

- 141 grants have no filovirus word in the title and at most one mention in the abstract.
- Some of these are fine, such as training grants in Liberia or Sierra Leone. Others only mention Ebola as an aside:
  - "Social Epidemiology of COVID-19", where Ebola appears in the PI biography.
  - Influenza hemagglutinin structure grants.
  - Dengue host-factor screens.
- Only 362 of the 681 grants have a filovirus word in the title.
- **Proposed fix:** add a relevance tier to `grants.csv`: strong (filovirus word in title), moderate (2 or more abstract mentions), weak (1 mention).

### 4. Duplicate links that differ only in keyword case (medium, easy fix)

- 364 evidence rows link the same grant to the same entity twice, once per spelling: `VP40`/`vp40`, `VP35`/`vp35`, `VP24`/`vp24`, `VP30`/`vp30`, `polymerase`/`POLYMERASE`.
- Both spellings come from `kg_keywords.csv`. The search is case-insensitive, so both spellings always match together.
- **Proposed fix:** dedupe links on the lowercased keyword.

### 5. Misleading title when a grant changed topic (low)

- `grants.csv` shows the most recent year's title. `K08AI139361` began as "Social Epidemiology of Ebola Virus Disease" and later became "Social Epidemiology of COVID-19", so the evidence row shows the COVID title.
- **Proposed fix:** use the title from the earliest matching year, or list every title the grant has had.

### 6. Inconsistent date format (low)

- 889 evidence rows (about 15%) have a year only (`2017`) instead of `YYYY-MM-DD`.
- These are intramural (`Z01`/`ZIA`) and interagency (`Y01`) records, which have no project start or award notice date.
- The ETL needs to handle both formats, or the script should mark these dates as approximate.

### 7. Not NIH, and not research (decision needed)

- RePORTER also covers CDC, FDA and VA awards. 61 grants come from CDC COTPER: state Public Health Emergency Preparedness supplements for Ebola traveler monitoring in 2014–15.
- These are real federal funding but not research. Decide whether to keep them.
- Other non-NIH agencies present: NCEZID (12), FDA (6), CGH (2), NCIRD (1), VA (1).

### 8. One project, several grant numbers (info, by design)

- 29 groups (111 grants) share identical titles because one project moved through several award mechanisms:
  - SBIR/STTR Phase I → II (`R41→R42`, `R43→R44`)
  - `R21→R33`, `K99→R00`, `R56→R01`
  - NIH's 2009 renumbering of intramural projects (`Z01AI005079` → `ZIAAI005079`)
- These are legitimately separate awards. The ETL could link them as one project family if needed.

### 9. Same grant and entity reached by several keywords (info, by design)

- 786 grant–entity pairs have more than one link, e.g. "Ebola virus" and "EBOV" both pointing to `Orthoebolavirus zairense`.
- Each row records a separate match route, so this is intended. The ETL should collapse them into one relation with several provenance rows.
- Six taxon keywords ("Ebola virus", "Zaire ebolavirus", "Sudan ebolavirus" and so on) each link to two taxon nodes, a legacy one and a current one. This comes straight from `kg_keywords.csv`.

### 10. Missing values

| Field | Count | Notes |
|---|---|---|
| condition `matched_entity_id` | 268 links | expected; `inputs/conditions.csv` has no KG IDs yet |
| study links | 0 | `inputs/immport_studies.csv` is empty |
| abstract | 64 grants | mostly older records and contracts |
| award_amount_sum = 0/blank | 22 grants | intramural records and subprojects |
| contact PI | 12 grants | |
| organization | 4 grants | |
| subproject-only grants | 27 | title is the subproject's, amount is partial |

### 11. Text cleanup

- 50 evidence rows (9 grants) have leading or trailing whitespace in the title. 5 titles contain double spaces.
- 62 titles are all caps. Some are administrative codes, e.g. "001.075; NIAID DCR; APPROPRIATED; …" and "IGF::OT::IGF …".
- 502 abstracts contain line breaks. They're correctly quoted in the CSV, but tools that read the file line by line will break. 2 abstracts contain HTML-like fragments.
- **Proposed fix:** strip and collapse whitespace in titles and abstracts.

### 12. ID cells too large for Excel

- 122 `matched_entity_id` cells are longer than Excel's 32,767-character cell limit. The longest is 38,090 characters, from protein labels shared by thousands of nodes.
- Opening and saving `grant_evidence.csv` in Excel will silently cut these off. Use pandas, or move the ID lists to a separate lookup file.

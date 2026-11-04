-- Data-quality checks. Not DDL -- run this after any ETL, as often as you like:
--
--     psql -d filovirus -f sql/checks.sql
--
-- Every row it returns should have failures = 0. Anything else is a defect,
-- and the `what_it_means` column says what kind.
--
-- WHY A SCRIPT RATHER THAN CONSTRAINTS. Three of this schema's most important
-- relationships cannot be foreign keys: core.xref, core.evidence_link and
-- ops.entity_provenance are polymorphic -- the table a row points into is named
-- by a discriminator column, not a REFERENCES clause. Postgres cannot enforce
-- those, so they are checked here instead. Each of these checks has caught a
-- real defect at least once; none is hypothetical.

\pset footer off
\echo
\echo '=== Data-quality checks: every `failures` below should be 0 ==='
\echo

WITH checks AS (

  -- 1. Polymorphic reference integrity -------------------------------------
  -- core.xref.entity_id has no FK; entity_type says which table it means.
  SELECT 'xref -> missing entity' AS check_name, count(*) AS failures,
         'an external id points at a row that no longer exists' AS what_it_means
  FROM core.xref x
  WHERE (entity_type='isolate'   AND NOT EXISTS (SELECT 1 FROM core.isolate   t WHERE t.isolate_id   = x.entity_id))
     OR (entity_type='sequence'  AND NOT EXISTS (SELECT 1 FROM core.sequence  t WHERE t.sequence_id  = x.entity_id))
     OR (entity_type='feature'   AND NOT EXISTS (SELECT 1 FROM core.feature   t WHERE t.feature_id   = x.entity_id))
     OR (entity_type='protein'   AND NOT EXISTS (SELECT 1 FROM core.protein   t WHERE t.protein_id   = x.entity_id))
     OR (entity_type='structure' AND NOT EXISTS (SELECT 1 FROM core.structure t WHERE t.structure_id = x.entity_id))
     OR (entity_type='epitope'   AND NOT EXISTS (SELECT 1 FROM core.epitope   t WHERE t.epitope_id   = x.entity_id))
     OR (entity_type='antibody'  AND NOT EXISTS (SELECT 1 FROM core.antibody  t WHERE t.antibody_id  = x.entity_id))
     OR (entity_type='patent'    AND NOT EXISTS (SELECT 1 FROM core.patent    t WHERE t.patent_id    = x.entity_id))

  UNION ALL
  -- The defect this one exists for: evidence links resolved to ids that an
  -- upstream ETL rerun had since renumbered, orphaning all 732 grants once.
  SELECT 'evidence_link -> missing entity', count(*),
         'an evidence edge points at a row that no longer exists'
  FROM core.evidence_link el
  WHERE (entity_type='taxon'       AND NOT EXISTS (SELECT 1 FROM core.taxon       t WHERE t.taxon_id::text       = el.entity_id))
     OR (entity_type='gene'        AND NOT EXISTS (SELECT 1 FROM core.gene        t WHERE t.gene_id::text        = el.entity_id))
     OR (entity_type='protein'     AND NOT EXISTS (SELECT 1 FROM core.protein     t WHERE t.protein_id::text     = el.entity_id))
     OR (entity_type='epitope'     AND NOT EXISTS (SELECT 1 FROM core.epitope     t WHERE t.epitope_id::text     = el.entity_id))
     OR (entity_type='antibody'    AND NOT EXISTS (SELECT 1 FROM core.antibody    t WHERE t.antibody_id::text    = el.entity_id))
     OR (entity_type='condition'   AND NOT EXISTS (SELECT 1 FROM core.condition   t WHERE t.condition_id::text   = el.entity_id))
     OR (entity_type='structure'   AND NOT EXISTS (SELECT 1 FROM core.structure   t WHERE t.structure_id::text   = el.entity_id))
     OR (entity_type='isolate'     AND NOT EXISTS (SELECT 1 FROM core.isolate     t WHERE t.isolate_id::text     = el.entity_id))
     OR (entity_type='sequence'    AND NOT EXISTS (SELECT 1 FROM core.sequence    t WHERE t.sequence_id::text    = el.entity_id))
     OR (entity_type='feature'     AND NOT EXISTS (SELECT 1 FROM core.feature     t WHERE t.feature_id::text     = el.entity_id))
     OR (entity_type='study'       AND NOT EXISTS (SELECT 1 FROM core.study       t WHERE t.study_accession      = el.entity_id))
     OR (entity_type='publication' AND NOT EXISTS (SELECT 1 FROM core.publication t WHERE t.publication_id::text = el.entity_id))
     OR (entity_type='grant_award' AND NOT EXISTS (SELECT 1 FROM core.grant_award t WHERE t.grant_award_id::text = el.entity_id))
     OR (entity_type='patent'      AND NOT EXISTS (SELECT 1 FROM core.patent      t WHERE t.patent_id::text      = el.entity_id))
     OR (entity_type='symptom'     AND NOT EXISTS (SELECT 1 FROM core.symptom     t WHERE t.symptom_id::text     = el.entity_id))
     OR (entity_type='assay_type'  AND NOT EXISTS (SELECT 1 FROM core.assay_type  t WHERE t.assay_type_id::text  = el.entity_id))

  UNION ALL
  SELECT 'evidence_link -> missing document', count(*),
         'an evidence edge points at a grant/patent/paper that no longer exists'
  FROM core.evidence_link el
  WHERE (evidence_type='grant_award' AND NOT EXISTS (SELECT 1 FROM core.grant_award t WHERE t.grant_award_id = el.evidence_id))
     OR (evidence_type='patent' AND NOT EXISTS (SELECT 1 FROM core.patent      t WHERE t.patent_id      = el.evidence_id))
     OR (evidence_type='publication' AND NOT EXISTS (SELECT 1 FROM core.publication t WHERE t.publication_id = el.evidence_id))

  UNION ALL
  SELECT 'entity_provenance -> missing core row', count(*),
         'lineage points at a core row that no longer exists'
  FROM ops.entity_provenance p
  WHERE (core_table='isolate'  AND NOT EXISTS (SELECT 1 FROM core.isolate  t WHERE t.isolate_id::text  = p.core_pk))
     OR (core_table='sequence' AND NOT EXISTS (SELECT 1 FROM core.sequence t WHERE t.sequence_id::text = p.core_pk))
     OR (core_table='feature'  AND NOT EXISTS (SELECT 1 FROM core.feature  t WHERE t.feature_id::text  = p.core_pk))
     OR (core_table='protein'  AND NOT EXISTS (SELECT 1 FROM core.protein  t WHERE t.protein_id::text  = p.core_pk))
     OR (core_table='epitope'  AND NOT EXISTS (SELECT 1 FROM core.epitope  t WHERE t.epitope_id::text  = p.core_pk))

  UNION ALL
  -- 2. Deduplication that no unique constraint can express -----------------
  -- BV-BRC exports every feature twice, once per annotation pipeline. Rows
  -- whose sequence did not resolve are excluded: they are legitimately
  -- distinct features that merely share a null sequence_id.
  SELECT 'duplicate features', coalesce(sum(n) - count(*), 0),
         'the same physical feature stored more than once'
  FROM (SELECT count(*) AS n FROM core.feature
        WHERE sequence_id IS NOT NULL
        GROUP BY sequence_id, feature_type, "start", "end", strand, gene, product
        HAVING count(*) > 1) d

  UNION ALL
  -- BV-BRC and NCBI report the same genome under accessions that differ only
  -- by a version suffix; an unmerged pair shows up here.
  SELECT 'accessions spanning >1 sequence', count(*),
         'one GenBank accession resolving to more than one core.sequence'
  FROM (SELECT split_part(xref_value, '.', 1) AS base
        FROM core.xref WHERE entity_type='sequence' AND xref_type='genbank_accession'
        GROUP BY 1 HAVING count(DISTINCT entity_id) > 1) d

  UNION ALL
  SELECT 'duplicate PDB ids', count(*), 'one PDB id on more than one core.structure'
  FROM (SELECT pdb_id FROM core.structure WHERE pdb_id IS NOT NULL
        GROUP BY 1 HAVING count(*) > 1) d

  UNION ALL
  SELECT 'duplicate pubmed ids', count(*), 'one PMID on more than one core.publication'
  FROM (SELECT pubmed_id FROM core.publication WHERE pubmed_id IS NOT NULL
        GROUP BY 1 HAVING count(*) > 1) d

  UNION ALL
  -- 3. Links that must be auditable ----------------------------------------
  SELECT 'evidence links with no support row', count(*),
         'an evidence edge that cannot be traced back to the text that made it'
  FROM core.evidence_link el
  WHERE NOT EXISTS (SELECT 1 FROM core.evidence_link_support s
                    WHERE s.evidence_link_id = el.evidence_link_id)

  UNION ALL
  SELECT 'core rows with no provenance', count(*),
         'an isolate with no recorded lineage back to a raw row'
  FROM core.isolate i
  WHERE NOT EXISTS (SELECT 1 FROM ops.entity_provenance p
                    WHERE p.core_table='isolate' AND p.core_pk = i.isolate_id::text)

  UNION ALL
  -- 4. Curated tables that go stale silently --------------------------------
  -- core.condition is truncated and rebuilt by etl_immport, cascading into
  -- core.taxon_condition; core.gene is rebuilt by etl_ucsc, which strands
  -- antibody targets. Both are reseeded by their own ETL, and both are empty
  -- rather than wrong if that step is skipped.
  SELECT 'taxon_condition empty', CASE WHEN count(*) = 0 THEN 1 ELSE 0 END,
         'rerun etl_taxon_condition (etl_immport truncates core.condition)'
  FROM core.taxon_condition

  UNION ALL
  -- Same failure shape for the clinical-vocabulary links: each is cascaded
  -- away by an upstream truncate. A link table is only flagged when its
  -- source rows exist, so a database without ImmPort/LANL loaded stays clean.
  SELECT 'isolate_symptom empty',
         CASE WHEN NOT EXISTS (SELECT 1 FROM core.isolate_symptom)
               AND to_regclass('raw.hfv_ebola_annotation_web') IS NOT NULL THEN 1 ELSE 0 END,
         'rerun etl_clinical_vocab (etl_bvbrc_hfv truncates core.isolate)'
  UNION ALL
  SELECT 'experiment_assay_type empty',
         CASE WHEN NOT EXISTS (SELECT 1 FROM core.experiment_assay_type)
               AND EXISTS (SELECT 1 FROM core.experiment) THEN 1 ELSE 0 END,
         'rerun etl_clinical_vocab (etl_immport truncates core.experiment)'
  UNION ALL
  SELECT 'epitope_assay_assay_type empty',
         CASE WHEN NOT EXISTS (SELECT 1 FROM core.epitope_assay_assay_type)
               AND EXISTS (SELECT 1 FROM core.epitope_assay) THEN 1 ELSE 0 END,
         'rerun etl_clinical_vocab (etl_bvbrc_hfv truncates core.epitope_assay)'

  UNION ALL
  SELECT 'antibody targets unresolved', count(*),
         'rerun etl_antibody_target (etl_bvbrc_hfv rebuilds antibody_epitope)'
  FROM core.antibody_epitope ae
  JOIN ops.entity_provenance p
    ON p.core_table='antibody' AND p.core_pk = ae.antibody_id::text
   AND p.raw_table='hfv_ebola_antibody'
  JOIN raw.hfv_ebola_antibody a ON a."order" = p.raw_pk
  WHERE ae.target_gene_symbol IS NULL AND lower(trim(a.protein)) <> 'unknown'

  UNION ALL
  -- target_gene_symbol is a natural key, not a foreign key, so nothing stops it
  -- holding a symbol core.gene does not have. This is the check that would
  -- otherwise have been an FK constraint -- one that turned out to be unsafe,
  -- because a real FK let etl_ucsc's TRUNCATE CASCADE empty this whole table.
  SELECT 'antibody target symbol not in core.gene', count(*),
         'target_gene_symbol holds a symbol with no matching core.gene row'
  FROM core.antibody_epitope ae
  WHERE ae.target_gene_symbol IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM core.gene g WHERE g.gene_symbol = ae.target_gene_symbol)

  UNION ALL
  -- 5. Normalisation --------------------------------------------------------
  -- The loaders read with keep_default_na=False, so a blank cell arrives as an
  -- empty string. write_core() normalises those to NULL; a column written by
  -- some other path can still reintroduce them, and the damage is invisible --
  -- `WHERE product IS NULL` simply stops finding the blank rows.
  SELECT 'empty strings in core.feature.product', count(*),
         'a blank stored as an empty string instead of NULL'
  FROM core.feature WHERE product = ''

  UNION ALL
  SELECT 'empty strings in core.sequence.sequence_type', count(*),
         'a blank stored as an empty string instead of NULL'
  FROM core.sequence WHERE sequence_type = ''

  UNION ALL
  -- 6. Completeness: every raw row accounted for -----------------------------
  -- Tables that map one raw row to one core row. A difference here means rows
  -- were dropped somewhere between raw and core with nothing reporting it --
  -- the failure mode behind every silent-loss bug this project has had.
  SELECT 'raw->core: taxonomy', abs((SELECT count(*) FROM raw.bvbrc_taxonomy) - (SELECT count(*) FROM core.taxon)),
         'raw.bvbrc_taxonomy and core.taxon should match one to one'
  UNION ALL
  SELECT 'raw->core: structures', abs((SELECT count(*) FROM raw.bvbrc_protein_structure) - (SELECT count(*) FROM core.structure)),
         'raw.bvbrc_protein_structure and core.structure should match one to one'
  UNION ALL
  SELECT 'raw->core: antibodies', abs((SELECT count(*) FROM raw.hfv_ebola_antibody) - (SELECT count(*) FROM core.antibody)),
         'raw.hfv_ebola_antibody and core.antibody should match one to one'
  UNION ALL
  SELECT 'raw->core: grant records', abs((SELECT count(*) FROM raw.nih_reporter_grants) - (SELECT count(*) FROM core.grant_funding)),
         'every RePORTER record should become one core.grant_funding row'
  UNION ALL
  SELECT 'raw->core: papers', abs((SELECT count(*) FROM raw.papers_unique) - (SELECT count(*) FROM core.publication WHERE source_id = (SELECT source_id FROM core.source WHERE source_code='pubmed'))),
         'every paper in the bundle should become one core.publication row'
  UNION ALL
  -- A paper that entered through a grant's citation list exists only because
  -- a grant cites it, so it must keep that edge. One without it means the
  -- cites step dropped it (award key mismatch) or a stale row was left behind.
  SELECT 'grant-cited paper with no cites edge', count(*),
         'a core.publication added from RePORTER citations is cited by no grant'
  FROM core.publication p
  WHERE p.source_id = (SELECT source_id FROM core.source WHERE source_code='nih_reporter')
    AND NOT EXISTS (SELECT 1 FROM core.evidence_link el
                    WHERE el.relation = 'cites' AND el.entity_type = 'publication'
                      AND el.entity_id = p.publication_id::text)

  UNION ALL
  -- Features are many-to-one by design (pipeline pairs merge), so they cannot
  -- be counted against raw directly. What CAN be checked is that a merge never
  -- crossed genomes: an unresolved feature's raw rows must all share a genome,
  -- which is the exact defect that once merged three genomes into one row.
  SELECT 'unresolved feature spanning >1 genome', count(*),
         'an unanchored feature merged raw rows from different genomes'
  FROM (SELECT f.feature_id FROM core.feature f
        JOIN ops.entity_provenance p ON p.core_table='feature' AND p.core_pk = f.feature_id::text
        WHERE f.sequence_id IS NULL AND p.raw_table = 'bvbrc_genome_feature'
        GROUP BY 1 HAVING count(DISTINCT split_part(p.raw_pk, '.', 3)) > 1) d

  UNION ALL
  -- A structure with neither a protein nor an organism connects to nothing.
  SELECT 'structures connected to nothing', count(*),
         'a structure with no protein_id and no taxon_id'
  FROM core.structure WHERE protein_id IS NULL AND taxon_id IS NULL

  UNION ALL
  -- 7. BIGSERIAL sequences left behind their table -------------------------
  -- The ETLs assign primary keys from in-process counters and INSERT them
  -- explicitly, so nextval() is never called and the sequence stays at 1.
  -- Nothing in the pipeline notices, because the pipeline never uses the
  -- default; the next ordinary INSERT does, takes an id that already exists,
  -- and fails on the primary key. run_all.py resyncs them after every run.
  SELECT 'serial sequences behind their table', count(*),
         'an ordinary INSERT would collide on the primary key -- rerun run_all'
  FROM (
      SELECT s.relname AS seq, t.relname AS tbl, a.attname AS col
      FROM pg_class s
      JOIN pg_namespace sn ON sn.oid = s.relnamespace AND sn.nspname = 'core'
      JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
      JOIN pg_class t ON t.oid = d.refobjid
      JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
      WHERE s.relkind = 'S'
  ) q
  WHERE (
      SELECT (xpath('/row/c/text()', query_to_xml(
          format('SELECT coalesce(max(%I), 0) AS c FROM core.%I', q.col, q.tbl),
          false, true, '')))[1]::text::bigint
  ) > (SELECT last_value FROM pg_sequences
       WHERE schemaname = 'core' AND sequencename = q.seq)
)
SELECT check_name,
       failures,
       CASE WHEN failures = 0 THEN 'ok' ELSE 'FAIL' END AS status,
       what_it_means
FROM checks
ORDER BY failures DESC, check_name;

import itertools
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text

from core_data_loader.common.accessions import base_accession, resolve_bvbrc_duplicate_genomes, pick_field
from core_data_loader.common.etl_common import (
    engine, ensure_sources, truncate, read_raw, write_core,
    safe_int, safe_float, safe_date, blank_to_none,
    add_xref, write_xref,
    add_provenance, write_provenance,
)

BVBRC = "bvbrc"
NCBI = "ncbi"
HFV = "hfv_lanl"

NEUTRALIZING_TRUE = {"y", "yes", "true", "1"}


def hfv_clean(v):
    # LANL uses a bare "_" as its own null placeholder throughout its HFV
    # exports (antibody/CTL iedb_id, the annotation_web patient fields),
    # distinct from blank_to_none's "empty string" handling -- without this,
    # "_" gets stored as if it were a real value (e.g. a fake iedb_id xref).
    v = blank_to_none(v)
    return None if v == "_" else v


ENTITY_TYPES_OWNED = ["isolate", "sequence", "feature", "protein", "epitope", "antibody"]

PROVENANCE_TABLES_OWNED = [
    "taxon", "isolate", "sequence", "feature", "protein",
    "structure", "epitope", "antibody", "alignment_member",
]


def _counter():
    return itertools.count(1)


def _isolate_field(ctx, isolate_id, field_name, *candidates):
    """
    pick_field(), plus recording the winning source as a core.isolate_field_source
    row when there was one -- see that table's comment in sql/02_core.sql.
    """
    value, source_id = pick_field(*candidates)
    if source_id is not None:
        ctx.field_source_rows.append({
            "isolate_id": isolate_id,
            "field_name": field_name,
            "source_id": source_id,
        })
    return value


def _sequence_field(ctx, sequence_id, field_name, *candidates):
    """Same as _isolate_field, for core.sequence_field_source."""
    value, source_id = pick_field(*candidates)
    if source_id is not None:
        ctx.sequence_field_source_rows.append({
            "sequence_id": sequence_id,
            "field_name": field_name,
            "source_id": source_id,
        })
    return value


@dataclass
class BuildContext:
    """
    State threaded through the build_* steps below: id counters, the
    cross-reference lookup dicts each step needs from earlier steps, and the
    accumulator lists (epitope/epitope_assay/antibody_epitope/xref) that more
    than one step appends to before a single final write.
    """

    bvbrc_sid: int
    ncbi_sid: int
    hfv_sid: int

    taxon_ids_present: set = field(default_factory=set)

    genome_id_to_isolate: dict = field(default_factory=dict)
    ncbi_isolate_accessions: set = field(default_factory=set)
    merged_isolate_count: int = 0
    bvbrc_internal_merged_count: int = 0

    accession_to_sequence: dict = field(default_factory=dict)   # genbank accession -> core.sequence_id
    sequence_to_isolate: dict = field(default_factory=dict)     # core.sequence_id -> core.isolate_id
    raw_seqid_to_sequence: dict = field(default_factory=dict)   # bvbrc genome_sequence.sequence_id (text) -> core.sequence_id

    raw_featureid_to_protein: dict = field(default_factory=dict)
    ncbi_protein_id_to_protein: dict = field(default_factory=dict)
    patric_id_to_protein: dict = field(default_factory=dict)
    uniprot_to_protein: dict = field(default_factory=dict)

    hfv_epitope_lookup: dict = field(default_factory=dict)  # (protein, sequence) -> epitope_id

    epitope_rows: list = field(default_factory=list)
    epitope_assay_rows: list = field(default_factory=list)
    antibody_epitope_rows: list = field(default_factory=list)
    xref_rows: list = field(default_factory=list)
    provenance_rows: list = field(default_factory=list)
    field_source_rows: list = field(default_factory=list)  # core.isolate_field_source
    sequence_field_source_rows: list = field(default_factory=list)  # core.sequence_field_source

    isolate_id_seq: itertools.count = field(default_factory=_counter)
    sequence_id_seq: itertools.count = field(default_factory=_counter)
    feature_id_seq: itertools.count = field(default_factory=_counter)
    protein_id_seq: itertools.count = field(default_factory=_counter)
    structure_id_seq: itertools.count = field(default_factory=_counter)
    epitope_id_seq: itertools.count = field(default_factory=_counter)
    antibody_id_seq: itertools.count = field(default_factory=_counter)
    alignment_id_seq: itertools.count = field(default_factory=_counter)
    alignment_member_id_seq: itertools.count = field(default_factory=_counter)

    def resolve_protein(self, ncbi_protein_id=None, patric_id=None, uniprot_accession=None):
        for key, table in (
            (ncbi_protein_id, self.ncbi_protein_id_to_protein),
            (patric_id, self.patric_id_to_protein),
            (uniprot_accession, self.uniprot_to_protein),
        ):
            key = blank_to_none(key)
            if key and key in table:
                return table[key]
        return None

    def get_or_create_hfv_epitope(self, protein, seq, epitope_type,
                                   host_species=None, organism=None, iedb_id=None,
                                   raw_table=None, raw_pk=None):
        key = (blank_to_none(protein), blank_to_none(seq))
        if key in self.hfv_epitope_lookup:
            return self.hfv_epitope_lookup[key]
        eid = next(self.epitope_id_seq)
        self.epitope_rows.append({
            "epitope_id": eid,
            "protein_id": None,
            "epitope_sequence": blank_to_none(seq),
            "start": None,
            "end": None,
            "epitope_type": epitope_type,
            "host_species": host_species,
            "organism": organism,
            "taxon_id": None,  # no bvbrc taxonomy resolution attempted for hfv-sourced epitopes
            "source_id": self.hfv_sid,
        })
        add_xref(self.xref_rows, "epitope", eid, self.hfv_sid, "iedb_id", hfv_clean(iedb_id))
        # raw_table/raw_pk describe whichever raw row first minted this
        # epitope; a later dedup hit against the same key isn't re-recorded.
        if raw_table is not None:
            add_provenance(self.provenance_rows, "epitope", eid, self.hfv_sid, raw_table, raw_pk)
        self.hfv_epitope_lookup[key] = eid
        return eid


def build_taxon(ctx: BuildContext) -> pd.DataFrame:
    taxonomy = read_raw("bvbrc_taxonomy")
    ctx.taxon_ids_present = set(taxonomy["taxon_id"].map(safe_int).dropna())

    def parent_if_present(v):
        pid = safe_int(v)
        return pid if pid in ctx.taxon_ids_present else None

    taxon_df = pd.DataFrame({
        "taxon_id": taxonomy["taxon_id"].map(safe_int),
        "taxon_name": taxonomy["taxon_name"],
        "taxon_rank": taxonomy["taxon_rank"],
        "genetic_code": taxonomy["genetic_code"].map(safe_int),
        "lineage": taxonomy["lineage"],
        "parent_id": taxonomy["parent_id"].map(parent_if_present),
        "source_id": ctx.bvbrc_sid,
    }).dropna(subset=["taxon_id"])

    for taxon_id in taxon_df["taxon_id"]:
        add_provenance(ctx.provenance_rows, "taxon", taxon_id, ctx.bvbrc_sid, "bvbrc_taxonomy", taxon_id)

    return taxon_df


def build_isolates_and_sequences(ctx: BuildContext, genome: pd.DataFrame, genome_sequence: pd.DataFrame,
                                  ncbi: pd.DataFrame):
    """
    BV-BRC re-annotates NCBI's own GenBank submissions, so a genome present
    in both raw sources is the same physical isolate/sequence, matched here
    by base GenBank accession -- one core.isolate/core.sequence row for it,
    carrying xrefs and provenance from both sources, instead of two
    duplicate rows. A genome only one source has still gets its own row.
    """
    isolate_rows = []
    sequence_rows = []

    ncbi_by_accession = {}
    for r in ncbi.itertuples(index=False):
        base = base_accession(r.accession)
        if base:
            ncbi_by_accession[base] = r
    matched_bases = set()

    seqs_by_genome: dict = {}
    genome_ids_by_accession: dict = {}
    for r in genome_sequence.itertuples(index=False):
        seqs_by_genome.setdefault(r.genome_id, []).append(r)
        base = base_accession(r.accession)
        if base:
            genome_ids_by_accession.setdefault(base, []).append(r.genome_id)

    # genome_ids that are themselves a reload of another genome_id in this
    # file -- skipped here and folded into their canonical genome_id's
    # isolate/sequence once the main loop below has built it.
    dup_genome_map = resolve_bvbrc_duplicate_genomes(
        genome_ids_by_accession,
        status_by_genome=dict(zip(genome["genome_id"], genome["genome_status"])),
        inserted_by_genome=dict(zip(genome["genome_id"], genome["date_inserted"])),
    )

    for grow in genome.itertuples(index=False):
        if grow.genome_id in dup_genome_map:
            continue
        gseqs = seqs_by_genome.get(grow.genome_id, [])

        # Filoviruses are non-segmented, so a genome matches at most one
        # NCBI accession in practice. A handful of BV-BRC genome_ids share
        # an accession with each other (a BV-BRC-side duplicate, not
        # something this merge is meant to fix) -- only the first one
        # encountered claims the NCBI match; the rest fall back to
        # bvbrc-only below, same as before this merge existed.
        nrow = None
        matched_base = None
        for srow in gseqs:
            base = base_accession(srow.accession)
            if base and base in ncbi_by_accession and base not in matched_bases:
                nrow = ncbi_by_accession[base]
                matched_base = base
                break

        iid = next(ctx.isolate_id_seq)
        ctx.genome_id_to_isolate[grow.genome_id] = iid

        add_xref(ctx.xref_rows, "isolate", iid, ctx.bvbrc_sid, "bvbrc_genome_id", grow.genome_id)
        add_provenance(ctx.provenance_rows, "isolate", iid, ctx.bvbrc_sid, "bvbrc_genome", grow.genome_id)

        # BV-BRC's own fields win where both sources have one; nrow (the
        # matched NCBI row, or None) only fills what BV-BRC leaves blank.
        # _isolate_field() also records, per field, which source's value
        # actually won -- isolate.source_id alone can't say that, since it's
        # always ctx.bvbrc_sid on a merged row even when e.g. country or
        # collection_date came entirely from NCBI.
        isolate_rows.append({
            "isolate_id": iid,
            "taxon_id": safe_int(grow.taxon_id) if safe_int(grow.taxon_id) in ctx.taxon_ids_present else None,
            "primary_strain_name": _isolate_field(
                ctx, iid, "primary_strain_name",
                (ctx.bvbrc_sid, blank_to_none(grow.genome_name)),
                (ctx.ncbi_sid, blank_to_none(nrow.organism_name) if nrow else None),
            ),
            "lanl_strain_name": None,
            "species": _isolate_field(
                ctx, iid, "species",
                (ctx.bvbrc_sid, blank_to_none(grow.species)),
                (ctx.ncbi_sid, blank_to_none(nrow.species) if nrow else None),
            ),
            "country": _isolate_field(
                ctx, iid, "country",
                (ctx.ncbi_sid, blank_to_none(nrow.country) if nrow else None),
            ),
            "geo_location": _isolate_field(
                ctx, iid, "geo_location",
                (ctx.ncbi_sid, blank_to_none(nrow.geo_location) if nrow else None),
            ),
            "host": _isolate_field(
                ctx, iid, "host",
                (ctx.bvbrc_sid, blank_to_none(grow.host_common_name)),
                (ctx.ncbi_sid, blank_to_none(nrow.host) if nrow else None),
            ),
            "tissue_specimen_source": _isolate_field(
                ctx, iid, "tissue_specimen_source",
                (ctx.ncbi_sid, blank_to_none(nrow.tissue_specimen_source) if nrow else None),
            ),
            "collection_date": _isolate_field(
                ctx, iid, "collection_date",
                (ctx.ncbi_sid, safe_date(nrow.collection_date) if nrow else None),
            ),
            "genome_status": _isolate_field(
                ctx, iid, "genome_status",
                (ctx.bvbrc_sid, blank_to_none(grow.genome_status)),
                (ctx.ncbi_sid, blank_to_none(nrow.nuc_completeness) if nrow else None),
            ),
            "source_id": ctx.bvbrc_sid,
            "notes": _isolate_field(
                ctx, iid, "notes",
                (ctx.ncbi_sid, blank_to_none(nrow.isolate) if nrow else None),
            ),
        })
        if nrow is not None:
            matched_bases.add(matched_base)
            ctx.ncbi_isolate_accessions.add(nrow.accession)
            add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_isolate_designation", nrow.isolate)
            add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_assembly", nrow.assembly)
            add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_sra_accession", nrow.sra_accession)
            add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_biosample", nrow.biosample)
            add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_bioproject", nrow.bioproject)
            add_provenance(ctx.provenance_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_sequences", nrow.accession)

        for srow in gseqs:
            sid = next(ctx.sequence_id_seq)
            ctx.raw_seqid_to_sequence[srow.sequence_id] = sid
            ctx.sequence_to_isolate[sid] = iid
            base = base_accession(srow.accession)
            if base:
                ctx.accession_to_sequence.setdefault(base, sid)
            add_xref(ctx.xref_rows, "sequence", sid, ctx.bvbrc_sid, "genbank_accession", blank_to_none(srow.accession))
            add_provenance(ctx.provenance_rows, "sequence", sid, ctx.bvbrc_sid, "bvbrc_genome_sequence", srow.sequence_id)

            seq_nrow = nrow if (nrow is not None and base == matched_base) else None
            if seq_nrow is not None:
                add_xref(ctx.xref_rows, "sequence", sid, ctx.ncbi_sid, "genbank_accession", blank_to_none(seq_nrow.accession))
                add_provenance(ctx.provenance_rows, "sequence", sid, ctx.ncbi_sid, "ncbi_sequences", seq_nrow.accession)

            # Same idea as the isolate fields above: srow's own fields win
            # where both sources have one; seq_nrow only fills what BV-BRC
            # leaves blank. _sequence_field() records, per field, which
            # source's value actually won -- sequence.source_id alone can't,
            # since it's always ctx.bvbrc_sid on a merged row.
            sequence_rows.append({
                "sequence_id": sid,
                "isolate_id": iid,
                "molecule_type": _sequence_field(
                    ctx, sid, "molecule_type",
                    (ctx.ncbi_sid, blank_to_none(seq_nrow.molecule_type) if seq_nrow else None),
                ),
                "sequence_type": srow.sequence_type,
                "segment": _sequence_field(
                    ctx, sid, "segment",
                    (ctx.bvbrc_sid, blank_to_none(srow.topology)),
                    (ctx.ncbi_sid, blank_to_none(seq_nrow.segment) if seq_nrow else None),
                ),
                "length": _sequence_field(
                    ctx, sid, "length",
                    (ctx.bvbrc_sid, safe_int(srow.length)),
                    (ctx.ncbi_sid, safe_int(seq_nrow.length) if seq_nrow else None),
                ),
                "sequence_text": blank_to_none(srow.sequence),
                "sequence_md5": srow.sequence_md5,
                "is_reference": False,
                "release_date": _sequence_field(
                    ctx, sid, "release_date",
                    (ctx.ncbi_sid, safe_date(seq_nrow.release_date) if seq_nrow else None),
                ),
                "source_id": ctx.bvbrc_sid,
            })

    # Fold each duplicate genome_id's isolate/sequence into its canonical
    # genome_id's -- the reload shares the canonical's accession(s) by
    # construction, so ctx.accession_to_sequence already resolves to it.
    for dup_gid, canonical_gid in dup_genome_map.items():
        canonical_iid = ctx.genome_id_to_isolate.get(canonical_gid)
        if canonical_iid is None:
            continue
        ctx.genome_id_to_isolate[dup_gid] = canonical_iid
        add_xref(ctx.xref_rows, "isolate", canonical_iid, ctx.bvbrc_sid, "bvbrc_genome_id", dup_gid)
        add_provenance(ctx.provenance_rows, "isolate", canonical_iid, ctx.bvbrc_sid, "bvbrc_genome", dup_gid)

        for srow in seqs_by_genome.get(dup_gid, []):
            base = base_accession(srow.accession)
            canonical_sid = ctx.accession_to_sequence.get(base) if base else None
            if canonical_sid is None:
                continue
            # so bvbrc_genome_feature rows keyed to this reloaded genome's
            # raw sequence_id still resolve to the one core.sequence for it
            ctx.raw_seqid_to_sequence[srow.sequence_id] = canonical_sid
            add_xref(ctx.xref_rows, "sequence", canonical_sid, ctx.bvbrc_sid,
                      "genbank_accession", blank_to_none(srow.accession))
            add_provenance(ctx.provenance_rows, "sequence", canonical_sid, ctx.bvbrc_sid,
                            "bvbrc_genome_sequence", srow.sequence_id)

    # NCBI rows with no BV-BRC counterpart at all -- standalone, as before.
    for r in ncbi.itertuples(index=False):
        base = base_accession(r.accession)
        if base and base in matched_bases:
            continue

        iid = next(ctx.isolate_id_seq)
        ctx.ncbi_isolate_accessions.add(r.accession)
        isolate_rows.append({
            "isolate_id": iid,
            "taxon_id": None,  # ncbi's taxon ids aren't in the bvbrc taxonomy set, so nothing to resolve against
            "primary_strain_name": r.organism_name,
            "lanl_strain_name": None,
            "species": r.species,
            "country": r.country,
            "geo_location": blank_to_none(r.geo_location),
            "host": r.host,
            "tissue_specimen_source": blank_to_none(r.tissue_specimen_source),
            "collection_date": safe_date(r.collection_date),
            "genome_status": r.nuc_completeness,
            "source_id": ctx.ncbi_sid,
            "notes": blank_to_none(r.isolate),
        })
        add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_isolate_designation", r.isolate)
        add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_assembly", r.assembly)
        add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_sra_accession", r.sra_accession)
        add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_biosample", r.biosample)
        add_xref(ctx.xref_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_bioproject", r.bioproject)
        add_provenance(ctx.provenance_rows, "isolate", iid, ctx.ncbi_sid, "ncbi_sequences", r.accession)

        sid = next(ctx.sequence_id_seq)
        ctx.sequence_to_isolate[sid] = iid
        if base:
            ctx.accession_to_sequence.setdefault(base, sid)
        add_xref(ctx.xref_rows, "sequence", sid, ctx.ncbi_sid, "genbank_accession", blank_to_none(r.accession))
        add_provenance(ctx.provenance_rows, "sequence", sid, ctx.ncbi_sid, "ncbi_sequences", r.accession)
        sequence_rows.append({
            "sequence_id": sid,
            "isolate_id": iid,
            "molecule_type": r.molecule_type,
            "sequence_type": "genomic",
            "segment": blank_to_none(r.segment),
            "length": safe_int(r.length),
            "sequence_text": None,  # sequences.csv is metadata-only, no raw sequence text
            "sequence_md5": None,
            "is_reference": False,
            "release_date": safe_date(r.release_date),
            "source_id": ctx.ncbi_sid,
        })

    ctx.merged_isolate_count = len(matched_bases)
    ctx.bvbrc_internal_merged_count = len(dup_genome_map)
    return pd.DataFrame(isolate_rows), pd.DataFrame(sequence_rows)


def build_features_and_proteins(ctx: BuildContext, genome_feature: pd.DataFrame):
    feature_rows = []
    protein_rows = []

    for r in genome_feature.itertuples(index=False):
        fid = next(ctx.feature_id_seq)
        feature_rows.append({
            "feature_id": fid,
            "sequence_id": ctx.raw_seqid_to_sequence.get(r.sequence_id),
            "feature_type": r.feature_type,
            "start": safe_int(r.start),
            "end": safe_int(r.end),
            "strand": r.strand,
            "gene": blank_to_none(r.gene),
            "product": r.product,
            "annotation": r.annotation,
            "source_id": ctx.bvbrc_sid,
        })
        add_provenance(ctx.provenance_rows, "feature", fid, ctx.bvbrc_sid, "bvbrc_genome_feature", r.feature_id)

        aa_len = safe_int(r.aa_length)
        if aa_len is None:
            continue  # not a protein-coding feature (e.g. UTR, tRNA) — no protein row for it

        pid = next(ctx.protein_id_seq)
        ctx.raw_featureid_to_protein[r.feature_id] = pid
        add_provenance(ctx.provenance_rows, "protein", pid, ctx.bvbrc_sid, "bvbrc_genome_feature", r.feature_id)
        protein_rows.append({
            "protein_id": pid,
            "feature_id": fid,
            "sequence_text": None,  # aa sequence isn't carried in genome_feature, only md5/length
            "aa_length": aa_len,
            "product": r.product,
            "gene": blank_to_none(r.gene),
            "source_id": ctx.bvbrc_sid,
        })

        ncbi_prot = blank_to_none(r.protein_id)
        if ncbi_prot:
            ctx.ncbi_protein_id_to_protein[ncbi_prot] = pid
        add_xref(ctx.xref_rows, "protein", pid, ctx.bvbrc_sid, "ncbi_protein_id", ncbi_prot)

        patric_id = blank_to_none(r.patric_id)
        if patric_id:
            ctx.patric_id_to_protein[patric_id] = pid
        add_xref(ctx.xref_rows, "protein", pid, ctx.bvbrc_sid, "patric_id", patric_id)

        add_xref(ctx.xref_rows, "protein", pid, ctx.bvbrc_sid, "refseq_locus_tag", r.refseq_locus_tag)

    return pd.DataFrame(feature_rows), pd.DataFrame(protein_rows)


def build_structures(ctx: BuildContext, protein_structure: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in protein_structure.itertuples(index=False):
        protein_id = ctx.raw_featureid_to_protein.get(blank_to_none(r.feature_id))

        # uniprotkb_accession can hold several accessions joined by "|"
        # (one per chain in a multi-chain PDB entry) — record each as its
        # own xref rather than one glued-together value
        uniprot_raw = blank_to_none(r.uniprotkb_accession)
        uniprot_ids = [u for u in (p.strip() for p in uniprot_raw.split("|"))] if uniprot_raw else []
        uniprot_ids = [u for u in uniprot_ids if u]

        if protein_id is None:
            for u in uniprot_ids:
                if u in ctx.uniprot_to_protein:
                    protein_id = ctx.uniprot_to_protein[u]
                    break

        if protein_id is not None:
            for u in uniprot_ids:
                if u not in ctx.uniprot_to_protein:
                    ctx.uniprot_to_protein[u] = protein_id
                    add_xref(ctx.xref_rows, "protein", protein_id, ctx.bvbrc_sid, "uniprotkb_accession", u)

        taxon_id = safe_int(r.taxon_id)
        structure_id = next(ctx.structure_id_seq)
        rows.append({
            "structure_id": structure_id,
            "protein_id": protein_id,
            "pdb_id": r.pdb_id,
            "resolution": safe_float(r.resolution),
            "method": r.method,
            "title": r.title,
            "release_date": safe_date(r.release_date),
            "organism_name": r.organism_name,
            "taxon_id": taxon_id if taxon_id in ctx.taxon_ids_present else None,
            "source_id": ctx.bvbrc_sid,
            "sequence_text": blank_to_none(r.sequence),
        })
        add_provenance(ctx.provenance_rows, "structure", structure_id, ctx.bvbrc_sid,
                        "bvbrc_protein_structure", r.pdb_id)
    return pd.DataFrame(rows)


def build_bvbrc_epitopes(ctx: BuildContext, epitope: pd.DataFrame):
    """Appends to ctx.epitope_rows / ctx.epitope_assay_rows / ctx.xref_rows."""
    for r in epitope.itertuples(index=False):
        eid = next(ctx.epitope_id_seq)
        protein_id = ctx.resolve_protein(ncbi_protein_id=r.protein_id, uniprot_accession=r.protein_accession)
        epitope_taxon_id = safe_int(r.taxon_id)
        ctx.epitope_rows.append({
            "epitope_id": eid,
            "protein_id": protein_id,
            "epitope_sequence": r.epitope_sequence,
            "start": safe_int(r.start),
            "end": safe_int(r.end),
            "epitope_type": r.epitope_type,
            "host_species": r.host_name,
            "organism": r.organism,
            "taxon_id": epitope_taxon_id if epitope_taxon_id in ctx.taxon_ids_present else None,
            "source_id": ctx.bvbrc_sid,
        })
        add_xref(ctx.xref_rows, "epitope", eid, ctx.bvbrc_sid, "bvbrc_epitope_id", r.epitope_id)
        add_provenance(ctx.provenance_rows, "epitope", eid, ctx.bvbrc_sid, "bvbrc_epitope", r.epitope_id)

        for count_val, assay_type in (
            (r.bcell_assays, "B cell"),
            (r.tcell_assays, "T cell"),
            (r.mhc_assays, "MHC binding"),
        ):
            # these columns hold "positive/total" strings (e.g. "4/4", "7/33"),
            # not plain counts, so total_assays comes from the denominator
            raw_val = blank_to_none(count_val)
            if not raw_val:
                continue
            total = safe_int(raw_val.split("/")[-1]) if "/" in raw_val else safe_int(raw_val)
            ctx.epitope_assay_rows.append({
                "epitope_id": eid,
                "assay_type": assay_type,
                "mhc_allele": None,
                "host_species": r.host_name,
                "assay_outcome": raw_val,
                "total_assays": total,
                "pmid": None,
                "source_id": ctx.bvbrc_sid,
            })


def build_antibodies(ctx: BuildContext, antibody: pd.DataFrame) -> pd.DataFrame:
    """Appends hfv-derived epitope rows + antibody_epitope_rows + xrefs as a side effect."""
    rows = []
    for r in antibody.itertuples(index=False):
        aid = next(ctx.antibody_id_seq)
        neut_raw = blank_to_none(r.neutralizing)
        rows.append({
            "antibody_id": aid,
            "name": r.antibody_name,
            "alias": blank_to_none(r.alias),
            "isotype": blank_to_none(r.isotype),
            "isolation_host": blank_to_none(r.isolation_host),
            "neutralizing": (neut_raw.lower() in NEUTRALIZING_TRUE) if neut_raw else None,
            "donor_outcome": blank_to_none(r.donor_outcome),
            "immunogen": blank_to_none(r.immunogen),
            "source_id": ctx.hfv_sid,
        })
        add_xref(ctx.xref_rows, "antibody", aid, ctx.hfv_sid, "iedb_id", hfv_clean(r.iedb_id))
        add_provenance(ctx.provenance_rows, "antibody", aid, ctx.hfv_sid, "hfv_ebola_antibody", r.iedb_id)

        if blank_to_none(r.epitope_location_sequence) or blank_to_none(r.protein):
            eid = ctx.get_or_create_hfv_epitope(
                r.protein, r.epitope_location_sequence, blank_to_none(r.epitope_type),
                iedb_id=r.iedb_id,
                raw_table="hfv_ebola_antibody", raw_pk=r.iedb_id,
            )
            ctx.antibody_epitope_rows.append({
                "antibody_id": aid,
                "epitope_id": eid,
                "protein_id": None,
                "binding_comment": blank_to_none(r.epitope_and_binding_comment),
            })

    return pd.DataFrame(rows)


def build_ctl_assays(ctx: BuildContext, ctl: pd.DataFrame):
    """Appends T-cell epitope_assay rows (+ hfv-derived epitope rows as needed)."""
    for r in ctl.itertuples(index=False):
        if not blank_to_none(r.peptide):
            continue
        eid = ctx.get_or_create_hfv_epitope(
            r.protein, r.peptide, "Linear peptide",
            host_species=blank_to_none(r.host_species_mouse_mus_musculus),
            organism=blank_to_none(r.species),
            iedb_id=r.iedb_id,
            raw_table="hfv_ebola_ctl", raw_pk=r.iedb_id,
        )
        ctx.epitope_assay_rows.append({
            "epitope_id": eid,
            "assay_type": "T cell",
            "mhc_allele": blank_to_none(r.hla) or blank_to_none(r.mhc),
            "host_species": blank_to_none(r.host_species_mouse_mus_musculus),
            "assay_outcome": blank_to_none(r.assay) or blank_to_none(r.predicted),
            "total_assays": None,
            "pmid": blank_to_none(r.pmid),
            "source_id": ctx.hfv_sid,
        })


ALIGNMENT_FILES = [("F15AG1", "hfv_f15ag1"), ("F15OG1", "hfv_f15og1"), ("F15RG1", "hfv_f15rg1")]


def build_alignments(ctx: BuildContext):
    alignment_rows = []
    member_rows = []

    for name, table in ALIGNMENT_FILES:
        aln_id = next(ctx.alignment_id_seq)
        alignment_rows.append({
            "alignment_id": aln_id,
            "name": name,
            "alignment_type": "multiple sequence alignment",
            "method": "LANL HFV curated alignment",
            "source_id": ctx.hfv_sid,
        })
        for order, r in enumerate(read_raw(table).itertuples(index=False)):
            # strain_label ends in a GenBank accession, e.g.
            # ".../Yambuku-Mayinga/NC_002549" -> "NC_002549"
            accession = r.strain_label.rsplit("/", 1)[-1] if r.strain_label else None
            seq_id = ctx.accession_to_sequence.get(base_accession(accession)) if accession else None
            isolate_id = ctx.sequence_to_isolate.get(seq_id) if seq_id is not None else None
            member_id = next(ctx.alignment_member_id_seq)
            member_rows.append({
                "alignment_member_id": member_id,
                "alignment_id": aln_id,
                "sequence_id": seq_id,
                "isolate_id": isolate_id,
                "strain_label": r.strain_label,
                "aligned_sequence": r.aligned_sequence,
                "row_order": order,
            })
            add_provenance(ctx.provenance_rows, "alignment_member", member_id, ctx.hfv_sid, table, r.strain_label)

    return pd.DataFrame(alignment_rows), pd.DataFrame(member_rows)


def build_hfv_features(ctx: BuildContext, ebov_features: pd.DataFrame) -> pd.DataFrame:
    """
    hfv_ebov_features rows are all coordinates on the Mayinga reference
    genome (NC_002549) specifically -- unlike bvbrc_genome_feature, which is
    per-isolate -- so every row attaches to that one reference sequence.
    """
    rows = []
    reference_sequence_id = ctx.accession_to_sequence.get("NC_002549")
    if reference_sequence_id is None:
        print("core.feature (hfv_ebov_features): skipped, NC_002549 not found among core.sequence accessions")
        return pd.DataFrame(rows)

    for r in ebov_features.itertuples(index=False):
        fid = next(ctx.feature_id_seq)
        rows.append({
            "feature_id": fid,
            "sequence_id": reference_sequence_id,
            "feature_type": blank_to_none(r.feature_type),
            "start": safe_int(r.start),
            "end": safe_int(r.stop),
            "strand": None,
            "gene": None,
            "product": blank_to_none(r.feature),
            "annotation": blank_to_none(r.note),
            "source_id": ctx.hfv_sid,
        })
        add_xref(ctx.xref_rows, "feature", fid, ctx.hfv_sid, "pmid", r.ref1pmid)
        add_xref(ctx.xref_rows, "feature", fid, ctx.hfv_sid, "pmid", r.ref2pmid)
        add_provenance(ctx.provenance_rows, "feature", fid, ctx.hfv_sid, "hfv_ebov_features", r.feature)

    return pd.DataFrame(rows)


def apply_hfv_isolate_enrichment(ctx: BuildContext, conn, annotation: pd.DataFrame) -> int:
    """
    hfv_ebola_annotation_web doesn't mint new isolates -- it enriches ones
    already built from bvbrc_genome/ncbi_sequences, matched by GenBank
    accession. COALESCE keeps whatever the isolate already had if this table
    doesn't cover it (e.g. an isolate BV-BRC/NCBI provided that LANL didn't
    curate), and lets a re-run stay idempotent.
    """
    updated = 0
    for r in annotation.itertuples(index=False):
        accession = blank_to_none(r.accession)
        seq_id = ctx.accession_to_sequence.get(base_accession(accession)) if accession else None
        isolate_id = ctx.sequence_to_isolate.get(seq_id) if seq_id is not None else None
        if isolate_id is None:
            continue

        # e.g. "EBOV/H.sap-tc/COD/76/Yambuku-Mayinga/NC_002549" -> drop the
        # trailing accession to match the LANL nomenclature documented on
        # core.isolate.lanl_strain_name.
        name = blank_to_none(r.modified_standardized_name)
        lanl_strain_name = name.rsplit("/", 1)[0] if name else None

        conn.execute(
            text(
                """
                UPDATE core.isolate
                SET lanl_strain_name = COALESCE(lanl_strain_name, :lanl_strain_name),
                    patient_outcome = COALESCE(patient_outcome, :patient_outcome),
                    patient_age = COALESCE(patient_age, :patient_age),
                    patient_sex = COALESCE(patient_sex, :patient_sex),
                    symptom_onset_date = COALESCE(symptom_onset_date, :symptom_onset_date),
                    death_date = COALESCE(death_date, :death_date)
                WHERE isolate_id = :isolate_id
                """
            ),
            {
                "lanl_strain_name": lanl_strain_name,
                "patient_outcome": hfv_clean(r.patient_outcome),
                "patient_age": hfv_clean(r.patient_age),
                "patient_sex": hfv_clean(r.patient_sex),
                "symptom_onset_date": safe_date(r.patient_date_of_symptoms_onset),
                "death_date": safe_date(r.patient_date_of_death),
                "isolate_id": isolate_id,
            },
        )
        updated += 1
    return updated


def main():
    source_ids = ensure_sources()
    ctx = BuildContext(
        bvbrc_sid=source_ids[BVBRC],
        ncbi_sid=source_ids[NCBI],
        hfv_sid=source_ids[HFV],
    )

    truncate(
        "core.alignment_member", "core.alignment",
        "core.antibody_epitope", "core.antibody",
        "core.epitope_assay", "core.epitope",
        "core.structure",
        "core.protein",
        "core.feature",
        "core.sequence_field_source", "core.sequence",
        "core.isolate_field_source", "core.isolate", "core.taxon",
    )

    write_core(build_taxon(ctx), "taxon")

    genome = read_raw("bvbrc_genome")
    ncbi = read_raw("ncbi_sequences")
    genome_sequence = read_raw("bvbrc_genome_sequence")
    isolate_df, sequence_df = build_isolates_and_sequences(ctx, genome, genome_sequence, ncbi)
    write_core(
        isolate_df, "isolate",
        f" ({len(ctx.genome_id_to_isolate)} bvbrc, {len(ctx.ncbi_isolate_accessions)} ncbi,"
        f" {ctx.merged_isolate_count} merged bvbrc+ncbi,"
        f" {ctx.bvbrc_internal_merged_count} bvbrc-internal reloads folded)",
    )
    write_core(pd.DataFrame(ctx.field_source_rows), "isolate_field_source")
    write_core(sequence_df, "sequence")
    write_core(pd.DataFrame(ctx.sequence_field_source_rows), "sequence_field_source")

    genome_feature = read_raw("bvbrc_genome_feature")
    feature_df, protein_df = build_features_and_proteins(ctx, genome_feature)
    write_core(feature_df, "feature")
    write_core(protein_df, "protein", f" ({len(ctx.raw_featureid_to_protein)} CDS/protein-coding)")

    protein_structure = read_raw("bvbrc_protein_structure")
    write_core(build_structures(ctx, protein_structure), "structure")

    epitope_df = read_raw("bvbrc_epitope")
    build_bvbrc_epitopes(ctx, epitope_df)

    antibody = read_raw("hfv_ebola_antibody")
    antibody_df = build_antibodies(ctx, antibody)
    write_core(antibody_df, "antibody")

    ctl = read_raw("hfv_ebola_ctl")
    build_ctl_assays(ctx, ctl)

    write_core(pd.DataFrame(ctx.epitope_rows), "epitope", f" ({len(ctx.hfv_epitope_lookup)} from hfv)")
    write_core(pd.DataFrame(ctx.epitope_assay_rows), "epitope_assay")
    write_core(pd.DataFrame(ctx.antibody_epitope_rows), "antibody_epitope")

    alignment_df, alignment_member_df = build_alignments(ctx)
    write_core(alignment_df, "alignment")
    member_df = write_core(alignment_member_df, "alignment_member")
    matched = member_df["sequence_id"].notna().sum()
    print(f"  ({matched} matched to a core.sequence)")

    ebov_features = read_raw("hfv_ebov_features")
    write_core(build_hfv_features(ctx, ebov_features), "feature", " (from hfv_ebov_features)")

    annotation_web = read_raw("hfv_ebola_annotation_web")
    with engine.begin() as conn:
        enriched = apply_hfv_isolate_enrichment(ctx, conn, annotation_web)
    print(f"core.isolate: {enriched} rows enriched from hfv_ebola_annotation_web")

    write_xref(ctx.xref_rows, ENTITY_TYPES_OWNED)
    write_provenance(ctx.provenance_rows, PROVENANCE_TABLES_OWNED)

    print("Finished BV-BRC/NCBI/HFV core ETL.")


if __name__ == "__main__":
    main()

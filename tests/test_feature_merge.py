"""
Regression tests for the BV-BRC feature/protein merge in
core_data_loader.etl.bvbrc_hfv.

BV-BRC's genome_feature.csv lists every feature twice, once per annotation
pipeline, at identical coordinates:

    PATRIC.1126254.3.JN408064.CDS.103.2190.fwd   annotation=PATRIC
    RefSeq.1126254.3.JN408064.CDS.103.2190.fwd   annotation=RefSeq

Both describe the same CDS and report the same NCBI protein, but only the
PATRIC row carries a patric_id and only the RefSeq row a refseq_locus_tag.
Emitting one core row per raw row produced two half-populated records for one
real feature -- 65,838 redundant features, a third of core.feature.
"""

import pandas as pd
import pytest

from core_data_loader.etl.bvbrc_hfv import BuildContext, build_features_and_proteins


def _ctx(seq_map=None):
    ctx = BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)
    ctx.raw_seqid_to_sequence = {"S1": 10, "S2": 20} if seq_map is None else seq_map
    return ctx


def _row(feature_id, annotation, sequence_id="S1", feature_type="CDS",
         start="103", end="2190", strand="+", gene="NP", product="nucleoprotein",
         aa_length="695", protein_id="AEW11935.1", patric_id="", refseq_locus_tag=""):
    return {
        "feature_id": feature_id, "annotation": annotation, "sequence_id": sequence_id,
        "feature_type": feature_type, "start": start, "end": end, "strand": strand,
        "gene": gene, "product": product, "aa_length": aa_length,
        "protein_id": protein_id, "patric_id": patric_id,
        "refseq_locus_tag": refseq_locus_tag,
    }


def _frame(*rows):
    return pd.DataFrame(list(rows))


PATRIC_ROW = _row("PATRIC.x.CDS.103.2190.fwd", "PATRIC", patric_id="fig|1.CDS.1")
REFSEQ_ROW = _row("RefSeq.x.CDS.103.2190.fwd", "RefSeq", refseq_locus_tag="LOCUS1")


def test_the_two_pipeline_rows_collapse_to_one_feature_and_one_protein():
    features, proteins = build_features_and_proteins(_ctx(), _frame(PATRIC_ROW, REFSEQ_ROW))
    assert len(features) == 1
    assert len(proteins) == 1


def test_identifiers_from_both_rows_land_on_the_one_protein():
    # The whole point of merging rather than dropping a row: before this, the
    # patric_id and the refseq_locus_tag lived on two different proteins.
    ctx = _ctx()
    build_features_and_proteins(ctx, _frame(PATRIC_ROW, REFSEQ_ROW))
    by_type = {x["xref_type"]: x["xref_value"] for x in ctx.xref_rows}
    assert by_type["patric_id"] == "fig|1.CDS.1"
    assert by_type["refseq_locus_tag"] == "LOCUS1"
    assert by_type["ncbi_protein_id"] == "AEW11935.1"
    assert len({x["entity_id"] for x in ctx.xref_rows}) == 1


def test_every_raw_feature_id_resolves_to_the_surviving_protein():
    # build_structures() looks proteins up by raw feature_id, and which
    # pipeline's row a structure cites is arbitrary -- both must work.
    ctx = _ctx()
    build_features_and_proteins(ctx, _frame(PATRIC_ROW, REFSEQ_ROW))
    assert set(ctx.raw_featureid_to_protein) == {PATRIC_ROW["feature_id"], REFSEQ_ROW["feature_id"]}
    assert len(set(ctx.raw_featureid_to_protein.values())) == 1


def test_provenance_keeps_a_row_per_source_record():
    ctx = _ctx()
    build_features_and_proteins(ctx, _frame(PATRIC_ROW, REFSEQ_ROW))
    feature_prov = [p for p in ctx.provenance_rows if p["core_table"] == "feature"]
    assert {p["raw_pk"] for p in feature_prov} == {
        PATRIC_ROW["feature_id"], REFSEQ_ROW["feature_id"]
    }


def test_annotation_lists_both_pipelines_in_a_stable_order():
    # Sorted, so the value doesn't depend on which row the CSV listed first.
    a, _ = build_features_and_proteins(_ctx(), _frame(PATRIC_ROW, REFSEQ_ROW))
    b, _ = build_features_and_proteins(_ctx(), _frame(REFSEQ_ROW, PATRIC_ROW))
    assert a.iloc[0]["annotation"] == "PATRIC; RefSeq"
    assert b.iloc[0]["annotation"] == "PATRIC; RefSeq"


def test_a_gene_row_over_the_same_coordinates_stays_separate():
    # RefSeq emits a `gene` feature spanning the same range as its CDS. Those
    # are different features; feature_type is part of the key.
    gene_row = _row("RefSeq.x.gene.103.2190.fwd", "RefSeq", feature_type="gene", aa_length="")
    features, proteins = build_features_and_proteins(
        _ctx(), _frame(PATRIC_ROW, REFSEQ_ROW, gene_row))
    assert len(features) == 2
    assert len(proteins) == 1, "a non-coding feature gets no protein row"


def test_features_on_different_sequences_do_not_merge():
    other = _row("PATRIC.y.CDS.103.2190.fwd", "PATRIC", sequence_id="S2")
    features, _ = build_features_and_proteins(_ctx(), _frame(PATRIC_ROW, other))
    assert len(features) == 2


def test_features_at_different_coordinates_do_not_merge():
    other = _row("PATRIC.x.CDS.500.900.fwd", "PATRIC", start="500", end="900")
    features, _ = build_features_and_proteins(_ctx(), _frame(PATRIC_ROW, other))
    assert len(features) == 2


def test_unresolved_sequences_fall_back_to_the_raw_id_and_stay_apart():
    # Both resolve to sequence_id NULL. Keying on the resolved id alone would
    # collapse every unresolved feature in the dataset into one row.
    a = _row("PATRIC.a.CDS.1.587.fwd", "PATRIC", sequence_id="UNKNOWN_A")
    b = _row("PATRIC.b.CDS.1.587.fwd", "PATRIC", sequence_id="UNKNOWN_B")
    features, _ = build_features_and_proteins(_ctx(seq_map={}), _frame(a, b))
    assert len(features) == 2
    assert features["sequence_id"].isna().all()


def test_reloaded_genomes_resolving_to_one_sequence_do_merge():
    # The other half of the key's design: two raw sequence ids that BV-BRC
    # reloaded and that resolve onto one core.sequence should collapse.
    a = _row("PATRIC.a.CDS.103.2190.fwd", "PATRIC", sequence_id="S1")
    b = _row("PATRIC.b.CDS.103.2190.fwd", "PATRIC", sequence_id="S1b")
    features, _ = build_features_and_proteins(
        _ctx(seq_map={"S1": 10, "S1b": 10}), _frame(a, b))
    assert len(features) == 1


def test_non_coding_features_produce_no_protein():
    utr = _row("RefSeq.x.UTR.1.50.fwd", "RefSeq", feature_type="UTR", aa_length="")
    features, proteins = build_features_and_proteins(_ctx(), _frame(utr))
    assert len(features) == 1
    assert proteins.empty


# --- unresolved sequences ---------------------------------------------------
# The rows that fail to resolve to a core.sequence are the ones BV-BRC exported
# with a blank accession AND blank coordinates. Coordinates therefore cannot
# separate them, and an earlier version of the key collapsed every unanchored
# feature of one type into a single row across genomes.

from core_data_loader.etl.bvbrc_hfv import _pipeline_neutral_feature_id


def _blank_row(feature_id, annotation="PATRIC"):
    return _row(feature_id, annotation, sequence_id="", feature_type="regulatory",
                start="", end="", strand="-", gene="", product="putative transcription start signal",
                aa_length="")


def test_unanchored_features_from_different_genomes_stay_apart():
    # The real defect: three genomes, one merged row.
    rows = [_blank_row("PATRIC.186538.3181..regulatory...rev"),
            _blank_row("PATRIC.186538.3277..regulatory...rev"),
            _blank_row("PATRIC.186538.3499..regulatory...rev")]
    features, _ = build_features_and_proteins(_ctx(seq_map={}), _frame(*rows))
    assert len(features) == 3


def test_unanchored_pipeline_pair_still_merges():
    # Losing the coordinates must not lose the PATRIC/RefSeq pairing.
    rows = [_blank_row("PATRIC.186538.3181..regulatory...rev", "PATRIC"),
            _blank_row("RefSeq.186538.3181..regulatory...rev", "RefSeq")]
    features, _ = build_features_and_proteins(_ctx(seq_map={}), _frame(*rows))
    assert len(features) == 1
    assert features.iloc[0]["annotation"] == "PATRIC; RefSeq"


@pytest.mark.parametrize("raw,expected", [
    ("PATRIC.186538.3181..regulatory...rev", "186538.3181..regulatory...rev"),
    ("RefSeq.186538.3181..regulatory...rev", "186538.3181..regulatory...rev"),
    ("PATRIC.1126254.3.JN408064.CDS.103.2190.fwd", "1126254.3.JN408064.CDS.103.2190.fwd"),
    # An id from neither pipeline is left whole rather than mangled.
    ("Other.1.2.3", "Other.1.2.3"),
    ("nodots", "nodots"),
    ("", None),
])
def test_pipeline_neutral_feature_id(raw, expected):
    assert _pipeline_neutral_feature_id(raw) == expected


# --- multi-chain taxon ids -----------------------------------------------------
# BV-BRC joins one taxon per chain with "|" on a structure. safe_int() cannot
# parse that, so all 53 antibody-antigen complexes lost their organism.

from core_data_loader.etl.bvbrc_hfv import resolve_taxon


def _taxa_ctx(*present):
    ctx = BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)
    ctx.taxon_ids_present = set(present)
    return ctx


@pytest.mark.parametrize("raw,expected", [
    ("10090|186540", 186540),       # mouse antibody | Sudan virus -> the virus
    ("186540|10090", 186540),       # order does not matter
    ("9844|128948", 128948),        # llama nanobody | Sudan strain
    ("1570291|9606|186538", 1570291),  # two viral ids: first present wins
    ("186540", 186540),             # the ordinary single-id case is unchanged
])
def test_resolve_taxon_picks_the_viral_id(raw, expected):
    ctx = _taxa_ctx(186540, 128948, 1570291, 186538)
    assert resolve_taxon(ctx, raw) == expected


def test_resolve_taxon_returns_none_when_no_part_is_a_known_taxon():
    # Human and mouse only: nothing to link to, rather than a wrong link.
    assert resolve_taxon(_taxa_ctx(186540), "9606|10090") is None


def test_resolve_taxon_handles_blank_and_garbage():
    ctx = _taxa_ctx(186540)
    assert resolve_taxon(ctx, "") is None
    assert resolve_taxon(ctx, None) is None
    assert resolve_taxon(ctx, "not-a-number|186540") == 186540

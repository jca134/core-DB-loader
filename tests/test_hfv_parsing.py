"""Unit tests for LANL/HFV value parsing in etl_bvbrc_hfv."""

import pandas as pd

from core_data_loader.etl.etl_bvbrc_hfv import (
    BuildContext,
    build_antibodies,
    hfv_clean,
    parse_neutralizing,
)


class TestHfvClean:
    def test_underscore_placeholder_is_none(self):
        assert hfv_clean("_") is None
        assert hfv_clean(" _ ") is None

    def test_real_value_kept(self):
        assert hfv_clean("HLA-A*02:01") == "HLA-A*02:01"


class TestParseNeutralizing:
    def test_strain_qualified_yes_is_true(self):
        assert parse_neutralizing("yes (Mayinga)") is True
        assert parse_neutralizing("yes ") is True

    def test_explicit_no_is_false(self):
        assert parse_neutralizing("no") is False

    def test_placeholder_and_unrecognized_are_unknown(self):
        assert parse_neutralizing("_") is None
        assert parse_neutralizing("partial") is None
        assert parse_neutralizing("") is None


class TestGetOrCreateHfvEpitope:
    """
    LANL gives several antibody rows the same protein + epitope location while
    only some of them carry an IEDB ID -- 2G4 and 4G7 both report "Q 508" on
    GP, but only 4G7's row has one (156605). These cover the collapsed row
    keeping every contributing row's identifiers regardless of load order.
    """

    @staticmethod
    def _ctx():
        return BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)

    def _mint_pair(self, first_iedb, second_iedb):
        ctx = self._ctx()
        a = ctx.get_or_create_hfv_epitope(
            "glycoprotein", "Q 508", "discontinuous",
            iedb_id=first_iedb, raw_table="hfv_ebola_antibody", raw_pk="11",
        )
        b = ctx.get_or_create_hfv_epitope(
            "glycoprotein", "Q 508", "discontinuous",
            iedb_id=second_iedb, raw_table="hfv_ebola_antibody", raw_pk="12",
        )
        return ctx, a, b

    def test_same_protein_and_location_collapse_to_one_epitope(self):
        ctx, a, b = self._mint_pair("_", "156605")
        assert a == b
        assert len(ctx.epitope_rows) == 1

    def test_iedb_id_kept_when_placeholder_row_loads_first(self):
        # The regression: keying the xref off the minting row alone dropped
        # 156605 entirely, because 2G4's "_" got there first.
        ctx, _, _ = self._mint_pair("_", "156605")
        assert [x["xref_value"] for x in ctx.xref_rows] == ["156605"]

    def test_iedb_id_kept_when_placeholder_row_loads_last(self):
        ctx, _, _ = self._mint_pair("156605", "_")
        assert [x["xref_value"] for x in ctx.xref_rows] == ["156605"]

    def test_every_contributing_raw_row_is_recorded_in_provenance(self):
        ctx, _, _ = self._mint_pair("_", "156605")
        assert sorted(p["raw_pk"] for p in ctx.provenance_rows) == ["11", "12"]

    def test_placeholder_raw_pk_is_not_stored_as_a_row_key(self):
        ctx = self._ctx()
        ctx.get_or_create_hfv_epitope(
            "glycoprotein", "EYLFEVDNL", "Linear peptide",
            iedb_id="_", raw_table="hfv_ebola_ctl", raw_pk="_",
        )
        assert [p["raw_pk"] for p in ctx.provenance_rows] == [None]

    def test_different_taxon_does_not_collapse(self):
        ctx = self._ctx()
        a = ctx.get_or_create_hfv_epitope("glycoprotein", "Q 508", "discontinuous", taxon_id=186538)
        b = ctx.get_or_create_hfv_epitope("glycoprotein", "Q 508", "discontinuous", taxon_id=3052505)
        assert a != b
        assert len(ctx.epitope_rows) == 2


class TestBuildAntibodies:
    """
    23 LANL antibody rows name only a protein ("glycoprotein"), with no
    location, epitope_type or IEDB id. Those must not mint an epitope row --
    keying them on protein alone used to collapse them into 4 contentless rows
    that asserted shared epitopes nobody had characterised.
    """

    COLUMNS = [
        "order", "antibody_name", "alias", "protein", "epitope_type",
        "epitope_location_sequence", "iedb_id", "isotype", "isolation_host",
        "donor_outcome", "immunogen", "infecting_vaccine_species_and_strain",
        "epitope_and_binding_comment", "neutralizing",
    ]

    def _frame(self, *rows):
        return pd.DataFrame(list(rows), columns=self.COLUMNS)

    def _row(self, order, name, protein, location, comment="_"):
        return [order, name, "_", protein, "_", location, "_", "_", "_",
                "_", "_", "_", comment, "_"]

    def test_protein_only_row_makes_no_epitope(self):
        ctx = BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)
        build_antibodies(ctx, self._frame(
            self._row("1", "KS14", "glycoprotein", "_", comment="binds GP"),
        ))
        assert ctx.epitope_rows == []
        # the antibody and its binding comment still survive
        assert len(ctx.antibody_epitope_rows) == 1
        assert ctx.antibody_epitope_rows[0]["epitope_id"] is None
        assert ctx.antibody_epitope_rows[0]["binding_comment"] == "binds GP"

    def test_protein_only_rows_are_not_collapsed_together(self):
        ctx = BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)
        build_antibodies(ctx, self._frame(
            self._row("1", "KS14", "glycoprotein", "_"),
            self._row("2", "LS4", "glycoprotein", "_"),
        ))
        assert ctx.epitope_rows == []
        assert [r["epitope_id"] for r in ctx.antibody_epitope_rows] == [None, None]

    def test_row_with_a_location_still_makes_one(self):
        ctx = BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)
        build_antibodies(ctx, self._frame(
            self._row("11", "2G4", "glycoprotein", "Q 508"),
        ))
        assert len(ctx.epitope_rows) == 1
        assert ctx.epitope_rows[0]["epitope_sequence"] == "Q 508"
        assert ctx.antibody_epitope_rows[0]["epitope_id"] is not None

    def test_antibody_provenance_uses_the_sheet_order_not_iedb_id(self):
        ctx = BuildContext(bvbrc_sid=1, ncbi_sid=2, hfv_sid=3)
        build_antibodies(ctx, self._frame(
            self._row("11", "2G4", "glycoprotein", "Q 508"),
        ))
        ab = [p for p in ctx.provenance_rows if p["core_table"] == "antibody"]
        assert [p["raw_pk"] for p in ab] == ["11"]

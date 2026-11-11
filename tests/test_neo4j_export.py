"""
The Neo4j export's mapping is data (NODES, EDGES, EVIDENCE_LABELS); these
catch a typo in it before a run does. The row-count comparison in
graph/export.py main() covers the rest against a live database.
"""

from datetime import date
from decimal import Decimal

from core_data_loader.graph.export import (
    EDGES,
    EVIDENCE_LABELS,
    LABEL_KEY,
    NODES,
    _props,
)


def test_every_edge_endpoint_is_a_node_label():
    for src, _rel, dst, _sql in EDGES:
        assert src in LABEL_KEY and dst in LABEL_KEY, (src, dst)


def test_every_evidence_type_maps_to_a_node_label():
    assert set(EVIDENCE_LABELS.values()) <= set(LABEL_KEY)


def test_labels_are_unique():
    labels = [label for label, *_ in NODES]
    assert len(labels) == len(set(labels))


def test_props_drop_nulls_and_bulk_columns_and_convert_decimals():
    props = _props({"a": None, "sequence_text": "ACGT", "source_id": 3,
                    "resolution": Decimal("2.5"), "d": date(2020, 1, 1), "l": ["x", None]})
    assert props == {"resolution": 2.5, "d": date(2020, 1, 1), "l": ["x"]}


def test_display_name_never_silently_replaces_a_real_name_column():
    # SELECT *, <expr> AS name keeps only one `name`: a table with its own name
    # column must either use it as the display name or build on it.
    tables_with_name_column = {"gene_family": "name"}
    for label, table, _key, name_expr in NODES:
        if table in tables_with_name_column:
            assert "name" in name_expr.replace("symbol", ""), label

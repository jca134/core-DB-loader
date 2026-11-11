"""
The Neo4j export's mapping is data (NODES, EDGES, EVIDENCE_LABELS); these
catch a typo in it before a run does. The row-count comparison in
export_neo4j.main() covers the rest against a live database.
"""

from datetime import date
from decimal import Decimal

from core_data_loader.export.export_neo4j import EDGES, EVIDENCE_LABELS, LABEL_KEY, NODES, _props


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

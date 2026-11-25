"""
Unit tests for core_data_loader.graph.explore -- the record format shared by
the dashboard and the live explorer behind `filovirus serve` (/explore), the
explorer's search ranking, and its subgraph walk.

No Neo4j: FakeGraph answers the handful of Cypher queries LiveExplorer sends
from an in-memory graph.
"""

import re

from core_data_loader.graph.explore import LABELS, LiveExplorer, node_row


class FakeGraph:
    def __init__(self, nodes, edges):
        self.nodes = nodes      # element id -> (label, properties)
        self.edges = edges      # (source id, type, target id)
        self.calls = 0
        self.confidence = {}    # frozenset({a, b}) -> edge confidence

    def _deg(self, i):
        return sum((a == i) + (b == i) for a, _, b in self.edges)

    def run(self, cypher, params=None):
        self.calls += 1
        ids = (params or {}).get("ids", [])
        if "RETURN count(n) AS c" in cypher:
            return [{"c": len(self.nodes)}]
        m = re.match(r"MATCH \(n:(\w+)\) RETURN elementId", cypher)
        if m:
            return [{"id": i, "p": p, "deg": self._deg(i)} for i, (label, p) in self.nodes.items() if label == m.group(1)]
        if "CHILD_OF*1.." in cypher:
            found, todo = set(), list(ids)
            while todo:
                t = todo.pop()
                for a, rel, b in self.edges:
                    if rel == "CHILD_OF" and b == t and a not in found:
                        found.add(a)
                        todo.append(a)
            return [{"c": c} for c in found]
        if "MATCH (n)-[e]-(m)" in cypher:
            out = {(a, b) for a, _, b in self.edges if a in ids} | {(b, a) for a, _, b in self.edges if b in ids}
            return [{"n": n, "m": m, "c": self.confidence.get(frozenset((n, m)), 1.0)} for n, m in out]
        if "MATCH (a)-[e]->(b)" in cypher:
            return [{"a": a, "t": t, "b": b, "c": None, "m": None, "sc": None} for a, t, b in self.edges if a in ids]
        raise AssertionError(f"unexpected query: {cypher}")


def graph():
    nodes = {
        "g": ("Gene", {"gene_id": 1, "gene_symbol": "VP35", "name": "VP35"}),
        "f": ("GeneFamily", {"gene_family_id": 2, "symbol": "VP35", "name": "VP35: Polymerase cofactor VP35"}),
        "p": ("Protein", {"protein_id": 3, "name": "VP35", "xref_ncbi_protein_id": ["AAA1.1"]}),
        "a": ("Antibody", {"antibody_id": 4, "name": "KZ52"}),
        "t1": ("Taxon", {"taxon_id": 100, "name": "Marburgvirus"}),
        "t2": ("Taxon", {"taxon_id": 101, "name": "Marburg marburgvirus"}),
        "i": ("Isolate", {"isolate_id": 5, "name": "Musoke"}),
    }
    edges = [("g", "IN_FAMILY", "f"), ("p", "IN_FAMILY", "f"), ("a", "TARGETS", "g"),
             ("t2", "CHILD_OF", "t1"), ("i", "OF_TAXON", "t2")]
    return FakeGraph(nodes, edges)


class TestNodeRow:
    def test_sequence_named_by_accession(self):
        row = node_row("Sequence", {"sequence_id": 7, "name": "ssRNA(-)", "xref_genbank_accession": ["KY785972", "KY785972.1"]})
        assert row[0] == LABELS.index("Sequence")
        assert row[2] == "KY785972 ssRNA(-)"

    def test_search_ids_not_repeated_from_shown_properties(self):
        row = node_row("Protein", {"protein_id": 3, "name": "VP35", "xref_ncbi_protein_id": ["AAA1.1"], "aa_length": 340})
        assert row[3] == {"aa_length": 340, "xref_ncbi_protein_id": "AAA1.1"}
        assert row[4] == ""


class TestSearch:
    def test_named_records_rank_before_bulk_ones(self):
        hits = LiveExplorer(graph()).search({"q": "vp35"})["results"]
        assert [LABELS[h["node"][0]] for h in hits] == ["Gene", "GeneFamily", "Protein"]

    def test_finds_a_record_by_its_external_id(self):
        hits = LiveExplorer(graph()).search({"q": "aaa1.1"})["results"]
        assert [h["id"] for h in hits] == ["p"]

    def test_index_is_reused_until_the_graph_changes(self):
        g = graph()
        x = LiveExplorer(g)
        x.search({"q": "kz52"})
        first = g.calls
        x.search({"q": "kz52"})
        assert g.calls == first + 1          # only the node count was asked again
        g.nodes["n"] = ("Antibody", {"antibody_id": 9, "name": "KZ53"})
        assert [h["node"][2] for h in x.search({"q": "kz5"})["results"]] == ["KZ52", "KZ53"]


class TestSubgraph:
    def test_walks_every_node_with_the_links_between_them(self):
        r = LiveExplorer(graph()).subgraph({"ids": ["a"], "hops": 2})
        assert set(r["ids"]) == {"a", "g", "f"}
        pairs = {(r["ids"][r["edges"][k]], r["rels"][r["edges"][k + 2]], r["ids"][r["edges"][k + 1]])
                 for k in range(0, len(r["edges"]), 3)}
        assert pairs == {("a", "TARGETS", "g"), ("g", "IN_FAMILY", "f")}
        assert r["starts"] == [r["ids"].index("a")]

    def test_sub_taxa_start_with_their_parent(self):
        r = LiveExplorer(graph()).subgraph({"taxa": [100], "hops": 1, "subtaxa": True})
        assert set(r["ids"]) == {"t1", "t2", "i"}      # the strain's isolate is one step from the sub-taxon
        assert [r["ids"][k] for k in r["starts"]] == ["t1"]

    def test_records_past_the_start_pass_on_only_their_best_links(self):
        g = graph()
        for k in range(5):      # the gene family becomes a hub: five more proteins point to it
            g.nodes[f"q{k}"] = ("Protein", {"protein_id": 10 + k, "name": f"VP35 {k}"})
            g.edges.append((f"q{k}", "IN_FAMILY", "f"))
        for k in range(4):      # four of them only weak keyword matches
            g.confidence[frozenset((f"q{k}", "f"))] = 0.25
        r = LiveExplorer(g).subgraph({"ids": ["g"], "hops": 2, "per": 2})
        # the start passes on all its links (the family and the antibody); the family passes on
        # its best two of six: the curated links with no confidence (p, q4) count as certain
        assert len(r["ids"]) == 1 + 2 + 2
        assert {"p", "q4"} <= set(r["ids"])
        assert r["limited"] == {r["ids"].index("f"): (2, 7)}
        assert len(LiveExplorer(g).subgraph({"ids": ["g"], "hops": 2})["ids"]) == 9

    def test_unknown_start_is_an_error_not_an_empty_graph(self):
        assert "error" in LiveExplorer(graph()).subgraph({"ids": ["gone"], "hops": 2})

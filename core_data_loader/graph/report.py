"""
A shape report of the Neo4j graph: where it is lopsided.

    filovirus graph-report

SerenQA prunes hub nodes before it walks (paper Appendix D.3), and a node with
no edges can never be reached. This prints, per node label, how many nodes,
how many are isolated, and the mean and largest degree; then the biggest hubs
overall and the isolated nodes that aren't expected to be.

Reads the graph as exported, so run `filovirus export` first.
"""

from neo4j import GraphDatabase

from core_data_loader.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

TOP_HUBS = 15
# Labels whose isolated nodes are expected (lookup vocabulary not every row of
# which is used): not listed one by one.
EXPECTED_ISOLATED = {"Feature", "Protocol", "Treatment", "AssayType", "Symptom", "Condition", "Organization"}


def main():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as s:
        per_label = s.run(
            "MATCH (n) WITH labels(n)[0] AS label, COUNT { (n)--() } AS degree "
            "RETURN label, count(*) AS nodes, sum(CASE WHEN degree = 0 THEN 1 ELSE 0 END) AS isolated, "
            "round(avg(degree), 1) AS mean, max(degree) AS max ORDER BY nodes DESC").data()
        hubs = s.run(
            "MATCH (n) WITH n, COUNT { (n)--() } AS degree ORDER BY degree DESC LIMIT $k "
            "RETURN labels(n)[0] AS label, n.name AS name, degree", k=TOP_HUBS).data()
        isolated = s.run(
            "MATCH (n) WHERE NOT (n)--() AND NOT labels(n)[0] IN $expected "
            "RETURN labels(n)[0] AS label, n.name AS name ORDER BY label, name LIMIT 40",
            expected=list(EXPECTED_ISOLATED)).data()
    driver.close()

    width = max(len(r["label"]) for r in per_label)
    print(f"{'label':<{width}}  {'nodes':>8}  {'isolated':>8}  {'mean deg':>8}  {'max deg':>8}")
    for r in per_label:
        print(f"{r['label']:<{width}}  {r['nodes']:>8,}  {r['isolated']:>8,}  {r['mean']:>8}  {r['max']:>8,}")

    print("\nLargest hubs (SerenQA prunes these before walking):")
    for r in hubs:
        print(f"  {r['degree']:>7,}  {r['label']:<12} {str(r['name'])[:70]}")

    print(f"\nIsolated nodes outside lookup vocabularies ({len(isolated)}"
          f"{'+' if len(isolated) == 40 else ''}):")
    for r in isolated:
        print(f"  {r['label']:<12} {str(r['name'])[:80]}")
    if not isolated:
        print("  none")
    return 0


if __name__ == "__main__":
    main()

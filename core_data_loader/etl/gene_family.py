"""
Seeds core.gene_family and assigns every core.gene and core.protein row to
its filovirus gene family, from core_data_loader/curated/gene_families.py.

Run after etl/bvbrc_hfv.py (which rebuilds core.protein) and etl/ucsc.py
(which rebuilds core.gene); both drop the assignments with their rows. The
families themselves have fixed ids, so nothing downstream goes stale.
"""

from collections import Counter

from sqlalchemy import text

from core_data_loader.common.db_utils import engine
from core_data_loader.curated.gene_families import GENE_FAMILIES, family_for


def main():
    with engine.begin() as conn:
        for family_id, symbol, name, _names in GENE_FAMILIES:
            conn.execute(
                text(
                    """
                    INSERT INTO core.gene_family (gene_family_id, symbol, name)
                    VALUES (:id, :symbol, :name)
                    ON CONFLICT (gene_family_id) DO UPDATE
                        SET symbol = EXCLUDED.symbol, name = EXCLUDED.name
                    """
                ),
                {"id": family_id, "symbol": symbol, "name": name},
            )

        genes = conn.execute(text("SELECT gene_id, gene_symbol FROM core.gene")).fetchall()
        gene_rows = [(gene_id, family_for(symbol, None)[0]) for gene_id, symbol in genes]
        conn.execute(text("UPDATE core.gene SET gene_family_id = NULL"))
        _assign(conn, "core.gene", "gene_id", [(i, f, None) for i, f in gene_rows if f])
        unmapped_genes = [symbol for (gene_id, symbol), (_, f) in zip(genes, gene_rows) if f is None]

        proteins = conn.execute(text("SELECT protein_id, gene, product FROM core.protein")).fetchall()
        assigned, unmapped = [], Counter()
        for protein_id, gene, product in proteins:
            family_id, basis = family_for(gene, product)
            if family_id is None:
                unmapped[(gene or product or "(no label)").strip()] += 1
            else:
                assigned.append((protein_id, family_id, basis))
        conn.execute(text("UPDATE core.protein SET gene_family_id = NULL, gene_family_basis = NULL"))
        _assign(conn, "core.protein", "protein_id", assigned)

    by_basis = Counter(basis for _, _, basis in assigned)
    print(f"core.gene_family: {len(GENE_FAMILIES)} families; "
          f"{len(genes) - len(unmapped_genes)} of {len(genes)} reference genes assigned")
    if unmapped_genes:
        print(f"  ! reference genes with no family: {', '.join(unmapped_genes)}")
    print(f"core.protein: {len(assigned)} of {len(proteins)} assigned "
          f"({by_basis['gene']} by gene label, {by_basis['product']} by product name); "
          f"{sum(unmapped.values())} left unmapped")
    for label, count in unmapped.most_common(8):
        print(f"    unmapped: {label!r} x{count}")


def _assign(conn, table: str, key: str, rows: list[tuple]):
    if not rows:
        return
    basis_sql = ", gene_family_basis = v.basis" if table == "core.protein" else ""
    conn.execute(
        text(
            f"""
            UPDATE {table} t SET gene_family_id = v.family{basis_sql}
            FROM (SELECT unnest(CAST(:ids AS bigint[])) AS id,
                         unnest(CAST(:families AS smallint[])) AS family,
                         unnest(CAST(:bases AS text[])) AS basis) v
            WHERE t.{key} = v.id
            """
        ),
        {"ids": [r[0] for r in rows], "families": [r[1] for r in rows], "bases": [r[2] for r in rows]},
    )


if __name__ == "__main__":
    main()

"""
Fills core.antibody_epitope.target_gene_symbol from the curated mapping in
core_data_loader.common.antibody_targets.

Run after etl_bvbrc_hfv, which builds core.antibody_epitope and truncates it on
every run. It stores a gene symbol rather than a gene_id, so it does not depend
on etl_ucsc and nothing it writes can be invalidated by a core.gene rebuild.
"""

from core_data_loader.common.antibody_targets import ensure_antibody_targets


def main():
    resolved, unresolved = ensure_antibody_targets()
    print(f"core.antibody_epitope.target_gene_symbol: {resolved} resolved, {unresolved} unresolved")
    if unresolved:
        print("  (unresolved rows name a product with no gene mapping, e.g. LANL's \"unknown\")")


if __name__ == "__main__":
    main()

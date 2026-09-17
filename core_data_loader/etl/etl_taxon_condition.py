"""
Populates core.taxon_condition from the curated mappings in
core_data_loader.common.taxon_conditions. Run last, after etl_bvbrc_hfv (or
etl_ucsc) and etl_immport -- it needs core.taxon and core.condition rows to
already exist, and etl_immport truncates core.condition (cascading into
core.taxon_condition) every time it runs.
"""

from core_data_loader.common.taxon_conditions import ensure_taxon_conditions


def main():
    inserted = ensure_taxon_conditions()
    print(f"core.taxon_condition: {inserted} rows")


if __name__ == "__main__":
    main()

"""
Populates core.taxon_condition from the curated mappings in
core_data_loader.common.taxon_conditions. Run last, after etl_bvbrc_hfv and
etl_immport -- it needs core.taxon and core.condition rows to already exist,
and those scripts truncate core.taxon and core.condition respectively
(cascading into core.taxon_condition) every time they run.
"""

from core_data_loader.common.taxon_conditions import ensure_taxon_conditions


def main():
    inserted = ensure_taxon_conditions()
    print(f"core.taxon_condition: {inserted} rows")


if __name__ == "__main__":
    main()

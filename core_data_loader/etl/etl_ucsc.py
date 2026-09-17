import re

from core_data_loader.common.etl_common import (
    engine, ensure_sources, truncate, read_raw, safe_int, blank_to_none,
    add_provenance, write_provenance,
)
from sqlalchemy import text

HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_gene_description(html: str) -> str | None:
    text_only = HTML_TAG_RE.sub(" ", html).strip()
    # Every row starts with a redundant "Gene Description" header once tags
    # are stripped -- drop it rather than repeating it in every row.
    text_only = re.sub(r"^Gene Description\s*", "", text_only)
    return blank_to_none(re.sub(r"\s+", " ", text_only))

PROVENANCE_TABLES_OWNED = ["gene", "gene_domain"]

UCSC = "ucsc"

VERSION_SUFFIX_RE = re.compile(r"v\d+$")


def resolve_sequence_id(conn, chrom: str):
    base_accession = VERSION_SUFFIX_RE.sub("", chrom)
    row = conn.execute(
        text(
            """
            SELECT MIN(entity_id) FROM core.xref
            WHERE entity_type = 'sequence' AND xref_type = 'genbank_accession'
              AND xref_value = :acc
            """
        ),
        {"acc": base_accession},
    ).fetchone()
    return row[0] if row else None


def main():
    sources = ensure_sources()
    ucsc_sid = sources[UCSC]

    truncate("core.gene_domain", "core.gene")

    gene_raw = read_raw("ucsc_ncbi_gene")
    pfam_raw = read_raw("ucsc_ncbi_gene_pfam")
    gene_desc_lookup = {
        r.name: clean_gene_description(r.html)
        for r in read_raw("ucsc_gene_desc").itertuples(index=False)
    }

    with engine.begin() as conn:
        genes = []
        for r in gene_raw.itertuples(index=False):
            genes.append({
                "sequence_id": resolve_sequence_id(conn, r.chrom),
                "gene_symbol": r.name,
                "strand": r.strand,
                "start_pos": safe_int(r.txStart),
                "end_pos": safe_int(r.txEnd),
                "cds_start": safe_int(r.cdsStart),
                "cds_end": safe_int(r.cdsEnd),
                "exon_count": safe_int(r.exonCount),
                "description": gene_desc_lookup.get(r.name),
                "source_id": ucsc_sid,
            })
        gene_ids = [
            conn.execute(
                text(
                    """
                    INSERT INTO core.gene
                        (sequence_id, gene_symbol, strand, start_pos, end_pos,
                         cds_start, cds_end, exon_count, description, source_id)
                    VALUES (:sequence_id, :gene_symbol, :strand, :start_pos, :end_pos,
                            :cds_start, :cds_end, :exon_count, :description, :source_id)
                    RETURNING gene_id
                    """
                ),
                g,
            ).scalar()
            for g in genes
        ]

    gene_lookup = [
        {**g, "gene_id": gid}
        for g, gid in zip(genes, gene_ids)
    ]

    provenance_rows = []
    for gid, r in zip(gene_ids, gene_raw.itertuples(index=False)):
        add_provenance(provenance_rows, "gene", gid, ucsc_sid, "ucsc_ncbi_gene", f"{r.name}:{r.txStart}-{r.txEnd}")

    def find_gene(start: int, end: int):
        containing = [
            g for g in gene_lookup
            if g["start_pos"] is not None and g["end_pos"] is not None
            and g["start_pos"] <= start and end <= g["end_pos"]
        ]
        if not containing:
            return None
        cds_containing = [
            g for g in containing
            if g["cds_start"] is not None and g["cds_end"] is not None
            and g["cds_start"] <= start and end <= g["cds_end"]
        ]
        return (cds_containing or containing)[0]["gene_id"]

    domains = []
    unresolved = 0
    for r in pfam_raw.itertuples(index=False):
        start_pos = safe_int(r.chromStart)
        end_pos = safe_int(r.chromEnd)
        gene_id = find_gene(start_pos, end_pos) if start_pos is not None and end_pos is not None else None
        if gene_id is None:
            unresolved += 1
        domains.append({
            "gene_id": gene_id,
            "pfam_name": r.name,
            "start_pos": start_pos,
            "end_pos": end_pos,
            "score": safe_int(r.score),
            "source_id": ucsc_sid,
        })

    with engine.begin() as conn:
        for d, r in zip(domains, pfam_raw.itertuples(index=False)):
            domain_id = conn.execute(
                text(
                    """
                    INSERT INTO core.gene_domain
                        (gene_id, pfam_name, start_pos, end_pos, score, source_id)
                    VALUES (:gene_id, :pfam_name, :start_pos, :end_pos, :score, :source_id)
                    RETURNING gene_domain_id
                    """
                ),
                d,
            ).scalar()
            add_provenance(provenance_rows, "gene_domain", domain_id, ucsc_sid,
                            "ucsc_ncbi_gene_pfam", f"{r.name}:{r.chromStart}-{r.chromEnd}")

    write_provenance(provenance_rows, PROVENANCE_TABLES_OWNED)

    print(f"core.gene: {len(genes)} rows")
    print(f"core.gene_domain: {len(domains)} rows ({unresolved} unresolved to a gene)")
    print("Finished UCSC core ETL.")


if __name__ == "__main__":
    main()

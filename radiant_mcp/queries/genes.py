"""SQL of ``gene_info``: gene panels and gnomAD constraint."""

from typing import Optional

from .. import sql as _sql

def gene_queries(gene: str, ensembl_gene_id: Optional[str], tenant: str) -> dict[str, tuple[str, _sql.Params]]:
    shared, tenant_db = _sql.shared_db(), _sql.tenant_db(tenant)
    queries: dict[str, tuple[str, _sql.Params]] = {}

    def add(name: str, build) -> None:
        p = _sql.Params()
        queries[name] = (build(p), p)

    add("omim", lambda p: (
        f"SELECT panel AS condition_name, omim_phenotype_id, omim_gene_id, inheritance_code "
        f"FROM {shared}.omim_gene_panel WHERE symbol = {p(gene)} ORDER BY panel LIMIT 50"))
    add("hpo", lambda p: (
        f"SELECT hpo_term_id, hpo_term_name FROM {shared}.hpo_gene_panel "
        f"WHERE symbol = {p(gene)} ORDER BY hpo_term_id"))
    add("orphanet", lambda p: (
        f"SELECT panel AS disorder, disorder_id, inheritance_code FROM {shared}.orphanet_gene_panel "
        f"WHERE symbol = {p(gene)} ORDER BY panel LIMIT 50"))
    add("ddd", lambda p: f"SELECT panel FROM {shared}.ddd_gene_panel WHERE symbol = {p(gene)} ORDER BY panel")
    add("cosmic", lambda p: f"SELECT panel FROM {shared}.cosmic_gene_panel WHERE symbol = {p(gene)} ORDER BY panel")
    add("tenant_panels", lambda p: (
        f"SELECT DISTINCT panel FROM {tenant_db}.gene_panel_mv WHERE symbol = {p(gene)} ORDER BY panel"))
    if ensembl_gene_id:
        # gnomAD constraint is per transcript: the gene's transcripts come from
        # the Ensembl exon table (keyed by gene_id); versions are ignored.
        add("constraint", lambda p: (
            "SELECT DISTINCT c.transcript_id, c.pli, c.loeuf "
            f"FROM (SELECT DISTINCT split_part(t.transcript_id, '.', 1) AS transcript_id "
            f"      FROM {shared}.ensembl_exon_by_gene e, unnest(e.transcript_ids) AS t(transcript_id) "
            f"      WHERE e.gene_id = {p(ensembl_gene_id)}) g "
            f"JOIN {shared}.gnomad_constraint c ON split_part(c.transcript_id, '.', 1) = g.transcript_id "
            "ORDER BY c.pli DESC LIMIT 10"))
    return queries

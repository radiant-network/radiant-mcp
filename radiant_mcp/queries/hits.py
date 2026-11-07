"""SQL of ``second_hit``. Every occurrence query filters on its key's ``part``."""

from .. import sql as _sql
from ..domain import rules as _rules

# Rows a list returns; one more is fetched to tell a truncated list.
ROW_LIMIT = 200


def _plp_condition(p: _sql.Params, interpretation_alias: str = "ig") -> str:
    return (
        f"(arrays_overlap(v.clinvar_interpretation, [{p.list(_rules.CLINVAR_PLP)}]) "
        f"OR o.exomiser_acmg_classification IN ({p.list(_rules.EXOMISER_PLP)}) "
        f"OR {interpretation_alias}.locus_id IS NOT NULL)"
    )


def _interpretations(p: _sql.Params, tenant_db: str, seq_id: int) -> str:
    """Loci of a sequencing that a geneticist classified P/LP in the portal."""
    return (
        f"(SELECT DISTINCT locus_id FROM {tenant_db}.interpretation_germline "
        f"WHERE sequencing_id = {p(str(seq_id))} AND classification IN ({p.list(_rules.INTERPRETATION_PLP)}))"
    )


def snv_cnv_query(tenant_db: str, snv_key: dict, cnv_key: dict) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        "WITH snv AS ("
        " SELECT o.locus_id, o.zygosity, o.transmission_mode, v.symbol, v.hgvsg, v.aa_change, v.vep_impact,"
        "  v.gnomad_v3_af, v.clinvar_interpretation, o.exomiser_acmg_classification, v.chromosome, v.start"
        f" FROM {tenant_db}.germline__snv__occurrence o"
        f" JOIN {tenant_db}.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = {p(snv_key['variant_part'])}"
        f" LEFT JOIN {_interpretations(p, tenant_db, snv_key['seq_id'])} ig ON ig.locus_id = CAST(o.locus_id AS VARCHAR)"
        f" WHERE o.seq_id = {p(snv_key['seq_id'])} AND o.task_id = {p(snv_key['task_id'])} AND o.part = {p(snv_key['part'])}"
        f"  AND o.zygosity IN ({p.list(_rules.ALT_ZYGOSITIES)})"
        f"  AND (v.gnomad_v3_af IS NULL OR v.gnomad_v3_af < {p(_rules.RARE_MAX_AF)})"
        f"  AND (v.vep_impact IN ({p.list(_rules.QUALIFYING_IMPACTS)}) OR {_plp_condition(p)})"
        "), cnv AS ("
        " SELECT c.cnv_id, c.chromosome, c.start, c.end, c.type, c.cn, c.length, g.symbol"
        f" FROM {tenant_db}.germline__cnv__occurrence c, unnest(c.symbol) AS g(symbol)"
        f" WHERE c.seq_id = {p(cnv_key['seq_id'])} AND c.task_id = {p(cnv_key['task_id'])} AND c.part = {p(cnv_key['part'])}"
        f"  AND c.filter = {p(_rules.PASS)} AND c.type IN ({p.list(_rules.GERMLINE_SECOND_HIT_CNV_TYPES)})"
        ")"
        " SELECT snv.symbol AS gene, snv.locus_id, snv.hgvsg, snv.start AS snv_start, snv.aa_change, snv.zygosity,"
        "  snv.transmission_mode,"
        "  snv.vep_impact, snv.gnomad_v3_af, snv.clinvar_interpretation, snv.exomiser_acmg_classification,"
        "  cnv.cnv_id, cnv.type AS cnv_type, cnv.chromosome AS cnv_chromosome, cnv.start AS cnv_start,"
        "  cnv.end AS cnv_end, cnv.length AS cnv_length, cnv.cn AS cnv_cn,"
        "  (snv.start BETWEEN cnv.start AND cnv.end) AS snv_inside_cnv"
        # A HET SNV anywhere in the gene, or a HOM/HEM SNV inside the deletion: the
        # remaining allele of a hemizygous region is called homozygous.
        " FROM snv JOIN cnv ON cnv.symbol = snv.symbol AND cnv.chromosome = snv.chromosome"
        f"  AND (snv.zygosity = {p(_rules.HET)} OR snv.start BETWEEN cnv.start AND cnv.end)"
        f" ORDER BY gene, snv.locus_id LIMIT {p(ROW_LIMIT + 1)}"
    )
    return sql, p


def family_deletions_query(tenant_db: str, cnv_key: dict, genes: list[str]) -> tuple[str, _sql.Params]:
    """PASS deletions over ``genes`` in another member's CNV analysis."""
    p = _sql.Params()
    sql = (
        "SELECT g.symbol AS gene, c.seq_id, c.cnv_id, c.chromosome, c.start, c.end, c.cn"
        f" FROM {tenant_db}.germline__cnv__occurrence c, unnest(c.symbol) AS g(symbol)"
        f" WHERE c.seq_id = {p(cnv_key['seq_id'])} AND c.task_id = {p(cnv_key['task_id'])} AND c.part = {p(cnv_key['part'])}"
        f"  AND c.filter = {p(_rules.PASS)} AND c.type IN ({p.list(_rules.GERMLINE_SECOND_HIT_CNV_TYPES)})"
        f"  AND g.symbol IN ({p.list(genes)})"
        f" ORDER BY gene LIMIT {p(ROW_LIMIT + 1)}"
    )
    return sql, p


def comphet_query(tenant_db: str, snv_key: dict) -> tuple[str, _sql.Params]:
    """The compound heterozygous pairs of a sequencing, once each.

    Slivar tags both variants of a pair, so the table holds each pair once per
    direction: the pair is read as (smaller locus, larger locus) per gene.
    """
    p = _sql.Params()
    sql = (
        "WITH pairs AS ("
        " SELECT DISTINCT c.symbol, LEAST(c.locus_id, c.partner_locus_id) AS locus_id,"
        "  GREATEST(c.locus_id, c.partner_locus_id) AS partner_locus_id"
        f" FROM {tenant_db}.germline__snv__comphet c"
        f" WHERE c.seq_id = {p(snv_key['seq_id'])} AND c.task_id = {p(snv_key['task_id'])} AND c.part = {p(snv_key['part'])}"
        ")"
        " SELECT pairs.symbol AS gene, pairs.locus_id, v1.hgvsg, v1.vep_impact, v1.gnomad_v3_af,"
        " pairs.partner_locus_id, v2.hgvsg AS partner_hgvsg, v2.vep_impact AS partner_vep_impact,"
        " v2.gnomad_v3_af AS partner_gnomad_v3_af"
        " FROM pairs"
        f" JOIN {tenant_db}.snv__variant_partitioned v1 ON v1.locus_id = pairs.locus_id AND v1.part = {p(snv_key['variant_part'])}"
        f" JOIN {tenant_db}.snv__variant_partitioned v2 ON v2.locus_id = pairs.partner_locus_id AND v2.part = {p(snv_key['variant_part'])}"
        f" ORDER BY gene, pairs.locus_id LIMIT {p(ROW_LIMIT + 1)}"
    )
    return sql, p


def germline_keys_query(shared_db: str, tenant: str, patient_id: int) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        "SELECT DISTINCT case_id, seq_id, task_id, part"
        f" FROM {shared_db}.staging_sequencing_experiment"
        f" WHERE tenant_code = {p(tenant)} AND patient_id = {p(patient_id)}"
        f"  AND task_type = {p(_rules.GERMLINE_SNV_TASK_TYPE)} AND deleted = false AND part IS NOT NULL"
    )
    return sql, p


def germline_plp_query(tenant_db: str, key: dict) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        "SELECT o.locus_id, v.symbol AS gene, v.hgvsg, v.aa_change, o.zygosity, v.clinvar_interpretation,"
        " o.exomiser_acmg_classification, (ig.locus_id IS NOT NULL) AS interpreted_plp"
        f" FROM {tenant_db}.germline__snv__occurrence o"
        f" JOIN {tenant_db}.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = {p(key['variant_part'])}"
        f" LEFT JOIN {_interpretations(p, tenant_db, key['seq_id'])} ig ON ig.locus_id = CAST(o.locus_id AS VARCHAR)"
        f" WHERE o.seq_id = {p(key['seq_id'])} AND o.task_id = {p(key['task_id'])} AND o.part = {p(key['part'])}"
        f"  AND {_plp_condition(p)}"
        f" ORDER BY gene LIMIT {p(ROW_LIMIT + 1)}"
    )
    return sql, p


def somatic_snv_query(tenant_db: str, key: dict, genes: list[str]) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        "SELECT v.symbol AS gene, o.locus_id, v.hgvsg, v.aa_change, v.vep_impact, o.tumor_ad_ratio"
        f" FROM {tenant_db}.somatic__snv__occurrence o"
        f" JOIN {tenant_db}.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = {p(key['variant_part'])}"
        f" WHERE o.tumor_seq_id = {p(key['seq_id'])} AND o.task_id = {p(key['task_id'])} AND o.part = {p(key['part'])}"
        f"  AND o.filter = {p(_rules.PASS)} AND v.vep_impact IN ({p.list(_rules.QUALIFYING_IMPACTS)})"
        f"  AND v.symbol IN ({p.list(genes)})"
        f" ORDER BY gene LIMIT {p(ROW_LIMIT + 1)}"
    )
    return sql, p


def somatic_cnv_query(tenant_db: str, key: dict, genes: list[str]) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        "SELECT g.symbol AS gene, c.cnv_id, c.type AS cnv_type, c.chromosome, c.start, c.end, c.cn"
        f" FROM {tenant_db}.somatic__cnv__occurrence c, unnest(c.symbol) AS g(symbol)"
        f" WHERE c.seq_id = {p(key['seq_id'])} AND c.task_id = {p(key['task_id'])} AND c.part = {p(key['part'])}"
        f"  AND c.filter = {p(_rules.PASS)} AND c.type IN ({p.list(_rules.SOMATIC_SECOND_HIT_CNV_TYPES)})"
        f"  AND g.symbol IN ({p.list(genes)})"
        f" ORDER BY gene LIMIT {p(ROW_LIMIT + 1)}"
    )
    return sql, p

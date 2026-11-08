"""SQL of the gene-level carrier tools (``cohort_carriers``, ``genotype_phenotype``,
``lollipop``, the carrier groups of ``survival_curve``).

The gene restricts the variant catalog (or the CNV gene arrays) first, then
the occurrences of those loci are mapped to cases and patients through
``staging_sequencing_experiment``.
"""

from .. import sql as _sql
from ..domain import rules as _rules
from ..domain.criteria import Criteria
from ..enums import ENUMS
from .sequencing import latest_task_join

_SUMMARY_LIMIT = 30


def carriers_cte(p: _sql.Params, tenant: str, criteria: Criteria) -> str:
    """``WITH ... car AS (...)``: one row per (case, sequencing, variant) of a carrier,
    from the latest task of the sequencing in that case (``latest_tasks``), with its
    patient. SNV CTEs also define ``v``.

    An SNV is in the gene when one of its consequences is on a transcript of the
    gene (``snv__consequence_filter``), not only its picked one, so a variant of two
    overlapping genes counts for both; ``vep_impact`` is its impact on that gene.
    """
    tenant_db, shared_db = _sql.tenant_db(tenant), _sql.shared_db()
    sequencing = latest_task_join(p, tenant, criteria.data_type)
    carrier_columns = (
        "s.case_id, s.patient_id, s.family_role, s.affected_status, s.experimental_strategy,"
        " x.seq_id, x.task_id, x.variant_id"
    )
    if criteria.is_snv:
        gene_where = [f"cf.symbol = {p(criteria.gene)}"]
        if criteria.vep_impact:
            gene_where.append(f"cf.vep_impact IN ({p.list(criteria.vep_impact)})")
        where = []
        if criteria.clinvar:
            where.append(f"arrays_overlap(sv.clinvar_interpretation, [{p.list(criteria.clinvar)}])")
        if criteria.max_gnomad_af is not None:
            where.append(f"(sv.gnomad_v3_af IS NULL OR sv.gnomad_v3_af < {p(criteria.max_gnomad_af)})")
        variant_filter = (" WHERE " + " AND ".join(where)) if where else ""
        if criteria.is_somatic:
            occurrence, seq_column, zygosity_column = "somatic__snv__occurrence", "tumor_seq_id", "tumor_zygosity"
        else:
            occurrence, seq_column, zygosity_column = "germline__snv__occurrence", "seq_id", "zygosity"
        occ_where = []
        if criteria.zygosity:
            occ_where.append(f"o.{zygosity_column} IN ({p.list(criteria.zygosity)})")
        if criteria.is_somatic:
            occ_where.append(f"o.filter = {p(_rules.PASS)}")
        occ_filter = (" WHERE " + " AND ".join(occ_where)) if occ_where else ""
        return (
            "WITH g AS ("
            f" SELECT DISTINCT cf.locus_id FROM {shared_db}.snv__consequence_filter cf WHERE {' AND '.join(gene_where)}"
            "), v AS ("
            " SELECT sv.locus_id, sv.hgvsg, sv.vep_impact, sv.variant_class, sv.consequences"
            f" FROM {tenant_db}.snv__variant sv JOIN g ON g.locus_id = sv.locus_id{variant_filter}"
            "), x AS ("
            f" SELECT o.{seq_column} AS seq_id, o.task_id, o.locus_id AS variant_id"
            f" FROM {tenant_db}.{occurrence} o JOIN v ON v.locus_id = o.locus_id{occ_filter}"
            f"), car AS (SELECT {carrier_columns} FROM x {sequencing})"
        )
    table = "somatic__cnv__occurrence" if criteria.is_somatic else "germline__cnv__occurrence"
    types = criteria.cnv_types or tuple(ENUMS["cnv_type"])
    return (
        "WITH x AS ("
        " SELECT c.seq_id, c.task_id, c.cnv_id AS variant_id, c.type"
        f" FROM {tenant_db}.{table} c, unnest(c.symbol) AS g(symbol)"
        f" WHERE g.symbol = {p(criteria.gene)} AND c.filter = {p(_rules.PASS)} AND c.type IN ({p.list(types)})"
        f"), car AS (SELECT {carrier_columns}, x.type FROM x {sequencing})"
    )


def totals_query(tenant: str, criteria: Criteria) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        carriers_cte(p, tenant, criteria)
        + " SELECT COUNT(DISTINCT case_id) AS cases, COUNT(DISTINCT patient_id) AS patients,"
        " COUNT(DISTINCT variant_id) AS variants FROM car"
    )
    return sql, p


def carriers_query(tenant: str, criteria: Criteria, limit: int) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    label = "v.hgvsg" if criteria.is_snv else "car.type"
    join = " JOIN v ON v.locus_id = car.variant_id" if criteria.is_snv else ""
    sql = (
        carriers_cte(p, tenant, criteria)
        + " SELECT car.case_id, car.patient_id, car.family_role, car.affected_status, car.experimental_strategy,"
        " car.seq_id, car.task_id, COUNT(DISTINCT car.variant_id) AS variant_count,"
        f" array_agg(DISTINCT {label}) AS variants, MIN(car.variant_id) AS first_variant_id"
        f" FROM car{join}"
        " GROUP BY car.case_id, car.patient_id, car.family_role, car.affected_status, car.experimental_strategy,"
        " car.seq_id, car.task_id"
        f" ORDER BY variant_count DESC, car.case_id LIMIT {p(limit)}"
    )
    return sql, p


def summary_queries(tenant: str, criteria: Criteria) -> dict[str, tuple[str, _sql.Params]]:
    out = {}
    if criteria.is_snv:
        p = _sql.Params()
        out["by_consequence"] = (
            carriers_cte(p, tenant, criteria)
            + " SELECT u.consequence, COUNT(DISTINCT car.variant_id) AS variants, COUNT(DISTINCT car.patient_id) AS patients"
            " FROM car JOIN v ON v.locus_id = car.variant_id, unnest(v.consequences) AS u(consequence)"
            f" GROUP BY u.consequence ORDER BY patients DESC LIMIT {p(_SUMMARY_LIMIT)}",
            p,
        )
        p = _sql.Params()
        out["by_variant_class"] = (
            carriers_cte(p, tenant, criteria)
            + " SELECT v.variant_class, COUNT(DISTINCT car.variant_id) AS variants, COUNT(DISTINCT car.patient_id) AS patients"
            " FROM car JOIN v ON v.locus_id = car.variant_id"
            f" GROUP BY v.variant_class ORDER BY patients DESC LIMIT {p(_SUMMARY_LIMIT)}",
            p,
        )
    else:
        p = _sql.Params()
        out["by_cnv_type"] = (
            carriers_cte(p, tenant, criteria)
            + " SELECT car.type AS cnv_type, COUNT(DISTINCT car.variant_id) AS cnvs, COUNT(DISTINCT car.patient_id) AS patients"
            f" FROM car GROUP BY car.type ORDER BY patients DESC LIMIT {p(_SUMMARY_LIMIT)}",
            p,
        )
    return out


def phenotypes_query(tenant: str, criteria: Criteria, limit: int) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    tenant_db, shared_db = _sql.tenant_db(tenant), _sql.shared_db()
    sql = (
        carriers_cte(p, tenant, criteria)
        + ", pat AS (SELECT DISTINCT patient_id FROM car)"
        " SELECT obs.code_value AS hpo_term_id, MAX(h.name) AS hpo_term_name,"
        " COUNT(DISTINCT obs.patient_id) AS patients"
        f" FROM {tenant_db}.obs_categorical obs JOIN pat ON pat.patient_id = obs.patient_id"
        f" LEFT JOIN {shared_db}.hpo_term h ON h.id = obs.code_value"
        f" WHERE obs.observation_code = {p(_rules.PHENOTYPE_OBSERVATION_CODE)}"
        f"  AND obs.interpretation_code = {p(_rules.POSITIVE_INTERPRETATION_CODE)} AND obs.fetus_id IS NULL"
        f" GROUP BY obs.code_value ORDER BY patients DESC, hpo_term_id LIMIT {p(limit)}"
    )
    return sql, p


def phenotyped_patients_query(tenant: str, criteria: Criteria) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    tenant_db = _sql.tenant_db(tenant)
    sql = (
        carriers_cte(p, tenant, criteria)
        + " SELECT COUNT(DISTINCT obs.patient_id) AS patients"
        f" FROM {tenant_db}.obs_categorical obs JOIN (SELECT DISTINCT patient_id FROM car) pat ON pat.patient_id = obs.patient_id"
        f" WHERE obs.observation_code = {p(_rules.PHENOTYPE_OBSERVATION_CODE)}"
        f"  AND obs.interpretation_code = {p(_rules.POSITIVE_INTERPRETATION_CODE)} AND obs.fetus_id IS NULL"
    )
    return sql, p


def carriers_with_term_query(tenant: str, criteria: Criteria, hpo_term: str, limit: int) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    tenant_db = _sql.tenant_db(tenant)
    sql = (
        carriers_cte(p, tenant, criteria)
        + " SELECT DISTINCT car.case_id, car.patient_id, car.affected_status"
        f" FROM car JOIN {tenant_db}.obs_categorical obs ON obs.patient_id = car.patient_id"
        f" WHERE obs.observation_code = {p(_rules.PHENOTYPE_OBSERVATION_CODE)}"
        f"  AND obs.interpretation_code = {p(_rules.POSITIVE_INTERPRETATION_CODE)} AND obs.fetus_id IS NULL"
        f"  AND obs.code_value = {p(hpo_term)}"
        f" ORDER BY car.case_id LIMIT {p(limit)}"
    )
    return sql, p


def carrier_patients_query(tenant: str, criteria_: Criteria) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    return carriers_cte(p, tenant, criteria_) + " SELECT DISTINCT patient_id FROM car", p


def lollipop_query(tenant: str, criteria_: Criteria) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    cte = carriers_cte(p, tenant, criteria_)
    tenant_db = _sql.tenant_db(tenant)
    return (
        cte + ", va AS (SELECT locus_id, transcript_id, aa_change, array_join(consequences, ',') AS kind"
        f" FROM {tenant_db}.snv__variant WHERE symbol = {p(criteria_.gene)})"
        " SELECT va.locus_id, va.transcript_id, va.aa_change, va.kind, COUNT(DISTINCT car.patient_id) AS carriers"
        " FROM car JOIN va ON va.locus_id = car.variant_id WHERE va.aa_change IS NOT NULL"
        f" GROUP BY va.locus_id, va.transcript_id, va.aa_change, va.kind ORDER BY carriers DESC LIMIT {p(5000)}"
    ), p

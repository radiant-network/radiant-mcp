"""Cohort tools: the carriers of a gene, and their phenotypes.

The portal API lists the cases of one variant, not of a gene, so these tools
read StarRocks: the gene restricts the variant catalog (or the CNV gene
arrays) first, then the occurrences of those loci are mapped to cases and
patients through ``staging_sequencing_experiment``. Everything runs under the
caller's JWT, so a user only counts the cases they may see.
"""

from dataclasses import dataclass, replace
from typing import Any, Optional

from ..enums import ENUMS, canonical_values
from ..portal_links import case_url, occurrence_url, with_url
from . import _api, _rules, _sql
from ._api import RadiantApi, clamp, resolve_tenant
from .genes import resolve_gene

DATA_TYPES = ("germline_snv", "germline_cnv", "somatic_snv", "somatic_cnv")
CARRIERS_MAX_LIMIT = 200
_SUMMARY_LIMIT = 30
PHENOTYPES_MAX_LIMIT = 100


@dataclass(frozen=True)
class Criteria:
    """Validated filters of a carrier query."""

    data_type: str
    gene: str
    vep_impact: tuple[str, ...] = ()
    clinvar: tuple[str, ...] = ()
    max_gnomad_af: Optional[float] = None
    zygosity: tuple[str, ...] = ()
    cnv_types: tuple[str, ...] = ()

    @property
    def is_snv(self) -> bool:
        return self.data_type.endswith("_snv")

    @property
    def is_somatic(self) -> bool:
        return self.data_type.startswith("somatic_")

    def describe(self) -> dict:
        out: dict[str, Any] = {"data_type": self.data_type, "gene": self.gene}
        for name in ("vep_impact", "clinvar", "zygosity", "cnv_types"):
            if getattr(self, name):
                out[name] = list(getattr(self, name))
        if self.max_gnomad_af is not None:
            out["max_gnomad_af"] = f"< {self.max_gnomad_af:g} or absent from gnomAD"
        if not self.is_snv or self.is_somatic:
            out["filter"] = _rules.PASS
        return out


def build_criteria(
    data_type: str,
    gene: str,
    vep_impact: Optional[list[str]],
    clinvar: Optional[list[str]],
    max_gnomad_af: Optional[float],
    zygosity: Optional[list[str]],
    cnv_types: Optional[list[str]],
) -> Criteria:
    _sql.one_of(data_type, DATA_TYPES, "data_type")
    _sql.symbol(gene)
    snv_only = {"vep_impact": vep_impact, "clinvar": clinvar, "max_gnomad_af": max_gnomad_af, "zygosity": zygosity}
    if not data_type.endswith("_snv"):
        used = [name for name, value in snv_only.items() if value not in (None, [], ())]
        if used:
            raise _sql.InvalidInput(f"{', '.join(used)} only apply to SNV data types")
    elif cnv_types:
        raise _sql.InvalidInput("cnv_types only applies to CNV data types")

    af = None
    if max_gnomad_af is not None:
        af = _sql.number(max_gnomad_af)
        if not 0 < af <= 1:
            raise _sql.InvalidInput("max_gnomad_af must be in (0, 1]")
    return Criteria(
        data_type=data_type,
        gene=gene,
        vep_impact=tuple(_sql.all_of(canonical_values("vep_impact", vep_impact or []), ENUMS["vep_impact"], "vep_impact")),
        clinvar=tuple(_sql.all_of(canonical_values("clinvar", clinvar or []), ENUMS["clinvar_interpretation"], "clinvar")),
        max_gnomad_af=af,
        zygosity=tuple(_sql.all_of(canonical_values("zygosity", zygosity or []), ENUMS["zygosity"], "zygosity")),
        cnv_types=tuple(_sql.all_of(canonical_values("type", cnv_types or []), ENUMS["cnv_type"], "cnv_types")),
    )


def carriers_cte(p: _sql.Params, tenant: str, criteria: Criteria) -> str:
    """``WITH ... car AS (...)``: one row per (sequencing, task, variant) of a
    carrier, with its case and patient. SNV CTEs also define ``v``."""
    tenant_db, shared_db = _sql.tenant_db(tenant), _sql.shared_db()
    staging = (
        f"JOIN {shared_db}.staging_sequencing_experiment s ON s.seq_id = x.seq_id AND s.task_id = x.task_id"
        f" AND s.tenant_code = {p(tenant)} AND s.deleted = false"
    )
    carrier_columns = (
        "s.case_id, s.patient_id, s.family_role, s.affected_status, s.experimental_strategy,"
        " x.seq_id, x.task_id, x.variant_id"
    )
    if criteria.is_snv:
        where = [f"symbol = {p(criteria.gene)}"]
        if criteria.vep_impact:
            where.append(f"vep_impact IN ({p.list(criteria.vep_impact)})")
        if criteria.clinvar:
            where.append(f"arrays_overlap(clinvar_interpretation, [{p.list(criteria.clinvar)}])")
        if criteria.max_gnomad_af is not None:
            where.append(f"(gnomad_v3_af IS NULL OR gnomad_v3_af < {p(criteria.max_gnomad_af)})")
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
            "WITH v AS ("
            " SELECT locus_id, hgvsg, vep_impact, variant_class, consequences"
            f" FROM {tenant_db}.snv__variant_partitioned WHERE {' AND '.join(where)}"
            "), x AS ("
            f" SELECT o.{seq_column} AS seq_id, o.task_id, o.locus_id AS variant_id"
            f" FROM {tenant_db}.{occurrence} o JOIN v ON v.locus_id = o.locus_id{occ_filter}"
            f"), car AS (SELECT {carrier_columns} FROM x {staging})"
        )
    table = "somatic__cnv__occurrence" if criteria.is_somatic else "germline__cnv__occurrence"
    types = criteria.cnv_types or tuple(ENUMS["cnv_type"])
    return (
        "WITH x AS ("
        " SELECT c.seq_id, c.task_id, c.cnv_id AS variant_id, c.type"
        f" FROM {tenant_db}.{table} c, unnest(c.symbol) AS g(symbol)"
        f" WHERE g.symbol = {p(criteria.gene)} AND c.filter = {p(_rules.PASS)} AND c.type IN ({p.list(types)})"
        f"), car AS (SELECT {carrier_columns}, x.type FROM x {staging})"
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
        f"  AND obs.interpretation_code = {p(_rules.POSITIVE_INTERPRETATION_CODE)}"
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
        f"  AND obs.interpretation_code = {p(_rules.POSITIVE_INTERPRETATION_CODE)}"
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
        f"  AND obs.interpretation_code = {p(_rules.POSITIVE_INTERPRETATION_CODE)}"
        f"  AND obs.code_value = {p(hpo_term)}"
        f" ORDER BY car.case_id LIMIT {p(limit)}"
    )
    return sql, p


def _resolve(api: RadiantApi, tenant: Optional[str], criteria: Criteria) -> tuple[Optional[str], Optional[Criteria], Optional[dict]]:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return None, None, err
    gene = resolve_gene(api, tenant_code, criteria.gene)
    if gene is None:
        return None, None, {"error": f"Gene {criteria.gene!r} not found.", "status": 404}
    return tenant_code, replace(criteria, gene=_sql.symbol(gene["symbol"])), None


def _link(tenant: str, criteria: Criteria, row: dict) -> Optional[str]:
    if criteria.data_type == "germline_snv":
        return occurrence_url(tenant, row.get("case_id"), row.get("seq_id"), "germline_snv",
                              task_id=row.get("task_id"), variant_id=str(row.get("first_variant_id")))
    if criteria.data_type == "germline_cnv":
        return occurrence_url(tenant, row.get("case_id"), row.get("seq_id"), "germline_cnv", task_id=row.get("task_id"))
    # Somatic: tumor-normal or tumor-only is not known here; open the variants tab.
    return case_url(tenant, row.get("case_id"), tab="variants")


def _cohort_carriers(api: RadiantApi, token: str, criteria: Criteria, tenant: Optional[str], limit: int) -> dict:
    tenant_code, criteria, err = _resolve(api, tenant, criteria)
    if err:
        return err
    totals = _sql.first(_sql.run(token, *totals_query(tenant_code, criteria))) or {}
    carriers = []
    for row in _sql.run(token, *carriers_query(tenant_code, criteria, limit)):
        row["variants"] = _sql.as_list(row.get("variants"))
        with_url(row, _link(tenant_code, criteria, row))
        row.pop("first_variant_id", None)
        carriers.append(row)
    summary = {name: _sql.run(token, sql, params) for name, (sql, params) in summary_queries(tenant_code, criteria).items()}
    return {
        "tenant": tenant_code,
        "criteria": criteria.describe(),
        "cases": totals.get("cases", 0),
        "patients": totals.get("patients", 0),
        "variants": totals.get("variants", 0),
        "returned": len(carriers),
        "carriers": carriers,
        "summary": summary,
    }


def _genotype_phenotype(
    api: RadiantApi, token: str, criteria: Criteria, hpo_term: Optional[str], tenant: Optional[str], limit: int,
) -> dict:
    tenant_code, criteria, err = _resolve(api, tenant, criteria)
    if err:
        return err
    totals = _sql.first(_sql.run(token, *totals_query(tenant_code, criteria))) or {}
    phenotyped = _sql.first(_sql.run(token, *phenotyped_patients_query(tenant_code, criteria))) or {}
    result: dict[str, Any] = {
        "tenant": tenant_code,
        "criteria": criteria.describe(),
        "carrier_patients": totals.get("patients", 0),
        "carrier_patients_with_phenotypes": phenotyped.get("patients", 0),
        "phenotypes": _sql.run(token, *phenotypes_query(tenant_code, criteria, limit)),
    }
    if hpo_term:
        rows = _sql.run(token, *carriers_with_term_query(tenant_code, criteria, hpo_term, CARRIERS_MAX_LIMIT))
        result["hpo_term"] = hpo_term
        result["carriers_with_term"] = [with_url(r, case_url(tenant_code, r.get("case_id"))) for r in rows]
    return result


# -- tools -------------------------------------------------------------------

async def cohort_carriers(
    gene: str,
    data_type: str = "germline_snv",
    vep_impact: Optional[list[str]] = None,
    clinvar: Optional[list[str]] = None,
    max_gnomad_af: Optional[float] = None,
    zygosity: Optional[list[str]] = None,
    cnv_types: Optional[list[str]] = None,
    limit: int = 50,
    tenant: Optional[str] = None,
) -> dict:
    """The cases and patients of the tenant that carry a variant in a gene.

    Returns ``cases``, ``patients`` and ``variants`` (distinct counts over the
    whole tenant), ``carriers`` (one row per carrier sequencing: case, patient,
    family role, affected status, strategy, the matching variants and a
    ``portal_url``, most variants first), ``summary`` (counts by consequence
    and variant class for SNVs, by type for CNVs) and ``criteria`` (the filters
    actually applied: quote them). Use this before any SQL for "which cases
    carry a variant in GENE" questions.

    Args:
        gene: Gene symbol or alias (resolved by the portal gene search).
        data_type: germline_snv (default), germline_cnv, somatic_snv or somatic_cnv.
        vep_impact: SNV only, e.g. ["HIGH", "MODERATE"].
        clinvar: SNV only, e.g. ["Pathogenic", "Likely_pathogenic"] (any of them).
        max_gnomad_af: SNV only; keeps variants with gnomAD v3 AF below it OR
            absent from gnomAD (e.g. 0.01).
        zygosity: SNV only, e.g. ["HOM"] (tumor zygosity for somatic).
        cnv_types: CNV only, among LOSS, GAIN, CNLOH, GAINLOH (default all).
            CNVs are always PASS; somatic SNVs too.
        limit: Max carriers listed (1–200, default 50); counts cover all.
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        criteria = build_criteria(data_type, gene, vep_impact, clinvar, max_gnomad_af, zygosity, cnv_types)
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_cohort_carriers, criteria, tenant, clamp(limit, 1, CARRIERS_MAX_LIMIT))


async def genotype_phenotype(
    gene: str,
    hpo_term: Optional[str] = None,
    data_type: str = "germline_snv",
    vep_impact: Optional[list[str]] = None,
    clinvar: Optional[list[str]] = None,
    max_gnomad_af: Optional[float] = None,
    zygosity: Optional[list[str]] = None,
    cnv_types: Optional[list[str]] = None,
    limit: int = 30,
    tenant: Optional[str] = None,
) -> dict:
    """The observed HPO phenotypes of a gene's carriers.

    Carriers are those of ``cohort_carriers`` with the same filters. Returns
    ``carrier_patients``, ``carrier_patients_with_phenotypes`` (the
    denominator to use) and ``phenotypes`` (the HPO terms observed in carriers,
    with the number of carrier patients for each, most frequent first). With
    ``hpo_term``, also ``carriers_with_term``: the carriers that have it, with a
    ``portal_url``. Only observed (positive) phenotypes are counted.

    Args:
        gene: Gene symbol or alias.
        hpo_term: Optional HPO id (HP:0000000); find it with lookup_terms.
        data_type: germline_snv (default), germline_cnv, somatic_snv or somatic_cnv.
        vep_impact: SNV only, e.g. ["HIGH", "MODERATE"].
        clinvar: SNV only, e.g. ["Pathogenic", "Likely_pathogenic"] (any of them).
        max_gnomad_af: SNV only; keeps variants with gnomAD v3 AF below it OR
            absent from gnomAD (e.g. 0.01).
        zygosity: SNV only, e.g. ["HOM"] (tumor zygosity for somatic).
        cnv_types: CNV only, among LOSS, GAIN, CNLOH, GAINLOH (default all).
            CNVs are always PASS; somatic SNVs too.
        limit: Max phenotypes listed (1–100, default 30).
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        criteria = build_criteria(data_type, gene, vep_impact, clinvar, max_gnomad_af, zygosity, cnv_types)
        term = _sql.hpo_id(hpo_term) if hpo_term else None
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_genotype_phenotype, criteria, term, tenant, clamp(limit, 1, PHENOTYPES_MAX_LIMIT))

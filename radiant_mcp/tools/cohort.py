"""Cohort tools: the carriers of a gene, and their phenotypes.

The portal API lists the cases of one variant, not of a gene, so these tools
read StarRocks: the gene restricts the variant catalog (or the CNV gene
arrays) first, then the occurrences of those loci are mapped to cases and
patients through ``staging_sequencing_experiment``. Everything runs under the
caller's JWT, so a user only counts the cases they may see.
"""

from dataclasses import replace
from typing import Any

from fastmcp.tools import ToolResult

from .. import portal_api as _api
from .. import sql as _sql
from ..domain.criteria import Criteria, build_criteria
from ..domain.data_types import CohortDataType
from ..portal_api import RadiantApi, clamp, resolve_tenant
from ..portal_links import case_url, occurrence_url, with_url
from ..queries.carriers import (
    carriers_query,
    carriers_with_term_query,
    phenotyped_patients_query,
    phenotypes_query,
    summary_queries,
    totals_query,
)
from ..results import ToolOutput, Truncation, failure, not_found, ok, respond
from ..services.genes import not_found_message, resolve_gene

CARRIERS_MAX_LIMIT = 200
PHENOTYPES_MAX_LIMIT = 100


def _resolve(api: RadiantApi, tenant: str | None, criteria: Criteria) -> tuple[str, Criteria]:
    tenant_code = resolve_tenant(api, tenant)
    gene = resolve_gene(api, tenant_code, criteria.gene)
    if gene is None:
        raise not_found(not_found_message(criteria.gene))
    return tenant_code, replace(criteria, gene=_sql.symbol(gene["symbol"]))


def _link(tenant: str, criteria: Criteria, row: dict) -> str | None:
    if criteria.data_type == "germline_snv":
        return occurrence_url(tenant, row.get("case_id"), row.get("seq_id"), "germline_snv",
                              task_id=row.get("task_id"), variant_id=str(row.get("first_variant_id")))
    if criteria.data_type == "germline_cnv":
        return occurrence_url(tenant, row.get("case_id"), row.get("seq_id"), "germline_cnv", task_id=row.get("task_id"))
    # Somatic: tumor-normal or tumor-only is not known here; open the variants tab.
    return case_url(tenant, row.get("case_id"), tab="variants")


def _cohort_carriers(api: RadiantApi, token: str, criteria: Criteria, tenant: str | None, limit: int) -> ToolOutput:
    tenant_code, criteria = _resolve(api, tenant, criteria)
    totals = _sql.first(_sql.run(token, *totals_query(tenant_code, criteria))) or {}
    carriers = []
    for row in _sql.run(token, *carriers_query(tenant_code, criteria, limit)):
        row["variants"] = _sql.as_list(row.get("variants"))
        with_url(row, _link(tenant_code, criteria, row))
        row.pop("first_variant_id", None)
        carriers.append(row)
    summary = {name: _sql.run(token, sql, params) for name, (sql, params) in summary_queries(tenant_code, criteria).items()}
    data = {
        "tenant": tenant_code,
        "criteria": criteria.describe(),
        "cases": totals.get("cases", 0),
        "cases_any_member": totals.get("cases_any_member", 0),
        "patients": totals.get("patients", 0),
        "variants": totals.get("variants", 0),
        "returned": len(carriers),
        "carriers": carriers,
        "summary": summary,
    }
    truncated = None
    if len(carriers) >= limit:
        truncated = Truncation(reason=f"`carriers` stops at {limit} rows (most variants first): raise `limit` (up to "
                                      f"{CARRIERS_MAX_LIMIT}) or add filters; the counts cover every carrier.",
                               lists=["carriers"])
    return ok(data, truncated=truncated)


def _genotype_phenotype(
    api: RadiantApi, token: str, criteria: Criteria, hpo_term: str | None, tenant: str | None, limit: int,
) -> ToolOutput:
    tenant_code, criteria = _resolve(api, tenant, criteria)
    totals = _sql.first(_sql.run(token, *totals_query(tenant_code, criteria))) or {}
    phenotyped = _sql.first(_sql.run(token, *phenotyped_patients_query(tenant_code, criteria))) or {}
    result: dict[str, Any] = {
        "tenant": tenant_code,
        "criteria": criteria.describe(),
        "carrier_patients": totals.get("patients", 0),
        "carrier_patients_with_phenotypes": phenotyped.get("patients", 0),
        "phenotypes": _sql.run(token, *phenotypes_query(tenant_code, criteria, limit)),
        "phenotype_source": ("positive HPO observations of the carrier patients; observations recorded for a "
                             "fetus are left out, since a fetal sample carries its mother's patient_id"),
    }
    cut: list[str] = []
    if hpo_term:
        rows = _sql.run(token, *carriers_with_term_query(tenant_code, criteria, hpo_term, CARRIERS_MAX_LIMIT))
        result["hpo_term"] = hpo_term
        result["carriers_with_term"] = [with_url(r, case_url(tenant_code, r.get("case_id"))) for r in rows]
        if len(rows) >= CARRIERS_MAX_LIMIT:
            cut.append("carriers_with_term")
    if len(result["phenotypes"]) >= limit:
        cut.append("phenotypes")
    truncated = None
    if cut:
        truncated = Truncation(reason=f"`phenotypes` stops at {limit} terms (raise `limit`, up to {PHENOTYPES_MAX_LIMIT}) "
                                      f"and `carriers_with_term` at {CARRIERS_MAX_LIMIT} carriers.", lists=cut)
    return ok(result, truncated=truncated)


# -- tools -------------------------------------------------------------------

async def cohort_carriers(
    gene: str,
    data_type: CohortDataType = "germline_snv",
    vep_impact: list[str] | None = None,
    clinvar: list[str] | None = None,
    max_gnomad_af: float | None = None,
    zygosity: list[str] | None = None,
    cnv_types: list[str] | None = None,
    limit: int = 50,
    tenant: str | None = None,
) -> ToolResult:
    """The cases and patients of the tenant that carry a variant in a gene.

    Returns distinct counts over the whole tenant: ``cases`` (cases whose
    proband carries), ``cases_any_member`` (cases where any sequenced member
    carries, the proband or only a relative), ``patients`` (every carrier,
    relatives included) and ``variants``; ``carriers`` (one row per carrier
    sequencing: case, patient, family role, affected status, strategy, the
    matching variants and a ``portal_url``, most variants first); ``summary``
    (counts by consequence and variant class for SNVs, by type for CNVs, by
    family role for germline); and ``criteria`` (the filters actually applied:
    quote them). Use this before any SQL for "which cases
    carry a variant in GENE" questions.

    Args:
        gene: Official HGNC symbol (case-insensitive, e.g. ERBB2) or Ensembl gene
            id; aliases (HER2, P53) are not resolved.
        data_type: germline_snv (default), germline_cnv, somatic_snv_tn or somatic_cnv.
            somatic_snv_tn reads the tumor-normal analyses only (tumor-only calls are mostly
            germline variants); somatic_cnv comes from the tumor-only CNV calling.
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
        return respond(failure(exc))
    return await _api.run(_cohort_carriers, criteria, tenant, clamp(limit, 1, CARRIERS_MAX_LIMIT))


async def genotype_phenotype(
    gene: str,
    hpo_term: str | None = None,
    data_type: CohortDataType = "germline_snv",
    vep_impact: list[str] | None = None,
    clinvar: list[str] | None = None,
    max_gnomad_af: float | None = None,
    zygosity: list[str] | None = None,
    cnv_types: list[str] | None = None,
    limit: int = 30,
    tenant: str | None = None,
) -> ToolResult:
    """The observed HPO phenotypes of a gene's carriers.

    Carriers are those of ``cohort_carriers`` with the same filters. Returns
    ``carrier_patients``, ``carrier_patients_with_phenotypes`` (the
    denominator to use) and ``phenotypes`` (the HPO terms observed in carriers,
    with the number of carrier patients for each, most frequent first). With
    ``hpo_term``, also ``carriers_with_term``: the carriers that have it, with a
    ``portal_url``. Only observed (positive) phenotypes are counted.

    Args:
        gene: Official HGNC symbol (case-insensitive) or Ensembl gene id; aliases
            are not resolved.
        hpo_term: Optional HPO id (HP:0000000); find it with lookup_terms.
        data_type: germline_snv (default), germline_cnv, somatic_snv_tn or somatic_cnv.
            somatic_snv_tn reads the tumor-normal analyses only (tumor-only calls are mostly
            germline variants); somatic_cnv comes from the tumor-only CNV calling.
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
        return respond(failure(exc))
    return await _api.run(_genotype_phenotype, criteria, term, tenant, clamp(limit, 1, PHENOTYPES_MAX_LIMIT))

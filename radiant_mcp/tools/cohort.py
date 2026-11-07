"""Cohort tools: the carriers of a gene, and their phenotypes.

The portal API lists the cases of one variant, not of a gene, so these tools
read StarRocks: the gene restricts the variant catalog (or the CNV gene
arrays) first, then the occurrences of those loci are mapped to cases and
patients through ``staging_sequencing_experiment``. Everything runs under the
caller's JWT, so a user only counts the cases they may see.
"""

from dataclasses import replace
from typing import Any, Optional

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
from ..services.genes import resolve_gene

CARRIERS_MAX_LIMIT = 200
PHENOTYPES_MAX_LIMIT = 100


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
        "phenotype_source": ("positive HPO observations of the carrier patients; observations recorded for a "
                             "fetus are left out, since a fetal sample carries its mother's patient_id"),
    }
    if hpo_term:
        rows = _sql.run(token, *carriers_with_term_query(tenant_code, criteria, hpo_term, CARRIERS_MAX_LIMIT))
        result["hpo_term"] = hpo_term
        result["carriers_with_term"] = [with_url(r, case_url(tenant_code, r.get("case_id"))) for r in rows]
    return result


# -- tools -------------------------------------------------------------------

async def cohort_carriers(
    gene: str,
    data_type: CohortDataType = "germline_snv",
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
        data_type: germline_snv (default), germline_cnv, somatic_snv_all or somatic_cnv.
            somatic_snv_all covers tumor-normal and tumor-only analyses together.
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
    data_type: CohortDataType = "germline_snv",
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
        data_type: germline_snv (default), germline_cnv, somatic_snv_all or somatic_cnv.
            somatic_snv_all covers tumor-normal and tumor-only analyses together.
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

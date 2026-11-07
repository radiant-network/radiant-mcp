"""``oncoprint``: which cases carry alterations in a gene list."""

import json
from typing import Any, Optional

from ... import charts
from ... import portal_api as _api
from ... import sql as _sql
from ...domain.consequences import consequence_class
from ...domain.data_types import COHORT_DATA_TYPES, CohortDataType
from ...enums import ENUMS, canonical_values
from ...portal_api import RadiantApi, resolve_tenant
from ...queries.charts import oncoprint_query
from ...services.genes import resolve_gene
from ._common import image as _image


MAX_ONCOPRINT_GENES = 20


MAX_ONCOPRINT_CASES = 100


def _oncoprint(api: RadiantApi, token: str, genes: list[str], data_types: list[str], tenant: Optional[str],
               max_gnomad_af: Optional[float], vep_impact: list[str]) -> Any:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err
    resolved, unknown = [], []
    for gene in genes:
        found = resolve_gene(api, tenant_code, gene)
        if found:
            resolved.append(_sql.symbol(found["symbol"]))
        else:
            unknown.append(gene)
    if not resolved:
        return {"error": f"No known gene among {genes}.", "status": 404}
    cells: dict[tuple[str, str], set[str]] = {}
    for data_type in data_types:
        for row in _sql.run(token, *oncoprint_query(tenant_code, data_type, resolved, max_gnomad_af, vep_impact)):
            kind = row["kind"] if data_type.endswith("_cnv") else consequence_class(row["kind"].split(","))
            cells.setdefault((row["gene"], f"case {row['case_id']}"), set()).add(kind)
    altered: dict[str, int] = {}
    for (_, case) in cells:
        altered[case] = altered.get(case, 0) + 1
    cases = sorted(altered, key=lambda c: (-altered[c], int(c.split()[1])))
    shown = cases[:MAX_ONCOPRINT_CASES]
    flat = {key: (next(iter(kinds)) if len(kinds) == 1 else "multiple") for key, kinds in cells.items() if key[1] in shown}
    if not flat:
        return {"tenant": tenant_code, "genes": resolved, "message": "No carrier of these genes with these filters."}
    title = f"Alterations in {', '.join(resolved)} ({', '.join(data_types)})"
    png = charts.oncoprint(resolved, shown, flat, title)
    per_gene = {g: sum(1 for (gene, _) in flat if gene == g) for g in resolved}
    text = json.dumps({
        "tenant": tenant_code, "genes": resolved, "unknown_genes": unknown, "data_types": data_types,
        "altered_cases": len(cases), "cases_shown": len(shown), "altered_cases_per_gene": per_gene,
        "filters": {"max_gnomad_af": max_gnomad_af, "vep_impact": vep_impact or None},
        "classes": "truncating > splice > missense > inframe > other for SNVs; CNV type for CNVs; "
                   "'multiple' when a case has several classes in a gene",
    })
    return _image(text, png)


async def oncoprint(
    genes: list[str],
    data_types: Optional[list[CohortDataType]] = None,
    max_gnomad_af: Optional[float] = None,
    vep_impact: Optional[list[str]] = None,
    tenant: Optional[str] = None,
) -> Any:
    """Oncoprint: which cases of the tenant carry an alteration in each gene, by type.

    Rows are genes, columns are cases (most altered first, at most 100);
    cells are coloured by alteration class: truncating, splice, missense,
    inframe, other for SNVs; LOSS, GAIN, CNLOH, GAINLOH for CNVs; 'multiple'
    when a case has several classes in a gene. Returns the image plus the
    altered cases per gene.

    Args:
        genes: 1 to 20 gene symbols.
        data_types: Among germline_snv (default), germline_cnv, somatic_snv_all (tumor-normal
            and tumor-only together), somatic_cnv.
        max_gnomad_af: SNV only; keeps variants below this gnomAD AF or absent (e.g. 0.01).
        vep_impact: SNV only, e.g. ["HIGH", "MODERATE"].
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        if not genes or len(genes) > MAX_ONCOPRINT_GENES:
            raise _sql.InvalidInput(f"give 1 to {MAX_ONCOPRINT_GENES} genes")
        genes = [_sql.symbol(g) for g in dict.fromkeys(genes)]
        types = _sql.all_of(data_types or ["germline_snv"], COHORT_DATA_TYPES, "data_type")
        af = None
        if max_gnomad_af is not None:
            af = _sql.number(max_gnomad_af)
            if not 0 < af <= 1:
                raise _sql.InvalidInput("max_gnomad_af must be in (0, 1]")
        impacts = _sql.all_of(canonical_values("vep_impact", vep_impact or []), ENUMS["vep_impact"], "vep_impact")
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_oncoprint, genes, types, tenant, af, impacts)

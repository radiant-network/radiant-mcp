"""``lollipop``: the carriers' variants of a gene along its protein."""

import re
from dataclasses import replace

from fastmcp.tools import ToolResult

from ... import charts
from ... import portal_api as _api
from ... import sql as _sql
from ...domain.consequences import consequence_class
from ...domain.criteria import Criteria, build_criteria
from ...domain.data_types import COHORT_SNV_DATA_TYPES, CohortSnvDataType
from ...portal_api import RadiantApi, resolve_tenant
from ...queries.carriers import lollipop_query
from ...results import ToolOutput, failure, not_found, ok, respond
from ...services.genes import resolve_gene

_AA_POSITION = re.compile(r"(\d+)")


def _lollipop(api: RadiantApi, token: str, criteria_: Criteria, tenant: str | None) -> ToolOutput:
    tenant_code = resolve_tenant(api, tenant)
    gene = resolve_gene(api, tenant_code, criteria_.gene)
    if gene is None:
        raise not_found(f"Gene {criteria_.gene!r} not found.")
    criteria_ = replace(criteria_, gene=_sql.symbol(gene["symbol"]))
    rows = _sql.run(token, *lollipop_query(tenant_code, criteria_))
    positioned = []
    for row in rows:
        match = _AA_POSITION.search(row.get("aa_change") or "")
        if match:
            positioned.append((row, int(match.group(1))))
    if not positioned:
        return ok({"tenant": tenant_code, "gene": criteria_.gene, "criteria": criteria_.describe(), "variants_drawn": 0,
                   "variants_without_protein_change": len(rows)},
                  warnings=["No carrier variant with a protein change: no chart drawn."])
    per_transcript: dict[str, int] = {}
    for row, _ in positioned:
        per_transcript[row["transcript_id"]] = per_transcript.get(row["transcript_id"], 0) + 1
    transcript = max(per_transcript, key=per_transcript.get)
    points = [charts.Lollipop(position, row["aa_change"], int(row["carriers"]), consequence_class(row["kind"].split(",")))
              for row, position in positioned if row["transcript_id"] == transcript]
    left_out = len(positioned) - len(points)
    png = charts.lollipop(points, f"{criteria_.gene} — variants of the carriers",
                          f"transcript {transcript or 'not annotated'}; {criteria_.data_type}")
    return ok({
        "tenant": tenant_code, "gene": criteria_.gene, "transcript": transcript, "criteria": criteria_.describe(),
        "variants_drawn": len(points), "variants_on_other_transcripts": left_out,
        "variants_without_protein_change": len(rows) - len(positioned),
        "top_variants": [{"aa_change": p.label, "carriers": p.carriers, "class": p.kind}
                         for p in sorted(points, key=lambda p: -p.carriers)[:15]],
    }, image=png)


async def lollipop(
    gene: str,
    data_type: CohortSnvDataType = "germline_snv",
    vep_impact: list[str] | None = None,
    clinvar: list[str] | None = None,
    max_gnomad_af: float | None = None,
    zygosity: list[str] | None = None,
    tenant: str | None = None,
) -> ToolResult:
    """Lollipop plot of the carriers' variants along the protein of a gene.

    Each variant is placed at its protein position (from aa_change), its height
    is the number of carrier patients, its colour its consequence class. The
    variants are drawn on the transcript most of them are annotated on, which
    the result names; variants on other transcripts are counted, not drawn.

    Args:
        gene: Gene symbol.
        data_type: germline_snv (default) or somatic_snv_all (tumor-normal and tumor-only together).
        vep_impact, clinvar, max_gnomad_af, zygosity: As in cohort_carriers.
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        _sql.one_of(data_type, COHORT_SNV_DATA_TYPES, "data_type")
        criteria_ = build_criteria(data_type, gene, vep_impact, clinvar, max_gnomad_af, zygosity, None)
    except _sql.InvalidInput as exc:
        return respond(failure(exc))
    return await _api.run(_lollipop, criteria_, tenant)

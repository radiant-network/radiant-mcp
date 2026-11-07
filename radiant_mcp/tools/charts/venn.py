"""``family_venn``: the SNVs shared by the members of a trio or a duo."""

import json
from typing import Any, Optional

from ... import charts
from ... import portal_api as _api
from ... import sql as _sql
from ...domain import rules as _rules
from ...domain.keys import variant_part
from ...enums import ENUMS, canonical_values
from ...portal_api import RadiantApi
from ...queries.charts import venn_query
from ...services.cases import case_context
from ._common import image as _image


def _members(case: dict, keys: list[dict]) -> list[tuple[str, dict]]:
    """``[(role, germline_snv key)]`` for the proband, then father and mother when sequenced."""
    roles = {s.get("seq_id"): (s.get("relationship_to_proband") or "").lower() for s in case.get("sequencing_experiments", [])}
    by_role: dict[str, dict] = {}
    for key in keys:
        if key.get("data_type") != "germline_snv" or key.get("part") is None:
            continue
        role = roles.get(key["seq_id"])
        if role in ("proband", "father", "mother") and role not in by_role:
            by_role[role] = key
    return [(role, by_role[role]) for role in ("proband", "father", "mother") if role in by_role]


def _family_venn(api: RadiantApi, token: str, case_id: int, tenant: Optional[str], rare_only: bool,
                 vep_impact: list[str]) -> Any:
    context = case_context(api, token, case_id, tenant)
    if "error" in context:
        return context
    tenant_code, case = context["tenant"], context["case"]
    members = _members(case, context["occurrence_keys"])
    if not members or members[0][0] != "proband":
        return {"error": f"Case {case_id} has no germline SNV analysis of the proband with a known `part`."}
    if len(members) == 1:
        return {"error": f"Case {case_id} has no sequenced parent: a Venn diagram needs a trio or a duo."}
    for _, key in members:
        key["variant_part"] = key.get("variant_part", variant_part(key["part"]))
    rows = _sql.run(token, *venn_query(_sql.tenant_db(tenant_code), members, rare_only, vep_impact))
    counts = {tuple(int(r[f"m{i}"]) for i in range(len(members))): int(r["n"]) for r in rows}
    labels = [role.capitalize() for role, _ in members]
    filters = []
    if rare_only:
        filters.append(_rules.RARE_DEFINITION)
    if vep_impact:
        filters.append(f"impact {' or '.join(vep_impact)}")
    title = f"Case {case_id} — SNVs shared by {' / '.join(labels).lower()}"
    png = charts.venn(labels, counts, title + (f"\n({'; '.join(filters)})" if filters else ""))
    regions = {" & ".join(l for l, flag in zip(labels, region, strict=True) if flag): n
               for region, n in sorted(counts.items(), key=lambda kv: (-sum(kv[0]), kv[0])) if any(region)}
    text = json.dumps({
        "case_id": case_id, "tenant": tenant_code, "members": labels,
        "variants_per_region": regions, "filters": filters or ["none (all called SNVs)"],
        "counted": "variants with an alternate allele (HET, HOM or HEM) in each member",
        "portal_url": case.get("portal_url"),
    })
    return _image(text, png)


async def family_venn(case_id: int, rare_only: bool = False, vep_impact: Optional[list[str]] = None,
                      tenant: Optional[str] = None) -> Any:
    """Venn diagram of the SNVs shared by a trio (proband, father, mother) or a duo.

    The tool counts, from each member's germline SNV analysis of the case, the
    variants with an alternate allele in each combination of members, and
    returns the diagram as an image plus the counts per region. The
    proband-only region holds the de novo candidates. Use this instead of SQL.

    Args:
        case_id: Numeric Radiant case ID (germline, with at least one parent sequenced).
        rare_only: Count only rare variants (gnomAD v3 AF < 1 % or absent).
        vep_impact: Count only these impacts, e.g. ["HIGH", "MODERATE"].
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        case_id = _sql.integer(case_id)
        impacts = _sql.all_of(canonical_values("vep_impact", vep_impact or []), ENUMS["vep_impact"], "vep_impact")
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_family_venn, case_id, tenant, bool(rare_only), impacts)

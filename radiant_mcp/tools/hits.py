"""``second_hit``: two events on the same gene, in one case.

Germline case, per sequencing:
- a rare HET qualifying SNV and a PASS LOSS CNV over the same gene;
- the compound heterozygous pairs computed by the pipeline (slivar).

Somatic case: the patient's germline P/LP variants (from their germline
sequencing) and a somatic event in the same gene: a PASS SNV of HIGH or
MODERATE impact, or a PASS LOSS / CNLOH CNV.

Every occurrence query filters on the ``part`` of its key, taken from
``get_case_context``. The rules live in ``_rules`` and are restated in the
output.
"""

from typing import Any

from fastmcp.tools import ToolResult

from .. import portal_api as _api
from .. import sql as _sql
from ..domain import rules as _rules
from ..domain.data_types import SOMATIC_SNV_CASE_DATA_TYPES
from ..domain.keys import with_parts
from ..portal_links import occurrence_url, variant_url, with_url
from ..queries.hits import (
    ROW_LIMIT,
    comphet_query,
    family_deletions_query,
    germline_keys_query,
    germline_plp_query,
    snv_cnv_query,
    somatic_cnv_query,
    somatic_snv_query,
)
from ..results import ToolOutput, Truncation, failure, ok, respond
from ..services.cases import case_context

# How an SNV is assigned to a gene (queries/hits.py), stated in every result.
_GENE_RULE = ("An SNV is in a gene when it has a consequence on one of its transcripts other than upstream, "
              "downstream or intergenic; its impact is the impact on that gene.")

def mechanism(zygosity: str, inside: bool) -> str:
    if zygosity in _rules.HEMIZYGOUS_CALLS and inside:
        return ("hemizygous: the SNV lies inside the deletion, so its homozygous call is the only remaining "
                "allele; the deletion is the second hit, in trans")
    if inside:
        return "HET call inside a heterozygous deletion: unexpected (one allele is deleted), check both calls"
    return "SNV and deletion on the same gene: compound heterozygous if they are in trans"


def _latest_task(keys: list[dict], what: str, warnings: list[str]) -> list[dict]:
    """One key per sequencing: its latest task. A re-run adds a task next to the old one,
    and reading both would list every hit twice."""
    latest: dict[int, dict] = {}
    for key in keys:
        kept = latest.get(key["seq_id"])
        if kept is None or key["task_id"] > kept["task_id"]:
            latest[key["seq_id"]] = key
    for key in keys:
        if latest[key["seq_id"]] is not key:
            warnings.append(f"seq_id={key['seq_id']} task_id={key['task_id']} ({what}): older task, "
                            f"task {latest[key['seq_id']]['task_id']} read instead")
    return list(latest.values())


def _usable(keys: list[dict], data_type: str, warnings: list[str]) -> list[dict]:
    out = []
    for key in keys:
        if key.get("data_type") != data_type:
            continue
        if key.get("part") is None:
            reason = "not imported yet" if key.get("imported") is False else "no `part`"
            warnings.append(f"seq_id={key['seq_id']} task_id={key.get('task_id')} ({data_type}): {reason}, skipped")
            continue
        out.append(key)
    return _latest_task(out, data_type, warnings)


def _capped(token: str, query: tuple, what: str, truncated: list[str]) -> list[dict]:
    """The rows of ``query``, cut at ROW_LIMIT; ``what`` is recorded when rows were left out."""
    rows = _sql.run(token, *query)
    if len(rows) > ROW_LIMIT:
        truncated.append(what)
        return rows[:ROW_LIMIT]
    return rows


def _germline(token: str, tenant: str, case: dict, keys: list[dict], warnings: list[str],
              truncated: list[str]) -> list[dict]:
    tenant_db = _sql.tenant_db(tenant)
    case_id = case.get("case_id")
    seqs = {s.get("seq_id"): s for s in case.get("sequencing_experiments", [])}
    snv_keys = _usable(keys, "germline_snv", warnings)
    cnv_keys = _usable(keys, "germline_cnv", warnings)
    results = []
    for snv_key in snv_keys:
        seq_id = snv_key["seq_id"]
        seq = seqs.get(seq_id, {})
        entry: dict[str, Any] = {
            "seq_id": seq_id,
            "relationship_to_proband": seq.get("relationship_to_proband"),
            "sample_submitter_id": seq.get("sample_submitter_id"),
        }

        snv_cnv: list[dict] = []
        same_seq_cnv = [k for k in cnv_keys if k["seq_id"] == seq_id]
        if not same_seq_cnv:
            entry["snv_cnv_note"] = "no germline CNV analysis for this sequencing"
        for cnv_key in same_seq_cnv:
            for row in _capped(token, snv_cnv_query(tenant_db, snv_key, cnv_key), f"seq_id={seq_id} snv_cnv",
                               truncated):
                row["clinvar_interpretation"] = _sql.as_list(row.get("clinvar_interpretation"))
                row["snv_inside_cnv"] = bool(row.get("snv_inside_cnv"))
                row["mechanism"] = mechanism(row.get("zygosity"), row["snv_inside_cnv"])
                with_url(row, occurrence_url(tenant, case_id, seq_id, "germline_snv",
                                             task_id=snv_key["task_id"], variant_id=str(row["locus_id"])))
                with_url(row, occurrence_url(tenant, case_id, seq_id, "germline_cnv", task_id=cnv_key["task_id"]),
                         key="cnv_portal_url")
                snv_cnv.append(row)
        _attach_family_deletions(token, tenant_db, snv_cnv, [k for k in cnv_keys if k["seq_id"] != seq_id], seqs,
                                 truncated)
        entry["snv_cnv"] = snv_cnv

        pairs: list[dict] = []
        seen: set[tuple] = set()
        for row in _capped(token, comphet_query(tenant_db, snv_key), f"seq_id={seq_id} compound_heterozygous",
                           truncated):
            # The same two variants can pair in two overlapping genes: one entry per gene.
            pair = (row["gene"], frozenset((row["locus_id"], row["partner_locus_id"])))
            if pair in seen:
                continue
            seen.add(pair)
            with_url(row, occurrence_url(tenant, case_id, seq_id, "germline_snv",
                                         task_id=snv_key["task_id"], variant_id=str(row["locus_id"])))
            with_url(row, occurrence_url(tenant, case_id, seq_id, "germline_snv",
                                         task_id=snv_key["task_id"], variant_id=str(row["partner_locus_id"])),
                     key="partner_portal_url")
            pairs.append(row)
        entry["compound_heterozygous"] = pairs
        results.append(entry)
    return results


def _attach_family_deletions(token: str, tenant_db: str, hits: list[dict], other_keys: list[dict], seqs: dict,
                             truncated: list[str]) -> None:
    """Add, to each SNV + deletion hit, the other members' deletions over the same gene.

    A parent's deletion over the SNV explains that parent's WT call, and with it an
    apparent uniparental disomy.
    """
    genes = sorted({h["gene"] for h in hits if h.get("gene")})
    if not genes:
        return
    found: dict[str, list[dict]] = {}
    for key in other_keys:
        for row in _capped(token, family_deletions_query(tenant_db, key, genes),
                           f"seq_id={key['seq_id']} family_deletions", truncated):
            row["relationship_to_proband"] = seqs.get(row["seq_id"], {}).get("relationship_to_proband")
            found.setdefault(row["gene"], []).append(row)
    for hit in hits:
        deletions = []
        for row in found.get(hit["gene"], []):
            covers = (row["chromosome"] == hit.get("cnv_chromosome") and hit.get("snv_start") is not None
                      and row["start"] <= hit["snv_start"] <= row["end"])
            deletions.append({**row, "covers_snv": covers})
        hit["family_deletions"] = deletions


def _proband_patient_id(case: dict) -> int | None:
    for member in case.get("members", []):
        if member.get("relationship_to_proband") == "proband" and member.get("patient_id") is not None:
            return int(member["patient_id"])
    return None


def _somatic(token: str, tenant: str, case: dict, keys: list[dict], warnings: list[str],
             truncated: list[str]) -> dict:
    tenant_db, shared_db = _sql.tenant_db(tenant), _sql.shared_db()
    case_id = case.get("case_id")
    patient_id = _proband_patient_id(case)
    if patient_id is None:
        warnings.append("no proband patient id in the case: the germline sequencing cannot be found, "
                        "so the germline first hit cannot be checked")
        return {}

    germline_keys = _latest_task([
        with_parts({"case_id": row["case_id"], "seq_id": int(row["seq_id"]), "task_id": int(row["task_id"])},
                   int(row["part"]))
        for row in _sql.run(token, *germline_keys_query(shared_db, tenant, patient_id))
    ], "germline_snv", warnings)
    if not germline_keys:
        warnings.append("no germline sequencing for this patient: the germline first hit cannot be checked")
        return {"patient_id": patient_id}

    germline: dict[str, list[dict]] = {}
    for key in germline_keys:
        for row in _capped(token, germline_plp_query(tenant_db, key), f"seq_id={key['seq_id']} germline P/LP",
                           truncated):
            row["clinvar_interpretation"] = _sql.as_list(row.get("clinvar_interpretation"))
            row["interpreted_plp"] = bool(row.get("interpreted_plp"))
            with_url(row, occurrence_url(tenant, key["case_id"], key["seq_id"], "germline_snv",
                                         task_id=key["task_id"], variant_id=str(row["locus_id"])))
            with_url(row, variant_url(tenant, str(row["locus_id"])), key="variant_portal_url")
            germline.setdefault(row["gene"], []).append(row)

    genes = sorted(g for g in germline if g)
    hits: dict[str, dict[str, list]] = {g: {"somatic_snv": [], "somatic_cnv": []} for g in genes}
    if genes:
        for data_type in SOMATIC_SNV_CASE_DATA_TYPES:
            for key in _usable(keys, data_type, warnings):
                for row in _capped(token, somatic_snv_query(tenant_db, key, genes),
                                   f"seq_id={key['seq_id']} {data_type}", truncated):
                    with_url(row, occurrence_url(tenant, case_id, key["seq_id"], data_type,
                                                 task_id=key["task_id"], variant_id=str(row["locus_id"])))
                    hits[row["gene"]]["somatic_snv"].append({**row, "data_type": data_type})
        for key in _usable(keys, "somatic_cnv", warnings):
            for row in _capped(token, somatic_cnv_query(tenant_db, key, genes),
                               f"seq_id={key['seq_id']} somatic_cnv", truncated):
                with_url(row, occurrence_url(tenant, case_id, key["seq_id"], "somatic_cnv", task_id=key["task_id"]))
                hits[row["gene"]]["somatic_cnv"].append(row)

    two_hit = [
        {"gene": g, "germline": germline[g], **hits[g]}
        for g in genes if hits[g]["somatic_snv"] or hits[g]["somatic_cnv"]
    ]
    return {
        "patient_id": patient_id,
        "germline_sequencings": [{k: v for k, v in key.items() if k != "variant_part"} for key in germline_keys],
        "genes": two_hit,
        "germline_plp_without_somatic_event": [g for g in genes if not (hits[g]["somatic_snv"] or hits[g]["somatic_cnv"])],
    }


def _second_hit(api: _api.RadiantApi, token: str, case_id: int, tenant: str | None) -> ToolOutput:
    context = case_context(api, token, case_id, tenant)
    tenant_code, case = context["tenant"], context["case"]
    warnings = list(context["warnings"])
    truncated: list[str] = []
    case_type = (case.get("case_type") or "").lower()
    result: dict[str, Any] = {"tenant": tenant_code, "case_id": case_id, "case_type": case_type}
    if case_type == "somatic":
        result["rules"] = (
            f"Germline first hit: P/LP variant of the patient ({_rules.PLP_DEFINITION}) "
            f"Somatic second hit in the same gene: PASS SNV of impact {' or '.join(_rules.QUALIFYING_IMPACTS)}, "
            f"or PASS CNV of type {' or '.join(_rules.SOMATIC_SECOND_HIT_CNV_TYPES)}. {_GENE_RULE}"
        )
        result.update(_somatic(token, tenant_code, case, context["occurrence_keys"], warnings, truncated))
    else:
        result["rules"] = (
            f"SNV + CNV: an SNV ({_rules.RARE_DEFINITION}, impact {' or '.join(_rules.QUALIFYING_IMPACTS)} or P/LP: "
            f"{_rules.PLP_DEFINITION}) with a PASS CNV of type {' or '.join(_rules.GERMLINE_SECOND_HIT_CNV_TYPES)} "
            "over the same gene: either a HET SNV anywhere in the gene, or a HOM/HEM SNV inside the deletion "
            "(hemizygous: the only remaining allele is called homozygous). `mechanism` says which. "
            "`family_deletions` lists the other members' deletions over the gene; one that covers the SNV explains "
            "that member's WT call and an apparent uniparental disomy. "
            f"Compound heterozygous: the pairs computed by the pipeline. {_GENE_RULE}"
        )
        result["sequencings"] = _germline(token, tenant_code, case, context["occurrence_keys"], warnings, truncated)
    cut = None
    if truncated:
        cut = Truncation(reason=f"These lists stop at {ROW_LIMIT} rows, in gene order: genes later in the alphabet "
                                "may be missing. Do not conclude that a gene has no second hit.", lists=truncated)
    return ok(result, warnings=warnings, truncated=cut, portal_url=case.get("portal_url"))


# -- tool --------------------------------------------------------------------

async def second_hit(case_id: int, tenant: str | None = None) -> ToolResult:
    """Find genes hit twice in a case.

    Germline case, for each sequencing with variants: ``snv_cnv`` lists the
    rare qualifying SNVs in a gene also covered by a PASS LOSS CNV: a HET SNV
    anywhere in the gene, or a HOM/HEM SNV inside the deletion (hemizygous:
    the remaining allele is called homozygous); ``mechanism`` says which.
    ``compound_heterozygous`` lists the compound heterozygous pairs computed
    by the pipeline, once per pair and gene.

    Somatic case: ``genes`` lists the genes where the patient has a germline
    P/LP variant (from their germline sequencing) and a somatic PASS SNV of
    HIGH/MODERATE impact or a PASS LOSS/CNLOH CNV (Knudson two-hit);
    ``germline_plp_without_somatic_event`` lists the germline P/LP genes with no
    somatic event.

    ``rules`` states the exact criteria applied: quote them when reporting.
    Each list stops at 200 rows; ``truncated.lists`` names the lists that were cut.
    When a sequencing was re-analysed, its latest task is read.
    Every variant and CNV carries a ``portal_url``. Use this instead of
    cross-checking SNV and CNV lists by hand.

    Args:
        case_id: Numeric Radiant case ID.
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        case_id = _sql.integer(case_id)
    except _sql.InvalidInput as exc:
        return respond(failure(exc))
    return await _api.run(_second_hit, case_id, tenant)

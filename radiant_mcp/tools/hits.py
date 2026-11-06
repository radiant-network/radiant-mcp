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

from typing import Any, Optional

from ..portal_links import occurrence_url, variant_url, with_url
from . import _api, _rules, _sql
from .cases import _get_case_context

_ROW_LIMIT = 200
_VARIANT_PART_DIVISOR = 10


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
        " SELECT o.locus_id, v.symbol, v.hgvsg, v.aa_change, v.vep_impact, v.gnomad_v3_af,"
        "  v.clinvar_interpretation, o.exomiser_acmg_classification, v.chromosome, v.start"
        f" FROM {tenant_db}.germline__snv__occurrence o"
        f" JOIN {tenant_db}.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = {p(snv_key['variant_part'])}"
        f" LEFT JOIN {_interpretations(p, tenant_db, snv_key['seq_id'])} ig ON ig.locus_id = CAST(o.locus_id AS VARCHAR)"
        f" WHERE o.seq_id = {p(snv_key['seq_id'])} AND o.task_id = {p(snv_key['task_id'])} AND o.part = {p(snv_key['part'])}"
        f"  AND o.zygosity = {p(_rules.HET)}"
        f"  AND (v.gnomad_v3_af IS NULL OR v.gnomad_v3_af < {p(_rules.RARE_MAX_AF)})"
        f"  AND (v.vep_impact IN ({p.list(_rules.QUALIFYING_IMPACTS)}) OR {_plp_condition(p)})"
        "), cnv AS ("
        " SELECT c.cnv_id, c.chromosome, c.start, c.end, c.type, c.cn, c.length, g.symbol"
        f" FROM {tenant_db}.germline__cnv__occurrence c, unnest(c.symbol) AS g(symbol)"
        f" WHERE c.seq_id = {p(cnv_key['seq_id'])} AND c.task_id = {p(cnv_key['task_id'])} AND c.part = {p(cnv_key['part'])}"
        f"  AND c.filter = {p(_rules.PASS)} AND c.type IN ({p.list(_rules.GERMLINE_SECOND_HIT_CNV_TYPES)})"
        ")"
        " SELECT snv.symbol AS gene, snv.locus_id, snv.hgvsg, snv.aa_change, snv.vep_impact, snv.gnomad_v3_af,"
        "  snv.clinvar_interpretation, snv.exomiser_acmg_classification,"
        "  cnv.cnv_id, cnv.type AS cnv_type, cnv.chromosome AS cnv_chromosome, cnv.start AS cnv_start,"
        "  cnv.end AS cnv_end, cnv.length AS cnv_length, cnv.cn AS cnv_cn,"
        "  (snv.start BETWEEN cnv.start AND cnv.end) AS snv_inside_cnv"
        " FROM snv JOIN cnv ON cnv.symbol = snv.symbol"
        f" ORDER BY gene, snv.locus_id LIMIT {p(_ROW_LIMIT)}"
    )
    return sql, p


def comphet_query(tenant_db: str, snv_key: dict) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    sql = (
        "SELECT c.symbol AS gene, c.locus_id, v1.hgvsg, v1.vep_impact, v1.gnomad_v3_af,"
        " c.partner_locus_id, v2.hgvsg AS partner_hgvsg, v2.vep_impact AS partner_vep_impact,"
        " v2.gnomad_v3_af AS partner_gnomad_v3_af"
        f" FROM {tenant_db}.germline__snv__comphet c"
        f" JOIN {tenant_db}.snv__variant_partitioned v1 ON v1.locus_id = c.locus_id AND v1.part = {p(snv_key['variant_part'])}"
        f" JOIN {tenant_db}.snv__variant_partitioned v2 ON v2.locus_id = c.partner_locus_id AND v2.part = {p(snv_key['variant_part'])}"
        f" WHERE c.seq_id = {p(snv_key['seq_id'])} AND c.task_id = {p(snv_key['task_id'])} AND c.part = {p(snv_key['part'])}"
        f" ORDER BY gene, c.locus_id LIMIT {p(_ROW_LIMIT)}"
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
        f" ORDER BY gene LIMIT {p(_ROW_LIMIT)}"
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
        f" ORDER BY gene LIMIT {p(_ROW_LIMIT)}"
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
        f" ORDER BY gene LIMIT {p(_ROW_LIMIT)}"
    )
    return sql, p


def _usable(keys: list[dict], data_type: str, warnings: list[str]) -> list[dict]:
    out = []
    for key in keys:
        if key.get("data_type") != data_type:
            continue
        if key.get("part") is None:
            warnings.append(f"seq_id={key['seq_id']} task_id={key.get('task_id')} ({data_type}): no `part`, skipped")
            continue
        out.append(key)
    return out


def _germline(token: str, tenant: str, case: dict, keys: list[dict], warnings: list[str]) -> list[dict]:
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
            for row in _sql.run(token, *snv_cnv_query(tenant_db, snv_key, cnv_key)):
                row["clinvar_interpretation"] = _sql.as_list(row.get("clinvar_interpretation"))
                row["snv_inside_cnv"] = bool(row.get("snv_inside_cnv"))
                with_url(row, occurrence_url(tenant, case_id, seq_id, "germline_snv",
                                             task_id=snv_key["task_id"], variant_id=str(row["locus_id"])))
                with_url(row, occurrence_url(tenant, case_id, seq_id, "germline_cnv", task_id=cnv_key["task_id"]),
                         key="cnv_portal_url")
                snv_cnv.append(row)
        entry["snv_cnv"] = snv_cnv

        pairs: list[dict] = []
        seen: set[frozenset] = set()
        for row in _sql.run(token, *comphet_query(tenant_db, snv_key)):
            pair = frozenset((row["locus_id"], row["partner_locus_id"]))
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


def _proband_patient_id(case: dict) -> Optional[int]:
    for member in case.get("members", []):
        if member.get("relationship_to_proband") == "proband" and member.get("patient_id") is not None:
            return int(member["patient_id"])
    if case.get("proband_id") is not None:
        return int(case["proband_id"])
    return None


def _somatic(token: str, tenant: str, case: dict, keys: list[dict], warnings: list[str]) -> dict:
    tenant_db, shared_db = _sql.tenant_db(tenant), _sql.shared_db()
    case_id = case.get("case_id")
    patient_id = _proband_patient_id(case)
    if patient_id is None:
        return {"note": "no proband patient id in the case: the germline sequencing cannot be found"}

    germline_keys = []
    for row in _sql.run(token, *germline_keys_query(shared_db, tenant, patient_id)):
        part = int(row["part"])
        germline_keys.append({
            "case_id": row["case_id"], "seq_id": int(row["seq_id"]), "task_id": int(row["task_id"]),
            "part": part, "variant_part": part // _VARIANT_PART_DIVISOR,
        })
    if not germline_keys:
        return {"patient_id": patient_id,
                "note": "no germline sequencing for this patient: the germline first hit cannot be checked"}

    germline: dict[str, list[dict]] = {}
    for key in germline_keys:
        for row in _sql.run(token, *germline_plp_query(tenant_db, key)):
            row["clinvar_interpretation"] = _sql.as_list(row.get("clinvar_interpretation"))
            row["interpreted_plp"] = bool(row.get("interpreted_plp"))
            with_url(row, occurrence_url(tenant, key["case_id"], key["seq_id"], "germline_snv",
                                         task_id=key["task_id"], variant_id=str(row["locus_id"])))
            with_url(row, variant_url(tenant, str(row["locus_id"])), key="variant_portal_url")
            germline.setdefault(row["gene"], []).append(row)

    genes = sorted(g for g in germline if g)
    hits: dict[str, dict[str, list]] = {g: {"somatic_snv": [], "somatic_cnv": []} for g in genes}
    if genes:
        for data_type in ("somatic_snv_tn", "somatic_snv_to"):
            for key in _usable(keys, data_type, warnings):
                for row in _sql.run(token, *somatic_snv_query(tenant_db, key, genes)):
                    with_url(row, occurrence_url(tenant, case_id, key["seq_id"], data_type,
                                                 task_id=key["task_id"], variant_id=str(row["locus_id"])))
                    hits[row["gene"]]["somatic_snv"].append({**row, "data_type": data_type})
        for key in _usable(keys, "somatic_cnv", warnings):
            for row in _sql.run(token, *somatic_cnv_query(tenant_db, key, genes)):
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


def _second_hit(api: _api.RadiantApi, token: str, case_id: int, tenant: Optional[str]) -> dict:
    context = _get_case_context(api, token, case_id, tenant)
    if "error" in context:
        return context
    tenant_code, case = context["tenant"], context["case"]
    warnings = list(context.get("warnings", []))
    case_type = (case.get("case_type") or "").lower()
    result: dict[str, Any] = {"tenant": tenant_code, "case_id": case_id, "case_type": case_type}
    with_url(result, case.get("portal_url"))
    if case_type == "somatic":
        result["rules"] = (
            f"Germline first hit: P/LP variant of the patient ({_rules.PLP_DEFINITION}) "
            f"Somatic second hit in the same gene: PASS SNV of impact {' or '.join(_rules.QUALIFYING_IMPACTS)}, "
            f"or PASS CNV of type {' or '.join(_rules.SOMATIC_SECOND_HIT_CNV_TYPES)}."
        )
        result.update(_somatic(token, tenant_code, case, context["occurrence_keys"], warnings))
    else:
        result["rules"] = (
            f"SNV + CNV: HET SNV, {_rules.RARE_DEFINITION}, impact {' or '.join(_rules.QUALIFYING_IMPACTS)} or P/LP "
            f"({_rules.PLP_DEFINITION}), with a PASS CNV of type "
            f"{' or '.join(_rules.GERMLINE_SECOND_HIT_CNV_TYPES)} over the same gene. snv_inside_cnv flags an SNV "
            "inside the deleted region (expected hemizygous, so a HET call there needs review). "
            "Compound heterozygous: the pairs computed by the pipeline."
        )
        result["sequencings"] = _germline(token, tenant_code, case, context["occurrence_keys"], warnings)
    if warnings:
        result["warnings"] = warnings
    return result


# -- tool --------------------------------------------------------------------

async def second_hit(case_id: int, tenant: Optional[str] = None) -> dict:
    """Find genes hit twice in a case.

    Germline case, for each sequencing with variants: ``snv_cnv`` lists the
    rare HET qualifying SNVs that sit in a gene also covered by a PASS LOSS CNV
    (a second hit for a recessive presentation), and
    ``compound_heterozygous`` lists the compound heterozygous pairs computed by
    the pipeline.

    Somatic case: ``genes`` lists the genes where the patient has a germline
    P/LP variant (from their germline sequencing) and a somatic PASS SNV of
    HIGH/MODERATE impact or a PASS LOSS/CNLOH CNV (Knudson two-hit);
    ``germline_plp_without_somatic_event`` lists the germline P/LP genes with no
    somatic event.

    ``rules`` states the exact criteria applied: quote them when reporting.
    Every variant and CNV carries a ``portal_url``. Use this instead of
    cross-checking SNV and CNV lists by hand.

    Args:
        case_id: Numeric Radiant case ID.
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        case_id = _sql.integer(case_id)
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_second_hit, case_id, tenant)

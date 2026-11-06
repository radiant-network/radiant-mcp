"""Chart tools (RAD-254): family Venn, survival, oncoprint, lollipop, patient
timeline and bar chart, returned as MCP image content.

Each tool reads its data itself, under the caller's JWT (portal API for
clinical data, curated SQL for genomic data), draws it with ``charts`` and
returns the PNG plus a text with the values drawn, so the model can comment
on the chart and a reader without the image still gets the numbers. LibreChat
saves MCP images as message attachments and shows them in the conversation.
"""

import json
import re
from typing import Any, Optional

from fastmcp.utilities.types import Image

from .. import charts
from ..enums import ENUMS, canonical_values
from . import _api, _rules, _sql
from ._api import RadiantApi, resolve_tenant
from .cases import _get_case_context, criteria
from .cohort import Criteria, build_criteria, carriers_cte
from .genes import resolve_gene
from .patients import PATIENT_SEARCH_FILTER_FIELDS, _get_patient_context, _no_clinical_data

ALT_ZYGOSITIES = ("HET", "HOM", "HEM")
MAX_ONCOPRINT_GENES = 20
MAX_ONCOPRINT_CASES = 100
MAX_SURVIVAL_PATIENTS = 5000
MAX_BARS = 50
_PAGE = 100
_DAYS_PER_YEAR = 365.25

# VEP consequence -> oncoprint / lollipop class, most severe first.
_CONSEQUENCE_CLASSES = (
    ("truncating", {"stop_gained", "frameshift_variant", "start_lost", "stop_lost", "transcript_ablation"}),
    ("splice", {"splice_acceptor_variant", "splice_donor_variant", "splice_donor_5th_base_variant",
                "splice_donor_region_variant", "splice_region_variant"}),
    ("missense", {"missense_variant"}),
    ("inframe", {"inframe_insertion", "inframe_deletion", "protein_altering_variant"}),
)
_AA_POSITION = re.compile(r"(\d+)")


def consequence_class(consequences) -> str:
    found = set(_sql.as_list(consequences) if isinstance(consequences, str) else consequences or [])
    for name, members in _CONSEQUENCE_CLASSES:
        if found & members:
            return name
    return "other"


def _image(text: str, png: bytes) -> list:
    return [text, Image(data=png, format="png")]


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


def venn_query(tenant_db: str, members: list[tuple[str, dict]], rare_only: bool,
               vep_impact: list[str]) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    flags = ", ".join(
        f"MAX(CASE WHEN o.seq_id = {p(k['seq_id'])} AND o.task_id = {p(k['task_id'])} THEN 1 ELSE 0 END) AS m{i}"
        for i, (_, k) in enumerate(members)
    )
    keys = " OR ".join(
        f"(o.seq_id = {p(k['seq_id'])} AND o.task_id = {p(k['task_id'])} AND o.part = {p(k['part'])})"
        for _, k in members
    )
    semi = ""
    if rare_only or vep_impact:
        conditions = ["v.locus_id = o.locus_id",
                      f"v.part IN ({p.list(sorted({k['variant_part'] for _, k in members}))})"]
        if rare_only:
            conditions.append(f"(v.gnomad_v3_af IS NULL OR v.gnomad_v3_af < {p(_rules.RARE_MAX_AF)})")
        if vep_impact:
            conditions.append(f"v.vep_impact IN ({p.list(vep_impact)})")
        semi = f" LEFT SEMI JOIN {tenant_db}.snv__variant_partitioned v ON {' AND '.join(conditions)}"
    group = ", ".join(f"m{i}" for i in range(len(members)))
    sql = (
        f"WITH occ AS (SELECT o.locus_id, {flags} FROM {tenant_db}.germline__snv__occurrence o{semi}"
        f" WHERE ({keys}) AND o.zygosity IN ({p.list(ALT_ZYGOSITIES)}) GROUP BY o.locus_id)"
        f" SELECT {group}, COUNT(*) AS n FROM occ GROUP BY {group}"
    )
    return sql, p


def _family_venn(api: RadiantApi, token: str, case_id: int, tenant: Optional[str], rare_only: bool,
                 vep_impact: list[str]) -> Any:
    context = _get_case_context(api, token, case_id, tenant)
    if "error" in context:
        return context
    tenant_code, case = context["tenant"], context["case"]
    members = _members(case, context["occurrence_keys"])
    if not members or members[0][0] != "proband":
        return {"error": f"Case {case_id} has no germline SNV analysis of the proband with a known `part`."}
    if len(members) == 1:
        return {"error": f"Case {case_id} has no sequenced parent: a Venn diagram needs a trio or a duo."}
    for _, key in members:
        key["variant_part"] = key.get("variant_part", key["part"] // 10)
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


# -- survival ------------------------------------------------------------------------

def _all_patients(api: RadiantApi, tenant: str, filters: dict) -> tuple[list[dict], int]:
    """Every patient of the list matching ``filters`` (capped), and the total count."""
    from radiant_python.models.list_body_with_criteria import ListBodyWithCriteria

    patients: list[dict] = []
    total = None
    while total is None or len(patients) < min(total, MAX_SURVIVAL_PATIENTS):
        body = ListBodyWithCriteria(search_criteria=criteria(filters), limit=_PAGE, offset=len(patients))
        resp = api.call("patients", "search_patients", tenant=tenant, list_body_with_criteria=body) or {}
        total = resp.get("count", 0)
        page = resp.get("list", [])
        if not page:
            break
        patients.extend(page)
    return patients[:MAX_SURVIVAL_PATIENTS], total or 0


def _survival_group(name: str, patients: list[dict]) -> tuple[charts.SurvivalGroup, int]:
    times, events, missing = [], [], 0
    for patient in patients:
        days = patient.get("survival_days")
        if days is None or days < 0:
            missing += 1
            continue
        times.append(days / _DAYS_PER_YEAR)
        events.append((patient.get("vital_status") or "").lower() == "deceased")
    return charts.SurvivalGroup(name, times, events), missing


def carrier_patients_query(tenant: str, criteria_: Criteria) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    return carriers_cte(p, tenant, criteria_) + " SELECT DISTINCT patient_id FROM car", p


def _survival_curve(api: RadiantApi, token: str, tenant: Optional[str], gene_criteria: Optional[Criteria],
                    group_a: Optional[dict], group_b: Optional[dict], labels: tuple[str, str]) -> Any:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err
    try:
        if gene_criteria is not None:
            gene = resolve_gene(api, tenant_code, gene_criteria.gene)
            if gene is None:
                return {"error": f"Gene {gene_criteria.gene!r} not found.", "status": 404}
            from dataclasses import replace

            gene_criteria = replace(gene_criteria, gene=_sql.symbol(gene["symbol"]))
            carrier_ids = {str(r["patient_id"]) for r in _sql.run(token, *carrier_patients_query(tenant_code, gene_criteria))}
            patients, total = _all_patients(api, tenant_code, {})
            sequenced = [p for p in patients if p.get("radiant_patient_id") is not None]
            carriers = [p for p in sequenced if str(p["radiant_patient_id"]) in carrier_ids]
            others = [p for p in sequenced if str(p["radiant_patient_id"]) not in carrier_ids]
            names = (labels[0] or f"{gene_criteria.gene} carriers", labels[1] or "Other sequenced patients")
            groups_in = [(names[0], carriers), (names[1], others)]
            definition = {"carriers": gene_criteria.describe(),
                          "excluded_not_sequenced": len(patients) - len(sequenced)}
        else:
            groups_in, definition, total = [], {}, 0
            for name, filters in ((labels[0] or "Group A", group_a), (labels[1] or "Group B", group_b)):
                patients, count = _all_patients(api, tenant_code, filters)
                groups_in.append((name, patients))
                definition[name] = filters
                total += count
    except Exception as exc:  # noqa: BLE001
        no_data = _no_clinical_data(tenant_code, exc)
        if no_data is None:
            raise
        return no_data

    groups, missing = [], {}
    for name, patients in groups_in:
        group, absent = _survival_group(name, patients)
        groups.append(group)
        missing[name] = absent
    if not any(g.times for g in groups):
        return {"tenant": tenant_code, "error": "No patient of these groups has survival data.", "groups": definition}
    title = f"Survival from initial diagnosis — {groups[0].name} vs {groups[1].name}"
    png, stats = charts.survival(groups, title)
    text = json.dumps({
        "tenant": tenant_code, "groups": stats.groups, "logrank_p": stats.logrank_p,
        "definition": definition, "excluded_without_survival_data": missing,
        "method": "Kaplan-Meier (scipy.stats.ecdf), log-rank test (scipy.stats.logrank); event = deceased, "
                  "time = survival_days (vital status day minus initial diagnosis day); ticks mark censored patients.",
        "capped_at": MAX_SURVIVAL_PATIENTS if total > MAX_SURVIVAL_PATIENTS else None,
    })
    return _image(text, png)


# -- oncoprint -----------------------------------------------------------------------

def oncoprint_query(tenant: str, data_type: str, genes: list[str], max_gnomad_af: Optional[float],
                    vep_impact: list[str]) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    tenant_db, shared_db = _sql.tenant_db(tenant), _sql.shared_db()
    staging = (f"JOIN {shared_db}.staging_sequencing_experiment s ON s.seq_id = x.seq_id AND s.task_id = x.task_id"
               f" AND s.tenant_code = {p(tenant)} AND s.deleted = false")
    if data_type.endswith("_snv"):
        where = [f"symbol IN ({p.list(genes)})"]
        if max_gnomad_af is not None:
            where.append(f"(gnomad_v3_af IS NULL OR gnomad_v3_af < {p(max_gnomad_af)})")
        if vep_impact:
            where.append(f"vep_impact IN ({p.list(vep_impact)})")
        somatic = data_type.startswith("somatic")
        table = "somatic__snv__occurrence" if somatic else "germline__snv__occurrence"
        seq = "tumor_seq_id" if somatic else "seq_id"
        occ_filter = (f"o.filter = {p(_rules.PASS)}" if somatic
                      else f"o.zygosity IN ({p.list(ALT_ZYGOSITIES)})")
        return (
            f"WITH v AS (SELECT locus_id, symbol, consequences FROM {tenant_db}.snv__variant_partitioned"
            f" WHERE {' AND '.join(where)}),"
            f" x AS (SELECT o.{seq} AS seq_id, o.task_id, v.symbol AS gene, array_join(v.consequences, ',') AS kind"
            f" FROM {tenant_db}.{table} o JOIN v ON v.locus_id = o.locus_id WHERE {occ_filter})"
            f" SELECT DISTINCT s.case_id, x.gene, x.kind FROM x {staging} LIMIT {p(50000)}"
        ), p
    table = "somatic__cnv__occurrence" if data_type.startswith("somatic") else "germline__cnv__occurrence"
    return (
        f"WITH x AS (SELECT c.seq_id, c.task_id, g.symbol AS gene, c.type AS kind"
        f" FROM {tenant_db}.{table} c, unnest(c.symbol) AS g(symbol)"
        f" WHERE g.symbol IN ({p.list(genes)}) AND c.filter = {p(_rules.PASS)})"
        f" SELECT DISTINCT s.case_id, x.gene, x.kind FROM x {staging} LIMIT {p(50000)}"
    ), p


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


# -- lollipop ------------------------------------------------------------------------

def lollipop_query(tenant: str, criteria_: Criteria) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    cte = carriers_cte(p, tenant, criteria_)
    tenant_db = _sql.tenant_db(tenant)
    return (
        cte + ", va AS (SELECT locus_id, transcript_id, aa_change, array_join(consequences, ',') AS kind"
        f" FROM {tenant_db}.snv__variant_partitioned WHERE symbol = {p(criteria_.gene)})"
        " SELECT va.locus_id, va.transcript_id, va.aa_change, va.kind, COUNT(DISTINCT car.patient_id) AS carriers"
        " FROM car JOIN va ON va.locus_id = car.variant_id WHERE va.aa_change IS NOT NULL"
        f" GROUP BY va.locus_id, va.transcript_id, va.aa_change, va.kind ORDER BY carriers DESC LIMIT {p(5000)}"
    ), p


def _lollipop(api: RadiantApi, token: str, criteria_: Criteria, tenant: Optional[str]) -> Any:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err
    gene = resolve_gene(api, tenant_code, criteria_.gene)
    if gene is None:
        return {"error": f"Gene {criteria_.gene!r} not found.", "status": 404}
    from dataclasses import replace

    criteria_ = replace(criteria_, gene=_sql.symbol(gene["symbol"]))
    rows = _sql.run(token, *lollipop_query(tenant_code, criteria_))
    positioned = []
    for row in rows:
        match = _AA_POSITION.search(row.get("aa_change") or "")
        if match:
            positioned.append((row, int(match.group(1))))
    if not positioned:
        return {"tenant": tenant_code, "gene": criteria_.gene, "message": "No carrier variant with a protein change."}
    per_transcript: dict[str, int] = {}
    for row, _ in positioned:
        per_transcript[row["transcript_id"]] = per_transcript.get(row["transcript_id"], 0) + 1
    transcript = max(per_transcript, key=per_transcript.get)
    points = [charts.Lollipop(position, row["aa_change"], int(row["carriers"]), consequence_class(row["kind"].split(",")))
              for row, position in positioned if row["transcript_id"] == transcript]
    left_out = len(positioned) - len(points)
    png = charts.lollipop(points, f"{criteria_.gene} — variants of the carriers",
                          f"transcript {transcript or 'not annotated'}; {criteria_.data_type}")
    text = json.dumps({
        "tenant": tenant_code, "gene": criteria_.gene, "transcript": transcript, "criteria": criteria_.describe(),
        "variants_drawn": len(points), "variants_on_other_transcripts": left_out,
        "variants_without_protein_change": len(rows) - len(positioned),
        "top_variants": [{"aa_change": p.label, "carriers": p.carriers, "class": p.kind}
                         for p in sorted(points, key=lambda p: -p.carriers)[:15]],
    })
    return _image(text, png)


# -- patient timeline ----------------------------------------------------------------

_TRACKS = ("Events", "Surgery", "Radiation", "Chemotherapy", "Imaging")


def _years(day) -> Optional[float]:
    return day / _DAYS_PER_YEAR if isinstance(day, (int, float)) else None


def timeline_items(context: dict) -> list[charts.TimelineItem]:
    items: list[charts.TimelineItem] = []
    patient = context.get("patient", {})
    for event in patient.get("events", []):
        start = _years(event.get("day"))
        if start is not None:
            items.append(charts.TimelineItem("Events", start, label=(event.get("event_type") or "").replace("_", " ")))
    treatments = context.get("treatments") or {}
    for surgery in treatments.get("surgeries", []):
        start = _years(surgery.get("day"))
        if start is not None:
            items.append(charts.TimelineItem("Surgery", start, label=surgery.get("extent_of_tumor_resection") or ""))
    for radiation in treatments.get("radiations", []):
        start = _years((radiation.get("start") or {}).get("day"))
        if start is not None:
            items.append(charts.TimelineItem("Radiation", start, _years((radiation.get("stop") or {}).get("day")),
                                             label=radiation.get("site") or ""))
    for therapy in treatments.get("therapies", []):
        start = _years((therapy.get("start") or {}).get("day"))
        if start is not None:
            label = therapy.get("protocol_name") or ", ".join(therapy.get("chemotherapy_agents") or [])[:40]
            items.append(charts.TimelineItem("Chemotherapy", start, _years((therapy.get("stop") or {}).get("day")),
                                              label=label))
    for session in (context.get("imaging") or {}).get("sessions", []):
        start = _years(session.get("day"))
        if start is not None:
            items.append(charts.TimelineItem("Imaging", start))
    vital_day = _years((patient.get("vital_status_at") or {}).get("day"))
    if vital_day is not None:
        items.append(charts.TimelineItem("Events", vital_day, label=f"vital status: {patient.get('vital_status', '')}"))
    return items


def _patient_timeline(api: RadiantApi, token: str, patient_key: str, tenant: Optional[str]) -> Any:
    context = _get_patient_context(api, token, patient_key, tenant, ["treatments", "imaging"])
    if "error" in context or context.get("clinical_data_available") is False:
        return context
    items = timeline_items(context)
    if not items:
        return {"tenant": context["tenant"], "message": "No dated event, treatment or imaging for this patient."}
    tracks = [t for t in _TRACKS if any(i.track == t for i in items)]
    png = charts.timeline(items, tracks, "Patient history")
    counts = {t: sum(1 for i in items if i.track == t) for t in tracks}
    text = json.dumps({
        "tenant": context["tenant"], "items_per_track": counts,
        "axis": "age in years (days from birth / 365.25)", "warnings": context.get("warnings"),
        "portal_url": context.get("patient", {}).get("portal_url"),
    })
    return _image(text, png)


# -- tools ---------------------------------------------------------------------------

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


async def survival_curve(
    gene: Optional[str] = None,
    data_type: str = "germline_snv",
    vep_impact: Optional[list[str]] = None,
    clinvar: Optional[list[str]] = None,
    max_gnomad_af: Optional[float] = None,
    zygosity: Optional[list[str]] = None,
    cnv_types: Optional[list[str]] = None,
    group_a: Optional[dict[str, Any]] = None,
    group_b: Optional[dict[str, Any]] = None,
    label_a: str = "",
    label_b: str = "",
    tenant: Optional[str] = None,
) -> Any:
    """Kaplan-Meier survival curves of two patient groups, with the log-rank test.

    Two ways to define the groups:
    - by genotype: ``gene`` (with the cohort_carriers filters) compares the
      carriers with the other patients sequenced in the portal;
    - by clinical criteria: ``group_a`` and ``group_b`` are two
      search_patients filter mappings (e.g. {"diagnosis_type_cohort": "..."}).

    Time is survival_days (from initial diagnosis to the vital status date);
    the event is death. Returns the image plus, per group, the patients,
    deaths and median survival, the log-rank p-value, and the patients left
    out (no survival data, not sequenced). State these when reporting.

    Args:
        gene: Gene symbol for the genotype mode.
        data_type, vep_impact, clinvar, max_gnomad_af, zygosity, cnv_types: As in cohort_carriers.
        group_a, group_b: search_patients filters for the clinical mode (both required there).
        label_a, label_b: Optional group names for the legend.
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        if gene and (group_a or group_b):
            raise _sql.InvalidInput("use either `gene` or `group_a`/`group_b`, not both")
        gene_criteria = None
        if gene:
            gene_criteria = build_criteria(data_type, gene, vep_impact, clinvar, max_gnomad_af, zygosity, cnv_types)
        else:
            if not (group_a and group_b):
                raise _sql.InvalidInput("give `gene`, or both `group_a` and `group_b`")
            for filters in (group_a, group_b):
                bad = sorted(set(filters) - set(PATIENT_SEARCH_FILTER_FIELDS))
                if bad:
                    raise _sql.InvalidInput(f"unknown patient filter field(s): {', '.join(bad)}; "
                                            f"allowed: {', '.join(PATIENT_SEARCH_FILTER_FIELDS)}")
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_survival_curve, tenant, gene_criteria, group_a, group_b, (label_a, label_b))


async def oncoprint(
    genes: list[str],
    data_types: Optional[list[str]] = None,
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
        data_types: Among germline_snv (default), germline_cnv, somatic_snv, somatic_cnv.
        max_gnomad_af: SNV only; keeps variants below this gnomAD AF or absent (e.g. 0.01).
        vep_impact: SNV only, e.g. ["HIGH", "MODERATE"].
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        if not genes or len(genes) > MAX_ONCOPRINT_GENES:
            raise _sql.InvalidInput(f"give 1 to {MAX_ONCOPRINT_GENES} genes")
        genes = [_sql.symbol(g) for g in dict.fromkeys(genes)]
        types = _sql.all_of(data_types or ["germline_snv"], ("germline_snv", "germline_cnv", "somatic_snv", "somatic_cnv"),
                            "data_type")
        af = None
        if max_gnomad_af is not None:
            af = _sql.number(max_gnomad_af)
            if not 0 < af <= 1:
                raise _sql.InvalidInput("max_gnomad_af must be in (0, 1]")
        impacts = _sql.all_of(canonical_values("vep_impact", vep_impact or []), ENUMS["vep_impact"], "vep_impact")
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_oncoprint, genes, types, tenant, af, impacts)


async def lollipop(
    gene: str,
    data_type: str = "germline_snv",
    vep_impact: Optional[list[str]] = None,
    clinvar: Optional[list[str]] = None,
    max_gnomad_af: Optional[float] = None,
    zygosity: Optional[list[str]] = None,
    tenant: Optional[str] = None,
) -> Any:
    """Lollipop plot of the carriers' variants along the protein of a gene.

    Each variant is placed at its protein position (from aa_change), its height
    is the number of carrier patients, its colour its consequence class. The
    variants are drawn on the transcript most of them are annotated on, which
    the result names; variants on other transcripts are counted, not drawn.

    Args:
        gene: Gene symbol.
        data_type: germline_snv (default) or somatic_snv.
        vep_impact, clinvar, max_gnomad_af, zygosity: As in cohort_carriers.
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        _sql.one_of(data_type, ("germline_snv", "somatic_snv"), "data_type")
        criteria_ = build_criteria(data_type, gene, vep_impact, clinvar, max_gnomad_af, zygosity, None)
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_lollipop, criteria_, tenant)


async def patient_timeline(patient_key: str, tenant: Optional[str] = None) -> Any:
    """A patient's history on one age axis: events (diagnosis, progression,
    recurrence), surgeries, radiation and chemotherapy periods, imaging
    sessions and the vital status, from the portal patient page.

    Args:
        patient_key: The patient page key (a UUID, from search_patients or get_case_context).
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    from .patients import _UUID

    if not _UUID.match(patient_key or ""):
        return {"error": f"`{patient_key}` is not a patient key (a UUID). Find it with search_patients first."}
    return await _api.run(_patient_timeline, patient_key, tenant)


async def bar_chart(
    title: str,
    categories: list[str],
    values: list[float],
    x_label: str = "",
    y_label: str = "",
    horizontal: bool = False,
) -> Any:
    """Bar chart of a distribution you already computed with another tool
    (e.g. carriers per consequence, patients per diagnosis). Counts or
    proportions only: never patient names or identifiers as categories.

    Args:
        title: Chart title.
        categories: Bar labels (1 to 50).
        values: One number per category.
        x_label, y_label: Axis labels.
        horizontal: Horizontal bars, better for long labels.
    """
    if not categories or len(categories) > MAX_BARS:
        return {"error": f"give 1 to {MAX_BARS} categories"}
    if len(values) != len(categories):
        return {"error": "`values` must have one number per category"}
    try:
        numbers = [_sql.number(v) for v in values]
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    png = charts.bar_chart([str(c)[:60] for c in categories], numbers, title[:120], x_label[:60], y_label[:60],
                           bool(horizontal))
    return _image(json.dumps({"title": title, "values": dict(zip(categories, numbers, strict=True))}), png)

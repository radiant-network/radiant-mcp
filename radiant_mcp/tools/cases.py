"""Case tools: tenant discovery, case search and the full case context."""

import os
from typing import Any, Optional

from ..portal_links import case_url, occurrence_url, patient_url, with_url
from . import _api
from ._api import RadiantApi, clamp, resolve_tenant

# Non-deprecated occurrence data types accepted by
# GET /{tenant}/cases/{case_id}/{seq_id}/tasks_with_occurrences, grouped by
# case type. "somatic_snv" is a deprecated alias of "somatic_snv_tn" and is
# intentionally omitted.
_GERMLINE_DATA_TYPES = ("germline_snv", "germline_cnv")
_SOMATIC_DATA_TYPES = ("somatic_snv_tn", "somatic_snv_to", "somatic_cnv")
_ALL_DATA_TYPES = _GERMLINE_DATA_TYPES + _SOMATIC_DATA_TYPES

# Field aliases accepted as `search_criteria[].field` by POST /{tenant}/cases/search
# (backend/internal/types/case.go: CasesFields entries with CanBeFiltered).
# Kept explicit so the model gets a clear error instead of an opaque API 500.
CASE_SEARCH_FILTER_FIELDS = (
    "case_id", "submitter_case_id", "patient_id", "mrn", "sequencing_experiment_id",
    "case_type_code", "status_code", "resolution_status_code", "priority_code",
    "case_category_code", "analysis_catalog_code", "panel_code", "project_code",
    "primary_condition_id", "primary_condition_name", "prescriber",
    "ordering_organization_code", "diagnosis_lab_code", "organization_code",
    "proband_life_status_code", "created_on", "updated_on",
)
CASE_SEARCH_SORT_FIELDS = (
    "case_id", "proband_id", "submitter_proband_id", "priority_code", "status_code",
    "resolution_status_code", "case_type_code", "case_category_code",
    "analysis_catalog_code", "panel_code", "project_code", "primary_condition_id",
    "primary_condition_name", "prescriber", "ordering_organization_code",
    "diagnosis_lab_code", "created_on", "updated_on",
)
SEARCH_MAX_LIMIT = 100
# Autocomplete match types that are integer ids AND directly filterable fields.
_AUTOCOMPLETE_INT_FIELDS = ("case_id", "patient_id", "sequencing_experiment_id")

# StarRocks database holding the shared (non per-tenant) Radiant tables, in
# particular `staging_sequencing_experiment`, which is the only place that
# currently maps (case_id, seq_id, task_id) -> `part`. The occurrence tables
# (germline/somatic snv/cnv, exomiser, snv__consequence_filter_partitioned)
# are PARTITION BY (part), and snv__variant_partitioned by
# part // _VARIANT_PART_DIVISOR — so every efficient occurrence query needs it.
# TEMPORARY: the Radiant API will eventually return `part` with each task, at
# which point `_lookup_parts` goes away.
_SHARED_DB_ENV = "RADIANT_SHARED_DB"
_SHARED_DB_DEFAULT = "radiant"
# Mirrors `_magic` in radiant-portal-pipeline/radiant/dags/import_part.py
# (compute_part): variant_part = part // 10.
_VARIANT_PART_DIVISOR = 10


def criteria(filters: dict) -> list:
    """``{field: value | [values]}`` -> ``SearchCriterion`` list (default op: in)."""
    from radiant_python.models.search_criterion import SearchCriterion

    out = []
    for field, value in filters.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        out.append(SearchCriterion(field=field, value=list(values)))
    return out


def _lookup_parts(token: str, case_id: int) -> tuple[dict[tuple[int, int], int], Optional[str]]:
    """Map every (seq_id, task_id) of ``case_id`` to its StarRocks ``part``.

    Reads the shared ``staging_sequencing_experiment`` table through the
    JWT-authenticated DB client installed by ``__main__`` (so the query runs
    under the caller's identity, like every other StarRocks access). Returns
    ``(mapping, warning)``; on any failure the mapping is empty and the
    warning explains why, so the case context is still returned.
    """
    try:
        import mcp_server_starrocks.server as sr_server
        client = sr_server.db_client
    except Exception as exc:  # noqa: BLE001
        return {}, f"part lookup skipped: StarRocks client unavailable ({exc})"

    db = os.getenv(_SHARED_DB_ENV, _SHARED_DB_DEFAULT)
    sql = (
        "SELECT seq_id, task_id, part "
        f"FROM `{db}`.staging_sequencing_experiment "
        f"WHERE case_id = {int(case_id)} AND deleted = false"
    )
    try:
        # JWTDBClient: pass the token explicitly (we are on a worker
        # thread). Fall back to plain execute() for any other client.
        if hasattr(client, "execute_with_token"):
            result = client.execute_with_token(token, sql)
        else:
            result = client.execute(sql)
    except Exception as exc:  # noqa: BLE001
        return {}, f"part lookup failed: {type(exc).__name__}: {exc}"

    if not getattr(result, "success", False):
        return {}, f"part lookup failed: {getattr(result, 'error_message', 'unknown error')}"

    cols = result.column_names or []
    try:
        i_seq, i_task, i_part = cols.index("seq_id"), cols.index("task_id"), cols.index("part")
    except ValueError:
        return {}, f"part lookup failed: unexpected columns {cols}"

    mapping: dict[tuple[int, int], int] = {}
    for row in result.rows or []:
        if row[i_part] is None:
            continue
        mapping[(int(row[i_seq]), int(row[i_task]))] = int(row[i_part])
    return mapping, None


def _list_tenants(api: RadiantApi, _token: str) -> dict:
    return {"tenants": _api.list_tenants(api)}


def _get_case_context(api: RadiantApi, token: str, case_id: int, tenant: Optional[str]) -> dict:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err

    case = api.call("cases", "case_entity", tenant=tenant_code, case_id=case_id) or {}
    with_url(case, case_url(tenant_code, case_id))
    for member in case.get("members", []):
        with_url(member, patient_url(tenant_code, member.get("patient_key")))

    case_type = (case.get("case_type") or "").lower()
    if case_type == "germline":
        data_types = _GERMLINE_DATA_TYPES
    elif case_type == "somatic":
        data_types = _SOMATIC_DATA_TYPES
    else:
        data_types = _ALL_DATA_TYPES

    # For every sequencing experiment that has variants, find the task(s)
    # that produced occurrences of each data type. (case_id, seq_id,
    # task_id, data_type) is the key needed by every occurrence endpoint
    # and by the StarRocks occurrence tables.
    occurrence_keys: list[dict] = []
    warnings: list[str] = []
    for seq in case.get("sequencing_experiments", []):
        seq_tasks: list[dict] = []
        if seq.get("has_variants"):
            for data_type in data_types:
                try:
                    tasks = api.call(
                        "cases", "case_tasks_with_occurrences",
                        tenant=tenant_code, case_id=case_id, seq_id=seq["seq_id"], data_type=data_type,
                    )
                except Exception as exc:  # noqa: BLE001 — surface, don't abort
                    warnings.append(
                        f"seq_id={seq['seq_id']} data_type={data_type}: {_api.api_error(exc)['error']}"
                    )
                    continue
                for task in tasks or []:
                    entry = {
                        "case_id": case_id,
                        "seq_id": seq["seq_id"],
                        "task_id": task.get("id"),
                        "data_type": data_type,
                        "task_type_code": task.get("task_type_code"),
                        "task_type_name": task.get("task_type_name"),
                        "pipeline_name": task.get("pipeline_name"),
                        "pipeline_version": task.get("pipeline_version"),
                        "genome_build": task.get("genome_build"),
                        "created_on": task.get("created_on"),
                    }
                    entry = {k: v for k, v in entry.items() if v is not None}
                    with_url(entry, occurrence_url(
                        tenant_code, case_id, seq["seq_id"], data_type, task_id=entry.get("task_id"),
                    ))
                    seq_tasks.append(entry)
                    occurrence_keys.append(entry)
        seq["occurrence_tasks"] = seq_tasks

    # Attach the StarRocks partition of every task. `entry` dicts are shared
    # between `occurrence_keys` and `seq["occurrence_tasks"]`, so mutating
    # them in place updates both views.
    if occurrence_keys:
        parts, part_warning = _lookup_parts(token, case_id)
        if part_warning:
            warnings.append(part_warning)
        for entry in occurrence_keys:
            part = parts.get((entry["seq_id"], entry.get("task_id")))
            if part is None:
                if not part_warning:
                    warnings.append(
                        f"seq_id={entry['seq_id']} task_id={entry.get('task_id')}: "
                        "no `part` found in staging_sequencing_experiment"
                    )
                continue
            entry["part"] = part
            entry["variant_part"] = part // _VARIANT_PART_DIVISOR

    result = {
        "tenant": tenant_code,
        "case": case,
        # Flat list of every (case_id, seq_id, task_id, data_type) tuple —
        # the handle for follow-up variant queries.
        "occurrence_keys": occurrence_keys,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _search_cases(
    api: RadiantApi,
    _token: str,
    tenant: Optional[str],
    filters: Optional[dict],
    query: Optional[str],
    limit: int,
    offset: int,
    sort_by: str,
    sort_order: str,
) -> dict:
    from radiant_python.models.list_body_with_criteria import ListBodyWithCriteria
    from radiant_python.models.sort_body import SortBody

    # -- validate inputs up front (clear errors beat opaque API 500s) --------
    filters = dict(filters or {})
    bad = sorted(set(filters) - set(CASE_SEARCH_FILTER_FIELDS))
    if bad:
        return {
            "error": f"Unknown filter field(s): {', '.join(bad)}",
            "allowed_filter_fields": list(CASE_SEARCH_FILTER_FIELDS),
        }
    if sort_by not in CASE_SEARCH_SORT_FIELDS:
        return {
            "error": f"Unknown sort field: {sort_by}",
            "allowed_sort_fields": list(CASE_SEARCH_SORT_FIELDS),
        }
    sort_order = (sort_order or "desc").lower()
    if sort_order not in ("asc", "desc"):
        return {"error": "sort_order must be 'asc' or 'desc'"}
    limit = clamp(limit, 1, SEARCH_MAX_LIMIT)
    offset = max(0, int(offset))

    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err

    def _search(extra: dict) -> dict:
        body = ListBodyWithCriteria(
            search_criteria=criteria({**filters, **extra}),
            limit=limit,
            offset=offset,
            sort=[SortBody(field=sort_by, order=sort_order)],
        )
        return api.call("cases", "search_cases", tenant=tenant_code, list_body_with_criteria=body) or {}

    def _linked(cases: list[dict]) -> list[dict]:
        return [with_url(c, case_url(tenant_code, c.get("case_id"))) for c in cases]

    if not query:
        resp = _search({})
        cases = resp.get("list", [])
        return {
            "tenant": tenant_code,
            "count": resp.get("count", 0),
            "returned": len(cases),
            "offset": offset,
            "cases": _linked(cases),
        }

    # Free-text lookup: resolve the prefix through the autocomplete
    # endpoint, then run one criteria search per matched identifier type
    # and merge. Criteria are AND-ed by the API, so matches of different
    # types can't share a single request.
    #
    # Autocomplete match types (backend CasesRepository.SearchById):
    #   case_id, patient_id, sequencing_experiment_id (internal integer
    #   ids) and the patient's submitter_patient_id_type (e.g. "mrn")
    #   for submitter patient ids. Anything else in that last group is
    #   still a submitter patient id, so it goes through the `mrn` filter
    #   (alias of patient.submitter_patient_id). Sample identifiers
    #   (submitter_sample_id, aliquot) are NOT covered by the API.
    matches = api.call(
        "cases", "autocomplete_cases", tenant=tenant_code, prefix=query, limit=str(SEARCH_MAX_LIMIT),
    ) or []
    by_field: dict[str, list] = {}
    for m in matches:
        match_type, value = m.get("type"), m.get("value")
        if match_type in _AUTOCOMPLETE_INT_FIELDS:
            # integer columns; autocomplete returns every value as a string
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            by_field.setdefault(match_type, []).append(value)
        else:
            by_field.setdefault("mrn", []).append(value)

    # Merged on case_id: a case matched by two identifier types counts once.
    merged: dict[int, dict] = {}
    truncated = False
    for field, values in by_field.items():
        resp = _search({field: values})
        found = resp.get("list", [])
        truncated = truncated or resp.get("count", 0) > offset + len(found)
        for c in found:
            merged.setdefault(c["case_id"], c)

    result = {
        "tenant": tenant_code,
        "query": query,
        "autocomplete_matches": matches,
        "count": len(merged),
        "returned": min(len(merged), limit),
        "cases": _linked(list(merged.values())[:limit]),
    }
    if truncated or len(merged) > limit:
        result["more_results"] = True
    return result


# -- tools -------------------------------------------------------------------

async def list_tenants() -> dict:
    """List the Radiant tenants the authenticated user belongs to.

    Returns each tenant's ``code`` (the value to pass as ``tenant`` to other
    Radiant tools), its display name and the caller's permissions in it.
    Call this when a tool reports that the tenant is ambiguous.
    """
    return await _api.run(_list_tenants)


async def get_case_context(case_id: int, tenant: Optional[str] = None) -> dict:
    """Retrieve the full clinical context of a Radiant case.

    Use this first whenever a question is about a specific case (e.g. "what is
    the most interesting variant for case 1234?"). It returns, in one call:

    - ``case``: metadata (type germline/somatic, status, category, analysis,
      gene panel, primary condition, diagnosis hypothesis, priority, project,
      ordering organization, lab, prescriber, note), the ``members``
      (patients: relationship to proband, sex, affected status, observed and
      non-observed HPO phenotypes, family history, exams, ethnicities,
      consanguinity), the ``tasks`` and the ``sequencing_experiments``.
      ``case.portal_url`` opens the case in the portal; a member's
      ``portal_url`` opens their patient page (absent when the patient view
      is off or the caller cannot see that patient).
    - Each sequencing experiment carries ``occurrence_tasks``: the analysis
      task(s) that produced variant occurrences, with ``data_type``
      (germline_snv, germline_cnv, somatic_snv_tn, somatic_snv_to,
      somatic_cnv), pipeline and genome build.
    - ``occurrence_keys``: the flat list of (case_id, seq_id, task_id,
      data_type) tuples. These are the keys required by ``prioritize_snv``
      and ``case_cnv`` (and, as a last resort, by SQL on the StarRocks
      occurrence tables). Each key's ``portal_url`` opens that variant table
      in the portal.
    - Each key also carries the StarRocks partitions ``part`` and
      ``variant_part``, needed only for SQL (see ``describe_schema``). A key
      without ``part`` means the partition could not be resolved — see
      ``warnings``.

    Args:
        case_id: Numeric Radiant case ID.
        tenant: Tenant code. Optional — if omitted and the user belongs to a
            single tenant it is auto-selected; if the user belongs to several
            the response lists them and asks for an explicit value.
    """
    return await _api.run(_get_case_context, case_id, tenant)


async def search_cases(
    filters: Optional[dict[str, Any]] = None,
    query: Optional[str] = None,
    tenant: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "updated_on",
    sort_order: str = "desc",
) -> dict:
    """Search Radiant cases by exact-match filters and/or a free-text identifier.

    Use this to find a case ID before calling ``get_case_context``, or to list
    cases matching criteria ("active prenatal cases", "cases for panel RGDI",
    "the case for patient MRN 12345").

    Args:
        filters: Mapping of field → value or list of values. Several fields are
            AND-ed; several values for one field are OR-ed (``in``). Allowed
            fields: case_id, submitter_case_id, patient_id, mrn (submitter
            patient id), sequencing_experiment_id, case_type_code
            (germline/somatic), status_code, resolution_status_code,
            priority_code, case_category_code, analysis_catalog_code,
            panel_code, project_code, primary_condition_id,
            primary_condition_name, prescriber, ordering_organization_code,
            diagnosis_lab_code, organization_code, proband_life_status_code,
            created_on, updated_on.
        query: Free-text prefix matched against case id, internal patient id,
            submitter patient id (MRN) and internal sequencing experiment id
            (seq_id). Resolved via autocomplete, then the matching cases are
            returned. Combine with ``filters`` to narrow. NOT matched: sample
            identifiers (submitter_sample_id, aliquot) — resolve those to a
            seq_id first, then filter on ``sequencing_experiment_id``.
        tenant: Tenant code. Optional — auto-resolved when the user belongs to
            a single tenant; otherwise the response lists the choices.
        limit: Max cases to return (1–100, default 20).
        offset: Pagination offset (default 0).
        sort_by: Sort field (default ``updated_on``). Allowed: case_id,
            proband_id, submitter_proband_id, priority_code, status_code,
            resolution_status_code, case_type_code, case_category_code,
            analysis_catalog_code, panel_code, project_code,
            primary_condition_id, primary_condition_name, prescriber,
            ordering_organization_code, diagnosis_lab_code, created_on,
            updated_on.
        sort_order: ``asc`` or ``desc`` (default ``desc``).

    Returns ``count`` (total matches; with ``query``, the distinct cases found,
    plus ``more_results: true`` when some were not returned), ``cases`` (one summary per case:
    case_id, case_type, status, priority, panel, primary condition, project,
    proband identifiers, has_variants, ``portal_url``, ...) and, for a
    ``query``, the raw ``autocomplete_matches``.
    """
    if not filters and not query:
        return {"error": "Provide at least one of `filters` or `query`."}
    return await _api.run(
        _search_cases, tenant, filters, query, limit, offset, sort_by, sort_order,
    )

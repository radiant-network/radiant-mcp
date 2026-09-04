"""MCP tools backed by the Radiant portal API (``radiant_python`` client).

These tools complement the upstream StarRocks SQL tools with *clinical
context* that lives in the Radiant API (cases, patients, phenotypes, tasks),
not in StarRocks. They follow the same per-user pass-through model as
``JWTDBClient``: the caller's Keycloak JWT is forwarded verbatim as a Bearer
token, so the Radiant API enforces its own per-user / per-organization
authorization. This component holds no Radiant API credential.

Registration is conditional on ``RADIANT_API_URL`` being set — without it the
server boots with the StarRocks tools only (see ``register_tools``).
"""

import os
from typing import Any, Optional

import anyio

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
_SEARCH_MAX_LIMIT = 100
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


# -- helpers -----------------------------------------------------------------

def _get_jwt_token() -> Optional[str]:
    """Raw JWT from the current MCP auth context (same as JWTDBClient)."""
    try:
        from fastmcp.server.dependencies import get_access_token
        access_token = get_access_token()
        if access_token is not None:
            return access_token.token
    except Exception:
        pass
    return None


def _api_client(token: str):
    """Build a ``radiant_python.ApiClient`` that forwards the caller's JWT."""
    import radiant_python

    host = os.environ["RADIANT_API_URL"].rstrip("/")
    configuration = radiant_python.Configuration(host=host, access_token=token)
    return radiant_python.ApiClient(configuration)


def _api_error(exc: Exception) -> dict:
    """Normalize a ``radiant_python`` ApiException (or anything else) into a
    tool-friendly error dict."""
    from radiant_python.exceptions import ApiException

    if isinstance(exc, ApiException):
        err: dict[str, Any] = {
            "error": f"Radiant API error {exc.status}: {exc.reason}",
            "status": exc.status,
        }
        if exc.body:
            body = exc.body.decode() if isinstance(exc.body, bytes) else str(exc.body)
            err["body"] = body[:2000]
        return err
    return {"error": f"{type(exc).__name__}: {exc}"}


def _dump(model) -> Any:
    """pydantic model → plain dict (drop nulls so the payload stays compact)."""
    if model is None:
        return None
    if isinstance(model, list):
        return [_dump(m) for m in model]
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True, by_alias=True)
    return model


def _list_tenants_sync(token: str) -> list[dict]:
    import radiant_python

    with _api_client(token) as api:
        memberships = radiant_python.AuthApi(api).get_me()
    return [_dump(m) for m in memberships]


def _resolve_tenant(token: str, tenant: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
    """Return ``(tenant_code, error_dict)``. Exactly one is non-None.

    If ``tenant`` is given it is used as-is (the API will 403 if the caller
    isn't a member). Otherwise the caller's memberships (GET /auth/me) are
    consulted: a single membership is auto-selected, several is ambiguous.
    """
    if tenant:
        return tenant, None

    memberships = _list_tenants_sync(token)
    codes = [m.get("code") for m in memberships if m.get("code")]
    if len(codes) == 1:
        return codes[0], None
    if not codes:
        return None, {"error": "The authenticated user is not a member of any tenant."}
    return None, {
        "error": "The authenticated user belongs to several tenants; "
                 "pass the `tenant` argument explicitly.",
        "tenants": [{"code": m.get("code"), "name": m.get("name")} for m in memberships],
    }


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
        # JWTDBClient: pass the token explicitly (we may be on a worker
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


def _get_case_context_sync(token: str, case_id: int, tenant: Optional[str]) -> dict:
    import radiant_python

    tenant_code, err = _resolve_tenant(token, tenant)
    if err:
        return err

    with _api_client(token) as api:
        cases_api = radiant_python.CasesApi(api)
        case = _dump(cases_api.case_entity(tenant=tenant_code, case_id=case_id))

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
                        tasks = cases_api.case_tasks_with_occurrences(
                            tenant=tenant_code,
                            case_id=case_id,
                            seq_id=seq["seq_id"],
                            data_type=data_type,
                        )
                    except Exception as exc:  # noqa: BLE001 — surface, don't abort
                        warnings.append(
                            f"seq_id={seq['seq_id']} data_type={data_type}: "
                            f"{_api_error(exc)['error']}"
                        )
                        continue
                    for task in _dump(tasks) or []:
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
            part = parts.get((entry["seq_id"], entry["task_id"]))
            if part is None:
                if not part_warning:
                    warnings.append(
                        f"seq_id={entry['seq_id']} task_id={entry['task_id']}: "
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


def _search_cases_sync(
    token: str,
    tenant: Optional[str],
    filters: Optional[dict],
    query: Optional[str],
    limit: int,
    offset: int,
    sort_by: str,
    sort_order: str,
) -> dict:
    import radiant_python
    from radiant_python.models.list_body_with_criteria import ListBodyWithCriteria
    from radiant_python.models.search_criterion import SearchCriterion
    from radiant_python.models.sort_body import SortBody

    tenant_code, err = _resolve_tenant(token, tenant)
    if err:
        return err

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
    limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
    offset = max(0, int(offset))

    def _criteria(extra: dict) -> list:
        merged = {**filters, **extra}
        out = []
        for field, value in merged.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            out.append(SearchCriterion(field=field, value=list(values)))  # default op: in
        return out

    def _search(cases_api, extra: dict):
        body = ListBodyWithCriteria(
            search_criteria=_criteria(extra),
            limit=limit,
            offset=offset,
            sort=[SortBody(field=sort_by, order=sort_order)],
        )
        return cases_api.search_cases(tenant=tenant_code, list_body_with_criteria=body)

    with _api_client(token) as api:
        cases_api = radiant_python.CasesApi(api)

        if not query:
            resp = _dump(_search(cases_api, {}))
            return {
                "tenant": tenant_code,
                "count": resp.get("count", 0),
                "returned": len(resp.get("list", [])),
                "offset": offset,
                "cases": resp.get("list", []),
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
        matches = _dump(cases_api.autocomplete_cases(
            tenant=tenant_code, prefix=query, limit=str(_SEARCH_MAX_LIMIT)
        )) or []
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

        merged: dict[int, dict] = {}
        total = 0
        for field, values in by_field.items():
            resp = _dump(_search(cases_api, {field: values}))
            total += resp.get("count", 0)
            for c in resp.get("list", []):
                merged.setdefault(c["case_id"], c)

        return {
            "tenant": tenant_code,
            "query": query,
            "autocomplete_matches": matches,
            "count": total,
            "returned": len(merged),
            "cases": list(merged.values())[:limit],
        }


# -- tools -------------------------------------------------------------------

async def list_tenants() -> dict:
    """List the Radiant tenants the authenticated user belongs to.

    Returns each tenant's ``code`` (the value to pass as ``tenant`` to other
    Radiant tools), its display name and the caller's permissions in it.
    Call this when a tool reports that the tenant is ambiguous.
    """
    token = _get_jwt_token()
    if not token:
        return {"error": "Authentication required: no JWT in request context"}
    try:
        tenants = await anyio.to_thread.run_sync(_list_tenants_sync, token)
    except Exception as exc:  # noqa: BLE001
        return _api_error(exc)
    return {"tenants": tenants}


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
    - Each sequencing experiment carries ``occurrence_tasks``: the analysis
      task(s) that produced variant occurrences, with ``data_type``
      (germline_snv, germline_cnv, somatic_snv_tn, somatic_snv_to,
      somatic_cnv), pipeline and genome build.
    - ``occurrence_keys``: the flat list of (case_id, seq_id, task_id,
      data_type) tuples. These are the keys required to query variant
      occurrences for this case, both through the Radiant occurrence
      endpoints and through the StarRocks occurrence tables (filter on
      ``case_id``, ``seq_id``, ``task_id``).
    - Each key also carries the StarRocks partitions: ``part`` (the
      occurrence tables — germline/somatic snv/cnv occurrence, exomiser —
      and the shared ``snv__consequence_filter_partitioned`` are
      ``PARTITION BY (part)``; always add ``AND part = <part>``), and
      ``variant_part`` (the partition of ``<tenant>_tenant.snv__variant_partitioned``;
      join it with ``v.locus_id = o.locus_id AND v.part = <variant_part>``).
      A key without ``part`` means the partition could not be resolved —
      see ``warnings``.

    Args:
        case_id: Numeric Radiant case ID.
        tenant: Tenant code. Optional — if omitted and the user belongs to a
            single tenant it is auto-selected; if the user belongs to several
            the response lists them and asks for an explicit value.
    """
    token = _get_jwt_token()
    if not token:
        return {"error": "Authentication required: no JWT in request context"}
    try:
        return await anyio.to_thread.run_sync(
            _get_case_context_sync, token, case_id, tenant
        )
    except Exception as exc:  # noqa: BLE001
        return _api_error(exc)


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

    Returns ``count`` (total matches), ``cases`` (one summary per case:
    case_id, case_type, status, priority, panel, primary condition, project,
    proband identifiers, has_variants, ...) and, for a ``query``, the raw
    ``autocomplete_matches``.
    """
    token = _get_jwt_token()
    if not token:
        return {"error": "Authentication required: no JWT in request context"}
    if not filters and not query:
        return {"error": "Provide at least one of `filters` or `query`."}
    try:
        return await anyio.to_thread.run_sync(
            _search_cases_sync, token, tenant, filters, query,
            limit, offset, sort_by, sort_order,
        )
    except Exception as exc:  # noqa: BLE001
        return _api_error(exc)


def register_tools(mcp) -> bool:
    """Register the Radiant API tools on the FastMCP instance.

    Returns ``False`` (and registers nothing) when ``RADIANT_API_URL`` is
    unset, so the server can still run as a pure StarRocks MCP server.
    """
    if not os.getenv("RADIANT_API_URL"):
        return False
    # Fail fast at boot if the client isn't installed rather than on first call.
    import radiant_python  # noqa: F401

    mcp.tool(list_tenants)
    mcp.tool(search_cases)
    mcp.tool(get_case_context)
    return True

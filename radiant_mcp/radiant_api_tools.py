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
    mcp.tool(get_case_context)
    return True

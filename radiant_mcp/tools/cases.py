"""Case tools: tenant discovery, case search and the full case context."""

from typing import Any

from .. import portal_api as _api
from ..api_fields import CASE_FILTER_FIELDS as CASE_SEARCH_FILTER_FIELDS
from ..api_fields import CASE_SORT_FIELDS as CASE_SEARCH_SORT_FIELDS
from ..portal_api import SEARCH_MAX_LIMIT, RadiantApi, clamp, resolve_tenant
from ..portal_links import case_url, with_url
from ..services.cases import case_context, merge_criteria, search_criteria

# Autocomplete match types that are integer ids AND directly filterable fields.
_AUTOCOMPLETE_INT_FIELDS = ("case_id", "patient_id", "sequencing_experiment_id")
# Autocomplete match types that are filterable fields with string values.
_AUTOCOMPLETE_TEXT_FIELDS = ("proband_submitter_sample_ids",)



def _list_tenants(api: RadiantApi, _token: str) -> dict:
    return {"tenants": _api.list_tenants(api)}


def _search_cases(
    api: RadiantApi,
    _token: str,
    tenant: str | None,
    filters: dict | None,
    query: str | None,
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
        merged_filters = merge_criteria(filters, extra)
        if merged_filters is None:
            return {"count": 0, "list": []}
        body = ListBodyWithCriteria(
            search_criteria=search_criteria(merged_filters),
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
    #   ids), proband_submitter_sample_ids (the proband's sample ids) and
    #   the patient's submitter_patient_id_type (e.g. "mrn") for submitter
    #   patient ids. Anything else is still a submitter patient id, so it
    #   goes through the `mrn` filter (alias of patient.submitter_patient_id).
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
        elif match_type in _AUTOCOMPLETE_TEXT_FIELDS:
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


async def get_case_context(case_id: int, tenant: str | None = None) -> dict:
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
    return await _api.run(case_context, case_id, tenant)


async def search_cases(
    filters: dict[str, Any] | None = None,
    query: str | None = None,
    tenant: str | None = None,
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
            patient id), submitter_patient_id, proband_submitter_sample_ids,
            sequencing_experiment_id, case_type_code (germline/somatic),
            status_code, priority_code, case_category_code,
            analysis_catalog_code, panel_code, project_code, project_name,
            case_group, primary_condition_id, primary_condition_name,
            prescriber, ordering_organization_code, diagnosis_lab_code,
            organization_code, proband_life_status_code, created_on, updated_on.
        query: Free-text prefix matched against case id, internal patient id,
            submitter patient id (MRN), internal sequencing experiment id
            (seq_id) and the proband's sample ids. Resolved via autocomplete,
            then the matching cases are returned. Combine with ``filters`` to
            narrow: a filter on the same field as a match keeps the values
            both allow.
        tenant: Tenant code. Optional — auto-resolved when the user belongs to
            a single tenant; otherwise the response lists the choices.
        limit: Max cases to return (1–100, default 20).
        offset: Pagination offset (default 0).
        sort_by: Sort field (default ``updated_on``). Allowed: case_id,
            proband_id, submitter_proband_id, proband_first_name,
            proband_last_name, proband_jhn, proband_life_status_code,
            priority_code, status_code, case_type_code, case_category_code,
            analysis_catalog_code, panel_code, panel_name, project_code,
            project_name, primary_condition_id, primary_condition_name,
            prescriber, ordering_organization_code, organization_code,
            diagnosis_lab_code, created_on, updated_on.
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

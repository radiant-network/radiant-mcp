"""Patient tools: the clinical data of the portal's patient page.

They read the portal's patient endpoints (``/{tenant}/patients/...``), which
the portal serves only when its patient view is enabled and which read the
secured ``v_pcx_30_*`` views of the tenant in StarRocks. Those views apply
the PHI rule per user, so a tool never sees more than the portal page would
show the same user.
"""

from typing import Any, Optional

from .. import portal_api as _api
from ..portal_api import SEARCH_MAX_LIMIT, RadiantApi, clamp, resolve_tenant
from ..portal_links import patient_url, with_url
from ..services.cases import search_criteria
from ..services.patients import (
    PATIENT_KEY,
    PATIENT_SEARCH_FILTER_FIELDS,
    PATIENT_SECTIONS,
    no_clinical_data,
    patient_context,
    unknown_filter_fields,
)

PATIENT_SEARCH_SORT_FIELDS = (
    "birth_year", "cns_integrated_diagnosis", "diagnosis_type_cohort", "gender",
    "organization_code", "patient_id", "patient_key", "survival_days", "vital_status",
)
# Autocomplete match types of GET /{tenant}/patients/autocomplete, which are
# also filter fields.
_AUTOCOMPLETE_FIELDS = ("patient_id", "patient_name")

DEFAULT_SECTIONS = ("treatments", "laboratory", "imaging", "genomics", "trial_eligibilities")


def _search_patients(
    api: RadiantApi,
    _token: str,
    tenant: Optional[str],
    filters: Optional[dict],
    query: Optional[str],
    limit: int,
    offset: int,
    sort_by: Optional[str],
    sort_order: str,
    include_filter_values: bool,
) -> dict:
    from radiant_python.models.list_body_with_criteria import ListBodyWithCriteria
    from radiant_python.models.sort_body import SortBody

    filters = dict(filters or {})
    bad = unknown_filter_fields(filters)
    if bad:
        return {
            "error": f"Unknown filter field(s): {', '.join(bad)}",
            "allowed_filter_fields": list(PATIENT_SEARCH_FILTER_FIELDS),
        }
    if sort_by is not None and sort_by not in PATIENT_SEARCH_SORT_FIELDS:
        return {
            "error": f"Unknown sort field: {sort_by}",
            "allowed_sort_fields": list(PATIENT_SEARCH_SORT_FIELDS),
        }
    sort_order = (sort_order or "asc").lower()
    if sort_order not in ("asc", "desc"):
        return {"error": "sort_order must be 'asc' or 'desc'"}
    limit = clamp(limit, 1, SEARCH_MAX_LIMIT)
    offset = max(0, int(offset))

    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err

    def _search(extra: dict, page_offset: int) -> dict:
        body = ListBodyWithCriteria(
            search_criteria=search_criteria({**filters, **extra}),
            limit=limit,
            offset=page_offset,
            sort=[SortBody(field=sort_by, order=sort_order)] if sort_by else None,
        )
        return api.call("patients", "search_patients", tenant=tenant_code, list_body_with_criteria=body) or {}

    try:
        if query:
            # One search per matched identifier type (criteria are AND-ed by
            # the API), merged on patient_key. `offset` does not apply.
            matches = api.call(
                "patients", "autocomplete_patients",
                tenant=tenant_code, prefix=query, limit=str(SEARCH_MAX_LIMIT),
            ) or []
            by_field: dict[str, list] = {}
            for m in matches:
                if m.get("type") in _AUTOCOMPLETE_FIELDS:
                    by_field.setdefault(m["type"], []).append(m.get("value"))
            merged: dict[str, dict] = {}
            truncated = False
            for field, values in by_field.items():
                resp = _search({field: values}, 0)
                found = resp.get("list", [])
                truncated = truncated or resp.get("count", 0) > len(found)
                for p in found:
                    merged.setdefault(p["patient_key"], p)
            patients = list(merged.values())[:limit]
            result: dict[str, Any] = {"tenant": tenant_code, "query": query, "count": len(merged)}
            if truncated or len(merged) > limit:
                result["more_results"] = True
        else:
            resp = _search({}, offset)
            patients = resp.get("list", [])
            result = {"tenant": tenant_code, "count": resp.get("count", 0), "offset": offset}
    except Exception as exc:  # noqa: BLE001
        no_data = no_clinical_data(tenant_code, exc)
        if no_data is None:
            raise
        return no_data

    result["returned"] = len(patients)
    result["patients"] = [with_url(p, patient_url(tenant_code, p.get("patient_key"))) for p in patients]
    if include_filter_values:
        try:
            result["filter_values"] = api.call("patients", "patients_filters", tenant=tenant_code)
        except Exception as exc:  # noqa: BLE001 — the search itself succeeded
            result["warnings"] = [f"filter_values: {_api.api_error(exc)['error']}"]
    return result


# -- tools -------------------------------------------------------------------

async def search_patients(
    query: Optional[str] = None,
    filters: Optional[dict[str, Any]] = None,
    tenant: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
    include_filter_values: bool = False,
) -> dict:
    """Search the patients of the portal's patient list (clinical data).

    Use this to find a patient's ``patient_key`` before calling
    ``get_patient_context``, or to list the patients of a clinical cohort
    ("deceased patients with a medulloblastoma", "patients who received
    radiation").

    Args:
        query: Free-text prefix matched against the patient identifier (MRN or
            research id) and the patient name, through the portal's
            autocomplete. Combine with ``filters`` to narrow.
        filters: Mapping of field → value or list of values. Several fields are
            AND-ed; several values for one field are OR-ed. Allowed fields:
            age_at_initial_dx_group, chemotherapy_agents,
            cns_integrated_diagnosis, diagnosis_type_cohort,
            got_radiation_ever, organization_code, patient_id,
            patient_name, vital_status. Values are matched exactly: call once
            with ``include_filter_values=True`` to get the accepted values.
        tenant: Tenant code. Optional — auto-resolved when the user belongs to
            a single tenant; otherwise the response lists the choices.
        limit: Max patients to return (1–100, default 20).
        offset: Pagination offset (default 0, ignored with ``query``).
            With ``query``, ``count`` is the number of distinct patients found
            and ``more_results`` is true when some were not returned.
        sort_by: Optional sort field: birth_year, cns_integrated_diagnosis,
            diagnosis_type_cohort, gender, organization_code, patient_id,
            patient_key, survival_days, vital_status. Default: the portal's
            list order.
        sort_order: ``asc`` (default) or ``desc``.
        include_filter_values: Also return ``filter_values``, the values each
            filter accepts.

    Returns ``count`` and ``patients`` (identifiers, demographics, vital
    status, initial diagnosis, survival, ``case_count``, ``patient_key`` and
    ``portal_url``). Names are placeholders when the user may not read PHI
    (``can_read_phi``). A tenant without clinical data returns
    ``clinical_data_available: false``.
    """
    return await _api.run(
        _search_patients, tenant, filters, query, limit, offset, sort_by, sort_order, include_filter_values,
    )


async def get_patient_context(
    patient_key: str,
    tenant: Optional[str] = None,
    sections: Optional[list[str]] = None,
) -> dict:
    """Retrieve the clinical context of a patient, as on the portal's patient page.

    Use this first for any question about one patient's clinical history. The
    key comes from ``search_patients`` or from a case member's ``patient_key``
    (``get_case_context``); an MRN is not a key — search it first.

    Returns ``patient`` (identifiers, demographics, vital status, events with
    the diagnoses, initial diagnosis, tumor and metastasis, treatment summary,
    key dates, ``portal_url``) and one entry per requested section:
    ``treatments`` (surgeries, radiation, medical therapies), ``laboratory``
    (latest blood count, CSF results, Lansky/Karnofsky performance status),
    ``imaging`` (MRI sessions), ``genomics`` (the patient's cases and their
    variant interpretations, each with a ``portal_url``),
    ``trial_eligibilities`` and, on request, ``external_records``. Each
    section carries the ``portal_url`` of its tab. ``case_ids`` lists the
    patient's cases for ``get_case_context``. A section that fails is listed
    in ``warnings``; the rest is still returned. A tenant without clinical
    data returns ``clinical_data_available: false``.

    Args:
        patient_key: The patient page key (a UUID).
        tenant: Tenant code. Optional — auto-resolved when the user belongs to
            a single tenant; otherwise the response lists the choices.
        sections: Sections to load among treatments, laboratory, imaging,
            genomics, trial_eligibilities, external_records. Default: all but
            external_records.
    """
    if not PATIENT_KEY.match(patient_key or ""):
        return {
            "error": f"`{patient_key}` is not a patient key (a UUID). "
                     "Find the key with search_patients(query=<identifier>) first.",
        }
    chosen = list(dict.fromkeys(sections)) if sections else list(DEFAULT_SECTIONS)
    unknown = sorted(set(chosen) - set(PATIENT_SECTIONS))
    if unknown:
        return {
            "error": f"Unknown section(s): {', '.join(unknown)}",
            "allowed_sections": list(PATIENT_SECTIONS),
        }
    return await _api.run(patient_context, patient_key, tenant, chosen)

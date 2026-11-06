"""Patient tools: the clinical data of the portal's patient page.

They read the portal's patient endpoints (``/{tenant}/patients/...``), which
the portal serves only when its patient view is enabled and which read the
secured ``v_pcx_30_*`` views of the tenant in StarRocks. Those views apply
the PHI rule per user, so a tool never sees more than the portal page would
show the same user.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ..portal_links import case_url, occurrence_url, patient_url, variant_url, with_url
from . import _api
from ._api import RadiantApi, clamp, is_status, resolve_tenant
from .cases import SEARCH_MAX_LIMIT, criteria

# Field aliases accepted by POST /{tenant}/patients/search
# (backend/internal/types/pcx_patient_query.go: PcxPatientListFields).
PATIENT_SEARCH_FILTER_FIELDS = (
    "age_at_initial_dx_group", "chemotherapy_agents", "cns_integrated_diagnosis",
    "diagnosis_type_cohort", "got_radiation_ever", "organization_code", "patient_id",
    "patient_key", "patient_name", "vital_status",
)
PATIENT_SEARCH_SORT_FIELDS = (
    "birth_year", "cns_integrated_diagnosis", "diagnosis_type_cohort", "gender",
    "organization_code", "patient_id", "patient_key", "survival_days", "vital_status",
)
# Autocomplete match types of GET /{tenant}/patients/autocomplete, which are
# also filter fields.
_AUTOCOMPLETE_FIELDS = ("patient_id", "patient_name")

# Section name -> (patients API operation, patient page tab).
PATIENT_SECTIONS = {
    "treatments": ("patient_treatments", "treatments"),
    "laboratory": ("patient_laboratory", "laboratory"),
    "imaging": ("patient_imaging", "imaging"),
    "genomics": ("patient_genomics", "genomics"),
    "trial_eligibilities": ("patient_trial_eligibilities", "clinical-trials"),
    "external_records": ("patient_external_records", None),
}
DEFAULT_SECTIONS = ("treatments", "laboratory", "imaging", "genomics", "trial_eligibilities")
# The backend gives each user a small StarRocks pool; stay under it.
_SECTION_WORKERS = 3

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _route_missing(exc: Exception) -> bool:
    """A 404 without the API's JSON error body: the patient routes are not
    registered, i.e. the portal runs with its patient view disabled."""
    if not is_status(exc, 404):
        return False
    try:
        body = json.loads(exc.body or "")
    except (TypeError, ValueError):
        return True
    return not (isinstance(body, dict) and "message" in body)


def _no_clinical_data(tenant: str, exc: Exception) -> Optional[dict]:
    """The result for a tenant whose clinical data cannot be read, or None
    when ``exc`` is an ordinary error."""
    if _route_missing(exc):
        reason = "the portal's patient view is not enabled"
    elif is_status(exc, 500, 502, 503, 504):
        reason = ("the tenant's patient views could not be read (they may not exist for this tenant, "
                  "or the portal had a transient error: retry once before concluding)")
    else:
        return None
    result = {
        "tenant": tenant,
        "clinical_data_available": False,
        "message": f"No clinical data for tenant {tenant}: {reason}.",
    }
    correlation_id = (getattr(exc, "headers", None) or {}).get("X-Correlation-ID")
    if correlation_id:
        result["correlation_id"] = correlation_id
    return result


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
    bad = sorted(set(filters) - set(PATIENT_SEARCH_FILTER_FIELDS))
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
            search_criteria=criteria({**filters, **extra}),
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
        no_data = _no_clinical_data(tenant_code, exc)
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


def _link_genomics(tenant: str, genomics: dict) -> None:
    for case in genomics.get("cases", []):
        with_url(case, case_url(tenant, case.get("case_id")))
    for interpretation in genomics.get("interpretations", []):
        locus_id = interpretation.get("locus_id")
        if interpretation.get("origin") == "germline":
            url = occurrence_url(
                tenant, interpretation.get("case_id"), interpretation.get("sequencing_id"),
                "germline_snv", variant_id=locus_id,
            )
        else:
            # The somatic interpretation does not say whether it came from the
            # tumor-normal or the tumor-only table: open the case variants tab.
            url = case_url(tenant, interpretation.get("case_id"), tab="variants")
        with_url(interpretation, url)
        with_url(interpretation, variant_url(tenant, locus_id), key="variant_portal_url")


def _get_patient_context(
    api: RadiantApi, _token: str, patient_key: str, tenant: Optional[str], sections: list[str],
) -> dict:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err

    try:
        patient = api.call("patients", "patient_entity", tenant=tenant_code, patient_key=patient_key) or {}
    except Exception as exc:  # noqa: BLE001
        no_data = _no_clinical_data(tenant_code, exc)
        if no_data is not None:
            return no_data
        if is_status(exc, 404):
            return {
                "error": f"Patient {patient_key} not found in tenant {tenant_code}, "
                         "or not visible to the current user.",
                "status": 404,
            }
        raise
    with_url(patient, patient_url(tenant_code, patient_key))

    def _fetch(section: str) -> tuple[str, Any, Optional[str]]:
        operation, _tab = PATIENT_SECTIONS[section]
        try:
            return section, api.call("patients", operation, tenant=tenant_code, patient_key=patient_key), None
        except Exception as exc:  # noqa: BLE001 — one section never fails the whole context
            return section, None, f"{section}: {_api.api_error(exc)['error']}"

    result: dict[str, Any] = {"tenant": tenant_code, "patient": patient}
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=_SECTION_WORKERS) as pool:
        for section, data, warning in pool.map(_fetch, sections):
            if warning:
                warnings.append(warning)
                continue
            data = data if data is not None else {}
            if section == "genomics" and isinstance(data, dict):
                _link_genomics(tenant_code, data)
            tab = PATIENT_SECTIONS[section][1]
            if tab and isinstance(data, dict):
                with_url(data, patient_url(tenant_code, patient_key, tab=tab))
            result[section] = data

    case_ids = [c.get("case_id") for c in (result.get("genomics") or {}).get("cases", [])]
    result["case_ids"] = [c for c in case_ids if c is not None]
    if warnings:
        result["warnings"] = warnings
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
            got_radiation_ever, organization_code, patient_id, patient_key,
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
    if not _UUID.match(patient_key or ""):
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
    return await _api.run(_get_patient_context, patient_key, tenant, chosen)

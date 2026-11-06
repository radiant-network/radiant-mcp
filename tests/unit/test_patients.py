import pytest
from conftest import PORTAL, api_exception, serialized

from radiant_mcp.tools.patients import get_patient_context, search_patients

KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"


def _patient(key):
    return {"patient_key": key, "patient_id": "MRN1", "can_read_phi": True}


def test_search_by_identifier_merges_autocomplete_matches(fake_api, run):
    fake_api.on("patients", "autocomplete_patients", [
        {"type": "patient_id", "value": "MRN1"}, {"type": "patient_name", "value": "Doe"},
    ])
    # The same patient matches by identifier and by name: it is returned once.
    fake_api.on("patients", "search_patients", {"count": 1, "list": [_patient(KEY)]})
    result = run(search_patients(query="MRN", tenant="radiant"))
    assert result["count"] == 1 and result["returned"] == 1 and "more_results" not in result
    assert result["patients"][0]["portal_url"] == f"{PORTAL}/radiant/patient/entity/{KEY}"
    fields = sorted(kw["list_body_with_criteria"].search_criteria[0].var_field
                    for api, op, kw in fake_api.calls if op == "search_patients")
    assert fields == ["patient_id", "patient_name"]


def test_search_by_filters_with_filter_values(fake_api, run):
    fake_api.on("patients", "search_patients", {"count": 3, "list": [_patient(KEY)]})
    fake_api.on("patients", "patients_filters", {"vital_status": [{"key": "Deceased"}]})
    result = run(search_patients(
        filters={"vital_status": "Deceased"}, tenant="radiant", sort_by="survival_days", include_filter_values=True,
    ))
    body = serialized(fake_api.kwargs("search_patients")["list_body_with_criteria"])
    assert body["search_criteria"] == [{"field": "vital_status", "value": ["Deceased"]}]
    assert body["sort"] == [{"field": "survival_days", "order": "asc"}]
    assert result["count"] == 3 and result["filter_values"]["vital_status"][0]["key"] == "Deceased"


@pytest.mark.parametrize("kwargs, message", [
    ({"filters": {"mrn": "1"}}, "Unknown filter"),
    ({"sort_by": "name"}, "Unknown sort"),
    ({"sort_order": "up"}, "sort_order"),
])
def test_search_validates_inputs_before_calling(fake_api, run, kwargs, message):
    assert message in run(search_patients(tenant="radiant", **kwargs))["error"]
    assert fake_api.calls == []


def test_failing_filter_values_keep_the_search_results(fake_api, run):
    fake_api.on("patients", "search_patients", {"count": 1, "list": [_patient(KEY)]})
    fake_api.on("patients", "patients_filters", api_exception(500))
    result = run(search_patients(filters={"vital_status": "Alive"}, tenant="radiant", include_filter_values=True))
    assert result["returned"] == 1 and "filter_values" not in result
    assert result["warnings"] == ["filter_values: Radiant API error 500: error"]


def test_patient_view_disabled_means_no_clinical_data(fake_api, run):
    fake_api.on("patients", "search_patients", api_exception(404, "404 page not found"))
    result = run(search_patients(filters={"vital_status": "Alive"}, tenant="pragmatiq"))
    assert result["clinical_data_available"] is False
    assert "pragmatiq" in result["message"] and "not enabled" in result["message"]


def test_context_rejects_an_identifier_that_is_not_a_key(fake_api, run):
    assert "search_patients" in run(get_patient_context("GM221891", tenant="radiant"))["error"]
    assert fake_api.calls == []


def test_context_rejects_unknown_sections(fake_api, run):
    assert "Unknown section" in run(get_patient_context(KEY, tenant="radiant", sections=["notes"]))["error"]


def test_context_loads_the_sections_and_links_them(fake_api, run):
    fake_api.on("patients", "patient_entity", _patient(KEY))
    fake_api.on("patients", "patient_treatments", {"surgeries": []})
    fake_api.on("patients", "patient_laboratory", api_exception(500, '{"message":"Internal Server Error"}'))
    fake_api.on("patients", "patient_genomics", {
        "cases": [{"case_id": 8}],
        "interpretations": [
            {"case_id": 8, "sequencing_id": 21, "locus_id": "42", "origin": "germline"},
            {"case_id": 9, "sequencing_id": 22, "locus_id": "43", "origin": "somatic"},
        ],
    })
    result = run(get_patient_context(KEY, tenant="radiant", sections=["treatments", "laboratory", "genomics"]))

    assert result["patient"]["portal_url"] == f"{PORTAL}/radiant/patient/entity/{KEY}"
    assert result["treatments"]["portal_url"].endswith("?tab=treatments")
    assert "laboratory" not in result and result["warnings"] == ["laboratory: Radiant API error 500: error"]
    genomics = result["genomics"]
    assert genomics["portal_url"].endswith("?tab=genomics")
    assert genomics["cases"][0]["portal_url"] == f"{PORTAL}/radiant/case/entity/8"
    germline, somatic = genomics["interpretations"]
    assert germline["portal_url"].endswith("seq_id=21&variant_section=snv&selectedVariant=42")
    assert somatic["portal_url"] == f"{PORTAL}/radiant/case/entity/9?tab=variants"
    assert somatic["variant_portal_url"] == f"{PORTAL}/radiant/variants/entity/43"
    assert result["case_ids"] == [8]


def test_context_for_an_unknown_patient(fake_api, run):
    fake_api.on("patients", "patient_entity", api_exception(404, '{"status":404,"message":"patient not found"}'))
    result = run(get_patient_context(OTHER, tenant="radiant"))
    assert result["status"] == 404 and "not found" in result["error"]


def test_context_for_a_tenant_without_patient_views(fake_api, run):
    fake_api.on("patients", "patient_entity", api_exception(
        500, '{"status":500,"message":"Internal Server Error"}', {"X-Correlation-ID": "abc"},
    ))
    result = run(get_patient_context(KEY, tenant="pragmatiq"))
    assert result == {
        "tenant": "pragmatiq",
        "clinical_data_available": False,
        "message": "No clinical data for tenant pragmatiq: the tenant's patient views could not be read "
                   "(they may not exist for this tenant, or the portal had a transient error: "
                   "retry once before concluding).",
        "correlation_id": "abc",
    }


def test_tenant_is_resolved_from_memberships(fake_api, run):
    fake_api.on("auth", "get_me", [{"code": "a", "name": "A"}, {"code": "b", "name": "B"}])
    result = run(get_patient_context(KEY))
    assert "several tenants" in result["error"] and [t["code"] for t in result["tenants"]] == ["a", "b"]

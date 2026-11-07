from conftest import PORTAL

from radiant_mcp.tools import cases
from radiant_mcp.tools.cases import get_case_context, search_cases


def test_search_cases_links_each_case(fake_api, run):
    fake_api.on("cases", "search_cases", {"count": 1, "list": [{"case_id": 1131}]})
    result = run(search_cases(filters={"status_code": "active"}, tenant="pragmatiq"))
    assert result["cases"][0]["portal_url"] == f"{PORTAL}/pragmatiq/case/entity/1131"


def test_search_cases_by_identifier_caps_the_merged_list(fake_api, run):
    fake_api.on("cases", "autocomplete_cases", [{"type": "mrn", "value": "GM1"}, {"type": "case_id", "value": "7"}])
    fake_api.on("cases", "search_cases", lambda list_body_with_criteria, **_: {
        "count": 2, "list": [{"case_id": 7}, {"case_id": 8}],
    })
    result = run(search_cases(query="GM1", tenant="t", limit=1))
    assert result["returned"] == 1 and [c["case_id"] for c in result["cases"]] == [7]
    assert result["count"] == 2 and result["more_results"] is True


def test_search_cases_validates_before_calling(fake_api, run):
    assert "Unknown filter" in run(search_cases(filters={"foo": 1}, tenant="t"))["error"]
    assert fake_api.calls == []


def test_case_context_links_the_case_members_and_keys(fake_api, run, monkeypatch):
    monkeypatch.setattr(cases, "_lookup_parts", lambda token, case_id: ({(21, 3): 154}, None))
    fake_api.on("cases", "case_entity", {
        "case_id": 8,
        "case_type": "germline",
        "members": [{"patient_id": 1, "patient_key": "k-1"}, {"patient_id": 2}],
        "sequencing_experiments": [{"seq_id": 21, "has_variants": True}],
    })
    fake_api.on("cases", "case_tasks_with_occurrences",
                lambda data_type, **_: [{"id": 3}] if data_type == "germline_snv" else [])

    result = run(get_case_context(8, tenant="radiant"))

    case = result["case"]
    assert case["portal_url"] == f"{PORTAL}/radiant/case/entity/8"
    assert case["members"][0]["portal_url"] == f"{PORTAL}/radiant/patient/entity/k-1"
    assert "portal_url" not in case["members"][1]
    [key] = result["occurrence_keys"]
    assert key["part"] == 154 and key["variant_part"] == 15
    assert key["portal_url"] == f"{PORTAL}/radiant/case/entity/8?tab=variants&seq_id=21&task_id=3&variant_section=snv"
    assert "warnings" not in result


def test_part_lookup_binds_the_case_and_skips_tasks_without_part(fake_sql):
    from radiant_mcp.tools import _sql

    fake_sql.on("staging_sequencing_experiment", [
        {"seq_id": 21, "task_id": 3, "part": 154}, {"seq_id": 22, "task_id": 4, "part": None},
    ])
    assert cases._lookup_parts("jwt", 1131) == ({(21, 3): 154}, None)
    ((sql, values),) = fake_sql.queries
    assert "radiant.staging_sequencing_experiment" in sql and values == {"p0": 1131}
    fake_sql.on("staging_sequencing_experiment", _sql.SqlError("Access denied"))
    fake_sql.rules.reverse()
    assert cases._lookup_parts("jwt", 1131) == ({}, "part lookup failed: Access denied")

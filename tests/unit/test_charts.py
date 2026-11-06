"""RAD-254 chart tools: rendering, data selection and tool contracts."""

import asyncio
import json
import re

import pytest
from conftest import api_exception, bound_value
from fastmcp import Client, FastMCP
from fastmcp.utilities.types import Image

from radiant_mcp import charts
from radiant_mcp.tools import cases
from radiant_mcp.tools import charts as chart_tools
from radiant_mcp.tools.charts import (
    bar_chart, consequence_class, family_venn, lollipop, oncoprint, patient_timeline, survival_curve,
)

PNG = b"\x89PNG\r\n\x1a\n"
KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"

# Freireich et al. 1963 remission data (weeks), the classic Kaplan-Meier / log-rank example.
DRUG_T = [6, 6, 6, 6, 7, 9, 10, 10, 11, 13, 16, 17, 19, 20, 22, 23, 25, 32, 32, 34, 35]
DRUG_E = [1, 1, 1, 0, 1, 0, 1, 0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0]
PLACEBO_T = [1, 1, 2, 2, 3, 4, 4, 5, 5, 8, 8, 8, 8, 11, 11, 12, 12, 15, 17, 22, 23]


def _parts(result) -> tuple[dict, bytes]:
    text, image = result
    assert isinstance(image, Image)
    return json.loads(text), image.data


# -- rendering -----------------------------------------------------------------------

def test_kaplan_meier_and_logrank_match_the_published_example():
    png, stats = charts.survival([
        charts.SurvivalGroup("6-MP", DRUG_T, [bool(e) for e in DRUG_E]),
        charts.SurvivalGroup("Placebo", PLACEBO_T, [True] * 21),
    ], "Freireich")
    assert png.startswith(PNG) and len(png) < charts.MAX_PNG_BYTES
    medians = {g["group"]: g["median_survival_years"] for g in stats.groups}
    assert medians == {"6-MP": 23.0, "Placebo": 8.0}
    assert stats.logrank_p == pytest.approx(4.17e-5, rel=0.01)  # chi-square 16.79, 1 df
    assert [g["deaths"] for g in stats.groups] == [9, 21]


def test_survival_without_deaths_draws_a_flat_curve_and_no_test():
    _, stats = charts.survival([charts.SurvivalGroup("A", [1.0, 2.0], [False, False]),
                                charts.SurvivalGroup("B", [1.5], [True])], "t")
    assert stats.logrank_p is None and stats.groups[0]["median_survival_years"] is None


@pytest.mark.parametrize("draw", [
    lambda: charts.venn(["A", "B", "C"], {(1, 1, 1): 3}, "t"),
    lambda: charts.venn(["A", "B"], {(1, 0): 1}, "t"),
    lambda: charts.oncoprint(["G"], ["case 1"], {("G", "case 1"): "missense"}, "t"),
    lambda: charts.lollipop([charts.Lollipop(10, "p.A10V", 2, "missense")], "t", "s"),
    lambda: charts.timeline([charts.TimelineItem("Events", 1.0), charts.TimelineItem("Radiation", 1.0, 2.0)],
                            ["Events", "Radiation"], "t"),
    lambda: charts.bar_chart(["a", "b"], [1, 2], "t"),
])
def test_every_chart_is_a_png_under_the_limit(draw):
    png = draw()
    assert png.startswith(PNG) and len(png) < charts.MAX_PNG_BYTES


def test_venn_needs_two_or_three_sets():
    with pytest.raises(ValueError):
        charts.venn(["A"], {}, "t")


@pytest.mark.parametrize("consequences, expected", [
    (["missense_variant", "stop_gained"], "truncating"),
    (["splice_region_variant", "intron_variant"], "splice"),
    (["missense_variant"], "missense"),
    (["inframe_deletion"], "inframe"),
    (["synonymous_variant"], "other"),
    ('["frameshift_variant"]', "truncating"),
])
def test_consequence_classes(consequences, expected):
    assert consequence_class(consequences) == expected


# -- family_venn ---------------------------------------------------------------------

def _trio_api(fake_api, monkeypatch, roles=("proband", "father", "mother")):
    seqs = {"proband": 10, "father": 13, "mother": 14}
    monkeypatch.setattr(cases, "_lookup_parts", lambda token, case_id: ({(seqs[r], 20): 5 for r in roles}, None))
    fake_api.on("cases", "case_entity", {
        "case_id": 1, "case_type": "germline", "members": [],
        "sequencing_experiments": [{"seq_id": seqs[r], "has_variants": True, "relationship_to_proband": r} for r in roles],
    })
    fake_api.on("cases", "case_tasks_with_occurrences",
                lambda data_type, **_: [{"id": 20}] if data_type == "germline_snv" else [])


def test_family_venn_trio(fake_api, fake_sql, run, monkeypatch):
    _trio_api(fake_api, monkeypatch)
    fake_sql.on("germline__snv__occurrence", [
        {"m0": 1, "m1": 1, "m2": 1, "n": 40}, {"m0": 1, "m1": 0, "m2": 0, "n": 3}, {"m0": 0, "m1": 1, "m2": 0, "n": 5},
    ])
    data, png = _parts(run(family_venn(1, tenant="demo")))
    assert png.startswith(PNG)
    assert data["members"] == ["Proband", "Father", "Mother"]
    assert data["variants_per_region"] == {"Proband & Father & Mother": 40, "Proband": 3, "Father": 5}
    [(sql, values)] = fake_sql.queries
    assert "LEFT SEMI JOIN" not in sql
    assert sorted(v for k, v in values.items() if v == 5) == [5, 5, 5]  # each key's part
    assert re.search(r"o\.zygosity IN \(", sql) and {"HET", "HOM", "HEM"} <= set(values.values())


def test_family_venn_duo_with_filters(fake_api, fake_sql, run, monkeypatch):
    _trio_api(fake_api, monkeypatch, roles=("proband", "mother"))
    fake_sql.on("germline__snv__occurrence", [{"m0": 1, "m1": 1, "n": 7}])
    data, _ = _parts(run(family_venn(1, rare_only=True, vep_impact=["high"], tenant="demo")))
    assert data["members"] == ["Proband", "Mother"] and data["variants_per_region"] == {"Proband & Mother": 7}
    assert data["filters"][1] == "impact HIGH"
    [(sql, values)] = fake_sql.queries
    assert "LEFT SEMI JOIN" in sql and bound_value(sql, values, "v.vep_impact IN (") == "HIGH"


def test_family_venn_needs_a_parent(fake_api, fake_sql, run, monkeypatch):
    _trio_api(fake_api, monkeypatch, roles=("proband",))
    assert "no sequenced parent" in run(family_venn(1, tenant="demo"))["error"]
    assert fake_sql.queries == []


# -- survival_curve ------------------------------------------------------------------

def _patients(fake_api, rows):
    def search(list_body_with_criteria, **_):
        offset = list_body_with_criteria.offset or 0
        return {"count": len(rows), "list": rows[offset:offset + list_body_with_criteria.limit]}
    fake_api.on("patients", "search_patients", search)


def test_survival_by_genotype(fake_api, fake_sql, run):
    fake_api.on("genes", "gene_search", [{"symbol": "SCN1A"}])
    rows = [{"radiant_patient_id": i, "survival_days": 365 * (i % 7 + 1), "vital_status": "deceased" if i % 2 else "alive"}
            for i in range(1, 151)]
    rows += [{"radiant_patient_id": None, "survival_days": 100, "vital_status": "alive"},
             {"radiant_patient_id": 999, "survival_days": None, "vital_status": "alive"}]
    _patients(fake_api, rows)
    fake_sql.on("SELECT DISTINCT patient_id FROM car", [{"patient_id": str(i)} for i in range(1, 21)])

    data, png = _parts(run(survival_curve(gene="scn1a", tenant="demo")))

    assert png.startswith(PNG)
    patients = {g["group"]: g["patients"] for g in data["groups"]}
    assert patients == {"SCN1A carriers": 20, "Other sequenced patients": 130}
    assert data["excluded_without_survival_data"] == {"SCN1A carriers": 0, "Other sequenced patients": 1}
    assert data["definition"]["excluded_not_sequenced"] == 1
    assert data["logrank_p"] is not None and "scipy.stats.logrank" in data["method"]
    assert len([c for c in fake_api.calls if c[1] == "search_patients"]) == 2  # paged by 100


def test_survival_by_clinical_groups(fake_api, run):
    _patients(fake_api, [{"survival_days": 400, "vital_status": "deceased"}, {"survival_days": 800, "vital_status": "alive"}])
    data, _ = _parts(run(survival_curve(group_a={"vital_status": "alive"}, group_b={"organization_code": "X"},
                                        label_a="A", label_b="B", tenant="demo")))
    assert [g["group"] for g in data["groups"]] == ["A", "B"]


@pytest.mark.parametrize("kwargs, message", [
    ({}, "give `gene`"),
    ({"gene": "SCN1A", "group_a": {"vital_status": "alive"}}, "either"),
    ({"group_a": {"mrn": "1"}, "group_b": {"vital_status": "alive"}}, "unknown patient filter"),
])
def test_survival_validates(fake_api, run, kwargs, message):
    assert message in run(survival_curve(tenant="demo", **kwargs))["error"]
    assert fake_api.calls == []


def test_survival_without_clinical_data(fake_api, run):
    fake_api.on("patients", "search_patients", api_exception(404, "404 page not found"))
    result = run(survival_curve(group_a={"vital_status": "alive"}, group_b={"vital_status": "deceased"}, tenant="x"))
    assert result["clinical_data_available"] is False


# -- oncoprint / lollipop ------------------------------------------------------------

def test_oncoprint(fake_api, fake_sql, run):
    fake_api.on("genes", "gene_search", lambda gene_search_body, **_: [{"symbol": i.upper()} for i in gene_search_body.inputs])
    fake_sql.on("germline__snv__occurrence", [
        {"case_id": 1, "gene": "SCN1A", "kind": "missense_variant"},
        {"case_id": 1, "gene": "SCN1A", "kind": "stop_gained"},
        {"case_id": 2, "gene": "BRCA1", "kind": "missense_variant"},
    ])
    fake_sql.on("germline__cnv__occurrence", [{"case_id": 2, "gene": "SCN1A", "kind": "LOSS"}])
    data, png = _parts(run(oncoprint(["scn1a", "brca1"], data_types=["germline_snv", "germline_cnv"], tenant="demo")))
    assert png.startswith(PNG)
    assert data["altered_cases"] == 2 and data["altered_cases_per_gene"] == {"SCN1A": 2, "BRCA1": 1}
    for sql, values in fake_sql.queries:
        assert bound_value(sql, values, "s.tenant_code = ") == "demo"


@pytest.mark.parametrize("kwargs, message", [
    ({"genes": []}, "1 to 20 genes"),
    ({"genes": ["A'--"]}, "invalid gene symbol"),
    ({"genes": ["TP53"], "data_types": ["rna"]}, "invalid data_type"),
])
def test_oncoprint_validates(fake_api, run, kwargs, message):
    assert message in run(oncoprint(**kwargs))["error"]


def test_lollipop_draws_the_main_transcript(fake_api, fake_sql, run):
    fake_api.on("genes", "gene_search", [{"symbol": "SMARCB1"}])
    fake_sql.on("COUNT(DISTINCT car.patient_id)", [
        {"locus_id": 1, "transcript_id": "T1", "aa_change": "p.Arg201Ter", "kind": "stop_gained", "carriers": 4},
        {"locus_id": 2, "transcript_id": "T1", "aa_change": "p.Ala150Val", "kind": "missense_variant", "carriers": 1},
        {"locus_id": 3, "transcript_id": "T2", "aa_change": "p.Gly12Asp", "kind": "missense_variant", "carriers": 1},
        {"locus_id": 4, "transcript_id": "T1", "aa_change": "", "kind": "splice_region_variant", "carriers": 1},
    ])
    data, _ = _parts(run(lollipop("smarcb1", tenant="demo")))
    assert data["transcript"] == "T1" and data["variants_drawn"] == 2
    assert data["variants_on_other_transcripts"] == 1 and data["variants_without_protein_change"] == 1
    assert data["top_variants"][0] == {"aa_change": "p.Arg201Ter", "carriers": 4, "class": "truncating"}


# -- patient_timeline / bar_chart ----------------------------------------------------

def test_timeline_items_from_the_patient_page():
    context = {
        "patient": {"events": [{"day": 1000, "event_type": "initial_cns_tumor"}, {"day": None, "event_type": "x"}],
                    "vital_status": "alive", "vital_status_at": {"day": 2000}},
        "treatments": {
            "surgeries": [{"day": 1010, "extent_of_tumor_resection": "Gross/Near total"}],
            "radiations": [{"start": {"day": 1100}, "stop": {"day": 1150}, "site": "Focal"}],
            "therapies": [{"start": {"day": 1060}, "stop": {"day": 1300}, "chemotherapy_agents": ["vincristine"]}],
        },
        "imaging": {"sessions": [{"day": 990}, {"day": 1500}]},
    }
    items = chart_tools.timeline_items(context)
    tracks = [i.track for i in items]
    assert tracks.count("Events") == 2 and tracks.count("Imaging") == 2
    chemo = next(i for i in items if i.track == "Chemotherapy")
    assert chemo.label == "vincristine" and chemo.end > chemo.start


def test_patient_timeline(fake_api, run):
    fake_api.on("patients", "patient_entity", {"patient_key": KEY, "events": [{"day": 1000, "event_type": "progressive"}]})
    fake_api.on("patients", "patient_treatments", {"surgeries": [], "radiations": [], "therapies": []})
    fake_api.on("patients", "patient_imaging", {"sessions": [{"day": 1200}]})
    data, png = _parts(run(patient_timeline(KEY, tenant="demo")))
    assert png.startswith(PNG) and data["items_per_track"] == {"Events": 1, "Imaging": 1}


def test_patient_timeline_rejects_an_mrn(fake_api, run):
    assert "not a patient key" in run(patient_timeline("MRN-1"))["error"]


def test_bar_chart(run):
    data, png = _parts(run(bar_chart("Carriers", ["missense", "stop_gained"], [3, 1])))
    assert png.startswith(PNG) and data["values"] == {"missense": 3.0, "stop_gained": 1.0}
    assert "one number per category" in run(bar_chart("t", ["a"], [1, 2]))["error"]
    assert "1 to 50" in run(bar_chart("t", [], []))["error"]


# -- MCP contract ----------------------------------------------------------------------

def test_a_chart_tool_answers_with_text_and_png_content():
    mcp = FastMCP("t")
    mcp.tool(bar_chart)

    async def call():
        async with Client(mcp) as client:
            return await client.call_tool("bar_chart", {"title": "t", "categories": ["a"], "values": [1]})

    result = asyncio.run(call())
    assert [part.type for part in result.content] == ["text", "image"]
    assert result.content[1].mimeType == "image/png"

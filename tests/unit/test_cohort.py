"""``cohort_carriers`` and ``genotype_phenotype``."""

import pytest
from conftest import PORTAL, bound_value, echo_gene_search

from radiant_mcp import sql as _sql
from radiant_mcp.domain.criteria import build_criteria
from radiant_mcp.queries import carriers as carrier_queries
from radiant_mcp.tools.cohort import cohort_carriers, genotype_phenotype


@pytest.mark.parametrize("kwargs, message", [
    ({"gene": "SCN1A'; --"}, "invalid gene symbol"),
    ({"gene": "SCN1A", "data_type": "germline_cnv", "vep_impact": ["HIGH"]}, "only apply to SNV"),
    ({"gene": "SCN1A", "cnv_types": ["LOSS"]}, "only applies to CNV"),
    ({"gene": "SCN1A", "max_gnomad_af": 2}, "max_gnomad_af"),
    ({"gene": "SCN1A", "vep_impact": ["HUGE"]}, "invalid vep_impact"),
    ({"gene": "SCN1A", "data_type": "rna"}, "invalid data_type"),
])
def test_carriers_validate_before_any_call(fake_api, fake_sql, run, kwargs, message):
    assert message in run(cohort_carriers(**kwargs))["error"]
    assert fake_api.calls == [] and fake_sql.queries == []


def test_carriers(fake_api, fake_sql, run):
    echo_gene_search(fake_api)
    fake_sql.on("COUNT(DISTINCT case_id)", [{"cases": 2, "patients": 2, "variants": 3}])
    fake_sql.on("array_agg", [{
        "case_id": 1, "patient_id": 100, "seq_id": 10, "task_id": 20, "variant_count": 2,
        "variants": '["chr2:g.1A>T", "chr2:g.2C>G"]', "first_variant_id": 1001,
    }])
    fake_sql.on("u.consequence", [{"consequence": "stop_gained", "variants": 1, "patients": 1}])

    result = run(cohort_carriers("scn1a", vep_impact=["high"], max_gnomad_af=0.01, tenant="demo"))

    assert (result["cases"], result["patients"], result["variants"]) == (2, 2, 3)
    [carrier] = result["carriers"]
    assert carrier["variants"] == ["chr2:g.1A>T", "chr2:g.2C>G"] and "first_variant_id" not in carrier
    assert carrier["portal_url"].endswith("seq_id=10&task_id=20&variant_section=snv&selectedVariant=1001")
    assert result["summary"]["by_consequence"][0]["consequence"] == "stop_gained"
    assert result["criteria"] == {
        "data_type": "germline_snv", "gene": "SCN1A", "vep_impact": ["HIGH"],
        "max_gnomad_af": "< 0.01 or absent from gnomAD",
        "gene_match": "a consequence on a transcript of the gene (not only the picked one); "
                      "vep_impact is the impact on that gene",
        "reruns": "the latest task of each sequencing",
    }
    for sql, values in fake_sql.queries:
        assert bound_value(sql, values, "WHERE cf.symbol = ") == "SCN1A"
        assert "sv.gnomad_v3_af IS NULL OR sv.gnomad_v3_af < " in sql


def test_carriers_unknown_gene(fake_api, fake_sql, run):
    fake_api.on("genes", "gene_search", [])
    assert run(cohort_carriers("NOPE1", tenant="demo"))["status"] == 404
    assert fake_sql.queries == []


def test_carriers_report_starrocks_errors(fake_api, fake_sql, run):
    echo_gene_search(fake_api)
    fake_sql.on("COUNT(DISTINCT case_id)", _sql.SqlError("Access denied"))
    assert run(cohort_carriers("SCN1A", tenant="demo")) == {"error": "StarRocks error: Access denied"}


def test_genotype_phenotype(fake_api, fake_sql, run):
    echo_gene_search(fake_api)
    fake_sql.on("COUNT(DISTINCT case_id)", [{"cases": 2, "patients": 2, "variants": 2}])
    fake_sql.on("hpo_term_name", [{"hpo_term_id": "HP:0001250", "hpo_term_name": "Seizure", "patients": 2}])
    fake_sql.on("COUNT(DISTINCT obs.patient_id) AS patients FROM", [{"patients": 2}])
    fake_sql.on("obs.code_value = ", [{"case_id": 3, "patient_id": 200}])

    result = run(genotype_phenotype("SCN1A", hpo_term="HP:0001263", tenant="demo"))

    assert result["carrier_patients"] == 2 and result["carrier_patients_with_phenotypes"] == 2
    assert result["phenotypes"][0]["hpo_term_id"] == "HP:0001250"
    assert result["carriers_with_term"] == [{"case_id": 3, "patient_id": 200, "portal_url": f"{PORTAL}/demo/case/entity/3"}]
    for sql, values in fake_sql.matching("obs_categorical"):
        assert bound_value(sql, values, "obs.observation_code = ") == "phenotype"
        assert bound_value(sql, values, "obs.interpretation_code = ") == "positive"


def test_genotype_phenotype_rejects_a_bad_hpo_id(fake_api, run):
    assert "invalid HPO id" in run(genotype_phenotype("SCN1A", hpo_term="seizure"))["error"]


def test_carriers_match_any_transcript_of_the_gene_and_the_latest_task():
    criteria = build_criteria("germline_snv", "SCN1A", ["HIGH"], None, None, None, None)
    sql, params = carrier_queries.totals_query("demo", criteria)
    # The gene and the impact come from the consequences on the gene's transcripts.
    assert "radiant.snv__consequence_filter cf" in sql
    assert bound_value(sql, params.values, "cf.vep_impact IN (") == "HIGH"
    assert "demo_tenant.snv__variant sv" in sql and "snv__variant_partitioned" not in sql
    assert "ROW_NUMBER() OVER (PARTITION BY x.seq_id, x.variant_id ORDER BY x.task_id DESC)" in sql
    cnv_sql, _ = carrier_queries.totals_query("demo", build_criteria("germline_cnv", "SCN1A", None, None, None, None, None))
    assert "ROW_NUMBER()" in cnv_sql


def test_phenotypes_leave_fetal_observations_out():
    criteria = build_criteria("germline_snv", "SCN1A", None, None, None, None, None)
    for sql, _ in (carrier_queries.phenotypes_query("demo", criteria, 10),
                   carrier_queries.phenotyped_patients_query("demo", criteria),
                   carrier_queries.carriers_with_term_query("demo", criteria, "HP:0001250", 10)):
        assert "obs.fetus_id IS NULL" in sql

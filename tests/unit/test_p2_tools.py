"""RAD-252 tools: second_hit, cohort_carriers, genotype_phenotype, gene_info, lookup_terms."""

import re

import pytest
from conftest import PORTAL, bound_value

from radiant_mcp.enums import ENUMS
from radiant_mcp import sql as _sql
from radiant_mcp.domain import rules as _rules
from radiant_mcp.domain.criteria import build_criteria
from radiant_mcp.queries import carriers as carrier_queries
from radiant_mcp.queries import hits as hit_queries
from radiant_mcp.services import cases as case_service
from radiant_mcp.tools import hits
from radiant_mcp.tools.cohort import cohort_carriers, genotype_phenotype
from radiant_mcp.tools.genes import gene_info, lookup_terms
from radiant_mcp.tools.hits import second_hit

SNV_KEY = {"seq_id": 10, "task_id": 20, "part": 5, "variant_part": 0}
CNV_KEY = {"seq_id": 10, "task_id": 21, "part": 5, "variant_part": 0}


def _placeholders(sql: str) -> set[str]:
    return set(re.findall(r"%\((p\d+)\)s", sql))


# -- rules ---------------------------------------------------------------------

def test_rules_use_stored_values():
    assert set(_rules.QUALIFYING_IMPACTS) <= set(ENUMS["vep_impact"])
    assert set(_rules.CLINVAR_PLP) <= set(ENUMS["clinvar_interpretation"])
    assert set(_rules.EXOMISER_PLP) <= set(ENUMS["exomiser_acmg_classification"])
    assert set(_rules.GERMLINE_SECOND_HIT_CNV_TYPES) <= set(ENUMS["cnv_type"])
    assert set(_rules.SOMATIC_SECOND_HIT_CNV_TYPES) <= set(ENUMS["cnv_type"])
    assert _rules.HET in ENUMS["zygosity"]


# -- every query binds exactly its placeholders ----------------------------------

def _all_queries():
    criteria = [
        build_criteria("germline_snv", "SCN1A", ["HIGH"], ["Pathogenic"], 0.01, ["HET"], None),
        build_criteria("somatic_snv_all", "TP53", None, None, None, None, None),
        build_criteria("germline_cnv", "DMD", None, None, None, None, ["LOSS"]),
        build_criteria("somatic_cnv", "CDKN2A", None, None, None, None, None),
    ]
    yield "snv_cnv", hit_queries.snv_cnv_query("demo_tenant", SNV_KEY, CNV_KEY)
    yield "comphet", hit_queries.comphet_query("demo_tenant", SNV_KEY)
    yield "germline_keys", hit_queries.germline_keys_query("radiant", "demo", 100)
    yield "germline_plp", hit_queries.germline_plp_query("demo_tenant", SNV_KEY)
    yield "somatic_snv", hit_queries.somatic_snv_query("demo_tenant", SNV_KEY, ["BRCA1", "TP53"])
    yield "somatic_cnv", hit_queries.somatic_cnv_query("demo_tenant", CNV_KEY, ["BRCA1"])
    for c in criteria:
        yield f"totals {c.data_type}", carrier_queries.totals_query("demo", c)
        yield f"carriers {c.data_type}", carrier_queries.carriers_query("demo", c, 10)
        for name, query in carrier_queries.summary_queries("demo", c).items():
            yield f"{name} {c.data_type}", query
        yield f"phenotypes {c.data_type}", carrier_queries.phenotypes_query("demo", c, 10)
        yield f"phenotyped {c.data_type}", carrier_queries.phenotyped_patients_query("demo", c)
        yield f"with_term {c.data_type}", carrier_queries.carriers_with_term_query("demo", c, "HP:0001250", 10)


@pytest.mark.parametrize("name, query", list(_all_queries()), ids=lambda v: v if isinstance(v, str) else "")
def test_each_query_binds_exactly_its_placeholders(name, query):
    sql, params = query
    assert _placeholders(sql) == set(params.values), name
    assert "'" not in sql, f"{name}: a literal value is written into the SQL"


def test_carrier_query_binds_the_gene_and_the_tenant_to_their_own_slots():
    criteria = build_criteria("germline_snv", "SCN1A", None, None, None, None, None)
    sql, params = carrier_queries.totals_query("demo", criteria)
    assert bound_value(sql, params.values, "WHERE symbol = ") == "SCN1A"
    assert bound_value(sql, params.values, "s.tenant_code = ") == "demo"


# -- second_hit ------------------------------------------------------------------

def _case_api(fake_api, case_type, keys):
    fake_api.on("cases", "case_entity", {
        "case_id": 1, "case_type": case_type,
        "members": [{"patient_id": 100, "relationship_to_proband": "proband"}],
        "sequencing_experiments": [{"seq_id": 10, "has_variants": True, "relationship_to_proband": "proband",
                                    "sample_submitter_id": "S1"}],
    })
    fake_api.on("cases", "case_tasks_with_occurrences", lambda data_type, **_: keys.get(data_type, []))


def test_second_hit_germline(fake_api, fake_sql, run, monkeypatch):
    _case_api(fake_api, "germline", {"germline_snv": [{"id": 20}], "germline_cnv": [{"id": 21}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({(10, 20): 5, (10, 21): 5}, None))
    fake_sql.on("germline__cnv__occurrence", [{
        "gene": "SCN1A", "locus_id": 1001, "clinvar_interpretation": '["Pathogenic"]', "cnv_id": 501,
        "snv_inside_cnv": 0,
    }])
    fake_sql.on("germline__snv__comphet", [
        {"gene": "SCN1A", "locus_id": 1001, "partner_locus_id": 1002},
        {"gene": "SCN1A", "locus_id": 1002, "partner_locus_id": 1001},
    ])

    result = run(second_hit(1, tenant="demo"))

    [seq] = result["sequencings"]
    [hit] = seq["snv_cnv"]
    assert hit["clinvar_interpretation"] == ["Pathogenic"] and hit["snv_inside_cnv"] is False
    assert hit["portal_url"] == f"{PORTAL}/demo/case/entity/1?tab=variants&seq_id=10&task_id=20&variant_section=snv&selectedVariant=1001"
    assert hit["cnv_portal_url"].endswith("task_id=21&variant_section=cnv")
    assert [(p["locus_id"], p["partner_locus_id"]) for p in seq["compound_heterozygous"]] == [(1001, 1002)]
    assert "rare = gnomAD v3 AF < 0.01" in result["rules"]
    # AC 4: every occurrence table is filtered on the part of its key.
    for sql, values in fake_sql.queries:
        for alias in re.findall(r"\b(o|c)\.part = ", sql):
            assert bound_value(sql, values, f"{alias}.part = ") == 5


def test_second_hit_skips_keys_without_part(fake_api, fake_sql, run, monkeypatch):
    _case_api(fake_api, "germline", {"germline_snv": [{"id": 20}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({}, "part lookup failed: boom"))
    result = run(second_hit(1, tenant="demo"))
    assert result["sequencings"] == []
    assert any("no `part`, skipped" in w for w in result["warnings"])
    assert fake_sql.queries == []


def test_second_hit_somatic(fake_api, fake_sql, run, monkeypatch):
    _case_api(fake_api, "somatic", {"somatic_snv_tn": [{"id": 40}], "somatic_cnv": [{"id": 41}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({(10, 40): 7, (10, 41): 7}, None))
    fake_sql.on("staging_sequencing_experiment", [{"case_id": 9, "seq_id": 11, "task_id": 22, "part": 15}])
    fake_sql.on("germline__snv__occurrence", [
        {"gene": "BRCA1", "locus_id": 1003, "clinvar_interpretation": "[]", "interpreted_plp": 1},
        {"gene": "SCN1A", "locus_id": 1001, "clinvar_interpretation": '["Pathogenic"]', "interpreted_plp": 0},
    ])
    fake_sql.on("somatic__snv__occurrence", [{"gene": "BRCA1", "locus_id": 1005}])
    fake_sql.on("somatic__cnv__occurrence", [{"gene": "BRCA1", "cnv_id": 601, "cnv_type": "CNLOH"}])

    result = run(second_hit(1, tenant="demo"))

    [gene] = result["genes"]
    assert gene["gene"] == "BRCA1" and gene["germline"][0]["interpreted_plp"] is True
    assert gene["germline"][0]["portal_url"].startswith(f"{PORTAL}/demo/case/entity/9?")
    assert [r["locus_id"] for r in gene["somatic_snv"]] == [1005] and gene["somatic_snv"][0]["data_type"] == "somatic_snv_tn"
    assert [r["cnv_id"] for r in gene["somatic_cnv"]] == [601]
    assert result["germline_plp_without_somatic_event"] == ["SCN1A"]
    [(sql, values)] = fake_sql.matching("germline__snv__occurrence")
    assert bound_value(sql, values, "o.part = ") == 15 and bound_value(sql, values, "v.part = ") == 1
    [(sql, values)] = fake_sql.matching("staging_sequencing_experiment")
    assert bound_value(sql, values, "task_type = ") == "radiant_germline_annotation"


def test_second_hit_somatic_without_germline_sequencing(fake_api, fake_sql, run, monkeypatch):
    _case_api(fake_api, "somatic", {"somatic_snv_tn": [{"id": 40}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({(10, 40): 7}, None))
    result = run(second_hit(1, tenant="demo"))
    assert "no germline sequencing" in result["note"]


def test_second_hit_rejects_a_non_numeric_case(fake_api, run):
    assert "invalid integer" in run(second_hit("1 OR 1=1"))["error"]
    assert fake_api.calls == []


# -- cohort_carriers / genotype_phenotype -------------------------------------------

def _gene_api(fake_api):
    fake_api.on("genes", "gene_search", lambda gene_search_body, **_: [
        {"symbol": i.upper(), "ensembl_gene_id": "ENSG1"} for i in gene_search_body.inputs])


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
    _gene_api(fake_api)
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
    }
    for sql, values in fake_sql.queries:
        assert bound_value(sql, values, "WHERE symbol = ") == "SCN1A"
        assert "gnomad_v3_af IS NULL OR gnomad_v3_af < " in sql


def test_carriers_unknown_gene(fake_api, fake_sql, run):
    fake_api.on("genes", "gene_search", [])
    assert run(cohort_carriers("NOPE1", tenant="demo"))["status"] == 404
    assert fake_sql.queries == []


def test_carriers_report_starrocks_errors(fake_api, fake_sql, run):
    _gene_api(fake_api)
    fake_sql.on("COUNT(DISTINCT case_id)", _sql.SqlError("Access denied"))
    assert run(cohort_carriers("SCN1A", tenant="demo")) == {"error": "StarRocks error: Access denied"}


def test_genotype_phenotype(fake_api, fake_sql, run):
    _gene_api(fake_api)
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


# -- gene_info / lookup_terms -----------------------------------------------------

def test_gene_info(fake_api, fake_sql, run):
    _gene_api(fake_api)
    fake_sql.on("omim_gene_panel", [{"condition_name": "Dravet", "inheritance_code": '["AD"]'}])
    fake_sql.on("hpo_gene_panel", [{"hpo_term_id": f"HP:{i:07d}"} for i in range(150)])
    fake_sql.on("gene_panel_mv", _sql.SqlError("Unknown table gene_panel_mv"))
    fake_sql.on("gnomad_constraint", [{"transcript_id": "ENST1", "pli": 1.0, "loeuf": 0.1}])

    result = run(gene_info("scn1a", tenant="demo"))

    assert result["symbol"] == "SCN1A" and result["ensembl_gene_id"] == "ENSG1"
    assert result["omim_conditions"] == [{"condition_name": "Dravet", "inheritance_code": ["AD"]}]
    assert result["hpo_terms_count"] == 150 and len(result["hpo_terms"]) == 100
    assert result["gnomad_constraint"][0]["pli"] == 1.0
    assert "tenant_gene_panels" not in result
    assert result["warnings"] == ["tenant_panels: Unknown table gene_panel_mv"]
    [(sql, values)] = fake_sql.matching("ensembl_exon_by_gene")
    assert bound_value(sql, values, "e.gene_id = ") == "ENSG1"


def test_gene_info_unknown_gene(fake_api, fake_sql, run):
    fake_api.on("genes", "gene_search", [])
    assert "not found" in run(gene_info("NOPE1", tenant="demo"))["error"]


def test_lookup_terms(fake_api, run):
    fake_api.on("mondo", "mondo_term_auto_complete", [
        {"highlight": {"id": "x"}, "source": {"id": "MONDO:0100135", "name": "Dravet syndrome"}},
        {"source": {}},
    ])
    result = run(lookup_terms("mondo", " dravet ", tenant="demo", limit=500))
    assert result["terms"] == [{"id": "MONDO:0100135", "name": "Dravet syndrome"}]
    assert fake_api.kwargs("mondo_term_auto_complete") == {"tenant": "demo", "prefix": "dravet", "limit": "50"}


def test_lookup_terms_validates(fake_api, run):
    assert "Unknown ontology" in run(lookup_terms("go", "x"))["error"]
    assert "non-empty" in run(lookup_terms("hpo", "  "))["error"]


def test_second_hit_finds_a_hemizygous_snv_inside_a_deletion(fake_api, fake_sql, run, monkeypatch):
    """Case 1129: CLCNKB p.Ala77Pro called HOM in the proband because it sits inside his deletion
    (LOSS, CN 1); the mother carries the deletion (her WT call), the father the SNV. slivar
    reports uniparental_disomy; the deletion explains it."""
    seqs = [(1032, "proband", 1070, 1071), (1033, "mother", None, 1072), (1034, "father", None, 1073)]
    fake_api.on("cases", "case_entity", {
        "case_id": 1129, "case_type": "germline", "members": [],
        "sequencing_experiments": [{"seq_id": s, "has_variants": True, "relationship_to_proband": r} for s, r, _, _ in seqs],
    })
    fake_api.on("cases", "case_tasks_with_occurrences", lambda seq_id, data_type, **_: [
        {"id": snv if data_type == "germline_snv" else cnv} for s, _, snv, cnv in seqs
        if s == seq_id and (snv if data_type == "germline_snv" else cnv)])
    parts = {(s, t): 2 for s, _, snv, cnv in seqs for t in (snv, cnv) if t}
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: (parts, None))
    fake_sql.on("WITH snv AS", [{
        "gene": "CLCNKB", "locus_id": 42, "hgvsg": "chr1:g.16045686G>C", "snv_start": 16045686, "zygosity": "HOM",
        "transmission_mode": "uniparental_disomy", "clinvar_interpretation": '["Pathogenic"]', "cnv_id": 7,
        "cnv_chromosome": "1", "cnv_start": 16033040, "cnv_end": 16057606, "cnv_cn": 1, "snv_inside_cnv": 1,
    }])
    fake_sql.on("SELECT g.symbol AS gene, c.seq_id", lambda values: [
        {"gene": "CLCNKB", "seq_id": 1033, "cnv_id": 8, "chromosome": "1", "start": 16037075, "end": 16057606, "cn": 1}
    ] if 1033 in values.values() else [])

    result = run(second_hit(1129, tenant="pragmatiq"))

    [proband] = [s for s in result["sequencings"] if s["seq_id"] == 1032]
    [hit] = proband["snv_cnv"]
    assert hit["zygosity"] == "HOM" and hit["snv_inside_cnv"] is True and hit["mechanism"].startswith("hemizygous")
    assert hit["family_deletions"] == [{
        "gene": "CLCNKB", "seq_id": 1033, "cnv_id": 8, "chromosome": "1", "start": 16037075, "end": 16057606,
        "cn": 1, "relationship_to_proband": "mother", "covers_snv": True,
    }]
    assert "apparent uniparental disomy" in result["rules"]
    [(sql, values)] = fake_sql.matching("WITH snv AS")
    assert {"HET", "HOM", "HEM"} <= set(values.values())
    assert "snv.zygosity = %(" in sql and "snv.start BETWEEN cnv.start AND cnv.end)" in sql


def test_mechanism_wording():
    assert hits.mechanism("HOM", True).startswith("hemizygous")
    assert hits.mechanism("HET", True).startswith("HET call inside")
    assert hits.mechanism("HET", False).startswith("SNV and deletion on the same gene")


def test_second_hit_flags_a_cut_list(fake_api, fake_sql, run, monkeypatch):
    from radiant_mcp.tools import hits as hits_tool

    monkeypatch.setattr(hits_tool, "ROW_LIMIT", 2)
    _case_api(fake_api, "germline", {"germline_snv": [{"id": 20}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({(10, 20): 5}, None))
    fake_sql.on("germline__snv__comphet", [
        {"gene": g, "locus_id": i, "partner_locus_id": i + 100} for i, g in enumerate(["A", "B", "C"], 1)
    ])
    result = run(second_hit(1, tenant="demo"))
    assert [p["gene"] for p in result["sequencings"][0]["compound_heterozygous"]] == ["A", "B"]
    assert result["truncated"] == ["seq_id=10 compound_heterozygous"] and "alphabet" in result["truncated_note"]
    [(sql, values)] = fake_sql.matching("germline__snv__comphet")
    assert "LEAST(c.locus_id, c.partner_locus_id)" in sql and values[re.findall(r"LIMIT %\((p\d+)\)s", sql)[0]] == 201


def test_second_hit_keeps_a_pair_per_gene(fake_api, fake_sql, run, monkeypatch):
    _case_api(fake_api, "germline", {"germline_snv": [{"id": 20}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({(10, 20): 5}, None))
    fake_sql.on("germline__snv__comphet", [
        {"gene": "GENE1", "locus_id": 1, "partner_locus_id": 2},
        {"gene": "GENE2", "locus_id": 1, "partner_locus_id": 2},
    ])
    result = run(second_hit(1, tenant="demo"))
    assert [p["gene"] for p in result["sequencings"][0]["compound_heterozygous"]] == ["GENE1", "GENE2"]
    assert "truncated" not in result


def test_second_hit_reads_the_latest_task_of_a_rerun(fake_api, fake_sql, run, monkeypatch):
    _case_api(fake_api, "somatic", {"somatic_snv_tn": [{"id": 40}, {"id": 42}]})
    monkeypatch.setattr(case_service, "lookup_parts", lambda token, case_id: ({(10, 40): 7, (10, 42): 7}, None))
    fake_sql.on("staging_sequencing_experiment", [
        {"case_id": 9, "seq_id": 11, "task_id": 22, "part": 15},
        {"case_id": 9, "seq_id": 11, "task_id": 23, "part": 15},
    ])
    fake_sql.on("germline__snv__occurrence", [{"gene": "BRCA1", "locus_id": 1003, "clinvar_interpretation": "[]"}])
    fake_sql.on("somatic__snv__occurrence", [{"gene": "BRCA1", "locus_id": 1005}])
    result = run(second_hit(1, tenant="demo"))
    assert [k["task_id"] for k in result["germline_sequencings"]] == [23]
    [gene] = result["genes"]
    assert len(gene["germline"]) == 1 and len(gene["somatic_snv"]) == 1
    assert sum("older task" in w for w in result["warnings"]) == 2
    [(sql, values)] = fake_sql.matching("germline__snv__occurrence")
    assert bound_value(sql, values, "o.task_id = ") == 23

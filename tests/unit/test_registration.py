"""The tool list the assistant sees (RAD-251, acceptance criterion 4)."""

import asyncio

from fastmcp import FastMCP

from radiant_mcp.tools import register_tools

API_TOOLS = {
    "list_tenants", "search_cases", "get_case_context", "search_patients", "get_patient_context",
    "prioritize_snv", "case_cnv", "variant_details", "variant_carriers",
    "second_hit", "gene_info", "lookup_terms", "cohort_carriers", "genotype_phenotype",
    "family_venn", "survival_curve", "oncoprint", "lollipop", "patient_timeline",
}
ALWAYS = {"describe_schema", "bar_chart", "read_query", "table_overview"}


def _names(mcp) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_the_assistant_sees_the_radiant_tools_and_the_read_only_sql(monkeypatch):
    monkeypatch.setenv("RADIANT_API_URL", "http://api.example")
    mcp = FastMCP("test")
    register_tools(mcp)
    assert _names(mcp) == API_TOOLS | ALWAYS
    assert asyncio.run(mcp.list_resources()) == []
    assert asyncio.run(mcp.list_resource_templates()) == []


def test_without_the_api_only_the_schema_chart_and_sql_tools_are_added(monkeypatch):
    monkeypatch.delenv("RADIANT_API_URL", raising=False)
    mcp = FastMCP("test")
    assert set(register_tools(mcp)) == ALWAYS


def test_unexpected_errors_do_not_leak_their_message():
    from radiant_mcp.portal_api import api_error

    error = api_error(ConnectionError("HTTPConnectionPool(host='radiant-api.internal', port=8090): refused"))
    assert error == {"error": "Unexpected ConnectionError in the tool; the details are in the server logs."}

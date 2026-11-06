"""The tool list the assistant sees (RAD-251, acceptance criterion 4)."""

import asyncio

import pytest
from fastmcp import FastMCP

from radiant_mcp.tools import register_tools
from radiant_mcp.upstream import HIDDEN_UPSTREAM_TOOLS, hide_upstream_tools

API_TOOLS = {
    "list_tenants", "search_cases", "get_case_context", "search_patients", "get_patient_context",
    "prioritize_snv", "case_cnv", "variant_details", "variant_carriers",
    "second_hit", "gene_info", "lookup_terms", "cohort_carriers", "genotype_phenotype",
    "family_venn", "survival_curve", "oncoprint", "lollipop", "patient_timeline",
}


def _names(mcp) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


@pytest.fixture
def upstream_mcp():
    """A fresh server holding the upstream StarRocks tools."""
    import mcp_server_starrocks.server as sr_server

    mcp = FastMCP("test")
    for tool in asyncio.run(sr_server.mcp.list_tools()):
        mcp.add_tool(tool)
    return mcp


def test_the_assistant_sees_the_radiant_tools_and_the_read_only_sql(upstream_mcp, monkeypatch):
    monkeypatch.setenv("RADIANT_API_URL", "http://api.example")
    assert set(hide_upstream_tools(upstream_mcp)) == set(HIDDEN_UPSTREAM_TOOLS)
    register_tools(upstream_mcp)
    assert _names(upstream_mcp) == API_TOOLS | {"describe_schema", "bar_chart", "read_query", "table_overview"}


def test_without_the_api_only_the_schema_tool_is_added(monkeypatch):
    monkeypatch.delenv("RADIANT_API_URL", raising=False)
    mcp = FastMCP("test")
    assert register_tools(mcp) == ["describe_schema", "bar_chart"]


def test_hiding_a_tool_the_upstream_no_longer_has_is_not_fatal():
    assert hide_upstream_tools(FastMCP("empty")) == []

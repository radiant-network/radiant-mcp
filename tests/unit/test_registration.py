"""The tool list the assistant sees (RAD-251, acceptance criterion 4)."""

import asyncio

import pytest
from fastmcp import FastMCP

from radiant_mcp.tools import register_tools
from radiant_mcp.upstream import (
    HIDDEN_UPSTREAM_RESOURCES,
    HIDDEN_UPSTREAM_TEMPLATES,
    HIDDEN_UPSTREAM_TOOLS,
    disable_table_overview_cache,
    hide_upstream_tools,
)

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
    """A fresh server holding the upstream StarRocks tools and resources."""
    import mcp_server_starrocks.server as sr_server

    mcp = FastMCP("test")
    for tool in asyncio.run(sr_server.mcp.list_tools()):
        mcp.add_tool(tool)
    for resource in asyncio.run(sr_server.mcp.list_resources()):
        mcp.add_resource(resource)
    for template in asyncio.run(sr_server.mcp.list_resource_templates()):
        mcp.add_template(template)
    return mcp


def test_the_assistant_sees_the_radiant_tools_and_the_read_only_sql(upstream_mcp, monkeypatch):
    monkeypatch.setenv("RADIANT_API_URL", "http://api.example")
    hidden = HIDDEN_UPSTREAM_TOOLS + HIDDEN_UPSTREAM_RESOURCES + HIDDEN_UPSTREAM_TEMPLATES
    assert set(hide_upstream_tools(upstream_mcp)) == set(hidden)
    register_tools(upstream_mcp)
    assert _names(upstream_mcp) == API_TOOLS | {"describe_schema", "bar_chart", "read_query", "table_overview"}


def test_the_assistant_sees_no_upstream_resource(upstream_mcp):
    hide_upstream_tools(upstream_mcp)
    assert asyncio.run(upstream_mcp.list_resources()) == []
    assert asyncio.run(upstream_mcp.list_resource_templates()) == []


def test_table_overview_does_not_serve_one_user_rows_to_the_next(monkeypatch):
    """Every call reaches StarRocks, so each caller's own grants decide what comes back."""
    import mcp_server_starrocks.server as sr_server
    from mcp_server_starrocks.db_client import ResultSet

    class Client:
        default_database = None

        def __init__(self, mrn):
            self.mrn = mrn

        def execute(self, query, db=None):
            if query.startswith("SELECT COUNT"):
                return ResultSet(success=True, column_names=["n"], rows=[[1]], execution_time=0.0)
            return ResultSet(success=True, column_names=["patient_id"], rows=[[self.mrn]], execution_time=0.0)

    monkeypatch.setattr(sr_server, "global_table_overview_cache", {})
    disable_table_overview_cache(sr_server)
    monkeypatch.setattr(sr_server, "db_client", Client("MRN-ALICE"))
    assert "MRN-ALICE" in sr_server.table_overview("t1_tenant.v_pcx_30_patient_key")
    monkeypatch.setattr(sr_server, "db_client", Client("MRN-BOB"))
    bob_sees = sr_server.table_overview("t1_tenant.v_pcx_30_patient_key")
    assert "MRN-BOB" in bob_sees and "MRN-ALICE" not in bob_sees


def test_without_the_api_only_the_schema_tool_is_added(monkeypatch):
    monkeypatch.delenv("RADIANT_API_URL", raising=False)
    mcp = FastMCP("test")
    assert register_tools(mcp) == ["describe_schema", "bar_chart"]


def test_hiding_a_tool_the_upstream_no_longer_has_is_not_fatal():
    assert hide_upstream_tools(FastMCP("empty")) == []

"""The one result shape of every tool (``radiant_mcp.results``)."""

import asyncio
import json

import pytest
from conftest import api_exception, envelope
from fastmcp import FastMCP
from urllib3.exceptions import MaxRetryError, NewConnectionError, ReadTimeoutError

from radiant_mcp.results import OUTPUT_SCHEMA, ToolFailure, Truncation, failure, ok, respond
from radiant_mcp.sql import InvalidInput, SqlError
from radiant_mcp.tools import register_tools


def test_every_tool_publishes_the_envelope_as_its_output_schema(configure):
    configure(radiant_api_url="http://api.example")
    mcp = FastMCP("test")
    register_tools(mcp)
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 23
    # fastmcp inlines the $defs: compare the published schemas with each other.
    schemas = {json.dumps(tool.output_schema, sort_keys=True) for tool in tools}
    assert len(schemas) == 1
    assert set(tools[0].output_schema["properties"]) == set(OUTPUT_SCHEMA["properties"])
    assert tools[0].output_schema["required"] == ["ok"]


def test_a_success_is_json_text_and_the_same_structured_content():
    result = respond(ok({"count": 2}, warnings=["", "partial"], truncated="2 more", portal_url="https://p/1"))
    assert result.is_error is False and [block.type for block in result.content] == ["text"]
    assert json.loads(result.content[0].text) == result.structured_content == {
        "ok": True, "data": {"count": 2}, "warnings": ["partial"], "truncated": {"reason": "2 more"},
        "portal_url": "https://p/1",
    }


def test_a_chart_adds_its_image_after_the_text():
    out = envelope(respond(ok({"title": "t"}, image=b"\x89PNG", truncated=Truncation(reason="r", lists=["a"]))))
    assert out.png == b"\x89PNG" and out["truncated"] == {"reason": "r", "lists": ["a"]}
    assert "image" not in json.dumps(dict(out))


def test_a_failure_sets_is_error_and_keeps_warnings_a_list():
    result = respond(failure(ToolFailure("not_found", "Gene 'X' not found.", gene="X")))
    assert result.is_error is True
    assert result.structured_content == {
        "ok": False, "warnings": [], "error": {"code": "not_found", "message": "Gene 'X' not found.",
                                               "details": {"gene": "X"}},
    }


@pytest.mark.parametrize("exc, code", [
    (InvalidInput("invalid gene symbol"), "invalid_input"),
    (SqlError("Access denied"), "upstream"),
    (api_exception(400), "invalid_input"),
    (api_exception(401), "auth_required"),
    (api_exception(403), "forbidden"),
    (api_exception(404), "not_found"),
    (api_exception(422), "invalid_input"),
    (api_exception(502), "upstream"),
    (ReadTimeoutError(None, "http://internal.host/x", "Read timed out."), "upstream"),
    (MaxRetryError(None, "http://internal.host/x", NewConnectionError(None, "refused")), "upstream"),
    (json.JSONDecodeError("Expecting value", "<html>", 0), "upstream"),
    (KeyError("internal.host"), "internal"),
])
def test_each_exception_maps_to_one_error_code(exc, code):
    error = failure(exc).error
    assert error.code == code
    assert "internal.host" not in error.message


def test_an_api_error_keeps_its_status_and_body():
    error = failure(api_exception(400, '{"message":"unknown field: foo"}')).error
    assert error.message == "Radiant API error 400: error"
    assert error.details == {"status": 400, "body": '{"message":"unknown field: foo"}'}

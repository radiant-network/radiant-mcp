"""Radiant tools registered on the upstream FastMCP instance.

``describe_schema`` is always registered. The tools backed by the Radiant
portal API are registered only when ``RADIANT_API_URL`` is set, so the server
still boots as a pure StarRocks MCP server without it.
"""

from ._api import api_enabled
from .cases import get_case_context, list_tenants, search_cases
from .occurrences import case_cnv, prioritize_snv
from .patients import get_patient_context, search_patients
from .schema import describe_schema
from .variants import variant_carriers, variant_details

API_TOOLS = (
    list_tenants,
    search_cases,
    get_case_context,
    search_patients,
    get_patient_context,
    prioritize_snv,
    case_cnv,
    variant_details,
    variant_carriers,
)


def register_tools(mcp) -> list[str]:
    """Register the Radiant tools on ``mcp`` and return their names."""
    tools = [describe_schema]
    if api_enabled():
        # Fail fast at boot if the client isn't installed rather than on first call.
        import radiant_python  # noqa: F401

        tools += API_TOOLS
    for tool in tools:
        mcp.tool(tool)
    return [tool.__name__ for tool in tools]

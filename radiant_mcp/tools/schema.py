"""The ``describe_schema`` tool: curated StarRocks documentation for SQL."""

from fastmcp.tools import ToolResult

from ..results import error, ok, respond
from ..schema_catalog import TOPICS, render


async def describe_schema(topic: str) -> ToolResult:
    """Describe the StarRocks tables of one topic, for writing SQL.

    Call this BEFORE any ``read_query``, and only when no Radiant tool
    (get_case_context, prioritize_snv, case_cnv, variant_details,
    variant_carriers, search_patients) answers the question. The tenant
    database is ``<tenant>_tenant`` (e.g. ``cbtn_tenant``); never guess another name. It returns, for the topic: the
    tables to use, their keys and partitions, the useful columns with the
    stored enum values (case-sensitive), the join and partition rules, and
    example queries.

    Args:
        topic: One of germline_snv, somatic_snv, cnv, annotations, frequencies, clinical
            (patient, diagnosis, survival and treatment views, e.g. protocols).
    """
    if topic not in TOPICS:
        return respond(error("invalid_input", f"Unknown topic {topic!r}.", allowed=list(TOPICS)))
    return respond(ok({"topic": topic, "documentation": render(topic)}))

"""Trim the tool list inherited from ``mcp-server-starrocks``.

The upstream package registers its tools on import. The assistant keeps only
the read-only SQL tools it needs as a last resort (``read_query``,
``table_overview``); the others write, dump internals or run model-written
Python, and ``describe_schema`` replaces ``db_summary`` for schema discovery.
"""

import logging

logger = logging.getLogger(__name__)

HIDDEN_UPSTREAM_TOOLS = (
    "write_query",
    "db_summary",
    "analyze_query",
    "collect_query_dump_and_profile",
    "query_and_plotly_chart",
)


def hide_upstream_tools(mcp) -> list[str]:
    """Remove the hidden upstream tools from ``mcp``; return the removed names.

    A name the upstream version does not register is skipped with a warning,
    so an upstream rename never blocks the boot.
    """
    removed = []
    for name in HIDDEN_UPSTREAM_TOOLS:
        try:
            mcp.local_provider.remove_tool(name)
        except KeyError:
            logger.warning("upstream tool %s not found, nothing to hide", name)
            continue
        removed.append(name)
    return removed

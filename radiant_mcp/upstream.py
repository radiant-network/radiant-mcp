"""Trim what the assistant inherits from ``mcp-server-starrocks``.

The upstream package registers its tools and resources on import. The
assistant keeps only the read-only SQL tools it needs as a last resort
(``read_query``, ``table_overview``); the other tools write, dump internals or
run model-written Python, and ``describe_schema`` replaces ``db_summary`` for
schema discovery. The resources (database and table listings, ``show proc``)
are hidden too: the assistant has no use for them.

``table_overview`` caches its output (sample rows included) in a module-level
dict keyed by ``(db, table)`` only, so one user's rows would be served to the
next user without StarRocks checking their grants. The cache is replaced by
one that never holds anything.
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

HIDDEN_UPSTREAM_RESOURCES = ("starrocks:///databases",)

HIDDEN_UPSTREAM_TEMPLATES = (
    "starrocks:///{db}/{table}/schema",
    "starrocks:///{db}/tables",
    "proc:///{path*}",
)


def hide_upstream_tools(mcp) -> list[str]:
    """Remove the hidden upstream tools and resources from ``mcp``; return the removed names.

    A name the upstream version does not register is skipped with a warning,
    so an upstream rename never blocks the boot.
    """
    provider = mcp.local_provider
    removed = []
    for remove, names in (
        (provider.remove_tool, HIDDEN_UPSTREAM_TOOLS),
        (provider.remove_resource, HIDDEN_UPSTREAM_RESOURCES),
        (provider.remove_template, HIDDEN_UPSTREAM_TEMPLATES),
    ):
        for name in names:
            try:
                remove(name)
            except KeyError:
                logger.warning("upstream component %s not found, nothing to hide", name)
                continue
            removed.append(name)
    return removed


class _NoCache(dict):
    """A dict that forgets every write, so every lookup misses."""

    def __setitem__(self, key, value):
        pass


def disable_table_overview_cache(sr_server) -> None:
    """Make ``table_overview`` query StarRocks under the caller's identity on every call."""
    sr_server.global_table_overview_cache = _NoCache()

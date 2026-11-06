"""``describe_schema`` against the ETL, which owns the StarRocks schema.

The ETL checkout comes from ``RADIANT_ETL_DIR``, or a sibling ``etl`` /
``radiant-portal-pipeline`` folder (the hub layout). Without one, the ETL
checks are skipped, unless ``RADIANT_REQUIRE_ETL=1`` (CI), where they fail.
"""

import ast
import asyncio
import os
import re
from pathlib import Path

import pytest

from radiant_mcp.enums import ENUMS, FIELD_ENUMS
from radiant_mcp.schema_catalog import ARRAY_RULE, SHARED_DB, TENANT_DB, TOPICS, render
from radiant_mcp.tools.schema import describe_schema

REPO = Path(__file__).resolve().parents[2]
_MAPPINGS_SHARED = ("STARROCKS_RADIANT_BASE_MAPPING", "STARROCKS_OPEN_DATA_MAPPING", "CLINICAL_TRANSFORM_LAYER_MAPPING")
_MAPPING_PER_TENANT = "STARROCKS_RADIANT_PER_TENANT_MAPPING"
_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\{\{\s*mapping\.(\w+)\s*\}\}\s*\(", re.I,
)
_TABLE_REF = re.compile(r"(<tenant>_tenant|\bradiant)\.(\w+)")


def _etl_dir() -> Path | None:
    candidates = [os.getenv("RADIANT_ETL_DIR"), REPO.parent / "etl", REPO.parent / "radiant-portal-pipeline"]
    for candidate in candidates:
        if candidate and (Path(candidate) / "radiant" / "dags" / "sql").is_dir():
            return Path(candidate)
    return None


@pytest.fixture(scope="module")
def etl() -> Path:
    path = _etl_dir()
    if path is None:
        if os.getenv("RADIANT_REQUIRE_ETL") == "1":
            pytest.fail("RADIANT_REQUIRE_ETL=1 but no ETL checkout found (set RADIANT_ETL_DIR)")
        pytest.skip("no ETL checkout: set RADIANT_ETL_DIR to check describe_schema against the ETL")
    return path


def _ddl_columns(sql: str) -> tuple[str, list[str]] | None:
    """``(mapping key, column names)`` of a create-table statement.

    Scans the column list with paren depth and quotes, so commas in a COMMENT
    or a type (``DECIMAL(10, 2)``, ``ARRAY<STRUCT<...>>``) do not split it.
    """
    sql = re.sub(r"--[^\n]*", "", sql)
    match = _CREATE.search(sql)
    if not match:
        return None
    depth, quote, current, parts = 1, None, "", []
    for ch in sql[match.end():]:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            parts.append(current)
            current = ""
            continue
        current += ch
    parts.append(current)
    columns = []
    for part in parts:
        name = re.match(r"\s*`?([A-Za-z_]\w*)`?\s+\S", part)
        if name and name.group(1).upper() not in ("INDEX", "PRIMARY", "KEY", "CONSTRAINT", "UNIQUE"):
            columns.append(name.group(1))
    return match.group(1), columns


def _mappings(etl: Path) -> dict[str, str]:
    """Mapping key -> qualified table name, from the ETL's table registry."""
    tree = ast.parse((etl / "radiant" / "tasks" / "data" / "radiant_tables.py").read_text())
    wanted = (_MAPPING_PER_TENANT, *_MAPPINGS_SHARED)
    dicts = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted
    }
    out = {key: f"{TENANT_DB}.{table}" for key, table in dicts[_MAPPING_PER_TENANT].items()}
    for name in _MAPPINGS_SHARED:
        out.update({key: f"{SHARED_DB}.{table}" for key, table in dicts[name].items()})
    return out


@pytest.fixture(scope="module")
def etl_tables(etl) -> dict[str, set[str]]:
    mappings = _mappings(etl)
    tables = {}
    for path in (etl / "radiant" / "dags" / "sql").glob("*/init/*_create_table.sql"):
        parsed = _ddl_columns(path.read_text())
        if parsed and parsed[0] in mappings:
            tables[mappings[parsed[0]]] = set(parsed[1])
    assert tables, "no create-table DDL found in the ETL checkout"
    return tables


@pytest.fixture(scope="module")
def etl_dictionaries(etl) -> dict[str, list[str]]:
    text = (etl / "radiant" / "data_qa" / "macros" / "dictionaries.sql").read_text()
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return {
        name: ast.literal_eval(values)
        for name, values in re.findall(r"macro\s+dict_(\w+)\(\)\s*%\}\s*\{\{\s*return\((\[.*?\])\)", text, re.S)
    }


def _catalog_tables():
    seen = set()
    for topic_name, topic in TOPICS.items():
        for table in topic.tables:
            if (table.qualified, tuple(table.columns)) not in seen:
                seen.add((table.qualified, tuple(table.columns)))
                yield topic_name, table


@pytest.mark.parametrize("topic_name, table", list(_catalog_tables()), ids=lambda v: getattr(v, "qualified", v))
def test_catalog_tables_and_columns_exist_in_the_etl(etl_tables, topic_name, table):
    assert table.qualified in etl_tables, f"{topic_name}: {table.qualified} is not created by the ETL"
    missing = sorted(set(table.columns) - etl_tables[table.qualified])
    assert not missing, f"{table.qualified}: columns not in the ETL DDL: {missing}"
    assert set(table.enums) <= set(table.columns)


@pytest.mark.parametrize("name", sorted(ENUMS))
def test_enums_match_the_etl_dictionaries(etl_dictionaries, name):
    assert name in etl_dictionaries, f"no dict_{name} macro in the ETL data QA dictionaries"
    assert sorted(ENUMS[name]) == sorted(etl_dictionaries[name])


def test_enum_references_are_known():
    assert set(FIELD_ENUMS.values()) <= set(ENUMS)
    for _, table in _catalog_tables():
        assert set(table.enums.values()) <= set(ENUMS)


@pytest.mark.parametrize("topic_name", sorted(TOPICS))
def test_example_queries_only_use_tables_of_the_catalog(topic_name):
    known = {table.qualified for topic in TOPICS.values() for table in topic.tables}
    for title, sql in TOPICS[topic_name].queries:
        for db, name in _TABLE_REF.findall(sql):
            assert f"{db}.{name}" in known, f"{topic_name} / {title}: {db}.{name} is not in the catalog"


@pytest.mark.parametrize("topic_name", sorted(TOPICS))
def test_render(topic_name):
    text = render(topic_name)
    assert text.startswith(f"# {TOPICS[topic_name].title}\n")
    for table in TOPICS[topic_name].tables:
        assert f"### {table.qualified}" in text


@pytest.mark.parametrize("topic_name", sorted(TOPICS))
def test_topics_with_array_columns_explain_unnest(topic_name):
    topic = TOPICS[topic_name]
    if any("(array" in d for table in topic.tables for d in table.columns.values()):
        assert ARRAY_RULE in topic.rules
        assert "unnest(" in render(topic_name)


def test_render_prints_the_stored_enum_values():
    assert "'likely_pathogenic'" in render("germline_snv")


def test_describe_schema_tool():
    assert asyncio.run(describe_schema("cnv"))["documentation"] == render("cnv")
    assert asyncio.run(describe_schema("nope"))["topics"] == list(TOPICS)

"""``describe_schema`` against the owners of the StarRocks schema: the ETL
for the tables, the portal for the clinical views.

The ETL checkout comes from ``RADIANT_ETL_DIR``, or a sibling ``etl`` /
``radiant-portal-pipeline`` folder (the hub layout); the portal checkout from
``RADIANT_PORTAL_DIR``, or a sibling ``portal`` / ``radiant-portal`` folder.
Without one, its checks are skipped, unless ``RADIANT_REQUIRE_ETL=1`` /
``RADIANT_REQUIRE_PORTAL=1`` (CI), where they fail.
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
_VIEW = re.compile(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+\S+?_tenant\.(\w+)\s+AS\s+SELECT\b", re.I)
_PORTAL_VIEW_DIRS = ("backend/scripts/seed/views", "backend/scripts/pcx_tables/access_controlled_views")


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


def _portal_dir() -> Path | None:
    candidates = [os.getenv("RADIANT_PORTAL_DIR"), REPO.parent / "portal", REPO.parent / "radiant-portal"]
    for candidate in candidates:
        if candidate and all((Path(candidate) / d).is_dir() for d in _PORTAL_VIEW_DIRS):
            return Path(candidate)
    return None


@pytest.fixture(scope="module")
def portal() -> Path:
    path = _portal_dir()
    if path is None:
        if os.getenv("RADIANT_REQUIRE_PORTAL") == "1":
            pytest.fail("RADIANT_REQUIRE_PORTAL=1 but no portal checkout found (set RADIANT_PORTAL_DIR)")
        pytest.skip("no portal checkout: set RADIANT_PORTAL_DIR to check the clinical views against the portal")
    return path


def _top_level_split(text: str) -> list[str]:
    depth, quote, current, parts = 0, None, "", []
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += ch
    return parts + [current]


def _view_columns(sql: str) -> tuple[str, list[str]] | None:
    """``(view name, column names)`` of a create-view statement: the select list up to its top-level FROM."""
    sql = re.sub(r"/\*.*?\*/", "", re.sub(r"--[^\n]*", "", sql), flags=re.S)
    match = _VIEW.search(sql)
    if not match:
        return None
    body = sql[match.end():]
    depth, end = 0, len(body)
    for m in re.finditer(r"\(|\)|\bFROM\b", body, re.I):
        token = m.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            end = m.start()
            break
    columns = []
    for item in _top_level_split(body[:end]):
        names = re.findall(r"[A-Za-z_]\w*", item.split(" AS ")[-1] if " AS " in item.upper() else item)
        alias = re.search(r"\bAS\s+([A-Za-z_]\w*)\s*$", item.strip(), re.I)
        columns.append(alias.group(1) if alias else names[-1])
    return match.group(1), columns


@pytest.fixture(scope="module")
def portal_views(portal) -> dict[str, set[str]]:
    views = {}
    for directory in _PORTAL_VIEW_DIRS:
        for path in (portal / directory).glob("*.sql.tmpl"):
            parsed = _view_columns(path.read_text())
            if parsed:
                views[f"{TENANT_DB}.{parsed[0]}"] = set(parsed[1])
    assert views, "no view definition found in the portal checkout"
    return views


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


def _owned_by(owner: str):
    return [(name, table) for name, table in _catalog_tables() if table.owner == owner]


def test_every_table_has_a_known_owner():
    assert {table.owner for _, table in _catalog_tables()} == {"etl", "portal"}


@pytest.mark.parametrize("topic_name, table", _owned_by("portal"), ids=lambda v: getattr(v, "qualified", v))
def test_clinical_views_and_columns_exist_in_the_portal(portal_views, topic_name, table):
    assert table.qualified in portal_views, f"{topic_name}: {table.qualified} is not created by the portal"
    missing = sorted(set(table.columns) - portal_views[table.qualified])
    assert not missing, f"{table.qualified}: columns not in the portal view: {missing}"


@pytest.mark.parametrize("topic_name, table", _owned_by("etl"), ids=lambda v: getattr(v, "qualified", v))
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

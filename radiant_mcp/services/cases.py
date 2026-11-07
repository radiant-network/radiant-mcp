"""The case context: the case, its sequencings and the occurrence keys of its tasks.

``get_case_context`` returns it, and ``second_hit`` and ``family_venn`` build
on it. Each key carries its StarRocks ``part`` / ``variant_part``. TEMPORARY:
``part`` is read from ``staging_sequencing_experiment`` (``lookup_parts``)
until the Radiant API returns it with each task.
"""


from .. import sql as _sql
from ..domain.data_types import CASE_DATA_TYPES, GERMLINE_CASE_DATA_TYPES, SOMATIC_CASE_DATA_TYPES
from ..domain.keys import variant_part
from ..portal_api import RadiantApi, resolve_tenant
from ..portal_links import case_url, occurrence_url, patient_url, with_url
from ..queries.sequencing import parts_query
from ..results import describe


def search_criteria(filters: dict) -> list:
    """``{field: value | [values]}`` -> ``SearchCriterion`` list (default op: in)."""
    from radiant_python.models.search_criterion import SearchCriterion

    out = []
    for field, value in filters.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        out.append(SearchCriterion(field=field, value=list(values)))
    return out


def merge_criteria(filters: dict, extra: dict) -> dict | None:
    """``filters`` AND ``extra``; None when a field of both has no value in common.

    The API takes one criterion per field, so the user's filter and an
    autocomplete match on the same field are intersected, not overwritten.
    """
    merged = dict(filters)
    for field, value in extra.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        if field in merged:
            wanted = merged[field] if isinstance(merged[field], (list, tuple)) else [merged[field]]
            keep = {str(v) for v in wanted}
            values = [v for v in values if str(v) in keep]
            if not values:
                return None
        merged[field] = list(values)
    return merged


def lookup_parts(token: str, case_id: int) -> tuple[dict[tuple[int, int], int], str | None]:
    """Map every (seq_id, task_id) of ``case_id`` to its StarRocks ``part``.

    Reads the shared ``staging_sequencing_experiment`` table under the
    caller's identity, like every other StarRocks access. Returns
    ``(mapping, warning)``; on any failure the mapping is empty and the
    warning explains why, so the case context is still returned.
    """
    try:
        rows = _sql.run(token, *parts_query(case_id))
    except Exception as exc:  # noqa: BLE001
        return {}, f"part lookup failed: {exc}"
    return {(int(r["seq_id"]), int(r["task_id"])): int(r["part"]) for r in rows if r["part"] is not None}, None


def case_context(api: RadiantApi, token: str, case_id: int, tenant: str | None) -> dict:
    """``tenant``, ``case``, ``occurrence_keys`` and ``warnings`` (a list, maybe empty).

    Raises ``ToolFailure`` when the tenant cannot be chosen, and the API error
    when the case cannot be read.
    """
    tenant_code = resolve_tenant(api, tenant)

    case = api.call("cases", "case_entity", tenant=tenant_code, case_id=case_id) or {}
    with_url(case, case_url(tenant_code, case_id))
    for member in case.get("members", []):
        with_url(member, patient_url(tenant_code, member.get("patient_key")))

    case_type = (case.get("case_type") or "").lower()
    if case_type == "germline":
        data_types = GERMLINE_CASE_DATA_TYPES
    elif case_type == "somatic":
        data_types = SOMATIC_CASE_DATA_TYPES
    else:
        data_types = CASE_DATA_TYPES

    # For every sequencing experiment that has variants, find the task(s)
    # that produced occurrences of each data type. (case_id, seq_id,
    # task_id, data_type) is the key needed by every occurrence endpoint
    # and by the StarRocks occurrence tables.
    occurrence_keys: list[dict] = []
    warnings: list[str] = []
    for seq in case.get("sequencing_experiments", []):
        seq_tasks: list[dict] = []
        if seq.get("has_variants"):
            for data_type in data_types:
                try:
                    tasks = api.call(
                        "cases", "case_tasks_with_occurrences",
                        tenant=tenant_code, case_id=case_id, seq_id=seq["seq_id"], data_type=data_type,
                    )
                except Exception as exc:  # noqa: BLE001 — surface, don't abort
                    warnings.append(
                        f"seq_id={seq['seq_id']} data_type={data_type}: {describe(exc)}"
                    )
                    continue
                for task in tasks or []:
                    entry = {
                        "case_id": case_id,
                        "seq_id": seq["seq_id"],
                        "task_id": task.get("id"),
                        "data_type": data_type,
                        "task_type_code": task.get("task_type_code"),
                        "task_type_name": task.get("task_type_name"),
                        "pipeline_name": task.get("pipeline_name"),
                        "pipeline_version": task.get("pipeline_version"),
                        "genome_build": task.get("genome_build"),
                        "created_on": task.get("created_on"),
                    }
                    entry = {k: v for k, v in entry.items() if v is not None}
                    with_url(entry, occurrence_url(
                        tenant_code, case_id, seq["seq_id"], data_type, task_id=entry.get("task_id"),
                    ))
                    seq_tasks.append(entry)
                    occurrence_keys.append(entry)
        seq["occurrence_tasks"] = seq_tasks

    # Attach the StarRocks partition of every task. `entry` dicts are shared
    # between `occurrence_keys` and `seq["occurrence_tasks"]`, so mutating
    # them in place updates both views.
    if occurrence_keys:
        parts, part_warning = lookup_parts(token, case_id)
        if part_warning:
            warnings.append(part_warning)
        for entry in occurrence_keys:
            part = parts.get((entry["seq_id"], entry.get("task_id")))
            if part is None:
                if not part_warning:
                    warnings.append(
                        f"seq_id={entry['seq_id']} task_id={entry.get('task_id')}: "
                        "no `part` found in staging_sequencing_experiment"
                    )
                continue
            entry["part"] = part
            entry["variant_part"] = variant_part(part)

    return {
        "tenant": tenant_code,
        "case": case,
        # Flat list of every (case_id, seq_id, task_id, data_type) tuple —
        # the handle for follow-up variant queries.
        "occurrence_keys": occurrence_keys,
        "warnings": warnings,
    }

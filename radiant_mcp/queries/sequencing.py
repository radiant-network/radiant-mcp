"""The shared ``staging_sequencing_experiment`` table: the only StarRocks table
that maps an occurrence's (seq_id, task_id) to its case, patient and ``part``."""

from .. import sql as _sql


def sequencing_join(p: _sql.Params, tenant: str, source: str = "x") -> str:
    """Join ``source`` (with seq_id / task_id) to its live sequencing experiment row ``s``, in ``tenant``."""
    return (
        f"JOIN {_sql.shared_db()}.staging_sequencing_experiment s"
        f" ON s.seq_id = {source}.seq_id AND s.task_id = {source}.task_id"
        f" AND s.tenant_code = {p(tenant)} AND s.deleted = false"
    )


def parts_query(case_id: int) -> tuple[str, _sql.Params]:
    p = _sql.Params()
    return (f"SELECT seq_id, task_id, part FROM {_sql.shared_db()}.staging_sequencing_experiment"
            f" WHERE case_id = {p(int(case_id))} AND deleted = false"), p

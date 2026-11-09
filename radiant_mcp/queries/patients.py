"""SQL of the clinical patient list read by ``survival_curve``.

The portal's ``search_patients`` reads the secured view ``v_pcx_30_patient_list``
as the caller, through a subquery that adds two virtual columns
(``patient_name``, ``age_at_initial_dx_group``), and turns each filter into
``field IN (...)``, or ``arrays_overlap`` for an array field
(``PcxPatientsRepository.SearchPatients``, ``types.filter.go``). This is the same
read, in one statement instead of pages of 100 through the API. The age groups
mirror ``ageAtInitialDxGroupSQL`` of the portal's ``pcx_patients.go``: a test
compares them, so a change on either side fails.
"""

from typing import Any

from .. import sql as _sql
from ..api_fields import PATIENT_FILTER_FIELDS

# Patients read per list; one more is fetched to tell a cut list.
PATIENT_LIST_ROW_LIMIT = 50000

_COLUMNS = ("patient_key", "survival_days", "vital_status", "radiant_patient_id", "case_count")
# Filter fields that are arrays in the view (types.PcxPatientListFields, IsArray).
ARRAY_FILTER_FIELDS = ("chemotherapy_agents",)
# The view columns the filters and the virtual columns read.
_SOURCE_COLUMNS = ("patient_id", "organization_code", "diagnosis_type_cohort", "cns_integrated_diagnosis",
                   "vital_status", "got_radiation_ever", "chemotherapy_agents", "can_read_phi", "given_name",
                   "family_name", "age_at_initial_dx_days")

# The portal's ageAtInitialDxGroupSQL, condition by condition: (condition, group), then the ELSE group.
AGE_AT_INITIAL_DX_GROUPS = (
    ("age_at_initial_dx_days IS NULL", "unknown"),
    ("age_at_initial_dx_days = 0", "congenital"),
    ("age_at_initial_dx_days < 28", "neonatal"),
    ("age_at_initial_dx_days < 365.25", "infantile"),
    ("age_at_initial_dx_days < 5 * 365.25", "childhood"),
    ("age_at_initial_dx_days < 16 * 365.25", "juvenile"),
    ("age_at_initial_dx_days < 40 * 365.25", "young_adult"),
    ("age_at_initial_dx_days < 60 * 365.25", "middle_age"),
)
AGE_AT_INITIAL_DX_ELSE = "senior"


def age_at_initial_dx_group_sql(p: _sql.Params | None) -> str:
    """The CASE expression, its groups bound through ``p``, or written as literals
    when ``p`` is None (to compare with the portal's text)."""
    def value(group: str) -> str:
        return f"'{group}'" if p is None else p(group)

    whens = " ".join(f"WHEN {condition} THEN {value(group)}" for condition, group in AGE_AT_INITIAL_DX_GROUPS)
    return f"CASE {whens} ELSE {value(AGE_AT_INITIAL_DX_ELSE)} END"


def patient_list_query(tenant: str, filters: dict[str, Any], limit: int) -> tuple[str, _sql.Params]:
    """``(patient_key, survival_days, vital_status, radiant_patient_id, case_count)`` of the
    patients matching ``filters`` (search_patients filters, AND-ed), by patient key."""
    p = _sql.Params()
    tenant_db = _sql.tenant_db(tenant)
    where = []
    for field, value in filters.items():
        # The field is written into the SQL: only the portal's filter fields.
        _sql.one_of(field, PATIENT_FILTER_FIELDS, "patient filter field")
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if not values or any(not isinstance(v, (str, int, float, bool)) for v in values):
            raise _sql.InvalidInput(f"patient filter {field}: give a value or a list of values")
        if field in ARRAY_FILTER_FIELDS:
            where.append(f"arrays_overlap(pl.{field}, [{p.list(values)}])")
        else:
            where.append(f"pl.{field} IN ({p.list(values)})")
    # patient_name exists only where the caller can read PHI, as in the portal.
    inner = (
        f"SELECT patient_key, survival_days, radiant_patient_id, case_count, {', '.join(_SOURCE_COLUMNS)},"
        f" CASE WHEN can_read_phi THEN concat(given_name, {p(' ')}, family_name) END AS patient_name,"
        f" {age_at_initial_dx_group_sql(p)} AS age_at_initial_dx_group"
        f" FROM {tenant_db}.v_pcx_30_patient_list"
    )
    sql = (
        f"SELECT {', '.join(f'pl.{c}' for c in _COLUMNS)} FROM ({inner}) pl"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY pl.patient_key LIMIT {p(limit)}"
    )
    return sql, p

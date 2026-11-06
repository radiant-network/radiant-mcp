"""Links to the Radiant portal pages, returned by the tools as ``portal_url``.

The model must never build a portal URL itself (it would guess ids, and the
patient page key is an opaque UUID), so every case, patient and variant a tool
returns carries a ready-made link. Links are only produced when
``RADIANT_PORTAL_URL`` is set; otherwise every function returns ``None`` and the
tools omit the field.

The routes mirror the portal frontend (``frontend/portals/radiant/config/
routes.module.ts`` and the case variants tab URL parameters). They are a
cross-module boundary: when the portal renames a route or a parameter, update
this module and its tests.
"""

import os
from typing import Optional
from urllib.parse import quote, urlencode

PORTAL_URL_ENV = "RADIANT_PORTAL_URL"

# Occurrence data type -> `variant_section` value of the case variants tab.
_VARIANT_SECTIONS = {
    "germline_snv": "snv",
    "germline_cnv": "cnv",
    "somatic_snv_tn": "snv-tn",
    "somatic_snv_to": "snv-to",
    "somatic_cnv": "cnv-to",
}
# Data types whose occurrence side panel opens from the `selectedVariant` param.
_SNV_DATA_TYPES = ("germline_snv", "somatic_snv_tn", "somatic_snv_to")

CASE_TABS = ("details", "variants", "files")
PATIENT_TABS = (
    "overview", "timeline", "treatments", "genomics", "tumor-board",
    "laboratory", "imaging", "clinical-trials",
)


def _base() -> Optional[str]:
    base = os.getenv(PORTAL_URL_ENV, "").strip()
    return base.rstrip("/") or None


def _build(tenant: Optional[str], path: str, params: Optional[dict] = None) -> Optional[str]:
    base = _base()
    if not base or not tenant:
        return None
    url = f"{base}/{quote(str(tenant), safe='')}/{path}"
    query = {k: v for k, v in (params or {}).items() if v is not None}
    return f"{url}?{urlencode(query)}" if query else url


def case_url(tenant: Optional[str], case_id, tab: Optional[str] = None) -> Optional[str]:
    if case_id is None:
        return None
    if tab is not None and tab not in CASE_TABS:
        raise ValueError(f"unknown case tab: {tab}")
    return _build(tenant, f"case/entity/{quote(str(case_id), safe='')}", {"tab": tab})


def occurrence_url(
    tenant: Optional[str],
    case_id,
    seq_id,
    data_type: str,
    task_id=None,
    variant_id: Optional[str] = None,
) -> Optional[str]:
    """Case variants tab on one sequencing and data type.

    For an SNV, ``variant_id`` (the ``locus_id``) opens its occurrence side
    panel. CNVs have no such panel: the link opens the CNV table. A missing
    ``task_id`` is fine, the portal then selects the sequencing's first task.
    """
    if case_id is None or seq_id is None or data_type not in _VARIANT_SECTIONS:
        return None
    params = {
        "tab": "variants",
        "seq_id": seq_id,
        "task_id": task_id,
        "variant_section": _VARIANT_SECTIONS[data_type],
        "selectedVariant": variant_id if data_type in _SNV_DATA_TYPES else None,
    }
    return _build(tenant, f"case/entity/{quote(str(case_id), safe='')}", params)


def patient_url(tenant: Optional[str], patient_key: Optional[str], tab: Optional[str] = None) -> Optional[str]:
    if not patient_key:
        return None
    if tab is not None and tab not in PATIENT_TABS:
        raise ValueError(f"unknown patient tab: {tab}")
    return _build(tenant, f"patient/entity/{quote(str(patient_key), safe='')}", {"tab": tab})


def variant_url(tenant: Optional[str], locus_id) -> Optional[str]:
    if locus_id is None or locus_id == "":
        return None
    return _build(tenant, f"variants/entity/{quote(str(locus_id), safe='')}")


def with_url(item: dict, url: Optional[str], key: str = "portal_url") -> dict:
    """Set ``item[key]`` when ``url`` is not None, and return ``item``."""
    if url is not None:
        item[key] = url
    return item

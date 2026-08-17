"""
Jurisdiction config registry.
Maps jurisdiction_id strings to their config objects.

Types listed in ENRICHMENT_BY_NUMBER are gated by docket_number: only those
numbers enrich, and each number maps to its own registry key. Types not in
that map use the type-only lookup in enrich_entry._DOCKET_TYPE_TO_JURISDICTION.
"""

from typing import Dict, Optional

from .stb import STB_CONFIG
from .mt_psc import MT_PSC_CONFIG
from .sd_puc import SD_PUC_CONFIG
from .nm_prc import NM_PRC_CONFIG
from .ne_psc import NE_PSC_CONFIG
from .va_puc import VA_PUC_CONFIG
from .cpuc import CPUC_CONFIG
from .nj_bpu import NJ_BPU_CONFIG
from .fcc_gsat import FCC_GSAT_CONFIG
from .fcc_dbrg_zayo import FCC_DBRG_ZAYO_CONFIG
from .fcc_dbrg_wow import FCC_DBRG_WOW_CONFIG

JURISDICTION_REGISTRY = {
    "stb": STB_CONFIG,
    "mt-psc": MT_PSC_CONFIG,
    "sd-puc": SD_PUC_CONFIG,
    "nm-prc": NM_PRC_CONFIG,
    "ne-psc": NE_PSC_CONFIG,
    "va-puc": VA_PUC_CONFIG,
    "cpuc": CPUC_CONFIG,
    "nj-bpu": NJ_BPU_CONFIG,
    "fcc-gsat": FCC_GSAT_CONFIG,
    "fcc-dbrg-zayo": FCC_DBRG_ZAYO_CONFIG,
    "fcc-dbrg-wow": FCC_DBRG_WOW_CONFIG,
}

# mongo metadata.docket_type -> { docket_number -> registry key }
# Adding a 4th FCC (or 2nd CPUC) docket is one line here.
ENRICHMENT_BY_NUMBER: Dict[str, Dict[str, str]] = {
    "fcc-ecfs": {
        "26-134": "fcc-gsat",
        "26-56": "fcc-dbrg-zayo",
        "26-40": "fcc-dbrg-wow",
    },
    "CPUC": {
        "A2507016": "cpuc",
    },
}


def get_number_map(docket_type: str) -> Optional[Dict[str, str]]:
    """Return the number→config map for a Mongo docket_type, or None if type-only."""
    dtype = (docket_type or "").strip()
    if not dtype:
        return None
    if dtype in ENRICHMENT_BY_NUMBER:
        return ENRICHMENT_BY_NUMBER[dtype]
    lowered = dtype.lower()
    for key, mapping in ENRICHMENT_BY_NUMBER.items():
        if key.lower() == lowered:
            return mapping
    return None


def get_config(jurisdiction_id: str):
    """Look up a jurisdiction config by ID. Raises KeyError if not found."""
    if jurisdiction_id not in JURISDICTION_REGISTRY:
        available = ", ".join(sorted(JURISDICTION_REGISTRY.keys()))
        raise KeyError(
            f"Unknown jurisdiction '{jurisdiction_id}'. Available: {available}"
        )
    return JURISDICTION_REGISTRY[jurisdiction_id]

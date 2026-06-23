"""
Jurisdiction config registry.
Maps jurisdiction_id strings to their config objects.
"""

from .stb import STB_CONFIG
from .mt_psc import MT_PSC_CONFIG
from .sd_puc import SD_PUC_CONFIG
from .nm_prc import NM_PRC_CONFIG
from .ne_psc import NE_PSC_CONFIG

JURISDICTION_REGISTRY = {
    "stb": STB_CONFIG,
    "mt-psc": MT_PSC_CONFIG,
    "sd-puc": SD_PUC_CONFIG,
    "nm-prc": NM_PRC_CONFIG,
    "ne-psc": NE_PSC_CONFIG,
}


def get_config(jurisdiction_id: str):
    """Look up a jurisdiction config by ID. Raises KeyError if not found."""
    if jurisdiction_id not in JURISDICTION_REGISTRY:
        available = ", ".join(sorted(JURISDICTION_REGISTRY.keys()))
        raise KeyError(
            f"Unknown jurisdiction '{jurisdiction_id}'. Available: {available}"
        )
    return JURISDICTION_REGISTRY[jurisdiction_id]

"""
deal_match_regex.py — Shared regex fallback deal-matching engine.

Two matching strategies:

  1. regex_match_split_orient — titles with a left/right separator (ACCC, FTC, UK CMA)
  2. regex_match_flat_scan   — flat party lists with no reliable split (CADE, BKA, Canada, NZ, EC, FS, SAMR)

Regulator wrappers handle title parsing; core logic lives here.

Usage:
    from deal_match_regex import regex_match_deal_by_title, apply_regex_match_subject

    deal_id = regex_match_deal_by_title("AkzoNobel - Axalta", deals)
    subject = apply_regex_match_subject(subject, matched_by_regex=True)
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Pattern

# ---------------------------------------------------------------------------
# Legal-suffix patterns (jurisdiction-specific)
# ---------------------------------------------------------------------------

DEFAULT_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|plc|llc|lp|l\.p|ltd|limited|"
    r"holdings|group|co|company|nv|ag|se|gmbh|trust|fund|partners|"
    r"foundation|pbc|pty)\b",
    re.IGNORECASE,
)

ACCC_SUFFIXES = re.compile(
    r"\b(pty|ltd|limited|inc|incorporated|corp|corporation|"
    r"plc|llc|holdings|group|co|company|aust|australia|nv|sa|ag|se|gmbh)\b",
    re.IGNORECASE,
)

CADE_SUFFIXES = re.compile(
    r"\b(ltda|sa|s\.a|s\.a\.|lda|inc|incorporated|corp|corporation|"
    r"plc|llc|holdings|group|co|company|ltd|limited|nv|ag|se|gmbh|pbc)\b",
    re.IGNORECASE,
)

BKA_SUFFIXES = re.compile(
    r"\b(gmbh|ag|se|kg|kgaa|ohg|ug|inc|incorporated|corp|corporation|"
    r"plc|llc|lp|l\.p|ltd|limited|holdings|group|co|company|nv|sa|"
    r"trust|fund|partners|foundation|pbc|pty)\b",
    re.IGNORECASE,
)

CANADA_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|plc|llc|lp|l\.p|ltd|limited|"
    r"holdings|group|co|company|nv|ag|se|gmbh|trust|fund|partners|"
    r"foundation|pbc|pty|partnership)\b",
    re.IGNORECASE,
)

NZ_SUFFIXES = re.compile(
    r"\b(limited|ltd|inc|incorporated|corp|corporation|plc|llc|lp|l\.p|"
    r"holdings|group|co|company|nv|ag|se|gmbh|trust|fund|partners|"
    r"foundation|pbc|pty)\b",
    re.IGNORECASE,
)

EC_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|plc|llc|lp|l\.p|ltd|limited|"
    r"holdings|group|co|company|nv|ag|se|gmbh|sa|s\.a|s\.a\.|kg|kgaa|"
    r"bv|oy|ab|as|spa|sp|trust|fund|partners|foundation|pbc|pty)\b",
    re.IGNORECASE,
)

SAMR_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|plc|llc|lp|l\.p|ltd|limited|"
    r"holdings|group|co|company|nv|ag|se|gmbh|sa|s\.a|s\.a\.|spa|"
    r"trust|fund|partners|foundation|pbc|pty|partnership)\b",
    re.IGNORECASE,
)

TURKEY_SUFFIXES = re.compile(
    r"\b(a\.?ş|anonim|şirketi|limited|ltd|inc|incorporated|corp|corporation|"
    r"plc|llc|lp|l\.p|holdings|group|co|company|nv|ag|se|gmbh|sa|s\.a|"
    r"trust|fund|partners|foundation|pbc|pty)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Title separators (split-orient wrappers)
# ---------------------------------------------------------------------------

DASH_SEPARATOR = re.compile(r"\s*[–—\-]\s*")
FTC_SEMICOLON_SEP = re.compile(r"\s*;\s*")
UK_CMA_SLASH_SEP = re.compile(r"\s*/\s*")
UK_CMA_TRAILING = re.compile(
    r"\s+merger\s+inquir(y|ies)\s*$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def normalise_company_name(text: str, suffixes: Pattern[str] = DEFAULT_SUFFIXES) -> str:
    """Lowercase, strip legal suffixes and punctuation for fuzzy comparison."""
    text = text.lower()
    text = suffixes.sub("", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Generic placeholder words that are sometimes stored as aliases but must never
# be used for matching (they match too broadly across unrelated deals).
_GENERIC_ALIAS_BLOCKLIST: frozenset[str] = frozenset({
    "parent",
    "merger sub",
    "merger sub i",
    "merger sub ii",
    "merger subs",
    "buyer parties",
    "sponsors",
    "purchaser",
    "acquirer",
    "target",
    "company",
    "seller",
})


def name_hits_text(name: str, text: str) -> bool:
    """
    Returns True if the full normalised name appears as a substring in text.

    Matching direction: the deal name must be contained within the input text,
    not the other way around.  e.g. deal="Warner Bros", text="Warner Bros Discovery"
    → match; deal="Warner Bros Discovery", text="Warner Bros" → no match.
    """
    return name in text


def build_deal_name_sets(
    deal: Dict[str, Any],
    normalise: Callable[[str], str],
) -> tuple[set[str], set[str]]:
    """Build normalised acquirer-side and target-side name sets (main + aliases).

    Generic placeholder aliases (e.g. 'Parent', 'Merger Sub', 'Company') are
    filtered out before returning to prevent false-positive matches.
    """
    acq_names: set[str] = set()
    for f in [deal.get("acquirer"), deal.get("acquire_name")]:
        if f:
            acq_names.add(normalise(f))
    for alias in deal.get("parent_aliases") or []:
        if alias:
            normed = normalise(alias)
            if normed not in _GENERIC_ALIAS_BLOCKLIST:
                acq_names.add(normed)
    acq_names.discard("")

    tgt_names: set[str] = set()
    for f in [deal.get("target"), deal.get("target_name")]:
        if f:
            tgt_names.add(normalise(f))
    for alias in deal.get("target_aliases") or []:
        if alias:
            normed = normalise(alias)
            if normed not in _GENERIC_ALIAS_BLOCKLIST:
                tgt_names.add(normed)
    tgt_names.discard("")

    return acq_names, tgt_names


def apply_regex_match_subject(subject: str, matched_by_regex: bool) -> str:
    """Replace [FRMD] with [FRRMD] when match came from regex fallback."""
    if matched_by_regex:
        return subject.replace("[FRMD]", "[FRRMD]")
    return subject


# ---------------------------------------------------------------------------
# Core strategies
# ---------------------------------------------------------------------------


def regex_match_split_orient(
    left: str,
    right: str,
    deals: List[Dict[str, Any]],
    *,
    suffixes: Pattern[str] = DEFAULT_SUFFIXES,
    min_name_len: int = 2,
) -> Optional[str]:
    """
    Match pre-split left/right sides against deals (both orientations).

    Uses simple substring matching (a in left or left in a).
    Caller must pass already-normalised left and right strings.
    """
    if not left or not right or not deals:
        return None

    norm = lambda s: normalise_company_name(s, suffixes)

    for deal in deals:
        acq_names, tgt_names = build_deal_name_sets(deal, norm)
        if not acq_names or not tgt_names:
            continue

        match_normal = (
            any(a in left or left in a for a in acq_names if len(a) > min_name_len)
            and any(t in right or right in t for t in tgt_names if len(t) > min_name_len)
        )
        match_reversed = (
            any(t in left or left in t for t in tgt_names if len(t) > min_name_len)
            and any(a in right or right in a for a in acq_names if len(a) > min_name_len)
        )

        if match_normal or match_reversed:
            return deal.get("deal_id")

    return None


def regex_match_flat_scan(
    text: str,
    deals: List[Dict[str, Any]],
    *,
    suffixes: Pattern[str] = DEFAULT_SUFFIXES,
    min_name_len: int = 2,
) -> Optional[str]:
    """
    Flat-scan: require acquirer hit AND target hit anywhere in normalised text.

    Uses exact substring matching: the deal name must appear fully inside the
    input text (not the reverse).  e.g. deal="Warner Bros", text="Warner Bros
    Discovery" → match; deal="Warner Bros Discovery", text="Warner Bros" → no match.
    """
    if not text or not deals:
        return None

    norm = lambda s: normalise_company_name(s, suffixes)
    norm_text = norm(text)

    for deal in deals:
        acq_names, tgt_names = build_deal_name_sets(deal, norm)
        if not acq_names or not tgt_names:
            continue

        acq_hit = any(
            name_hits_text(a, norm_text)
            for a in acq_names
            if len(a) > min_name_len
        )
        tgt_hit = any(
            name_hits_text(t, norm_text)
            for t in tgt_names
            if len(t) > min_name_len
        )

        if acq_hit and tgt_hit:
            return deal.get("deal_id")

    return None


# ---------------------------------------------------------------------------
# Regulator wrappers — split + orient
# ---------------------------------------------------------------------------


def regex_match_deal_by_title(
    title: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """ACCC: 'ACQUIRER - TARGET' (dash variants, maxsplit=1)."""
    if not title or not deals:
        return None

    parts = DASH_SEPARATOR.split(title, maxsplit=1)
    if len(parts) < 2:
        return None

    left = normalise_company_name(parts[0].strip(), ACCC_SUFFIXES)
    right = normalise_company_name(parts[1].strip(), ACCC_SUFFIXES)
    return regex_match_split_orient(left, right, deals, suffixes=ACCC_SUFFIXES)


def regex_match_ftc_deal(
    title: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """FTC: 'PARTY1; PARTY2' (semicolon, maxsplit=1)."""
    if not title or not deals:
        return None

    parts = FTC_SEMICOLON_SEP.split(title, maxsplit=1)
    if len(parts) < 2:
        return None

    left = normalise_company_name(parts[0].strip(), DEFAULT_SUFFIXES)
    right = normalise_company_name(parts[1].strip(), DEFAULT_SUFFIXES)
    return regex_match_split_orient(left, right, deals, suffixes=DEFAULT_SUFFIXES)


def regex_match_uk_cma_deal(
    title: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """UK CMA: 'X / Y merger inquiry' (slash, strip trailing suffix)."""
    if not title or not deals:
        return None

    cleaned = UK_CMA_TRAILING.sub("", title).strip()
    parts = UK_CMA_SLASH_SEP.split(cleaned, maxsplit=1)
    if len(parts) < 2:
        return None

    left = normalise_company_name(parts[0].strip(), DEFAULT_SUFFIXES)
    right = normalise_company_name(parts[1].strip(), DEFAULT_SUFFIXES)
    return regex_match_split_orient(left, right, deals, suffixes=DEFAULT_SUFFIXES)


# ---------------------------------------------------------------------------
# Regulator wrappers — flat scan
# ---------------------------------------------------------------------------


def regex_match_cade_deal(
    translated_text: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """CADE: English translation of interessados (flat party list)."""
    return regex_match_flat_scan(translated_text, deals, suffixes=CADE_SUFFIXES)


def regex_match_bka_deal(
    pursue_en: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """Bundeskartellamt: English translation of pursue/Unternehmen field."""
    return regex_match_flat_scan(pursue_en, deals, suffixes=BKA_SUFFIXES)


def regex_match_canada_deal(
    parties: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """Canada: parties string (flat, English)."""
    return regex_match_flat_scan(parties, deals, suffixes=CANADA_SUFFIXES)


def regex_match_nz_deal(
    title: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """NZ ComCom: case title only (flat scan)."""
    return regex_match_flat_scan(title, deals, suffixes=NZ_SUFFIXES)


def regex_match_ec_deal(
    companies_text: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """EC merger: slash-separated company list from case title/companies field."""
    return regex_match_flat_scan(companies_text, deals, suffixes=EC_SUFFIXES)


def regex_match_fs_deal(
    companies_text: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """EC Foreign Subsidies: slash-separated company list from case title/companies."""
    return regex_match_flat_scan(companies_text, deals, suffixes=EC_SUFFIXES)


def regex_match_samr_deal(
    title_en: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """SAMR public notice: English translation of notice title (flat scan)."""
    return regex_match_flat_scan(title_en, deals, suffixes=SAMR_SUFFIXES)


def regex_match_turkey_deal(
    text_en: str,
    deals: List[Dict[str, Any]],
) -> Optional[str]:
    """Turkey Rekabet Kurumu: combined English title + description (flat scan)."""
    return regex_match_flat_scan(text_en, deals, suffixes=TURKEY_SUFFIXES)


# Aliases for test scripts
_normalise_accc = lambda t: normalise_company_name(t, ACCC_SUFFIXES)
_normalise_cade = lambda t: normalise_company_name(t, CADE_SUFFIXES)
_normalise_nz = lambda t: normalise_company_name(t, NZ_SUFFIXES)
_normalise_ec = lambda t: normalise_company_name(t, EC_SUFFIXES)
_normalise_fs = lambda t: normalise_company_name(t, EC_SUFFIXES)
_normalise_samr = lambda t: normalise_company_name(t, SAMR_SUFFIXES)

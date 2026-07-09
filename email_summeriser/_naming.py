"""Shared utility for unique file naming across SEC summarizer modules."""
import re
import hashlib


def filing_uid(url) -> str:
    """Extract a short unique ID from an SEC filing URL.

    Uses the accession number from the URL path, which is unique per filing.
    Accepts a single URL string or a list (uses the first URL).
    Example: .../edgar/data/1434868/000162828025057944/... → '25057944'
    """
    if isinstance(url, list):
        url = url[0]
    # SEC URLs: /Archives/edgar/data/{CIK}/{accession}/{filename}
    m = re.search(r'/Archives/edgar/data/\d+/(\d+)/', url)
    if m:
        acc = m.group(1).lstrip('0') or '0'
        return acc[-8:]
    # Fallback: hash of URL
    return hashlib.md5(url.encode()).hexdigest()[:8]

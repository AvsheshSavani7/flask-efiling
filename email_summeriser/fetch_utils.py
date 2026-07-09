"""
Shared text extraction utility for SEC filing summarizers.
Handles HTML pages, local files, and PDFs transparently.

Two-pass extraction:  Haiku reads the full filing and extracts only the
sections relevant to a given form type.  The concentrated extract is then
passed to Opus for analysis — cheaper and more complete than truncation.
"""

import logging
import os
import re
import sys
from pathlib import Path
from io import BytesIO

import requests
from bs4 import BeautifulSoup

try:
    import anthropic
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "anthropic", "-q"])
    import anthropic

# Suppress noisy pypdf warnings about malformed PDF cross-references
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Auto-install pypdf if missing (matches existing pattern in summarizers)
try:
    import pypdf
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pypdf", "-q"])
    import pypdf


def fetch_text(source: str, word_limit: int = 10000) -> str:
    """
    Fetch and extract text from a URL or local file.
    Automatically handles HTML pages and PDFs.
    """
    is_url = source.startswith("http")
    is_pdf = source.lower().endswith(".pdf")

    if is_url:
        headers = {"User-Agent": "MergerArbDashboard/1.0 (merger-arb-research@outlook.com)"}
        resp = requests.get(source, headers=headers, timeout=60)
        resp.raise_for_status()

        # Detect PDF from content-type or URL
        content_type = resp.headers.get("Content-Type", "")
        if is_pdf or "application/pdf" in content_type:
            return _extract_pdf_bytes(resp.content, word_limit)

        raw = resp.text
    else:
        path = Path(source)
        if path.suffix.lower() == ".pdf":
            return _extract_pdf_file(path, word_limit)
        raw = path.read_text()

    return _extract_html(raw, word_limit)


def _extract_html(html: str, word_limit: int) -> str:
    """Parse HTML and extract clean text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)

    words = text.split()
    if word_limit and len(words) > word_limit:
        text = " ".join(words[:word_limit])

    return text


def _extract_pdf_bytes(data: bytes, word_limit: int) -> str:
    """Extract text from PDF bytes (downloaded from URL)."""
    reader = pypdf.PdfReader(BytesIO(data))
    return _read_pdf_pages(reader, word_limit)


def _extract_pdf_file(path: Path, word_limit: int) -> str:
    """Extract text from a local PDF file."""
    reader = pypdf.PdfReader(str(path))
    return _read_pdf_pages(reader, word_limit)


def _read_pdf_pages(reader: pypdf.PdfReader, word_limit: int) -> str:
    """Read pages from a PdfReader and return cleaned text."""
    pages = []
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            pages.append(page_text)

    text = "\n\n".join(pages)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)

    words = text.split()
    if word_limit and len(words) > word_limit:
        text = " ".join(words[:word_limit])

    return text


# ══════════════════════════════════════════════════════════════════════
#  Two-pass extraction  (Haiku reads full doc → Opus analyses extract)
# ══════════════════════════════════════════════════════════════════════

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
EXTRACTION_MAX_TOKENS = 16000

EXTRACTION_PROMPT_TEMPLATE = """You are a document extraction specialist for SEC filings.
Your job is to read the FULL filing text and extract only the sections and information
that are relevant for the specified analysis.

FILING TYPE CONTEXT:
{extraction_guidance}

INSTRUCTIONS:
- Read the entire document carefully from beginning to end
- Extract VERBATIM the sections, paragraphs, and data points described above
- Preserve exact numbers, dates, dollar amounts, percentages, names, and quoted language
- Include section headers to maintain document structure and context
- If a requested section is not found in the filing, note: "[Section not found: <name>]"
- Do NOT summarize, paraphrase, or interpret — extract the raw relevant text faithfully
- Prioritize the most critical sections first in case of space constraints

FULL FILING TEXT:
"""

DEFAULT_EXTRACTION_GUIDANCE = """This is a generic SEC filing. Extract:
- All deal terms (prices, exchange ratios, premiums, conditions)
- Regulatory approvals mentioned (required, obtained, pending) — every jurisdiction
- Timeline information (closing dates, expiration dates, deadlines, meeting dates)
- Risk factors and litigation mentions
- Board recommendations and fairness opinions
- Key financial figures (revenue, earnings, deal value)
- Any material conditions precedent
- Background of the transaction or negotiation history"""


def _get_anthropic_client() -> anthropic.Anthropic:
    """Lazy-load the Anthropic client (avoids import-time .env dependency)."""
    from dotenv import load_dotenv
    env_path = Path.home() / "Downloads" / "Course+Materials" / "Merger Scraper" / ".env"
    load_dotenv(env_path)
    api_key = os.getenv("ANTHROPIC_API_KEY_TEST")
    if not api_key:
        raise RuntimeError(f"ANTHROPIC_API_KEY_TEST not found in {env_path}")
    return anthropic.Anthropic(api_key=api_key)


MAX_CHUNK_WORDS = 100_000  # safe limit per Haiku call (~130K tokens)


def extract_relevant_sections(full_text: str, extraction_guidance: str) -> str:
    """Use Haiku to extract relevant sections from a long filing.

    Pass 1 of two-pass approach: Haiku reads the full document and returns
    only the sections relevant to the specific filing type.

    For very large filings (>150K words), splits into chunks and extracts
    from each chunk separately, then combines the results.
    """
    client = _get_anthropic_client()

    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        extraction_guidance=extraction_guidance,
    )

    word_count = len(full_text.split())
    print(f"   Extraction pass: sending {word_count:,} words to {EXTRACTION_MODEL}...")

    if word_count <= MAX_CHUNK_WORDS:
        # Single pass — fits in one call
        msg = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=EXTRACTION_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt + full_text}],
        )
        extracted = msg.content[0].text.strip()
    else:
        # Chunk the text and extract from each chunk
        words = full_text.split()
        chunks = []
        for start in range(0, len(words), MAX_CHUNK_WORDS):
            chunks.append(" ".join(words[start:start + MAX_CHUNK_WORDS]))
        print(f"   Document exceeds {MAX_CHUNK_WORDS:,} words — splitting into {len(chunks)} chunks")

        extracts = []
        for i, chunk in enumerate(chunks, 1):
            print(f"   Extracting chunk {i}/{len(chunks)} ({len(chunk.split()):,} words)...")
            msg = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=EXTRACTION_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt + chunk}],
            )
            extracts.append(msg.content[0].text.strip())
        extracted = ("\n\n" + "=" * 40 + "\n\n").join(extracts)

    extract_words = len(extracted.split())
    print(f"   Extraction complete: {extract_words:,} words extracted "
          f"({extract_words / word_count * 100:.0f}% of original)")

    return extracted


def _extract_exhibit_urls(html_content: str, source_url: str) -> list[str]:
    """Extract exhibit links from SEC filing HTML in the same filing directory.

    Only follows links within the same EDGAR accession directory — never
    cross-filing or external links.
    """
    from urllib.parse import urljoin

    # Base directory: everything up to and including the last /
    base_dir = source_url.rsplit("/", 1)[0] + "/"

    soup = BeautifulSoup(html_content, "html.parser")
    exhibit_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        absolute = urljoin(source_url, href)
        # Same EDGAR directory only
        if not absolute.startswith(base_dir):
            continue
        # .htm / .html files only
        if not absolute.lower().endswith((".htm", ".html")):
            continue
        # Skip the source document itself
        if absolute == source_url:
            continue
        exhibit_urls.add(absolute)

    return sorted(exhibit_urls)


def fetch_text_with_extraction(source, extraction_guidance: str = None) -> str:
    """Fetch filing text using two-pass extraction.

    1. Fetches the FULL text (no truncation).  Accepts a single URL/path
       or a list of URLs/paths (combined into one document).
    2. Auto-detects linked exhibits in SEC filings and fetches those too.
    3. Sends combined text to Haiku for targeted extraction of relevant sections.
    4. Returns the concentrated extract for Opus analysis.

    Args:
        source: URL, file path, or list of URLs/paths.
        extraction_guidance: Form-specific instructions for what to extract.
            Falls back to DEFAULT_EXTRACTION_GUIDANCE if None.
    """
    if isinstance(source, list):
        parts = []
        for i, s in enumerate(source, 1):
            label = s.split("/")[-1] if "/" in s else s
            print(f"   Fetching document {i}/{len(source)}: {label}")
            parts.append(fetch_text(s, word_limit=0))
        full_text = ("\n\n" + "=" * 60 + "\n\n").join(parts)
    elif isinstance(source, str) and "sec.gov" in source and source.startswith("http"):
        # SEC filing — fetch raw HTML, check for exhibit links
        headers = {"User-Agent": "MergerArbDashboard/1.0 (merger-arb-research@outlook.com)"}
        resp = requests.get(source, headers=headers, timeout=60)
        resp.raise_for_status()
        raw_html = resp.text

        # Extract text from main document (no duplicate fetch)
        main_text = _extract_html(raw_html, word_limit=0)

        # Check for linked exhibits in the same filing directory
        exhibit_urls = _extract_exhibit_urls(raw_html, source)
        if exhibit_urls:
            print(f"   Found {len(exhibit_urls)} linked exhibit(s) — fetching automatically")
            parts = [main_text]
            for i, url in enumerate(exhibit_urls, 1):
                label = url.split("/")[-1]
                print(f"   Fetching exhibit {i}/{len(exhibit_urls)}: {label}")
                parts.append(fetch_text(url, word_limit=0))
            full_text = ("\n\n" + "=" * 60 + "\n\n").join(parts)
        else:
            full_text = main_text
    else:
        full_text = fetch_text(source, word_limit=0)
    guidance = extraction_guidance or DEFAULT_EXTRACTION_GUIDANCE
    return extract_relevant_sections(full_text, guidance)


# ══════════════════════════════════════════════════════════════════════
#  DOCX field suppression — hide "N/A" / "Not stated" in client output
# ══════════════════════════════════════════════════════════════════════

_EMPTY_PATTERNS = frozenset([
    "n/a",
    "not stated",
    "not specified",
    "not mentioned",
    "not explicitly stated",
    "not disclosed",
    "not available",
    "not applicable",
    "not provided",
    "none",
    "none stated",
    "none mentioned",
    "none disclosed",
    "unknown",
    "unable to determine",
    "not found in filing",
    "not found in the filing",
    "not included",
    "not addressed",
    "null",
])


def is_empty_value(value) -> bool:
    """Check if a summary field value is effectively empty / not-stated.

    Returns True for None, empty strings, empty lists, and values that are
    variations of 'N/A' or 'Not stated'.  Used to suppress noise in DOCX
    output while keeping full data in JSON.
    """
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0 or all(is_empty_value(item) for item in value)
    if isinstance(value, dict):
        return len(value) == 0 or all(is_empty_value(v) for v in value.values())
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return True
    normalized = stripped.lower().rstrip(".").strip()
    if normalized in _EMPTY_PATTERNS:
        return True
    # Catch form-specific variants like "Not stated in the Schedule TO cover filing"
    # and "Not applicable — this is the initial filing"
    for prefix in ("not stated in", "not specified in", "not explicitly stated",
                    "not mentioned in", "not disclosed in", "not included in",
                    "not applicable", "not addressed", "not available",
                    "no competing", "no litigation",
                    "no specific litigation", "no material litigation",
                    "no known opposition", "no known dissent",
                    "none identified", "none disclosed",
                    "none mentioned", "none reported",
                    "unable to determine", "n/a"):
        if normalized.startswith(prefix):
            return True
    return False


def has_content(value) -> bool:
    """Check if a dict or list has any meaningful (non-empty) values inside.

    For dicts: True if at least one value passes ``is_empty_value``.
    For lists: True if at least one item passes ``is_empty_value``.
    For strings/other: inverse of ``is_empty_value``.
    """
    if value is None:
        return False
    if isinstance(value, dict):
        return any(not is_empty_value(v) for v in value.values())
    if isinstance(value, list):
        return any(not is_empty_value(item) for item in value)
    return not is_empty_value(value)


def add_field(paragraph, label: str, value, newline: bool = True) -> bool:
    """Add a labelled field to a DOCX paragraph, skipping empty values.

    Returns True if the field was written, False if skipped.
    """
    if is_empty_value(value):
        return False
    paragraph.add_run(label).bold = True
    paragraph.add_run(str(value) + ("\n" if newline else ""))
    return True

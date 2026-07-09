"""
International Regulatory Filing Summarizer — Multi-level summaries via Claude API
Usage: python intl_regulatory_summary.py

Handles filings from CADE, EU Commission, CMA, ACCC, SAMR, and other
international competition/antitrust authorities. Extracts structured fields
for jurisdiction, process numbers, review stage, approval status, etc.
"""

from pathlib import Path
from _naming import filing_uid

# ──── PASTE YOUR REGULATORY FILING URL HERE ────
FILING_URL = ""
# ──── OUTPUT FOLDER ────
OUTPUT_DIR = Path.home() / "Downloads" / "Course+Materials" / "Merger Scraper" / "8K Test" / "Output Summaries"
# ─────────────────────────────────

import os
import sys
import json
import re
import anthropic

try:
    import requests
    from bs4 import BeautifulSoup
    from dotenv import load_dotenv
    from docx import Document as DocxDocument
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "requests", "beautifulsoup4", "python-dotenv", "python-docx", "-q"])
    import requests
    from bs4 import BeautifulSoup
    from dotenv import load_dotenv
    from docx import Document as DocxDocument
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

# Load API key from .env
ENV_PATH = Path.home() / "Downloads" / "Course+Materials" / "Merger Scraper" / ".env"
load_dotenv(ENV_PATH)

if not os.getenv("ANTHROPIC_API_KEY_TEST"):
    print(f"ANTHROPIC_API_KEY not found. Check your .env at:\n   {ENV_PATH}")
    sys.exit(1)


SUMMARY_PROMPT = """You are an expert analyst summarizing international regulatory filings for a merger arbitrage desk.

You receive documents from foreign competition/antitrust authorities (CADE, EU Commission, CMA, ACCC, SAMR, COFECE, KFTC, JFTC, etc.). These may be in any language. Your job is to translate to English, extract structured information, and frame it through a merger arbitrage lens.

Given the regulatory filing text below, produce summaries at 3 levels. Respond ONLY in valid JSON (no markdown fences).

{
  "jurisdiction": "<country or region — e.g., Brazil, EU, UK, Australia, China>",
  "regulatory_body": "<full name of authority — e.g., CADE, European Commission DG Competition, CMA>",
  "document_type": "<e.g., Deficiency Order, Phase 1 Decision, Remedies Proposal, Clearance Decision>",
  "process_number": "<official case/process number as stated in the document>",
  "original_language": "<language the document was written in>",
  "document_date": "<MM/DD/YY>",
  "parties": {
    "acquirer": "<acquiring company name>",
    "target": "<target company name>",
    "tickers": ["<any stock tickers mentioned>"],
    "other_parties": ["<any other named parties — intervenors, complainants, etc.>"]
  },

  "L1_headline": "+ <TICKER or ACQUIRER/TARGET> – <key takeaway in 8 words>. | <date>",

  "L2_brief": "<2-3 sentence summary covering: what authority did what, current status, and immediate implications for the deal>",

  "L3_detailed": {
    "action_taken": "<what the authority did — e.g., issued deficiency order, approved unconditionally, opened Phase 2>",
    "review_stage": "<Pre-notification | Phase 1 | Phase 2 | Information Request | Decision | Remedy Negotiation | Appeal | Other>",
    "approval_status": "<Approved Unconditionally | Approved with Conditions | Blocked | Pending - Information Requested | Pending - Under Review | Not Recognized | Other>",
    "decision_authority": "<name/title of signing official if stated>",
    "relevant_markets": ["<product and geographic markets identified by the authority>"],
    "information_requested": ["<specific data or documents demanded, for deficiency orders>"],
    "conditions_or_remedies": ["<imposed or proposed conditions/remedies>"],
    "competitive_concerns": ["<theories of harm or competitive concerns stated>"],
    "legal_basis": "<statutes, articles, or regulations cited>",
    "timeline": {
      "notification_date": "<date the merger was notified to the authority>",
      "decision_date": "<date of the decision, if this is a decision>",
      "response_deadline": "<deadline for response, if an information request>",
      "next_milestone": "<next expected event or deadline>"
    },
    "deal_implications_for_us_investors": "<stated facts relevant to deal timeline, closing, or risk for US-listed securities>",
    "related_proceedings": ["<parallel reviews in other jurisdictions mentioned in the document>"],
    "risks_flagged": ["<any risks, delays, or concerns stated in the document>"],
    "significance": "<critical | high | medium | low | routine>",
    "significance_reasoning": "<1 sentence explaining the rating>"
  }
}

SIGNIFICANCE RATING GUIDE — rate the update's importance for a merger arbitrage desk:
- "critical": Major milestone that directly affects deal outcome — unconditional approval, approval with conditions, blocking decision, Phase 2 escalation, formal objections issued.
- "high": Substantive progress — formal information request with deadline, remedies proposed or accepted, key competitive concerns identified, significant third-party intervention (e.g., competitor files opposition).
- "medium": Notable development — initial merger notification filed, new jurisdiction opens review, questionnaires issued to market participants, consultation period opens/closes, notable procedural step.
- "low": Minor procedural — internal case routing between departments, routine document acknowledgments, standard protocol registrations, file transfers with no substantive change.
- "routine": No material content — duplicate docket entry, boilerplate filing receipt, purely administrative with zero information value.
When in doubt between two levels, choose the LOWER one — false negatives (missing a low email) are less costly than false positives (sending noise).

Rules:
- DEAL HISTORY: If a "DEAL HISTORY CONTEXT" section is provided, use it to understand what has previously happened in this case. Your L2_brief should reference the progression factually (e.g., "Following the initial notification on [date], CADE has now..."). Your L1_headline should reflect ONLY the new development. Do NOT repeat the full history — just reference it for continuity.
- CRITICAL: The deal history is provided for factual context ONLY. Do NOT use it to draw conclusions, assess trajectory, identify patterns, predict outcomes, or add any analytical color. Report only what THIS document says. The history tells you what was already known — your job is to state what is NEW.
- CRITICAL — FACTS ONLY: Every statement must be directly traceable to the document text. Report ONLY what the document says. Do NOT add analysis, assess significance, interpret motives, predict outcomes, evaluate probability, or editorialize. Do NOT state what is "not disclosed" or "not mentioned" — simply omit fields where the document is silent. If the document does not say it, do not write it.
  GOOD: "CADE requested revenue data for 2021-2025 across four markets."
  BAD: "The broad scope of information requested indicates potentially detailed competitive analysis ahead."
  GOOD: "The offer expires June 10, 2026."
  BAD: "This tight timeline may create pressure on shareholders to tender quickly."
- PRECISION: Use the document's exact terminology for legal, regulatory, and financial terms. Do NOT paraphrase in ways that broaden or narrow the stated meaning.
- TRANSLATION: Translate all content to English while preserving proper nouns, case numbers, regulatory terms, and legal citations in their original form. For example, keep "Ato de Concentracao" or "Superintendencia-Geral" as-is alongside the English translation.
- L1 format MUST be: + <TICKER or ACQUIRER/TARGET> – <takeaway>. | <date>
- Extract exact case/process numbers, legal article citations, official names
- Identify the review stage and approval status precisely
- Note any deadlines or response requirements with exact dates

REGULATORY FILING TEXT:
"""

EXTRACTION_GUIDANCE = """This is an international regulatory/competition authority filing (e.g., CADE, EU Commission, CMA, ACCC, SAMR).
Extract VERBATIM in the original language (preserve exact case numbers, legal citations, party names, regulatory terms):
- DEAL HISTORY CONTEXT section (if present) — pass through verbatim
- Authority name and jurisdiction
- Case/process number and document type
- Parties involved (acquirer, target, other named parties)
- Action taken by the authority (decision, information request, approval, etc.)
- Review stage and approval status
- Markets identified (product markets, geographic markets)
- Information or documents requested (for deficiency orders)
- Conditions or remedies imposed or proposed
- Competitive concerns or theories of harm stated
- Legal basis (statutes, articles, regulations cited)
- All dates (notification, decision, deadlines, next milestones)
- References to parallel proceedings in other jurisdictions
- Any facts relevant to deal timeline or closing
- Names and titles of signing officials"""


def fetch_filing_text(source: str) -> str:
    """Fetch and extract text from an international regulatory filing.

    Tries the standard fetch pipeline first. If the source URL returns
    403 or 503 (common for bot-protected government sites), falls back
    to Jina Reader (r.jina.ai) for extraction.
    """
    from fetch_utils import fetch_text, extract_relevant_sections

    is_url = isinstance(source, str) and source.startswith("http")

    if is_url:
        try:
            full_text = fetch_text(source, word_limit=0)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 503):
                print(f"   Got {e.response.status_code} — trying Jina Reader fallback...")
                jina_url = f"https://r.jina.ai/{source}"
                headers = {"User-Agent": "MergerArbDashboard/1.0 (merger-arb-research@outlook.com)"}
                resp = requests.get(jina_url, headers=headers, timeout=60)
                resp.raise_for_status()
                full_text = resp.text
            else:
                raise
    else:
        full_text = fetch_text(source, word_limit=0)

    return extract_relevant_sections(full_text, EXTRACTION_GUIDANCE)


def summarize(text: str, model: str = "claude-opus-4-6") -> dict:
    """Call Claude API to produce multi-level summary."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY_TEST"))

    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": SUMMARY_PROMPT + "\n\n" + text
        }]
    )

    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # If response was truncated, try to close the JSON
    if msg.stop_reason != "end_turn":
        if raw.count('{') > raw.count('}'):
            raw += '"' + '}' * (raw.count('{') - raw.count('}'))
        if raw.count('[') > raw.count(']'):
            raw += ']' * (raw.count('[') - raw.count(']'))

    return json.loads(raw)


def print_summary(s: dict):
    """Pretty-print the multi-level summary."""
    print("\n" + "=" * 70)
    print(f"  INTERNATIONAL REGULATORY FILING SUMMARY")
    print("=" * 70)

    print(f"\n   Jurisdiction:    {s.get('jurisdiction', 'N/A')}")
    print(f"   Authority:       {s.get('regulatory_body', 'N/A')}")
    print(f"   Document Type:   {s.get('document_type', 'N/A')}")
    print(f"   Process Number:  {s.get('process_number', 'N/A')}")
    print(f"   Language:        {s.get('original_language', 'N/A')}")
    print(f"   Date:            {s.get('document_date', 'N/A')}")

    parties = s.get("parties", {})
    print(f"\n   Acquirer:  {parties.get('acquirer', 'N/A')}")
    print(f"   Target:    {parties.get('target', 'N/A')}")
    if parties.get("tickers"):
        print(f"   Tickers:   {', '.join(parties['tickers'])}")
    if parties.get("other_parties"):
        print(f"   Other:     {', '.join(parties['other_parties'])}")

    print(f"\n   L1 | HEADLINE")
    print(f"   {s['L1_headline']}")

    print(f"\n   L2 | BRIEF")
    print(f"   {s['L2_brief']}")

    d = s["L3_detailed"]
    print(f"\n   L3 | DETAILED")
    print(f"   Action Taken:     {d.get('action_taken', 'N/A')}")
    print(f"   Review Stage:     {d.get('review_stage', 'N/A')}")
    print(f"   Approval Status:  {d.get('approval_status', 'N/A')}")
    if d.get("decision_authority"):
        print(f"   Decision By:      {d['decision_authority']}")
    if d.get("relevant_markets"):
        print(f"   Relevant Markets:")
        for m in d["relevant_markets"]:
            print(f"     - {m}")
    if d.get("information_requested"):
        print(f"   Information Requested:")
        for i in d["information_requested"]:
            print(f"     - {i}")
    if d.get("conditions_or_remedies"):
        print(f"   Conditions/Remedies:")
        for c in d["conditions_or_remedies"]:
            print(f"     - {c}")
    if d.get("competitive_concerns"):
        print(f"   Competitive Concerns:")
        for c in d["competitive_concerns"]:
            print(f"     - {c}")
    if d.get("legal_basis"):
        print(f"   Legal Basis:      {d['legal_basis']}")
    timeline = d.get("timeline", {})
    if any(timeline.get(k) for k in timeline):
        print(f"   Timeline:")
        for k, v in timeline.items():
            if v:
                label = k.replace("_", " ").title()
                print(f"     - {label}: {v}")
    if d.get("deal_implications_for_us_investors"):
        print(f"   Deal Implications: {d['deal_implications_for_us_investors']}")
    if d.get("related_proceedings"):
        print(f"   Related Proceedings:")
        for r in d["related_proceedings"]:
            print(f"     - {r}")
    if d.get("risks_flagged"):
        print(f"   Risks:")
        for r in d["risks_flagged"]:
            print(f"     - {r}")

    print("=" * 70)


def export_docx(s: dict, filepath: str = None):
    """Export summary to a formatted Word document."""
    from fetch_utils import is_empty_value, has_content, add_field

    jurisdiction = s.get("jurisdiction", "INTL")
    authority = s.get("regulatory_body", "")
    date = s.get("document_date", "")
    parties = s.get("parties", {})
    acquirer = parties.get("acquirer", "UNKNOWN")

    if filepath is None:
        safe_acquirer = re.sub(r'[^\w\-\.]', '_', acquirer)
        safe_jurisdiction = re.sub(r'[^\w\-\.]', '_', jurisdiction)
        filepath = f"IntlReg_{safe_jurisdiction}_{safe_acquirer}_{date.replace('/', '-')}.docx"

    doc = DocxDocument()

    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)

    title = doc.add_heading(f"Regulatory Filing: {jurisdiction} — {authority}", level=0)
    title.runs[0].font.size = Pt(18)

    # Metadata
    meta = doc.add_paragraph()
    add_field(meta, "Document Type: ", s.get("document_type"), newline=False)
    meta.add_run("    ")
    add_field(meta, "Process Number: ", s.get("process_number"), newline=False)

    meta2 = doc.add_paragraph()
    add_field(meta2, "Date: ", date, newline=False)
    meta2.add_run("    ")
    add_field(meta2, "Original Language: ", s.get("original_language"), newline=False)

    meta3 = doc.add_paragraph()
    add_field(meta3, "Acquirer: ", parties.get("acquirer"), newline=False)
    meta3.add_run("    ")
    add_field(meta3, "Target: ", parties.get("target"), newline=False)
    if parties.get("tickers"):
        meta3.add_run("    ")
        add_field(meta3, "Tickers: ", ", ".join(parties["tickers"]), newline=False)

    # L1
    doc.add_heading("L1 — Headline", level=1)
    p = doc.add_paragraph()
    run = p.add_run(s["L1_headline"])
    run.bold = True
    run.font.size = Pt(14)
    run.font.color.rgb = RGBColor(0, 102, 68)  # Green theme

    # L2
    doc.add_heading("L2 — Brief", level=1)
    doc.add_paragraph(s["L2_brief"])

    # L3
    doc.add_heading("L3 — Detailed", level=1)
    d = s["L3_detailed"]

    if not is_empty_value(d.get("action_taken")):
        doc.add_heading("Action Taken", level=2)
        doc.add_paragraph(d["action_taken"])

    status_para = doc.add_paragraph()
    add_field(status_para, "Review Stage: ", d.get("review_stage"), newline=True)
    add_field(status_para, "Approval Status: ", d.get("approval_status"), newline=True)
    add_field(status_para, "Decision Authority: ", d.get("decision_authority"), newline=True)

    items = d.get("relevant_markets")
    if has_content(items):
        doc.add_heading("Relevant Markets", level=2)
        for m in items:
            if not is_empty_value(m):
                doc.add_paragraph(m, style="List Bullet")

    items = d.get("information_requested")
    if has_content(items):
        doc.add_heading("Information Requested", level=2)
        for i in items:
            if not is_empty_value(i):
                doc.add_paragraph(i, style="List Bullet")

    items = d.get("conditions_or_remedies")
    if has_content(items):
        doc.add_heading("Conditions / Remedies", level=2)
        for c in items:
            if not is_empty_value(c):
                doc.add_paragraph(c, style="List Bullet")

    items = d.get("competitive_concerns")
    if has_content(items):
        doc.add_heading("Competitive Concerns", level=2)
        for c in items:
            if not is_empty_value(c):
                doc.add_paragraph(c, style="List Bullet")

    if not is_empty_value(d.get("legal_basis")):
        doc.add_heading("Legal Basis", level=2)
        doc.add_paragraph(d["legal_basis"])

    timeline = d.get("timeline", {})
    if has_content(timeline):
        doc.add_heading("Timeline", level=2)
        for k, v in timeline.items():
            if not is_empty_value(v):
                label = k.replace("_", " ").title()
                p = doc.add_paragraph()
                add_field(p, f"{label}: ", v, newline=False)

    if not is_empty_value(d.get("deal_implications_for_us_investors")):
        doc.add_heading("Deal Implications for US Investors", level=2)
        doc.add_paragraph(d["deal_implications_for_us_investors"])

    items = d.get("related_proceedings")
    if has_content(items):
        doc.add_heading("Related Proceedings", level=2)
        for r in items:
            if not is_empty_value(r):
                doc.add_paragraph(r, style="List Bullet")

    items = d.get("risks_flagged")
    if has_content(items):
        doc.add_heading("Risks Flagged", level=2)
        for r in items:
            if not is_empty_value(r):
                doc.add_paragraph(r, style="List Bullet")

    doc.save(filepath)
    return filepath


def main():
    source = FILING_URL

    print(f"Fetching international regulatory filing from: {source}")

    text = fetch_filing_text(source)
    print(f"Extracted {len(text.split())} words of text")

    print("Generating summary via Claude Opus...")
    result = summarize(text)

    print_summary(result)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Save JSON
    jurisdiction = result.get("jurisdiction", "INTL")
    safe_jurisdiction = re.sub(r'[^\w\-\.]', '_', jurisdiction)
    uid = filing_uid(FILING_URL) if "sec.gov" in str(FILING_URL) else re.sub(r'[^\w]', '', str(FILING_URL))[-8:]
    out_path = OUTPUT_DIR / f"intl_reg_summary_{safe_jurisdiction}_{uid}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\nRaw JSON saved to: {out_path}")

    # Save DOCX
    parties = result.get("parties", {})
    acquirer = parties.get("acquirer", "UNKNOWN")
    safe_acquirer = re.sub(r'[^\w\-\.]', '_', acquirer)
    date = result.get("document_date", "")
    safe_date = date.replace("/", "-")
    filename = f"IntlReg_{safe_jurisdiction}_{safe_acquirer}_{safe_date}_{uid}.docx"
    docx_path = export_docx(result, str(OUTPUT_DIR / filename))
    print(f"Word doc saved to:  {docx_path}")

    return result


if __name__ == "__main__":
    main()

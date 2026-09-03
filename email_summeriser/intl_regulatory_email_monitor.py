"""
International Regulatory Filing Email Monitor
Monitors Gmail for international regulatory filing notifications (CADE, CMA, EU Commission, etc.),
summarizes them with Claude, and emails results.

Key differences from 8k_email_summary.py:
- Filters emails from the same Hyperion sender that do NOT match 8-K pattern
- Treats email body as content (not just a pointer to a URL)
- Extracts ALL URLs (not just sec.gov)
- Green-themed HTML output (#006644)
- Separate tracking file (processed_intl_reg_emails.txt)
"""

from intl_regulatory_summary import SUMMARY_PROMPT, EXTRACTION_GUIDANCE
import os
import re
import time
import json
import tempfile
import imaplib
import email
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('intl_reg_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables — from root project .env (one level up)
ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH)

# Configuration
GMAIL_EMAIL = os.getenv('GMAIL_EMAIL_2')
GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD_2')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY_TEST')

# Output directory — JSON + DOCX summaries saved here
OUTPUT_DIR = Path(__file__).parent / "Output Summaries"

# Deal state file — tracks accumulated history per case/proceeding
DEAL_STATE_FILE = Path(__file__).parent / "intl_deal_state.json"

# Email patterns — match all Hyperion sender addresses
SENDERS = [
    'info@hyperiontechnologies.ai',
    'alerts@hyperiontechnologies.ai'
]

# Recipients for summary emails (in addition to self)
SUMMARY_RECIPIENTS = [
    'josh@hyperiontechnologies.ai',
    'aaron.glick@guggenheimsecurities.com',
    'chris.colpitts@guggenheimsecurities.com'
]
# Exclude 8-K emails (handled by the 8-K monitor)
EXCLUDE_PATTERN = re.compile(r'SEC Filing.*8-K', re.IGNORECASE)
# Subject-only pattern for international regulatory content.
# Requires the [FRMD] tag (foreign regulatory monitoring). The agency name
# alone is NOT sufficient — a subject must carry [FRMD] to be processed, so
# mistagged emails (e.g. [FRUD]) or untagged agency mentions are rejected.
INTL_SUBJECT_PATTERN = re.compile(r'\[FRMD\]', re.IGNORECASE)

# URLs to filter out (noise)
NOISE_URL_PATTERNS = re.compile(
    r'unsubscribe|mailto:|facebook\.com|twitter\.com|linkedin\.com|'
    r'instagram\.com|youtube\.com|google-analytics|doubleclick|'
    r'mailchimp|sendgrid|list-manage|tracking|pixel|beacon',
    re.IGNORECASE
)

# ─── CADE Boilerplate Document Filter ───────────────────────────────────────
#
# CADE's SEI system includes many administrative documents that have zero
# substantive value for merger arbitrage analysis. These consume token budget
# (we have a 150K word safety-valve on combined text) and dilute the signal.
#
# This filter identifies boilerplate documents by their FILENAME (from the
# HTTP Content-Disposition header) or by their CONTENT (first ~500 chars).
#
# WHAT WE FILTER (always boilerplate — structurally cannot contain substance):
#
#   Guia de Recolhimento da União (GRU)
#     - Government payment form. Contains bank reference numbers, payment
#       codes, amounts. The filing fee is standardized; this tells us nothing
#       about the deal or competitive analysis.
#
#   Comprovante de recolhimento / pagamento GRU
#     - Payment confirmation receipt. Bank account details, transaction IDs.
#       Pure financial plumbing.
#
#   Procuração / Procuração + renúncia
#     - Power of attorney. "I, [lawyer], am authorized to act for [party]."
#       The only marginally useful info (which law firm represents each party)
#       is already stated in the notification itself.
#
#   Substabelecimento
#     - Sub-delegation of proxy from one lawyer to another at the same firm.
#       One-paragraph form document.
#
#   Solicitação de Acesso ao Processo Restrito
#     - Request for access to the restricted/confidential case file.
#       One-paragraph form letter: "We request access." Never contains
#       strategic arguments or substantive information.
#
#   Recibo de Notificação de AC / Recibo Eletrônico de Protocolo
#     - System-generated filing receipts. Confirm process number and parties
#       (already known from the notification itself). Machine output.
#
# WHAT WE KEEP (could contain substance — must be read):
#
#   Despacho Decisório — Could be access grant, deadline order, clearance
#   Despacho Ordinatório — Usually routing, but could set deadlines
#   Anexo — Generic "attachment," could be anything
#   Ofício — Official letter, could be information request
#   Nota Técnica — Staff competitive analysis (highly substantive)
#   Parecer — Legal opinion
#   Voto — Commissioner's decision vote
#   Edital — Public notice (contains CNAE codes, case announcement)
#   Notificação de Ato de Concentração — The actual merger notification
#   Formulário de Notificação — The notification form (market data, shares)
#   Any document type not explicitly listed above
#
# RISK ASSESSMENT: The filtered types are structurally boilerplate in CADE
# proceedings. A Procuração will always be a lawyer authorization form.
# A GRU will always be a bank payment form. There is no scenario where
# substantive competitive analysis, decision reasoning, or deal-relevant
# information would appear in these document types.
#
# See also: docs/cade_document_types.md for the full reference.
# ─────────────────────────────────────────────────────────────────────────────

# Filename patterns (matched against Content-Disposition filename, case-insensitive)
CADE_BOILERPLATE_FILENAME_PATTERNS = re.compile(
    r'Guia_de_Recolhimento|'             # GRU payment form
    r'Comprovante_de_(?:pagamento|recolhimento)|'  # Payment confirmation
    # Power of attorney (with/without accents)
    r'Procura[cç][aã]o|'
    r'Substabelecimento|'                # Proxy sub-delegation
    r'Pet[\._].*de[\._].*acesso|'        # Petition for file access
    r'Peticao_de_acesso|'               # Access petition (variant)
    r'Solicita[cç][aã]o.*Acesso',        # Access request (variant)
    re.IGNORECASE
)

# Content patterns (matched against first ~500 chars of extracted text)
# Used as fallback when filename is not available (e.g., HTML documents)
CADE_BOILERPLATE_CONTENT_PATTERNS = re.compile(
    r'Recibo de Notifica[cç][aã]o de AC|'           # Filing receipt
    r'Recibo Eletr[oô]nico de Protocolo|'           # Electronic receipt
    # GRU generation system
    r'Conecta\.Cade.*Sistema de Gera[cç][aã]o de Boletos|'
    r'Comprovante de pagamento de boleto|'           # Payment confirmation
    r'Solicita[cç][aã]o de Acesso.*Restrito',        # Access request in body
    re.IGNORECASE
)

# Claude API settings
CLAUDE_MODEL = "claude-opus-4-8"

# Import the summarizer's prompt and schema


class IntlRegulatoryMonitor:
    """Monitors Gmail for international regulatory filings and sends automated summaries."""

    def __init__(self):
        self.gmail_email = GMAIL_EMAIL
        self.gmail_password = GMAIL_APP_PASSWORD
        self.anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.processed_emails = self._load_processed_emails()
        self.deal_state = self._load_deal_state()

        if not all([self.gmail_email, self.gmail_password, ANTHROPIC_API_KEY]):
            raise ValueError(
                "Missing required environment variables. Check your .env file for: "
                "GMAIL_EMAIL_2, GMAIL_APP_PASSWORD_2, ANTHROPIC_API_KEY_TEST"
            )

        logger.info(
            f"Initialized International Regulatory Monitor for {self.gmail_email}")

    PROCESSED_FILE = Path(__file__).parent / 'processed_intl_reg_emails.txt'

    def _load_processed_emails(self) -> set:
        """Load previously processed email IDs to avoid duplicates."""
        if self.PROCESSED_FILE.exists():
            return set(self.PROCESSED_FILE.read_text().strip().split('\n'))
        return set()

    def _save_processed_email(self, email_id: str):
        """Save processed email ID."""
        self.processed_emails.add(email_id)
        with open(self.PROCESSED_FILE, 'a') as f:
            f.write(f"{email_id}\n")

    # ── Deal State Management ──────────────────────────────────────────

    def _load_deal_state(self) -> Dict:
        """Load deal state from JSON file."""
        if DEAL_STATE_FILE.exists():
            try:
                return json.loads(DEAL_STATE_FILE.read_text())
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(
                    f"Failed to load deal state, starting fresh: {e}")
        return {}

    def _save_deal_state(self):
        """Save deal state to JSON file (atomic write via tmp+rename)."""
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=DEAL_STATE_FILE.parent, suffix='.tmp'
            )
            with os.fdopen(fd, 'w') as f:
                json.dump(self.deal_state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, DEAL_STATE_FILE)
            logger.info(
                f"   Deal state saved ({len(self.deal_state)} deals tracked)")
        except Exception as e:
            logger.error(f"Failed to save deal state: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def _clean_md(value: str) -> str:
        """Strip markdown artifacts (*, **, |) and excess whitespace from extracted values."""
        value = re.sub(r'^\s*\*+\s*', '', value)   # leading * or **
        value = re.sub(r'\s*\*+\s*$', '', value)   # trailing * or **
        value = re.sub(r'\*+', '', value)           # inline bold markers
        value = re.sub(r'\|', '', value)            # stray pipe chars
        return value.strip()

    def _extract_deal_key(self, email_text: str, subject: str = '') -> Tuple[str, Dict]:
        """Parse structured fields from email body + subject to get deal key + metadata.

        Looks for Hyperion's structured block with fields like:
          Process: 08700.004680/2026-01
          Deal ID: 69a0b95554958e923cd9829d
          Acquirer: The Brink's Company
          Target: NCR Atleos Corporation

        Also parses subject lines like:
          [FRMD] CADE Brazil (Updated) – 08700.003036/2026-15
          [FRMD] German Bundeskartellamt- B5-54/26 (New) – Klöckner & Co SE / Worthington Steel GmbH
          AXTA: CADE Brazil - Regulatory Update - [FRMD]

        Returns (deal_key, metadata_dict).
        Primary key: process number. Fallback: jurisdiction_acquirer_target.
        """
        meta = {}

        # Extract structured fields from body (case-insensitive, flexible spacing)
        patterns = {
            'process_number': r'(?:Process(?:\s*Number)?|Case(?:\s*Number)?|Processo)\s*[:：]\s*(.+?)(?:\n|$)',
            'deal_id': r'Deal\s*ID\s*[:：]\s*([a-f0-9]{20,})',
            'acquirer': r'Acquir(?:er|ing(?:\s+Company)?)\s*[:：]\s*(.+?)(?:\s*[|*\n]|$)',
            'target': r'Target(?:\s+Company)?\s*[:：]\s*(.+?)(?:\s*[|*\n]|$)',
            'jurisdiction': r'Jurisdiction\s*[:：]\s*(.+?)(?:\n|$)',
            'regulatory_body': r'(?:Regulatory\s*Body|Authority)\s*[:：]\s*(.+?)(?:\n|$)',
            'deal_type': r'(?:Deal\s*Type|Type(?:\s+of\s+(?:Act|Proceeding))?)\s*[:：]\s*(.+?)(?:\n|$)',
            'registration_date': r'(?:Registration\s*Date|Filing\s*Date|Notification\s*Date)\s*[:：]\s*(.+?)(?:\n|$)',
        }

        for field, pattern in patterns.items():
            match = re.search(pattern, email_text, re.IGNORECASE)
            if match:
                meta[field] = self._clean_md(match.group(1))

        # Parse subject line for supplemental info
        # Pattern 1: "[FRMD] Agency- CaseNum (New/Updated) – Party1 / Party2"
        subj_case = re.search(
            r'\[FRMD\]\s*.*?[-–]\s*([A-Z0-9][\w\-./]+\d)\s*\(', subject)
        if subj_case and not meta.get('process_number'):
            meta['process_number'] = subj_case.group(1).strip()

        # Pattern 2: CADE-style process number in subject "– 08700.004680/2026-01"
        subj_cade = re.search(r'[–-]\s*(\d{5}\.\d{6}/\d{4}-\d{2})', subject)
        if subj_cade and not meta.get('process_number'):
            meta['process_number'] = subj_cade.group(1).strip()

        # Pattern 3: ACCC case number "WA-85027" or "MN-80016"
        subj_accc = re.search(r'[–-]\s*([A-Z]{2}-\d{5,})', subject)
        if subj_accc and not meta.get('process_number'):
            meta['process_number'] = subj_accc.group(1).strip()

        # Pattern 4: NZ case number "PRJ0048867"
        subj_nz = re.search(r'(PRJ\d{5,})', subject)
        if subj_nz and not meta.get('process_number'):
            meta['process_number'] = subj_nz.group(1).strip()

        # Extract parties from subject if not found in body
        # "[FRMD] ... – Party1 / Party2" or "[FRMD] ... – Party1 - Party2"
        subj_parties = re.search(
            r'[–]\s*(?:[A-Z0-9][\w\-./]*\s*(?:\(.*?\))?\s*[–:]\s*)?(.+?)\s*/\s*(.+?)$', subject)
        if subj_parties:
            if not meta.get('acquirer'):
                meta['acquirer'] = subj_parties.group(1).strip()
            if not meta.get('target'):
                meta['target'] = subj_parties.group(2).strip()

        # "TICKER/TICKER: Agency - Regulatory Update - [FRMD]" — extract ticker + agency
        subj_ticker_agency = re.search(
            r'^([A-Z][A-Z0-9 .,&/\-]+?):\s*(.+?)\s*-\s*(?:Regulatory Update|New Regulatory Case)', subject)
        if subj_ticker_agency:
            meta.setdefault('ticker', subj_ticker_agency.group(1).strip())
            meta.setdefault('subject_agency',
                            subj_ticker_agency.group(2).strip())
        else:
            # Shorter ticker-only match (with optional /TICKER2)
            subj_ticker = re.search(r'^([A-Z]{2,5}(?:/[A-Z]{2,5})?):', subject)
            if subj_ticker:
                meta.setdefault('ticker', subj_ticker.group(1))

        # EC merger case number from subject or email text (e.g. M.12278)
        ec_case = re.search(r'\b(M\.\d{4,6})\b', email_text) or re.search(
            r'\b(M\.\d{4,6})\b', subject)
        if ec_case and not meta.get('process_number'):
            meta['process_number'] = ec_case.group(1)

        # Build deal key
        # Priority: process number from body > process number from subject > ticker_agency
        process_num = meta.get('process_number', '')
        if process_num:
            deal_key = process_num
        elif meta.get('ticker') and meta.get('subject_agency'):
            # Fallback: TICKER_agency (e.g., "WBD_CADE_Brazil", "NATL_CADE_Brazil")
            ticker = meta['ticker']
            agency = meta['subject_agency']
            deal_key = re.sub(r'[^a-z0-9]+', '_',
                              f"{ticker}_{agency}".lower()).strip('_')
        else:
            # Last resort: jurisdiction_acquirer_target (normalized)
            jurisdiction = meta.get('jurisdiction', 'unknown')
            acquirer = meta.get('acquirer', 'unknown')
            target = meta.get('target', 'unknown')
            deal_key = re.sub(
                r'[^a-z0-9]+', '_', f"{jurisdiction}_{acquirer}_{target}".lower()).strip('_')

        return deal_key, meta

    MAX_HISTORY_UPDATES = 6  # Max prior updates to inject into LLM context

    def _build_deal_history_context(self, deal_entry: Dict) -> str:
        """Build ESTABLISHED FACTS block from known_facts + recent timeline.

        Returns empty string if no prior updates exist.
        Caps timeline at MAX_HISTORY_UPDATES most recent to preserve token budget.
        """
        updates = deal_entry.get('updates', [])
        if not updates:
            return ""

        process_num = deal_entry.get('process_number', 'N/A')
        jurisdiction = deal_entry.get('jurisdiction', 'N/A')
        regulatory_body = deal_entry.get('regulatory_body', 'N/A')
        acquirer = deal_entry.get('acquirer', 'N/A')
        target = deal_entry.get('target', 'N/A')
        update_number = deal_entry.get('update_count', len(updates)) + 1

        lines = [
            "=== DEAL HISTORY CONTEXT ===",
            f"Case: {process_num} ({regulatory_body} {jurisdiction})",
            f"Target: {target} | Acquirer: {acquirer}",
            f"This is update #{update_number} for this case.",
        ]

        # Build ESTABLISHED FACTS from known_facts dict
        kf = deal_entry.get('known_facts', {})
        if kf:
            lines.append("")
            lines.append(
                "ESTABLISHED FACTS (already reported — do NOT repeat):")
            if kf.get('review_stage'):
                lines.append(f"- Review stage: {kf['review_stage']}")
            if kf.get('approval_status'):
                lines.append(f"- Approval status: {kf['approval_status']}")
            if kf.get('notification_date'):
                lines.append(f"- Notification date: {kf['notification_date']}")
            if kf.get('questionnaires_issued_to'):
                lines.append(
                    f"- Questionnaires issued to: {', '.join(kf['questionnaires_issued_to'])}")
            if kf.get('questionnaire_responses_from'):
                lines.append(
                    f"- Questionnaire responses from: {', '.join(kf['questionnaire_responses_from'])}")
            if kf.get('questionnaire_topics'):
                lines.append(
                    f"- Questionnaire topics: {', '.join(kf['questionnaire_topics'])}")
            if kf.get('competitive_concerns'):
                lines.append(
                    f"- Competitive concerns identified: {', '.join(kf['competitive_concerns'])}")
            if kf.get('theories_of_harm'):
                lines.append(
                    f"- Theories of harm: {', '.join(kf['theories_of_harm'])}")
            if kf.get('filings_reported'):
                lines.append(
                    f"- Filings already reported: {', '.join(kf['filings_reported'])}")
            if kf.get('remedies_proposed'):
                lines.append(
                    f"- Remedies proposed: {', '.join(kf['remedies_proposed'])}")
            if kf.get('divestiture_assets'):
                lines.append(
                    f"- Divestiture assets: {', '.join(kf['divestiture_assets'])}")
            if kf.get('intervenors'):
                lines.append(f"- Intervenors: {', '.join(kf['intervenors'])}")
            if kf.get('data_requested'):
                lines.append(
                    f"- Data requested: {', '.join(kf['data_requested'])}")
            if kf.get('decisions'):
                lines.append(f"- Decisions: {', '.join(kf['decisions'])}")
            if kf.get('conditions_imposed'):
                lines.append(
                    f"- Conditions imposed: {', '.join(kf['conditions_imposed'])}")
            if kf.get('pending_deadlines'):
                lines.append(
                    f"- Pending deadlines: {', '.join(kf['pending_deadlines'])}")
            if kf.get('relevant_markets'):
                lines.append(
                    f"- Relevant markets: {', '.join(kf['relevant_markets'])}")

        # Recent timeline
        lines.append("")
        lines.append("RECENT TIMELINE:")
        if len(updates) > self.MAX_HISTORY_UPDATES:
            skipped = len(updates) - self.MAX_HISTORY_UPDATES
            lines.append(f"  [... {skipped} earlier updates omitted ...]")
            recent = updates[-self.MAX_HISTORY_UPDATES:]
            start_idx = skipped + 1
        else:
            recent = updates
            start_idx = 1

        for i, update in enumerate(recent, start_idx):
            ts = update.get('timestamp', 'N/A')
            if 'T' in ts:
                ts = ts.split('T')[0]
            headline = update.get('L1_headline', update.get(
                'headline', update.get('action_taken', 'N/A')))
            backfill_tag = " [from docket]" if update.get(
                'source') == 'backfill' else ""
            lines.append(f"  [{i}] {ts}: {headline}{backfill_tag}")

        lines.append("")
        lines.append(
            "YOUR TASK: Report ONLY facts from the new document NOT in ESTABLISHED FACTS above.")
        lines.append("===")
        return "\n".join(lines)

    # Status badge color mapping
    STATUS_COLORS = {
        'Approved Unconditionally': '#2e7d32',
        'Approved with Conditions': '#f57f17',
        'Pending - Under Review': '#1565c0',
        'Pending - Information Requested': '#e65100',
        'Blocked': '#c62828',
    }

    def _build_case_status_html(self, update_number: int, deal_entry: Optional[Dict],
                                summary: Dict) -> str:
        """Build the Case Status HTML section for the summary email.

        For NEW CASE (update #1): returns a slim one-line badge — header already
        shows process number, stage, and status so we don't duplicate.
        For UPDATE #2+: shows update label, tracking info, and case timeline.
        """
        if deal_entry:
            first_seen = deal_entry.get('first_seen', '')
            if 'T' in first_seen:
                first_seen = first_seen.split('T')[0]
        else:
            first_seen = datetime.now().strftime('%Y-%m-%d')

        # ── NEW CASE: slim badge only ──
        if update_number <= 1:
            return (
                f'<div style="margin:10px 0;font-size:12px;color:#006644;">'
                f'<span style="display:inline-block;padding:3px 10px;border-radius:3px;'
                f'font-size:11px;font-weight:bold;color:white;background-color:#006644;">'
                f'NEW CASE</span>'
                f' &nbsp; First tracked: {first_seen}'
                f'</div>'
            )

        # ── UPDATE #N: tracking info + timeline ──
        count = deal_entry.get(
            'update_count', update_number) if deal_entry else update_number
        tracking_line = f"Tracked since {first_seen} &middot; {count} updates"

        MAX_TIMELINE_ROWS = 8
        timeline_html = ""
        if deal_entry:
            all_updates = deal_entry.get('updates', [])
            rows = ""

            # Cap displayed rows — show "... N earlier" if truncated
            if len(all_updates) > MAX_TIMELINE_ROWS:
                skipped = len(all_updates) - MAX_TIMELINE_ROWS
                display_updates = all_updates[-MAX_TIMELINE_ROWS:]
                rows += (
                    f'<div style="margin:4px 0;font-size:12px;color:#999;font-style:italic;">'
                    f'... {skipped} earlier update{"s" if skipped > 1 else ""}</div>'
                )
            else:
                display_updates = all_updates

            prev_status = None
            # Get the status before our display window for transition detection
            if len(all_updates) > MAX_TIMELINE_ROWS:
                prev_status = all_updates[-(MAX_TIMELINE_ROWS + 1)
                                          ].get('approval_status', '')

            for i, u in enumerate(display_updates):
                ts = u.get('timestamp', '')
                if 'T' in ts:
                    ts = ts.split('T')[0]
                try:
                    date_parts = ts.split('-')
                    ts_short = f"{date_parts[1]}/{date_parts[2]}"
                except (IndexError, ValueError):
                    ts_short = ts

                action = u.get('L1_headline', u.get(
                    'headline', u.get('action_taken', '')))
                if len(action) > 80:
                    action = action[:77] + "..."

                # Detect status transitions
                cur_status = u.get('approval_status', '')
                transition_marker = ""
                if prev_status and cur_status and cur_status != prev_status:
                    transition_marker = (
                        f' <span style="font-size:11px;color:#e65100;font-weight:bold;">'
                        f'&#9650; {cur_status}</span>'
                    )
                prev_status = cur_status or prev_status

                is_current = (u is display_updates[-1])
                is_backfill = u.get('source') == 'backfill'

                if is_current:
                    rows += (
                        f'<div style="margin:4px 0;font-size:13px;">'
                        f'<span style="color:#006644;font-weight:bold;">&#9679;</span> '
                        f'<span style="color:#333;font-weight:bold;">{ts_short} &nbsp; {action}'
                        f'{transition_marker}'
                        f' &nbsp;<span style="color:#006644;font-size:11px;">&#8592; NOW</span>'
                        f'</span></div>'
                    )
                elif is_backfill:
                    rows += (
                        f'<div style="margin:4px 0;font-size:12px;color:#999;font-style:italic;">'
                        f'<span style="color:#bbb;">&#9675;</span> '
                        f'{ts_short} &nbsp; {action}{transition_marker}</div>'
                    )
                else:
                    rows += (
                        f'<div style="margin:4px 0;font-size:13px;color:#666;">'
                        f'<span style="color:#999;">&#9675;</span> '
                        f'{ts_short} &nbsp; {action}{transition_marker}</div>'
                    )

            timeline_html = (
                f'<div style="margin-top:10px;padding-top:10px;border-top:1px solid #c8e6c9;">'
                f'<div style="font-size:11px;font-weight:bold;color:#006644;'
                f'letter-spacing:0.5px;margin-bottom:6px;">CASE TIMELINE</div>'
                f'{rows}</div>'
            )

        return (
            f'<div style="margin:15px 0;padding:14px 18px;background-color:#e8f5e9;'
            f'border-left:4px solid #006644;border-radius:3px;">'
            f'<div style="font-size:14px;font-weight:bold;color:#006644;margin-bottom:4px;">'
            f'&#9679; UPDATE #{update_number}</div>'
            f'<div style="font-size:12px;color:#666;">{tracking_line}</div>'
            f'{timeline_html}'
            f'</div>'
        )

    @staticmethod
    def _append_unique(lst: list, items):
        """Append items to lst only if not already present (set-like dedup)."""
        for item in (items if isinstance(items, list) else [items]):
            if item and item not in lst:
                lst.append(item)

    def _update_deal_state(self, deal_key: str, meta: Dict, summary: Dict,
                           email_data: Dict, backfill_entries: List[Dict] = None) -> int:
        """Upsert deal entry after successful summarization.

        Returns the update_number for this update.
        Accumulates known_facts from each summary for delta detection.
        """
        # Use email Date header for the timestamp (not processing time)
        raw_date = email_data.get('date', '')
        try:
            email_dt = parsedate_to_datetime(raw_date)
            ts = email_dt.isoformat(timespec='seconds')
        except Exception:
            ts = datetime.now().isoformat(timespec='seconds')

        cs = summary.get('case_snapshot', {})

        if deal_key in self.deal_state:
            entry = self.deal_state[deal_key]
            entry['update_count'] += 1
            entry['last_updated'] = ts
        else:
            entry = {
                'deal_id': meta.get('deal_id', ''),
                'process_number': meta.get('process_number', deal_key),
                'target': summary.get('parties', {}).get('target', '') or meta.get('target', ''),
                'acquirer': summary.get('parties', {}).get('acquirer', '') or meta.get('acquirer', ''),
                'jurisdiction': meta.get('jurisdiction', summary.get('jurisdiction', '')),
                'regulatory_body': meta.get('regulatory_body', summary.get('regulatory_body', '')),
                'deal_type': meta.get('deal_type', summary.get('document_type', '')),
                'registration_date': meta.get('registration_date', ''),
                'update_count': 1,
                'first_seen': ts,
                'last_updated': ts,
                'current_status': '',
                'current_review_stage': '',
                'known_facts': {},
                'updates': [],
            }
            self.deal_state[deal_key] = entry

            # Insert backfill entries (e.g., from CADE docket) before the current update
            if backfill_entries:
                entry['updates'].extend(backfill_entries)
                entry['update_count'] += len(backfill_entries)
                logger.info(
                    f"   Pre-seeded {len(backfill_entries)} backfill entries")

        # Update current status from summary
        if cs.get('approval_status'):
            entry['current_status'] = cs['approval_status']
        if cs.get('review_stage'):
            entry['current_review_stage'] = cs['review_stage']

        # Append update record
        headline = summary.get('L1_headline', summary.get('headline', ''))
        entry['updates'].append({
            'email_id': email_data.get('email_id', ''),
            'timestamp': ts,
            'email_subject': email_data.get('subject', ''),
            'L1_headline': headline,
            'headline': headline,  # backward compat
            'action_taken': summary.get('filing_category', ''),
            'review_stage': cs.get('review_stage', ''),
            'approval_status': cs.get('approval_status', ''),
            'significance': summary.get('significance', 'medium'),
        })

        # ── Accumulate known_facts ──
        kf = entry.setdefault('known_facts', {})

        # From case_snapshot
        if cs.get('review_stage'):
            kf['review_stage'] = cs['review_stage']
        if cs.get('approval_status'):
            kf['approval_status'] = cs['approval_status']
        timeline = cs.get('timeline', {})
        if timeline.get('notification_date'):
            kf['notification_date'] = timeline['notification_date']
        kf.setdefault('relevant_markets', [])
        self._append_unique(kf['relevant_markets'],
                            cs.get('relevant_markets', []))

        # From new_filings
        kf.setdefault('filings_reported', [])
        for nf in summary.get('new_filings', []):
            desc = nf.get('filing_description', '')
            self._append_unique(kf['filings_reported'], desc)

        # From questionnaire_detail
        qd = summary.get('questionnaire_detail', {})
        if qd.get('has_questionnaires'):
            kf.setdefault('questionnaires_issued_to', [])
            kf.setdefault('questionnaire_responses_from', [])
            kf.setdefault('questionnaire_topics', [])
            for resp in qd.get('respondents', []):
                name = resp.get('entity_name', '')
                if resp.get('date_responded'):
                    entry_str = f"{name} ({resp['date_responded']})"
                    self._append_unique(
                        kf['questionnaire_responses_from'], entry_str)
                elif resp.get('date_issued'):
                    entry_str = f"{name} ({resp['date_issued']})"
                    self._append_unique(
                        kf['questionnaires_issued_to'], entry_str)
                elif name:
                    self._append_unique(kf['questionnaires_issued_to'], name)
                self._append_unique(
                    kf['questionnaire_topics'], resp.get('topics_covered', []))

        # From remedy_detail
        rd = summary.get('remedy_detail', {})
        if rd.get('has_remedies'):
            kf.setdefault('remedies_proposed', [])
            kf.setdefault('divestiture_assets', [])
            if rd.get('remedy_type'):
                kf['remedy_type'] = rd['remedy_type']
            for rem in rd.get('remedies', []):
                desc = rem.get('description', '')
                self._append_unique(kf['remedies_proposed'], desc)
                if rem.get('divestiture_assets'):
                    self._append_unique(
                        kf['divestiture_assets'], rem['divestiture_assets'])

        # From objection_detail
        od = summary.get('objection_detail', {})
        if od.get('has_objections'):
            kf.setdefault('competitive_concerns', [])
            kf.setdefault('theories_of_harm', [])
            for concern in od.get('concerns', []):
                if concern.get('description'):
                    self._append_unique(
                        kf['competitive_concerns'], concern['description'])
                if concern.get('theory_of_harm'):
                    self._append_unique(
                        kf['theories_of_harm'], concern['theory_of_harm'])

        # From intervention_detail
        ivd = summary.get('intervention_detail', {})
        if ivd.get('has_interventions'):
            kf.setdefault('intervenors', [])
            for iv in ivd.get('intervenors', []):
                name = iv.get('entity_name', '')
                pos = iv.get('position', '')
                if name:
                    entry_str = f"{name} ({pos})" if pos else name
                    self._append_unique(kf['intervenors'], entry_str)

        # From information_request_detail
        ird = summary.get('information_request_detail', {})
        if ird.get('has_info_request'):
            kf.setdefault('data_requested', [])
            kf.setdefault('pending_deadlines', [])
            for req in ird.get('requests', []):
                for d_item in req.get('data_requested', []):
                    self._append_unique(kf['data_requested'], d_item)
                if req.get('deadline'):
                    self._append_unique(
                        kf['pending_deadlines'], req['deadline'])

        # From decision_detail
        dd = summary.get('decision_detail', {})
        if dd.get('has_decision'):
            kf.setdefault('decisions', [])
            kf.setdefault('conditions_imposed', [])
            dtype = dd.get('decision_type', '')
            ddate = dd.get('decision_date', '')
            if dtype:
                entry_str = f"{dtype} ({ddate})" if ddate else dtype
                self._append_unique(kf['decisions'], entry_str)
            for cond in dd.get('conditions_imposed', []):
                self._append_unique(kf['conditions_imposed'], cond)

        return entry['update_count']

    BACKFILL_MODEL = "claude-haiku-4-5-20251001"

    BACKFILL_PROMPT = """Extract all dated events/milestones from this CADE regulatory docket page.
Return ONLY a JSON array of objects with these fields:
- "date": date in YYYY-MM-DD format (convert from DD/MM/YYYY Brazilian format)
- "action": brief English description of the event (1-2 sentences max)

Include document filings, petitions, decisions, despachos, editais, certidões,
questionnaires, transfers between units, and any other procedural steps.
Order chronologically (earliest first). Deduplicate similar entries on the same date.
If a date is ambiguous, use your best judgment.

Return ONLY the JSON array, no markdown fences or explanation."""

    def _backfill_cade_docket(self, sei_url: str) -> List[Dict]:
        """Fetch CADE SEI process page and extract historical milestones via Haiku.

        Returns list of backfill update entries for pre-seeding deal state.
        Returns empty list on any failure.
        """
        try:
            # Fetch the docket page text (reuses same pattern as _fetch_url_text)
            docket_text = self._fetch_url_text(sei_url)
            if not docket_text or len(docket_text.strip()) < 100:
                logger.warning(
                    "   Backfill: docket page text too short or empty")
                return []

            # Extract milestones via Haiku
            logger.info("   Backfill: extracting milestones via Haiku...")
            msg = self.anthropic_client.messages.create(
                model=self.BACKFILL_MODEL,
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": self.BACKFILL_PROMPT + "\n\n" + docket_text
                }]
            )

            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            milestones = json.loads(raw)

            if not isinstance(milestones, list):
                logger.warning(
                    "   Backfill: Haiku response was not a JSON array")
                return []

            # Convert milestones to backfill update entries
            entries = []
            for m in milestones:
                date_str = m.get('date', '')
                action = m.get('action', '')
                if not date_str or not action:
                    continue
                entries.append({
                    'email_id': '',
                    'timestamp': date_str,
                    'email_subject': '',
                    'L1_headline': '',
                    'action_taken': action,
                    'review_stage': '',
                    'approval_status': '',
                    'source': 'backfill',
                })

            logger.info(
                f"   Backfill: extracted {len(entries)} milestones from docket")
            return entries

        except json.JSONDecodeError as e:
            logger.warning(
                f"   Backfill: failed to parse Haiku response as JSON: {e}")
            return []
        except Exception as e:
            logger.warning(
                f"   Backfill: failed to extract docket milestones: {e}")
            return []

    # ── End Deal State Management ──────────────────────────────────────

    def connect_to_gmail(self) -> imaplib.IMAP4_SSL:
        """Connect to Gmail via IMAP."""
        try:
            mail = imaplib.IMAP4_SSL('imap.gmail.com')
            mail.login(self.gmail_email, self.gmail_password)
            logger.info("Successfully connected to Gmail")
            return mail
        except Exception as e:
            logger.error(f"Failed to connect to Gmail: {e}")
            raise

    def check_for_new_emails(self, mail: imaplib.IMAP4_SSL, incremental: bool = False) -> List[Dict]:
        """Check for new international regulatory emails.

        Args:
            mail: Connected IMAP4_SSL instance.
            incremental: If True, only check messages with UID > self._last_uid
                         (much lighter on Gmail API quota). If False, scan all
                         messages from last 30 days (full catchup mode).
        """
        mail.select('inbox')

        if incremental and hasattr(self, '_last_uid') and self._last_uid:
            # Only fetch messages newer than our high-water mark
            all_uids = set()
            for sender in SENDERS:
                _, data = mail.uid('SEARCH', None,
                                   f'(FROM "{sender}" UID {self._last_uid + 1}:*)')
                if data[0]:
                    all_uids.update(int(u) for u in data[0].split())
            # Filter out the last_uid itself (UID range is inclusive)
            all_uids.discard(self._last_uid)

            if not all_uids:
                return []

            sorted_uids = sorted(all_uids)
            logger.info(
                f"Incremental scan: {len(sorted_uids)} new UID(s) since {self._last_uid}")
        else:
            # Full scan — used for initial catchup
            thirty_days_ago = (
                datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
            logger.info(
                f"Full scan: emails from Hyperion senders since {thirty_days_ago}")

            all_uids = set()
            for sender in SENDERS:
                _, data = mail.uid('SEARCH', None,
                                   f'(FROM "{sender}" SINCE {thirty_days_ago})')
                if data[0]:
                    all_uids.update(int(u) for u in data[0].split())

            if not all_uids:
                logger.warning(
                    "No emails found from Hyperion senders in last 30 days")
                return []

            sorted_uids = sorted(all_uids)
            logger.info(f"Full scan: {len(sorted_uids)} emails to check")

        new_emails = []
        skipped_processed = 0
        skipped_8k = 0
        skipped_no_match = 0

        for uid in sorted_uids:
            try:
                # Fetch only headers first (lightweight)
                _, msg_data = mail.uid('FETCH', str(uid),
                                       '(BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE)])')
                if not msg_data or not msg_data[0] or msg_data[0] == b')':
                    continue
                header_bytes = msg_data[0][1] if isinstance(
                    msg_data[0], tuple) else b''
                header_msg = email.message_from_bytes(header_bytes)

                email_id = header_msg.get('Message-ID')
                subject = header_msg.get('Subject', '')

                # Decode subject
                decoded_parts = decode_header(subject)
                subject = ''.join([
                    part.decode(
                        encoding or 'utf-8') if isinstance(part, bytes) else part
                    for part, encoding in decoded_parts
                ])

                if email_id in self.processed_emails:
                    skipped_processed += 1
                    continue
                if EXCLUDE_PATTERN.search(subject):
                    skipped_8k += 1
                    continue
                if not INTL_SUBJECT_PATTERN.search(subject):
                    skipped_no_match += 1
                    continue

                # Passed all filters — now fetch full body
                _, full_data = mail.uid('FETCH', str(uid), '(RFC822)')
                email_body = full_data[0][1]
                email_message = email.message_from_bytes(email_body)

                email_content = self._extract_email_content(email_message)
                logger.info(f"   Found intl regulatory email: {subject}")

                new_emails.append({
                    'email_id': email_id,
                    'subject': subject,
                    'content': email_content,
                    'date': email_message.get('Date')
                })
            except Exception as e:
                logger.warning(f"   Error processing UID {uid}: {e}")
                continue

        # Update high-water mark
        if sorted_uids:
            self._last_uid = max(sorted_uids)

        if not incremental or new_emails:
            logger.info(f"Scan summary: {len(sorted_uids)} checked, "
                        f"{skipped_processed} processed, {skipped_8k} 8-K, "
                        f"{skipped_no_match} no match, {len(new_emails)} new")

        return new_emails

    def _extract_email_content(self, email_message) -> Dict:
        """Extract content from email — body text + all URLs."""
        content = {
            'html_body': '',
            'text_body': '',
            'urls': []
        }

        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                if content_type == 'text/html':
                    try:
                        content['html_body'] = part.get_payload(
                            decode=True).decode('utf-8', errors='ignore')
                    except Exception:
                        pass
                elif content_type == 'text/plain':
                    try:
                        content['text_body'] = part.get_payload(
                            decode=True).decode('utf-8', errors='ignore')
                    except Exception:
                        pass
        else:
            try:
                content['text_body'] = email_message.get_payload(
                    decode=True).decode('utf-8', errors='ignore')
            except Exception:
                pass

        # Extract all URLs from whichever body we have
        text_to_search = content['html_body'] if content['html_body'] else content['text_body']
        content['urls'] = self._extract_urls(text_to_search)

        # If we have HTML body, also extract plain text version for the combined doc
        if content['html_body'] and not content['text_body']:
            soup = BeautifulSoup(content['html_body'], 'html.parser')
            content['text_body'] = soup.get_text(separator='\n', strip=True)

        return content

    def _extract_urls(self, text: str) -> List[str]:
        """Extract all HTTP/HTTPS URLs, filtering out noise."""
        url_pattern = r'https?://[^\s<>"\')\]]+[^\s<>"\')\].,;:!?]'
        raw_urls = re.findall(url_pattern, text)

        # Deduplicate while preserving order, filter noise
        seen = set()
        clean_urls = []
        for url in raw_urls:
            # Strip trailing HTML artifacts
            url = re.sub(r'[&;].*$', '', url) if '&amp;' in url else url
            if url in seen:
                continue
            if NOISE_URL_PATTERNS.search(url):
                continue
            seen.add(url)
            clean_urls.append(url)

        return clean_urls

    # Minimum words expected from a real page; below this → likely JS-rendered
    _THIN_RESPONSE_THRESHOLD = 50

    def _fetch_url_text(self, url: str) -> str:
        """Fetch and extract text from a URL.

        Falls back to Playwright (headless browser) or Jina Reader when:
        - Response is 403/503 (bot-protected sites)
        - 200 response yields < 50 words (JS-rendered SPA pages)
        """
        headers = {
            "User-Agent": "MergerArbDashboard/1.0 (merger-arb-research@outlook.com)"}
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "application/pdf" in content_type or url.lower().endswith(".pdf"):
                # Use fetch_utils for PDF handling
                from fetch_utils import fetch_text
                return fetch_text(url, word_limit=10000)

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "meta", "link"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r" {2,}", " ", text)

            words = text.split()

            # Detect JS-rendered pages: 200 OK but near-empty after parsing
            if len(words) < self._THIN_RESPONSE_THRESHOLD:
                logger.info(
                    f"   Thin response ({len(words)} words) from {url} — likely JS-rendered")
                rendered = self._fetch_with_playwright(url)
                if rendered:
                    return rendered
                logger.info(
                    f"   Playwright unavailable — trying Jina Reader...")
                return self._fetch_with_jina(url, headers)

            if len(words) > 10000:
                text = " ".join(words[:10000])
            return text

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 503):
                logger.info(
                    f"   Got {e.response.status_code} for {url} — trying Playwright...")
                rendered = self._fetch_with_playwright(url)
                if rendered:
                    return rendered
                logger.info(
                    f"   Playwright unavailable — trying Jina Reader...")
                return self._fetch_with_jina(url, headers)
            else:
                logger.warning(f"   Failed to fetch {url}: {e}")
                return ""
        except Exception as e:
            logger.warning(f"   Failed to fetch {url}: {e}")
            return ""

    def _fetch_with_playwright(self, url: str) -> Optional[str]:
        """Render a JS-heavy page with Playwright headless Chromium.

        Returns extracted text, or None if Playwright is not installed.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                browser.close()

            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "meta", "link", "nav", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r" {2,}", " ", text)

            words = text.split()
            if len(words) < self._THIN_RESPONSE_THRESHOLD:
                logger.warning(
                    f"   Playwright also returned thin content ({len(words)} words) for {url}")
                return None

            logger.info(
                f"   Playwright rendered {len(words)} words from {url}")
            if len(words) > 10000:
                text = " ".join(words[:10000])
            return text
        except Exception as e:
            logger.warning(f"   Playwright failed for {url}: {e}")
            return None

    def _fetch_with_jina(self, url: str, headers: dict) -> str:
        """Fetch URL content via Jina Reader as last-resort fallback."""
        try:
            jina_url = f"https://r.jina.ai/{url}"
            resp = requests.get(jina_url, headers=headers, timeout=60)
            resp.raise_for_status()
            text = resp.text
            words = text.split()
            logger.info(
                f"   Jina Reader returned {len(words)} words for {url}")
            return text
        except Exception as jina_e:
            logger.warning(f"   Jina Reader also failed for {url}: {jina_e}")
            return ""

    def _is_boilerplate_document(self, url: str, linked_text: str) -> Optional[str]:
        """Check if a fetched document is boilerplate (zero merger-arb value).

        Checks the URL's Content-Disposition filename first (lightweight),
        then falls back to checking the first ~500 chars of extracted text.

        Returns:
            A string describing why it was filtered (for logging), or None if
            the document should be kept.

        See CADE_BOILERPLATE_FILENAME_PATTERNS and CADE_BOILERPLATE_CONTENT_PATTERNS
        for the full list of filtered types and rationale.
        """
        # Only apply to CADE SEI document URLs
        if 'sei.cade.gov.br' not in url:
            return None

        # Check 1: Filename from Content-Disposition (if we can get it cheaply)
        # We already fetched the content, but we can do a HEAD to get the filename
        # without re-downloading. For efficiency, check the text content first.

        # Check 2: Content-based detection (first ~500 chars)
        text_preview = linked_text[:500] if linked_text else ''
        match = CADE_BOILERPLATE_CONTENT_PATTERNS.search(text_preview)
        if match:
            return f"content match: '{match.group()[:50]}'"

        # Check 3: Filename-based detection via HEAD request
        try:
            headers = {
                "User-Agent": "MergerArbDashboard/1.0 (merger-arb-research@outlook.com)"}
            resp = requests.head(url, headers=headers,
                                 timeout=10, allow_redirects=True)
            cd = resp.headers.get('Content-Disposition', '')
            fname_match = re.search(r'filename="?([^"]+)"?', cd)
            if fname_match:
                fname = fname_match.group(1)
                if CADE_BOILERPLATE_FILENAME_PATTERNS.search(fname):
                    return f"filename match: '{fname}'"
        except Exception:
            # If HEAD fails, keep the document (err on the side of inclusion)
            pass

        return None

    def build_combined_text(self, email_content: Dict) -> Dict:
        """Build combined document from email body + linked documents.

        Fetches all linked URLs, filters out known boilerplate documents
        (payment receipts, proxies, access requests — see CADE_BOILERPLATE_*
        constants for full list), and concatenates into a single text block
        for LLM summarization.

        Returns dict with:
          'text': combined text string
          'fetch_results': {
              'succeeded': [urls that were fetched and included],
              'failed': [urls that could not be fetched],
              'filtered': [{'url': str, 'reason': str} for boilerplate docs],
              'total': int (total URLs attempted)
          }
        """
        parts = []
        fetch_succeeded = []
        fetch_failed = []
        fetch_filtered = []

        # Email body is content (not just a pointer like 8-K emails)
        body_text = email_content.get('text_body', '').strip()
        if body_text:
            parts.append("=== EMAIL BODY ===\n\n" + body_text)

        # Fetch each linked URL
        urls = email_content.get('urls', [])
        if urls:
            logger.info(f"   Found {len(urls)} URL(s) to fetch")
            for url in urls:
                logger.info(f"   Fetching: {url}")
                linked_text = self._fetch_url_text(url)
                if linked_text.strip():
                    # Check if this is a boilerplate document before including
                    boilerplate_reason = self._is_boilerplate_document(
                        url, linked_text)
                    if boilerplate_reason:
                        fetch_filtered.append(
                            {'url': url, 'reason': boilerplate_reason})
                        logger.info(
                            f"   FILTERED (boilerplate): {boilerplate_reason}")
                    else:
                        parts.append(
                            f"=== LINKED DOCUMENT: {url} ===\n\n" + linked_text)
                        fetch_succeeded.append(url)
                else:
                    # CRITICAL: Tell the LLM what it DOESN'T have
                    parts.append(
                        f"=== SOURCE UNAVAILABLE: {url} ===\n"
                        f"WARNING: This linked document could not be fetched (access restricted, "
                        f"JS-rendered, or server error). You have ONLY the email body above — "
                        f"NOT the full document text. Do NOT infer document content from titles or labels alone."
                    )
                    fetch_failed.append(url)

        if fetch_filtered:
            logger.info(
                f"   BOILERPLATE FILTERED: {len(fetch_filtered)}/{len(urls)} document(s) "
                f"excluded (payment receipts, proxies, access requests)"
            )
        if fetch_failed:
            logger.warning(
                f"   FETCH FAILURES: {len(fetch_failed)}/{len(urls)} URL(s) could not be fetched"
            )
            for fu in fetch_failed:
                logger.warning(f"     FAILED: {fu}")

        combined = "\n\n".join(parts)

        # Safety-valve truncation only — the Haiku extraction pass (50K-word
        # chunks, auto-parallel) handles condensation.  The old 15K cap silently
        # discarded most questionnaire responses when 60+ docs were fetched.
        words = combined.split()
        if len(words) > 150000:
            combined = " ".join(words[:150000])
            logger.info(
                f"   Truncated combined text to 150000 words (safety valve)")

        logger.info(f"   Total combined text: {len(combined.split())} words "
                    f"({len(fetch_succeeded)} docs included, "
                    f"{len(fetch_filtered)} boilerplate filtered, "
                    f"{len(fetch_failed)} fetch failures)")

        return {
            'text': combined,
            'fetch_results': {
                'succeeded': fetch_succeeded,
                'failed': fetch_failed,
                'filtered': fetch_filtered,
                'total': len(urls),
            }
        }

    def summarize(self, text: str) -> Dict:
        """Generate summary using two-pass extraction (Haiku) then Opus summarization."""
        from fetch_utils import extract_relevant_sections

        # Pass 1: Haiku extraction
        logger.info("   Pass 1: Extracting relevant sections via Haiku...")
        extracted = extract_relevant_sections(text, EXTRACTION_GUIDANCE)
        logger.info(f"   Extracted {len(extracted.split())} words")

        # Pass 2: Opus summarization
        logger.info("   Pass 2: Generating summary via Claude Opus...")
        msg = self.anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16384,
            messages=[{
                "role": "user",
                "content": SUMMARY_PROMPT + "\n\n" + extracted
            }]
        )

        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        # Handle truncated JSON
        if msg.stop_reason != "end_turn":
            if raw.count('{') > raw.count('}'):
                raw += '"' + '}' * (raw.count('{') - raw.count('}'))
            if raw.count('[') > raw.count(']'):
                raw += ']' * (raw.count('[') - raw.count(']'))

        summary = json.loads(raw)
        logger.info(
            f"   Generated summary: {summary.get('jurisdiction', '?')} / {summary.get('regulatory_body', '?')}")
        return summary

    def _validate_high_stakes_claims(self, summary: Dict, combined_text: str) -> Dict:
        """Validate high-stakes claims (approvals, blocks) against source evidence.

        Returns modified summary with claims downgraded if unsupported.
        """
        approval = summary.get('case_snapshot', {}).get('approval_status', '')
        decision = summary.get('decision_detail', {})
        source_quality = summary.get('source_quality', {})

        HIGH_STAKES_DECISIONS = ['clearance',
                                 'conditional_clearance', 'blocking']

        is_high_stakes = (
            any(kw in approval for kw in ('Approved', 'Blocked', 'Conditions')) or
            decision.get('decision_type') in HIGH_STAKES_DECISIONS
        )

        if not is_high_stakes:
            return summary

        # Check 1: Does the source quality indicate we have full document text?
        has_full_text = source_quality.get('has_full_document_text', False)
        basis = source_quality.get('basis_for_claims', 'unknown')

        # Check 2: Does the combined text contain explicit approval/blocking language?
        approval_evidence_terms = [
            'aprovação', 'aprovado', 'approved', 'clearance', 'cleared',
            'sem restrições', 'without conditions', 'unconditional',
            'aprovação sem restrições', 'merger cleared',
            'denied', 'blocked', 'prohibition', 'proibição'
        ]
        text_lower = combined_text.lower()
        has_evidence = any(
            term in text_lower for term in approval_evidence_terms)

        if not has_full_text and not has_evidence:
            # DOWNGRADE: Insufficient evidence for high-stakes claim
            logger.warning(
                f"   HIGH-STAKES CLAIM DOWNGRADED: '{approval}' / "
                f"decision_type='{decision.get('decision_type')}' — "
                f"source basis is '{basis}', no approval language found in text"
            )

            # Reset to safe defaults
            summary['case_snapshot']['approval_status'] = 'Pending - Under Review'
            summary['decision_detail'] = {'has_decision': False}
            summary['significance'] = 'low'
            summary['significance_reasoning'] = (
                'DOWNGRADED: Approval/blocking claim could not be verified against source text. '
                'Only docket entry available — actual document content inaccessible.'
            )

            # Rewrite headline to remove false claim
            headline_key = 'L1_headline' if 'L1_headline' in summary else 'headline'
            headline = summary.get(headline_key, '')
            if any(w in headline.lower() for w in ['approves', 'approved', 'blocks', 'blocked', 'clears', 'cleared', 'denies', 'denied']):
                summary[headline_key] = headline.replace('approves', 'files decision order for') \
                                                .replace('approved', 'filed decision order for') \
                                                .replace('blocks', 'files order regarding') \
                                                .replace('clears', 'files order regarding')
                summary['_downgraded'] = True

        return summary

    def format_summary_email(self, summary: Dict, urls: List[str],
                             update_number: int = 1, deal_entry: Optional[Dict] = None) -> str:
        """Format summary as HTML email — L1/L2/L3 layout.

        L1: Headline (5-second scan)
        L2: Brief + filing cards (30-second read)
        L3: Conditional detail block (deep dive)
        Case Snapshot: Compact reference
        Footer: Source links, source notes, attribution
        """
        jurisdiction = summary.get('jurisdiction', 'INTL')
        authority = summary.get('regulatory_body', '')
        date = summary.get('document_date', '')
        doc_type = summary.get('document_type', '')
        process_num = summary.get('process_number', '')
        parties = summary.get('parties', {})
        cs = summary.get('case_snapshot', {})
        headline = summary.get('L1_headline', summary.get('headline', ''))
        brief = summary.get('L2_brief', summary.get('what_happened', ''))
        review_stage = cs.get('review_stage', '')
        approval_status = cs.get('approval_status', '')

        # ── Significance badge ──
        sig = summary.get('significance', 'medium')
        sig_colors = {'critical': '#c62828', 'high': '#e65100',
                      'medium': '#1565c0', 'low': '#616161', 'routine': '#9e9e9e'}
        sig_color = sig_colors.get(sig, '#616161')

        # ── Source issue indicator for header ──
        source_quality = summary.get('source_quality', {})
        fetch_results = summary.get('_fetch_results', {})
        failed_urls = fetch_results.get('failed', [])
        total_urls = fetch_results.get('total', 0)
        has_source_issues = bool(failed_urls) or source_quality.get(
            'basis_for_claims') == 'docket_table_only'
        source_indicator = (' &nbsp;<span style="font-size:11px;" title="Source limitations — see footer">'
                            '&#9888;</span>') if has_source_issues else ''

        # ── Status badges ──
        status_color = self.STATUS_COLORS.get(approval_status, '#616161')
        stage_badge = (f'<span style="display:inline-block;padding:3px 10px;border-radius:3px;'
                       f'font-size:11px;font-weight:bold;color:white;background-color:#006644;">'
                       f'{review_stage}</span>') if review_stage else ''
        status_badge = (f'<span style="display:inline-block;padding:3px 10px;border-radius:3px;'
                        f'font-size:11px;font-weight:bold;color:white;background-color:{status_color};">'
                        f'{approval_status}</span>') if approval_status else ''
        badge_separator = ' &nbsp; ' if stage_badge and status_badge else ''

        # ── Case status (UPDATE #N / NEW CASE + tracking) ──
        case_status_html = self._build_case_status_html(
            update_number, deal_entry, summary)

        # ── L2: Filing cards ──
        filing_cards_html = ""
        new_filings = summary.get('new_filings', [])
        if new_filings:
            seen_type_explanations = set()
            for f in new_filings:
                first_badge = ""
                if f.get('is_first_occurrence'):
                    first_badge = (' <span style="display:inline-block;padding:2px 8px;border-radius:3px;'
                                   'font-size:10px;font-weight:bold;color:white;background-color:#2e7d32;'
                                   'vertical-align:middle;">FIRST REPORTED</span>')
                source_link = ""
                if f.get('source_url'):
                    source_link = (f' <a href="{f["source_url"]}" target="_blank" '
                                   f'style="font-size:12px;color:#4a90e2;">View source</a>')

                # Deduplicate filing_type_explanation — show only on first card of each type
                type_exp = f.get('filing_type_explanation', '') or ''
                type_exp_html = ""
                if type_exp and type_exp not in seen_type_explanations:
                    seen_type_explanations.add(type_exp)
                    type_exp_html = (f'<div style="font-style:italic;color:#888;font-size:13px;margin:6px 0;">'
                                     f'{type_exp}</div>')

                # Source link only (what_it_means is covered by L2_brief)
                link_html = ""
                if source_link:
                    link_html = f' {source_link}'

                filing_cards_html += (
                    f'<div style="margin:8px 0 0 0;padding:8px 12px;background:#f9f9f9;border:1px solid #e8e8e8;'
                    f'border-radius:3px;font-size:13px;color:#555;">'
                    f'<strong style="color:#333;">{f.get("filing_description", "")}</strong>{first_badge}{link_html}'
                    f'{type_exp_html}'
                    f'</div>'
                )

        # ── L3: Detail block (conditional — only the relevant one) ──
        detail_html = ""
        detail_label = ""

        # Questionnaire detail
        qd = summary.get('questionnaire_detail', {})
        if qd.get('has_questionnaires') and qd.get('respondents'):
            detail_label = "Questionnaire Detail"
            respondent_cards = ""
            for r in qd['respondents']:
                date_parts = []
                if r.get('date_issued'):
                    date_parts.append(f"Issued: {r['date_issued']}")
                if r.get('date_responded'):
                    date_parts.append(f"Responded: {r['date_responded']}")
                if r.get('response_deadline'):
                    date_parts.append(f"Deadline: {r['response_deadline']}")
                dates_line = f'<div style="font-size:12px;color:#666;margin:4px 0;">{" | ".join(date_parts)}</div>' if date_parts else ""

                topics = ''.join(
                    f'<li>{t}</li>' for t in (r.get('topics_covered') or []) if t)
                topics_html = f'<div style="margin-top:6px;"><strong style="font-size:12px;color:#555;">Topics:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{topics}</ul></div>' if topics else ""

                positions = ''.join(
                    f'<li>{p}</li>' for p in (r.get('key_positions_stated') or []) if p)
                positions_html = f'<div style="margin-top:6px;"><strong style="font-size:12px;color:#555;">Positions:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{positions}</ul></div>' if positions else ""

                concerns = ''.join(
                    f'<li>{c}</li>' for c in (r.get('concerns_raised') or []) if c)
                concerns_html = f'<div style="margin-top:6px;"><strong style="font-size:12px;color:#555;">Concerns Raised:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{concerns}</ul></div>' if concerns else ""

                source_link = ""
                if r.get('source_url'):
                    source_link = f' <a href="{r["source_url"]}" target="_blank" style="font-size:11px;color:#4a90e2;">Link</a>'

                type_badge = f' <span style="font-size:11px;color:#666;font-style:italic;">({r.get("questionnaire_type", "")})</span>' if r.get(
                    'questionnaire_type') else ""

                # Takeaway one-liner
                takeaway = r.get('takeaway') or ''
                takeaway_html = ""
                if takeaway:
                    takeaway_html = (
                        f'<div style="font-size:13px;font-weight:bold;color:#006644;margin:4px 0;'
                        f'padding:4px 8px;background:#e8f5e9;border-radius:3px;">'
                        f'{takeaway}</div>'
                    )

                respondent_cards += (
                    f'<div style="margin:8px 0;padding:10px;background:#fff;border:1px solid #e0e0e0;border-radius:3px;">'
                    f'<div style="font-weight:bold;color:#333;">{r.get("entity_name", "")}{type_badge}{source_link}</div>'
                    f'<div style="font-size:12px;color:#888;margin:2px 0;">{r.get("role_description", "")}</div>'
                    f'{takeaway_html}'
                    f'{dates_line}{topics_html}{positions_html}{concerns_html}'
                    f'</div>'
                )
            detail_html += respondent_cards

        # Remedy detail
        rd = summary.get('remedy_detail', {})
        if rd.get('has_remedies') and rd.get('remedies'):
            detail_label = detail_label or "Remedy Detail"
            remedy_type = rd.get('remedy_type', '')
            proposed_by = rd.get('proposed_by', '')
            detail_html += f'<div style="font-size:13px;color:#666;margin-bottom:8px;">Type: {remedy_type} | Proposed by: {proposed_by}</div>'
            for rem in rd['remedies']:
                markets = ''.join(
                    f'<li>{m}</li>' for m in rem.get('markets_addressed', []) if m)
                markets_html = f'<ul style="margin:2px 0;padding-left:18px;font-size:13px;">{markets}</ul>' if markets else ""
                commitments = ''.join(
                    f'<li>{c}</li>' for c in rem.get('behavioral_commitments', []) if c)
                commitments_html = f'<div style="margin-top:4px;"><strong style="font-size:12px;">Behavioral:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{commitments}</ul></div>' if commitments else ""
                assets_line = f'<div style="font-size:13px;margin:4px 0;"><strong>Assets:</strong> {rem["divestiture_assets"]}</div>' if rem.get(
                    'divestiture_assets') else ""
                buyer_line = f'<div style="font-size:13px;margin:4px 0;"><strong>Buyer:</strong> {rem["divestiture_buyer"]}</div>' if rem.get(
                    'divestiture_buyer') else ""
                duration_line = f'<div style="font-size:13px;margin:4px 0;"><strong>Duration:</strong> {rem["duration"]}</div>' if rem.get(
                    'duration') else ""

                detail_html += (
                    f'<div style="margin:8px 0;padding:10px;background:#fff;border:1px solid #e0e0e0;border-radius:3px;">'
                    f'<div style="font-weight:bold;color:#333;">{rem.get("description", "")}</div>'
                    f'{assets_line}{buyer_line}{markets_html}{commitments_html}{duration_line}</div>'
                )

        # Objection detail
        od = summary.get('objection_detail', {})
        if od.get('has_objections') and od.get('concerns'):
            detail_label = detail_label or "Objection Detail"
            for c in od['concerns']:
                markets = ''.join(
                    f'<li>{m}</li>' for m in c.get('markets_affected', []) if m)
                markets_html = f'<div style="margin-top:4px;"><strong style="font-size:12px;">Markets:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{markets}</ul></div>' if markets else ""
                evidence = ''.join(
                    f'<li>{e}</li>' for e in c.get('evidence_cited', []) if e)
                evidence_html = f'<div style="margin-top:4px;"><strong style="font-size:12px;">Evidence:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{evidence}</ul></div>' if evidence else ""

                detail_html += (
                    f'<div style="margin:8px 0;padding:10px;background:#fff;border:1px solid #e0e0e0;border-radius:3px;">'
                    f'<div style="font-weight:bold;color:#333;">Theory: {c.get("theory_of_harm", "")}</div>'
                    f'<div style="font-size:13px;color:#333;margin:4px 0;">{c.get("description", "")}</div>'
                    f'{markets_html}{evidence_html}</div>'
                )

        # Intervention detail
        ivd = summary.get('intervention_detail', {})
        if ivd.get('has_interventions') and ivd.get('intervenors'):
            detail_label = detail_label or "Intervention Detail"
            for iv in ivd['intervenors']:
                position = iv.get('position', '')
                pos_color = '#2e7d32' if position == 'supports' else '#c62828' if position == 'opposes' else '#616161'
                pos_badge = (f' <span style="display:inline-block;padding:2px 8px;border-radius:3px;'
                             f'font-size:10px;font-weight:bold;color:white;background-color:{pos_color};">'
                             f'{position.upper()}</span>') if position else ""
                args = ''.join(
                    f'<li>{a}</li>' for a in iv.get('key_arguments', []) if a)
                args_html = f'<ul style="margin:4px 0;padding-left:18px;font-size:13px;">{args}</ul>' if args else ""
                remedies_req = ''.join(
                    f'<li>{r}</li>' for r in iv.get('remedies_requested', []) if r)
                remedies_html = f'<div style="margin-top:4px;"><strong style="font-size:12px;">Remedies Requested:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{remedies_req}</ul></div>' if remedies_req else ""

                detail_html += (
                    f'<div style="margin:8px 0;padding:10px;background:#fff;border:1px solid #e0e0e0;border-radius:3px;">'
                    f'<div style="font-weight:bold;color:#333;">{iv.get("entity_name", "")} '
                    f'<span style="font-size:11px;color:#666;">({iv.get("entity_type", "")})</span>{pos_badge}</div>'
                    f'{args_html}{remedies_html}</div>'
                )

        # Information request detail
        ird = summary.get('information_request_detail', {})
        if ird.get('has_info_request') and ird.get('requests'):
            detail_label = detail_label or "Information Request Detail"
            for req in ird['requests']:
                data_items = ''.join(
                    f'<li>{d}</li>' for d in req.get('data_requested', []) if d)
                data_html = f'<ul style="margin:4px 0;padding-left:18px;font-size:13px;">{data_items}</ul>' if data_items else ""
                scope_line = f'<div style="font-size:13px;margin:4px 0;"><strong>Scope:</strong> {req["scope"]}</div>' if req.get(
                    'scope') else ""
                deadline_line = f'<div style="font-size:13px;margin:4px 0;"><strong>Deadline:</strong> {req["deadline"]}</div>' if req.get(
                    'deadline') else ""
                basis_line = f'<div style="font-size:13px;margin:4px 0;"><strong>Legal Basis:</strong> {req["legal_basis"]}</div>' if req.get(
                    'legal_basis') else ""

                detail_html += (
                    f'<div style="margin:8px 0;padding:10px;background:#fff;border:1px solid #e0e0e0;border-radius:3px;">'
                    f'<div style="font-weight:bold;color:#333;">From: {req.get("requested_from", "")} '
                    f'({req.get("request_type", "")})</div>'
                    f'{data_html}{scope_line}{deadline_line}{basis_line}</div>'
                )

        # Decision detail
        dd = summary.get('decision_detail', {})
        if dd.get('has_decision'):
            detail_label = detail_label or "Decision Detail"
            dtype = dd.get('decision_type', '')
            dtype_color = '#2e7d32' if 'clearance' in dtype else '#c62828' if 'blocking' in dtype else '#1565c0'
            dtype_badge = (f'<span style="display:inline-block;padding:4px 12px;border-radius:3px;'
                           f'font-size:12px;font-weight:bold;color:white;background-color:{dtype_color};">'
                           f'{dtype.replace("_", " ").upper()}</span>')
            conditions = ''.join(
                f'<li>{c}</li>' for c in dd.get('conditions_imposed', []) if c)
            conditions_html = f'<div style="margin-top:6px;"><strong>Conditions:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{conditions}</ul></div>' if conditions else ""

            detail_html += (
                f'<div style="margin-bottom:8px;">{dtype_badge}</div>'
                f'<div style="font-size:13px;color:#333;">'
                f'{"<strong>Authority:</strong> " + dd["decision_authority"] + "<br>" if dd.get("decision_authority") else ""}'
                f'{"<strong>Date:</strong> " + dd["decision_date"] + "<br>" if dd.get("decision_date") else ""}'
                f'{"<strong>Legal Basis:</strong> " + dd["legal_basis"] + "<br>" if dd.get("legal_basis") else ""}'
                f'{"<strong>Effective:</strong> " + dd["effective_date"] + "<br>" if dd.get("effective_date") else ""}'
                f'{"<strong>Appeal Deadline:</strong> " + dd["appeal_deadline"] + "<br>" if dd.get("appeal_deadline") else ""}'
                f'{"<strong>Panel:</strong> " + dd["vote_or_panel"] if dd.get("vote_or_panel") else ""}'
                f'</div>{conditions_html}'
            )

        # Wrap L3 detail in a single section if any detail exists
        l3_html = ""
        if detail_html:
            l3_html = (
                f'<div style="margin:20px 0;">'
                f'<h3 style="margin:0 0 8px 0;color:#006644;font-size:13px;letter-spacing:0.5px;">L3 &mdash; {detail_label}</h3>'
                f'<div style="padding:15px;background-color:#f5f5f5;border-radius:5px;">'
                f'{detail_html}</div></div>'
            )

        # ── Case Snapshot ──
        snapshot_timeline = cs.get('timeline', {})
        timeline_items = ''.join(
            f'<li><strong>{k.replace("_", " ").title()}:</strong> {v}</li>'
            for k, v in snapshot_timeline.items() if v
        )
        timeline_html = f'<ul style="margin:4px 0;padding-left:18px;font-size:13px;">{timeline_items}</ul>' if timeline_items else ""
        markets = ''.join(
            f'<li>{m}</li>' for m in cs.get('relevant_markets', []) if m)
        markets_html = f'<div style="margin-top:6px;"><strong style="font-size:12px;">Markets:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{markets}</ul></div>' if markets else ""
        related = ''.join(
            f'<li>{r}</li>' for r in cs.get('related_proceedings', []) if r)
        related_html = f'<div style="margin-top:6px;"><strong style="font-size:12px;">Related:</strong><ul style="margin:2px 0;padding-left:18px;font-size:13px;">{related}</ul></div>' if related else ""

        snapshot_html = (
            f'<div style="margin:20px 0;padding:15px;background-color:#f5f5f5;border-radius:5px;">'
            f'<h3 style="margin:0 0 10px 0;color:#006644;">Case Snapshot</h3>'
            f'<div style="font-size:13px;color:#333;">'
            f'<strong>Stage:</strong> {review_stage} &middot; '
            f'<strong>Status:</strong> {approval_status}'
            f'{"<br><strong>Legal Basis:</strong> " + cs["legal_basis"] if cs.get("legal_basis") else ""}'
            f'</div>{timeline_html}{markets_html}{related_html}</div>'
        )

        # ── Footer: main case link + source notes ──
        links_html = ""
        if urls:
            # Show only the first URL (main case/process page), not every individual doc
            main_url = urls[0]
            domain = re.search(r'https?://([^/]+)', main_url)
            domain_label = domain.group(1) if domain else main_url
            links_html = f'<p><a href="{main_url}" target="_blank">View case on {domain_label}</a></p>'

        source_notes_html = ""
        if failed_urls:
            failed_list = ''.join(
                f'<li style="font-size:11px;word-break:break-all;">{u}</li>'
                for u in failed_urls
            )
            source_notes_html += (
                f'<div style="margin:8px 0;padding:8px 12px;background-color:#fff3e0;border-radius:3px;font-size:12px;">'
                f'<strong style="color:#e65100;">&#9888; {len(failed_urls)} of {total_urls} source URL(s) '
                f'could not be fetched.</strong> Claims about their content should be verified.'
                f'<ul style="margin:4px 0;padding-left:16px;color:#666;">{failed_list}</ul>'
                f'</div>'
            )
        if source_quality.get('basis_for_claims') == 'docket_table_only':
            source_notes_html += (
                '<div style="margin:8px 0;padding:8px 12px;background-color:#fff3e0;border-radius:3px;font-size:12px;">'
                '<strong style="color:#e65100;">&#9888; Limited Source:</strong> '
                'Summary based on docket table listing only &mdash; full document text not available. '
                'Content should be independently verified.'
                '</div>'
            )

        html = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
                .header {{ background-color: #006644; color: white; padding: 20px; border-radius: 5px; }}
                .header h2 {{ margin: 0 0 5px 0; }}
                .header p {{ margin: 3px 0; opacity: 0.9; }}
                .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 12px; color: #999; }}
                a {{ color: #4a90e2; text-decoration: none; }}
            </style>
        </head>
        <body>
            <!-- HEADER -->
            <div class="header">
                <h2>{jurisdiction} &mdash; {authority}</h2>
                <p>Process: {process_num} | {date} &nbsp;
                    <span style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;color:white;background-color:{sig_color};">{sig.upper()}</span>{source_indicator}</p>
                <p>Target: {parties.get('target', 'N/A')} | Acquirer: {parties.get('acquirer', 'N/A')}</p>
                <p>{stage_badge}{badge_separator}{status_badge}</p>
            </div>

            {case_status_html}

            <!-- L1: HEADLINE -->
            <div style="margin:20px 0;">
                <h3 style="margin:0 0 8px 0;color:#006644;font-size:13px;letter-spacing:0.5px;">L1 &mdash; Headline</h3>
                <div style="padding:14px 18px;border-left:4px solid #006644;background-color:#e8f5e9;border-radius:3px;">
                    <div style="font-size:16px;font-weight:bold;color:#006644;line-height:1.4;">{headline}</div>
                </div>
            </div>

            <!-- L2: BRIEF -->
            <div style="margin:20px 0;">
                <h3 style="margin:0 0 8px 0;color:#006644;font-size:13px;letter-spacing:0.5px;">L2 &mdash; Brief</h3>
                <div style="font-size:14px;color:#333;line-height:1.6;">{brief}</div>
                {filing_cards_html}
            </div>

            <!-- L3: DETAIL -->
            {l3_html}

            <!-- CASE SNAPSHOT -->
            {snapshot_html}

            <!-- FOOTER -->
            <div class="footer">
                {links_html}
                {source_notes_html}
                <p>Automated International Regulatory Summary generated by Hyperion Technologies</p>
                <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
        </body>
        </html>
        """

        return html

    def _send_filtered_notification(self, summary: Dict, original_subject: str,
                                    significance: str, reason: str,
                                    update_number: int = 0, deal_entry: Optional[Dict] = None):
        """Send brief 'filtered' notification to josh@ only for low-value updates."""
        try:
            headline = summary.get('L1_headline', summary.get('headline', ''))
            jurisdiction = summary.get('jurisdiction', '')
            body = summary.get('regulatory_body', '')
            target = summary.get('parties', {}).get('target', '')
            acquirer = summary.get('parties', {}).get('acquirer', '')

            # Find the last emailed update for context
            last_emailed = ''
            if deal_entry and deal_entry.get('updates'):
                for u in reversed(deal_entry['updates']):
                    u_headline = u.get('L1_headline', u.get('headline', ''))
                    if u.get('significance') not in ('low', 'routine') and u_headline:
                        last_emailed = u_headline
                        break

            context_line = ''
            if update_number and last_emailed:
                context_line = (f'<div style="font-size:12px;color:#888;margin-bottom:12px;">'
                                f'This is update #{update_number} for <b>{acquirer} / {target}</b>.<br>'
                                f'Last emailed update: <i>{last_emailed}</i></div>')
            elif update_number:
                context_line = (f'<div style="font-size:12px;color:#888;margin-bottom:12px;">'
                                f'Update #{update_number} for <b>{acquirer} / {target}</b></div>')

            html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;padding:16px;">
<div style="background:#f5f5f5;border-left:4px solid #ccc;padding:12px;margin-bottom:12px;">
<span style="color:#999;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
Filtered — {significance}</span>
<div style="font-size:14px;color:#666;margin-top:6px;">{headline}</div>
</div>
{context_line}
<div style="font-size:13px;color:#555;line-height:1.6;background:#fffbe6;border:1px solid #f0e68c;padding:10px;border-radius:4px;">
<b>Why filtered:</b> {reason}
</div>
<div style="font-size:11px;color:#bbb;margin-top:16px;border-top:1px solid #eee;padding-top:8px;">
Full JSON saved locally. Not sent to distribution list.
</div>
</div>"""

            msg = MIMEMultipart('alternative')
            msg['From'] = self.gmail_email
            msg['To'] = self.gmail_email  # josh@ only
            msg['Subject'] = f"[Filtered] {original_subject}"
            msg.attach(MIMEText(html, 'html'))

            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(self.gmail_email, self.gmail_password)
                server.sendmail(self.gmail_email, [
                                self.gmail_email], msg.as_string())
            logger.info(f"   Sent filtered notification to {self.gmail_email}")
        except Exception as e:
            logger.warning(f"   Failed to send filtered notification: {e}")

    def send_summary_email(self, summary: Dict, urls: List[str],
                           update_number: int = 1, deal_entry: Optional[Dict] = None,
                           original_subject: str = ''):
        """Send summary email via Gmail SMTP."""
        l1 = summary.get('L1_headline', summary.get('headline', ''))
        if original_subject and l1:
            subject = f"{original_subject} | {l1}"
        else:
            subject = original_subject or l1 or 'Intl Regulatory Update'

        html_body = self.format_summary_email(
            summary, urls, update_number, deal_entry)

        all_recipients = [self.gmail_email] + SUMMARY_RECIPIENTS

        msg = MIMEMultipart('alternative')
        msg['From'] = self.gmail_email
        msg['To'] = ', '.join(all_recipients)
        msg['Subject'] = subject

        msg.attach(MIMEText(html_body, 'html'))

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(self.gmail_email, self.gmail_password)
                server.sendmail(self.gmail_email,
                                all_recipients, msg.as_string())
            logger.info(
                f"Successfully sent summary email to {len(all_recipients)} recipients: {subject}")
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            raise

    def save_outputs(self, summary: Dict, email_subject: str):
        """Save JSON and DOCX outputs."""
        from intl_regulatory_summary import export_docx

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        jurisdiction = summary.get('jurisdiction', 'INTL')
        safe_jurisdiction = re.sub(r'[^\w\-\.]', '_', jurisdiction)
        parties = summary.get('parties', {})
        target = parties.get('target', 'UNKNOWN')
        safe_target = re.sub(r'[^\w\-\.]', '_', target)
        date = summary.get('document_date', '')
        safe_date = date.replace('/', '-')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        # JSON
        json_path = OUTPUT_DIR / \
            f"intl_reg_{safe_jurisdiction}_{safe_target}_{timestamp}.json"
        json_path.write_text(json.dumps(summary, indent=2))
        logger.info(f"   JSON saved: {json_path}")

        # DOCX
        docx_filename = f"IntlReg_{safe_jurisdiction}_{safe_target}_{safe_date}_{timestamp}.docx"
        docx_path = export_docx(summary, str(OUTPUT_DIR / docx_filename))
        logger.info(f"   DOCX saved: {docx_path}")

    def process_email(self, email_data: Dict):
        """Process a single international regulatory email."""
        try:
            subject = email_data['subject']
            content = email_data['content']
            urls = content.get('urls', [])

            logger.info(f"Processing: {subject}")

            # 1. Extract deal key + metadata from email body + subject
            body_text = content.get('text_body', '')
            deal_key, deal_meta = self._extract_deal_key(body_text, subject)
            logger.info(f"   Deal key: {deal_key}")

            # 1b. Key reconciliation — if we resolved to a process number but an
            # earlier email created a ticker_agency entry, migrate it so history
            # isn't lost under a stale key.
            if deal_meta.get('process_number') and deal_meta.get('ticker') and deal_meta.get('subject_agency'):
                ticker = deal_meta['ticker']
                agency = deal_meta['subject_agency']
                old_key = re.sub(r'[^a-z0-9]+', '_',
                                 f"{ticker}_{agency}".lower()).strip('_')
                if old_key != deal_key and old_key in self.deal_state and deal_key not in self.deal_state:
                    logger.info(
                        f"   Migrating deal state: {old_key} → {deal_key}")
                    self.deal_state[deal_key] = self.deal_state.pop(old_key)
                    self.deal_state[deal_key]['process_number'] = deal_meta['process_number']

            # 2. Look up existing deal entry for history context
            deal_entry = self.deal_state.get(deal_key)
            history_context = ""
            if deal_entry:
                history_context = self._build_deal_history_context(deal_entry)
                logger.info(
                    f"   Found existing deal with {deal_entry['update_count']} prior update(s)")

            # 3. Build combined text from email body + linked documents
            build_result = self.build_combined_text(content)
            combined_text = build_result['text']
            fetch_results = build_result['fetch_results']

            if not combined_text.strip():
                logger.warning(f"   No text content found in email: {subject}")
                return

            # 4. Prepend deal history context to combined text
            if history_context:
                combined_text = history_context + "\n\n" + combined_text

            # 5. Generate summary (two-pass: Haiku extraction + Opus summarization)
            summary = self.summarize(combined_text)

            # 5a. Attach fetch results metadata to summary for email rendering
            summary['_fetch_results'] = fetch_results

            # 5c. Validate high-stakes claims before sending
            summary = self._validate_high_stakes_claims(summary, combined_text)

            # 5b. CADE docket backfill — on first encounter, pre-seed historical milestones
            backfill = []
            if deal_key not in self.deal_state:
                cade_urls = [
                    u for u in urls if 'sei.cade.gov.br' in u and 'md_pesq_processo_exibir' in u]
                if cade_urls:
                    logger.info(
                        f"   New CADE case — attempting docket backfill from SEI page")
                    backfill = self._backfill_cade_docket(cade_urls[0])
                    logger.info(
                        f"   Backfill: extracted {len(backfill)} historical milestones")

            # 6. Update deal state with new summary results
            update_number = self._update_deal_state(deal_key, deal_meta, summary, email_data,
                                                    backfill_entries=backfill or None)
            logger.info(f"   Deal state updated — update #{update_number}")

            # 7. Save deal state to disk
            self._save_deal_state()

            # 7b. Significance gate — skip email for low-value updates
            significance = summary.get('significance', 'medium')
            sig_reason = summary.get('significance_reasoning', '')

            # Hard override: always email for VALIDATED approvals/blocks
            approval = summary.get('case_snapshot', {}).get(
                'approval_status', '')
            if any(kw in approval for kw in ('Approved', 'Blocked', 'Conditions')):
                if not summary.get('_downgraded'):
                    significance = 'critical'
                else:
                    logger.warning(
                        f"   Approval/block claim was downgraded — NOT escalating to critical")

            is_new_deal = (update_number == 1 and not backfill)

            if significance in ('low', 'routine') and not is_new_deal:
                logger.info(
                    f"   Significance: {significance} — filtered ({sig_reason})")
                # Send brief notification to josh@ only (not full distribution)
                current_deal_entry = self.deal_state.get(deal_key, {})
                self._send_filtered_notification(summary, subject, significance, sig_reason,
                                                 update_number, current_deal_entry)
                self.save_outputs(summary, subject)
                self._save_processed_email(email_data['email_id'])
                logger.info(f"Successfully processed (filtered): {subject}")
                return

            logger.info(f"   Significance: {significance} — sending email")

            # 8. Send summary email (with deal context)
            current_deal_entry = self.deal_state.get(deal_key, {})
            self.send_summary_email(summary, urls, update_number, current_deal_entry,
                                    original_subject=subject)

            # 9. Save outputs
            self.save_outputs(summary, subject)

            # 10. Mark as processed
            self._save_processed_email(email_data['email_id'])

            logger.info(f"Successfully processed: {subject}")

        except Exception as e:
            logger.error(f"Error processing email: {e}", exc_info=True)

    def _mark_all_as_seen(self):
        """Set UID high-water mark to skip backlog without fetching any emails.

        Used by live-only mode — simply records the highest current UID so the
        incremental scan only picks up truly new messages.
        """
        try:
            mail = self.connect_to_gmail()
            mail.select('inbox')

            # Get the highest UID from any Hyperion sender in the last 30 days
            thirty_days_ago = (
                datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
            all_uids = set()
            for sender in SENDERS:
                _, data = mail.uid('SEARCH', None,
                                   f'(FROM "{sender}" SINCE {thirty_days_ago})')
                if data[0]:
                    all_uids.update(int(u) for u in data[0].split())

            if all_uids:
                self._last_uid = max(all_uids)
                logger.info(f"High-water UID set to {self._last_uid} "
                            f"({len(all_uids)} existing emails skipped)")
            else:
                self._last_uid = 0
                logger.info("No existing emails found — starting from UID 0")

            mail.logout()
        except Exception as e:
            logger.error(
                f"Error setting UID high-water mark: {e}", exc_info=True)
            self._last_uid = 0

    def run(self, check_interval: int = 60, live_only: bool = False):
        """Run the monitor continuously.

        Args:
            check_interval: Seconds between inbox checks.
            live_only: If True, skip backlog processing — mark all existing
                       emails as seen and only process new arrivals.
        """
        logger.info("=" * 70)
        logger.info("Starting International Regulatory Filing Monitor")
        logger.info("=" * 70)
        logger.info(f"Monitoring: {self.gmail_email}")
        logger.info(f"Looking for emails from: {', '.join(SENDERS)}")
        logger.info(f"Excluding: 8-K filings (handled by 8K monitor)")
        logger.info(f"Matching: [FRMD] subject tag required")
        logger.info(
            f"Previously processed: {len(self.processed_emails)} emails")
        logger.info(
            f"Mode: {'LIVE ONLY (new emails only)' if live_only else 'CATCHUP + LIVE'}")
        # Deal state summary
        if self.deal_state:
            jurisdictions = set(e.get('jurisdiction', '?')
                                for e in self.deal_state.values())
            logger.info(
                f"Deal state: Tracking {len(self.deal_state)} deal(s) across {len(jurisdictions)} jurisdiction(s)")
        else:
            logger.info(f"Deal state: No deals tracked yet (fresh start)")
        logger.info(f"Check interval: {check_interval} seconds")
        logger.info("=" * 70)

        if live_only:
            # Mark everything currently in inbox as seen, then go straight to monitoring
            logger.info("\nLive-only mode: marking existing emails as seen...")
            self._mark_all_as_seen()
        else:
            # Initial catchup
            logger.info("\nPerforming initial catchup scan...")
            try:
                mail = self.connect_to_gmail()
                catchup_emails = self.check_for_new_emails(mail)

                if catchup_emails:
                    logger.info(
                        f"Found {len(catchup_emails)} unprocessed email(s)")
                    for email_data in catchup_emails:
                        self.process_email(email_data)
                    logger.info(
                        f"Catchup complete! Processed {len(catchup_emails)} email(s)")
                else:
                    logger.info(
                        "No unprocessed international regulatory emails found")

                mail.logout()
            except Exception as e:
                logger.error(
                    f"Error during initial catchup: {e}", exc_info=True)

        # Continuous monitoring
        logger.info("\n" + "=" * 70)
        logger.info(
            "Now monitoring for new international regulatory filings...")
        logger.info("   Press Ctrl+C to stop")
        logger.info("=" * 70 + "\n")

        while True:
            try:
                mail = self.connect_to_gmail()
                new_emails = self.check_for_new_emails(mail, incremental=True)

                if new_emails:
                    logger.info(f"Found {len(new_emails)} new email(s)")
                    for email_data in new_emails:
                        self.process_email(email_data)

                mail.logout()
                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("\n" + "=" * 70)
                logger.info("Monitor stopped by user")
                logger.info("=" * 70)
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                # Back off longer on errors (e.g., Gmail rate limits)
                time.sleep(max(check_interval, 300))


def main():
    """Main entry point."""
    import sys
    live_only = '--live' in sys.argv
    monitor = IntlRegulatoryMonitor()
    monitor.run(check_interval=60, live_only=live_only)


if __name__ == "__main__":
    main()

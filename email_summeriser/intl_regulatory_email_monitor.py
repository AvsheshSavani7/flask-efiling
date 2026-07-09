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
    'kaushal@hyperiontechnologies.ai',
    'info@hyperiontechnologies.ai',
    'alerts@hyperiontechnologies.ai'
]

# Recipients for summary emails (in addition to self)
SUMMARY_RECIPIENTS = [
    'josh@hyperiontechnologies.ai',
]
# Exclude 8-K emails (handled by the 8-K monitor)
EXCLUDE_PATTERN = re.compile(r'SEC Filing.*8-K', re.IGNORECASE)
# Subject-only pattern for international regulatory content.
# Matches the [FRMD] tag (foreign regulatory monitoring) or
# "TICKER: Agency - Regulatory Update" format from Hyperion.
INTL_SUBJECT_PATTERN = re.compile(
    r'\[FRMD\]|Regulatory Update.*\[FRMD\]|'
    r'CADE Brazil|Bundeskartellamt|ACCC.*(?:Case|Regulatory)|'
    r'CMA.*(?:Case|Regulatory)|EU Commission.*(?:Case|Regulatory)|'
    r'SAMR.*(?:Case|Regulatory)|COFECE.*(?:Case|Regulatory)|'
    r'KFTC.*(?:Case|Regulatory)|JFTC.*(?:Case|Regulatory)|'
    r'CNMC.*(?:Case|Regulatory)|Autorit[eé] de la concurrence.*(?:Case|Regulatory)',
    re.IGNORECASE
)

# URLs to filter out (noise)
NOISE_URL_PATTERNS = re.compile(
    r'unsubscribe|mailto:|facebook\.com|twitter\.com|linkedin\.com|'
    r'instagram\.com|youtube\.com|google-analytics|doubleclick|'
    r'mailchimp|sendgrid|list-manage|tracking|pixel|beacon',
    re.IGNORECASE
)

# Claude API settings
CLAUDE_MODEL = "claude-opus-4-6"

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
        """Build plain-text history block from prior updates for injection into combined text.

        Returns empty string if no prior updates exist.
        Caps at MAX_HISTORY_UPDATES most recent to preserve token budget.
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
            f"This is update #{update_number} for this case ({len(updates)} prior updates).",
        ]

        # Show only the most recent N updates to stay within token budget
        if len(updates) > self.MAX_HISTORY_UPDATES:
            skipped = len(updates) - self.MAX_HISTORY_UPDATES
            lines.append(f"  [... {skipped} earlier updates omitted ...]")
            recent = updates[-self.MAX_HISTORY_UPDATES:]
            start_idx = skipped + 1
        else:
            recent = updates
            start_idx = 1

        prev_status = None
        for i, update in enumerate(recent, start_idx):
            ts = update.get('timestamp', 'N/A')
            if 'T' in ts:
                ts = ts.split('T')[0]
            headline = update.get(
                'L1_headline', update.get('action_taken', 'N/A'))

            # Flag status transitions
            status = update.get('approval_status', '')
            stage = update.get('review_stage', '')
            transition = ""
            if prev_status and status and status != prev_status:
                transition = f" [STATUS CHANGE: {prev_status} → {status}]"
            prev_status = status or prev_status

            backfill_tag = " [from docket]" if update.get(
                'source') == 'backfill' else ""
            lines.append(f"  [{i}] {ts}: {headline}{transition}{backfill_tag}")

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

        Shows status badges and a timeline of all updates. Appears in every email.
        """
        d = summary.get('L3_detailed', {})
        review_stage = d.get('review_stage', '')
        approval_status = d.get('approval_status', '')
        status_color = self.STATUS_COLORS.get(approval_status, '#616161')

        # Pull case identifiers from deal_entry first, fall back to summary
        if deal_entry:
            process_num = deal_entry.get(
                'process_number', summary.get('process_number', ''))
            jurisdiction = deal_entry.get(
                'jurisdiction', summary.get('jurisdiction', ''))
            reg_body = deal_entry.get(
                'regulatory_body', summary.get('regulatory_body', ''))
            first_seen = deal_entry.get('first_seen', '')
            if 'T' in first_seen:
                first_seen = first_seen.split('T')[0]
        else:
            process_num = summary.get('process_number', '')
            jurisdiction = summary.get('jurisdiction', '')
            reg_body = summary.get('regulatory_body', '')
            first_seen = datetime.now().strftime('%Y-%m-%d')

        # Header: NEW CASE or UPDATE #N
        if update_number <= 1:
            header_label = "NEW CASE"
            tracking_line = f"First tracked: {first_seen}"
        else:
            header_label = f"UPDATE #{update_number}"
            count = deal_entry.get(
                'update_count', update_number) if deal_entry else update_number
            tracking_line = f"Tracked since {first_seen} &middot; {count} updates"

        # Agency label
        agency_label = reg_body if reg_body else jurisdiction
        case_id_line = f"{process_num} &middot; {agency_label}" if process_num else agency_label

        # Badge HTML (use &nbsp; separator — reliable across email clients)
        stage_badge = ""
        if review_stage:
            stage_badge = (
                f'<span style="display:inline-block;padding:4px 12px;border-radius:3px;'
                f'font-size:12px;font-weight:bold;color:white;background-color:#006644;">'
                f'{review_stage}</span>'
            )
        status_badge = ""
        if approval_status:
            status_badge = (
                f'<span style="display:inline-block;padding:4px 12px;border-radius:3px;'
                f'font-size:12px;font-weight:bold;color:white;background-color:{status_color};">'
                f'{approval_status}</span>'
            )
        badge_separator = " &nbsp; " if stage_badge and status_badge else ""

        # Timeline (for update #2+)
        MAX_TIMELINE_ROWS = 8
        timeline_html = ""
        if update_number > 1 and deal_entry:
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

                action = u.get('action_taken', u.get('L1_headline', ''))
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
            f'&#9679; {header_label}</div>'
            f'<div style="font-size:13px;color:#333;margin-bottom:6px;">{case_id_line}</div>'
            f'<div style="margin-bottom:6px;">{stage_badge}{badge_separator}{status_badge}</div>'
            f'<div style="font-size:12px;color:#666;">{tracking_line}</div>'
            f'{timeline_html}'
            f'</div>'
        )

    def _update_deal_state(self, deal_key: str, meta: Dict, summary: Dict,
                           email_data: Dict, backfill_entries: List[Dict] = None) -> int:
        """Upsert deal entry after successful summarization.

        Returns the update_number for this update.
        """
        # Use email Date header for the timestamp (not processing time)
        raw_date = email_data.get('date', '')
        try:
            email_dt = parsedate_to_datetime(raw_date)
            ts = email_dt.isoformat(timespec='seconds')
        except Exception:
            ts = datetime.now().isoformat(timespec='seconds')
        d = summary.get('L3_detailed', {})

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
        if d.get('approval_status'):
            entry['current_status'] = d['approval_status']
        if d.get('review_stage'):
            entry['current_review_stage'] = d['review_stage']

        # Append update record
        entry['updates'].append({
            'email_id': email_data.get('email_id', ''),
            'timestamp': ts,
            'email_subject': email_data.get('subject', ''),
            'L1_headline': summary.get('L1_headline', ''),
            'action_taken': d.get('action_taken', ''),
            'review_stage': d.get('review_stage', ''),
            'approval_status': d.get('approval_status', ''),
            'significance': d.get('significance', 'medium'),
        })

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

    def _fetch_url_text(self, url: str) -> str:
        """Fetch and extract text from a URL.

        Falls back to Jina Reader for 403/503 responses (bot-protected gov sites).
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
            if len(words) > 10000:
                text = " ".join(words[:10000])
            return text

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 503):
                logger.info(
                    f"   Got {e.response.status_code} for {url} — trying Jina Reader...")
                try:
                    jina_url = f"https://r.jina.ai/{url}"
                    resp = requests.get(jina_url, headers=headers, timeout=60)
                    resp.raise_for_status()
                    return resp.text
                except Exception as jina_e:
                    logger.warning(
                        f"   Jina Reader also failed for {url}: {jina_e}")
                    return ""
            else:
                logger.warning(f"   Failed to fetch {url}: {e}")
                return ""
        except Exception as e:
            logger.warning(f"   Failed to fetch {url}: {e}")
            return ""

    def build_combined_text(self, email_content: Dict) -> str:
        """Build combined document from email body + linked documents.

        Returns text in format:
        === EMAIL BODY ===
        [email text]
        === LINKED DOCUMENT: [url] ===
        [fetched text]
        """
        parts = []

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
                    parts.append(
                        f"=== LINKED DOCUMENT: {url} ===\n\n" + linked_text)

        combined = "\n\n".join(parts)

        # Truncate to stay within token budget
        words = combined.split()
        if len(words) > 15000:
            combined = " ".join(words[:15000])
            logger.info(f"   Truncated combined text to 15000 words")

        logger.info(f"   Total combined text: {len(combined.split())} words")
        return combined

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
            max_tokens=4096,
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

    def format_summary_email(self, summary: Dict, urls: List[str],
                             update_number: int = 1, deal_entry: Optional[Dict] = None) -> str:
        """Format summary as HTML email with green theme."""
        jurisdiction = summary.get('jurisdiction', 'INTL')
        authority = summary.get('regulatory_body', '')
        date = summary.get('document_date', '')
        doc_type = summary.get('document_type', '')
        process_num = summary.get('process_number', '')
        parties = summary.get('parties', {})
        d = summary.get('L3_detailed', {})

        # Build filing links
        links_html = ""
        for url in urls:
            domain = re.search(r'https?://([^/]+)', url)
            domain_label = domain.group(1) if domain else url
            links_html += f'<p><a href="{url}" target="_blank">View on {domain_label}</a></p>'

        # Build detailed sections
        detail_sections = ""

        if d.get('action_taken'):
            detail_sections += f"""
                <div class="detail-label">Action Taken:</div>
                <p>{d['action_taken']}</p>
            """

        if d.get('review_stage'):
            detail_sections += f"""
                <div class="detail-label">Review Stage:</div>
                <p>{d['review_stage']}</p>
            """

        if d.get('approval_status'):
            detail_sections += f"""
                <div class="detail-label">Approval Status:</div>
                <p>{d['approval_status']}</p>
            """

        if d.get('relevant_markets'):
            detail_sections += f"""
                <div class="detail-label">Relevant Markets:</div>
                <ul>{''.join(f'<li>{m}</li>' for m in d['relevant_markets'])}</ul>
            """

        if d.get('information_requested'):
            detail_sections += f"""
                <div class="detail-label">Information Requested:</div>
                <ul>{''.join(f'<li>{i}</li>' for i in d['information_requested'])}</ul>
            """

        if d.get('conditions_or_remedies'):
            detail_sections += f"""
                <div class="detail-label">Conditions / Remedies:</div>
                <ul>{''.join(f'<li>{c}</li>' for c in d['conditions_or_remedies'])}</ul>
            """

        if d.get('competitive_concerns'):
            detail_sections += f"""
                <div class="detail-label">Competitive Concerns:</div>
                <ul>{''.join(f'<li>{c}</li>' for c in d['competitive_concerns'])}</ul>
            """

        if d.get('legal_basis'):
            detail_sections += f"""
                <div class="detail-label">Legal Basis:</div>
                <p>{d['legal_basis']}</p>
            """

        timeline = d.get('timeline', {})
        if any(v for v in timeline.values()):
            timeline_items = ''.join(
                f'<li><strong>{k.replace("_", " ").title()}:</strong> {v}</li>'
                for k, v in timeline.items() if v
            )
            detail_sections += f"""
                <div class="detail-label">Timeline:</div>
                <ul>{timeline_items}</ul>
            """

        if d.get('deal_implications_for_us_investors'):
            detail_sections += f"""
                <div class="detail-label">Deal Implications for US Investors:</div>
                <p>{d['deal_implications_for_us_investors']}</p>
            """

        if d.get('related_proceedings'):
            detail_sections += f"""
                <div class="detail-label">Related Proceedings:</div>
                <ul>{''.join(f'<li>{r}</li>' for r in d['related_proceedings'])}</ul>
            """

        if d.get('risks_flagged'):
            detail_sections += f"""
                <div class="detail-label">Risks Flagged:</div>
                <ul>{''.join(f'<li>{r}</li>' for r in d['risks_flagged'])}</ul>
            """

        # Build case status section (appears in every email)
        case_status_html = self._build_case_status_html(
            update_number, deal_entry, summary)

        html = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
                .header {{ background-color: #006644; color: white; padding: 20px; border-radius: 5px; }}
                .header h2 {{ margin: 0 0 5px 0; }}
                .header p {{ margin: 3px 0; opacity: 0.9; }}
                .section {{ margin: 20px 0; padding: 15px; background-color: #f5f5f5; border-radius: 5px; }}
                .headline {{ font-size: 18px; font-weight: bold; color: #006644; margin: 10px 0; }}
                .brief {{ font-size: 14px; line-height: 1.6; margin: 10px 0; }}
                .detail-label {{ font-weight: bold; color: #555; margin-top: 10px; }}
                ul {{ margin: 5px 0; padding-left: 20px; }}
                .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 12px; color: #999; }}
                a {{ color: #4a90e2; text-decoration: none; }}
                .meta {{ font-size: 13px; margin: 5px 0; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h2>{jurisdiction} — {authority}</h2>
                <p>{doc_type}</p>
                <p>Process: {process_num} | Date: {date}</p>
                <p>Target: {parties.get('target', 'N/A')} | Acquirer: {parties.get('acquirer', 'N/A')}</p>
            </div>

            {case_status_html}

            <div class="section">
                <h3>L1 — Headline</h3>
                <p class="headline">{summary.get('L1_headline', '')}</p>
            </div>

            <div class="section">
                <h3>L2 — Brief</h3>
                <p class="brief">{summary.get('L2_brief', '')}</p>
            </div>

            <div class="section">
                <h3>L3 — Detailed</h3>
                {detail_sections}
            </div>

            <div class="footer">
                {links_html}
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
            headline = summary.get('L1_headline', '')
            jurisdiction = summary.get('jurisdiction', '')
            body = summary.get('regulatory_body', '')
            target = summary.get('parties', {}).get('target', '')
            acquirer = summary.get('parties', {}).get('acquirer', '')

            # Find the last emailed update for context
            last_emailed = ''
            if deal_entry and deal_entry.get('updates'):
                for u in reversed(deal_entry['updates']):
                    if u.get('significance') not in ('low', 'routine') and u.get('L1_headline'):
                        last_emailed = u['L1_headline']
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
        l1 = summary.get('L1_headline', '')
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
            combined_text = self.build_combined_text(content)

            if not combined_text.strip():
                logger.warning(f"   No text content found in email: {subject}")
                return

            # 4. Prepend deal history context to combined text
            if history_context:
                combined_text = history_context + "\n\n" + combined_text

            # 5. Generate summary (two-pass: Haiku extraction + Opus summarization)
            summary = self.summarize(combined_text)

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
            significance = summary.get('L3_detailed', {}).get(
                'significance', 'medium')
            sig_reason = summary.get('L3_detailed', {}).get(
                'significance_reasoning', '')

            # Hard override: always email for approvals/blocks
            approval = summary.get('L3_detailed', {}).get(
                'approval_status', '')
            if any(kw in approval for kw in ('Approved', 'Blocked', 'Conditions')):
                significance = 'critical'

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
        logger.info(
            f"Matching: [FRMD] tags and agency regulatory updates (subject-only)")
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

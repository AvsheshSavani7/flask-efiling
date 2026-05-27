import os
import sys
import json
import time
import logging
import re
import base64
import datetime
from datetime import timezone, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from scraper_error_utils import collect_error, send_error_summary
from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from cade_cases_register import match_case_to_deal
from html import escape as escape_html
from log_utils import cleanup_old_logs, refresh_log_file

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv(".env")
ENV_PATH = ".env"

BASE_PESQUISA_URL = "https://sei.cade.gov.br/sei/modulos/pesquisa/"
RECAPTCHA_SITE_KEY = "6Le2a7gqAAAAAAVxMYQ-mn7GyO8lcWAQq4Hxm-2G"

CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY")

BASE_URL = os.getenv("BASE_URL")
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_INTERNAL_WITH_JOSH",
    f"{BASE_URL}/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------------------------
# Logging — production setup (RotatingFileHandler, IST, env-based settings)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "brazil_cases_update_monitor"
IST = timezone(timedelta(hours=5, minutes=30))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


logger = logging.getLogger(SCRIPT_NAME)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

if not logger.handlers:
    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")

    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

logger.propagate = False

cleanup_old_logs(os.path.dirname(LOG_FILE), LOG_RETENTION_DAYS)


def utc_now_iso() -> str:
    return datetime.datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_brazil_cases_collection():
    db = get_database()
    if db is None:
        return None
    return db["brazil_cases"]


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate_to_english(text: str) -> str:
    if not text or not isinstance(text, str) or text.strip() == "":
        return text
    if len(text) > 500:
        return text
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "pt",
                  "tl": "en", "dt": "t", "q": text}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            return response.json()[0][0][0]
    except requests.Timeout:
        return text
    except Exception:
        return text
    return text


# ---------------------------------------------------------------------------
# CAPTCHA solving (copied from cade_cases_register — standalone)
# ---------------------------------------------------------------------------

def solve_recaptcha_v2(site_key: str, page_url: str, api_key: str = None) -> Optional[str]:
    if not api_key:
        return None
    print("🔐 Solving reCAPTCHA with 2captcha...")
    try:
        resp = requests.post(CAPTCHA_SOLVER_URL, data={
            "key": api_key, "method": "userrecaptcha",
            "googlekey": site_key, "pageurl": page_url, "json": 1,
        })
        result = resp.json()
        if result.get("status") != 1:
            print(f"❌ 2Captcha submission failed: {result.get('request')}")
            return None
        task_id = result.get("request")
        if not task_id:
            return None
        for attempt in range(30):
            time.sleep(5)
            r = requests.get(CAPTCHA_RESULT_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            })
            res = r.json()
            if res.get("status") == 1:
                token = res.get("request")
                print(f"✅ reCAPTCHA solved! Token: {token[:20]}...")
                return token
            elif res.get("request") != "CAPCHA_NOT_READY":
                break
            if attempt % 3 == 0:
                print(f"⏳ Waiting for captcha... (attempt {attempt + 1}/30)")
        return None
    except Exception as e:
        print(f"❌ Error solving captcha: {e}")
        return None


def fill_recaptcha_token(page, token: str) -> bool:
    if not token:
        return False
    try:
        time.sleep(1)
        page.evaluate("""
            (token) => {
                var ta = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                         document.querySelector('#g-recaptcha-response');
                if (ta) {
                    ta.value = token; ta.innerHTML = token;
                    ta.dispatchEvent(new Event('input', {bubbles:true}));
                    ta.dispatchEvent(new Event('change', {bubbles:true}));
                }
                var v = document.getElementById('verificaRecaptcha');
                if (v) v.value = 'true';
                if (typeof recaptchaCallback === 'function') { try { recaptchaCallback(); } catch(e){} }
            }
        """, token)
        time.sleep(1)
        ok = page.evaluate("""
            () => {
                var ta = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                         document.querySelector('#g-recaptcha-response');
                return ta ? ta.value.length > 0 : false;
            }
        """)
        return bool(ok)
    except Exception:
        return False


def check_and_solve_recaptcha(page) -> bool:
    try:
        loc = page.locator("#g-recaptcha")
        if loc.count() > 0 and loc.is_visible():
            token = solve_recaptcha_v2(
                RECAPTCHA_SITE_KEY, page.url, CAPTCHA_API_KEY)
            if token:
                for _ in range(3):
                    if fill_recaptcha_token(page, token):
                        return True
                    time.sleep(2)
            return False
    except Exception:
        pass
    return True


def solve_image_captcha(page, api_key: str = None) -> Optional[str]:
    if not api_key:
        return None
    try:
        loc = page.locator(
            "img[src*='captcha' i], img[alt*='captcha' i], img[id*='captcha' i]")
        img_src = None
        if loc.count() > 0:
            img_src = loc.first.get_attribute("src")
        if not img_src:
            for img in page.locator("img").all():
                try:
                    box = img.bounding_box()
                    if box and 50 < box["width"] < 300 and 30 < box["height"] < 150:
                        src = img.get_attribute("src") or ""
                        if "data:image" in src or "captcha" in src.lower():
                            img_src = src
                            break
                except Exception:
                    continue
        if not img_src:
            return None
        if img_src.startswith("data:image"):
            img_base64 = img_src.split(",")[1] if "," in img_src else None
            if not img_base64:
                return None
        else:
            if not img_src.startswith("http"):
                img_src = urljoin(page.url, img_src)
            r = requests.get(img_src, timeout=10)
            if r.status_code != 200:
                return None
            img_base64 = base64.b64encode(r.content).decode("utf-8")
        print("🔐 Solving image CAPTCHA...")
        resp = requests.post(CAPTCHA_SOLVER_URL, data={
            "key": api_key, "method": "base64", "body": img_base64, "json": 1,
        })
        result = resp.json()
        if result.get("status") != 1:
            return None
        task_id = result.get("request")
        for attempt in range(30):
            time.sleep(5)
            r2 = requests.get(CAPTCHA_RESULT_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            })
            res = r2.json()
            if res.get("status") == 1:
                print(f"✅ Image CAPTCHA solved: {res.get('request')}")
                return res.get("request")
            elif res.get("request") != "CAPCHA_NOT_READY":
                break
            if attempt % 3 == 0:
                print(
                    f"⏳ Waiting for image CAPTCHA... (attempt {attempt + 1}/30)")
        return None
    except Exception as e:
        print(f"❌ Error solving image CAPTCHA: {e}")
        return None


def handle_image_captcha_if_present(page) -> bool:
    try:
        captcha_img = None
        try:
            loc = page.locator("img[src*='captcha' i], img[alt*='captcha' i]")
            if loc.count() > 0:
                captcha_img = loc.first
        except Exception:
            pass
        if not captcha_img:
            for img in page.locator("img").all():
                try:
                    box = img.bounding_box()
                    if box and 50 < box["width"] < 300:
                        src = img.get_attribute("src") or ""
                        if "captcha" in src.lower() or "data:image" in src:
                            captcha_img = img
                            break
                except Exception:
                    continue
        if not captcha_img:
            return False
        print("🖼️ Image CAPTCHA detected, solving...")
        captcha_input = None
        for inp in page.locator("input[type='text']").all():
            try:
                box = inp.bounding_box()
                if box and box["width"] < 200:
                    captcha_input = inp
                    break
            except Exception:
                continue
        submit_button = None
        try:
            loc = page.locator(
                "button:has-text('Enviar'), input[type='submit'][value*='Enviar' i]")
            if loc.count() > 0:
                submit_button = loc.first
        except Exception:
            pass
        if not captcha_input:
            return False
        solution = solve_image_captcha(page, CAPTCHA_API_KEY)
        if solution:
            captcha_input.fill(solution)
            time.sleep(1)
            if submit_button:
                submit_button.click()
            else:
                captcha_input.press("Enter")
            time.sleep(3)
            print("✅ Image CAPTCHA solved and submitted")
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Detail page extraction (standalone copies)
# ---------------------------------------------------------------------------

def extract_autuacao_info(
    page,
    context,
    url: str,
    max_retries: int = 2,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Open detail page and extract Autuação info. Retries on timeout."""
    empty_result = {"process": "", "type": "",
                    "registration_date": "", "interessados": ""}

    for attempt in range(1, max_retries + 1):
        logger.info(
            f"    Attempt {attempt}/{max_retries} — opening detail page for Autuação: {url}")
        detail_page = None
        try:
            detail_page = context.new_page()
            detail_page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(5)
            if handle_image_captcha_if_present(detail_page):
                time.sleep(4)
            time.sleep(2)

            html = detail_page.content()
            soup = BeautifulSoup(html, "html.parser")
            info = {"process": "", "type": "",
                    "registration_date": "", "interessados": ""}

            for table in soup.find_all("table"):
                header = table.find("th", string=re.compile(r"Autuação", re.I))
                if not header:
                    continue
                for row in table.find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    if len(cells) == 2:
                        label = cells[0].get_text(strip=True)
                        value = cells[1].get_text(separator=" ", strip=True)
                        if "Processo" in label:
                            info["process"] = value
                        elif "Tipo" in label:
                            info["type"] = value
                        elif "Data de Registro" in label:
                            info["registration_date"] = value
                        elif "Interessados" in label:
                            info["interessados"] = value
                break

            if not info["process"]:
                for tbody in soup.find_all("tbody"):
                    header = tbody.find(
                        "th", string=re.compile(r"Autuação", re.I))
                    if not header:
                        continue
                    for row in tbody.find_all("tr"):
                        cells = row.find_all(["td", "th"])
                        if len(cells) == 2:
                            label = cells[0].get_text(strip=True)
                            value = cells[1].get_text(
                                separator=" ", strip=True)
                            if "Processo" in label:
                                info["process"] = value
                            elif "Tipo" in label:
                                info["type"] = value
                            elif "Data de Registro" in label:
                                info["registration_date"] = value
                            elif "Interessados" in label:
                                info["interessados"] = value
                    break

            detail_page.close()
            return info
        except Exception as e:
            logger.warning(
                f"    Attempt {attempt}/{max_retries} failed for Autuação {url}: {e}")
            if detail_page:
                if attempt < max_retries:
                    try:
                        detail_page.close()
                    except Exception:
                        pass
                    logger.info(f"    Retrying in 10s...")
                    time.sleep(10)
                    continue
                screenshot_path = None
                try:
                    log_dir = os.path.dirname(LOG_FILE)
                    screenshot_path = os.path.join(
                        log_dir,
                        f"debug_screenshot_{datetime.datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.png",
                    )
                    detail_page.screenshot(path=screenshot_path)
                    logger.error(
                        f"    Debug screenshot saved to {screenshot_path}")
                except Exception:
                    pass
                try:
                    detail_page.close()
                except Exception:
                    pass
            explanation = (
                f"Failed to extract Autuação after {max_retries} attempts. "
                f"URL: {url}. Last error: {e}. "
                f"The CADE SEI portal may be temporarily unavailable, slow to render, "
                f"or its page structure may have changed."
            )
            if error_items is not None:
                collect_error(
                    error_items,
                    explanation,
                    step="extract_autuacao_info",
                    context={
                        "url": url,
                        "attempts": max_retries,
                        "traceback": str(e),
                        "screenshot": screenshot_path or "capture failed",
                    },
                )
            return empty_result


def extract_tables(
    page,
    context,
    url: str,
    max_retries: int = 2,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract tblDocumentos + tblHistorico from detail page in one visit. Retries on timeout."""

    for attempt in range(1, max_retries + 1):
        logger.info(
            f"    Attempt {attempt}/{max_retries} — opening detail page for tables: {url}")
        detail_page = None
        try:
            detail_page = context.new_page()
            detail_page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(5)
            if handle_image_captcha_if_present(detail_page):
                time.sleep(4)
            time.sleep(2)

            html = detail_page.content()
            soup = BeautifulSoup(html, "html.parser")

            # --- tblDocumentos ---
            table = soup.find("table", id="tblDocumentos")
            table_data: List[Dict[str, Any]] = []
            if table:
                for row in table.find_all("tr"):
                    if row.find("th"):
                        continue
                    cells = row.find_all("td")
                    if len(cells) < 5:
                        continue
                    row_data: Dict[str, Any] = {}
                    doc_link = row.find("a", class_="ancoraPadraoAzul")
                    if doc_link:
                        doc_number = doc_link.get_text(strip=True)
                        if doc_number and re.match(r"^\d+$", doc_number):
                            row_data["documento_processo"] = doc_number
                        onclick = doc_link.get("onclick", "")
                        if onclick and "window.open" in onclick:
                            match = re.search(
                                r"window\.open\('([^']+)'\)", onclick)
                            if match:
                                doc_url = match.group(1)
                                if not doc_url.startswith("http"):
                                    doc_url = requests.compat.urljoin(
                                        BASE_PESQUISA_URL, doc_url)
                                row_data["document_url"] = doc_url
                        doc_type = doc_link.get(
                            "alt") or doc_link.get("title") or ""
                        if doc_type.strip():
                            row_data["tipo_documento"] = doc_type.strip()
                    dates_found = []
                    for cell in cells:
                        ct = cell.get_text(strip=True)
                        if re.match(r"^\d{2}/\d{2}/\d{4}$", ct):
                            dates_found.append(ct)
                    if len(dates_found) >= 1:
                        row_data["data_documento"] = dates_found[0]
                    if len(dates_found) >= 2:
                        row_data["data_registro"] = dates_found[1]
                    unidade_link = row.find("a", class_="ancoraSigla")
                    if unidade_link:
                        u = unidade_link.get_text(strip=True)
                        if u:
                            row_data["unidade"] = u
                    row_data = {k: v for k, v in row_data.items()
                                if v and str(v).strip()}
                    if row_data.get("documento_processo"):
                        table_data.append(row_data)

            # --- tblHistorico ---
            hist_table = soup.find("table", id="tblHistorico")
            hist_data: List[Dict[str, Any]] = []
            if hist_table:
                for row in hist_table.find_all("tr"):
                    if row.find("th"):
                        continue
                    cells = row.find_all("td")
                    if len(cells) < 3:
                        continue
                    date_time = cells[0].get_text(strip=True)
                    unit_link = cells[1].find("a", class_="ancoraSigla")
                    unit = unit_link.get_text(
                        strip=True) if unit_link else cells[1].get_text(strip=True)
                    description = cells[2].get_text(strip=True)
                    if not description:
                        continue
                    hist_data.append({
                        "date_time": date_time,
                        "unit": unit,
                        "description": description,
                    })

            detail_page.close()
            return table_data, hist_data
        except Exception as e:
            logger.warning(
                f"    Attempt {attempt}/{max_retries} failed for tables {url}: {e}")
            if detail_page:
                if attempt < max_retries:
                    try:
                        detail_page.close()
                    except Exception:
                        pass
                    logger.info(f"    Retrying in 10s...")
                    time.sleep(10)
                    continue
                screenshot_path = None
                try:
                    log_dir = os.path.dirname(LOG_FILE)
                    screenshot_path = os.path.join(
                        log_dir,
                        f"debug_screenshot_{datetime.datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.png",
                    )
                    detail_page.screenshot(path=screenshot_path)
                    logger.error(
                        f"    Debug screenshot saved to {screenshot_path}")
                except Exception:
                    pass
                try:
                    detail_page.close()
                except Exception:
                    pass
            explanation = (
                f"Failed to extract tables after {max_retries} attempts. "
                f"URL: {url}. Last error: {e}. "
                f"The CADE SEI portal may be temporarily unavailable, slow to render, "
                f"or its page structure may have changed."
            )
            if error_items is not None:
                collect_error(
                    error_items,
                    explanation,
                    step="extract_tables",
                    context={
                        "url": url,
                        "attempts": max_retries,
                        "traceback": str(e),
                        "screenshot": screenshot_path or "capture failed",
                    },
                )
            return [], []


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def live_detail_scrape_looks_incomplete(
    stored: Dict[str, Any],
    live_type: str,
    live_interessados: str,
    live_table_records: List[Dict[str, Any]],
    live_historico_records: List[Dict[str, Any]],
) -> bool:
    """
    Heuristic: live HTML is probably a failed or partially rendered fetch.

    When both SEI tables are empty but we expect real content, treating the
    snapshot as authoritative causes false interessados removals and DB churn.
    """
    n_tab, n_hist = len(live_table_records), len(live_historico_records)
    if n_tab > 0 or n_hist > 0:
        return False

    stored_tab = stored.get("table_records") or []
    stored_hist = stored.get("historico_records") or []
    had_tabular = len(stored_tab) > 0 or len(stored_hist) > 0

    stored_type = (stored.get("type") or "").strip()
    stored_inter = (stored.get("interessados") or "").strip()
    live_ty = (live_type or "").strip()
    live_inter = (live_interessados or "").strip()

    if had_tabular:
        return True
    if stored_inter and not live_inter:
        return True
    if (stored_type or stored_inter) and not live_ty and not live_inter:
        return True
    return False


def detect_changes(
    stored: Dict[str, Any],
    live_type: str,
    live_interessados: str,
    live_table_records: List[Dict[str, Any]],
    live_historico_records: List[Dict[str, Any]],
) -> List[Tuple[str, Any, Any, str]]:
    """
    Compare stored record with live-scraped data.

    Returns list of (field, old_value, new_value, change_type).
    change_type: "updated" for scalar changes, "new_items" for new list entries.
    """
    changes: List[Tuple[str, Any, Any, str]] = []

    # 1) type
    old_type = (stored.get("type") or "").strip()
    new_type = (live_type or "").strip()
    if old_type and new_type and old_type != new_type:
        changes.append(("type", old_type, new_type, "updated"))

    # 2) interessados — only compare when stored value is empty (waiting to fill)
    old_inter = (stored.get("interessados") or "").strip()
    new_inter = (live_interessados or "").strip()
    if not old_inter and new_inter:
        changes.append(("interessados", old_inter, new_inter, "new"))

    # 3) table_records — keyed by documento_processo
    existing_doc_ids = set()
    for rec in (stored.get("table_records") or []):
        doc_id = rec.get("documento_processo") or rec.get(
            "document_process", "")
        if doc_id:
            existing_doc_ids.add(str(doc_id))

    new_table_items = []
    for rec in live_table_records:
        doc_id = rec.get("documento_processo", "")
        if doc_id and str(doc_id) not in existing_doc_ids:
            new_table_items.append(rec)

    if new_table_items:
        changes.append(("table_records", None, new_table_items, "new_items"))

    # 4) historico_records — keyed by date_time + description
    existing_hist_ids = set()
    for rec in (stored.get("historico_records") or []):
        key = f"{rec.get('date_time', '')}|{rec.get('description', '')}"
        existing_hist_ids.add(key)

    new_hist_items = []
    for rec in live_historico_records:
        key = f"{rec.get('date_time', '')}|{rec.get('description', '')}"
        if key not in existing_hist_ids:
            new_hist_items.append(rec)

    if new_hist_items:
        changes.append(("historico_records", None,
                       new_hist_items, "new_items"))

    return changes


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------

def update_case_in_db(
    collection,
    case_doc: Dict[str, Any],
    changes: List[Tuple[str, Any, Any, str]],
    live_table_records: List[Dict[str, Any]],
    live_historico_records: List[Dict[str, Any]],
    close_case: bool = False,
    new_deal_id: Optional[str] = None,
) -> bool:
    """Apply detected changes to the stored record."""
    try:
        case_id = case_doc.get("_id")
        if not case_id:
            print("    ⚠️ No _id on case document; cannot update")
            return False

        update_fields: Dict[str, Any] = {"updated_at": utc_now_iso()}

        if close_case:
            update_fields["is_open"] = False

        if new_deal_id:
            update_fields["deal_id"] = new_deal_id

        for field, _old, new_val, change_type in changes:
            if field == "type":
                update_fields["type"] = new_val
                update_fields["type_en"] = translate_to_english(new_val)

            elif field == "interessados":
                update_fields["interessados"] = new_val
                update_fields["interessados_en"] = translate_to_english(
                    new_val)

            elif field == "table_records":
                # Merge: existing + new, translate new tipo_documento
                existing = list(case_doc.get("table_records") or [])
                for rec in new_val:
                    tr = rec.copy()
                    if "tipo_documento" in tr:
                        tr["document_type"] = translate_to_english(
                            tr["tipo_documento"])
                        tr.pop("tipo_documento", None)
                    existing.append(tr)
                update_fields["table_records"] = existing

            elif field == "historico_records":
                # Merge: existing + new, translate descriptions
                existing = list(case_doc.get("historico_records") or [])
                for rec in new_val:
                    tr = rec.copy()
                    tr["description_en"] = translate_to_english(
                        tr.get("description", ""))
                    existing.append(tr)
                update_fields["historico_records"] = existing

        result = collection.update_one(
            {"_id": case_id}, {"$set": update_fields})
        if result.modified_count > 0:
            logger.info(f"    Updated case in brazil_cases")
        else:
            logger.info(f"    No DB changes (document may be identical)")
        return True
    except Exception as e:
        logger.exception(f"Error updating case: {e}")
        return False


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _post_email_payload(payload: Dict[str, Any]) -> bool:
    logger.info(f"    Sending email: {payload.get('subject', 'N/A')}")
    try:
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"    Email sent successfully (status={resp.status_code})")
        return True
    except Exception as e:
        logger.exception(f"Error sending email via webhook: {e}")
        return False


def generate_update_email_html(
    case_data: Dict[str, Any],
    changes: List[Tuple[str, Any, Any, str]],
    deal: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """Generate subject + HTML email for an update notification."""
    process = case_data.get("process", "N/A")
    case_type = case_data.get("type_en") or case_data.get("type", "N/A")
    reg_date = case_data.get("registration_date", "N/A")
    interessados = case_data.get(
        "interessados_en") or case_data.get("interessados", "N/A")
    detail_url = case_data.get("detail_url", "")

    # Build change summary lines
    change_lines: List[str] = []
    new_table_items: List[Dict[str, Any]] = []
    new_hist_items: List[Dict[str, Any]] = []

    for field, old_val, new_val, change_type in changes:
        if field == "type":
            change_lines.append(f"Type changed: {old_val} → {new_val}")
        elif field == "interessados":
            old_display = old_val if old_val else "(empty)"
            new_display = new_val if new_val else "(empty)"

            if change_type == "removed":
                change_lines.append(
                    f"Interested parties removed: {old_display} → {new_display}"
                )
            elif change_type == "new":
                change_lines.append(
                    f"Interested parties added: {old_display} → {new_display}"
                )
            else:
                change_lines.append(
                    f"Interested parties changed: {old_display} → {new_display}"
                )
        elif field == "table_records":
            new_table_items = new_val or []
            change_lines.append(
                f"{len(new_table_items)} new document record(s)")
        elif field == "historico_records":
            new_hist_items = new_val or []
            change_lines.append(f"{len(new_hist_items)} new history record(s)")

    change_summary_html = "".join(
        f"<li>{escape_html(l)}</li>" for l in change_lines)

    # Deal info
    if deal:
        target = deal.get("target") or deal.get("target_name", "N/A")
        acquirer = deal.get("acquirer") or deal.get("acquire_name", "N/A")
        deal_id = str(deal.get("_id", "N/A"))
        prefix = "[FRMD]"
        deal_banner = f"""
<div style="background:#dbeafe;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #2563eb;">
  <div style="font-weight:800;color:#1e40af;margin-bottom:4px;">Matched Deal</div>
  <div style="font-size:14px;color:#1e3a8a;"><b>Acquirer:</b> {escape_html(acquirer)} | <b>Target:</b> {escape_html(target)} | <b>Deal ID:</b> {escape_html(deal_id)}</div>
</div>"""
    else:
        prefix = "[FRUD]"
        deal_banner = """
<div style="background:#fef3c7;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #f59e0b;">
  <div style="font-weight:800;color:#92400e;">USA-Related (Unmatched)</div>
</div>"""

    subject = f"{prefix} CADE Brazil (Updated) – {process}"

    # New document records table
    doc_table_html = ""
    if new_table_items:
        rows = ""
        for idx, rec in enumerate(new_table_items):
            bg = "#fffacd" if idx % 2 == 0 else "#fff9b3"
            dp = escape_html(str(rec.get("documento_processo", "")))
            dt = escape_html(
                str(rec.get("document_type", rec.get("tipo_documento", ""))))
            dd = escape_html(str(rec.get("data_documento", "")))
            dr = escape_html(str(rec.get("data_registro", "")))
            un = escape_html(str(rec.get("unidade", "")))
            du = rec.get("document_url", "")
            dp_h = f'<a href="{escape_html(du)}" style="color:#4a90e2;">{dp}</a>' if du else dp
            rows += f'<tr style="background:{bg};"><td style="padding:6px;border:1px solid #ddd;">{dp_h}</td><td style="padding:6px;border:1px solid #ddd;">{dt}</td><td style="padding:6px;border:1px solid #ddd;">{dd}</td><td style="padding:6px;border:1px solid #ddd;">{dr}</td><td style="padding:6px;border:1px solid #ddd;">{un}</td></tr>'
        doc_table_html = f"""
<h3 style="margin-top:18px;">New Document Records ({len(new_table_items)})</h3>
<table style="width:100%;border-collapse:collapse;"><thead><tr style="background:#f5f5f5;"><th style="padding:6px;border:1px solid #ddd;text-align:left;">Doc Process</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Type</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Doc Date</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Reg Date</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Unit</th></tr></thead><tbody>{rows}</tbody></table>"""

    # New history records table
    hist_table_html = ""
    if new_hist_items:
        rows = ""
        for idx, rec in enumerate(new_hist_items):
            bg = "#e0f2fe" if idx % 2 == 0 else "#dbeafe"
            dt_val = escape_html(str(rec.get("date_time", "")))
            un_val = escape_html(str(rec.get("unit", "")))
            desc = escape_html(str(rec.get("description", "")))
            desc_en = escape_html(
                str(rec.get("description_en", rec.get("description", ""))))
            rows += f'<tr style="background:{bg};"><td style="padding:6px;border:1px solid #ddd;">{dt_val}</td><td style="padding:6px;border:1px solid #ddd;">{un_val}</td><td style="padding:6px;border:1px solid #ddd;">{desc_en}</td></tr>'
        hist_table_html = f"""
<h3 style="margin-top:18px;">New History Records ({len(new_hist_items)})</h3>
<table style="width:100%;border-collapse:collapse;"><thead><tr style="background:#f5f5f5;"><th style="padding:6px;border:1px solid #ddd;text-align:left;">Date/Time</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Unit</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Description</th></tr></thead><tbody>{rows}</tbody></table>"""

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:900px;margin:0 auto;">
  <div style="background:#fef2f2;border-radius:6px;padding:14px 20px;margin-bottom:18px;border-left:4px solid #ef4444;">
    <div style="font-weight:800;color:#dc2626;margin-bottom:6px;">CADE Brazil – Case Updated</div>
    <ul style="margin:0;padding-left:20px;color:#991b1b;font-size:14px;">{change_summary_html}</ul>
  </div>
  {deal_banner}
  <div style="margin-bottom:14px;">
    <div><b>Process:</b> {escape_html(process)}</div>
    <div><b>Type:</b> {escape_html(case_type)}</div>
    <div><b>Registration Date:</b> {escape_html(reg_date)}</div>
    <div><b>Interested Parties:</b> {escape_html(interessados)}</div>
  </div>
  {'<div style="margin-bottom:14px;"><a href="'+escape_html(detail_url)+'" target="_blank">View CADE Detail Page →</a></div>' if detail_url else ''}
  {doc_table_html}
  {hist_table_html}
</div>""".strip()

    return subject, html


def send_update_email(
    case_data: Dict[str, Any],
    changes: List[Tuple[str, Any, Any, str]],
    deal: Optional[Dict[str, Any]],
) -> bool:
    subject, html = generate_update_email_html(case_data, changes, deal)
    print(f"    📤 Sending email: {subject}")
    return _post_email_payload({
        "subject": subject,
        "html": html,
        "process": case_data.get("process", "N/A"),
        "detail_url": case_data.get("detail_url", ""),
        "deal_id": str(deal.get("_id", "")) if deal else None,
        "update_type": "brazil_case_update",
        "changed_fields": [c[0] for c in changes],
    })


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def process_brazil_cases_updates(headless: bool = True):
    """
    Monitor all records in ``brazil_cases`` for updates.

    Change-detection fields: type, interessados, table_records, historico_records.
    """
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []

    logger.info("=" * 60)
    logger.info("Starting CADE Brazil Cases Update Monitor")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    total_checked = 0
    total_changed = 0

    try:
        logger.info("[STEP 1] Initializing MongoDB connection...")
        ok, msg = init_mongodb_connection(ENV_PATH)
        if not ok:
            collect_error(
                error_items,
                f"MongoDB connection failed: {msg}",
                step="mongodb_connect",
            )
            return
        logger.info(f"[STEP 1.1] MongoDB: {msg}")

        if not is_connected():
            collect_error(
                error_items,
                "MongoDB not connected after init",
                step="mongodb_connect",
            )
            return

        cases_collection = get_brazil_cases_collection()
        if cases_collection is None:
            collect_error(
                error_items,
                "Could not access 'brazil_cases' collection",
                step="get_collection",
            )
            return

        logger.info("[STEP 1.4] brazil_cases collection ready")

        deals_collection = get_deals_collection()
        deals_status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }

        cases = list(cases_collection.find({"is_open": True}))
        if not cases:
            logger.info("[STEP 1.5] No open records in brazil_cases. Exiting.")
            return

        logger.info(f"[STEP 1.6] Found {len(cases)} open records in brazil_cases")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            context.on("dialog", lambda dialog: dialog.accept())
            page = context.new_page()

            try:
                for idx, case_doc in enumerate(cases, 1):
                    try:
                        logger.info(
                            f"[STEP 2] Checking case: {case_doc.get('process', 'N/A')}")
                        logger.info(
                            f"[STEP 2.1] Detail URL: {case_doc.get('detail_url', 'N/A')}")
                        logger.info(f"[STEP 2.2]  type: {case_doc.get('type', 'N/A')}")
                        logger.info(
                            f"[STEP 2.3]  interessados: {case_doc.get('interessados', 'N/A')}")

                        total_checked += 1
                        process_num = case_doc.get("process", "N/A")
                        detail_url = case_doc.get("detail_url")

                        logger.info(
                            f"[STEP 2.4] [{idx}/{len(cases)}] Process {process_num}")

                        if not detail_url:
                            logger.warning("[STEP 2.5] No detail_url; skipping")
                            continue

                        autuacao = extract_autuacao_info(
                            page, context, detail_url, error_items=error_items)
                        live_type = autuacao.get("type", "")
                        live_interessados = autuacao.get("interessados", "")

                        live_table, live_historico = extract_tables(
                            page, context, detail_url, error_items=error_items)
                        logger.info(f"[STEP 2.6] Live: type={live_type[:50]}..., "
                                    f"table_records={len(live_table)}, historico={len(live_historico)}")

                        if live_detail_scrape_looks_incomplete(
                            case_doc,
                            live_type,
                            live_interessados,
                            live_table,
                            live_historico,
                        ):
                            logger.warning(
                                "[STEP 2.6b] Live scrape looks incomplete (empty tables vs stored "
                                "expectations); skipping updates and notifications — retry next run"
                            )
                            continue

                        should_close = any(
                            rec.get("tipo_documento", "").strip(
                            ) == "Certidão de Trânsito em Julgado"
                            for rec in live_table
                        )
                        if should_close:
                            logger.info(
                                "[STEP 2.7] 'Certidão de Trânsito em Julgado' found — will set is_open=False")

                        changes = detect_changes(
                            case_doc, live_type, live_interessados, live_table, live_historico,
                        )

                        if not changes and not should_close:
                            logger.info("[STEP 2.8] No changes detected")
                            continue

                        if not changes and should_close:
                            logger.info(
                                "[STEP 2.9] No field changes but closing case (is_open → False)")
                            if not update_case_in_db(
                                cases_collection, case_doc, changes,
                                live_table, live_historico,
                                close_case=True,
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to update case document",
                                    step="update_case",
                                    context={"process": process_num, "detail_url": detail_url},
                                )
                            continue

                        total_changed += 1
                        logger.info(f"[STEP 2.10] {len(changes)} change(s) detected:")
                        for field, old_val, new_val, ctype in changes:
                            if ctype == "new_items":
                                logger.info(
                                    f"[STEP 2.11]    {field}: {len(new_val)} new item(s)")
                            else:
                                logger.info(
                                    f"[STEP 2.12]    {field}: {old_val} → {new_val} ({ctype})")

                        deal = None
                        deal_id = case_doc.get("deal_id")

                        if deal_id and deals_collection is not None:
                            try:
                                deal = deals_collection.find_one(
                                    {"_id": ObjectId(deal_id), **deals_status_filter}
                                )
                            except Exception as e:
                                logger.exception(f"[STEP 2.13] Invalid deal_id: {e}")
                                collect_error(
                                    error_items,
                                    str(e),
                                    step="resolve_deal",
                                    context={"process": process_num},
                                )

                            if deal:
                                logger.info("[STEP 2.14] Deal linked — sending email")
                                if not send_update_email(case_doc, changes, deal):
                                    collect_error(
                                        error_items,
                                        "Failed to send update email",
                                        step="send_email",
                                        context={"process": process_num, "detail_url": detail_url},
                                    )
                                if not update_case_in_db(
                                    cases_collection, case_doc, changes,
                                    live_table, live_historico,
                                    close_case=should_close,
                                ):
                                    collect_error(
                                        error_items,
                                        "Failed to update case document",
                                        step="update_case",
                                        context={"process": process_num, "detail_url": detail_url},
                                    )
                                continue

                        interessados_text = case_doc.get(
                            "interessados") or live_interessados
                        translated_text = case_doc.get(
                            "interessados_en") or translate_to_english(interessados_text) if interessados_text else ""

                        matched_deal_id = None
                        if interessados_text:
                            try:
                                matched_deal_id = match_case_to_deal(
                                    interessados_text, translated_text)
                            except Exception as e:
                                logger.exception(
                                    f"[STEP 2.15] Error during deal matching: {e}")
                                collect_error(
                                    error_items,
                                    str(e),
                                    step="match_case_to_deal",
                                    context={"process": process_num, "detail_url": detail_url},
                                )

                        if matched_deal_id:
                            logger.info(
                                f"[STEP 2.16] Deal match found (deal_id={matched_deal_id})")
                            matched_deal = None
                            if deals_collection is not None:
                                try:
                                    matched_deal = deals_collection.find_one(
                                        {"_id": ObjectId(matched_deal_id)}
                                    )
                                except Exception as e:
                                    logger.exception(
                                        f"[STEP 2.17] Error resolving matched deal: {e}")
                                    collect_error(
                                        error_items,
                                        str(e),
                                        step="resolve_matched_deal",
                                        context={"process": process_num},
                                    )

                            if not send_update_email(case_doc, changes, matched_deal):
                                collect_error(
                                    error_items,
                                    "Failed to send update email",
                                    step="send_email",
                                    context={"process": process_num, "detail_url": detail_url},
                                )
                            if not update_case_in_db(
                                cases_collection, case_doc, changes,
                                live_table, live_historico,
                                close_case=should_close,
                                new_deal_id=matched_deal_id,
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to update case document",
                                    step="update_case",
                                    context={"process": process_num, "detail_url": detail_url},
                                )
                        else:
                            is_usa = False
                            if interessados_text:
                                try:
                                    company_details = (
                                        f"Process: {process_num}\n"
                                        f"Type: {live_type}\n"
                                        f"Registration Date: {case_doc.get('registration_date', '')}\n"
                                        f"Interested Parties (PT): {interessados_text}\n"
                                        f"Interested Parties (EN): {case_doc.get('interessados_en', '')}\n"
                                        f"Detail URL: {detail_url}"
                                    )
                                    is_usa = bool(verify_usa_relation(
                                        company_details=company_details,
                                        case_type="BRAZIL",
                                    ))
                                except Exception as e:
                                    logger.exception(
                                        f"[STEP 2.18] Error verifying USA relation: {e}")
                                    collect_error(
                                        error_items,
                                        str(e),
                                        step="verify_usa_relation",
                                        context={"process": process_num, "detail_url": detail_url},
                                    )

                            if is_usa:
                                logger.info("[STEP 2.19] USA-related — sending email")
                                if not send_update_email(case_doc, changes, None):
                                    collect_error(
                                        error_items,
                                        "Failed to send update email",
                                        step="send_email",
                                        context={"process": process_num, "detail_url": detail_url},
                                    )

                            if not update_case_in_db(
                                cases_collection, case_doc, changes,
                                live_table, live_historico,
                                close_case=should_close,
                            ):
                                collect_error(
                                    error_items,
                                    "Failed to update case document",
                                    step="update_case",
                                    context={"process": process_num, "detail_url": detail_url},
                                )

                        time.sleep(2)

                    except Exception as e:
                        logger.exception(f"Error processing case #{idx}: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="process_case",
                            context={
                                "process": case_doc.get("process", "N/A"),
                                "detail_url": case_doc.get("detail_url"),
                            },
                        )
                        continue

            finally:
                browser.close()
                logger.info("[STEP 2.20] Browser closed")

    except Exception as e:
        logger.exception(f"Unhandled error in process_brazil_cases_updates(): {e}")
        collect_error(
            error_items,
            f"Unhandled error in process_brazil_cases_updates(): {e}",
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"[STEP 2.23] Total records checked        : {total_checked}")
        logger.info(f"[STEP 2.24] Records with changes         : {total_changed}")
        logger.info(
            f"[STEP 2.25] Errors encountered           : {len(error_items)}")
        logger.info(f"[STEP 2.26] Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    process_brazil_cases_updates(headless=True)

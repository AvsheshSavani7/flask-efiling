import os
import sys
import json
import time
import logging
import builtins
import re
import base64
import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bson import ObjectId
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from mongodb_connection import (
    get_database,
    get_deals_collection,
    init_mongodb_connection,
    is_connected,
)
from html import escape as escape_html

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

N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
    # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logger = logging.getLogger("cade_cases_update_monitor")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
logger.propagate = False


def _logged_print(*args, level: str = "info", **kwargs):
    msg = " ".join(str(a) for a in args)
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)
    builtins.print(*args, **kwargs)


print = _logged_print  # type: ignore


def utc_now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


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

def extract_autuacao_info(page, context, url: str) -> Dict[str, str]:
    """Open detail page and extract Autuação info."""
    detail_page = None
    try:
        detail_page = context.new_page()
        detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
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
                header = tbody.find("th", string=re.compile(r"Autuação", re.I))
                if not header:
                    continue
                for row in tbody.find_all("tr"):
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

        detail_page.close()
        return info
    except Exception as e:
        print(f"❌ Failed to extract Autuação from {url}: {e}")
        if detail_page:
            try:
                detail_page.close()
            except Exception:
                pass
        return {"process": "", "type": "", "registration_date": "", "interessados": ""}


def extract_tables(page, context, url: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract tblDocumentos + tblHistorico from detail page in one visit."""
    detail_page = None
    try:
        detail_page = context.new_page()
        detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
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
        print(f"❌ Failed to extract tables from {url}: {e}")
        if detail_page:
            try:
                detail_page.close()
            except Exception:
                pass
        return [], []


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

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

    # 2) interessados
    old_inter = (stored.get("interessados") or "").strip()
    new_inter = (live_interessados or "").strip()
    if old_inter != new_inter:
        if old_inter and not new_inter:
            change_t = "removed"
        elif not old_inter and new_inter:
            change_t = "new"
        else:
            change_t = "updated"
        changes.append(("interessados", old_inter, new_inter, change_t))

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
            print("    ✅ Updated case in brazil_cases")
        else:
            print("    ℹ️ No DB changes (document may be identical)")
        return True
    except Exception as e:
        print(f"    ❌ Error updating case: {e}")
        return False


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _post_email_payload(payload: Dict[str, Any]) -> bool:
    try:
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"    ✅ Email sent! Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"    ⚠️ Error sending email: {e}")
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
    print("🚀 Starting CADE Brazil Cases Update Monitor\n")

    print("🔌 Initializing MongoDB connection...")
    ok, msg = init_mongodb_connection(ENV_PATH)
    if not ok:
        print(f"❌ {msg}")
        return
    print(f"✅ {msg}\n")

    if not is_connected():
        print("❌ MongoDB not connected. Exiting.")
        return

    cases_collection = get_brazil_cases_collection()
    if cases_collection is None:
        print("❌ Could not access 'brazil_cases' collection. Exiting.")
        return

    deals_collection = get_deals_collection()
    deals_status_filter = {
        "$or": [
            {"deal_status": {"$in": ["Open", "Unknown"]}},
            {"deal_status": None},
            {"deal_status": {"$exists": False}},
        ]
    }

    # Step 1: fetch open records from brazil_cases
    cases = list(cases_collection.find({"is_open": True}))
    if not cases:
        print("⚠️ No open records in brazil_cases. Exiting.")
        return

    print(f"📊 Found {len(cases)} open records in brazil_cases\n")

    total_checked = 0
    total_changed = 0

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
                total_checked += 1
                process_num = case_doc.get("process", "N/A")
                detail_url = case_doc.get("detail_url")

                print(f"[{idx}/{len(cases)}] Process {process_num}")

                if not detail_url:
                    print("  ⚠️ No detail_url; skipping")
                    continue

                # Step 3: extract fresh data from live page
                autuacao = extract_autuacao_info(page, context, detail_url)
                live_type = autuacao.get("type", "")
                live_interessados = autuacao.get("interessados", "")

                live_table, live_historico = extract_tables(
                    page, context, detail_url)
                print(f"  📊 Live: type={live_type[:50]}..., "
                      f"table_records={len(live_table)}, historico={len(live_historico)}")

                should_close = any(
                    rec.get("tipo_documento", "").strip(
                    ) == "Certidão de Trânsito em Julgado"
                    for rec in live_table
                )
                if should_close:
                    print(
                        "  🔒 'Certidão de Trânsito em Julgado' found — will set is_open=False")

                # Step 4: detect changes
                changes = detect_changes(
                    case_doc, live_type, live_interessados, live_table, live_historico,
                )

                if not changes and not should_close:
                    print("  ✅ No changes detected")
                    continue

                if not changes and should_close:
                    print("  🔒 No field changes but closing case (is_open → False)")
                    update_case_in_db(
                        cases_collection, case_doc, changes,
                        live_table, live_historico,
                        close_case=True,
                    )
                    continue

                total_changed += 1
                print(f"  🔄 {len(changes)} change(s) detected:")
                for field, old_val, new_val, ctype in changes:
                    if ctype == "new_items":
                        print(f"    • {field}: {len(new_val)} new item(s)")
                    else:
                        print(
                            f"    • {field}: {old_val} → {new_val} ({ctype})")

                # Step 5: branch on deal_id
                deal = None
                deal_id = case_doc.get("deal_id")

                if deal_id and deals_collection is not None:
                    # Has deal_id → resolve deal, send email, update DB
                    try:
                        deal = deals_collection.find_one(
                            {"_id": ObjectId(deal_id), **deals_status_filter}
                        )
                    except Exception as e:
                        print(f"    ⚠️ Invalid deal_id: {e}")

                    if deal:
                        print("    🔗 Deal linked — sending email")
                        send_update_email(case_doc, changes, deal)
                        update_case_in_db(
                            cases_collection, case_doc, changes,
                            live_table, live_historico,
                            close_case=should_close,
                        )
                        continue

                # No deal_id (or deal not found) → check USA relation
                is_usa = False
                interessados_text = case_doc.get(
                    "interessados") or live_interessados
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
                        print(f"    ⚠️ Error verifying USA relation: {e}")

                if is_usa:
                    print("    🇺🇸 USA-related — sending email")
                    send_update_email(case_doc, changes, None)

                # Always update DB (with or without email)
                update_case_in_db(
                    cases_collection, case_doc, changes,
                    live_table, live_historico,
                    close_case=should_close,
                )

                time.sleep(2)

        except Exception as e:
            print(f"❌ Error in monitoring: {e}")
            import traceback
            traceback.print_exc()
        finally:
            browser.close()

    print(f"\n{'=' * 60}")
    print(f"📊 Summary:")
    print(f"   Total records checked: {total_checked}")
    print(f"   Records with changes: {total_changed}")
    print(f"{'=' * 60}")
    print("🎉 Done!")


if __name__ == "__main__":
    process_brazil_cases_updates(headless=True)

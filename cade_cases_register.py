import os
import sys
import json
import time
import logging
import re
import base64
import datetime
from datetime import date, timezone, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from scraper_error_utils import collect_error, send_error_summary
from mongodb_connection import (
    get_database,
    get_deals_collection,
    get_deal_by_id,
    init_mongodb_connection,
    is_connected,
)
from deal_match_llm import llm_match_deal_id, fetch_open_deals
from deal_match_regex import regex_match_cade_deal
from html import escape as escape_html
from log_utils import cleanup_old_logs, refresh_log_file
from email_subject_builder import build_subject
from n8n_email_service import post_email_payload

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv(".env")
ENV_PATH = ".env"

BASE__SCRAPER_URL = (
    "https://sei.cade.gov.br/sei/modulos/pesquisa/"
    "md_pesq_processo_pesquisar.php"
    "?acao_externa=protocolo_pesquisar"
    "&acao_origem_externa=protocolo_pesquisar"
    "&id_orgao_acesso_externo=0"
)
BASE_PESQUISA_URL = "https://sei.cade.gov.br/sei/modulos/pesquisa/"
RECAPTCHA_SITE_KEY = "6Le2a7gqAAAAAAVxMYQ-mn7GyO8lcWAQq4Hxm-2G"

CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY")

BACKUP_JSON = "cade_cases_register_backup.json"

PROCESS_TYPES = {
    "Finalístico: Ato de Concentração Sumário": "100000513",
    "Finalístico: Ato de Concentração Ordinário": "100000512",
    "Finalístico: Apuração de Ato de Concentração": "100000511",
    "Finalístico: Medida Cautelar": "100000566",
}
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------------------------
# Logging — production setup (RotatingFileHandler, IST, env-based settings)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "brazil_cases_register"
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


def case_exists_by_url(collection, detail_url: str) -> bool:
    try:
        return collection.count_documents({"detail_url": detail_url}, limit=1) > 0
    except Exception as e:
        logger.exception(f"Error checking existing case: {e}")
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    process = case_info.get("process", "?")
    try:
        result = collection.insert_one(case_info)
        inserted_id = str(result.inserted_id)
        logger.info(f"  [{process}] Inserted into DB (id={inserted_id})")
        return inserted_id
    except Exception as e:
        logger.exception(f"Error inserting case {process}: {e}")
        return None


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate_to_english(text: str) -> str:
    if not text or not isinstance(text, str) or text.strip() == "":
        return text
    if len(text) > 500:
        print(f"⚠️ Skipping translation for long text ({len(text)} chars)")
        return text
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "pt",
                  "tl": "en", "dt": "t", "q": text}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            return response.json()[0][0][0]
    except requests.Timeout:
        print(f"⚠️ Translation timeout for: {text[:50]}...")
        return text
    except Exception as e:
        print(f"⚠️ Translation failed: {e}")
        return text
    return text


# ---------------------------------------------------------------------------
# reCAPTCHA v2 solving (2captcha)
# ---------------------------------------------------------------------------

def solve_recaptcha_v2(site_key: str, page_url: str, api_key: str = None) -> Optional[str]:
    if not api_key:
        print("⚠️ No CAPTCHA API key found.")
        return None

    print("🔐 Solving reCAPTCHA with 2captcha...")
    data = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    try:
        resp = requests.post(CAPTCHA_SOLVER_URL, data=data)
        result = resp.json()
        if result.get("status") != 1:
            print(
                f"❌ 2Captcha submission failed: {result.get('request', resp.text)}")
            return None

        task_id = result.get("request")
        if not task_id:
            return None

        print(f"📝 2Captcha task id: {task_id}")
        for attempt in range(30):
            time.sleep(5)
            r = requests.get(CAPTCHA_RESULT_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1
            })
            res = r.json()
            if res.get("status") == 1:
                token = res.get("request")
                print(f"✅ reCAPTCHA solved! Token: {token[:20]}...")
                return token
            elif res.get("request") != "CAPCHA_NOT_READY":
                print(f"❌ 2Captcha error: {res}")
                break
            if attempt % 3 == 0:
                print(f"⏳ Waiting for captcha... (attempt {attempt + 1}/30)")

        print("⏱️ Captcha was not solved in time.")
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
                var textarea = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                               document.querySelector('#g-recaptcha-response');
                if (textarea) {
                    textarea.value = token;
                    textarea.innerHTML = token;
                    var evt1 = new Event('input', {bubbles: true});
                    var evt2 = new Event('change', {bubbles: true});
                    textarea.dispatchEvent(evt1);
                    textarea.dispatchEvent(evt2);
                }
                var verificaField = document.getElementById('verificaRecaptcha');
                if (verificaField) verificaField.value = 'true';
                if (typeof recaptchaCallback === 'function') {
                    try { recaptchaCallback(); } catch(e) {}
                }
            }
        """, token)
        time.sleep(1)

        verification = page.evaluate("""
            () => {
                var ta = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                         document.querySelector('#g-recaptcha-response');
                return ta ? ta.value.length > 0 : false;
            }
        """)
        if verification:
            print("✅ Token verified")
            return True
        print("❌ Token injection failed")
        return False
    except Exception as e:
        print(f"⚠️ Error injecting token: {e}")
        return False


def check_and_solve_recaptcha(page) -> bool:
    try:
        recaptcha_div = page.locator("#g-recaptcha")
        if recaptcha_div.count() > 0 and recaptcha_div.is_visible():
            print("🔐 reCAPTCHA detected, solving...")
            token = solve_recaptcha_v2(
                RECAPTCHA_SITE_KEY, page.url, CAPTCHA_API_KEY)
            if token:
                for retry in range(3):
                    if fill_recaptcha_token(page, token):
                        return True
                    time.sleep(2)
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Image CAPTCHA solving (detail page)
# ---------------------------------------------------------------------------

def solve_image_captcha(page, api_key: str = None) -> Optional[str]:
    if not api_key:
        return None
    try:
        captcha_img = page.locator(
            "img[src*='captcha' i], img[alt*='captcha' i], img[id*='captcha' i]"
        )
        img_src = None
        if captcha_img.count() > 0:
            img_src = captcha_img.first.get_attribute("src")

        if not img_src:
            all_imgs = page.locator("img").all()
            for img in all_imgs:
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
            img_response = requests.get(img_src, timeout=10)
            if img_response.status_code != 200:
                return None
            img_base64 = base64.b64encode(img_response.content).decode("utf-8")

        print("🔐 Solving image CAPTCHA with 2captcha...")
        resp = requests.post(CAPTCHA_SOLVER_URL, data={
            "key": api_key, "method": "base64", "body": img_base64, "json": 1,
        })
        result = resp.json()
        if result.get("status") != 1:
            print(
                f"❌ Image CAPTCHA submission failed: {result.get('request')}")
            return None

        task_id = result.get("request")
        for attempt in range(30):
            time.sleep(5)
            r = requests.get(CAPTCHA_RESULT_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            })
            res = r.json()
            if res.get("status") == 1:
                solution = res.get("request")
                print(f"✅ Image CAPTCHA solved: {solution}")
                return solution
            elif res.get("request") != "CAPCHA_NOT_READY":
                print(f"❌ 2Captcha error: {res}")
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
            all_imgs = page.locator("img").all()
            for img in all_imgs:
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
        try:
            inputs = page.locator("input[type='text']").all()
            for inp in inputs:
                try:
                    box = inp.bounding_box()
                    if box and box["width"] < 200:
                        captcha_input = inp
                        break
                except Exception:
                    continue
        except Exception:
            pass

        submit_button = None
        try:
            loc = page.locator(
                "button:has-text('Enviar'), input[type='submit'][value*='Enviar' i]"
            )
            if loc.count() > 0:
                submit_button = loc.first
        except Exception:
            pass

        if not captcha_input:
            print("⚠️ Could not find CAPTCHA input field")
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

        print("⚠️ Failed to solve image CAPTCHA")
        return False
    except Exception as e:
        print(f"⚠️ Error handling image CAPTCHA: {e}")
        return False


# ---------------------------------------------------------------------------
# Search form submission
# ---------------------------------------------------------------------------

def submit_search_form(page, start_date, end_date, process_type_id: Optional[str] = None) -> bool:
    try:
        start_fmt = start_date.strftime("%d/%m/%Y")
        end_fmt = end_date.strftime("%d/%m/%Y")
        print(f"📅 Setting date range: {start_fmt} to {end_fmt}")

        if process_type_id:
            page.locator("#selTipoProcedimentoPesquisa").select_option(
                value=process_type_id)
            print(f"📋 Selected process type ID: {process_type_id}")

        page.locator("#txtDataInicio").fill(start_fmt)
        page.locator("#txtDataFim").fill(end_fmt)

        checkbox = page.locator("#chkSinProcessos")
        if not checkbox.is_checked():
            checkbox.check()

        try:
            recaptcha_div = page.locator("#g-recaptcha")
            if recaptcha_div.is_visible():
                print("🔐 reCAPTCHA detected on search form, solving...")
                token = solve_recaptcha_v2(
                    RECAPTCHA_SITE_KEY, page.url, CAPTCHA_API_KEY)
                if token:
                    filled = False
                    for retry in range(3):
                        if fill_recaptcha_token(page, token):
                            filled = True
                            print("✅ reCAPTCHA token filled successfully")
                            break
                        time.sleep(2)
                    if not filled:
                        print("⚠️ Failed to fill reCAPTCHA token after 3 retries")
                else:
                    print("⚠️ Failed to obtain reCAPTCHA token — search may fail")
        except Exception as e:
            print(f"⚠️ reCAPTCHA handling error: {e}")

        page.locator("#sbmPesquisar").click()
        print("⏳ Waiting for search results...")

        try:
            page.wait_for_selector(
                "table tr, .pesquisaTituloRegistro, .infraTabelaResultado",
                timeout=15000,
            )
            print("✅ Results table detected")
        except Exception:
            print(
                "⚠️ No results table detected within 15s, continuing with fallback wait...")
            time.sleep(5)

        return True
    except Exception as e:
        print(f"❌ Error submitting form: {e}")
        return False


# ---------------------------------------------------------------------------
# Search result parsing
# ---------------------------------------------------------------------------

def parse_search_results(page) -> List[Dict[str, Any]]:
    try:
        time.sleep(3)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        # print(f"🔍 Parsing search results: {soup}")

        results: List[Dict[str, Any]] = []
        tables = soup.find_all("table")
        all_rows = []
        for table in tables:
            for row in table.find_all("tr"):
                if row.find_all("td"):
                    all_rows.append(row)

        if not all_rows:
            try:
                debug_dir = os.path.dirname(LOG_FILE)
                debug_path = os.path.join(debug_dir, "debug_no_results.png")
                page.screenshot(path=debug_path)
                print(
                    f"⚠️ No table rows found — screenshot saved to {debug_path}")
            except Exception as ss_err:
                print(f"⚠️ No table rows found (screenshot failed: {ss_err})")
            return []

        idx = 0
        record_index = 1
        while idx < len(all_rows):
            row = all_rows[idx]
            if "pesquisaTituloRegistro" in (row.get("class") or []):
                title_row = row
                metadata_row = all_rows[idx + 1] if idx + \
                    1 < len(all_rows) else None
                try:
                    title_text = title_row.get_text(
                        separator=" | ", strip=True)
                    all_links = title_row.find_all("a", href=True)
                    if metadata_row:
                        all_links.extend(metadata_row.find_all("a", href=True))

                    detail_url = None
                    for link in all_links:
                        href = link.get("href", "")
                        if href:
                            if not href.startswith("http"):
                                href = requests.compat.urljoin(
                                    BASE__SCRAPER_URL, href)
                            detail_url = href
                            break

                    if detail_url:
                        results.append({
                            "index": record_index,
                            "title": title_text,
                            "detail_url": detail_url,
                        })
                        record_index += 1
                    idx += 2
                except Exception as e:
                    print(f"⚠️ Error parsing record at row {idx}: {e}")
                    idx += 1
            else:
                idx += 1

        print(f"✅ Parsed {len(results)} records")
        return results
    except Exception as e:
        print(f"❌ Error parsing results: {e}")
        return []


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def collect_all_pages(page) -> List[Dict[str, Any]]:
    """Paginate through all search results and return combined list."""
    all_results: List[Dict[str, Any]] = []
    page_num = 1

    while True:
        print(f"\n[STEP 2.2.1] Processing page {page_num}...")
        check_and_solve_recaptcha(page)

        parsed = parse_search_results(page)
        all_results.extend(parsed)
        print(
            f"[STEP 2.2.2] ✅ Found {len(parsed)} results on page {page_num} (total: {len(all_results)})")

        try:
            current_indicator = page.locator(".pesquisaPaginaSelecionada")
            current_displayed = page_num
            if current_indicator.count() > 0:
                text = current_indicator.inner_text().strip()
                if text.isdigit():
                    current_displayed = int(text)
                    page_num = current_displayed

            has_next = False
            next_button = None

            next_locator = page.locator(
                "a:has-text('Próxima'), a:has-text('Próximo')")
            if next_locator.count() > 0:
                has_next = True
                next_button = next_locator.first

            if not has_next:
                page_links = page.locator(".pesquisaPaginas a").all()
                for link in page_links:
                    link_text = link.inner_text().strip()
                    if link_text.isdigit() and int(link_text) > current_displayed:
                        has_next = True
                        next_button = link
                        break

            if not has_next:
                all_page_links = page.locator(".pesquisaPaginas a").all()
                found_higher = False
                for link in all_page_links:
                    lt = link.inner_text().strip()
                    if lt.isdigit() and int(lt) > current_displayed:
                        found_higher = True
                        break
                if not found_higher:
                    print(
                        f"[STEP 2.2.3] ✅ Reached last page ({current_displayed})")
                    break

            if has_next and next_button:
                is_disabled = (
                    "disabled" in (next_button.get_attribute(
                        "class") or "").lower()
                    or (next_button.get_attribute("aria-disabled") or "").lower() == "true"
                )
                if is_disabled:
                    print("[STEP 2.2.4] ✅ Reached last page (next button disabled)")
                    break

                next_button.scroll_into_view_if_needed()
                time.sleep(0.5)
                next_button.click()
                time.sleep(4)
                check_and_solve_recaptcha(page)
                time.sleep(1)

                new_indicator = page.locator(".pesquisaPaginaSelecionada")
                if new_indicator.count() > 0:
                    nt = new_indicator.inner_text().strip()
                    if nt.isdigit():
                        page_num = int(nt)
                    else:
                        page_num += 1
                else:
                    page_num += 1
                print(f"[STEP 2.2.5] ✅ Navigated to page {page_num}")
            else:
                print("[STEP 2.2.6] ✅ No more pages")
                break
        except Exception as e:
            print(f"[STEP 2.2.7] ⚠️ Pagination error: {e}")
            break

    return all_results


# ---------------------------------------------------------------------------
# Detail page extraction (single visit)
# ---------------------------------------------------------------------------

_EMPTY_AUTUACAO = {"process": "", "type": "",
                   "registration_date": "", "interessados": ""}


def extract_detail_page(
    page,
    context,
    url: str,
    skip_certidao_transitada: bool = False,
    error_items: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Open the detail page ONCE and extract everything:
      - Autuação info (process, type, registration_date, interessados)
      - tblDocumentos records
      - tblHistorico records

    Returns a dict with keys: autuacao, table_records, historico_records.
    Returns None when skip_certidao_transitada triggers.
    Retries on timeout/failure.
    """
    max_retries = 2
    for attempt in range(1, max_retries + 1):
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

            # --- Autuação ---
            info = dict(_EMPTY_AUTUACAO)

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

            print(f"  📄 Autuação: process={info['process']}, type={info['type'][:60]}..., "
                  f"date={info['registration_date']}, interessados={info['interessados'][:80]}...")

            # --- tblDocumentos ---
            doc_table = soup.find("table", id="tblDocumentos")
            table_data: List[Dict[str, Any]] = []

            if doc_table:
                for row in doc_table.find_all("tr"):
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
                        cell_text = cell.get_text(strip=True)
                        if re.match(r"^\d{2}/\d{2}/\d{4}$", cell_text):
                            dates_found.append(cell_text)

                    if len(dates_found) >= 1:
                        row_data["data_documento"] = dates_found[0]
                    if len(dates_found) >= 2:
                        row_data["data_registro"] = dates_found[1]

                    unidade_link = row.find("a", class_="ancoraSigla")
                    if unidade_link:
                        u_text = unidade_link.get_text(strip=True)
                        if u_text:
                            row_data["unidade"] = u_text

                    row_data = {k: v for k, v in row_data.items()
                                if v and str(v).strip()}
                    if row_data.get("documento_processo"):
                        table_data.append(row_data)

                print(
                    f"  📊 Extracted {len(table_data)} records from tblDocumentos")
            else:
                print("  ⚠️ tblDocumentos not found on detail page")

            if skip_certidao_transitada and any(
                rec.get("tipo_documento", "").strip(
                ) == "Certidão de Trânsito em Julgado"
                for rec in table_data
            ):
                print("  ⏩ 'Certidão de Trânsito em Julgado' found — skipping record")
                detail_page.close()
                return None

            # --- tblHistorico ---
            historico_table = soup.find("table", id="tblHistorico")
            historico_data: List[Dict[str, Any]] = []

            if historico_table:
                for row in historico_table.find_all("tr"):
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

                    description_en = translate_to_english(description)

                    historico_data.append({
                        "date_time": date_time,
                        "unit": unit,
                        "description": description,
                        "description_en": description_en,
                    })

                print(
                    f"  📊 Extracted {len(historico_data)} records from tblHistorico")
            else:
                print("  ⚠️ tblHistorico not found on detail page")

            detail_page.close()
            return {
                "autuacao": info,
                "table_records": table_data,
                "historico_records": historico_data,
            }
        except Exception as e:
            logger.warning(
                f"  Attempt {attempt}/{max_retries} failed for {url}: {e}")
            if detail_page:
                if attempt < max_retries:
                    try:
                        detail_page.close()
                    except Exception:
                        pass
                    logger.info(f"  Retrying in 10s...")
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
                        f"  Debug screenshot saved to {screenshot_path}")
                except Exception:
                    pass
                try:
                    detail_page.close()
                except Exception:
                    pass
            explanation = (
                f"Failed to extract detail page after {max_retries} attempts. "
                f"URL: {url}. Last error: {e}. "
                f"The CADE SEI portal may be temporarily unavailable, slow to render, "
                f"or its page structure may have changed."
            )
            if error_items is not None:
                collect_error(
                    error_items,
                    explanation,
                    step="extract_detail_page",
                    context={
                        "url": url,
                        "attempts": max_retries,
                        "traceback": str(e),
                        "screenshot": screenshot_path or "capture failed",
                    },
                )
            return None


# ---------------------------------------------------------------------------
# LLM deal matching (ACCC pattern)
# ---------------------------------------------------------------------------

def match_case_to_deal(
    interessados_text: str,
    translated_text: str,
    deals: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """
    Use LLM to match CADE interessados text against deals.

    Args:
        interessados_text: Original Portuguese text.
        translated_text:   English translation of the interessados text.
        deals:             Pre-loaded deal list. Fetched from MongoDB if None.

    Returns deal_id string or None.
    """
    return llm_match_deal_id(
        regulator_name="CADE Brazil",
        case_sections={
            "INTERESSADOS TEXT (translated to English)": translated_text,
            "ORIGINAL TEXT (Portuguese)": interessados_text,
        },
        source_label="the interessados text",
        deals=deals,
    )


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _post_email_payload(payload: Dict[str, Any]) -> bool:
    logger.info(f"  Sending email: {payload.get('subject', 'N/A')}")
    return post_email_payload(payload)


def send_matched_email(
    case_data: Dict[str, Any],
    deal_id: str,
    deal_match: Optional[Dict[str, Any]] = None,
    matched_by_regex: bool = False,
) -> bool:
    process = case_data.get("process", "N/A")
    interessados = case_data.get(
        "interessados_en") or case_data.get("interessados", "N/A")
    case_type = case_data.get("type_en") or case_data.get("type", "N/A")
    reg_date = case_data.get("registration_date", "N/A")
    detail_url = case_data.get("detail_url", "")
    table_records = case_data.get("table_records", [])

    subject = build_subject("cade", "new", deal_match)
    if matched_by_regex:
        subject = subject.replace("[FRMD]", "[FRRMD]")

    table_html = ""
    if table_records:
        rows_html = ""
        for idx, rec in enumerate(table_records):
            bg = "#ffffff" if idx % 2 == 0 else "#f9f9f9"
            doc_process = escape_html(str(rec.get("documento_processo", "")))
            doc_type = escape_html(
                str(rec.get("document_type", rec.get("tipo_documento", ""))))
            doc_date = escape_html(str(rec.get("data_documento", "")))
            reg_d = escape_html(str(rec.get("data_registro", "")))
            unit = escape_html(str(rec.get("unidade", "")))
            doc_url = rec.get("document_url", "")
            dp_html = f'<a href="{escape_html(doc_url)}" style="color:#4a90e2;">{doc_process}</a>' if doc_url else doc_process
            rows_html += f'<tr style="background:{bg};"><td style="padding:6px;border:1px solid #ddd;">{dp_html}</td><td style="padding:6px;border:1px solid #ddd;">{doc_type}</td><td style="padding:6px;border:1px solid #ddd;">{doc_date}</td><td style="padding:6px;border:1px solid #ddd;">{reg_d}</td><td style="padding:6px;border:1px solid #ddd;">{unit}</td></tr>'

        table_html = f"""<h3 style="margin-top:16px;">Documents ({len(table_records)})</h3>
<table style="width:100%;border-collapse:collapse;"><thead><tr style="background:#f5f5f5;"><th style="padding:6px;border:1px solid #ddd;text-align:left;">Doc Process</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Type</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Doc Date</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Reg Date</th><th style="padding:6px;border:1px solid #ddd;text-align:left;">Unit</th></tr></thead><tbody>{rows_html}</tbody></table>"""

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:900px;margin:0 auto;">
  <h2 style="margin:0 0 10px 0;border-bottom:3px solid #4a90e2;padding-bottom:12px;">CADE Brazil – Matched Deal</h2>
  <div style="margin-bottom:12px;">
    <div><b>Process:</b> {escape_html(process)}</div>
    <div><b>Type:</b> {escape_html(case_type)}</div>
    <div><b>Registration Date:</b> {escape_html(reg_date)}</div>
    <div><b>Interested Parties:</b> {escape_html(interessados)}</div>
    <div><b>Deal ID:</b> {escape_html(deal_id)}</div>
  </div>
  {'<div><a href="'+escape_html(detail_url)+'" target="_blank">View CADE Detail Page →</a></div>' if detail_url else ''}
  {table_html}
</div>""".strip()

    return _post_email_payload({
        "subject": subject,
        "html": html,
        "process": process,
        "deal_id": deal_id,
        "detail_url": detail_url,
        "is_new_case": True,
    })


def send_usa_related_email(case_data: Dict[str, Any]) -> bool:
    process = case_data.get("process", "N/A")
    interessados = case_data.get(
        "interessados_en") or case_data.get("interessados", "N/A")
    case_type = case_data.get("type_en") or case_data.get("type", "N/A")
    reg_date = case_data.get("registration_date", "N/A")
    detail_url = case_data.get("detail_url", "")

    subject = build_subject("cade", "new")

    html = f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:900px;margin:0 auto;">
  <h2 style="margin:0 0 10px 0;border-bottom:3px solid #f59e0b;padding-bottom:12px;">CADE Brazil – USA-Related (Unmatched)</h2>
  <div style="background:#f59e0b;color:white;padding:6px 12px;border-radius:4px;display:inline-block;margin-bottom:12px;font-weight:bold;">USA-RELATED</div>
  <div style="margin-bottom:12px;">
    <div><b>Process:</b> {escape_html(process)}</div>
    <div><b>Type:</b> {escape_html(case_type)}</div>
    <div><b>Registration Date:</b> {escape_html(reg_date)}</div>
    <div><b>Interested Parties:</b> {escape_html(interessados)}</div>
  </div>
  {'<div><a href="'+escape_html(detail_url)+'" target="_blank">View CADE Detail Page →</a></div>' if detail_url else ''}
</div>""".strip()

    return _post_email_payload({
        "subject": subject,
        "html": html,
        "process": process,
        "deal_id": None,
        "detail_url": detail_url,
        "usa_related": True,
        "is_unmatched": True,
        "is_new_case": True,
    })


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_cade_cases_register(
    start_date=None,
    end_date=None,
    headless: bool = True,
    test_mode: bool = False,
    skip_certidao_transitada: bool = False,
):
    """
    Scrape CADE SEI public notices for a date range and store every record
    in the ``brazil_cases`` collection.

    - Matched records: extract table data, ``are_we_follow=True``, send email
    - USA-related (unmatched): extract table data, ``are_we_follow=True``, send email
    - Other: save basic info only, ``are_we_follow=False``, no email
    """
    global LOG_FILE
    LOG_FILE = refresh_log_file(logger, LOG_FILE, _get_log_file)
    run_start = time.time()
    error_items: List[Dict[str, Any]] = []
    mode_label = "TEST MODE" if test_mode else "LIVE MODE"

    logger.info("=" * 60)
    logger.info(f"Starting CADE Cases Register ({mode_label})")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)

    new_cases: List[Dict[str, Any]] = []
    llm_match_count = 0
    regex_match_count = 0

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

        collection = get_brazil_cases_collection()
        if collection is None:
            collect_error(
                error_items,
                "Could not access 'brazil_cases' collection",
                step="get_collection",
            )
            return

        logger.info("[STEP 1.4] brazil_cases collection ready")

        open_deals = fetch_open_deals()

        if end_date is None:
            end_date = datetime.datetime.now()
        if start_date is None:
            start_date = end_date - datetime.timedelta(days=2)

        logger.info(
            f"[STEP 1.5] Date range: {start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')}")

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
                logger.info(
                    f"[STEP 2] Searching {start_date.strftime('%Y-%m-%d')} → "
                    f"{end_date.strftime('%Y-%m-%d')} "
                    f"across {len(PROCESS_TYPES)} process types"
                )

                all_results: List[Dict[str, Any]] = []
                seen_urls: set = set()

                for type_name, type_id in PROCESS_TYPES.items():
                    logger.info(
                        f"[STEP 2.1] Searching: {type_name} (id={type_id})")

                    page.goto(BASE__SCRAPER_URL, wait_until="domcontentloaded",
                              timeout=50000)
                    time.sleep(3)

                    if not submit_search_form(page, start_date, end_date, process_type_id=type_id):
                        collect_error(
                            error_items,
                            f"Failed to submit search form for {type_name}",
                            step="submit_search_form",
                            context={"process_type": type_name},
                        )
                        continue

                    type_results = collect_all_pages(page)
                    logger.info(
                        f"[STEP 2.3] Found {len(type_results)} results for {type_name}")

                    added = 0
                    for r in type_results:
                        url = r.get("detail_url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            r["process_type_filter"] = type_name
                            all_results.append(r)
                            added += 1
                    logger.info(
                        f"[STEP 2.4]  +{added} new (after dedup), {len(type_results) - added} duplicates skipped")

                logger.info(
                    f"[STEP 2.5] Total collected (deduplicated): {len(all_results)} results")

                for idx, result in enumerate(all_results, 1):
                    try:
                        detail_url = result.get("detail_url")
                        title = result.get("title", "N/A")

                        logger.info(
                            f"[STEP 2.6] [{idx}/{len(all_results)}] {title[:80]}...")

                        if not detail_url:
                            logger.warning("[STEP 2.7] No detail URL; skipping")
                            continue

                        if not test_mode and case_exists_by_url(collection, detail_url):
                            logger.info("[STEP 2.8] Already in brazil_cases; skipping")
                            continue

                        detail = extract_detail_page(
                            page,
                            context,
                            detail_url,
                            skip_certidao_transitada=skip_certidao_transitada,
                            error_items=error_items,
                        )

                        if detail is None:
                            continue

                        autuacao = detail["autuacao"]
                        table_data = detail["table_records"]
                        historico_data = detail["historico_records"]
                        process_num = autuacao.get("process", "")

                        interessados_text = autuacao.get("interessados", "").strip()

                        translated = ""
                        if interessados_text:
                            translated = translate_to_english(interessados_text)
                            logger.info(
                                f"[STEP 2.9] Translated: {translated[:150]}...")

                        type_en = ""
                        if autuacao.get("type"):
                            type_en = translate_to_english(autuacao["type"])

                        now_iso = utc_now_iso()
                        case_doc: Dict[str, Any] = {
                            "process": process_num,
                            "type": autuacao.get("type", ""),
                            "type_en": type_en,
                            "registration_date": autuacao.get("registration_date", ""),
                            "interessados": interessados_text,
                            "interessados_en": translated,
                            "detail_url": detail_url,
                            "is_open": True,
                            "created_at": now_iso,
                            "updated_at": now_iso,
                        }

                        translated_table = []
                        for rec in table_data:
                            tr = rec.copy()
                            if "tipo_documento" in tr:
                                doc_type = tr["tipo_documento"]
                                if isinstance(doc_type, str) and doc_type.strip():
                                    tr["document_type"] = translate_to_english(
                                        doc_type)
                                    tr.pop("tipo_documento", None)
                            translated_table.append(tr)

                        case_doc["table_records"] = translated_table
                        case_doc["historico_records"] = historico_data

                        matched_deal_id = None
                        if interessados_text:
                            try:
                                matched_deal_id = match_case_to_deal(
                                    interessados_text, translated, deals=open_deals)
                            except Exception as e:
                                logger.exception(
                                    f"[STEP 2.10] Error during deal matching: {e}")
                                collect_error(
                                    error_items,
                                    str(e),
                                    step="match_case_to_deal",
                                    context={
                                        "detail_url": detail_url,
                                        "process": process_num,
                                    },
                                )

                        # Regex fallback — only when LLM found nothing
                        matched_by_regex = False
                        if matched_deal_id:
                            llm_match_count += 1
                        else:
                            matched_deal_id = regex_match_cade_deal(
                                translated, open_deals
                            )
                            if matched_deal_id:
                                matched_by_regex = True
                                regex_match_count += 1
                                logger.info(
                                    f"[STEP 2.10b] Regex fallback matched deal_id={matched_deal_id}")
                            else:
                                logger.info(
                                    "[STEP 2.10b] No match (LLM + regex both returned None)")

                        if matched_deal_id:
                            logger.info(
                                f"[STEP 2.11] Deal match found (deal_id={matched_deal_id})")
                            case_doc["deal_id"] = matched_deal_id

                            if not test_mode:
                                deal_match = get_deal_by_id(matched_deal_id)
                                if not send_matched_email(case_doc, matched_deal_id, deal_match, matched_by_regex=matched_by_regex):
                                    collect_error(
                                        error_items,
                                        "Failed to send matched-case email",
                                        step="send_email",
                                        context={
                                            "detail_url": detail_url,
                                            "process": process_num,
                                        },
                                    )
                        else:
                            is_usa = False
                            if interessados_text:
                                try:
                                    company_details = (
                                        f"Process: {autuacao.get('process', '')}\n"
                                        f"Type: {autuacao.get('type', '')}\n"
                                        f"Registration Date: {autuacao.get('registration_date', '')}\n"
                                        f"Interested Parties (PT): {interessados_text}\n"
                                        f"Interested Parties (EN): {translated}\n"
                                        f"Detail URL: {detail_url}"
                                    )
                                    is_usa = bool(verify_usa_relation(
                                        company_details=company_details,
                                        case_type="BRAZIL",
                                    ))
                                except Exception as e:
                                    logger.exception(
                                        f"[STEP 2.12] Error verifying USA relation: {e}")
                                    collect_error(
                                        error_items,
                                        str(e),
                                        step="verify_usa_relation",
                                        context={
                                            "detail_url": detail_url,
                                            "process": process_num,
                                        },
                                    )

                            if is_usa:
                                logger.info(
                                    "[STEP 2.13] USA-related (unmatched) — sending email")
                                if not test_mode:
                                    if not send_usa_related_email(case_doc):
                                        collect_error(
                                            error_items,
                                            "Failed to send USA-related email",
                                            step="send_email",
                                            context={
                                                "detail_url": detail_url,
                                                "process": process_num,
                                            },
                                        )
                            else:
                                logger.info(
                                    "[STEP 2.14] No match, not USA-related — saving record only")

                        inserted_id = insert_case(collection, case_doc)
                        if inserted_id:
                            logger.info(
                                f"[STEP 2.15] Inserted into brazil_cases (id={inserted_id})")
                            backup = dict(case_doc)
                            backup.pop("_id", None)
                            new_cases.append(backup)
                        else:
                            collect_error(
                                error_items,
                                "Insert failed",
                                step="insert_case",
                                context={
                                    "detail_url": detail_url,
                                    "process": process_num,
                                },
                            )
                    except Exception as e:
                        logger.exception(
                            f"Error processing list item #{idx}: {e}")
                        collect_error(
                            error_items,
                            str(e),
                            step="process_list_item",
                            context={"detail_url": result.get("detail_url")},
                        )

            finally:
                browser.close()
                logger.info("[STEP 2.18] Browser closed")

        if new_cases:
            try:
                serializable = []
                for c in new_cases:
                    d = dict(c)
                    d.pop("_id", None)
                    serializable.append(d)
                with open(BACKUP_JSON, "w", encoding="utf-8") as f:
                    json.dump(serializable, f, indent=2,
                              ensure_ascii=False, default=str)
                logger.info(
                    f"[STEP 2.19] Saved {len(serializable)} cases to {BACKUP_JSON}")
            except Exception as e:
                logger.warning(f"[STEP 2.20] Error writing backup JSON: {e}")

    except Exception as e:
        logger.exception(f"Unhandled error in run_cade_cases_register(): {e}")
        collect_error(
            error_items,
            f"Unhandled error in run_cade_cases_register(): {e}",
            step="run_main",
        )

    finally:
        send_error_summary(error_items, SCRIPT_NAME)

        elapsed = round(time.time() - run_start, 1)
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info(f"[STEP 2.23] New cases inserted           : {len(new_cases)}")
        logger.info(f"[STEP 2.23a] LLM deal matches            : {llm_match_count}")
        logger.info(f"[STEP 2.23b] Regex fallback matches      : {regex_match_count}")
        logger.info(
            f"[STEP 2.24] Errors encountered           : {len(error_items)}")
        logger.info(f"[STEP 2.25] Total time                   : {elapsed}s")
        logger.info("=" * 60)


if __name__ == "__main__":
    env_flag = os.getenv("CADE_CASES_TEST_MODE", "").lower()
    test_mode_env = env_flag in ("1", "true", "yes", "y")
    run_cade_cases_register(test_mode=False, skip_certidao_transitada=True)

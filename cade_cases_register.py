import os
import sys
import json
import time
import logging
import builtins
import re
import base64
import datetime
import traceback
from datetime import date, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

from llm_verification_service import verify_usa_relation
from error_email_service import send_error_email
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

BASE_URL = (
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
N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://n8n-xwx1.onrender.com/webhook/4670ee2c-cc2a-4316-a975-d68cba2cd4a6",
    # "https://n8n-xwx1.onrender.com/webhook/d50502ea-6746-4d4b-8dfe-fb7bd71e0a1f",
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------------------------------------------
# Logging — date-wise log files under /var/data/logs/ (persistent disk)
# Timestamps in IST (UTC+5:30)
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
SCRIPT_NAME = "brazil_cases_register"
IST = timezone(timedelta(hours=5, minutes=30))


def _get_log_file() -> str:
    base = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."
    log_dir = os.path.join(base, SCRIPT_NAME)
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"{today}.log")


LOG_FILE = _get_log_file()

logger = logging.getLogger("brazil_cases_register")
logger.setLevel(logging.INFO)


class _ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.datetime.fromtimestamp(timestamp, tz=IST)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %I:%M:%S %p IST")


if not logger.handlers:
    formatter = _ISTFormatter(fmt="%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

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


def _log_error_and_email(msg: str, context: Optional[Dict[str, Any]] = None):
    """Log at ERROR level and fire an error email."""
    logger.error(msg)
    send_error_email(
        script_name=SCRIPT_NAME,
        error_message=msg,
        context=context,
        traceback_str=traceback.format_exc() if sys.exc_info()[0] else None,
    )


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
        _log_error_and_email(
            f"Error checking existing case: {e}",
            {"detail_url": detail_url, "step": "case_exists_by_url"},
        )
        return False


def insert_case(collection, case_info: Dict[str, Any]) -> Optional[str]:
    process = case_info.get("process", "?")
    try:
        result = collection.insert_one(case_info)
        inserted_id = str(result.inserted_id)
        logger.info(f"  [{process}] Inserted into DB (id={inserted_id})")
        return inserted_id
    except Exception as e:
        _log_error_and_email(
            f"Error inserting case {process}: {e}",
            {"process": process, "step": "insert_case"},
        )
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
                token = solve_recaptcha_v2(
                    RECAPTCHA_SITE_KEY, page.url, CAPTCHA_API_KEY)
                if token:
                    for retry in range(3):
                        if fill_recaptcha_token(page, token):
                            break
                        time.sleep(2)
        except Exception:
            pass

        page.locator("#sbmPesquisar").click()
        print("⏳ Waiting for search results...")
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

        results: List[Dict[str, Any]] = []
        tables = soup.find_all("table")
        all_rows = []
        for table in tables:
            for row in table.find_all("tr"):
                if row.find_all("td"):
                    all_rows.append(row)

        if not all_rows:
            print("⚠️ No table rows found")
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
                                href = requests.compat.urljoin(BASE_URL, href)
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
        print(f"\n📄 Processing page {page_num}...")
        check_and_solve_recaptcha(page)

        parsed = parse_search_results(page)
        all_results.extend(parsed)
        print(
            f"✅ Found {len(parsed)} results on page {page_num} (total: {len(all_results)})")

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
                    print(f"✅ Reached last page ({current_displayed})")
                    break

            if has_next and next_button:
                is_disabled = (
                    "disabled" in (next_button.get_attribute(
                        "class") or "").lower()
                    or (next_button.get_attribute("aria-disabled") or "").lower() == "true"
                )
                if is_disabled:
                    print("✅ Reached last page (next button disabled)")
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
                print(f"✅ Navigated to page {page_num}")
            else:
                print("✅ No more pages")
                break
        except Exception as e:
            print(f"⚠️ Pagination error: {e}")
            break

    return all_results


# ---------------------------------------------------------------------------
# Detail page extraction (single visit)
# ---------------------------------------------------------------------------

_EMPTY_AUTUACAO = {"process": "", "type": "",
                   "registration_date": "", "interessados": ""}


def extract_detail_page(
    page, context, url: str, skip_certidao_transitada: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Open the detail page ONCE and extract everything:
      - Autuação info (process, type, registration_date, interessados)
      - tblDocumentos records
      - tblHistorico records

    Returns a dict with keys: autuacao, table_records, historico_records.
    Returns None when skip_certidao_transitada triggers.
    """
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
        _log_error_and_email(
            f"Failed to extract detail page {url}: {e}",
            {"url": url, "step": "extract_detail_page"},
        )
        if detail_page:
            try:
                detail_page.close()
            except Exception:
                pass
        return {
            "autuacao": dict(_EMPTY_AUTUACAO),
            "table_records": [],
            "historico_records": [],
        }


# ---------------------------------------------------------------------------
# LLM deal matching (ACCC pattern)
# ---------------------------------------------------------------------------

def match_case_to_deal(interessados_text: str, translated_text: str) -> Optional[str]:
    """
    Use LLM to match interessados text against deals.
    Returns deal_id or None.
    """
    try:
        deals_collection = get_deals_collection()
        if deals_collection is None:
            return None

        status_filter = {
            "$or": [
                {"deal_status": {"$in": ["Open", "Unknown"]}},
                {"deal_status": None},
                {"deal_status": {"$exists": False}},
            ]
        }
        deals = list(deals_collection.find(status_filter))
        if not deals:
            return None

        lines = []
        for d in deals:
            deal_id = str(d.get("_id"))
            target = d.get("target") or d.get("target_name", "N/A")
            acquirer = d.get("acquirer") or d.get("acquire_name", "N/A")
            line = f"Deal ID: {deal_id} | Target: {target} | Acquirer: {acquirer}"
            target_aliases = d.get("target_aliases") or []
            parent_aliases = d.get("parent_aliases") or []
            if target_aliases:
                line += f" | Target aliases: {', '.join(str(a) for a in target_aliases)}"
            if parent_aliases:
                line += f" | Parent aliases: {', '.join(str(a) for a in parent_aliases)}"
            lines.append(line)

        deals_text = "\n".join(lines)

        prompt = f"""You are an expert M&A deal matcher. Determine whether this CADE Brazil case directly refers to a specific deal in our deals database.

DEALS DATABASE:
{deals_text}

INTERESSADOS TEXT (translated to English):
{translated_text}

ORIGINAL TEXT (Portuguese):
{interessados_text}

MATCHING INSTRUCTIONS:
1. Extract only the company names that are explicitly and directly mentioned from the interessados text.
2. Ignore indirect relevance, industry overlap, market similarity, inferred relationships, competitors, customers, regulators, service providers, or any company not actually written in the interessados text.
3. For each deal in the deals database, check whether:
   - the Acquirer (or its known alias), AND
   - the Target (or its known alias)
   are both directly mentioned in the interessados text.
4. A deal is a valid match only if BOTH sides of the same deal are confidently matched from the interessados text:
   - one match for the Acquirer side
   - one match for the Target side
5. Do not return a match if only one side is present, even if that single company is an exact match.
6. Allow only normal name variations when they clearly refer to the same company, such as:
   - punctuation differences
   - “Inc.” vs “Incorporated”
   - “Corp.” vs “Corporation”
   - “Ltd” vs “Limited”
   - obvious spacing/casing differences
7. Do not match based only on sector, business type, article topic, indirect association, or partial deal overlap.
8. If the interessados text does not directly name both companies for the same deal, return None.


RESPONSE FORMAT:
-If BOTH the Acquirer and Target for one deal are directly matched, respond EXACTLY: Match: DEAL_ID
-If no deal satisfies this rule, respond exactly: None
"""

        res = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert M&A deal identifier for Brazilian regulatory notices.",
                },
                {"role": "user", "content": prompt},
            ],
        )

        content = (res.choices[0].message.content or "").strip()
        tokens_used = getattr(res.usage, "total_tokens",
                              "N/A") if res.usage else "N/A"
        logger.info(
            f"  LLM match raw response: {content} (tokens={tokens_used})")

        if not content.lower().startswith("match"):
            logger.info(f"  LLM match result: None (no match prefix)")
            return None

        try:
            _prefix, deal_id_raw = content.split(":", 1)
            deal_id = deal_id_raw.strip()
            logger.info(f"  LLM match result: deal_id={deal_id}")
            return deal_id or None
        except Exception:
            logger.warning(f"  LLM match result: malformed response")
            return None
    except Exception as e:
        _log_error_and_email(
            f"LLM deal match error: {e}",
            {"interessados": interessados_text[:100],
                "step": "match_case_to_deal"},
        )
        return None


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _post_email_payload(payload: Dict[str, Any]) -> bool:
    logger.info(f"  Sending email: {payload.get('subject', 'N/A')}")
    try:
        resp = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"  Email sent successfully (status={resp.status_code})")
        return True
    except Exception as e:
        _log_error_and_email(
            f"Error sending email via webhook: {e}",
            {"subject": payload.get("subject", "N/A"),
             "step": "_post_email_payload"},
        )
        return False


def send_matched_email(case_data: Dict[str, Any], deal_id: str) -> bool:
    process = case_data.get("process", "N/A")
    interessados = case_data.get(
        "interessados_en") or case_data.get("interessados", "N/A")
    case_type = case_data.get("type_en") or case_data.get("type", "N/A")
    reg_date = case_data.get("registration_date", "N/A")
    detail_url = case_data.get("detail_url", "")
    table_records = case_data.get("table_records", [])

    subject = f"[FRMD] CADE Brazil (New) – {process}"

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

    subject = f"[FRUD] CADE Brazil (USA-Related) – {process}"

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
    run_start = time.time()
    error_count = 0
    mode_label = "TEST MODE" if test_mode else "LIVE MODE"

    logger.info("=" * 60)
    logger.info(f"Starting CADE Cases Register ({mode_label})")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info("=" * 60)
    print(f"🚀 Starting CADE Cases Register scraper ({mode_label})\n")

    # MongoDB
    print("🔌 Initializing MongoDB connection...")
    ok, msg = init_mongodb_connection(ENV_PATH)
    if not ok:
        _log_error_and_email(f"MongoDB connection failed: {msg}", {
                             "step": "mongodb_connect"})
        return
    print(f"✅ {msg}\n")

    if not is_connected():
        _log_error_and_email("MongoDB not connected after init", {
                             "step": "mongodb_connect"})
        return

    collection = get_brazil_cases_collection()
    if collection is None:
        _log_error_and_email("Could not access 'brazil_cases' collection", {
                             "step": "get_collection"})
        return

    # Default date range
    if end_date is None:
        end_date = datetime.datetime.now()
    if start_date is None:
        start_date = end_date - datetime.timedelta(days=2)

    new_cases: List[Dict[str, Any]] = []

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
            print(
                f"🔍 Searching {start_date.strftime('%Y-%m-%d')} → "
                f"{end_date.strftime('%Y-%m-%d')} "
                f"across {len(PROCESS_TYPES)} process types\n"
            )

            # Run one search per process type, collect & deduplicate
            all_results: List[Dict[str, Any]] = []
            seen_urls: set = set()

            for type_name, type_id in PROCESS_TYPES.items():
                print(f"\n{'='*60}")
                print(f"📋 Searching: {type_name} (id={type_id})")
                print(f"{'='*60}")

                page.goto(BASE_URL, wait_until="domcontentloaded",
                          timeout=30000)
                time.sleep(3)

                if not submit_search_form(page, start_date, end_date, process_type_id=type_id):
                    print(f"❌ Failed to submit form for {type_name}")
                    continue

                type_results = collect_all_pages(page)
                print(f"✅ Found {len(type_results)} results for {type_name}")

                added = 0
                for r in type_results:
                    url = r.get("detail_url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        r["process_type_filter"] = type_name
                        all_results.append(r)
                        added += 1
                print(
                    f"   ➕ {added} new (after dedup), {len(type_results) - added} duplicates skipped")

            print(
                f"\n✅ Total collected (deduplicated): {len(all_results)} results")

            # Step 3–7: Process each record
            for idx, result in enumerate(all_results, 1):
                detail_url = result.get("detail_url")
                title = result.get("title", "N/A")

                print(f"\n[{idx}/{len(all_results)}] {title[:80]}...")

                if not detail_url:
                    print("  ⚠️ No detail URL; skipping")
                    continue

                if not test_mode and case_exists_by_url(collection, detail_url):
                    print("  ⏩ Already in brazil_cases; skipping")
                    continue

                # Single page visit: autuação + tblDocumentos + tblHistorico
                detail = extract_detail_page(
                    page, context, detail_url,
                    skip_certidao_transitada=skip_certidao_transitada,
                )

                if detail is None:
                    continue

                autuacao = detail["autuacao"]
                table_data = detail["table_records"]
                historico_data = detail["historico_records"]

                interessados_text = autuacao.get("interessados", "").strip()

                translated = ""
                if interessados_text:
                    translated = translate_to_english(interessados_text)
                    print(f"  🌐 Translated: {translated[:150]}...")

                type_en = ""
                if autuacao.get("type"):
                    type_en = translate_to_english(autuacao["type"])

                now_iso = utc_now_iso()
                case_doc: Dict[str, Any] = {
                    "process": autuacao.get("process", ""),
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

                # Translate table_records tipo_documento only
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

                # LLM deal matching (after extraction so skipped records don't waste an API call)
                matched_deal_id = None
                if interessados_text:
                    try:
                        matched_deal_id = match_case_to_deal(
                            interessados_text, translated)
                    except Exception as e:
                        print(f"  ⚠️ Error during deal matching: {e}")

                if matched_deal_id:
                    print(f"  🎯 Deal match found (deal_id={matched_deal_id})")
                    case_doc["deal_id"] = matched_deal_id

                    if not test_mode:
                        send_matched_email(case_doc, matched_deal_id)
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
                            print(f"  ⚠️ Error verifying USA relation: {e}")

                    if is_usa:
                        print("  🇺🇸 USA-related (unmatched) — sending email")
                        if not test_mode:
                            send_usa_related_email(case_doc)
                    else:
                        print("  ℹ️ No match, not USA-related — saving record only")

                inserted_id = insert_case(collection, case_doc)
                if inserted_id:
                    print(f"  ✅ Inserted into brazil_cases (id={inserted_id})")
                    backup = dict(case_doc)
                    backup.pop("_id", None)
                    new_cases.append(backup)
                else:
                    print("  ⚠️ Insert failed")

        except Exception as e:
            error_count += 1
            _log_error_and_email(
                f"Unhandled error in main execution: {e}",
                {"step": "run_main"},
            )
        finally:
            browser.close()
            logger.info("Browser closed")

    # Backup JSON
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
            print(f"\n💾 Saved {len(serializable)} cases to {BACKUP_JSON}")
        except Exception as e:
            print(f"⚠️ Error writing backup JSON: {e}")

    elapsed = round(time.time() - run_start, 1)
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  New cases inserted           : {len(new_cases)}")
    logger.info(f"  Errors encountered           : {error_count}")
    logger.info(f"  Total time                   : {elapsed}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    env_flag = os.getenv("CADE_CASES_TEST_MODE", "").lower()
    test_mode_env = env_flag in ("1", "true", "yes", "y")
    run_cade_cases_register(test_mode=False, skip_certidao_transitada=True)

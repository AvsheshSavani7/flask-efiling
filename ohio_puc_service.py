"""
Ohio PUC document fetcher — WAF bypass + native reCAPTCHA via real Chrome (CDP).

TEST-ONLY API: no email sending, no MongoDB writes, no background jobs.
Endpoints return fetch results directly in the HTTP response.

Architecture
------------
Phase 1 (Playwright + residential proxy):
  Homepage → POR → DI → DocumentRecord
  Collects session cookies and ViewImage URL.

Phase 2 (real Chrome via CDP):
  Import cookies into the same browser context that will load ViewImage.
  Let the page call grecaptcha.enterprise.execute() and form.submit() natively.
  Capture the PDF from the response or download event.

No CAPTCHA solver tokens are injected.

Server setup
------------
1. Start Chrome with remote debugging (see start_chrome_cdp() or POST /ohio-puc/chrome/start):

   google-chrome \\
     --remote-debugging-port=9222 \\
     --user-data-dir=/tmp/oh-puc-chrome \\
     --no-first-run --no-default-browser-check \\
     --disable-blink-features=AutomationControlled \\
     --proxy-server=http://HOST:PORT \\
     --proxy-bypass-list="*.google.com;*.gstatic.com;*.googleapis.com;*.recaptcha.net"

2. Set proxy env vars (OH_PUC_PROXY_*) and call the API.

On Linux headless servers use Xvfb:
  xvfb-run -a python app.py
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import platform
import random
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth.stealth import Stealth

logger = logging.getLogger(__name__)

BASE_URL = "https://dis.puc.state.oh.us"
RECAPTCHA_SITE_KEY = "6LeXf3UpAAAAAELDBvGkol8Iom9gTKwG-pIkxuL9"

PROXY_BYPASS = (
    "*.google.com,*.gstatic.com,*.googleapis.com,*.googletagmanager.com,"
    "*.google-analytics.com,*.visualstudio.com,*.recaptcha.net"
)
PROXY_BYPASS_CHROMIUM = PROXY_BYPASS.replace(",", ";")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_PROXY = {
    "server": os.environ.get(
        "OH_PUC_PROXY_SERVER", "http://thehub.proxy-cheap.com:8080"
    ),
    "username": os.environ.get("OH_PUC_PROXY_USERNAME", "OAvucx71Nfse7sA"),
    "password": os.environ.get(
        "OH_PUC_PROXY_PASSWORD", "MMnZtnp9g1VNyIL_country-US"
    ),
    "bypass": PROXY_BYPASS,
}

CDP_URL = os.environ.get("OH_PUC_CDP_URL", "http://127.0.0.1:9222")
CDP_PORT = int(os.environ.get("OH_PUC_CDP_PORT", "9222"))
CHROME_USER_DATA_DIR = os.environ.get(
    "OH_PUC_CHROME_USER_DATA_DIR", "/tmp/oh-puc-chrome"
)
VIEWIMAGE_TIMEOUT_SEC = int(os.environ.get("OH_PUC_VIEWIMAGE_TIMEOUT", "120"))
MAX_WAF_RETRIES = int(os.environ.get("OH_PUC_MAX_WAF_RETRIES", "15"))
WAF_RETRY_DELAY = int(os.environ.get("OH_PUC_WAF_RETRY_DELAY", "5"))

_chrome_proc: Optional[subprocess.Popen] = None


@dataclass
class NavigationResult:
    cookies: list[dict]
    view_url: str
    doc_record_url: str
    doc_record_html: str = ""
    case_no: str = ""
    doc_id: str = ""
    cmid: str = ""


@dataclass
class PdfResult:
    success: bool
    pdf_bytes: bytes = b""
    view_url: str = ""
    cmid: str = ""
    error: str = ""
    debug: dict = field(default_factory=dict)


def jitter(a: int = 800, b: int = 2000) -> int:
    return random.randint(a, b)


def is_waf_challenge(html: str) -> bool:
    return (
        "bobcmn" in html
        or "TSPD" in html
        or "Request Rejected" in (html or "")
    )


def build_view_url(cmid: str) -> str:
    return f"{BASE_URL}/ViewImage.aspx?CMID={cmid}"


def build_doc_record_url(doc_id: str) -> str:
    return f"{BASE_URL}/DocumentRecord.aspx?DocID={doc_id}"


def _chrome_executable() -> str:
    override = os.environ.get("OH_PUC_CHROME_PATH", "").strip()
    if override:
        return override
    system = platform.system()
    if system == "Darwin":
        return (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        "Chrome not found. Set OH_PUC_CHROME_PATH or install Google Chrome."
    )


def _proxy_server_for_chrome(proxy: dict) -> str:
    server = proxy.get("server", "")
    parsed = urlparse(server if "://" in server else f"http://{server}")
    host = parsed.hostname or ""
    port = parsed.port
    if not host:
        raise ValueError(f"Invalid proxy server: {server}")
    hostport = f"{host}:{port}" if port else host
    user = proxy.get("username", "")
    password = proxy.get("password", "")
    if user and password:
        return f"http://{user}:{password}@{hostport}"
    return hostport


def _cdp_reachable(cdp_url: str = CDP_URL, timeout: float = 1.5) -> bool:
    try:
        parsed = urlparse(cdp_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or CDP_PORT
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def chrome_status(cdp_url: str = CDP_URL) -> dict[str, Any]:
    chrome_path = None
    try:
        chrome_path = _chrome_executable()
        if not (os.path.exists(chrome_path) or shutil.which(chrome_path)):
            chrome_path = None
    except FileNotFoundError:
        pass
    return {
        "cdp_url": cdp_url,
        "reachable": _cdp_reachable(cdp_url),
        "chrome_path": chrome_path,
        "user_data_dir": CHROME_USER_DATA_DIR,
        "proxy_server": DEFAULT_PROXY.get("server"),
    }


def start_chrome_cdp(
    proxy: Optional[dict] = None,
    cdp_port: int = CDP_PORT,
    user_data_dir: str = CHROME_USER_DATA_DIR,
) -> dict[str, Any]:
    """Launch real Chrome with CDP + proxy. Returns status dict."""
    global _chrome_proc

    cdp_url = f"http://127.0.0.1:{cdp_port}"
    if _cdp_reachable(cdp_url):
        return {
            "started": False,
            "already_running": True,
            "cdp_url": cdp_url,
            "message": "Chrome CDP already reachable",
        }

    proxy = proxy or DEFAULT_PROXY
    chrome = _chrome_executable()
    if not os.path.exists(chrome) and not shutil.which(chrome):
        raise FileNotFoundError(f"Chrome executable not found: {chrome}")

    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    proxy_server = _proxy_server_for_chrome(proxy)

    args = [
        chrome,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        f"--proxy-server={proxy_server}",
        f"--proxy-bypass-list={PROXY_BYPASS_CHROMIUM}",
    ]

    env = os.environ.copy()

    _chrome_proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_reachable(cdp_url):
            return {
                "started": True,
                "already_running": False,
                "cdp_url": cdp_url,
                "pid": _chrome_proc.pid,
                "message": "Chrome started with CDP",
            }
        if _chrome_proc.poll() is not None:
            raise RuntimeError("Chrome exited immediately after launch")
        time.sleep(0.5)

    raise RuntimeError("Chrome CDP did not become reachable within 20s")


async def _new_waf_context(browser: Browser) -> BrowserContext:
    context = await browser.new_context(
        user_agent=USER_AGENT,
        ignore_https_errors=True,
        accept_downloads=True,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )
    return context


async def navigate_to_document_record(
    case_no: str,
    doc_id: str,
    cmid: str = "",
    proxy: Optional[dict] = None,
    max_retries: int = MAX_WAF_RETRIES,
) -> NavigationResult:
    """
    WAF-safe navigation to DocumentRecord via proxy Playwright session.
    Returns cookies + ViewImage URL for CDP handoff.
    """
    proxy = proxy or DEFAULT_PROXY
    home_url = f"{BASE_URL}/"
    por_url = f"{BASE_URL}/CaseRecord.aspx?Caseno={case_no}&link=POR"
    di_url = f"{BASE_URL}/CaseRecord.aspx?Caseno={case_no}&link=DI"
    doc_record_url = build_doc_record_url(doc_id)
    view_url = build_view_url(cmid) if cmid else ""

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        f"--proxy-bypass-list={PROXY_BYPASS_CHROMIUM}",
    ]

    async with async_playwright() as pw:
        for attempt in range(1, max_retries + 1):
            logger.info("WAF navigation attempt %s/%s", attempt, max_retries)
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy,
                args=launch_args,
            )
            try:
                context = await _new_waf_context(browser)
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)

                await page.goto(home_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(jitter(1000, 2000))

                await page.goto(por_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_selector("table tr", timeout=12_000)
                except Exception:
                    await page.wait_for_timeout(5_000)
                await page.wait_for_timeout(jitter(1000, 2000))

                if is_waf_challenge(await page.content()):
                    logger.warning("WAF block on POR tab (attempt %s)", attempt)
                    continue

                tab = await page.query_selector("a[href*='link=DI']")
                if tab:
                    await tab.scroll_into_view_if_needed()
                    await page.wait_for_timeout(jitter(300, 700))
                    await tab.click()
                else:
                    await page.goto(di_url, wait_until="domcontentloaded", timeout=60_000)

                try:
                    await page.wait_for_selector("table tr", timeout=15_000)
                except Exception:
                    await page.wait_for_timeout(8_000)

                di_html = await page.content()
                if is_waf_challenge(di_html) or "DocumentRecord" not in di_html:
                    logger.warning("WAF block or empty DI tab (attempt %s)", attempt)
                    continue

                doc_link = await page.query_selector(f"a[href*='{doc_id}']")
                if doc_link:
                    await doc_link.scroll_into_view_if_needed()
                    await page.wait_for_timeout(jitter(300, 600))
                    await doc_link.click()
                else:
                    await page.goto(
                        doc_record_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )

                try:
                    await page.wait_for_selector("a[href*='ViewImage']", timeout=12_000)
                except Exception:
                    await page.wait_for_timeout(5_000)

                doc_html = await page.content()
                if is_waf_challenge(doc_html):
                    logger.warning("WAF block on DocumentRecord (attempt %s)", attempt)
                    continue

                if not view_url:
                    view_link = await page.query_selector("a[href*='ViewImage']")
                    if view_link:
                        href = await view_link.get_attribute("href")
                        if href:
                            view_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                    if not view_url:
                        raise ValueError(
                            "Could not resolve ViewImage URL; provide cmid explicitly."
                        )

                cookies = await context.cookies()
                return NavigationResult(
                    cookies=cookies,
                    view_url=view_url,
                    doc_record_url=doc_record_url,
                    doc_record_html=doc_html,
                    case_no=case_no,
                    doc_id=doc_id,
                    cmid=cmid or _cmid_from_view_url(view_url),
                )
            except Exception as exc:
                logger.warning("Navigation error attempt %s: %s", attempt, exc)
            finally:
                await browser.close()

            if attempt < max_retries:
                await asyncio.sleep(WAF_RETRY_DELAY)

    raise RuntimeError(
        f"WAF navigation failed after {max_retries} attempts with rotating proxy."
    )


def _cmid_from_view_url(view_url: str) -> str:
    from urllib.parse import parse_qs

    qs = parse_qs(urlparse(view_url).query)
    values = qs.get("CMID") or qs.get("cmid") or []
    return values[0] if values else ""


async def _wait_for_pdf_on_page(
    page: Page,
    timeout_sec: int = VIEWIMAGE_TIMEOUT_SEC,
) -> tuple[Optional[bytes], list[str]]:
    captured: list[bytes] = []
    net_log: list[str] = []

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        loc = response.headers.get("location", "")
        entry = f"{response.status} [{ct[:40]}] {response.url[:90]}"
        if loc:
            entry += f" → {loc[:60]}"
        net_log.append(entry)
        if captured:
            return
        try:
            body = await response.body()
            if body and len(body) > 500 and body[:4] == b"%PDF":
                captured.append(body)
                logger.info("PDF captured from response (%s bytes)", len(body))
        except Exception:
            pass

    def on_download(download):
        async def _save():
            try:
                path = Path(f"/tmp/oh_puc_{int(time.time())}.pdf")
                await download.save_as(path)
                data = path.read_bytes()
                path.unlink(missing_ok=True)
                if data[:4] == b"%PDF":
                    captured.append(data)
                    logger.info("PDF captured via download (%s bytes)", len(data))
            except Exception as exc:
                logger.debug("Download handler error: %s", exc)

        asyncio.create_task(_save())

    page.on("response", on_response)
    page.on("download", on_download)

    deadline = time.time() + timeout_sec
    last_url = ""
    while time.time() < deadline:
        if captured:
            return captured[0], net_log

        url = page.url or ""
        if url != last_url:
            logger.info("Page URL → %s", url[:100])
            last_url = url

        if "DISError" in url or "CaptchaValidation=False" in url:
            logger.warning("Server rejected captcha: %s", url[:120])
            break

        # Native reCAPTCHA progress: enterprise.js loads, then execute() fires POST
        try:
            has_enterprise = await page.evaluate(
                "() => !!(window.grecaptcha && window.grecaptcha.enterprise)"
            )
            if has_enterprise:
                net_log.append("grecaptcha.enterprise present")
        except Exception:
            pass

        await page.wait_for_timeout(1500)

    return None, net_log


async def fetch_pdf_cdp_full_session(
    case_no: str,
    doc_id: str,
    cmid: str = "",
    view_url: str = "",
    cdp_url: str = CDP_URL,
    timeout_sec: int = VIEWIMAGE_TIMEOUT_SEC,
    max_retries: int = MAX_WAF_RETRIES,
) -> PdfResult:
    """
    Full flow in one real Chrome session (CDP): WAF navigation + native reCAPTCHA.
    Same browser fingerprint, cookies, and proxy IP for load and submit.
    """
    if not _cdp_reachable(cdp_url):
        return PdfResult(
            success=False,
            error=(
                f"Chrome CDP not reachable at {cdp_url}. "
                "Call POST /ohio-puc/chrome/start first."
            ),
        )

    home_url = f"{BASE_URL}/"
    por_url = f"{BASE_URL}/CaseRecord.aspx?Caseno={case_no}&link=POR"
    di_url = f"{BASE_URL}/CaseRecord.aspx?Caseno={case_no}&link=DI"
    doc_record_url = build_doc_record_url(doc_id)
    resolved_view_url = view_url or (build_view_url(cmid) if cmid else "")
    resolved_cmid = cmid or (_cmid_from_view_url(resolved_view_url) if resolved_view_url else "")

    async with async_playwright() as pw:
        for attempt in range(1, max_retries + 1):
            logger.info("CDP full-session attempt %s/%s", attempt, max_retries)
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            try:
                context = (
                    browser.contexts[0]
                    if browser.contexts
                    else await browser.new_context()
                )
                page = await context.new_page()

                await page.goto(home_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(jitter(1000, 2000))

                await page.goto(por_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_selector("table tr", timeout=12_000)
                except Exception:
                    await page.wait_for_timeout(5_000)
                await page.wait_for_timeout(jitter(1000, 2000))

                if is_waf_challenge(await page.content()):
                    logger.warning("WAF on POR (CDP attempt %s)", attempt)
                    await page.close()
                    if attempt < max_retries:
                        await asyncio.sleep(WAF_RETRY_DELAY)
                    continue

                tab = await page.query_selector("a[href*='link=DI']")
                if tab:
                    await tab.scroll_into_view_if_needed()
                    await page.wait_for_timeout(jitter(300, 700))
                    await tab.click()
                else:
                    await page.goto(di_url, wait_until="domcontentloaded", timeout=60_000)

                try:
                    await page.wait_for_selector("table tr", timeout=15_000)
                except Exception:
                    await page.wait_for_timeout(8_000)

                di_html = await page.content()
                if is_waf_challenge(di_html) or "DocumentRecord" not in di_html:
                    logger.warning("WAF on DI (CDP attempt %s)", attempt)
                    await page.close()
                    if attempt < max_retries:
                        await asyncio.sleep(WAF_RETRY_DELAY)
                    continue

                doc_link = await page.query_selector(f"a[href*='{doc_id}']")
                if doc_link:
                    await doc_link.scroll_into_view_if_needed()
                    await page.wait_for_timeout(jitter(300, 600))
                    await doc_link.click()
                else:
                    await page.goto(
                        doc_record_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )

                try:
                    await page.wait_for_selector("a[href*='ViewImage']", timeout=12_000)
                except Exception:
                    await page.wait_for_timeout(5_000)

                if is_waf_challenge(await page.content()):
                    logger.warning("WAF on DocumentRecord (CDP attempt %s)", attempt)
                    await page.close()
                    if attempt < max_retries:
                        await asyncio.sleep(WAF_RETRY_DELAY)
                    continue

                if not resolved_view_url:
                    view_link_el = await page.query_selector("a[href*='ViewImage']")
                    if view_link_el:
                        href = await view_link_el.get_attribute("href")
                        if href:
                            resolved_view_url = (
                                href
                                if href.startswith("http")
                                else f"{BASE_URL}/{href.lstrip('/')}"
                            )
                            resolved_cmid = _cmid_from_view_url(resolved_view_url)

                view_link = await page.query_selector("a[href*='ViewImage']")
                if view_link:
                    logger.info("Clicking ViewImage — native reCAPTCHA will run")
                    await view_link.click()
                elif resolved_view_url:
                    await page.goto(
                        resolved_view_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                else:
                    return PdfResult(
                        success=False,
                        error="No ViewImage link found; provide cmid or view_url.",
                    )

                pdf_bytes, net_log = await _wait_for_pdf_on_page(page, timeout_sec)
                if pdf_bytes:
                    return PdfResult(
                        success=True,
                        pdf_bytes=pdf_bytes,
                        view_url=resolved_view_url,
                        cmid=resolved_cmid,
                        debug={"network": net_log[-30:], "mode": "cdp_full"},
                    )

                debug_html = ""
                try:
                    debug_html = await page.content()
                except Exception:
                    pass
                return PdfResult(
                    success=False,
                    view_url=resolved_view_url,
                    cmid=resolved_cmid,
                    error="Native reCAPTCHA did not produce PDF within timeout.",
                    debug={
                        "final_url": page.url,
                        "network": net_log[-30:],
                        "page_snippet": debug_html[:500],
                        "mode": "cdp_full",
                    },
                )
            except Exception as exc:
                logger.warning("CDP session error attempt %s: %s", attempt, exc)
                if attempt < max_retries:
                    await asyncio.sleep(WAF_RETRY_DELAY)
            finally:
                await browser.close()

    return PdfResult(
        success=False,
        error=f"CDP full-session failed after {max_retries} attempts.",
    )


async def fetch_pdf_via_cdp(
    nav: NavigationResult,
    cdp_url: str = CDP_URL,
    timeout_sec: int = VIEWIMAGE_TIMEOUT_SEC,
    click_from_doc_record: bool = True,
) -> PdfResult:
    """
    Phase 2: attach to real Chrome, import WAF cookies, load ViewImage untouched.
    Page runs grecaptcha.enterprise.execute() → form.submit() on its own.
    """
    if not _cdp_reachable(cdp_url):
        return PdfResult(
            success=False,
            view_url=nav.view_url,
            cmid=nav.cmid,
            error=(
                f"Chrome CDP not reachable at {cdp_url}. "
                "Start Chrome with --remote-debugging-port or call /ohio-puc/chrome/start."
            ),
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            # Fresh page in real Chrome — no stealth patches, no token hooks
            page = await context.new_page()

            if nav.cookies:
                await context.add_cookies(nav.cookies)

            captured: Optional[bytes] = None
            net_log: list[str] = []

            if click_from_doc_record and nav.doc_id:
                # Natural flow: DocumentRecord → click ViewImage link (same as real user)
                await page.goto(
                    nav.doc_record_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await page.wait_for_timeout(jitter(800, 1500))

                view_link = await page.query_selector("a[href*='ViewImage']")
                if view_link:
                    logger.info("Clicking ViewImage link from DocumentRecord")
                    await view_link.click()
                else:
                    logger.info("ViewImage link not found; navigating directly")
                    await page.goto(
                        nav.view_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
            else:
                await page.goto(
                    nav.view_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

            captured, net_log = await _wait_for_pdf_on_page(page, timeout_sec)

            if captured:
                return PdfResult(
                    success=True,
                    pdf_bytes=captured,
                    view_url=nav.view_url,
                    cmid=nav.cmid,
                    debug={"network": net_log[-30:]},
                )

            debug_html = ""
            try:
                debug_html = await page.content()
            except Exception:
                pass

            return PdfResult(
                success=False,
                view_url=nav.view_url,
                cmid=nav.cmid,
                error="Native reCAPTCHA did not produce a PDF within timeout.",
                debug={
                    "final_url": page.url,
                    "network": net_log[-30:],
                    "page_snippet": debug_html[:500],
                },
            )
        finally:
            await browser.close()


async def fetch_ohio_puc_pdf_async(
    case_no: str,
    doc_id: str,
    cmid: str = "",
    view_url: str = "",
    cdp_url: str = CDP_URL,
    timeout_sec: int = VIEWIMAGE_TIMEOUT_SEC,
    max_waf_retries: int = MAX_WAF_RETRIES,
    auto_start_chrome: bool = True,
    mode: str = "cdp_full",
) -> PdfResult:
    """
    Full pipeline for Ohio PUC PDF.

    mode:
      - cdp_full (default): entire flow in real Chrome via CDP — same session/IP/fingerprint
      - hybrid: Playwright proxy WAF nav, then cookie handoff to CDP Chrome
    """
    if cdp_url is None:
        cdp_url = CDP_URL

    if auto_start_chrome and not _cdp_reachable(cdp_url):
        try:
            start_chrome_cdp()
        except Exception as exc:
            logger.warning("Could not auto-start Chrome: %s", exc)

    if mode == "hybrid":
        if view_url and not cmid:
            cmid = _cmid_from_view_url(view_url)
        nav = await navigate_to_document_record(
            case_no=case_no,
            doc_id=doc_id,
            cmid=cmid,
            max_retries=max_waf_retries,
        )
        if view_url:
            nav.view_url = view_url
        return await fetch_pdf_via_cdp(nav, cdp_url=cdp_url, timeout_sec=timeout_sec)

    return await fetch_pdf_cdp_full_session(
        case_no=case_no,
        doc_id=doc_id,
        cmid=cmid,
        view_url=view_url,
        cdp_url=cdp_url,
        timeout_sec=timeout_sec,
        max_retries=max_waf_retries,
    )


async def fetch_doc_record_html_async(
    case_no: str,
    doc_id: str,
    cmid: str = "",
    max_waf_retries: int = MAX_WAF_RETRIES,
) -> dict[str, Any]:
    nav = await navigate_to_document_record(
        case_no=case_no,
        doc_id=doc_id,
        cmid=cmid,
        max_retries=max_waf_retries,
    )
    return {
        "success": True,
        "case_no": nav.case_no,
        "doc_id": nav.doc_id,
        "cmid": nav.cmid,
        "view_url": nav.view_url,
        "doc_record_url": nav.doc_record_url,
        "html": nav.doc_record_html,
        "html_length": len(nav.doc_record_html),
        "cookie_count": len(nav.cookies),
    }


def fetch_ohio_puc_pdf(
    case_no: str,
    doc_id: str,
    cmid: str = "",
    view_url: str = "",
    cdp_url: str = CDP_URL,
    timeout_sec: int = VIEWIMAGE_TIMEOUT_SEC,
    max_waf_retries: int = MAX_WAF_RETRIES,
    auto_start_chrome: bool = True,
    mode: str = "cdp_full",
) -> PdfResult:
    return asyncio.run(
        fetch_ohio_puc_pdf_async(
            case_no=case_no,
            doc_id=doc_id,
            cmid=cmid,
            view_url=view_url,
            cdp_url=cdp_url or CDP_URL,
            timeout_sec=timeout_sec,
            max_waf_retries=max_waf_retries,
            auto_start_chrome=auto_start_chrome,
            mode=mode,
        )
    )


def fetch_doc_record_html(
    case_no: str,
    doc_id: str,
    cmid: str = "",
    max_waf_retries: int = MAX_WAF_RETRIES,
) -> dict[str, Any]:
    return asyncio.run(
        fetch_doc_record_html_async(
            case_no=case_no,
            doc_id=doc_id,
            cmid=cmid,
            max_waf_retries=max_waf_retries,
        )
    )


def pdf_result_to_api_dict(result: PdfResult, as_base64: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": result.success,
        "view_url": result.view_url,
        "cmid": result.cmid,
        "error": result.error,
        "debug": result.debug,
    }
    if result.success and result.pdf_bytes:
        payload["pdf_size"] = len(result.pdf_bytes)
        if as_base64:
            payload["pdf_base64"] = base64.b64encode(result.pdf_bytes).decode("ascii")
    return payload

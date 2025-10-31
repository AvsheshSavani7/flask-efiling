import asyncio
import requests
import time
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# 2Captcha API Key
API_KEY = "1cc2815b0dfbc0b37d0218bc5f4325d1"

CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"


async def get_sitekey(page):
    """Extract Cloudflare Turnstile sitekey from page"""
    content = await page.content()
    soup = BeautifulSoup(content, "html.parser")
    sitekey = None
    iframe = soup.find(
        "iframe", src=lambda x: x and "challenges.cloudflare.com/cdn-cgi/challenge-platform" in x)
    if iframe and "sitekey=" in iframe["src"]:
        import urllib.parse
        q = urllib.parse.parse_qs(iframe["src"].split("?", 1)[1])
        sitekey = q.get("sitekey", [None])[0]
    if not sitekey:
        inp = soup.find("input", {"name": "cf-turnstile-sitekey"})
        if inp:
            sitekey = inp["value"]
    if not sitekey:
        div = soup.find("div", {"data-sitekey": True})
        if div:
            sitekey = div.get("data-sitekey")
    return sitekey


async def fetch_no_proxy_with_stealth(url, wait_time=20):
    """
    Fetch URL using Playwright without proxy, but with stealth mode and better headers.
    This attempts to bypass 403 errors by appearing more like a real browser.
    """
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--disable-gpu"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
        )

        page = await context.new_page()

        try:
            await page.goto(url, timeout=120_000, wait_until="domcontentloaded")
            await asyncio.sleep(wait_time)

            # Check for Cloudflare challenge
            sitekey = await get_sitekey(page)
            if sitekey:
                print(f"Cloudflare Turnstile detected, solving with 2Captcha...")
                token = await solve_turnstile_captcha(url, sitekey)
                if token:
                    print("Injecting token and submitting form...")
                    # Inject the token into the form
                    await page.evaluate("""
                        (token) => {
                            let input = document.querySelector('input[name="cf-turnstile-response"]');
                            if (input) {
                                input.value = token;
                            }
                        }
                    """, token)

                    # This is a download URL, so we need to handle the download event
                    print("Submitting form and waiting for download...")
                    download_path = None

                    # Try to capture the download
                    try:
                        async with page.expect_download(timeout=60_000) as download_info:
                            await page.evaluate("""
                                () => {
                                    let form = document.querySelector('form#turnstile-form') || document.querySelector('form');
                                    if (form) {
                                        form.submit();
                                    }
                                }
                            """)

                        download = await download_info.value
                        print(
                            f"Download started: {download.suggested_filename}")

                        # Save the download to a temporary location
                        download_path = f"/tmp/{download.suggested_filename}"
                        await download.save_as(download_path)
                        print(f"Download saved to: {download_path}")

                        # Read the downloaded file
                        with open(download_path, 'rb') as f:
                            download_content = f.read()

                        await browser.close()

                        # Try to detect if it's a PDF and extract text
                        if download_path.endswith('.pdf') or download_content.startswith(b'%PDF'):
                            print("Detected PDF file, returning binary content")
                            # Return binary content that will be processed by extract_text_from_document
                            return download_content
                        else:
                            # Return as text
                            return download_content.decode('utf-8', errors='ignore')

                    except PlaywrightTimeoutError:
                        print("Download timeout - no download was triggered")
                    except Exception as e:
                        print(f"Download capture error: {str(e)}")

                    # If download didn't work, try alternative approach
                    print(
                        "Trying alternative: Make authenticated request to download URL...")
                    await asyncio.sleep(5)

                    # Check current page and try navigating again with the authenticated session
                    current_content = await page.content()
                    if "Security check" not in current_content:
                        print("Successfully bypassed security check!")

            html = await page.content()
            await browser.close()
            return html

        except PlaywrightTimeoutError:
            print("Timed out waiting for page load. Returning what loaded.")
            html = await page.content()
            await browser.close()
            return html
        except Exception as e:
            print(f"Error fetching page: {str(e)}")
            await browser.close()
            return ""


async def fetch_with_residential_proxy(url, wait_time=20):
    """
    Fetch URL using public/free proxy services.
    You can replace this with your own proxy service.
    """
    # Example: Using free proxy list or your own proxy service
    # For now, this is a placeholder - you'll need to add actual proxy credentials
    print("Note: This function needs proxy configuration. Using direct connection for now.")
    return await fetch_no_proxy_with_stealth(url, wait_time)


async def solve_turnstile_captcha(url, sitekey):
    """Solve Cloudflare Turnstile using 2Captcha"""
    data = {
        "key": API_KEY,
        "method": "turnstile",
        "sitekey": sitekey,
        "pageurl": url,
        "json": 1,
    }

    try:
        resp = requests.post(CAPTCHA_SOLVER_URL, data=data, timeout=30)
        result = resp.json()
        task_id = result.get("request")

        if not task_id:
            print("2Captcha task creation failed:", resp.text)
            return None

        print(f"2Captcha task id: {task_id} - waiting for solution...")

        # Poll for solution (max 3 minutes)
        for _ in range(36):
            time.sleep(5)
            params = {
                "key": API_KEY,
                "action": "get",
                "id": task_id,
                "json": 1,
            }
            r = requests.get(CAPTCHA_RESULT_URL, params=params, timeout=30)
            result = r.json()

            if result.get("status") == 1:
                token = result.get("request")
                print(f"Captcha solved successfully!")
                return token
            elif result.get("request") != "CAPCHA_NOT_READY":
                print("2Captcha error:", result)
                return None

        print("Captcha was not solved in time.")
        return None

    except Exception as e:
        print(f"Error solving captcha: {str(e)}")
        return None


async def fetch_simple_requests(url):
    """
    Simple requests-based fetch with good headers (no JavaScript support).
    Works for non-protected pages but won't work if JavaScript is required.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        response = requests.get(url, headers=headers,
                                timeout=30, allow_redirects=True)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f"Simple requests failed: {str(e)}")
        return ""


def fetch_with_playwright_no_proxy(url, wait_time=20):
    """
    Synchronous wrapper for fetch_no_proxy_with_stealth.
    This is a drop-in replacement for fetch_with_playwright_2captcha from demo4.py
    """
    try:
        return asyncio.run(fetch_no_proxy_with_stealth(url, wait_time))
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(fetch_no_proxy_with_stealth(url, wait_time))


def fetch_multi_strategy(url, wait_time=20):
    """
    Try multiple strategies in order:
    1. Simple requests (fastest, but often blocked)
    2. Playwright with stealth mode (more reliable)
    """
    print(f"Strategy 1: Trying simple requests...")
    result = asyncio.run(fetch_simple_requests(url))

    # Check if we got blocked (403, 503, Cloudflare page, etc.)
    if result and "403" not in result and "Cloudflare" not in result and len(result) > 1000:
        print("Simple requests succeeded!")
        return result

    print(f"Strategy 2: Trying Playwright with stealth mode...")
    return fetch_with_playwright_no_proxy(url, wait_time)


if __name__ == "__main__":
    # Test the function
    TEST_URL = "https://efiling.web.commerce.state.mn.us/documents/%7BD0394096-0000-CB1F-B555-13FACCA89806%7D/download?contentSequence=0&rowIndex=376"

    print("Testing fetch function...")
    content = fetch_with_playwright_no_proxy(TEST_URL, wait_time=15)

    if content:
        print(f"Successfully fetched {len(content)} bytes/characters")

        # Save as binary if it's bytes, otherwise as text
        if isinstance(content, bytes):
            # It's a PDF, try to extract text from it
            from mn_doc_scraper import extract_text_from_document
            text = extract_text_from_document(content, "pdf")
            print(f"Extracted {len(text)} characters of text from PDF")
            with open("test_fetch.txt", "w", encoding="utf-8") as f:
                f.write(text)
            print("Saved extracted text to test_fetch.txt")
            # Also save the raw PDF
            with open("test_fetch.pdf", "wb") as f:
                f.write(content)
            print("Saved raw PDF to test_fetch.pdf")
        else:
            # It's HTML or text
            with open("test_fetch.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("Saved to test_fetch.html")
    else:
        print("Failed to fetch content")

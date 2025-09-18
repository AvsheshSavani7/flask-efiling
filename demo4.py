import asyncio
import requests
import time
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

TARGET_URL = "https://ipinfo.io/ip"
API_KEY = "1cc2815b0dfbc0b37d0218bc5f4325d1"

proxy_host = "95.135.111.60"
proxy_port = 45237
proxy_username = "oEhXl624nXDSDBR"
proxy_password = "YC7SFyimYnBQLbt"


CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"


async def get_sitekey(page):
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


async def playwright_2captcha_fetch(url, wait_time=15):
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--ignore-certificate-errors",
                f"--proxy-server=http://{proxy_host}:{proxy_port}"
            ]
        )
        context = await browser.new_context(
            proxy={
                "server": f"http://{proxy_host}:{proxy_port}",
                "username": proxy_username,
                "password": proxy_password
            },
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }

        )
        page = await context.new_page()
        try:
            await page.goto(url, timeout=120_000)
        except PlaywrightTimeoutError:
            print("Timed out waiting for initial page load. Saving what loaded.")
            html = await page.content()
            with open("debug_initial_timeout.html", "w", encoding="utf-8") as f:
                f.write(html)
            await browser.close()
            return html
        await asyncio.sleep(5)

        sitekey = await get_sitekey(page)
        if not sitekey:
            print("No Turnstile found. Returning current page.")
            await asyncio.sleep(20)
            html = await page.content()
            await browser.close()
            return html

        print(f"Turnstile sitekey found: {sitekey}")

        # 2Captcha submission
        data = {
            "key": API_KEY,
            "method": "turnstile",
            "sitekey": sitekey,
            "pageurl": url,
            "json": 1,
        }
        resp = requests.post(CAPTCHA_SOLVER_URL, data=data)
        task_id = resp.json().get("request")
        if not task_id:
            print("2Captcha task creation failed:", resp.text)
            await browser.close()
            return ""

        print(f"2Captcha task id: {task_id} - waiting for solution...")
        token = None
        for _ in range(30):
            time.sleep(5)
            params = {
                "key": API_KEY,
                "action": "get",
                "id": task_id,
                "json": 1,
            }
            r = requests.get(CAPTCHA_RESULT_URL, params=params)
            result = r.json()
            if result["status"] == 1:
                token = result["request"]
                break
            elif result["request"] != "CAPCHA_NOT_READY":
                print("2Captcha error:", result)
                break
            print("Waiting for captcha solution ...")
        if not token:
            print("Captcha was not solved in time.")
            await browser.close()
            return ""

        print(f"Captcha solved: {token[:8]}... (truncated)")

        # DEBUG: Save HTML just before submitting the form
        html_before = await page.content()
        with open("debug_before_submit.html", "w", encoding="utf-8") as f:
            f.write(html_before)

        # 3. Inject token into correct form and submit
        await page.evaluate("""
            (token) => {
                let input = document.querySelector('input[name="cf-turnstile-response"]');
                if (!input) {
                    input = document.createElement('input');
                    input.type = "hidden";
                    input.name = "cf-turnstile-response";
                    document.body.appendChild(input);
                }
                input.value = token;
                let form = input.form || document.querySelector('form');
                if (form) {
                    form.submit();
                } else {
                    location.reload();
                }
            }
        """, token)

        # 4. Wait for next page
        try:
            await page.wait_for_load_state('networkidle', timeout=50000)
        except Exception:
            print("Waiting extra for challenge navigation...")
            await asyncio.sleep(10)

        for _ in range(10):
            if not page.is_closed():
                try:
                    html_after = await page.content()
                    break
                except Exception:
                    await asyncio.sleep(2)
            else:
                return ""
        with open("debug_after_submit.html", "w", encoding="utf-8") as f:
            f.write(html_after)

        print("Saving HTML ...")
        await browser.close()
        return html_after


def fetch_with_playwright_2captcha(url, wait_time=15):
    try:
        return asyncio.run(playwright_2captcha_fetch(url, wait_time))
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(playwright_2captcha_fetch(url, wait_time))


if __name__ == "__main__":
    html = fetch_with_playwright_2captcha(TARGET_URL)
    with open("page.html", "w", encoding="utf-8") as f:
        f.write(html or "")
    print("HTML saved to page.html")

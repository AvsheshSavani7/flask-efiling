import asyncio
import requests
import time
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

API_KEY = "1cc2815b0dfbc0b37d0218bc5f4325d1"  # <-- INSERT YOUR 2Captcha key here

CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"


async def get_sitekey(page):
    # Grab iframe src or <input name="cf-turnstile-sitekey"> or scan for Turnstile widget
    content = await page.content()
    soup = BeautifulSoup(content, "html.parser")
    sitekey = None

    # Try iframe (most common)
    iframe = soup.find(
        "iframe", src=lambda x: x and "challenges.cloudflare.com/cdn-cgi/challenge-platform" in x)
    if iframe and "sitekey=" in iframe["src"]:
        import urllib.parse
        q = urllib.parse.parse_qs(iframe["src"].split("?", 1)[1])
        sitekey = q.get("sitekey", [None])[0]

    # Try hidden input
    if not sitekey:
        inp = soup.find("input", {"name": "cf-turnstile-sitekey"})
        if inp:
            sitekey = inp["value"]

    # Try div[data-sitekey]
    if not sitekey:
        div = soup.find("div", {"data-sitekey": True})
        if div:
            sitekey = div.get("data-sitekey")

    return sitekey


async def playwright_2captcha_fetch(url, wait_time=20):
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(url)
        await asyncio.sleep(3)
        sitekey = await get_sitekey(page)
        if not sitekey:
            await asyncio.sleep(wait_time)
            html = await page.content()
            await browser.close()
            return html
        # 2Captcha logic same as before...
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
            await browser.close()
            return ""
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
                break
        if not token:
            await browser.close()
            return ""
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
        try:
            await page.wait_for_load_state('networkidle', timeout=20000)
        except Exception:
            await asyncio.sleep(8)
        for _ in range(10):
            if not page.is_closed():
                try:
                    html = await page.content()
                    break
                except Exception:
                    await asyncio.sleep(2)
            else:
                return ""
        await browser.close()
        return html


def fetch_with_playwright_2captcha(url, wait_time=20):
    try:
        return asyncio.run(playwright_2captcha_fetch(url, wait_time))
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(playwright_2captcha_fetch(url, wait_time))


if __name__ == "__main__":
    html = fetch_with_playwright_2captcha(
        "https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198")
    with open("page.html", "w", encoding="utf-8") as f:
        f.write(html or "")
    print("HTML saved to page.html")

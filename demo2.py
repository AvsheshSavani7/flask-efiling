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


async def main(wait_time=20, url="https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198"):
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print(f"Navigating to {url} ...")
        await page.goto(url)
        await asyncio.sleep(5)  # Let page render

        # Detect if Turnstile is present
        print("Detecting Turnstile sitekey ...")
        sitekey = await get_sitekey(page)
        if not sitekey:
            print(
                "No Turnstile sitekey found. Page may not be protected or site structure changed.")
            html = await page.content()
            with open("page.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("Page HTML saved as page.html")
            await browser.close()
            return

        print(f"Turnstile sitekey found: {sitekey}")

        # Submit captcha challenge to 2Captcha
        print("Submitting to 2Captcha...")
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
            return

        print(
            f"2Captcha task id: {task_id} -- waiting for solution (may take 10-60 sec)...")
        # Poll for result
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
            return

        print(f"Captcha solved: {token[:8]}... (truncated)")

        # Inject the solution token and trigger form submit
        print("Injecting solution and submitting ...")
        # The form selector may need to be adjusted for your page.
        await page.evaluate("""
        (token) => {
            // Try to set cf-turnstile-response
            let input = document.querySelector('input[name="cf-turnstile-response"]');
            if (!input) {
                input = document.createElement('input');
                input.type = "hidden";
                input.name = "cf-turnstile-response";
                document.body.appendChild(input);
            }
            input.value = token;
            // Try to find and submit a form
            let form = input.form || document.querySelector('form');
            if (form) {
                form.submit();
            } else {
                // If no form, reload with token
                location.reload();
            }
        }
        """, token)

       # Wait for navigation or the challenge to finish
        try:
            await page.wait_for_load_state('networkidle', timeout=20000)
        except Exception:
            print("Still navigating... Waiting a few more seconds just in case.")
            await asyncio.sleep(8)

        # Double-check: Wait until page is not navigating
        for _ in range(10):
            if not page.is_closed():
                try:
                    html = await page.content()
                    break
                except Exception:
                    print("Page still navigating, retrying...")
                    await asyncio.sleep(2)
            else:
                print("Page closed unexpectedly!")
                return
        else:
            print("Failed to get page content after challenge.")
            return

        print("Saving HTML ...")
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Saved final page as page.html")
        await browser.close()
        return html


if __name__ == "__main__":
    asyncio.run(main())

import undetected_chromedriver as uc
import time
import os

# Proxy configuration
PROXY_HOST = "198.23.239.134"
PROXY_PORT = "6540"
PROXY_USER = "czixlqmu"
PROXY_PASS = "y1ywjimq4gos"


def scrape_mn_documents(wait_time=20, url="https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198"):
    """
    Scrape Minnesota e-filing documents for a given docket number.
    """
    options = uc.ChromeOptions()

    # Check if running in Docker/container environment
    if os.environ.get('DISPLAY'):
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1200x600")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-plugins")
        options.add_argument("--disable-images")
        # Don't add --headless, let it use the virtual display
    else:
        options.add_argument("--window-size=1200x600")

    # User agent
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")

    # --- ADD THIS SECTION ---
    # Add proxy with authentication
    proxy_arg = f"--proxy-server=http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    options.add_argument(proxy_arg)
    # ------------------------

    driver = uc.Chrome(options=options)

    try:
        driver.get(url)
        time.sleep(wait_time)
        html = driver.page_source
        return html

    finally:
        driver.quit()


if __name__ == "__main__":
    html_content = scrape_mn_documents()
    with open("page.html", "w", encoding="utf-8") as f:
        f.write(html_content)

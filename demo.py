import requests

# === CONFIGURE THESE ===
# REPLACE with your BrightData endpoint!
BRIGHTDATA_API_ENDPOINT = "https://brightdata.your-api-endpoint.com/web/unblock"
# REPLACE with your actual API key
BRIGHTDATA_API_KEY = "72f792c7d07749f2ddfeebe143409f01bcd5d19bcdc0a33d4b4e9a5fa5ef4064"

TARGET_URL = "https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198"


def fetch_protected_page():
    headers = {
        "Authorization": f"Bearer {BRIGHTDATA_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "url": TARGET_URL,
        "country": "us",  # optional, to use US IPs
        # add other params as per your BrightData API product documentation
    }
    try:
        resp = requests.post(BRIGHTDATA_API_ENDPOINT,
                             headers=headers, json=data, timeout=60)
        resp.raise_for_status()
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        # The HTML after Cloudflare/Turnstile, etc.
        print(resp.text)
    except requests.HTTPError as e:
        print("HTTP error:", e.response.status_code, e.response.text)
    except Exception as ex:
        print("Error:", ex)


if __name__ == "__main__":
    fetch_protected_page()

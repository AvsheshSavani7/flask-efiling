import undetected_chromedriver as uc
import time
import os


def scrape_mn_documents(wait_time=20, url="https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198"):
    """
    Scrape Minnesota e-filing documents for a given docket number.

    Args:
        wait_time (int): Time to wait for page load in seconds
        url (str): The URL to scrape
        wait_time (int): Time to wait for page load in seconds

    Returns:
        str: HTML content of the scraped page
    """
    options = uc.ChromeOptions()

    # Check if running in Docker/container environment
    if os.environ.get('DISPLAY'):
        # Running with virtual display (Docker)
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1200x600")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-plugins")
        options.add_argument("--disable-images")
        # Don't add --headless, let it use the virtual display
    else:
        # Running locally, use normal options
        options.add_argument("--window-size=1200x600")

    # Replace with your real UA if needed
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")

    # Optional: Use a persistent profile for cookie/session reuse (uncomment below if desired)
    # options.user_data_dir = "/tmp/uc-profile"

    # Start undetected Chrome
    driver = uc.Chrome(options=options)

    try:
        # Load the page
        driver.get(url)
        time.sleep(wait_time)

        # Get page HTML
        html = driver.page_source

        # Ensure we have valid HTML content
        if html is None:
            return ""

        return html

    except Exception as e:
        print(f"Error during scraping: {str(e)}")
        return ""
    finally:
        driver.quit()


if __name__ == "__main__":
    # For testing the function directly
    html_content = scrape_mn_documents()
    with open("page.html", "w", encoding="utf-8") as f:
        f.write(html_content)

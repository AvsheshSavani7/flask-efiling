import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import sys
import time
import urllib3

# Disable SSL warnings for requests with verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Proxy configuration (same as puc_scraper.py)
proxy_host = "108.59.242.138"
proxy_port = 46885
proxy_username = "GSenAgrfKhuNWkd"
proxy_password = "8lmVa5yl0pKp9MI"


def fetch_rss_feed(url, max_retries=3, retry_delay=2):
    """
    Fetch RSS feed from the given URL with retry logic using residential proxy
    Returns the XML content as a string
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': '*/*'
    }

    # Always use residential proxy (same as puc_scraper.py)
    proxy_dict = {
        "http": f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}",
        "https": f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
    }

    print(f"Using residential proxy: {proxy_host}:{proxy_port}")

    for attempt in range(max_retries):
        try:
            print(f"Attempt {attempt + 1}/{max_retries}...")
            print(f"Fetching URL through proxy: {url}")
            # Use a fresh connection for each attempt (no session)
            response = requests.get(
                url,
                headers=headers,
                proxies=proxy_dict,
                timeout=(5, 120),  # Longer timeouts: 5s connect, 120s read
                stream=False,
                verify=False,  # Disable SSL verification like puc_scraper.py
                allow_redirects=True
            )
            response.raise_for_status()
            print(f"Successfully fetched {len(response.text)} characters")
            return response.text
        except requests.exceptions.Timeout as e:
            print(f"Timeout error on attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"Failed after {max_retries} attempts due to timeout")
                return None
        except requests.exceptions.ConnectionError as e:
            print(f"Connection error on attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(
                    f"Failed after {max_retries} attempts due to connection error")
                return None
        except requests.exceptions.RequestException as e:
            print(f"Request error on attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"Failed after {max_retries} attempts")
                return None
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            return None

    return None


def parse_rss_items(xml_content):
    """
    Parse RSS XML and extract items
    Returns a list of dictionaries containing item data
    """
    try:
        root = ET.fromstring(xml_content)

        # RSS feeds typically have items in <item> tags
        # Handle both RSS 2.0 and Atom feeds
        items = []

        # Try RSS 2.0 format first (items in channel/item)
        channel = root.find('channel')
        if channel is not None:
            for item in channel.findall('item'):
                item_dict = {}
                for child in item:
                    tag = child.tag
                    # Remove namespace if present (e.g., '{http://...}title' -> 'title')
                    if '}' in tag:
                        tag = tag.split('}')[1]

                    text = child.text if child.text else ''

                    # Handle nested elements (like enclosure, category, etc.)
                    if len(child) > 0:
                        # For elements with children, store as dict
                        item_dict[tag] = {subchild.tag.split(
                            '}')[-1]: subchild.text for subchild in child}
                    else:
                        item_dict[tag] = text

                items.append(item_dict)
        else:
            # Try Atom format (entries)
            for entry in root.findall('{http://www.w3.org/2005/Atom}entry'):
                item_dict = {}
                for child in entry:
                    tag = child.tag
                    if '}' in tag:
                        tag = tag.split('}')[1]

                    text = child.text if child.text else ''
                    item_dict[tag] = text

                items.append(item_dict)

        return items
    except ET.ParseError as e:
        print(f"Error parsing XML: {str(e)}")
        return None
    except Exception as e:
        print(f"Error processing XML: {str(e)}")
        return None


def main():
    # Default URL if not provided as argument
    default_url = "https://ecfsapi.fcc.gov/filings?sort=date_disseminated%2CDESC&proceedings_name=25-233&limit=100&sort=date_disseminated,DESC&type=rss"

    # Accept URL from command line argument or use default
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = default_url

    print(f"Fetching RSS feed from: {url}")

    # Fetch the RSS feed
    xml_content = fetch_rss_feed(url)

    if xml_content is None:
        print("Failed to fetch RSS feed")
        return

    print("Parsing XML and extracting items...")

    # Parse items from XML
    items = parse_rss_items(xml_content)

    if items is None:
        print("Failed to parse RSS feed")
        return

    print(f"Found {len(items)} items")

    # Save to JSON file
    output_filename = f'fcc_rss_items_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'

    with open(output_filename, 'w', encoding='utf-8') as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Conversion complete!")
    print(f"Extracted: {len(items)} items")
    print(f"Output saved to: {output_filename}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

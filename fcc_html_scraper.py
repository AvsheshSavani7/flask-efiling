import asyncio
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from io import BytesIO
from datetime import datetime
import re
from fcc_rss_to_json import fetch_rss_feed, parse_rss_items

# Proxy configuration (same as puc_scraper.py)
proxy_host = "108.59.242.138"
proxy_port = 46885
proxy_username = "GSenAgrfKhuNWkd"
proxy_password = "8lmVa5yl0pKp9MI"

# Try to import document processing libraries
try:
    import PyPDF2
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

logger = logging.getLogger(__name__)


async def scrape_html_from_url_async(url, wait_time=10):
    """
    Scrape HTML content from a given URL using Playwright to handle JavaScript-rendered content
    Uses residential proxy (same as puc_scraper.py)
    Returns HTML content as string or None on error
    """
    try:
        async with async_playwright() as p:
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
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                }
            )
            page = await context.new_page()
            logger.info(
                f"Using residential proxy for HTML scraping: {proxy_host}:{proxy_port}")

            try:
                # Navigate to the URL
                await page.goto(url, wait_until='networkidle', timeout=30000)

                # Wait for the React app to load - wait for content in the root div
                try:
                    await page.wait_for_selector('#root', state='attached', timeout=10000)
                    # Wait for document download section to appear
                    try:
                        await page.wait_for_selector('div.card-header', timeout=10000)
                        logger.info("Document download section found")
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "Document download section not found, continuing anyway")
                    # Wait a bit more for React to render content
                    await page.wait_for_timeout(wait_time * 1000)
                except PlaywrightTimeoutError:
                    logger.warning(
                        f"Timeout waiting for React app to load, continuing anyway")

                # Get the fully rendered HTML
                html_content = await page.content()
                await browser.close()
                return html_content

            except PlaywrightTimeoutError as e:
                await browser.close()
                logger.error(
                    f"Timeout error scraping HTML from {url}: {str(e)}")
                return None
            except Exception as e:
                await browser.close()
                logger.error(f"Error scraping HTML from {url}: {str(e)}")
                return None

    except Exception as e:
        logger.error(f"Error initializing Playwright for {url}: {str(e)}")
        return None


def scrape_html_from_url(url, wait_time=10):
    """
    Synchronous wrapper for scraping HTML content from a given URL
    Uses Playwright to handle JavaScript-rendered content
    Returns HTML content as string or None on error
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(scrape_html_from_url_async(url, wait_time))


def extract_document_download_links(html_content, base_url=None):
    """
    Extract all document download links from the "Document Download" section in HTML

    Args:
        html_content: HTML content as string
        base_url: Base URL to resolve relative links (optional)

    Returns:
        List of dictionaries containing link information (deduplicated by URL):
        [
            {
                "url": "https://...",
                "text": "filename.pdf",
                "aria_label": "Download filename.pdf",
                "title": "..."
            },
            ...
        ]
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        download_links = []
        seen_urls = set()  # Track URLs to avoid duplicates

        # Find the "Document Download" section
        # Look for div with class "card-header" containing "Document Download"
        card_headers = soup.find_all('div', class_='card-header')

        for header in card_headers:
            if 'Document Download' in header.get_text():
                # Find the parent card or the next sibling list-group
                # The links are typically in a list-group after the card-header
                parent = header.find_parent()
                list_groups_checked = set()  # Track which list-groups we've already processed

                if parent:
                    # Find all links in the list-group
                    list_group = parent.find('div', class_='list-group')
                    if list_group and id(list_group) not in list_groups_checked:
                        list_groups_checked.add(id(list_group))
                        links = list_group.find_all('a', href=True)
                        for link in links:
                            href = link.get('href', '')
                            # Make absolute URL if base_url provided
                            if base_url and not href.startswith('http'):
                                href = urljoin(base_url, href)

                            # Skip if we've already seen this URL
                            if href in seen_urls:
                                continue
                            seen_urls.add(href)

                            link_info = {
                                "url": href,
                                "text": link.get_text(strip=True),
                                "aria_label": link.get('aria-label', ''),
                                "title": link.get('title', '')
                            }
                            download_links.append(link_info)

                # Also check if links are in a sibling element
                next_sibling = header.find_next_sibling(
                    'div', class_='list-group')
                if next_sibling and id(next_sibling) not in list_groups_checked:
                    list_groups_checked.add(id(next_sibling))
                    links = next_sibling.find_all('a', href=True)
                    for link in links:
                        href = link.get('href', '')
                        if base_url and not href.startswith('http'):
                            href = urljoin(base_url, href)

                        # Skip if we've already seen this URL
                        if href in seen_urls:
                            continue
                        seen_urls.add(href)

                        link_info = {
                            "url": href,
                            "text": link.get_text(strip=True),
                            "aria_label": link.get('aria-label', ''),
                            "title": link.get('title', '')
                        }
                        download_links.append(link_info)

                break  # Found the section, no need to continue

        return download_links

    except Exception as e:
        logger.error(f"Error extracting document download links: {str(e)}")
        return []


async def download_document_async(document_url, wait_time=5):
    """
    Download a document from a URL using Playwright with residential proxy
    Handles JavaScript-rendered pages and waits for actual document download
    Returns the document content as bytes or None on error
    """
    try:
        async with async_playwright() as p:
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
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                accept_downloads=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                }
            )
            logger.info(
                f"Using residential proxy for document download: {proxy_host}:{proxy_port}")

            try:
                # For direct PDF/document URLs, use request API instead of page.goto()
                # This is more reliable for binary content downloads
                if document_url.lower().endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.txt')):
                    try:
                        logger.info(
                            f"Using request API for direct document download: {document_url}")
                        response = await context.request.get(
                            document_url,
                            timeout=60000
                        )
                        if response.ok:
                            content = await response.body()
                            if content and (content.startswith(b'%PDF') or len(content) > 100):
                                logger.info(
                                    f"Successfully downloaded document via request API: {len(content)} bytes")
                                await browser.close()
                                return content
                        logger.warning(
                            f"Request API returned non-OK status for {document_url}, status: {response.status}, trying page.goto()")
                        # Fall through to page.goto() method
                    except Exception as e:
                        logger.warning(
                            f"Request API failed for {document_url}: {str(e)}, trying page.goto()")
                        # Fall through to page.goto() method

                # For other URLs or if request API fails, use page.goto()
                page = await context.new_page()

                # Set up download listener for actual download events
                download_content = None
                download_error = None

                async def handle_download(download):
                    nonlocal download_content, download_error
                    try:
                        # Wait for download to complete with timeout
                        path = await download.path()
                        if path:
                            with open(path, 'rb') as f:
                                download_content = f.read()
                    except Exception as e:
                        # Only log if it's not a cancellation (cancellation is expected for direct streams)
                        if "canceled" not in str(e).lower():
                            logger.warning(
                                f"Error reading downloaded file: {str(e)}")
                        download_error = str(e)

                page.on("download", handle_download)

                # Navigate to the document URL with more lenient wait strategy
                try:
                    response = await page.goto(document_url, wait_until='domcontentloaded', timeout=60000)
                except Exception as goto_error:
                    # If goto fails, try with load strategy
                    try:
                        logger.warning(
                            f"domcontentloaded failed for {document_url}, trying load strategy")
                        response = await page.goto(document_url, wait_until='load', timeout=60000)
                    except Exception as load_error:
                        # If both fail, try without wait_until
                        try:
                            logger.warning(
                                f"load failed for {document_url}, trying commit strategy")
                            response = await page.goto(document_url, wait_until='commit', timeout=60000)
                        except Exception as commit_error:
                            logger.error(
                                f"All navigation strategies failed for {document_url}")
                            await browser.close()
                            return None

                # Check response content-type first
                if response:
                    content_type = response.headers.get(
                        'content-type', '').lower()

                    # If it's a direct PDF or document stream, read it directly
                    if 'application/pdf' in content_type or 'application/octet-stream' in content_type or document_url.lower().endswith('.pdf'):
                        content = await response.body()
                        if content and content.startswith(b'%PDF'):
                            logger.info(
                                f"Direct PDF stream detected for {document_url}")
                            await browser.close()
                            return content

                    # Wait a bit for any JavaScript to trigger downloads (only if not direct PDF)
                    if 'application/pdf' not in content_type:
                        await page.wait_for_timeout(wait_time * 1000)

                    # If we got a download, return it
                    if download_content:
                        await browser.close()
                        return download_content

                    # Otherwise, try to get the response body
                    content = await response.body()

                    # Check if it's a PDF by content signature
                    if content and content.startswith(b'%PDF'):
                        logger.info(
                            f"PDF detected by content signature for {document_url}")
                        await browser.close()
                        return content

                    # Check if it's HTML (error page) or actual document
                    try:
                        content_str = content.decode('utf-8', errors='ignore')
                        if '<html' in content_str.lower() or 'javascript' in content_str.lower():
                            # It's HTML, not a document - try to find the actual download link
                            # Look for direct download links or iframe sources
                            soup = BeautifulSoup(content_str, 'html.parser')

                            # Try to find iframe with document
                            iframe = soup.find('iframe', src=True)
                            if iframe:
                                iframe_src = iframe.get('src', '')
                                if iframe_src:
                                    # Navigate to iframe source
                                    if not iframe_src.startswith('http'):
                                        from urllib.parse import urljoin
                                        iframe_src = urljoin(
                                            document_url, iframe_src)

                                    iframe_response = await page.goto(iframe_src, wait_until='networkidle', timeout=60000)
                                    if iframe_response:
                                        iframe_content = await iframe_response.body()
                                        await browser.close()
                                        return iframe_content

                            # If no iframe, return None (couldn't get actual document)
                            await browser.close()
                            logger.warning(
                                f"Document URL returned HTML instead of document: {document_url}")
                            return None
                    except:
                        pass

                    await browser.close()
                    return content
                else:
                    await browser.close()
                    return None

            except Exception as e:
                error_str = str(e).lower()
                # Check if it's an ERR_ABORTED error - this often happens with direct PDF streams
                if "err_aborted" in error_str or "aborted" in error_str:
                    # Try one more time with request API as fallback
                    try:
                        logger.info(
                            f"ERR_ABORTED detected, trying request API fallback for {document_url}")
                        response = await context.request.get(document_url, timeout=60000)
                        if response.ok:
                            content = await response.body()
                            if content and (content.startswith(b'%PDF') or len(content) > 100):
                                logger.info(
                                    f"Successfully downloaded via request API fallback: {len(content)} bytes")
                                await browser.close()
                                return content
                    except Exception as fallback_error:
                        logger.warning(
                            f"Request API fallback also failed: {str(fallback_error)}")

                await browser.close()
                # Only log as error if it's not a common abort (which we tried to handle)
                if "err_aborted" not in error_str:
                    logger.error(
                        f"Error downloading document from {document_url}: {str(e)}")
                else:
                    logger.warning(
                        f"Document download aborted for {document_url} (may be due to direct stream)")
                return None

    except Exception as e:
        logger.error(
            f"Error initializing Playwright for document download: {str(e)}")
        return None


def download_document(document_url, wait_time=5):
    """
    Synchronous wrapper for downloading a document
    Returns document content as bytes or None on error
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(download_document_async(document_url, wait_time))


def extract_text_from_document(file_content, file_url):
    """
    Extract text from different document types (PDF, DOCX, DOC, TXT, etc.)

    Args:
        file_content: Document content as bytes
        file_url: URL of the document (to determine file type)

    Returns:
        Extracted text as string
    """
    try:
        if not file_content:
            logger.warning("Empty file content provided")
            return ""

        # Check if it's a PDF by content (starts with %PDF) - check first before HTML check
        # Strip any leading whitespace/newlines
        content_start = file_content.lstrip()[:10]
        if content_start.startswith(b'%PDF'):
            logger.info(f"Detected PDF by content signature for {file_url}")
            if PDF_AVAILABLE:
                try:
                    pdf_reader = PyPDF2.PdfReader(BytesIO(file_content))
                    logger.info(f"PDF has {len(pdf_reader.pages)} pages")
                    text = ""
                    for page_num, page in enumerate(pdf_reader.pages):
                        try:
                            page_text = page.extract_text()
                            if page_text:
                                text += page_text + "\n"
                                logger.debug(
                                    f"Extracted {len(page_text)} characters from page {page_num + 1}")
                        except Exception as page_error:
                            logger.warning(
                                f"Error extracting text from page {page_num + 1}: {str(page_error)}")
                            continue

                    extracted_text = text.strip()
                    if extracted_text:
                        logger.info(
                            f"Successfully extracted {len(extracted_text)} characters from PDF")
                        return extracted_text
                    else:
                        logger.warning(
                            f"PDF extraction returned empty text for {file_url}")
                        return "PDF text extraction returned empty (PDF may be image-based or encrypted)"
                except Exception as e:
                    logger.error(
                        f"Error extracting text from PDF: {str(e)}", exc_info=True)
                    return f"Error extracting PDF text: {str(e)}"
            else:
                logger.warning(
                    "PDF extraction not available (PyPDF2 not installed)")
                return "PDF extraction not available (PyPDF2 not installed)"

        # First, check if content is HTML (error page)
        try:
            content_str = file_content.decode('utf-8', errors='ignore')
            if '<html' in content_str.lower() or '<!doctype' in content_str.lower() or 'javascript' in content_str.lower():
                logger.info(f"Detected HTML content for {file_url}")
                soup = BeautifulSoup(content_str, 'html.parser')
                return soup.get_text().strip()
        except:
            pass

        # Determine file extension from URL as fallback
        file_extension = file_url.split(
            '.')[-1].lower() if '.' in file_url else ''

        # Extract text based on file extension
        if file_extension == 'pdf' and PDF_AVAILABLE:
            try:
                pdf_reader = PyPDF2.PdfReader(BytesIO(file_content))
                text = ""
                for page in pdf_reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
                return text.strip()
            except Exception as e:
                logger.error(f"Error extracting text from PDF: {str(e)}")
                return f"Error extracting PDF text: {str(e)}"

        elif file_extension in ['docx'] and DOCX_AVAILABLE:
            try:
                doc = docx.Document(BytesIO(file_content))
                text = ""
                for paragraph in doc.paragraphs:
                    if paragraph.text:
                        text += paragraph.text + "\n"
                return text.strip()
            except Exception as e:
                logger.error(f"Error extracting text from DOCX: {str(e)}")
                return f"Error extracting DOCX text: {str(e)}"

        elif file_extension == 'txt':
            return file_content.decode('utf-8', errors='ignore')

        elif file_extension in ['html', 'htm']:
            soup = BeautifulSoup(file_content.decode(
                'utf-8', errors='ignore'), 'html.parser')
            return soup.get_text().strip()

        else:
            # Unknown file type - check if it might be PDF that we missed
            content_start = file_content.lstrip()[:10]
            if content_start.startswith(b'%PDF'):
                logger.warning(
                    f"PDF detected in else clause for {file_url}, attempting extraction")
                if PDF_AVAILABLE:
                    try:
                        pdf_reader = PyPDF2.PdfReader(BytesIO(file_content))
                        text = ""
                        for page in pdf_reader.pages:
                            page_text = page.extract_text()
                            if page_text:
                                text += page_text + "\n"
                        return text.strip()
                    except Exception as e:
                        logger.error(
                            f"Error extracting PDF in else clause: {str(e)}")

            # If it looks like binary data, don't try to decode as text
            if len(file_content) > 0:
                content_start_bytes = file_content[:4]
                if content_start_bytes not in [b'%PDF', b'PK\x03\x04', b'\x00']:
                    try:
                        # Try decoding as text
                        decoded = file_content.decode('utf-8', errors='ignore')
                        # If it contains mostly printable characters, return it
                        if all(ord(c) < 128 and (c.isprintable() or c.isspace()) for c in decoded[:1000]):
                            logger.info(
                                f"Decoded as plain text for {file_url}")
                            return decoded
                    except:
                        pass

            # If we can't determine the type, return an error message
            logger.warning(
                f"Unknown file type for {file_url}, size: {len(file_content)} bytes, first bytes: {file_content[:20]}")
            return f"Unknown file type or unable to extract text. File size: {len(file_content)} bytes"

    except Exception as e:
        logger.error(f"Error extracting text from document: {str(e)}")
        return f"Error extracting text: {str(e)}"


def extract_metadata_from_rss_item(item):
    """
    Extract metadata from RSS item element

    Args:
        item: Dictionary containing RSS item data

    Returns:
        Dictionary with metadata fields
    """
    try:
        metadata = {
            "docket_type": "FCC"
        }

        description = item.get('description', '') or ''
        desc_text = description.replace('&#xD;', '').replace('<br/>', '\n')

        # Extract date from dc:date or date field
       # 1) Prefer "Date Received: MM/DD/YYYY"
        m = re.search(
            r'Date Received:\s*(\d{2}/\d{2}/\d{4})', desc_text, re.IGNORECASE)
        if m:
            metadata["date"] = m.group(1)
        else:
            # 2) Fallback to dc:date ISO string
            iso = (item.get('dc:date') or item.get('date') or '').strip()
            if iso:
                dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
                metadata["date"] = dt.strftime("%m/%d/%Y")

                # Extract document_type from description (Comment Type)
                description = item.get('description', '')
                comment_type_match = re.search(
                    r'Comment Type:\s*([^<]+)', description, re.IGNORECASE)
                if comment_type_match:
                    metadata["document_type"] = comment_type_match.group(
                        1).strip()

        # Normalise description for line-based parsing
        desc_text = description.replace('&#xD;', '').replace('<br/>', '\n')

        proceeding_match = re.search(
            r'Proceeding\(s\):\s*([^\n<]+)', desc_text, re.IGNORECASE
        )
        if proceeding_match:
            # Example: "25-233 : In the Matter of Cox Enterprises, Inc. and Charter Communications, Inc."
            metadata["additional_info"] = proceeding_match.group(1).strip()

        elif description:
            # Fallback: cleaned full description text
            soup = BeautifulSoup(desc_text, 'html.parser')
            metadata["additional_info"] = soup.get_text().strip()

      # ----- on_behalf_of: from Filers(s) -----
        filers_match = re.search(
            r'Filers\(s\):\s*([^\n<]+)', desc_text, re.IGNORECASE
        )
        if filers_match:
            filers_raw = filers_match.group(1).strip()
            # Normalise to "A, B, C"
            filers_list = [f.strip()
                           for f in filers_raw.split(',') if f.strip()]
            metadata["on_behalf_of"] = ", ".join(filers_list)

        # Fallback: on_behalf_of from title if Filers(s) not found
        if "on_behalf_of" not in metadata:
            title = item.get('title', '') or ''
            if title:
                # Remove CDATA wrapper if present
                title_clean = re.sub(
                    r'<!\[CDATA\[(.*?)\]\]>', r'\1', title, flags=re.DOTALL
                )
                metadata["on_behalf_of"] = title_clean.strip()

        # Extract docket_number from title or description (e.g., "25-233")
        title_for_docket = item.get('title', '') or ''
        docket_match = re.search(
            r'(\d+-\d+)', (title_for_docket or '') + ' ' + description
        )

        if docket_match:
            metadata["docket_number"] = docket_match.group(1)

        # Extract document_id from link
        link = item.get('link', '')
        if link:
            metadata["document_id"] = link

        metadata["docket_type"] = "FCC"

        return metadata

    except Exception as e:
        logger.error(f"Error extracting metadata from RSS item: {str(e)}")
        return {
            "docket_type": "FCC",
            "error": str(e)
        }


def extract_additional_details_from_html(html_content):
    """
    Extract additional details from HTML page if possible

    Args:
        html_content: HTML content as string

    Returns:
        Dictionary with additional details
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        details = {}

        # Try to find filing information in the page
        # This will depend on the actual HTML structure of FCC pages

        return details

    except Exception as e:
        logger.error(
            f"Error extracting additional details from HTML: {str(e)}")
        return {}


def extract_brief_comment_from_html(html_content):
    """
    Extract Brief Comment value from HTML content if available.

    Looks for a label with id="comment" that is within a div containing "Brief Comment" label.

    Args:
        html_content: HTML content as string

    Returns:
        Brief comment text as string, or empty string if not found
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')

        # Find the label with id="comment"
        comment_label = soup.find('label', {'id': 'comment'})

        if comment_label:
            # Check if it's within a div that contains "form-group" class
            parent_div = comment_label.find_parent(
                'div', class_=lambda x: x and 'form-group' in x)
            if parent_div:
                # Check if there's a "Brief Comment" label in the parent div
                brief_comment_label = parent_div.find(
                    'label', string=re.compile(r'Brief Comment', re.I))
                if brief_comment_label:
                    comment_text = comment_label.get_text(strip=True)
                    if comment_text:
                        logger.info(
                            f"Found Brief Comment: {len(comment_text)} characters")
                        return comment_text

        return ""

    except Exception as e:
        logger.error(f"Error extracting Brief Comment from HTML: {str(e)}")
        return ""


def process_fcc_scraper(url, document_id, wait_time=10):
    """
    Main function to process FCC scraper request.
    Checks for new FCC filings by comparing document_id with RSS feed items.
    If new records found, scrapes HTML and extracts documents.
    If document_id is not provided or is empty string, treats as first entry and scrapes all items.

    Args:
        url: RSS feed URL
        document_id: Document ID (link) to compare against. If None or empty string, scrapes all items.
        wait_time: Wait time for React app to render (default: 10)

    Returns:
        Dictionary with success status and scraped data
    """
    try:
        if not url:
            return {
                "success": False,
                "error": "url is required"
            }

        logger.info(f"Fetching RSS feed from: {url}")

        # Fetch RSS feed
        xml_content = fetch_rss_feed(url)
        if xml_content is None:
            return {
                "success": False,
                "error": "Failed to fetch RSS feed"
            }

        # Parse RSS items
        items = parse_rss_items(xml_content)
        if items is None:
            return {
                "success": False,
                "error": "Failed to parse RSS feed"
            }

        logger.info(f"Parsed {len(items)} items from RSS feed")

        # If document_id is not provided or is empty string, treat as first entry (scrape all items)
        if not document_id or document_id.strip() == "":
            logger.info(
                f"No document_id provided or empty string. Treating as first entry. All {len(items)} items will be scraped.")
            matched_index = len(items)
        else:
            # Find the index of document_id in the items
            matched_index = None
            for idx, item in enumerate(items):
                item_link = item.get('link', '')
                if item_link == document_id:
                    matched_index = idx
                    break

            # If document_id not found, treat all items as new
            if matched_index is None:
                logger.info(
                    f"Document ID {document_id} not found in RSS feed. All {len(items)} items are new.")
                matched_index = len(items)

        # If matched at index 0, no new records
        if matched_index == 0:
            return {
                "success": True,
                "new_records_found": False,
                "message": "No new records found",
                "matched_index": 0,
                "total_items": len(items)
            }

        # New records found (items from index 0 to matched_index - 1)
        new_items = items[:matched_index]
        logger.info(
            f"Found {len(new_items)} new records (indices 0 to {matched_index - 1})")

        # Scrape HTML from all new item links
        scraped_data = []
        for idx, item in enumerate(new_items):
            item_link = item.get('link')
            if not item_link:
                logger.warning(f"Item at index {idx} has no link, skipping")
                continue

            logger.info(f"Scraping HTML from: {item_link}")
            html_content = scrape_html_from_url(item_link, wait_time=wait_time)

            # Extract metadata from RSS item
            metadata = extract_metadata_from_rss_item(item)

            # Extract additional details from HTML if possible
            additional_details = {}
            if html_content:
                additional_details = extract_additional_details_from_html(
                    html_content)
                # Merge additional details into metadata
                metadata.update(additional_details)

            document_links = []
            documents_data = []
            combined_document_text = []

            # Extract Brief Comment from HTML if available
            brief_comment = ""
            if html_content:
                brief_comment = extract_brief_comment_from_html(html_content)
                if brief_comment:
                    logger.info(f"Extracted Brief Comment for item {idx + 1}")

            if html_content:
                # Extract document download links from HTML
                logger.info(
                    f"Extracting document download links from: {item_link}")
                document_links = extract_document_download_links(
                    html_content, base_url=item_link)
                logger.info(
                    f"Found {len(document_links)} document download links")

                # Download each document one by one and extract text
                for doc_idx, doc_link_info in enumerate(document_links):
                    doc_url = doc_link_info.get('url')
                    if not doc_url:
                        continue

                    logger.info(
                        f"Downloading document {doc_idx + 1}/{len(document_links)}: {doc_url}")
                    doc_content = download_document(doc_url)

                    # Extract text from document
                    doc_text = ""
                    if doc_content:
                        # Log content type detection
                        if doc_content.startswith(b'%PDF'):
                            logger.info(
                                f"Document {doc_idx + 1} is a PDF (detected by content)")
                        else:
                            logger.info(
                                f"Document {doc_idx + 1} content type: first 50 bytes = {doc_content[:50]}")

                        doc_text = extract_text_from_document(
                            doc_content, doc_url)

                        # Log extraction result
                        if doc_text and len(doc_text) > 0:
                            logger.info(
                                f"Extracted {len(doc_text)} characters from document {doc_idx + 1}")
                            if doc_text.startswith('%PDF'):
                                logger.warning(
                                    f"WARNING: Document {doc_idx + 1} text extraction returned raw PDF content!")
                        else:
                            logger.warning(
                                f"Document {doc_idx + 1} text extraction returned empty or error")

                        # Add to combined text with separator
                        combined_document_text.append(
                            f"--- Document {doc_idx + 1}: {doc_link_info.get('text', doc_url)} ---\n{doc_text}\n")

                    documents_data.append({
                        "index": doc_idx,
                        "url": doc_url,
                        "text": doc_link_info.get('text', ''),
                        "aria_label": doc_link_info.get('aria_label', ''),
                        "title": doc_link_info.get('title', ''),
                        "content_length": len(doc_content) if doc_content else 0,
                        "extracted_text": doc_text,
                        "text_length": len(doc_text),
                        "downloaded_successfully": doc_content is not None
                    })

            # Combine all document texts
            combined_text = "\n\n".join(
                combined_document_text) if combined_document_text else ""

            # Append Brief Comment to combined text if available
            if brief_comment:
                if combined_text:
                    combined_text += f"\n\n--- Brief Comment ---\n{brief_comment}"
                else:
                    combined_text = f"--- Brief Comment ---\n{brief_comment}"

            scraped_data.append({
                "scraped_successfully": html_content is not None,
                "metadata": metadata,
                "combined_document_text": combined_text,
                "combined_text_length": len(combined_text),
                "brief_comment": brief_comment if brief_comment else None
            })

        # Calculate total documents found and downloaded
        total_combined_text_length = sum(
            d.get("combined_text_length", 0) for d in scraped_data)

        return {
            "success": True,
            "new_records_found": True,
            "matched_index": matched_index,
            "total_items": len(items),
            "new_records_count": len(new_items),
            "scraped_count": len([d for d in scraped_data if d["scraped_successfully"]]),
            "total_combined_text_length": total_combined_text_length,
            "scraped_data": scraped_data
        }

    except Exception as e:
        logger.error(f"Error during FCC scraping: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

import asyncio
import requests
import time
import zipfile
import os
import tempfile
import urllib3
from io import BytesIO
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import PyPDF2

# Try to import openpyxl for Excel file parsing
try:
    import openpyxl
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False
    print("Warning: openpyxl not installed. XLSX files will not be parsed. Install with: pip install openpyxl")

# Try to import python-docx for Word document parsing
try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    print("Warning: python-docx not installed. DOCX files will not be parsed. Install with: pip install python-docx")

# Disable SSL warnings for requests with verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TARGET_URL = "https://ipinfo.io/ip"
API_KEY = "1cc2815b0dfbc0b37d0218bc5f4325d1"

# proxy_host = "95.135.111.121"
# proxy_port = 49815
# proxy_username = "OcFu9LdAZ1XbXpa"
# proxy_password = "T7jFlurhMqMWhha"


proxy_host = "108.59.242.138"
proxy_port = 46885
proxy_username = "GSenAgrfKhuNWkd"
proxy_password = "8lmVa5yl0pKp9MI"


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


async def playwright_2captcha_fetch_puc(url, wait_time=15):
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
            with open("debug_puc_initial_timeout.html", "w", encoding="utf-8") as f:
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
        with open("debug_puc_before_submit.html", "w", encoding="utf-8") as f:
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
        with open("debug_puc_after_submit.html", "w", encoding="utf-8") as f:
            f.write(html_after)

        print("Saving HTML ...")
        await browser.close()
        return html_after


def extract_zip_links_from_html(html_content, base_url):
    """
    Extract ZIP file links from the HTML table.

    Args:
        html_content: HTML content as string
        base_url: Base URL to resolve relative links

    Returns:
        List of dictionaries containing zip_url, name, description, and type
    """
    soup = BeautifulSoup(html_content, "html.parser")
    zip_files = []

    # Find the table with documents
    table = soup.find("table", class_="table")
    if not table:
        print("No table found in HTML")
        return zip_files

    # Find all rows in tbody (skip header)
    tbody = table.find("tbody")
    if tbody:
        rows = tbody.find_all("tr")
    else:
        # If no tbody, get all rows except the first (header)
        rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        # Get the link in the first cell
        link_elem = cells[0].find("a")
        if not link_elem or not link_elem.get("href"):
            continue

        zip_url = link_elem.get("href")
        name = link_elem.get_text(strip=True)
        description = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        file_type = cells[2].get_text(strip=True) if len(cells) > 2 else ""

        # Only process ZIP files
        if file_type.upper() == "ZIP" or zip_url.upper().endswith(".ZIP"):
            # Resolve relative URLs
            if not zip_url.startswith("http"):
                zip_url = urljoin(base_url, zip_url)

            zip_files.append({
                "url": zip_url,
                "name": name,
                "description": description,
                "type": file_type
            })

    return zip_files


def download_and_extract_zip(zip_url, output_dir=None):
    """
    Download a ZIP file and extract its contents.

    Args:
        zip_url: URL of the ZIP file to download
        output_dir: Directory to extract files to (if None, uses temp directory)

    Returns:
        Dictionary with extraction results
    """
    try:
        # Use proxy if available
        proxy_dict = None
        if proxy_host and proxy_port:
            proxy_dict = {
                "http": f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}",
                "https": f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
            }

        # Download the ZIP file
        # Disable SSL verification to match Playwright browser context settings
        print(f"Downloading ZIP from: {zip_url}")
        response = requests.get(
            zip_url, proxies=proxy_dict, timeout=120, verify=False)
        response.raise_for_status()

        # Create output directory if not provided
        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix="puc_extracted_")
        else:
            os.makedirs(output_dir, exist_ok=True)

        # Save ZIP file temporarily
        zip_filename = os.path.basename(urlparse(zip_url).path)
        zip_path = os.path.join(output_dir, zip_filename)

        with open(zip_path, "wb") as f:
            f.write(response.content)

        print(f"ZIP file saved to: {zip_path}")

        # Extract ZIP file
        extracted_files = []
        extract_dir = os.path.join(output_dir, f"{zip_filename}_extracted")
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)
            extracted_files = zip_ref.namelist()

        print(f"Extracted {len(extracted_files)} files to: {extract_dir}")

        # Get full paths of extracted files and extract text from PDFs
        extracted_file_paths = []
        pdf_text_content = []

        for file_name in extracted_files:
            full_path = os.path.join(extract_dir, file_name)
            if os.path.isfile(full_path):
                file_info = {
                    "name": file_name,
                    "path": full_path,
                    "size": os.path.getsize(full_path),
                    "type": "unknown"
                }

                # Check if it's a PDF file and extract text
                if file_name.lower().endswith('.pdf'):
                    file_info["type"] = "pdf"
                    try:
                        # Read PDF file and extract text
                        with open(full_path, 'rb') as pdf_file:
                            pdf_content = pdf_file.read()

                            # Validate PDF header
                            if pdf_content.startswith(b'%PDF'):
                                pdf_reader = PyPDF2.PdfReader(
                                    BytesIO(pdf_content))
                                text = ""
                                page_count = len(pdf_reader.pages)

                                for page in pdf_reader.pages:
                                    page_text = page.extract_text()
                                    text += page_text + "\n"

                                pdf_text = text.strip()
                                file_info["pdf_text"] = pdf_text
                                file_info["pdf_page_count"] = page_count
                                file_info["pdf_text_length"] = len(pdf_text)

                                pdf_text_content.append({
                                    "file_name": file_name,
                                    "page_count": page_count,
                                    "text_length": len(pdf_text),
                                    "text": pdf_text
                                })

                                print(
                                    f"Extracted text from PDF {file_name}: {len(pdf_text)} characters, {page_count} pages")
                            else:
                                print(
                                    f"Invalid PDF file {file_name}: doesn't start with PDF header")
                                file_info["pdf_error"] = "Invalid PDF header"
                    except Exception as e:
                        print(
                            f"Error extracting text from PDF {file_name}: {str(e)}")
                        file_info["pdf_error"] = str(e)

                # Check if it's an XLSX file and extract text
                elif file_name.lower().endswith(('.xlsx', '.xls')):
                    file_info["type"] = "xlsx"
                    try:
                        if not OPENPYXL_AVAILABLE:
                            file_info["xlsx_error"] = "openpyxl library not installed"
                            print(
                                f"Skipping XLSX {file_name}: openpyxl not available")
                        else:
                            # Read and parse Excel file
                            workbook = openpyxl.load_workbook(
                                full_path, data_only=True)
                            text_content = []

                            # Process all sheets
                            for sheet_name in workbook.sheetnames:
                                sheet = workbook[sheet_name]
                                sheet_text = [f"Sheet: {sheet_name}"]
                                sheet_text.append("=" * 50)

                                # Read all rows
                                for row_num, row in enumerate(sheet.iter_rows(values_only=True), 1):
                                    # Filter out completely empty rows
                                    if any(cell is not None and str(cell).strip() for cell in row):
                                        # Convert row to text, handling None values
                                        row_text = []
                                        for cell in row:
                                            if cell is None:
                                                row_text.append("")
                                            else:
                                                # Convert cell to string and handle special characters
                                                cell_str = str(cell).strip()
                                                row_text.append(cell_str)

                                        # Join with tab or pipe separator for readability
                                        sheet_text.append(" | ".join(row_text))

                                text_content.append("\n".join(sheet_text))
                                # Add spacing between sheets
                                text_content.append("\n")

                            xlsx_text = "\n".join(text_content).strip()
                            file_info["xlsx_text"] = xlsx_text
                            file_info["xlsx_sheet_count"] = len(
                                workbook.sheetnames)
                            file_info["xlsx_sheets"] = workbook.sheetnames
                            file_info["xlsx_text_length"] = len(xlsx_text)

                            print(
                                f"Extracted text from XLSX {file_name}: {len(xlsx_text)} characters, {len(workbook.sheetnames)} sheet(s)")
                            workbook.close()
                    except Exception as e:
                        print(
                            f"Error extracting text from XLSX {file_name}: {str(e)}")
                        file_info["xlsx_error"] = str(e)

                # Check if it's a DOCX file and extract text
                elif file_name.lower().endswith(('.docx', '.doc')):
                    file_info["type"] = "docx"
                    try:
                        if not DOCX_AVAILABLE:
                            file_info["docx_error"] = "python-docx library not installed"
                            print(
                                f"Skipping DOCX {file_name}: python-docx not available")
                        else:
                            # Read and parse Word document
                            doc = docx.Document(full_path)
                            text_content = []

                            # Extract text from all paragraphs
                            for paragraph in doc.paragraphs:
                                if paragraph.text.strip():
                                    text_content.append(paragraph.text.strip())

                            # Also extract text from tables if present
                            for table in doc.tables:
                                text_content.append("\n[Table]")
                                for row in table.rows:
                                    row_text = []
                                    for cell in row.cells:
                                        cell_text = cell.text.strip()
                                        row_text.append(cell_text)
                                    if any(row_text):
                                        text_content.append(
                                            " | ".join(row_text))
                                text_content.append("[End Table]\n")

                            docx_text = "\n".join(text_content).strip()
                            file_info["docx_text"] = docx_text
                            file_info["docx_text_length"] = len(docx_text)
                            file_info["docx_paragraph_count"] = len(
                                [p for p in doc.paragraphs if p.text.strip()])
                            file_info["docx_table_count"] = len(doc.tables)

                            print(
                                f"Extracted text from DOCX {file_name}: {len(docx_text)} characters, {len(doc.paragraphs)} paragraphs, {len(doc.tables)} tables")
                    except Exception as e:
                        print(
                            f"Error extracting text from DOCX {file_name}: {str(e)}")
                        file_info["docx_error"] = str(e)

                extracted_file_paths.append(file_info)

        # Return simplified file list with only essential info
        simplified_files = []
        for file_info in extracted_file_paths:
            simplified_file = {
                "name": file_info["name"],
                "type": file_info.get("type", "unknown")
            }

            # Add text content if it's a PDF
            if file_info.get("type") == "pdf" and file_info.get("pdf_text"):
                simplified_file["text"] = file_info["pdf_text"]

            # Add text content if it's an XLSX file
            elif file_info.get("type") == "xlsx" and file_info.get("xlsx_text"):
                simplified_file["text"] = file_info["xlsx_text"]

            # Add text content if it's a DOCX file
            elif file_info.get("type") == "docx" and file_info.get("docx_text"):
                simplified_file["text"] = file_info["docx_text"]

            simplified_files.append(simplified_file)

        if pdf_text_content:
            print(f"Extracted text from {len(pdf_text_content)} PDF file(s)")

        return {
            "success": True,
            "files": simplified_files
        }

    except Exception as e:
        print(f"Error downloading/extracting ZIP from {zip_url}: {str(e)}")
        return {
            "success": False,
            "zip_url": zip_url,
            "error": str(e)
        }


def process_puc_documents(html_content, base_url, extract_zips=True):
    """
    Process PUC documents from HTML: extract ZIP links, download and extract them.

    Args:
        html_content: HTML content as string
        base_url: Base URL of the page
        extract_zips: Whether to download and extract ZIP files

    Returns:
        Dictionary with zip_urls array and extracted_files array
    """
    # Extract ZIP links from HTML
    zip_files = extract_zip_links_from_html(html_content, base_url)
    zip_urls = [zip_info["url"] for zip_info in zip_files]

    print(f"Found {len(zip_files)} ZIP file(s) in the table")

    # Collect all extracted files from all ZIPs
    all_extracted_files = []

    # Download and extract ZIP files if requested
    if extract_zips and zip_files:
        for zip_info in zip_files:
            print(f"Processing ZIP: {zip_info['name']}")
            extraction_result = download_and_extract_zip(zip_info["url"])
            if extraction_result.get("success") and extraction_result.get("files"):
                # Add all files from this ZIP to the main array
                all_extracted_files.extend(extraction_result["files"])

    return {
        "zip_urls": zip_urls,
        "extracted_files": all_extracted_files
    }


def fetch_with_playwright_2captcha_puc(url, wait_time=30, extract_zips=True):
    """
    Synchronous wrapper for playwright_2captcha_fetch_puc.
    Properly handles event loop conflicts when called from Flask.
    Also processes ZIP files from the scraped HTML.

    Args:
        url: URL to scrape
        wait_time: Wait time for page load
        extract_zips: Whether to extract ZIP files found in the table

    Returns:
        If extract_zips=True: Dictionary with HTML and extracted files
        If extract_zips=False: HTML content as string
    """
    try:
        # First try without nest_asyncio
        html_content = asyncio.run(
            playwright_2captcha_fetch_puc(url, wait_time))
    except RuntimeError as e:
        # If we get a RuntimeError about event loop, apply nest_asyncio
        import nest_asyncio
        nest_asyncio.apply()
        # Now try again with nest_asyncio applied
        html_content = asyncio.run(
            playwright_2captcha_fetch_puc(url, wait_time))

    # If HTML is empty or None, return appropriate structure
    if not html_content:
        if not extract_zips:
            return html_content
        else:
            return {
                "zip_urls": [],
                "extracted_files": []
            }

    # If extract_zips is False, just return HTML
    if not extract_zips:
        return html_content

    # Process ZIP files and return simplified structure
    return process_puc_documents(html_content, url, extract_zips=True)


if __name__ == "__main__":
    result = fetch_with_playwright_2captcha_puc(TARGET_URL, extract_zips=True)

    if isinstance(result, dict):
        # Result includes ZIP extraction info
        html_content = result.get("html_content", "")
        with open("puc_page.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"HTML saved to puc_page.html")
        print(f"Found {len(result.get('zip_files', []))} ZIP file(s)")
        print(
            f"Extracted {len(result.get('extracted_files', []))} ZIP file(s)")
        for idx, extracted in enumerate(result.get("extracted_files", []), 1):
            extraction = extracted.get("extraction", {})
            if extraction.get("success"):
                print(
                    f"  ZIP {idx}: Extracted {extraction.get('file_count', 0)} files to {extraction.get('extract_dir', '')}")
            else:
                print(
                    f"  ZIP {idx}: Failed - {extraction.get('error', 'Unknown error')}")
    else:
        # Just HTML content
        with open("puc_page.html", "w", encoding="utf-8") as f:
            f.write(result or "")
        print("HTML saved to puc_page.html")

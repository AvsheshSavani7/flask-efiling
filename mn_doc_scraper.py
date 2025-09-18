import undetected_chromedriver as uc
import time
import os
import requests
from bs4 import BeautifulSoup
import PyPDF2
import docx
from io import BytesIO


def extract_text_from_document(file_content, file_extension):
    """
    Extract text from different document types.

    Args:
        file_content (bytes): The file content as bytes
        file_extension (str): File extension (pdf, docx, txt, etc.)

    Returns:
        str: Extracted text content
    """
    try:
        # Check if content is HTML (error page)
        content_str = file_content.decode('utf-8', errors='ignore')
        if '<html' in content_str.lower() or '<!doctype' in content_str.lower():
            soup = BeautifulSoup(content_str, 'html.parser')
            return soup.get_text().strip()

        if file_extension.lower() == 'pdf':
            # Validate and extract PDF content
            if not file_content.startswith(b'%PDF'):
                return f"Invalid PDF file - content doesn't start with PDF header"

            pdf_reader = PyPDF2.PdfReader(BytesIO(file_content))
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            return text.strip()

        elif file_extension.lower() in ['docx', 'doc']:
            # Extract text from Word document
            doc = docx.Document(BytesIO(file_content))
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text.strip()

        elif file_extension.lower() == 'txt':
            return file_content.decode('utf-8', errors='ignore')

        elif file_extension.lower() in ['html', 'htm']:
            soup = BeautifulSoup(file_content.decode(
                'utf-8', errors='ignore'), 'html.parser')
            return soup.get_text().strip()

        else:
            return file_content.decode('utf-8', errors='ignore')

    except Exception as e:
        try:
            return file_content.decode('utf-8', errors='ignore')
        except:
            return f"Error extracting text: {str(e)}"


def parse_mn_documents(wait_time=20, url=""):
    """
    Download and parse Minnesota e-filing documents to extract text content.

    Args:
        wait_time (int): Time to wait for page load in seconds
        url (str): The document download URL (should be a direct download link)

    Returns:
        dict: Dictionary containing extracted text and metadata
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
        # Load the document download page

        driver.get(url)
        time.sleep(wait_time)

        # Get the current URL after any redirects
        current_url = driver.current_url

        # Determine file type from URL
        file_extension = "pdf"  # default for Minnesota e-filing documents
        if '.pdf' in current_url.lower():
            file_extension = "pdf"
        elif '.docx' in current_url.lower() or '.doc' in current_url.lower():
            file_extension = "docx"
        elif '.txt' in current_url.lower():
            file_extension = "txt"
        elif '.html' in current_url.lower() or '.htm' in current_url.lower():
            file_extension = "html"

        # Handle security check page
        page_source = driver.page_source
        if "Security check" in page_source or "verify your browser" in page_source:
            time.sleep(10)
            driver.refresh()
            time.sleep(5)
            current_url = driver.current_url

            # Check if still on security page
            page_source = driver.page_source
            if "Security check" in page_source or "verify your browser" in page_source:
                return {
                    "success": False,
                    "error": "Security check page detected - unable to bypass",
                    "url": current_url,
                    "security_check": True
                }

        # Download the document content using the browser's session
        try:
            # Get cookies from the browser session
            cookies = driver.get_cookies()
            cookie_dict = {cookie['name']: cookie['value']
                           for cookie in cookies}

            # Get the raw content as bytes using requests with browser cookies
            response = requests.get(current_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }, cookies=cookie_dict)

            if response.status_code == 200:
                if len(response.content) == 0:
                    return {
                        "success": False,
                        "error": "Downloaded file is empty",
                        "url": current_url
                    }
                # Extract text from the document
                text_content = extract_text_from_document(
                    response.content, file_extension)
                return {
                    "success": True,
                    "content_type": file_extension,
                    "text_content": text_content,
                    "url": current_url,
                    "content_length": len(text_content),
                    "file_size_bytes": len(response.content)
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to download document. Status code: {response.status_code}",
                    "url": current_url
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Error downloading document: {str(e)}",
                "url": current_url
            }

    finally:
        driver.quit()


if __name__ == "__main__":
    # For testing the function directly
    result = parse_mn_documents()

    if result.get("success"):
        with open("extracted_text.txt", "w", encoding="utf-8") as f:
            f.write(result.get("text_content", ""))
        print("Text content saved to extracted_text.txt")
    else:
        print("Error:", result.get("error"))

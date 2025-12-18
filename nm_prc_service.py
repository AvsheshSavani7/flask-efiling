# nm_prc_service.py
# Service functions for NM PRC eDocket system
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional
from io import BytesIO
import requests
from http.cookiejar import LWPCookieJar
from PyPDF2 import PdfReader

from nm_prc_cookie import EDocketSessionManager, EDocketConfig

# Constants
BASE = "https://edocket.prc.nm.gov/"
COOKIES_FILE = "edocket_cookies.lwp"


def load_session_with_cookies() -> requests.Session:
    """Load a requests session with saved cookies"""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; edocket-listener/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": BASE,
        }
    )

    jar_path = Path(COOKIES_FILE)
    if not jar_path.exists():
        raise FileNotFoundError(
            f"Cookie file not found: {COOKIES_FILE}. Please login first using /nm-prc-login endpoint."
        )

    jar = LWPCookieJar(str(jar_path))
    jar.load(ignore_discard=True, ignore_expires=True)
    s.cookies = jar
    return s


def login_nm_prc(username: str, password: str) -> Dict:
    """
    Login to NM PRC eDocket system and save cookies.

    Args:
        username: Login username
        password: Login password

    Returns:
        Dictionary with success status, message, and file paths

    Raises:
        RuntimeError: If login fails
        ValueError: If username or password is missing
    """
    if not username or not password:
        raise ValueError("Username and password are required")

    # Initialize session manager
    config = EDocketConfig()
    mgr = EDocketSessionManager(config)

    # Perform login (this will save cookies automatically)
    mgr.login(username, password)

    return {
        "success": True,
        "message": "Login successful. Cookies saved.",
        "cookies_file": config.cookies_file,
        "meta_file": config.meta_file
    }


def get_html_from_nm_prc(target_url: str) -> Dict:
    """
    Fetch HTML from a protected NM PRC eDocket URL.

    Args:
        target_url: Full URL to fetch

    Returns:
        Dictionary with success status, HTML content, and content length

    Raises:
        ValueError: If target_url is missing
        FileNotFoundError: If cookie file doesn't exist (session expired)
        requests.exceptions.RequestException: If request fails
    """
    if not target_url:
        raise ValueError("target_url is required")

    # Load session with saved cookies
    s = load_session_with_cookies()

    # Fetch the HTML
    r = s.get(target_url, timeout=30, allow_redirects=True)
    r.raise_for_status()

    # Check if we were redirected to login (session expired)
    if "txtUserName" in r.text and "btnLogin" in r.text:
        raise RuntimeError(
            "Session expired. Please login again using /nm-prc-login endpoint."
        )

    html_content = r.text

    return {
        "success": True,
        "html_content": html_content,
        "content_length": len(html_content)
    }


def extract_pdf_text_from_nm_prc(pdf_url: str) -> Dict:
    """
    Fetch PDF from a protected NM PRC eDocket URL and extract text.

    Args:
        pdf_url: Full URL to the PDF file

    Returns:
        Dictionary with success status, extracted text, page count, and text length

    Raises:
        ValueError: If pdf_url is missing or invalid PDF
        FileNotFoundError: If cookie file doesn't exist (session expired)
        requests.exceptions.RequestException: If request fails
    """
    if not pdf_url:
        raise ValueError("pdf_url is required")

    # Load session with saved cookies
    s = load_session_with_cookies()

    # Fetch the PDF
    r = s.get(pdf_url, timeout=60, allow_redirects=True, stream=True)
    r.raise_for_status()

    # Check if we were redirected to login (session expired)
    content_type = r.headers.get('Content-Type', '').lower()
    if 'text/html' in content_type and ("txtUserName" in r.text and "btnLogin" in r.text):
        raise RuntimeError(
            "Session expired. Please login again using /nm-prc-login endpoint."
        )

    # Get PDF content
    pdf_content = r.content

    # Validate PDF header
    if not pdf_content.startswith(b'%PDF'):
        raise ValueError(
            "Invalid PDF file - content doesn't start with PDF header")

    # Extract text from PDF
    try:
        pdf_file = BytesIO(pdf_content)
        pdf_reader = PdfReader(pdf_file)

        text_content = []
        for page_num, page in enumerate(pdf_reader.pages):
            try:
                text = page.extract_text()
                if text:
                    text_content.append(text)
            except Exception as page_error:
                # Continue with other pages if one fails
                continue

        full_text = "\n\n".join(text_content)
        page_count = len(pdf_reader.pages)

        return {
            "success": True,
            "text": full_text,
            "page_count": page_count,
            "text_length": len(full_text),
            "url": pdf_url
        }
    except Exception as e:
        raise ValueError(f"Error extracting text from PDF: {str(e)}")

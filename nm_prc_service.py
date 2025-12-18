# nm_prc_service.py
# Service functions for NM PRC eDocket system
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional
import requests
from http.cookiejar import LWPCookieJar

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

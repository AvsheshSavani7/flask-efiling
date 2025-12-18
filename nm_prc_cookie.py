# edocket_session.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from http.cookiejar import LWPCookieJar


@dataclass
class EDocketConfig:
    base_url: str = "https://edocket.prc.nm.gov/"
    login_path: str = "Login.aspx"
    cookies_file: str = "edocket_cookies.lwp"
    meta_file: str = "edocket_session_meta.json"
    user_agent: str = "Mozilla/5.0 (compatible; edocket-listener/1.0)"


class EDocketSessionManager:
    def __init__(self, config: EDocketConfig):
        self.cfg = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

        # Persistent cookie jar
        self.cookiejar_path = Path(self.cfg.cookies_file)
        self.session.cookies = LWPCookieJar(str(self.cookiejar_path))
        self._load_cookies_if_present()

    def _load_cookies_if_present(self) -> None:
        if self.cookiejar_path.exists():
            try:
                self.session.cookies.load(
                    ignore_discard=True, ignore_expires=True)
            except Exception:
                # If file is corrupted or format changed, delete and re-login
                self.cookiejar_path.unlink(missing_ok=True)

    def save_cookies(self) -> None:
        self.session.cookies.save(ignore_discard=True, ignore_expires=True)

    def _write_meta(self, data: Dict) -> None:
        Path(self.cfg.meta_file).write_text(json.dumps(data, indent=2))

    def _read_meta(self) -> Optional[Dict]:
        p = Path(self.cfg.meta_file)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    def _get_login_url(self) -> str:
        return urljoin(self.cfg.base_url, self.cfg.login_path)

    def _fetch_login_form_payload(self) -> Dict[str, str]:
        """
        GET Login.aspx and capture hidden fields required for ASP.NET WebForms login.
        """
        login_url = self._get_login_url()
        r = self.session.get(login_url, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise RuntimeError("Login form not found on login page.")

        # Hidden inputs
        payload: Dict[str, str] = {}
        for inp in form.select("input[type='hidden']"):
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value", "")

        # The submit button is often required in WebForms
        # From your HTML: btnLogin
        payload["btnLogin"] = "Login"

        return payload

    def login(self, username: str, password: str) -> None:
        """
        Logs in and persists session cookies for future runs.
        """
        payload = self._fetch_login_form_payload()

        # Credential input names from your Login HTML
        payload["txtUserName"] = username
        payload["txtPassword"] = password

        login_url = self._get_login_url()

        # POST to same endpoint (don't follow redirects initially to check response)
        r = self.session.post(login_url, data=payload,
                              timeout=30, allow_redirects=False)
        r.raise_for_status()

        # Check for redirect to Index.aspx (successful login indicator)
        redirect_location = r.headers.get('Location', '')
        is_redirect_to_index = 'Index.aspx' in redirect_location or '/Index.aspx' in redirect_location

        # Check for authentication cookie (.INFOSHARE or similar)
        has_auth_cookie = any(
            '.INFOSHARE' in str(cookie) or 'ASP.NET_SessionId' in str(cookie)
            for cookie in self.session.cookies
        )

        # If redirected, follow it to get final response
        if r.status_code in (301, 302, 303, 307, 308):
            # Handle relative redirects by joining with base URL
            if redirect_location:
                if redirect_location.startswith('http://') or redirect_location.startswith('https://'):
                    redirect_url = redirect_location
                else:
                    redirect_url = urljoin(
                        self.cfg.base_url, redirect_location)
            else:
                redirect_url = login_url
            r = self.session.get(redirect_url, timeout=30,
                                 allow_redirects=True)
            r.raise_for_status()

        # Multiple checks for login success:
        # 1. Redirect to Index.aspx
        # 2. Has auth cookie
        # 3. No login form in response
        has_login_form = "btnLogin" in r.text and "txtUserName" in r.text
        final_url = r.url

        if has_login_form and not is_redirect_to_index and not has_auth_cookie:
            raise RuntimeError(
                f"Login appears to have failed (still seeing login form). "
                f"Status: {r.status_code}, Final URL: {final_url}, "
                f"Has auth cookie: {has_auth_cookie}, Redirect: {redirect_location}"
            )

        # Save cookies + meta
        self.save_cookies()
        self._write_meta(
            {
                "base_url": self.cfg.base_url,
                "login_url": login_url,
                "logged_in_at_epoch": int(time.time()),
                "has_auth_cookie": has_auth_cookie,
                "redirect_location": redirect_location,
            }
        )

    def is_session_likely_valid(self) -> bool:
        """
        Lightweight check: do we have any cookies stored?
        (This does not guarantee validity, but is useful for a quick decision.)
        """
        return len(self.session.cookies) > 0

    def verify_session(self) -> bool:
        """
        Verify that the current session is valid by trying to access a protected page.
        Returns True if session appears valid, False otherwise.
        """
        try:
            # Try accessing the index page (protected area)
            index_url = urljoin(self.cfg.base_url, "Index.aspx")
            r = self.session.get(index_url, timeout=10, allow_redirects=True)

            # If we get redirected to login, session is invalid
            if 'Login.aspx' in r.url:
                return False

            # If response contains login form, session is invalid
            if "txtUserName" in r.text and "btnLogin" in r.text:
                return False

            return True
        except Exception:
            return False

    def ensure_logged_in(self, username: str, password: str) -> None:
        """
        If we already have cookies, try using them first.
        If anything fails later, you can call login() again.
        """
        if self.is_session_likely_valid() and self.verify_session():
            return
        self.login(username, password)


if __name__ == "__main__":
    # Example usage:
    mgr = EDocketSessionManager(EDocketConfig())

    # Replace with env vars in real usage
    USERNAME = "kp_hyperion"
    PASSWORD = "kd@hyperion"

    mgr.ensure_logged_in(USERNAME, PASSWORD)

    print("Cookies stored at:", mgr.cfg.cookies_file)
    print("Meta stored at:", mgr.cfg.meta_file)

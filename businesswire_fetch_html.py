#!/usr/bin/env python3
"""
Fetch BusinessWire Mergers & Acquisitions newsroom page and save the HTML.

Strategy (in order):
  1. curl_cffi + residential proxy  ← best for Hostinger VPS/Docker (residential IP + Chrome TLS)
  2. curl_cffi direct               ← works on local machines
  3. requests + residential proxy   ← fallback if curl_cffi unavailable on server
  4. requests direct                ← last resort

For Hostinger VPS / Docker:
  - Add `RUN pip install curl_cffi` to your Dockerfile.
  - Strategy 1 will handle it via the residential proxy.

URL: https://www.businesswire.com/newsroom/subject/merger-acquisition

Run:
    python businesswire_fetch_html.py
    python businesswire_fetch_html.py --output my_output.html
    python businesswire_fetch_html.py --no-proxy   # skip proxy (local only)
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import requests as std_requests

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    print("[WARN] curl_cffi unavailable — using requests fallback. "
          "Fix: pip install curl_cffi --force-reinstall", flush=True)

TARGET_URL     = "https://www.businesswire.com/newsroom/subject/merger-acquisition"
DEFAULT_OUTPUT = "businesswire_merger_acquisition.html"

# Residential proxy — same config as accc_cases_register.py
PROXY_HOST     = "108.59.242.138"
PROXY_PORT     = 46885
PROXY_USERNAME = "GSenAgrfKhuNWkd"
PROXY_PASSWORD = "8lmVa5yl0pKp9MI"
PROXY_DICT     = {
    "http":  f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}",
    "https": f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":              "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":          "document",
    "Sec-Fetch-Mode":          "navigate",
    "Sec-Fetch-Site":          "none",
    "Sec-Fetch-User":          "?1",
    "Cache-Control":           "max-age=0",
}


def _is_blocked(html: str) -> bool:
    return "Reference #" in html and "edgesuite.net" in html


def _fetch_cffi(url: str, proxies: dict | None) -> str:
    resp = cffi_requests.get(
        url,
        headers=HEADERS,
        proxies=proxies,
        impersonate="chrome124",
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def _fetch_requests(url: str, proxies: dict | None) -> str:
    resp = std_requests.get(
        url,
        headers=HEADERS,
        proxies=proxies,
        timeout=30,
        verify=False,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def fetch_html(url: str, use_proxy: bool, max_retries: int = 3) -> str:
    proxy = PROXY_DICT if use_proxy else None

    strategies = []
    if use_proxy and CURL_CFFI_AVAILABLE:
        strategies.append(("curl_cffi + residential proxy", _fetch_cffi,    proxy))
    if CURL_CFFI_AVAILABLE:
        strategies.append(("curl_cffi direct",              _fetch_cffi,    None))
    if use_proxy:
        strategies.append(("requests + residential proxy",  _fetch_requests, proxy))
    strategies.append(    ("requests direct",               _fetch_requests, None))

    last_error = None
    for label, fetcher, proxies in strategies:
        for attempt in range(1, max_retries + 1):
            try:
                print(f"[INFO] {label} — attempt {attempt}/{max_retries}", flush=True)
                html = fetcher(url, proxies)
                if _is_blocked(html):
                    print(f"[WARN] Akamai block via {label}", flush=True)
                    last_error = "Akamai block"
                    time.sleep(2)
                    continue
                print(f"[OK] {len(html):,} bytes via {label}", flush=True)
                return html
            except Exception as exc:
                print(f"[WARN] {label} attempt {attempt} failed: {exc}", flush=True)
                last_error = exc
                time.sleep(2)

    raise RuntimeError(
        f"All strategies failed. Last: {last_error}\n"
        "Check proxy credentials or ensure curl_cffi is installed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch BusinessWire M&A newsroom HTML."
    )
    parser.add_argument("--url",      default=TARGET_URL,     help="URL to fetch")
    parser.add_argument("--output",   default=DEFAULT_OUTPUT, help="Output HTML file")
    parser.add_argument("--no-proxy", action="store_true",
                        help="Skip proxy and go direct (useful on local machines)")
    args = parser.parse_args()

    try:
        html = fetch_html(args.url, use_proxy=not args.no_proxy)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", flush=True)
        return 1

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"[DONE] Saved {len(html):,} bytes → {output_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

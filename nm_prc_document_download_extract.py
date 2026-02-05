#!/usr/bin/env python3
"""
NM PRC e360 Document Download and Text Extraction
=================================================
Uses the NM PRC e360 APIs (from nm api doc.json lines 229-247) to:
1. POST get download token (documentId)
2. GET download document with token
3. Extract text from PDF
4. Return param object with extracted text added

API flow:
  - POST https://e360.prc.nm.gov/core/api/apiflow/v1/prc/nm/cms/document/downloadToken
  - GET https://e360.prc.nm.gov/core/api/document/v1/download-chunked?token={token}
"""

from __future__ import annotations

import copy
import requests
from io import BytesIO
from typing import Any

from PyPDF2 import PdfReader

# e360 API endpoints (from nm api doc.json)
BASE_URL = "https://e360.prc.nm.gov/core"
DOWNLOAD_TOKEN_URL = f"{BASE_URL}/api/apiflow/v1/prc/nm/cms/document/downloadToken"
DOWNLOAD_URL = f"{BASE_URL}/api/document/v1/download-chunked"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
}


def get_download_token(session: requests.Session, document_id: str) -> str:
    """
    Step 1: POST to get download token for a document.

    Args:
        session: requests Session (with optional cookies/headers)
        document_id: The document id (param["id"])

    Returns:
        JWT token string for download

    Raises:
        requests.RequestException: If API call fails
        ValueError: If token not in response
    """
    payload = {
        "context": "File",
        "documentId": document_id,
    }
    r = session.post(DOWNLOAD_TOKEN_URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    if data.get("statusCode") != 200:
        raise ValueError(
            f"Download token API returned statusCode {data.get('statusCode')}: {data.get('message', 'unknown')}"
        )
    token = data.get("token")
    if not token:
        raise ValueError("No token in download token response")
    return token


def download_document(session: requests.Session, token: str) -> bytes:
    """
    Step 2: GET download document using token.

    Args:
        session: requests Session
        token: JWT token from get_download_token

    Returns:
        Raw document bytes (e.g. PDF)

    Raises:
        requests.RequestException: If download fails
    """
    url = f"{DOWNLOAD_URL}?token={token}"
    r = session.get(url, timeout=60, stream=True)
    r.raise_for_status()
    return r.content


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Step 3: Extract text from PDF bytes.

    Args:
        pdf_bytes: Raw PDF content

    Returns:
        Extracted text from all pages, or empty string if not a valid PDF or no text
    """
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ""
    try:
        pdf_file = BytesIO(pdf_bytes)
        reader = PdfReader(pdf_file)
        texts = []
        for page in reader.pages:
            try:
                t = page.extract_text()
                if t:
                    texts.append(t)
            except Exception:
                continue
        return "\n\n".join(texts)
    except Exception:
        return ""


def download_and_extract(param: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    """
    Download document via e360 APIs, extract text, and return param with extracted text.

    Uses APIs from nm api doc.json (229-247):
      1) POST downloadToken with documentId
      2) GET download-chunked with token
      3) Extract text from PDF

    Args:
        param: Document record (must include "id" as documentId). Same shape as
               casepublicdocument/getAll items, e.g. row_number, Docket Number,
               caseId, id, documentnumber, documentname, etc.
        session: Optional requests.Session (with cookies if e360 requires auth).
                 If None, creates a new session with default headers.

    Returns:
        Deep copy of param with "extracted_text" key added. On error, adds
        "extracted_text_error" and "extracted_text" as empty string.
    """
    result = copy.deepcopy(param)
    result["extracted_text"] = ""
    document_id = param.get("id")
    if not document_id:
        result["extracted_text_error"] = "param missing 'id' (documentId)"
        return result

    if session is None:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)

    try:
        token = get_download_token(session, str(document_id))
        pdf_bytes = download_document(session, token)
        text = extract_text_from_pdf(pdf_bytes)
        result["extracted_text"] = text or ""
    except requests.RequestException as e:
        result["extracted_text_error"] = str(e)
    except ValueError as e:
        result["extracted_text_error"] = str(e)
    except Exception as e:
        result["extracted_text_error"] = str(e)

    return result


def main():
    """Example: run with a sample param (for testing)."""
    import json

    sample = {
        "row_number": 2,
        "Docket Number": "24-00266-UT",
        "caseId": "975411b4-5f48-4186-85f6-64bc6b8da180",
        "id": "29205b89-7d22-402c-a90e-b3e501832893",
        "documentnumber": "DOC-000180445-26",
        "documentname": "24-00266-UT 2.2.2026  Supplemental Filing to Motion To Reopen Case re Extraordinary Evidence 1000 Percent Rate Increase",
    }
    out = download_and_extract(sample)
    print(out)
    print(json.dumps({k: v for k, v in out.items() if k in (
        "id", "extracted_text", "extracted_text_error")}, indent=2))


if __name__ == "__main__":
    main()

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

import base64
import copy
import os
import requests
from io import BytesIO
from pathlib import Path
from typing import Any

from openai import OpenAI
from PyPDF2 import PdfReader
from dotenv import load_dotenv

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

LLM_OCR_MODEL = "gpt-4.1-mini"
LLM_OCR_MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB

# Load environment variables from project .env
_SCRIPT_DIR = Path(__file__).resolve().parent
_LOCAL_ENV_PATH = _SCRIPT_DIR / ".env"
# On platforms like Render, OPENAI_API_KEY is provided as an environment variable.
# Only load local .env when the key is not already present.
if not os.getenv("OPENAI_API_KEY") and _LOCAL_ENV_PATH.exists():
    load_dotenv(_LOCAL_ENV_PATH)


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


def extract_text_with_llm_from_pdf(pdf_bytes: bytes, document_name: str = "document.pdf") -> str:
    """
    Fallback OCR/text extraction using OpenAI for image-only/non-selectable PDFs.

    Args:
        pdf_bytes: Raw PDF content
        document_name: Optional filename for model context

    Returns:
        Extracted text, or empty string if extraction fails
    """
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ""
    if len(pdf_bytes) > LLM_OCR_MAX_PDF_BYTES:
        return ""

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return ""

    try:
        client = OpenAI(api_key=api_key)
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        response = client.responses.create(
            model=LLM_OCR_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Extract all readable text from this PDF. "
                                "Return only extracted text, preserving order as best as possible. "
                                "Do not summarize."
                            ),
                        },
                        {
                            "type": "input_file",
                            "filename": document_name,
                            "file_data": f"data:application/pdf;base64,{pdf_b64}",
                        },
                    ],
                }
            ],
        )

        print(f"Response: {response}")

        text = (response.output_text or "").strip()
        print(f"Text: {text}")

        return text
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
        extraction_method = "pypdf2"

        # Fallback for scanned/non-selectable PDFs.
        if not (text or "").strip():
            doc_name = f"{param.get('documentnumber') or document_id}.pdf"
            llm_text = extract_text_with_llm_from_pdf(
                pdf_bytes, document_name=doc_name)
            if llm_text:
                text = llm_text
                extraction_method = "llm_fallback"
            else:
                if "OPENAI_API_KEY" not in os.environ:
                    result["extracted_text_error"] = "No selectable PDF text and OPENAI_API_KEY missing for LLM fallback"
                elif len(pdf_bytes) > LLM_OCR_MAX_PDF_BYTES:
                    result["extracted_text_error"] = (
                        f"No selectable PDF text and PDF too large for LLM fallback "
                        f"({len(pdf_bytes)} bytes > {LLM_OCR_MAX_PDF_BYTES} bytes)"
                    )
                else:
                    result["extracted_text_error"] = "No selectable PDF text and LLM fallback returned empty text"

        result["extracted_text"] = text or ""
        result["extraction_method"] = extraction_method
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
        "Docket Number": "25-00060-UT",
        "caseId": "df8795c2-9498-457d-a78a-0e47e11cf20b",
        "id": "99663142-93a3-476d-bba5-b41a013adf07",
        "documentnumber": "DOC-000246487-26",
        "documentname": "Parties' Request for Guidance in Advance of Show Cause Proceeding",
        "docname": "DOC-000246487-26 [Parties' Request for Guidance in Advance of Show Cause Proceeding]",
        "documenttype": "Request",
        "accesstype": "PUBLIC",
        "audiencetype": "PUBLIC",
        "typeCode": "REQUEST",
        "storageSite": "",
        "storagesitevalue": "",
        "IsLegacy": True,
        "shortdescription": "25-00060-UT 3.27.2026 request for guidance, COS",
        "remarks": "25-00060-UT 3.27.2026 request for guidance, COS",
        "confidential": "No",
        "source": "Online",
        "islinked": "No",
        "islinkedparent": "No",
        "documentRole": "",
        "companyparties": "NEW ENERGY ECONOMY",
        "caseid": "df8795c2-9498-457d-a78a-0e47e11cf20b",
        "casenumber": "25-00060-UT",
        "companypartyid": "4bc37f26-a269-4c21-bb48-8c23fc5fc7a0",
        "company": "PUBLIC SERVICE COMPANY OF NEW MEXICO",
        "filedby": "Mariel Nanasi",
        "filedon": "2026-03-27T19:07:04.853796",
        "fileddate": "2026-03-27T19:07:04.853796",
        "canEdit": "",
        "canAnnotate": "",
        "canRedact": "",
        "canBatesNumber": "",
        "checkout": "No",
        "checkoutby": " ",
        "checkouton": "",
        "hasdeletepermission": "",
        "cancheckout": True,
        "cancheckin": "",
        "docType": "application/pdf",
        "candownload": True,
        "canMakeInternal": "",
        "canpreview": "Yes",
        "contenttype": "application/pdf",
        "entityType": "cms.casex",
        "entityId": "df8795c2-9498-457d-a78a-0e47e11cf20b",
        "isLegacy": False,
        "author": "Mariel Nanasi",
        "canEditLegacyDocument": "",
        "companypartylist": [
            "NEW ENERGY ECONOMY"
        ],
        "fieldchanges": {}
    }
    out = download_and_extract(sample)
    print(out)
    print(json.dumps({k: v for k, v in out.items() if k in (
        "id", "extracted_text", "extracted_text_error")}, indent=2))


if __name__ == "__main__":
    main()

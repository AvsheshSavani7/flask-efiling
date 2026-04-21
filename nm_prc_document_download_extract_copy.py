#!/usr/bin/env python3
"""
NM PRC e360 Document Download and Text Extraction
=================================================
Uses the NM PRC e360 API to:
1. GET download document by documentId (without annotation)
2. Extract text from PDF
3. Return param object with extracted text added

API flow:
  - GET https://e360.prc.nm.gov/core/api/Document/v1/{documentId}/download?type=WITHOUT_ANNOTATION
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

# e360 API endpoint
BASE_URL = "https://e360.prc.nm.gov/core"
DOWNLOAD_URL = f"{BASE_URL}/api/Document/v1"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCIgOiAiSldUIiwia2lkIiA6ICJ0UkdsVGZXYkUySzE3U1RreDgxcFJQZjdHS0dxUVN4RzFGaXl6ODdQaU5RIn0.eyJleHAiOjE3NzY3Nzk3MDcsImlhdCI6MTc3Njc3NzkwNywiYXV0aF90aW1lIjoxNzc2Nzc3OTAzLCJqdGkiOiIxN2UwYjU4Yi0zNzQ3LTQ5MjUtYTUwZi0zOTExNzFjMmRjMzEiLCJpc3MiOiJodHRwczovL2UzNjAucHJjLm5tLmdvdi9hdXRoL3JlYWxtcy9ubS1wcmMtcHVibGljIiwiYXVkIjoiYWNjb3VudCIsInN1YiI6IjBhZjdhODhmLTJkNTktNDVmMS1hZWI0LWY2NmVmYjI0ZTAxOSIsInR5cCI6IkJlYXJlciIsImF6cCI6Im5tLXByYy1wdWJsaWMtY2xpZW50Iiwibm9uY2UiOiI1YTNlMmQ4YThlNGMwYThhYWM5MWZhZWI0NWQwNDdmODJic1dHMVV5eiIsInNlc3Npb25fc3RhdGUiOiI2ZTZjNDE1NS0zNjQ1LTQ1MTctODdjNC1mMWJhZjdjNDFlZjMiLCJhY3IiOiIxIiwiYWxsb3dlZC1vcmlnaW5zIjpbImh0dHBzOi8vZTM2MC5wcmMubm0uZ292L3BvcnRhbC9wdWJsaWMiLCJodHRwczovL2UzNjAucHJjLm5tLmdvdiJdLCJyZWFsbV9hY2Nlc3MiOnsicm9sZXMiOlsiZGVmYXVsdC1yb2xlcy1ubS1wcmMtcHVibGljIiwib2ZmbGluZV9hY2Nlc3MiLCJ1bWFfYXV0aG9yaXphdGlvbiJdfSwicmVzb3VyY2VfYWNjZXNzIjp7ImFjY291bnQiOnsicm9sZXMiOlsibWFuYWdlLWFjY291bnQiLCJtYW5hZ2UtYWNjb3VudC1saW5rcyIsInZpZXctcHJvZmlsZSJdfX0sInNjb3BlIjoib3BlbmlkIHByb2ZpbGUgZW1haWwiLCJzaWQiOiI2ZTZjNDE1NS0zNjQ1LTQ1MTctODdjNC1mMWJhZjdjNDFlZjMiLCJzY3AiOiJhY2Nlc3NfYXNfdXNlciIsInVwbiI6ImthdXNoYWxAaHlwZXJpb250ZWNobm9sb2dpZXMuYWkiLCJ1bmlxdWVfbmFtZSI6ImthdXNoYWxAaHlwZXJpb250ZWNobm9sb2dpZXMuYWkiLCJFbWFpbCI6ImthdXNoYWxAaHlwZXJpb250ZWNobm9sb2dpZXMuYWkiLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZSwibmFtZSI6IkthdXNoYWwgRGV2YW5pIiwiQ05hbWUiOiJrYXVzaGFsQGh5cGVyaW9udGVjaG5vbG9naWVzLmFpIiwicHJlZmVycmVkX3VzZXJuYW1lIjoia2F1c2hhbEBoeXBlcmlvbnRlY2hub2xvZ2llcy5haSIsImdpdmVuX25hbWUiOiJLYXVzaGFsIiwiZmFtaWx5X25hbWUiOiJEZXZhbmkiLCJlbWFpbCI6ImthdXNoYWxAaHlwZXJpb250ZWNobm9sb2dpZXMuYWkifQ.O7A7EfDILfcoOXRynrzuybOLb79uJovEqN7dD6m5-u0El15NLtW_fnM_Kn-5b7BdR8Z_WPBbtLHSaQGgal-w0wyY-PLZ1hikxVN2rhVMdMp1VvNUV45PXlaFFoMoYKXbkASeqAC67lwpL7MJKnIvghr6Vr_OoKMN9ElZ4OiZwkyCje2nfG5DSW-WVoyoDRT5oR8n68tSl8ThLQY9sWrIpWeWRgW2Ifxhfmjw2oiMR220ncU1LZ1o8WA9ZixFQspVOtDxD6gIOu7aSBrlvGATkfBHVDOC5QzzlWlE6aTVYoUBj2oBKjeUsySHx_Trgb9sJyXv3h-oqKc65-15l3dfXQ",
    "Connection": "keep-alive",
    "Referer": "https://e360.prc.nm.gov/portal/public/",
}

DEFAULT_COOKIES = {
    "sessionId": "3e9af0b6-4a99-4b0e-bbba-b43300dbf251",
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


def download_document(session: requests.Session, document_id: str) -> bytes:
    """
    GET download document directly by documentId.

    Args:
        session: requests Session
        document_id: The document id (param["id"])

    Returns:
        Raw document bytes (e.g. PDF)

    Raises:
        requests.RequestException: If download fails
    """
    url = f"{DOWNLOAD_URL}/{document_id}/download?type=WITHOUT_ANNOTATION"
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


def temp_download_and_extract(param: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    """
    Download document via e360 API, extract text, and return param with extracted text.

    Uses direct download endpoint:
      GET /api/Document/v1/{documentId}/download?type=WITHOUT_ANNOTATION

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
        session.cookies.update(DEFAULT_COOKIES)

    try:
        pdf_bytes = download_document(session, str(document_id))
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
        "id": "aa8af340-fc57-470a-b833-b43201680053",
        "documentnumber": "DOC-000265923-26",
        "documentname": " Response Testimony and Exhibits of Bruce Throne",
        "docname": "DOC-000265923-26 [ Response Testimony and Exhibits of Bruce Throne]",
        "documenttype": "Testimony",
        "accesstype": "PUBLIC",
        "audiencetype": "PUBLIC",
        "typeCode": "TESTIMONIES",
        "storageSite": "",
        "storagesitevalue": "",
        "IsLegacy": True,
        "shortdescription": "25-00060-UT 4.20.2026 Response Testimony and Exhib...",
        "remarks": "25-00060-UT 4.20.2026 Response Testimony and Exhib...",
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
        "filedon": "2026-04-20T21:51:34.803273",
        "fileddate": "2026-04-20T21:51:34.803273",
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
    out = temp_download_and_extract(sample)
    print(out)
    print(json.dumps({k: v for k, v in out.items() if k in (
        "id", "extracted_text", "extracted_text_error")}, indent=2))


if __name__ == "__main__":
    main()

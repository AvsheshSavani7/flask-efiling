from flask import Flask, request, jsonify
from flask_cors import CORS
from mn_doc_scraper import parse_mn_documents
from mn_scraper import scrape_mn_documents
from demo4 import fetch_with_playwright_2captcha
from puc_scraper import fetch_with_playwright_2captcha_puc
from docket_entry_analyzer import analyze_docket_entry
from docket_manager import get_dockets
from fcc_html_scraper import process_fcc_scraper
from mergers_manager import get_all_mergers
from nm_prc_service import login_nm_prc, get_html_from_nm_prc, extract_pdf_text_from_nm_prc
from nm_prc_document_download_extract import download_and_extract
from nm_prc_document_download_extract_copy import temp_download_and_extract
from cade_public_notice_brazil import main as cade_main
from cade_brazil_update_monitor import monitor_brazil_deals
from new_samr_public_notice_db import main as new_samr_public_notice_main
from new_samr_conditional_approval_db import main as new_samr_conditional_approval_main
from new_samr_unconditional_approval_db import main as new_samr_unconditional_approval_main
from uk_cma_mergers_scraper_atom import main as uk_cma_main
from new_uk_cma_mergers_scraper_atom import main as new_uk_cma_main
from new_uk_cma_mergers_update_monitor import main as new_uk_cma_update_monitor_main
from bundeskartellamt_scraper import main as bundeskartellamt_main
from bundeskartellamt_initial_proxy import main as bundeskartellamt_initial_proxy_main
from bundeskartellamt_update_monitor import main as bundeskartellamt_update_monitor_main
from bundeskartellamt_press_release import main as bundeskartellamt_press_release_main
from ec_case_register import run_ec_case_register as ec_case_register_main
from fs_case_register import run_fs_case_register as fs_case_register_main
from accc_acquisitions import main as accc_acquisitions_main
from accc_case_update_monitor import process_accc_case_updates
from accc_cases_register import run_accc_cases_register
from cade_cases_register import run_cade_cases_register
from cade_cases_update_monitor import process_brazil_cases_updates
from accc_cases_update_monitor import process_accc_cases_updates
from ftc_early_termination_scraper import main as ftc_early_termination_main
from nz_comcom_case_register import main as nz_comcom_case_register_main
from nz_comcom_case_register_to_db import run as nz_comcom_case_register_to_db_run
from competition_bureau_canada_mergers import main as competition_bureau_canada_main
from canada_competition_bureau_case_update_monitor import process_canada_case_updates
from canada_cases_register import run_canada_cases_register
from canada_cases_update_monitor import process_canada_cases_updates
from nz_comcom_case_update_monitor import process_nz_case_updates
from nz_cases_update_monitor import run as nz_cases_update_monitor_run
from mt_psc_scraper import scrape_mt_psc
from ne_psc_scraper import scrape_ne_psc
from sd_puc_scraper import scrape_sd_puc
from mongodb_connection import init_mongodb_connection, close_mongodb_connection, is_connected
import logging
import os
import asyncio
import socket
import subprocess
import platform
import datetime
import atexit
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# Configure logging
logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

for _pymongo_logger in ["pymongo", "pymongo.monitoring", "pymongo.serverSelection",
                        "pymongo.connection", "pymongo.command", "pymongo.topology"]:
    logging.getLogger(_pymongo_logger).setLevel(logging.WARNING)


app = Flask(__name__)

# Initialize MongoDB connection on server startup
logger.info("Initializing MongoDB connection...")
mongodb_success, mongodb_message = init_mongodb_connection()
if mongodb_success:
    logger.info(f"✓ {mongodb_message}")
else:
    logger.warning(f"⚠ {mongodb_message} - Some features may not work")

# Register cleanup function to close MongoDB connection on server shutdown
atexit.register(close_mongodb_connection)

# Configure CORS to allow requests from http://localhost:8080
CORS(app, origins=["http://localhost:8080",
     "https://rag-summary-fe.onrender.com"])

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

# --- Memory-safe background task infrastructure ---
scraper_pool = ThreadPoolExecutor(max_workers=5)
_running_tasks = {}
_running_lock = Lock()


def _run_and_cleanup(task_name, func):
    """Wrapper that removes the task from the registry when done."""
    try:
        func()
    except Exception:
        logger.exception(f"Background task '{task_name}' failed")
    finally:
        with _running_lock:
            _running_tasks.pop(task_name, None)


def submit_unique_task(task_name, func):
    """Submit *func* only if *task_name* is not already running.
    Returns (submitted: bool, message: str).
    """
    with _running_lock:
        future = _running_tasks.get(task_name)
        if future and not future.done():
            return False, f"{task_name} is already running"
        future = scraper_pool.submit(_run_and_cleanup, task_name, func)
        _running_tasks[task_name] = future
        return True, f"{task_name} started in background"


@app.route('/')
def home():
    """Home endpoint with API information"""
    return jsonify({
        "message": "Minnesota E-filing Scraper API",
        "endpoints": {
            "/scrape": "POST - Scrape documents for a given URL",
            "/puc-scrape": "POST - Scrape PUC documents for a given URL",
            "/fcc-scraper": "POST - Check for new FCC filings and scrape HTML",
            "/proxy-check": "POST - Check if a proxy port is open",
            "/analyze-docket": "POST - Analyze docket entry with tier 2 and tier 3 analysis",
            "/dockets": "GET - Fetch docket entries with pagination (query params: docket_type, docket_number, page, limit, sort_field, sort_order)",
            "/mergers": "GET - Get all merger records from MongoDB",
            "/nm-prc-fetch": "POST - Fetch HTML from NM PRC eDocket system (requires authentication)",
            "/nm-prc-login": "POST - Login to NM PRC eDocket system and save cookies",
            "/nm-prc-get-html": "POST - Fetch HTML from protected NM PRC eDocket URL (requires login first)",
            "/nm-prc-extract-pdf": "POST - Fetch PDF from protected NM PRC eDocket URL and extract text (requires login first)",
            "/nm-prc-download-extract": "POST - Download NM PRC e360 document by document param and extract text",
            "/brazil-scraper": "GET - Scrape CADE public notices and match with deals (date range: yesterday to today, query param: headless)",
            "/cade-brazil-monitor": "GET - Monitor existing Brazil deals for new table records updates (query param: headless)",
            "/new-samr-public-scraper": "GET - Scrape SAMR China public notices and match with deals (query param: headless)",
            "/new-samr-conditional-scraper": "GET - Scrape SAMR China conditional approval notices and match with deals (query params: headless, use_html)",
            "/new-samr-unconditional-scraper": "GET - Scrape SAMR China unconditional approval notices and match with deals (query params: headless, use_html)",
            "/uk-cma-scraper": "GET - Scrape UK CMA merger cases and match with deals (query params: use_html)",
            "/bundeskartellamt-scraper": "GET - Scrape Bundeskartellamt German merger cases and match with deals",
            "/bundeskartellamt-initial": "GET - Scrape Bundeskartellamt Laufende Verfahren (initial filing) — new cases to german_cases collection",
            "/bundeskartellamt-update-monitor": "GET - Monitor open german_cases for changes, match deals, send update emails",
            "/bundeskartellamt-press-release": "GET - Scrape Bundeskartellamt press releases and match with deals",
            "/new-ec-case-register": "GET - Filter and match EC merger cases with deals",
            "/new-fs-case-register": "GET - Filter and match EC Foreign Subsidies cases with deals",
            "/new-ec-case-update-monitor": "GET - Monitor EC merger cases for updates and send email notifications",
            "/new-fs-case-update-monitor-new": "GET - Monitor EC Foreign Subsidies cases for updates and send email notifications",
            "/new-accc-cases-register": "GET - Scrape ACCC acquisitions and match with deals",
            "/new-cade-cases-register": "GET - Scrape CADE Brazil public notices and store in brazil_cases collection (query params: headless, days)",
            "/new-cade-cases-update-monitor": "GET - Monitor brazil_cases for updates and send email notifications (query param: headless)",
            "/new-accc-cases-update-monitor": "GET - Monitor ACCC acquisition cases for updates and send email notifications",
            "/ftc-early-termination-scraper": "GET - Scrape FTC early termination notices and match with deals",
            "/nz-comcom-case-register": "GET - Scrape NZ ComCom case register and match with deals",
            "/nz-comcom-case-update-monitor": "GET - Monitor NZ ComCom cases for updates and send email notifications",
            "/nz-comcom-case-register-to-db": "GET - Scrape NZ ComCom case register and save new records to nz_cases collection",
            "/nz-cases-update-monitor": "GET - Monitor nz_cases collection for updates, match to deals, and send emails",
            "/competition-bureau-canada-scraper": "GET - Scrape Canada Competition Bureau merger reviews and match with deals",
            "/canada-competition-bureau-case-update-monitor": "GET - Monitor Canada Competition Bureau cases for updates and send email notifications",
            "/new-canada-cases-register": "GET - Register new Canada Competition Bureau cases into canada_cases collection",
            "/new-canada-cases-update-monitor": "GET - Monitor canada_cases collection for updates and send email notifications",
            "/system-check": "GET - Check system dependencies for document extraction",
            "/health": "GET - Health check endpoint"
        },
        "usage": {
            "POST /scrape": {
                "body": {
                    "url": "string (optional, default: https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198)",
                    "wait_time": "integer (optional, default: 20)",
                    "type": "string (optional, 'html' or 'document', default: 'html')"
                },
                "description": {
                    "html": "Returns HTML content of the scraped page",
                    "document": "Downloads and extracts text content from documents (PDF, Word, etc.)"
                }
            },
            "POST /proxy-check": {
                "body": {
                    "host": "string (optional, default: 95.135.111.121)",
                    "port": "integer (optional, default: 45237)",
                    "timeout": "integer (optional, default: 5)"
                },
                "description": "Checks if a proxy port is open and accessible"
            }
        }
    })


@app.route('/scrape/', methods=['POST'])
def scrape_documents_post():
    """Scrape documents using POST request with JSON body"""
    try:
        data = request.get_json() or {}

        wait_time = data.get('wait_time', 30)
        type = data.get('type', 'html')
        url = data.get(
            'url', 'https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198')

        if type == 'html':
            html_content = fetch_with_playwright_2captcha(url)
            return jsonify({
                "success": True,
                "url": url,
                "content_length": len(html_content) if html_content else 0,
                "html_content": html_content
            }), 200
        elif type == 'document':
            result = parse_mn_documents(wait_time=wait_time, url=url)
            return jsonify(result), 200 if result.get("success") else 500

    except Exception as e:
        logger.error(f"Error during scraping: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/proxy-check', methods=['POST'])
def proxy_check():
    """Check if a proxy port is open and accessible"""
    try:
        data = request.get_json() or {}

        host = data.get('host', '95.135.111.121')
        port = data.get('port', 45237)
        timeout = data.get('timeout', 5)

        s = socket.socket()
        s.settimeout(timeout)

        try:
            s.connect((host, port))
            s.close()
            return jsonify({
                "success": True,
                "host": host,
                "port": port,
                "timeout": timeout,
                "status": "Proxy port open!",
                "accessible": True
            }), 200
        except Exception as e:
            s.close()
            return jsonify({
                "success": False,
                "host": host,
                "port": port,
                "timeout": timeout,
                "status": f"Proxy port closed or blocked: {str(e)}",
                "accessible": False,
                "error": str(e)
            }), 200

    except Exception as e:
        logger.error(f"Error during proxy check: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/puc-scrape/', methods=['POST'])
def puc_scrape():
    """Scrape PUC documents using POST request with JSON body"""
    try:
        data = request.get_json() or {}

        url = data.get('url')
        wait_time = data.get('wait_time', 30)
        extract_zips = data.get('extract_zips', True)  # Default to True

        if not url:
            return jsonify({
                "success": False,
                "error": "URL is required"
            }), 400

        result = fetch_with_playwright_2captcha_puc(
            url, wait_time, extract_zips=extract_zips)

        # Handle different return types
        if isinstance(result, dict) and "zip_urls" in result:
            # Result includes ZIP extraction info - return simplified structure
            return jsonify({
                "success": True,
                "zip_urls": result.get("zip_urls", []),
                "extracted_files": result.get("extracted_files", []),
                "metadata": result.get("metadata", {})
            }), 200
        else:
            # Just HTML content (when extract_zips=False)
            return jsonify({
                "success": True,
                "html_content": result if result else ""
            }), 200

    except Exception as e:
        logger.error(f"Error during PUC scraping: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/analyze-docket', methods=['POST'])
def analyze_docket():
    """Analyze docket entry with tier 2 and tier 3 analysis"""
    try:
        data = request.get_json() or {}

        doc_number = data.get('doc_number')
        text = data.get('text')
        metadata = data.get('metadata')  # Optional metadata
        test_mode = data.get('test_mode', False)

        if not doc_number:
            return jsonify({
                "success": False,
                "error": "doc_number is required"
            }), 400

        if not text:
            return jsonify({
                "success": False,
                "error": "text is required"
            }), 400

        # Call the analyzer function
        result = analyze_docket_entry(doc_number, text, metadata, test_mode)

        # Check if there was an error
        if "error" in result:
            return jsonify({
                "success": False,
                "error": result["error"],
                "doc_number": doc_number
            }), 500

        # Return only tier2 and tier3 analysis
        response = {
            "success": True,
            "doc_number": doc_number,
            "status": result.get("status"),
            "metadata": result.get("metadata"),
            "tier2_analysis": result.get("tier2_analysis"),
            "tier3_risk_assessment": result.get("tier3_risk_assessment"),
            "comprehensive_summary": result.get("comprehensive_summary") if result.get("comprehensive_summary") else text
        }

        # If it's a skipped entry (already exists), extract tier2 and tier3 from the entry
        if result.get("status") == "skipped" and "entry" in result:
            entry = result["entry"]
            response["tier2_analysis"] = entry.get("tier2_analysis")
            response["tier3_risk_assessment"] = entry.get(
                "tier3_risk_assessment")
            response["metadata"] = entry.get("metadata")
            response["comprehensive_summary"] = entry.get(
                "comprehensive_summary") if entry.get("comprehensive_summary") else text
            response["entry"] = entry  # Include the full entry in the response

        return jsonify(response), 200

    except Exception as e:
        logger.error(f"Error during docket analysis: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/dockets', methods=['GET'])
def fetch_dockets():
    """Fetch docket entries with pagination, filtered by docket_type and/or docket_number, and sorted by specified field"""
    try:
        # Get query parameters
        docket_type = request.args.get('docket_type', None)
        docket_number = request.args.get('docket_number', None)
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 10, type=int)
        sort_field = request.args.get(
            'sort_field', 'date')  # 'date' or 'hash_id'
        sort_order = request.args.get('sort_order', 'asc')  # 'asc' or 'desc'

        # Validate pagination parameters
        if page < 1:
            return jsonify({
                "success": False,
                "error": "Page must be greater than 0"
            }), 400

        if limit < 1:
            return jsonify({
                "success": False,
                "error": "Limit must be greater than 0"
            }), 400

        # Validate sort_field parameter
        if sort_field.lower() not in ['date', 'hash_id']:
            return jsonify({
                "success": False,
                "error": "sort_field must be 'date' or 'hash_id'"
            }), 400

        # Validate sort_order parameter
        if sort_order.lower() not in ['asc', 'desc']:
            return jsonify({
                "success": False,
                "error": "sort_order must be 'asc' (ascending) or 'desc' (descending)"
            }), 400

        # Call the docket manager function
        result = get_dockets(
            docket_type=docket_type,
            docket_number=docket_number,
            page=page,
            limit=limit,
            sort_field=sort_field,
            sort_order=sort_order
        )

        # Return appropriate status code based on result
        status_code = 200 if result.get("success") else 500

        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"Error fetching dockets: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with active scraper visibility"""
    mongodb_status = "connected" if is_connected() else "disconnected"
    with _running_lock:
        active = [k for k, v in _running_tasks.items() if not v.done()]
    return jsonify({
        "status": "healthy",
        "service": "Minnesota E-filing Scraper API",
        "mongodb": mongodb_status,
        "active_scrapers": active,
        "active_count": len(active),
        "pool_max_workers": scraper_pool._max_workers
    }), 200


@app.route('/system-check', methods=['GET'])
def system_check():
    """Check system dependencies for document extraction"""
    checks = {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "antiword_available": False,
        "antiword_version": None,
        "textutil_available": False,
        "python_docx_available": False,
        "pypdf2_available": False,
        "openpyxl_available": False
    }

    # Check antiword
    try:
        result = subprocess.run(
            ["antiword", "-v"],
            capture_output=True,
            text=True,
            timeout=5
        )
        checks["antiword_available"] = True
        checks["antiword_version"] = result.stdout.strip(
        ) or result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as e:
        checks["antiword_error"] = str(e)

    # Check textutil (macOS)
    try:
        result = subprocess.run(
            ["which", "textutil"],
            capture_output=True,
            text=True,
            timeout=5
        )
        checks["textutil_available"] = result.returncode == 0
    except Exception:
        pass

    # Check Python libraries
    try:
        import docx
        checks["python_docx_available"] = True
    except ImportError:
        pass

    try:
        import PyPDF2
        checks["pypdf2_available"] = True
    except ImportError:
        pass

    try:
        import openpyxl
        checks["openpyxl_available"] = True
    except ImportError:
        pass

    # Overall status
    doc_extraction_ready = (
        checks["antiword_available"] or
        checks["textutil_available"] or
        checks["python_docx_available"]
    )

    checks["doc_extraction_ready"] = doc_extraction_ready
    checks["status"] = "ready" if doc_extraction_ready else "limited"

    if not checks["antiword_available"] and checks["os"] == "Linux":
        checks["warning"] = "antiword not installed - old .doc files cannot be extracted on Linux"

    return jsonify(checks), 200


@app.route('/fcc-scraper', methods=['POST'])
def fcc_scraper():
    """
    Check for new FCC filings by comparing document_id with RSS feed items.
    If new records found, scrape HTML from their links.
    If document_id is not provided or is empty string, treats as first entry and scrapes all items.
    """
    try:
        data = request.get_json() or {}

        url = data.get('url')
        document_id = data.get('document_id')
        wait_time = data.get('wait_time', 10)

        # Call the main processing function
        result = process_fcc_scraper(url, document_id, wait_time)

        # Determine HTTP status code based on result
        status_code = 200
        if not result.get("success"):
            if "url is required" in result.get("error", ""):
                status_code = 400
            else:
                status_code = 500

        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"Error in FCC scraper endpoint: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/mergers', methods=['GET'])
def fetch_mergers():
    """Get all merger records from MongoDB"""
    try:
        result = get_all_mergers()

        # Return appropriate status code based on result
        status_code = 200 if result.get("success") else 500

        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"Error fetching mergers: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "data": []
        }), 500


@app.route('/nm-prc-login', methods=['POST'])
def nm_prc_login():
    """
    Login endpoint that authenticates and saves cookies.

    Request body:
    {
        "username": "string (required)",
        "password": "string (required)"
    }

    Returns:
    {
        "success": bool,
        "message": "string",
        "cookies_file": "string",
        "meta_file": "string"
    }
    """
    try:
        data = request.get_json() or {}
        username = data.get('username')
        password = data.get('password')

        result = login_nm_prc(username, password)
        return jsonify(result), 200

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400
    except RuntimeError as e:
        logger.error(f"Login failed: {str(e)}")
        return jsonify({
            "success": False,
            "error": f"Login failed: {str(e)}"
        }), 401
    except Exception as e:
        logger.error(f"Error during login: {str(e)}")
        return jsonify({
            "success": False,
            "error": f"Error during login: {str(e)}"
        }), 500


@app.route('/nm-prc-get-html', methods=['POST'])
def nm_prc_get_html():
    """
    Fetch HTML from a protected NM PRC eDocket URL.

    Request body:
    {
        "target_url": "string (required) - Full URL to fetch"
    }

    Returns:
    {
        "success": bool,
        "html_content": "string",
        "content_length": int
    }
    """
    try:
        data = request.get_json() or {}
        target_url = data.get('target_url')

        result = get_html_from_nm_prc(target_url)
        return jsonify(result), 200

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400
    except FileNotFoundError as e:
        logger.error(f"Cookie file not found: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 401
    except RuntimeError as e:
        logger.error(f"Session error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 401
    except Exception as e:
        logger.error(f"Error fetching HTML: {str(e)}")
        return jsonify({
            "success": False,
            "error": f"Error fetching HTML: {str(e)}"
        }), 500


@app.route('/nm-prc-extract-pdf', methods=['POST'])
def nm_prc_extract_pdf():
    """
    Fetch PDF from a protected NM PRC eDocket URL and extract text.

    Request body:
    {
        "pdf_url": "string (required) - Full URL to the PDF file"
    }

    Returns:
    {
        "success": bool,
        "text": "string",
        "page_count": int,
        "text_length": int
    }
    """
    try:
        data = request.get_json() or {}
        pdf_url = data.get('pdf_url')
        document_id = data.get('document_id')

        result = extract_pdf_text_from_nm_prc(pdf_url, document_id)
        return jsonify(result), 200

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400
    except FileNotFoundError as e:
        logger.error(f"Cookie file not found: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 401
    except RuntimeError as e:
        logger.error(f"Session error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 401
    except Exception as e:
        logger.error(f"Error extracting PDF text: {str(e)}")
        return jsonify({
            "success": False,
            "error": f"Error extracting PDF text: {str(e)}"
        }), 500


@app.route('/nm-prc-download-extract', methods=['POST'])
def nm_prc_download_extract():
    """
    Download NM PRC e360 document via API and extract text.

    Uses e360 APIs: get download token, download document, extract PDF text.
    Request body: document param object (must include "id" as documentId), e.g.:
    {
        "row_number": 2,
        "Docket Number": "24-00266-UT",
        "caseId": "...",
        "id": "29205b89-7d22-402c-a90e-b3e501832893",
        "documentnumber": "DOC-000180445-26",
        "documentname": "...",
        ...
    }

    Returns the same param object with "extracted_text" added (and optionally
    "extracted_text_error" on failure).
    """
    try:
        data = request.get_json() or {}
        if not data.get("id"):
            return jsonify({
                "success": False,
                "error": "Request body must include 'id' (documentId)"
            }), 400

        result = download_and_extract(data)
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error in nm-prc-download-extract: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/temp-nm-prc-download-extract', methods=['POST'])
def temp_nm_prc_download_extract():
    """
    Download NM PRC e360 document via API and extract text.

    Uses e360 APIs: get download token, download document, extract PDF text.
    Request body: document param object (must include "id" as documentId), e.g.:
    {
        "row_number": 2,
        "Docket Number": "24-00266-UT",
        "caseId": "...",
        "id": "29205b89-7d22-402c-a90e-b3e501832893",
        "documentnumber": "DOC-000180445-26",
        "documentname": "...",
        ...
    }

    Returns the same param object with "extracted_text" added (and optionally
    "extracted_text_error" on failure).
    """
    try:
        data = request.get_json() or {}
        if not data.get("id"):
            return jsonify({
                "success": False,
                "error": "Request body must include 'id' (documentId)"
            }), 400

        result = temp_download_and_extract(data)
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error in nm-prc-download-extract: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/brazil-scraper', methods=['GET'])
def brazil_scraper():
    """
    Scrape CADE public notices and match with deals.
    Date range is hardcoded: yesterday to today.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true") - Run browser in headless mode

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Hardcode date range: yesterday to today
        end_date = datetime.datetime.now()
        start_date = datetime.datetime.now()
        # start_date = end_date - datetime.timedelta(days=1)

        # Get headless parameter from query
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        # Run the scraping process in background thread
        def run_scraper():
            try:
                logger.info(
                    f"Starting CADE scraper in background (date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})")
                result = cade_main(start_date=start_date,
                                   end_date=end_date, headless=headless)
                if result.get("success"):
                    logger.info(
                        f"CADE scraper completed successfully. Found {result.get('total_matched', 0)} matches.")
                else:
                    logger.warning(
                        f"CADE scraper completed with errors: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.error(f"Error in background CADE scraper: {str(e)}")

        submitted, msg = submit_unique_task("brazil-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
            "date_range": {
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d")
            }
        }), 200

    except Exception as e:
        logger.error(f"Error starting CADE scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/cade-brazil-monitor', methods=['GET'])
def brazil_monitor():
    """
    Monitor existing Brazil deals for new table records updates.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true") - Run browser in headless mode

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Get headless parameter from query
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        # Run the monitoring process in background thread
        def run_monitor():
            try:
                logger.info("Starting CADE Brazil deal monitor in background")
                result = monitor_brazil_deals(headless=headless)
                if result.get("success"):
                    logger.info(
                        f"CADE Brazil monitor completed. Checked {result.get('total_deals_checked', 0)} deals, "
                        f"found updates in {result.get('deals_with_updates', 0)} deals "
                        f"({result.get('total_new_records', 0)} new records total).")
                else:
                    logger.warning(
                        f"CADE Brazil monitor completed with errors: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.error(
                    f"Error in background CADE Brazil monitor: {str(e)}")

        submitted, msg = submit_unique_task("cade-brazil-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting CADE Brazil monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-samr-public-scraper', methods=['GET'])
def new_samr_public_scraper():
    """
    Scrape SAMR China public notices and match with deals.
    Extracts records from SAMR website with cutoff date filtering.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true") - Run browser in headless mode

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Get query parameters
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        # Run the scraping process in background thread
        def run_scraper():
            try:
                logger.info(
                    f"Starting new SAMR public notice scraper in background (headless={headless})")
                result = new_samr_public_notice_main(headless=headless)
                if result.get("success"):
                    logger.info(
                        f"new SAMR scraper completed successfully. Extracted {result.get('total_extracted', 0)} records, "
                        f"found {result.get('total_matched', 0)} matches.")
                else:
                    logger.warning(
                        f"new SAMR scraper completed with errors: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(f"Error in background new SAMR scraper")

        submitted, msg = submit_unique_task("samr-public-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
            "headless": headless
        }), 200

    except Exception as e:
        logger.error(f"Error starting new SAMR scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-samr-conditional-scraper', methods=['GET'])
def new_samr_conditional_scraper():
    """
    Scrape SAMR China conditional approval notices and match with deals.
    Extracts records from SAMR website with cutoff date filtering.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true") - Run browser in headless mode

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Get query parameters
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        # Run the scraping process in background thread
        def run_scraper():
            try:
                logger.info(
                    f"Starting new SAMR conditional approval scraper in background (headless={headless})")
                result = new_samr_conditional_approval_main(
                    headless=headless)
                if result.get("success"):
                    logger.info(
                        f"new SAMR conditional approval scraper completed successfully. Extracted {result.get('total_extracted', 0)} records, "
                        f"found {result.get('total_matched', 0)} matches.")
                else:
                    logger.warning(
                        f"new SAMR conditional approval scraper completed with errors: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(
                    "Error in background new SAMR conditional approval scraper")

        submitted, msg = submit_unique_task(
            "samr-conditional-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
            "headless": headless
        }), 200

    except Exception as e:
        logger.error(
            f"Error starting new SAMR conditional approval scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-samr-unconditional-scraper', methods=['GET'])
def new_samr_unconditional_scraper():
    """
    Scrape SAMR China unconditional approval notices and match with deals.
    Extracts records from SAMR website with cutoff date filtering.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true") - Run browser in headless mode

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Get query parameters
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        # Run the scraping process in background thread
        def run_scraper():
            try:
                logger.info(
                    f"Starting new SAMR unconditional approval scraper in background (headless={headless})")
                result = new_samr_unconditional_approval_main(
                    headless=headless)
                if result.get("success"):
                    logger.info(
                        f"new SAMR unconditional approval scraper completed successfully. Extracted {result.get('total_extracted', 0)} records, "
                        f"found {result.get('total_matched', 0)} matches.")
                else:
                    logger.warning(
                        f"new SAMR unconditional approval scraper completed with errors: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(
                    "Error in background new SAMR unconditional approval scraper")

        submitted, msg = submit_unique_task(
            "samr-unconditional-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
            "headless": headless
        }), 200

    except Exception as e:
        logger.error(
            f"Error starting new SAMR unconditional approval scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/uk-cma-scraper', methods=['GET'])
def uk_cma_scraper():
    """
    Scrape UK CMA merger cases and match with deals.
    Extracts records from CMA website with cutoff date filtering.
    Process runs in background - returns immediately.

    Query parameters:
        use_html: string (optional, "true" or "false", default: "false") - Extract from existing HTML files instead of scraping

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Get query parameters
        use_html_str = request.args.get('use_html', 'false')
        use_html = use_html_str.lower() in ('true', '1', 'yes')

        # Run the scraping process in background thread
        def run_scraper():
            try:
                logger.info(
                    f"Starting CMA merger cases scraper in background (use_html={use_html})")
                uk_cma_main()
                logger.info(
                    f"CMA merger cases scraper completed successfully.")
            except Exception as e:
                logger.exception("Error in background CMA scraper")

        submitted, msg = submit_unique_task("uk-cma-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
            "use_html": use_html
        }), 200

    except Exception as e:
        logger.error(f"Error starting CMA scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-uk-cma-scraper', methods=['GET'])
def new_uk_cma_scraper():
    """
    Scrape UK CMA open merger cases, match with deals, and store in uk_cma_cases collection.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_scraper():
            try:
                logger.info(
                    "Starting new UK CMA open mergers scraper in background")
                new_uk_cma_main()
                logger.info(
                    "New UK CMA open mergers scraper completed successfully.")
            except Exception as e:
                logger.exception("Error in background new UK CMA scraper")

        submitted, msg = submit_unique_task("new-uk-cma-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting new UK CMA scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-uk-cma-update-monitor', methods=['GET'])
def new_uk_cma_update_monitor():
    """
    Monitor existing open UK CMA cases for updates, detect changes, and send emails.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_monitor():
            try:
                logger.info("Starting new UK CMA update monitor in background")
                new_uk_cma_update_monitor_main()
                logger.info(
                    "New UK CMA update monitor completed successfully.")
            except Exception as e:
                logger.exception(
                    "Error in background new UK CMA update monitor")

        submitted, msg = submit_unique_task(
            "new-uk-cma-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting new UK CMA update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

# German Bundeskartellamt Scraper


@app.route('/bundeskartellamt-scraper', methods=['GET'])
def bundeskartellamt_scraper():
    """
    Scrape Bundeskartellamt German merger cases and match with deals.
    Extracts records from Bundeskartellamt website.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Run the scraping process in background thread
        def run_scraper():
            try:
                logger.info(
                    f"Starting Bundeskartellamt scraper in background")
                result = bundeskartellamt_main()
                if result.get("success"):
                    logger.info(
                        f"Bundeskartellamt scraper completed successfully. Extracted {result.get('total_extracted', 0)} records, "
                        f"found {result.get('total_matched', 0)} matches.")
                else:
                    logger.warning(
                        f"Bundeskartellamt scraper completed with errors: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(
                    "Error in background Bundeskartellamt scraper")

        submitted, msg = submit_unique_task(
            "bundeskartellamt-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting Bundeskartellamt scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/bundeskartellamt-initial', methods=['GET'])
def bundeskartellamt_initial():
    """
    Scrape Bundeskartellamt Laufende Verfahren (initial filing) and match with deals.
    Extracts table from Laufende Verfahren form URL, applies cutoff date, matches with deals via LLM,
    appends to deal german_scrap array with source: initial_filing.
    Process runs in background - returns immediately.
    """
    try:
        def run_scraper():
            try:
                logger.info(
                    "Starting Bundeskartellamt Laufende Verfahren (initial) scraper in background")
                result = bundeskartellamt_initial_proxy_main()
                if result.get("success"):
                    logger.info(
                        f"Bundeskartellamt initial scraper completed. Extracted {result.get('total_extracted', 0)} records, "
                        f"matched {result.get('total_matched', 0)}.")
                else:
                    logger.warning(
                        f"Bundeskartellamt initial scraper failed: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(
                    "Error in background Bundeskartellamt initial scraper")

        submitted, msg = submit_unique_task(
            "bundeskartellamt-initial", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409
        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200
    except Exception as e:
        logger.error(
            f"Error starting Bundeskartellamt initial scraper: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/bundeskartellamt-update-monitor', methods=['GET'])
def bundeskartellamt_update_monitor():
    """
    Monitor open german_cases for changes against the live Bundeskartellamt listing.
    Detects field changes, matches deals via LLM, sends [FRMD]/[FRUD] update emails.
    Process runs in background - returns immediately.
    """
    try:
        def run_monitor():
            try:
                logger.info(
                    "Starting Bundeskartellamt update monitor in background")
                result = bundeskartellamt_update_monitor_main()
                if result.get("success"):
                    logger.info(
                        f"Bundeskartellamt update monitor completed. "
                        f"Checked {result.get('checked', 0)}, "
                        f"updated {result.get('updated', 0)}, "
                        f"emails sent {result.get('email_sent', 0)}.")
                else:
                    logger.warning(
                        f"Bundeskartellamt update monitor failed: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(
                    "Error in background Bundeskartellamt update monitor")

        submitted, msg = submit_unique_task(
            "bundeskartellamt-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409
        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200
    except Exception as e:
        logger.error(
            f"Error starting Bundeskartellamt update monitor: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/bundeskartellamt-press-release', methods=['GET'])
def bundeskartellamt_press_release():
    """
    Scrape Bundeskartellamt press releases and match with deals.
    Extracts press release list from Expertensuche URL, applies cutoff date, matches headline with deals via LLM,
    appends to deal german_scrap array with source: press_release.
    Process runs in background - returns immediately.
    """
    try:
        def run_scraper():
            try:
                logger.info(
                    "Starting Bundeskartellamt press release scraper in background")
                result = bundeskartellamt_press_release_main()
                if result.get("success"):
                    logger.info(
                        f"Bundeskartellamt press release scraper completed. Extracted {result.get('total_extracted', 0)} records, "
                        f"matched {result.get('total_matched', 0)}.")
                else:
                    logger.warning(
                        f"Bundeskartellamt press release scraper failed: {result.get('error', 'Unknown error')}")
            except Exception as e:
                logger.exception(
                    "Error in background Bundeskartellamt press release scraper")

        submitted, msg = submit_unique_task(
            "bundeskartellamt-press-release", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409
        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200
    except Exception as e:
        logger.error(
            f"Error starting Bundeskartellamt press release scraper: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/new-ec-case-register', methods=['GET'])
def new_ec_case_register():
    """
    Filter and match EC merger cases with deals.
    Downloads EC case data, filters by criteria, and matches with MongoDB deals.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Run the filtering process in background thread
        def run_filter():
            try:
                logger.info("Starting EC case filter in background")
                result = ec_case_register_main()
                if result and result.get("success"):
                    logger.info(
                        f"EC case filter completed successfully. Filtered {result.get('total_filtered', 0)} cases, "
                        f"matched {result.get('total_matched', 0)} with deals.")
                else:
                    error_msg = result.get(
                        'error', 'Unknown error') if result else 'No result returned'
                    logger.warning(
                        f"EC case filter completed with errors: {error_msg}")
            except Exception as e:
                logger.exception("Error in background EC case filter")

        submitted, msg = submit_unique_task("ec-case-register", run_filter)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting EC case filter: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-fs-case-register', methods=['GET'])
def fs_case_register():
    """
    Filter and match EC Foreign Subsidies cases with deals.
    Downloads FS case data, filters by caseInstrument + empty decisions + cutoff date,
    matches with MongoDB deals via LLM (company names from caseTitle), saves to fs_ec_cases.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_filter():
            try:
                logger.info(
                    "Starting FS (Foreign Subsidies) case register in background")
                result = fs_case_register_main()
                if result and result.get("success"):
                    logger.info(
                        f"FS case register completed successfully. Filtered {result.get('total_filtered', 0)} cases, "
                        f"matched {result.get('total_matched', 0)} with deals.")
                else:
                    error_msg = result.get(
                        'error', 'Unknown error') if result else 'No result returned'
                    logger.warning(
                        f"FS case register completed with errors: {error_msg}")
            except Exception as e:
                logger.exception("Error in background FS case register")

        submitted, msg = submit_unique_task("fs-case-register", run_filter)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting FS case filter: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-ec-case-update-monitor', methods=['GET'])
def new_ec_case_update_monitor():
    """
    Monitor EC merger cases for updates.
    Compares latest EC case data with stored MongoDB records and sends email notifications for changes.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        from ec_case_update_monitor_new import process_ec_case_updates

        # Run the monitor process in background thread
        def run_monitor():
            try:
                logger.info("Starting EC case update monitor in background")
                process_ec_case_updates()
                logger.info("✅ EC case update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in EC case update monitor")

        submitted, msg = submit_unique_task(
            "ec-case-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting EC case update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/ec-cases-html-register', methods=['GET'])
def ec_cases_html_register():
    """
    Register new EC merger cases via Playwright scraping.
    Scrapes the EC Competition portal, parses detail pages, matches deals via LLM,
    and inserts new cases into ec_cases collection.
    Process runs in background - returns immediately.
    """
    try:
        from new_ec_cases_html import run as ec_cases_html_run, START_URL

        def run_register():
            try:
                logger.info(
                    "Starting EC cases HTML register (Playwright) in background")
                ec_cases_html_run(START_URL, max_pages=None, headed=False)
                logger.info("EC cases HTML register completed successfully")
            except Exception as e:
                logger.exception("Error in EC cases HTML register")

        submitted, msg = submit_unique_task(
            "ec-cases-html-register", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting EC cases HTML register: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/ec-cases-html-update-monitor', methods=['GET'])
def ec_cases_html_update_monitor():
    """
    Monitor open EC merger cases for updates via Playwright scraping.
    Scrapes each open case's detail page, compares with DB record,
    sends email notifications for changes, and closes cases with empty investigation_phase.
    Process runs in background - returns immediately.
    """
    try:
        from new_ec_cases_update_monitor import run as ec_update_monitor_run

        def run_monitor():
            try:
                logger.info(
                    "Starting EC cases HTML update monitor (Playwright) in background")
                ec_update_monitor_run(headed=False, max_cases=None)
                logger.info(
                    "EC cases HTML update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in EC cases HTML update monitor")

        submitted, msg = submit_unique_task(
            "ec-cases-html-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting EC cases HTML update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-fs-case-update-monitor-new', methods=['GET'])
def new_fs_case_update_monitor_new():
    """
    Monitor EC Foreign Subsidies cases for updates.
    Compares latest FS case data with stored fs_ec_cases on deals, sends email notifications for changes.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        from fs_case_update_monitor_new import process_fs_case_updates

        def run_monitor():
            try:
                logger.info(
                    "Starting FS (Foreign Subsidies) case update monitor in background")
                process_fs_case_updates()
                logger.info("✅ FS case update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in FS case update monitor")

        submitted, msg = submit_unique_task(
            "fs-case-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting FS case update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-fs-cases-html-register', methods=['GET'])
def fs_cases_html_register():
    """
    Register new EC Foreign Subsidies cases via Playwright scraping.
    Scrapes the EC Competition portal (FS instrument), parses detail pages,
    matches deals via LLM, and inserts new cases into fs_cases collection.
    Process runs in background - returns immediately.
    """
    try:
        from new_fs_cases_html import run as fs_cases_html_run, START_URL

        def run_register():
            try:
                logger.info(
                    "Starting FS cases HTML register (Playwright) in background")
                fs_cases_html_run(START_URL, max_pages=None, headed=False)
                logger.info("FS cases HTML register completed successfully")
            except Exception as e:
                logger.exception("Error in FS cases HTML register")

        submitted, msg = submit_unique_task(
            "fs-cases-html-register", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting FS cases HTML register: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-fs-cases-html-update-monitor', methods=['GET'])
def fs_cases_html_update_monitor():
    """
    Monitor open EC Foreign Subsidies cases for updates via Playwright scraping.
    Scrapes each open case's detail page, compares with DB record,
    sends email notifications for changes, and closes cases with real last_decision_date.
    Process runs in background - returns immediately.
    """
    try:
        from new_fs_cases_html_update_monitor import run as fs_update_monitor_run

        def run_monitor():
            try:
                logger.info(
                    "Starting FS cases HTML update monitor (Playwright) in background")
                fs_update_monitor_run(headed=False, max_cases=None)
                logger.info(
                    "FS cases HTML update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in FS cases HTML update monitor")

        submitted, msg = submit_unique_task(
            "fs-cases-html-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting FS cases HTML update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/accc-acquisitions-scraper', methods=['GET'])
def accc_acquisitions_scraper():
    """
    Scrape ACCC acquisitions and match with deals.
    Fetches latest ACCC acquisition records and matches them with deals in MongoDB.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Run the scraper process in background thread
        def run_scraper():
            try:
                logger.info("Starting ACCC acquisitions scraper in background")
                accc_acquisitions_main()
                logger.info(
                    "✅ ACCC acquisitions scraper completed successfully")
            except Exception as e:
                logger.exception("Error in ACCC acquisitions scraper")

        submitted, msg = submit_unique_task(
            "accc-acquisitions-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting ACCC acquisitions scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/ftc-early-termination-scraper', methods=['GET'])
def ftc_early_termination_scraper():
    """
    Scrape FTC early termination notices and match with deals.
    Fetches page 0 and 1 of FTC legal library early termination notices,
    filters by current date, matches with deals via LLM, saves to deals and sends emails.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_scraper():
            try:
                logger.info(
                    "Starting FTC early termination scraper in background")
                ftc_early_termination_main()
                logger.info(
                    "✅ FTC early termination scraper completed successfully")
            except Exception as e:
                logger.exception("Error in FTC early termination scraper")

        submitted, msg = submit_unique_task(
            "ftc-early-termination-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting FTC early termination scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/competition-bureau-canada-scraper', methods=['GET'])
def competition_bureau_canada_scraper():
    """
    Scrape Canada Competition Bureau merger reviews and match with deals.
    Scrapes the Competition Bureau Canada report of merger reviews table,
    matches party names with deals via LLM, saves matched cases to MongoDB
    under 'canada_competition_bureau_cases', sends email notifications for
    matched and USA-related cases, and writes a JSON backup file.

    Process runs in a background thread – this endpoint returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_scraper():
            try:
                logger.info(
                    "Starting Canada Competition Bureau scraper in background")
                competition_bureau_canada_main()
                logger.info(
                    "✅ Canada Competition Bureau scraper completed successfully")
            except Exception as e:
                logger.exception("Error in Canada Competition Bureau scraper")

        submitted, msg = submit_unique_task(
            "competition-bureau-canada-scraper", run_scraper)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(
            f"Error starting Canada Competition Bureau scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/canada-competition-bureau-case-update-monitor', methods=['GET'])
def canada_competition_bureau_case_update_monitor():
    """
    Monitor Canada Competition Bureau cases for updates.
    Fetches current report HTML, compares with stored canada_competition_bureau_cases
    on deals; if concluded_date, industry, or outcome changed, sends email and updates DB.
    Process runs in background - returns immediately.
    """
    try:
        def run_monitor():
            try:
                logger.info(
                    "Starting Canada Competition Bureau case update monitor in background")
                process_canada_case_updates()
                logger.info(
                    "✅ Canada Competition Bureau case update monitor completed")
            except Exception as e:
                logger.exception(
                    "Error in Canada Competition Bureau case update monitor")

        submitted, msg = submit_unique_task(
            "canada-competition-bureau-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(
            f"Error starting Canada Competition Bureau case update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-canada-cases-register', methods=['GET'])
def canada_cases_register():
    """
    Register new Canada Competition Bureau cases into canada_cases collection.
    Scrapes the Competition Bureau report, filters by 3-day cutoff,
    matches with deals via LLM, checks USA-relation for unmatched cases,
    and sends email notifications for matched and USA-related cases.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_register():
            try:
                logger.info("Starting Canada cases register in background")
                run_canada_cases_register()
                logger.info("✅ Canada cases register completed successfully")
            except Exception as e:
                logger.exception("Error in Canada cases register")

        submitted, msg = submit_unique_task(
            "canada-cases-register", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting Canada cases register: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-canada-cases-update-monitor', methods=['GET'])
def canada_cases_update_monitor():
    """
    Monitor canada_cases collection for updates and send email notifications.
    Fetches fresh Competition Bureau data, compares with stored cases,
    attempts deal matching for cases without deal_id, and sends rich HTML emails.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_monitor():
            try:
                logger.info(
                    "Starting Canada cases update monitor in background")
                process_canada_cases_updates()
                logger.info("✅ Canada cases update monitor completed")
            except Exception as e:
                logger.exception("Error in Canada cases update monitor")

        submitted, msg = submit_unique_task(
            "canada-cases-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting Canada cases update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/accc-case-update-monitor', methods=['GET'])
def accc_case_update_monitor():
    """
    Monitor ACCC acquisition cases for updates.
    Checks existing ACCC cases in MongoDB for changes in:
    - Acquisition status
    - Stage
    - Determination publication date
    - ACCC Determination
    Sends email notifications when changes are detected.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        # Run the monitor process in background thread
        def run_monitor():
            try:
                logger.info("Starting ACCC case update monitor in background")
                process_accc_case_updates()
                logger.info(
                    "✅ ACCC case update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in ACCC case update monitor")

        submitted, msg = submit_unique_task(
            "accc-case-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting ACCC case update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-accc-cases-register', methods=['GET'])
def accc_cases_register_endpoint():
    """
    Scrape ACCC Acquisitions Register (Under assessment) and store cases in
    the 'accc_cases' collection (no deal matching).
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_register():
            try:
                logger.info(
                    "Starting ACCC cases register scraper in background")
                run_accc_cases_register(test_mode=False)
                logger.info(
                    "✅ ACCC cases register scraper completed successfully")
            except Exception as e:
                logger.exception("Error in ACCC cases register scraper")

        submitted, msg = submit_unique_task(
            "accc-cases-register", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting ACCC cases register scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-cade-cases-register', methods=['GET'])
def cade_cases_register_endpoint():
    """
    Scrape CADE Brazil public notices for a date range and store all records
    in the 'brazil_cases' collection. Matched and USA-related records get
    table extraction and email notifications.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true")
        days: int (optional, default: 10) - Number of days back from today

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        days = int(request.args.get('days', '10'))
        end_date = datetime.datetime.now()
        start_date = end_date - datetime.timedelta(days=days)

        def run_register():
            try:
                logger.info(
                    f"Starting CADE cases register scraper in background "
                    f"(date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})"
                )
                run_cade_cases_register(
                    start_date=start_date,
                    end_date=end_date,
                    headless=headless,
                    test_mode=False,
                )
                logger.info(
                    "✅ CADE cases register scraper completed successfully")
            except Exception as e:
                logger.exception("Error in CADE cases register scraper")

        submitted, msg = submit_unique_task(
            "cade-cases-register", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
            "date_range": {
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d"),
            },
        }), 200

    except Exception as e:
        logger.error(f"Error starting CADE cases register scraper: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-cade-cases-update-monitor', methods=['GET'])
def cade_cases_update_monitor_endpoint():
    """
    Monitor all records in 'brazil_cases' for updates (type, interessados,
    table_records, historico_records). Sends emails for deal-linked and
    USA-related changes.
    Process runs in background - returns immediately.

    Query parameters:
        headless: string (optional, "true" or "false", default: "true")

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        headless_str = request.args.get('headless', 'true')
        headless = headless_str.lower() in ('true', '1', 'yes')

        def run_monitor():
            try:
                logger.info("Starting CADE cases update monitor in background")
                process_brazil_cases_updates(headless=headless)
                logger.info(
                    "✅ CADE cases update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in CADE cases update monitor")

        submitted, msg = submit_unique_task(
            "cade-cases-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running",
        }), 200

    except Exception as e:
        logger.error(f"Error starting CADE cases update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-accc-cases-update-monitor', methods=['GET'])
def accc_cases_update_monitor_endpoint():
    """
    Monitor ACCC cases stored in 'accc_cases' for updates.
    Compares acquisition_status, type, effective_notification_date, status,
    about_the_acquisition, and decisions_and_key_events against the live
    ACCC detail pages; sends diff emails and updates MongoDB.
    Process runs in background - returns immediately.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_monitor():
            try:
                logger.info(
                    "Starting ACCC cases update monitor in background")
                process_accc_cases_updates()
                logger.info(
                    "✅ ACCC cases update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in ACCC cases update monitor")

        submitted, msg = submit_unique_task(
            "accc-cases-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting ACCC cases update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/nz-comcom-case-register', methods=['GET'])
def nz_comcom_case_register():
    """
    Scrape NZ ComCom case register and match with deals.
    Fetches cases from the NZ Commerce Commission case register, matches with deals in MongoDB,
    saves to nz_cases, and sends email notifications. Process runs in background.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_register():
            try:
                logger.info("Starting NZ ComCom case register in background")
                nz_comcom_case_register_main()
                logger.info("✅ NZ ComCom case register completed successfully")
            except Exception as e:
                logger.exception("Error in NZ ComCom case register")

        submitted, msg = submit_unique_task(
            "nz-comcom-case-register", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting NZ ComCom case register: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/nz-comcom-case-update-monitor', methods=['GET'])
def nz_comcom_case_update_monitor():
    """
    Monitor NZ ComCom cases for updates.
    Checks existing nz_cases in MongoDB for changes in case details, timeline,
    documents, and updates_media. Sends email notifications when changes are detected.
    Process runs in background.

    Returns:
    {
        "success": bool,
        "message": "string",
        "status": "string"
    }
    """
    try:
        def run_monitor():
            try:
                logger.info(
                    "Starting NZ ComCom case update monitor in background")
                process_nz_case_updates()
                logger.info(
                    "✅ NZ ComCom case update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in NZ ComCom case update monitor")

        submitted, msg = submit_unique_task(
            "nz-comcom-case-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting NZ ComCom case update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-nz-comcom-case-register-to-db', methods=['GET'])
def new_nz_comcom_case_register_to_db_endpoint():
    """
    Scrape NZ ComCom case register (Open, open_date=last week) and save new records
    into the 'nz_cases' MongoDB collection (dedupe by case_number).
    Process runs in background.
    """
    try:
        def run_register():
            try:
                logger.info(
                    "Starting NZ ComCom case register → nz_cases in background")
                nz_comcom_case_register_to_db_run()
                logger.info(
                    "✅ NZ ComCom case register → nz_cases completed successfully")
            except Exception as e:
                logger.exception(
                    "Error in NZ ComCom case register to nz_cases")

        submitted, msg = submit_unique_task(
            "nz-comcom-case-register-to-db", run_register)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(
            f"Error starting NZ ComCom case register to nz_cases: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/new-nz-cases-update-monitor', methods=['GET'])
def new_nz_cases_update_monitor_endpoint():
    """
    Monitor cases stored in the 'nz_cases' collection for updates.
    Fetches each case detail_url, detects changes (case_details, timeline, documents, updates_media, etc.),
    optionally matches to deals via LLM, sends email notifications, and updates the nz_cases record.
    Process runs in background.
    """
    try:
        def run_monitor():
            try:
                logger.info("Starting nz_cases update monitor in background")
                nz_cases_update_monitor_run()
                logger.info("✅ nz_cases update monitor completed successfully")
            except Exception as e:
                logger.exception("Error in nz_cases update monitor")

        submitted, msg = submit_unique_task(
            "nz-cases-update-monitor", run_monitor)
        if not submitted:
            return jsonify({"success": False, "error": msg, "status": "already_running"}), 409

        return jsonify({
            "success": True,
            "message": msg,
            "status": "running"
        }), 200

    except Exception as e:
        logger.error(f"Error starting nz_cases update monitor: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/mt-psc-scraper', methods=['GET', 'POST'])
def mt_psc_scraper_endpoint():
    """
    Scrape Montana PSC REDDI docket filings via Playwright + OKTA SSO.

    GET params or POST JSON body:
        docket_number: Docket number (default: 2025.10.078)
        case_id: REDDI Case ID (default: DCKT-3556)
        last_id: Watermark (e.g. FIL-38222_DOC-69608). Only new items before this are processed.
        username: OKTA username (or set MT_PSC_USERNAME env var)
        password: OKTA password (or set MT_PSC_PASSWORD env var)
        headless: Run headless (default: true)
    """
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
        else:
            data = request.args.to_dict()

        docket_number = data.get("docket_number", "2025.10.078")
        case_id = data.get("case_id", "DCKT-3556")
        last_id = data.get("last_id")
        username = data.get("username") or os.getenv("MT_PSC_USERNAME", "")
        password = data.get("password") or os.getenv("MT_PSC_PASSWORD", "")
        headless = str(data.get("headless", "true")).lower() != "false"
        row_number = data.get("row_number", None)

        if not username or not password or not row_number or not last_id:
            return jsonify({
                "success": False,
                "error": "Username and password required. Pass in request body or set MT_PSC_USERNAME/MT_PSC_PASSWORD env vars. Also, row_number and last_id are required."
            }), 400

        logger.info(
            f"Starting MT PSC scraper for docket {docket_number}, "
            f"case {case_id}, last_id={last_id}"
        )
        result = scrape_mt_psc(
            docket_number=docket_number,
            case_id=case_id,
            last_id=last_id,
            username=username,
            password=password,
            headless=headless,
            row_number=row_number,
        )
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error starting MT PSC scraper: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/ne-psc-scraper', methods=['GET', 'POST'])
def ne_psc_scraper_endpoint():
    """
    Scrape Nebraska PSC Order Search via Playwright.

    GET params or POST JSON body:
        docket_number: Docket number (required, e.g. 128)
        row_number: Row number for batch tracking (required)
        last_pdf_url: Watermark — PDF URL of the last processed record. Only newer records are processed. (required)
        department: Department dropdown value (default: Natural_Gas)
        from_date: Start date MM/DD/YYYY (default: 2 days ago)
        to_date: End date MM/DD/YYYY (default: today)
        division: Division prefix filter (optional)
        headless: Run headless (default: true)
    """
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
        else:
            data = request.args.to_dict()

        docket_number = data.get("docket_number")
        row_number = data.get("row_number")
        last_pdf_url = data.get("last_pdf_url")
        from_days = data.get("from_days", 2)
        department = data.get("department", "Natural_Gas")
        from datetime import datetime, timedelta
        today = datetime.now()
        default_from = (today - timedelta(days=from_days)).strftime("%m/%d/%Y")
        default_to = today.strftime("%m/%d/%Y")
        from_date = data.get("from_date", default_from)
        to_date = data.get("to_date", default_to)
        headless = str(data.get("headless", "true")).lower() != "false"

        if not docket_number or not row_number or not last_pdf_url:
            return jsonify({
                "success": False,
                "error": "docket_number, row_number, and last_pdf_url are required."
            }), 400

        logger.info(
            f"Starting NE PSC scraper for department={department}, "
            f"docket={docket_number}, last_pdf_url={last_pdf_url}"
        )
        result = scrape_ne_psc(
            docket_number=docket_number,
            from_date=from_date,
            to_date=to_date,
            last_pdf_url=last_pdf_url,
            headless=headless,
            row_number=row_number,
        )
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error starting NE PSC scraper: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/sd-puc-scraper', methods=['GET', 'POST'])
def sd_puc_scraper_endpoint():
    """
    Scrape South Dakota PUC docket Filed Documents.

    GET params or POST JSON body:
        docket_number: Docket number (required, e.g. GE25-001)
        row_number: Row number for batch tracking (required)
        last_url: Watermark — PDF URL of the last processed doc (optional)
        save_json: Save nested/flat JSON files (optional, default false)
    """
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
        else:
            data = request.args.to_dict()

        docket_number = data.get("docket_number")
        row_number = data.get("row_number")

        if not docket_number or not row_number:
            return jsonify({
                "success": False,
                "error": "docket_number and row_number are required."
            }), 400

        last_url = data.get("last_url")
        save_json = str(data.get("save_json", "false")).lower() == "true"

        url = f"https://puc.sd.gov/Dockets/GasElectric/2025/{docket_number}.aspx"

        logger.info(
            f"Starting SD PUC scraper for docket={docket_number}, "
            f"row_number={row_number}, last_url={last_url}"
        )
        result = scrape_sd_puc(
            url=url,
            last_url=last_url,
            save_json=save_json,
            row_number=row_number,
        )
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error starting SD PUC scraper: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Log viewer endpoints — date-wise logs from /var/data/logs/
# ---------------------------------------------------------------------------
PERSISTENT_LOG_DIR = "/var/data/logs"
_LOG_BASE = PERSISTENT_LOG_DIR if os.path.isdir("/var/data") else "."

KNOWN_LOG_SCRIPTS = {
    "fs_cases_register",
    "fs_cases_update_monitor",
    "ec_cases_register",
    "ec_cases_update_monitor",
    "brazil_cases_register",
    "brazil_cases_update_monitor",
    "newzealand_cases_register",
    "newzealand_cases_update_monitor",
}


@app.route('/logs/list', methods=['GET'])
def list_log_dates():
    """List available log dates for a script.
    GET /logs/list?script=fs_cases_register
    """
    script = request.args.get("script", "").strip()
    if script not in KNOWN_LOG_SCRIPTS:
        return jsonify({
            "success": False,
            "error": f"Unknown script. Available: {sorted(KNOWN_LOG_SCRIPTS)}"
        }), 400

    log_dir = os.path.join(_LOG_BASE, script)
    if not os.path.isdir(log_dir):
        return jsonify({"success": True, "script": script, "dates": []}), 200

    dates = sorted(
        f.replace(".log", "")
        for f in os.listdir(log_dir)
        if f.endswith(".log")
    )
    return jsonify({"success": True, "script": script, "dates": dates}), 200


@app.route('/logs', methods=['GET'])
def get_log_content():
    """Return log content for a script + date.
    GET /logs?script=fs_cases_register&date=2026-05-01
    Omit date to get today's log. Use tail=N to get last N lines.
    """
    script = request.args.get("script", "").strip()
    if script not in KNOWN_LOG_SCRIPTS:
        return jsonify({
            "success": False,
            "error": f"Unknown script. Available: {sorted(KNOWN_LOG_SCRIPTS)}"
        }), 400

    date_str = request.args.get("date", "").strip()
    if not date_str:
        date_str = datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%d")

    log_path = os.path.join(_LOG_BASE, script, f"{date_str}.log")
    if not os.path.isfile(log_path):
        return jsonify({
            "success": False,
            "error": f"No log found for {script} on {date_str}"
        }), 404

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        tail = request.args.get("tail", type=int)
        if tail and tail > 0:
            lines = lines[-tail:]

        return jsonify({
            "success": True,
            "script": script,
            "date": date_str,
            "total_lines": len(lines),
            "content": "".join(lines),
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, host='0.0.0.0', port=port)

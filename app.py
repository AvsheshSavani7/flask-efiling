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
import logging
import os
import asyncio
import socket
import subprocess
import platform

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configure CORS to allow requests from http://localhost:8080
CORS(app, origins=["http://localhost:8080",
     "https://rag-summary-fe.onrender.com"])


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
        result = analyze_docket_entry(doc_number, text, metadata)

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
            "tier3_risk_assessment": result.get("tier3_risk_assessment")
        }

        # If it's a skipped entry (already exists), extract tier2 and tier3 from the entry
        if result.get("status") == "skipped" and "entry" in result:
            entry = result["entry"]
            response["tier2_analysis"] = entry.get("tier2_analysis")
            response["tier3_risk_assessment"] = entry.get(
                "tier3_risk_assessment")
            response["metadata"] = entry.get("metadata")
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
        print(result)

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
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "Minnesota E-filing Scraper API"
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

        result = extract_pdf_text_from_nm_prc(pdf_url)
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, host='0.0.0.0', port=port)

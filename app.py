from flask import Flask, request, jsonify
from mn_doc_scraper import parse_mn_documents
from mn_scraper import scrape_mn_documents
from demo4 import fetch_with_playwright_2captcha
from puc_scraper import fetch_with_playwright_2captcha_puc
from docket_entry_analyzer import analyze_docket_entry
import logging
import os
import asyncio
import socket

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route('/')
def home():
    """Home endpoint with API information"""
    return jsonify({
        "message": "Minnesota E-filing Scraper API",
        "endpoints": {
            "/scrape": "POST - Scrape documents for a given URL",
            "/puc-scrape": "POST - Scrape PUC documents for a given URL",
            "/proxy-check": "POST - Check if a proxy port is open",
            "/analyze-docket": "POST - Analyze docket entry with tier 2 and tier 3 analysis"
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
                "extracted_files": result.get("extracted_files", [])
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

        # If it's an existing entry, extract tier2 and tier3 from the entry
        if result.get("status") == "existing" and "entry" in result:
            entry = result["entry"]
            response["tier2_analysis"] = entry.get("tier2_analysis")
            response["tier3_risk_assessment"] = entry.get(
                "tier3_risk_assessment")
            response["metadata"] = entry.get("metadata")

        return jsonify(response), 200

    except Exception as e:
        logger.error(f"Error during docket analysis: {str(e)}")
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, host='0.0.0.0', port=port)

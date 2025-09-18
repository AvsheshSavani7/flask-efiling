from flask import Flask, request, jsonify
from mn_doc_scraper import parse_mn_documents
from mn_scraper import scrape_mn_documents
from demo2 import fetch_with_playwright_2captcha
import logging
import os
import asyncio

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
            "/scrape": "POST - Scrape documents for a given URL"
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
            }
        }
    })


@app.route('/scrape/', methods=['POST'])
def scrape_documents_post():
    """Scrape documents using POST request with JSON body"""
    try:
        data = request.get_json() or {}

        wait_time = data.get('wait_time', 20)
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
            result = parse_mn_documents(wait_time, url)
            return jsonify(result), 200 if result.get("success") else 500

    except Exception as e:
        logger.error(f"Error during scraping: {str(e)}")
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

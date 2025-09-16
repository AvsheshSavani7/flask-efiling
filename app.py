from flask import Flask, request, jsonify
from mn_scraper import scrape_mn_documents
import logging
import os

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
                    "wait_time": "integer (optional, default: 20)"
                }
            },
            "POST /scrape": {
                "body": {
                    "url": "string (optional, default: https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198)",
                    "wait_time": "integer (optional, default: 20)"
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
        url = data.get(
            'url', 'https://efiling.web.commerce.state.mn.us/documents?doSearch=true&dockets=24-198')

        logger.info(
            f"Starting scrape for URL: {url} with wait_time: {wait_time}")
        html_content = scrape_mn_documents(wait_time, url)
        logger.info(
            f"Scraping completed successfully. Content length: {len(html_content)}")

        return jsonify({
            "success": True,
            "url": url,
            "content_length": len(html_content),
            "html_content": html_content
        }), 200

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

import asyncio
import time
import os
import requests
from bs4 import BeautifulSoup
import PyPDF2
import docx
from io import BytesIO
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

# Proxy configuration from demo4.py
proxy_host = "95.135.111.60"
proxy_port = 45237
proxy_username = "oEhXl624nXDSDBR"
proxy_password = "YC7SFyimYnBQLbt"

# 2Captcha configuration from demo4.py
API_KEY = "1cc2815b0dfbc0b37d0218bc5f4325d1"
CAPTCHA_SOLVER_URL = "http://2captcha.com/in.php"
CAPTCHA_RESULT_URL = "http://2captcha.com/res.php"


def extract_text_from_document(file_content, file_extension):
    """
    Extract text from different document types.

    Args:
        file_content (bytes): The file content as bytes
        file_extension (str): File extension (pdf, docx, txt, etc.)

    Returns:
        str: Extracted text content
    """
    try:
        # Check if content is HTML (error page)
        content_str = file_content.decode('utf-8', errors='ignore')
        if '<html' in content_str.lower() or '<!doctype' in content_str.lower():
            soup = BeautifulSoup(content_str, 'html.parser')
            return soup.get_text().strip()

        if file_extension.lower() == 'pdf':
            # Validate and extract PDF content
            if not file_content.startswith(b'%PDF'):
                return f"Invalid PDF file - content doesn't start with PDF header"

            pdf_reader = PyPDF2.PdfReader(BytesIO(file_content))
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            return text.strip()

        elif file_extension.lower() in ['docx', 'doc']:
            # Extract text from Word document
            doc = docx.Document(BytesIO(file_content))
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text.strip()

        elif file_extension.lower() == 'txt':
            return file_content.decode('utf-8', errors='ignore')

        elif file_extension.lower() in ['html', 'htm']:
            soup = BeautifulSoup(file_content.decode(
                'utf-8', errors='ignore'), 'html.parser')
            return soup.get_text().strip()

        else:
            return file_content.decode('utf-8', errors='ignore')

    except Exception as e:
        try:
            return file_content.decode('utf-8', errors='ignore')
        except:
            return f"Error extracting text: {str(e)}"


async def get_sitekey(page):
    """Extract Turnstile sitekey from page content"""
    content = await page.content()
    soup = BeautifulSoup(content, "html.parser")
    sitekey = None
    iframe = soup.find(
        "iframe", src=lambda x: x and "challenges.cloudflare.com/cdn-cgi/challenge-platform" in x)
    if iframe and "sitekey=" in iframe["src"]:
        import urllib.parse
        q = urllib.parse.parse_qs(iframe["src"].split("?", 1)[1])
        sitekey = q.get("sitekey", [None])[0]
    if not sitekey:
        inp = soup.find("input", {"name": "cf-turnstile-sitekey"})
        if inp:
            sitekey = inp["value"]
    if not sitekey:
        div = soup.find("div", {"data-sitekey": True})
        if div:
            sitekey = div.get("data-sitekey")
    return sitekey


async def solve_captcha(sitekey, page_url):
    """Solve Cloudflare Turnstile captcha using 2captcha service"""
    # 2Captcha submission
    data = {
        "key": API_KEY,
        "method": "turnstile",
        "sitekey": sitekey,
        "pageurl": page_url,
        "json": 1,
    }
    resp = requests.post(CAPTCHA_SOLVER_URL, data=data)
    task_id = resp.json().get("request")
    if not task_id:
        print("2Captcha task creation failed:", resp.text)
        return None

    print(f"2Captcha task id: {task_id} - waiting for solution...")
    token = None
    for _ in range(30):
        time.sleep(5)
        params = {
            "key": API_KEY,
            "action": "get",
            "id": task_id,
            "json": 1,
        }
        r = requests.get(CAPTCHA_RESULT_URL, params=params)
        result = r.json()
        if result["status"] == 1:
            token = result["request"]
            break
        elif result["request"] != "CAPCHA_NOT_READY":
            print("2Captcha error:", result)
            break
        print("Waiting for captcha solution ...")

    if not token:
        print("Captcha was not solved in time.")
        return None

    print(f"Captcha solved: {token[:8]}... (truncated)")
    return token


async def parse_mn_documents_async(wait_time=20, url="", use_proxy=True):
    """
    Download and parse Minnesota e-filing documents using Playwright with proxy and 2captcha.
    For direct PDF URLs, downloads and extracts text directly without browser automation.

    Args:
        wait_time (int): Time to wait for page load in seconds
        url (str): The document download URL (should be a direct download link)
        use_proxy (bool): Whether to use proxy (default: True)

    Returns:
        dict: Dictionary containing extracted text and metadata
    """

    # Check if URL is a direct PDF link - handle it directly without browser automation
    if url.lower().endswith('.pdf'):
        print(f"Direct PDF URL detected: {url}")
        try:
            # Prepare request parameters
            request_params = {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'Accept': 'application/pdf,application/octet-stream,*/*',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                },
                'timeout': 30
            }

            # For direct PDF downloads, try without proxy first as many PDF servers don't require it
            # and proxy can cause authentication issues
            print("Downloading PDF directly (without proxy)...")
            response = requests.get(url, **request_params)

            # If direct download fails and proxy is enabled, try with proxy as fallback
            if response.status_code != 200 and use_proxy:
                print(
                    f"Direct download failed (status: {response.status_code}), trying with proxy...")
                request_params['proxies'] = {
                    'http': f'http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}',
                    'https': f'http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}'
                }
                print(
                    f"Using proxy for PDF download: {proxy_host}:{proxy_port}")
                response = requests.get(url, **request_params)

            if response.status_code == 200:
                print(
                    f"PDF downloaded successfully. Size: {len(response.content)} bytes")

                # Extract text from PDF
                text_content = extract_text_from_document(
                    response.content, 'pdf')

                return {
                    "success": True,
                    "content_type": "pdf",
                    "text_content": text_content,
                    "url": url,
                    "content_length": len(text_content),
                    "file_size_bytes": len(response.content),
                    "direct_pdf_download": True
                }
            else:
                print(
                    f"Failed to download PDF. Status code: {response.status_code}")
                return {
                    "success": False,
                    "error": f"Failed to download PDF. Status code: {response.status_code}",
                    "url": url,
                    "direct_pdf_download": True
                }

        except Exception as e:
            print(f"Error downloading PDF directly: {str(e)}")
            return {
                "success": False,
                "error": f"Error downloading PDF directly: {str(e)}",
                "url": url,
                "direct_pdf_download": True
            }
    async with Stealth().use_async(async_playwright()) as p:
        # Configure browser launch args
        launch_args = [
            "--no-sandbox",
            "--ignore-certificate-errors",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=VizDisplayCompositor"
        ]

        # Add proxy if enabled
        if use_proxy:
            launch_args.append(
                f"--proxy-server=http://{proxy_host}:{proxy_port}")

        browser = await p.chromium.launch(
            headless=True,
            args=launch_args
        )

        # Configure context
        context_options = {
            "ignore_https_errors": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            },
            "viewport": {"width": 1920, "height": 1080},
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False
        }

        # Add proxy if enabled
        if use_proxy:
            context_options["proxy"] = {
                "server": f"http://{proxy_host}:{proxy_port}",
                "username": proxy_username,
                "password": proxy_password
            }

        context = await browser.new_context(**context_options)
        page = await context.new_page()

        try:
            # Load the document download page
            await page.goto(url, timeout=120_000)
            await asyncio.sleep(wait_time)

            # Get the current URL after any redirects
            current_url = page.url

            # Check for Cloudflare Turnstile challenge
            captcha_processed = False
            sitekey = await get_sitekey(page)
            if sitekey:
                print(f"Turnstile sitekey found: {sitekey}")

                # Solve the captcha
                token = await solve_captcha(sitekey, current_url)
                print(f"Captcha token: {token}")
                if not token:
                    await browser.close()
                    return {
                        "success": False,
                        "error": "Failed to solve Cloudflare challenge",
                        "url": current_url,
                        "cloudflare_challenge": True
                    }

                # Inject token and submit form with better logic
                print("Injecting captcha token...")
                captcha_injected = await page.evaluate("""
                    (token) => {
                        // Look for existing Turnstile response input
                        let input = document.querySelector('input[name="cf-turnstile-response"]');
                        
                        if (!input) {
                            // Try to find the Turnstile widget container
                            let widget = document.querySelector('[data-sitekey]');
                            if (widget && widget.parentElement) {
                                input = document.createElement('input');
                                input.type = "hidden";
                                input.name = "cf-turnstile-response";
                                widget.parentElement.appendChild(input);
                            } else {
                                // Fallback: add to body
                                input = document.createElement('input');
                                input.type = "hidden";
                                input.name = "cf-turnstile-response";
                                document.body.appendChild(input);
                            }
                        }
                        
                        input.value = token;
                        
                        // Trigger change event to notify Turnstile
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        
                        // Try to find and submit the correct form
                        let form = input.form;
                        if (!form) {
                            // Look for forms that might contain Turnstile
                            let forms = document.querySelectorAll('form');
                            for (let f of forms) {
                                if (f.querySelector('[data-sitekey]') || f.querySelector('.cf-turnstile')) {
                                    form = f;
                                    break;
                                }
                            }
                        }
                        
                        if (!form) {
                            form = document.querySelector('form');
                        }
                        
                        if (form) {
                            console.log('Submitting form after captcha injection');
                            form.submit();
                            return true;
                        } else {
                            console.log('No form found, reloading page');
                            location.reload();
                            return true;
                        }
                    }
                """, token)

                if not captcha_injected:
                    print("Failed to inject captcha token")
                    await browser.close()
                    return {
                        "success": False,
                        "error": "Failed to inject captcha token",
                        "url": current_url,
                        "cloudflare_challenge": True
                    }

                # Wait for navigation after captcha submission with better strategy
                print("Waiting for captcha processing...")
                try:
                    # Wait for either navigation or a specific timeout
                    await asyncio.wait_for(
                        page.wait_for_load_state('networkidle', timeout=30000),
                        timeout=35
                    )
                except asyncio.TimeoutError:
                    print("Navigation timeout, checking current state...")
                    await asyncio.sleep(5)
                except Exception as e:
                    print(f"Navigation error: {e}, waiting extra...")
                    await asyncio.sleep(15)

                # Check if we're still on the challenge page
                current_content = await page.content()
                current_url = page.url

                # Validate captcha was processed - check if we're still on turnstile challenge
                if "challenges.cloudflare.com" in current_url:
                    print(
                        "Still on Cloudflare challenge page after captcha injection - may need retry")
                    await asyncio.sleep(10)  # Give it more time
                    current_content = await page.content()
                    current_url = page.url
                    print(f"After additional wait - URL: {current_url}")
                else:
                    print(
                        f"Captcha processed successfully - redirected to: {current_url}")
                    captcha_processed = True

            # Determine file type from URL
            file_extension = "pdf"  # default for Minnesota e-filing documents
            if '.pdf' in current_url.lower():
                file_extension = "pdf"
            elif '.docx' in current_url.lower() or '.doc' in current_url.lower():
                file_extension = "docx"
            elif '.txt' in current_url.lower():
                file_extension = "txt"
            elif '.html' in current_url.lower() or '.htm' in current_url.lower():
                file_extension = "html"

            # Handle security check page with more aggressive bypass attempts
            page_source = await page.content()
            security_check_attempts = 0
            max_security_attempts = 5

            # Check if we're still on a challenge/security page after captcha
            # But only if we haven't already processed a captcha successfully
            security_indicators = [
                "verify your browser", "checking your browser",
                "challenges.cloudflare.com", "cf-turnstile", "Just a moment"
            ]

            # Only check for "Security check" if we're still on turnstile page
            # If we've moved away from turnstile, don't enter security retry loop
            is_security_page = any(
                indicator in page_source for indicator in security_indicators)

            # If we've successfully processed a captcha or moved away from turnstile, skip security retry loop
            if captcha_processed or ("turnstile" not in current_url.lower() and "challenges.cloudflare.com" not in current_url.lower()):
                print(
                    "Captcha processed or not on turnstile page - skipping security check retry loop")
                is_security_page = False

            while is_security_page and security_check_attempts < max_security_attempts:
                security_check_attempts += 1
                print(
                    f"Security check detected (attempt {security_check_attempts}/{max_security_attempts})")
                print(f"Current URL: {current_url}")

                # If we're still on turnstile after captcha, wait longer
                if "challenges.cloudflare.com" in current_url:
                    print("Still on Cloudflare challenge page - waiting longer...")
                    await asyncio.sleep(20)

                    # Check if there's a new sitekey (indicating a new challenge)
                    new_sitekey = await get_sitekey(page)
                    if new_sitekey and new_sitekey != sitekey:
                        print(
                            f"New Turnstile challenge detected with sitekey: {new_sitekey}")
                        # Solve the new challenge
                        new_token = await solve_captcha(new_sitekey, current_url)
                        if new_token:
                            # Re-inject the new token
                            await page.evaluate("""
                                (token) => {
                                    let input = document.querySelector('input[name="cf-turnstile-response"]');
                                    if (input) {
                                        input.value = token;
                                        input.dispatchEvent(new Event('change', { bubbles: true }));
                                    }
                                }
                            """, new_token)
                            await asyncio.sleep(10)

                # Wait longer and try different approaches
                await asyncio.sleep(15)

                # Try to click any "Continue" or "Proceed" buttons
                try:
                    continue_buttons = await page.query_selector_all('button, input[type="submit"], a')
                    for button in continue_buttons:
                        text = await button.inner_text()
                        if any(word in text.lower() for word in ['continue', 'proceed', 'verify', 'submit']):
                            print(f"Clicking button with text: {text}")
                            await button.click()
                            await asyncio.sleep(8)
                            break
                except Exception as e:
                    print(f"Error clicking buttons: {e}")

                # Try refreshing the page if still stuck
                if security_check_attempts < max_security_attempts:
                    print("Refreshing page to retry...")
                    await page.reload()
                    await asyncio.sleep(10)

                # Check if we're still on security page
                page_source = await page.content()
                current_url = page.url
                is_security_page = any(
                    indicator in page_source for indicator in security_indicators)

                # If we're redirected away from challenge pages, break the loop
                if not any(challenge in current_url.lower() for challenge in ["challenges.cloudflare.com", "turnstile"]):
                    print("Redirected away from challenge page")
                    break

            # Final check for security page
            if is_security_page:
                print("Still on security check page after all attempts")
                # Try one more approach - wait much longer and check again
                await asyncio.sleep(30)
                page_source = await page.content()
                current_url = page.url
                is_security_page = any(
                    indicator in page_source for indicator in security_indicators)

                if is_security_page:
                    print("Final attempt failed - continuing anyway to try download")
                else:
                    print("Security check resolved after extended wait")

            # Download the document content using the browser's session
            try:
                print(
                    f"Starting download process from current URL: {current_url}")

                # Get cookies from the browser session
                cookies = await context.cookies()
                cookie_dict = {cookie['name']: cookie['value']
                               for cookie in cookies}

                print(f"Using {len(cookie_dict)} cookies for download")

                # Try the original URL first (in case we have valid session)
                original_url = url
                # Also try to construct the direct download URL
                if '{' in original_url and '}' in original_url:
                    # Extract document ID from URL
                    import re
                    doc_id_match = re.search(r'\{([^}]+)\}', original_url)
                    if doc_id_match:
                        doc_id = doc_id_match.group(1)
                        direct_url = f"https://efiling.web.commerce.state.mn.us/documents/{doc_id}/download?contentSequence=0&rowIndex=1"
                        urls_to_try = [original_url, direct_url, current_url]
                        print(f"Constructed direct download URL: {direct_url}")
                    else:
                        urls_to_try = [original_url, current_url]
                else:
                    urls_to_try = [original_url, current_url]

                response = None
                successful_url = None

                for i, attempt_url in enumerate(urls_to_try, 1):
                    print(
                        f"Download attempt {i}/{len(urls_to_try)}: {attempt_url}")
                    try:
                        # Prepare request parameters
                        request_params = {
                            'headers': {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                                'Accept-Language': 'en-US,en;q=0.5',
                                'Accept-Encoding': 'gzip, deflate',
                                'Connection': 'keep-alive',
                                'Upgrade-Insecure-Requests': '1',
                                'Referer': current_url  # Add referer to maintain session context
                            },
                            'cookies': cookie_dict,
                            'timeout': 30
                        }

                        # Add proxy configuration if enabled
                        if use_proxy:
                            request_params['proxies'] = {
                                'http': f'http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}',
                                'https': f'http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}'
                            }
                            print(
                                f"Using proxy for download: {proxy_host}:{proxy_port}")

                        response = requests.get(attempt_url, **request_params)

                        print(
                            f"Response status: {response.status_code}, Content length: {len(response.content)}")

                        # Check if response content is valid document content
                        content_type = response.headers.get(
                            'content-type', '').lower()
                        content_disposition = response.headers.get(
                            'content-disposition', '')

                        print(f"Content-Type: {content_type}")
                        print(f"Content-Disposition: {content_disposition}")

                        # Valid content - check for actual document content vs error pages
                        if response.status_code == 200 and len(response.content) > 1000:
                            # Check if it's still a security/challenge page
                            content_preview = response.content[:500].decode(
                                'utf-8', errors='ignore')
                            if any(indicator in content_preview for indicator in ["Security check", "verify your browser", "challenges.cloudflare.com"]):
                                print(
                                    f"Response from {attempt_url} is still a security page")
                                continue

                            print(
                                f"Successfully downloaded from: {attempt_url}")
                            current_url = attempt_url
                            successful_url = attempt_url
                            break
                        else:
                            print(
                                f"Failed to download from {attempt_url}: Status {response.status_code}, Content length: {len(response.content)}")
                            if len(response.content) < 1000:
                                content_preview = response.content.decode(
                                    'utf-8', errors='ignore')
                                print(
                                    f"Content preview: {content_preview[:200]}...")
                    except Exception as e:
                        print(f"Error downloading from {attempt_url}: {e}")
                        continue

                if response and response.status_code == 200:
                    if len(response.content) == 0:
                        await browser.close()
                        return {
                            "success": False,
                            "error": "Downloaded file is empty",
                            "url": current_url
                        }

                    # Check if the response is still a security page
                    content_str = response.content.decode(
                        'utf-8', errors='ignore')
                    if any(phrase in content_str.lower() for phrase in ['security check', 'verify your browser', 'checking your browser']):
                        print("Response is still a security check page")
                        await browser.close()
                        return {
                            "success": False,
                            "error": "Security check page detected - unable to bypass even after captcha solution",
                            "url": current_url,
                            "security_check": True,
                            "content_preview": content_str[:500]
                        }

                    # Extract text from the document
                    text_content = extract_text_from_document(
                        response.content, file_extension)
                    await browser.close()
                    return {
                        "success": True,
                        "content_type": file_extension,
                        "text_content": text_content,
                        "url": current_url,
                        "content_length": len(text_content),
                        "file_size_bytes": len(response.content)
                    }
                else:
                    await browser.close()
                    return {
                        "success": False,
                        "error": f"Failed to download document. Status code: {response.status_code}",
                        "url": current_url
                    }

            except Exception as e:
                await browser.close()
                return {
                    "success": False,
                    "error": f"Error downloading document: {str(e)}",
                    "url": current_url
                }

        except PlaywrightTimeoutError:
            print("Timed out waiting for page load. Saving what loaded.")
            html = await page.content()
            with open("debug_initial_timeout.html", "w", encoding="utf-8") as f:
                f.write(html)
            await browser.close()
            return {
                "success": False,
                "error": "Page load timeout",
                "url": url,
                "timeout": True
            }
        except Exception as e:
            await browser.close()
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "url": url
            }


def parse_mn_documents(wait_time=20, url="", use_proxy=True):
    """
    Wrapper function to run the async document parser.

    Args:
        wait_time (int): Time to wait for page load in seconds
        url (str): The document download URL (should be a direct download link)
        use_proxy (bool): Whether to use proxy (default: True)

    Returns:
        dict: Dictionary containing extracted text and metadata
    """
    try:
        return asyncio.run(parse_mn_documents_async(wait_time, url, use_proxy))
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(parse_mn_documents_async(wait_time, url, use_proxy))


if __name__ == "__main__":
    # For testing the function directly
    result = parse_mn_documents()

    if result.get("success"):
        with open("extracted_text.txt", "w", encoding="utf-8") as f:
            f.write(result.get("text_content", ""))
        print("Text content saved to extracted_text.txt")
    else:
        print("Error:", result.get("error"))

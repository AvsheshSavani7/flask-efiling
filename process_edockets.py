import csv
import json
import os
# from demo4 import fetch_with_playwright_2captcha  # Old version with broken proxy
from fetch_alternatives import fetch_with_playwright_no_proxy as fetch_with_playwright_2captcha
from mn_doc_scraper import extract_text_from_document
from tqdm import tqdm
import time
import logging
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Thread-safe lock for file operations
file_lock = threading.Lock()


def read_csv_to_dict(csv_file_path):
    """
    Read CSV file and convert to list of dictionaries.

    Args:
        csv_file_path (str): Path to the CSV file

    Returns:
        list: List of dictionaries, one per CSV row
    """
    records = []

    try:
        with open(csv_file_path, 'r', encoding='utf-8') as f:
            # Use csv.DictReader to automatically parse headers
            reader = csv.DictReader(f)

            for row in reader:
                records.append(row)

        logger.info(
            f"Successfully read {len(records)} records from {csv_file_path}")
        return records

    except Exception as e:
        logger.error(f"Error reading CSV file: {str(e)}")
        return []


def download_and_extract_content(download_link, wait_time=20):
    """
    Download a document and extract its text content using Playwright + 2Captcha to bypass Cloudflare.

    Args:
        download_link (str): URL to download the document from
        wait_time (int): Time to wait for page load in seconds

    Returns:
        str: Extracted text content or error message
    """
    if not download_link or download_link.strip() == "":
        return ""

    try:
        logger.info(f"Downloading document from: {download_link}")

        # Use Playwright with stealth mode (no proxy) + 2Captcha to bypass Cloudflare security
        content = fetch_with_playwright_2captcha(download_link, wait_time)

        if not content:
            logger.warning("Failed to fetch content - empty response")
            return "[Error: Empty response from server]"

        # Check if content is binary (PDF)
        if isinstance(content, bytes):
            # Try to extract as PDF
            if content.startswith(b'%PDF'):
                logger.info("Processing PDF document...")
                try:
                    text_content = extract_text_from_document(content, "pdf")
                    logger.info(
                        f"Extracted {len(text_content)} characters from PDF")
                    return text_content
                except Exception as e:
                    logger.error(f"Failed to extract text from PDF: {str(e)}")
                    return f"[Error: Failed to extract PDF text - {str(e)}]"
            else:
                # Binary content but not PDF, try to decode
                logger.info("Converting binary content to text...")
                content = content.decode('utf-8', errors='ignore')

        # If we got here, content is a string (HTML or text)
        html_content = content

        # Check if we got a security check page
        if "Security check" in html_content:
            logger.warning("Still on security check page after bypass attempt")
            return "[Error: Could not bypass security check]"

        # Check content type - if it's HTML, extract text
        if '<html' in html_content.lower() or '<!doctype' in html_content.lower():
            soup = BeautifulSoup(html_content, 'html.parser')

            # Check if it's an error page or actual document
            if 'error' in html_content.lower() or 'not found' in html_content.lower():
                text_content = soup.get_text().strip()
                logger.warning(f"Got error page: {text_content[:200]}")
                return f"[Error: {text_content[:200]}...]"

            # It might be embedded PDF viewer or document viewer HTML
            text_content = soup.get_text().strip()
            logger.info(f"Extracted {len(text_content)} characters from HTML")
            return text_content

        # Check if it's PDF content that was decoded as string
        if html_content.startswith('%PDF'):
            logger.info("Processing PDF document (from string)...")
            try:
                content_bytes = html_content.encode('latin-1')
                text_content = extract_text_from_document(content_bytes, "pdf")
                logger.info(
                    f"Extracted {len(text_content)} characters from PDF")
                return text_content
            except Exception as e:
                logger.error(f"Failed to extract text from PDF: {str(e)}")
                return f"[Error: Failed to extract PDF text - {str(e)}]"

        # Default: return as text
        logger.info(f"Extracted {len(html_content)} characters")
        return html_content

    except Exception as e:
        logger.error(f"Exception while downloading/extracting: {str(e)}")
        return f"[Exception: {str(e)}]"


def process_single_record(record_data):
    """
    Process a single record (worker function for thread pool).

    Args:
        record_data (tuple): (idx, record, skip_trade_secret, wait_time)

    Returns:
        tuple: (idx, processed_record)
    """
    idx, record, skip_trade_secret, wait_time = record_data

    try:
        # Add content field
        download_link = record.get("Download Link", "").strip()
        file_class = record.get("Class ", "").strip()

        # Skip Trade Secret documents if requested
        if skip_trade_secret and file_class.lower() == "trade secret":
            logger.info(f"Record #{idx}: Skipping Trade Secret document")
            record["content"] = "[Trade Secret - Not Downloaded]"
        elif not download_link:
            logger.info(f"Record #{idx}: No download link available")
            record["content"] = ""
        else:
            # Download and extract content
            logger.info(f"Record #{idx}: Processing document...")
            content = download_and_extract_content(
                download_link, wait_time=wait_time)
            record["content"] = content

        return (idx, record)

    except Exception as e:
        logger.error(f"Error processing record #{idx}: {str(e)}")
        record["content"] = f"[Error: {str(e)}]"
        return (idx, record)


def process_edockets_csv(
    csv_file_path="eDockets.csv",
    output_json_path="edockets_with_content.json",
    max_records=None,
    skip_trade_secret=True,
    delay_between_downloads=2,
    wait_time=20,
    num_workers=1,
    batch_size=50
):
    """
    Process eDockets CSV file and create JSON with document content.

    Args:
        csv_file_path (str): Path to input CSV file
        output_json_path (str): Path to output JSON file (base name for batch files)
        max_records (int): Maximum number of records to process (None for all)
        skip_trade_secret (bool): Whether to skip Trade Secret documents
        delay_between_downloads (int): Seconds to wait between downloads (only used with 1 worker)
        wait_time (int): Time to wait for page load in seconds
        num_workers (int): Number of parallel workers (default: 1)
        batch_size (int): Number of records per batch file (default: 50)

    Returns:
        list: List of processed records with content
    """
    # Read CSV file
    logger.info(f"Reading CSV file: {csv_file_path}")
    records = read_csv_to_dict(csv_file_path)

    if not records:
        logger.error("No records found in CSV file")
        return []

    # Limit records if specified
    if max_records:
        records = records[:max_records]
        logger.info(f"Processing first {max_records} records")

    # Calculate number of batches
    total_batches = (len(records) + batch_size - 1) // batch_size
    logger.info(
        f"Total records: {len(records)}, Batch size: {batch_size}, Total batches: {total_batches}")

    # Prepare output file paths
    output_dir = os.path.dirname(output_json_path) or "."
    output_basename = os.path.splitext(os.path.basename(output_json_path))[0]

    all_processed_records = []
    batch_files = []

    # Process records in batches
    for batch_num in range(total_batches):
        batch_start = batch_num * batch_size
        batch_end = min(batch_start + batch_size, len(records))
        batch_records = records[batch_start:batch_end]

        logger.info("=" * 80)
        logger.info(f"Processing BATCH {batch_num + 1}/{total_batches}")
        logger.info(
            f"Records {batch_start + 1} to {batch_end} ({len(batch_records)} records)")
        logger.info("=" * 80)

        # Pre-allocate list to maintain order within this batch
        processed_batch = [None] * len(batch_records)

        if num_workers == 1:
            # Single-threaded processing
            for idx, record in enumerate(tqdm(batch_records, desc=f"Batch {batch_num + 1}/{total_batches}"), 1):
                global_idx = batch_start + idx
                try:
                    _, processed_record = process_single_record(
                        (global_idx, record, skip_trade_secret, wait_time))
                    processed_batch[idx - 1] = processed_record

                    # Add delay to avoid overwhelming the server
                    if delay_between_downloads > 0 and idx < len(batch_records):
                        time.sleep(delay_between_downloads)

                except Exception as e:
                    logger.error(
                        f"Error processing record #{global_idx}: {str(e)}")
                    record["content"] = f"[Error: {str(e)}]"
                    processed_batch[idx - 1] = record
        else:
            # Multi-threaded processing
            if batch_num == 0:
                logger.info(f"Using {num_workers} parallel workers")
                logger.warning(
                    "Note: With parallel processing, delay_between_downloads is ignored")

            # Prepare work items for this batch
            work_items = [
                (batch_start + idx + 1, record, skip_trade_secret, wait_time)
                for idx, record in enumerate(batch_records)
            ]

            # Process with thread pool
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                # Submit all tasks for this batch
                future_to_idx = {
                    executor.submit(process_single_record, item): item[0]
                    for item in work_items
                }

                # Process completed tasks with progress bar
                with tqdm(total=len(batch_records), desc=f"Batch {batch_num + 1}/{total_batches}") as pbar:
                    for future in as_completed(future_to_idx):
                        try:
                            global_idx, processed_record = future.result()
                            local_idx = global_idx - batch_start - 1
                            processed_batch[local_idx] = processed_record
                            pbar.update(1)

                        except Exception as e:
                            global_idx = future_to_idx[future]
                            logger.error(
                                f"Error in worker for record #{global_idx}: {str(e)}")
                            pbar.update(1)

        # Filter out None values
        processed_batch = [r for r in processed_batch if r is not None]

        # Save this batch to a separate file
        batch_filename = f"{output_basename}_batch_{batch_num + 1:03d}.json"
        batch_filepath = os.path.join(output_dir, batch_filename)

        with file_lock:
            save_json(processed_batch, batch_filepath)
            logger.info(
                f"✓ Saved batch {batch_num + 1}/{total_batches} to: {batch_filename}")
            batch_files.append(batch_filename)

        # Add to overall list
        all_processed_records.extend(processed_batch)

        # Brief pause between batches (give system a moment to breathe)
        if batch_num < total_batches - 1:
            logger.info("Pausing 5 seconds before next batch...")
            time.sleep(5)

    # Print summary
    logger.info("=" * 80)
    logger.info("Processing Summary:")
    logger.info(f"Total records processed: {len(all_processed_records)}")
    logger.info(f"Total batches created: {len(batch_files)}")

    success_count = sum(1 for r in all_processed_records if r.get(
        "content") and not r["content"].startswith("[Error"))
    error_count = sum(1 for r in all_processed_records if r.get(
        "content", "").startswith("[Error"))
    empty_count = sum(1 for r in all_processed_records if not r.get("content"))

    logger.info(f"Successfully extracted: {success_count}")
    logger.info(f"Errors: {error_count}")
    logger.info(f"Empty/No link: {empty_count}")
    logger.info("")
    logger.info("Batch files created:")
    for batch_file in batch_files:
        logger.info(f"  - {batch_file}")
    logger.info("=" * 80)

    return all_processed_records


def save_json(data, output_path):
    """
    Save data to JSON file.

    Args:
        data (list): Data to save
        output_path (str): Path to output JSON file
    """
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(data)} records to {output_path}")
    except Exception as e:
        logger.error(f"Error saving JSON: {str(e)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Process eDockets CSV and extract document content"
    )
    parser.add_argument(
        "--input",
        default="eDockets.csv",
        help="Input CSV file path (default: eDockets.csv)"
    )
    parser.add_argument(
        "--output",
        default="edockets_with_content.json",
        help="Output JSON file path (default: edockets_with_content.json)"
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Maximum number of records to process (default: all)"
    )
    parser.add_argument(
        "--include-trade-secret",
        action="store_true",
        help="Include Trade Secret documents (by default they are skipped)"
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=2,
        help="Delay in seconds between downloads (default: 2)"
    )
    parser.add_argument(
        "--wait-time",
        type=int,
        default=20,
        help="Time to wait for page load in seconds (default: 20)"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers for processing (default: 1). Use 3-5 for faster processing."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of records per batch file (default: 50). Each batch is saved to a separate JSON file."
    )

    args = parser.parse_args()

    # Check if input file exists
    if not os.path.exists(args.input):
        logger.error(f"Input file not found: {args.input}")
        exit(1)

    # Process the CSV
    logger.info("Starting eDockets CSV processing...")
    logger.info(f"Input file: {args.input}")
    logger.info(f"Output file pattern: {args.output}")
    logger.info(
        f"Max records: {args.max_records if args.max_records else 'All'}")
    logger.info(f"Skip Trade Secret: {not args.include_trade_secret}")
    logger.info(f"Batch size: {args.batch_size} records per file")
    logger.info(f"Number of workers: {args.workers}")
    if args.workers == 1:
        logger.info(f"Delay between downloads: {args.delay}s")
    logger.info(f"Wait time per page: {args.wait_time}s")
    logger.info(
        "Using Playwright (Chromium) with stealth mode + 2Captcha (no proxy)")
    logger.info("=" * 80)

    process_edockets_csv(
        csv_file_path=args.input,
        output_json_path=args.output,
        max_records=args.max_records,
        skip_trade_secret=not args.include_trade_secret,
        delay_between_downloads=args.delay,
        wait_time=args.wait_time,
        num_workers=args.workers,
        batch_size=args.batch_size
    )

    logger.info("Processing complete!")

"""
Simple CSV to JSON converter
This version works perfectly on macOS - no browser automation needed
Converts eDockets CSV to JSON format with empty content fields ready to fill
"""

import csv
import json
import argparse
import os


def csv_to_json(csv_file_path, output_json_path, add_empty_content=True):
    """
    Convert CSV to JSON format.
    
    Args:
        csv_file_path (str): Path to input CSV file
        output_json_path (str): Path to output JSON file
        add_empty_content (bool): Add empty 'content' field to each record
        
    Returns:
        list: List of records
    """
    records = []
    
    print(f"Reading CSV file: {csv_file_path}")
    
    with open(csv_file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if add_empty_content:
                row['content'] = ""  # Add empty content field
            records.append(row)
    
    print(f"Read {len(records)} records")
    
    # Save to JSON
    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    
    print(f"Saved to: {output_json_path}")
    
    # Print statistics
    print("\n" + "=" * 60)
    print("Statistics:")
    print(f"Total records: {len(records)}")
    
    # Count by document type
    doc_types = {}
    for record in records:
        doc_type = record.get("Document Type ", "Unknown").strip()
        doc_types[doc_type] = doc_types.get(doc_type, 0) + 1
    
    print("\nDocument Types:")
    for doc_type, count in sorted(doc_types.items(), key=lambda x: x[1], reverse=True):
        print(f"  {doc_type}: {count}")
    
    # Count records with download links
    with_links = sum(1 for r in records if r.get("Download Link", "").strip())
    without_links = len(records) - with_links
    
    print(f"\nWith download links: {with_links}")
    print(f"Without download links: {without_links}")
    
    # Count by class
    classes = {}
    for record in records:
        class_type = record.get("Class ", "Unknown").strip()
        classes[class_type] = classes.get(class_type, 0) + 1
    
    print("\nBy Class:")
    for class_type, count in sorted(classes.items(), key=lambda x: x[1], reverse=True):
        print(f"  {class_type}: {count}")
    
    print("=" * 60)
    
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert eDockets CSV to JSON"
    )
    parser.add_argument(
        "--input",
        default="eDockets.csv",
        help="Input CSV file path (default: eDockets.csv)"
    )
    parser.add_argument(
        "--output",
        default="edockets.json",
        help="Output JSON file path (default: edockets.json)"
    )
    parser.add_argument(
        "--no-content-field",
        action="store_true",
        help="Don't add empty 'content' field"
    )
    
    args = parser.parse_args()
    
    # Check if input file exists
    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}")
        exit(1)
    
    # Convert
    csv_to_json(
        csv_file_path=args.input,
        output_json_path=args.output,
        add_empty_content=not args.no_content_field
    )
    
    print("\n✅ Conversion complete!")
    print(f"\nTo view the JSON:")
    print(f"  cat {args.output} | head -50")
    print(f"\nTo view a specific record:")
    print(f"  cat {args.output} | jq '.[0]'")


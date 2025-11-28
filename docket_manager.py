#!/usr/bin/env python3
"""
Docket Manager
==============
Functions for managing docket entries in the database.
Includes functions for fetching, updating, and managing docket records.
"""

import os
from typing import Dict, Any, Optional, List
from pymongo import MongoClient

ENV_FILE = ".env"


def _load_env_file(env_path: str) -> None:
    """Load environment variables from .env file"""
    if not os.path.exists(env_path):
        return

    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value


def _get_database_connection():
    """Get MongoDB database connection"""
    _load_env_file(ENV_FILE)

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        raise ValueError("MongoDB connection string not found in .env")

    mongo_client = MongoClient(mongodb_uri)
    db = mongo_client.get_database()
    collection = db["docket"]

    return collection, mongo_client


def get_dockets(
    docket_type: Optional[str] = None,
    page: int = 1,
    limit: int = 10,
    sort_field: str = "date",
    sort_order: str = "asc"
) -> Dict[str, Any]:
    """
    Fetch docket entries with pagination, filtered by docket_type and sorted by specified field.

    Args:
        docket_type: Optional docket type to filter by (e.g., "PUC", "FCC", "CPUC", "STB")
                    If None or empty, returns all dockets
        page: Page number (1-indexed)
        limit: Number of records per page
        sort_field: Field to sort by - "date" or "hash_id" (default: "date")
        sort_order: Sort order - "asc" for ascending, "desc" for descending (default: "asc")

    Returns:
        Dictionary containing:
            - success: Boolean indicating success
            - data: List of docket entries
            - pagination: Dictionary with page, limit, total, total_pages
            - error: Error message if any
    """
    try:
        collection, mongo_client = _get_database_connection()

        # Build query filter
        query_filter = {}
        if docket_type and docket_type.strip() and docket_type != "N/A":
            query_filter = {"metadata.docket_type": docket_type}

        # Get total count for pagination
        total_count = collection.count_documents(query_filter)

        # Calculate pagination
        if limit <= 0:
            limit = 10  # Default limit
        if page <= 0:
            page = 1  # Default page

        total_pages = (total_count + limit - 1) // limit  # Ceiling division
        skip = (page - 1) * limit

        print(f"Sort field: {sort_field}")
        print(f"Sort order: {sort_order}")

        # Map sort_field to MongoDB field path
        if sort_field.lower() == "hash_id":
            sort_field_path = "hash_id"
            # For hash_id sorting, use date as secondary sort for entries without hash_id
            secondary_sort = ("metadata.date", 1)
        elif sort_field.lower() == "date":
            sort_field_path = "metadata.date"
            # For date sorting, use hash_id as secondary sort for consistency
            secondary_sort = ("hash_id", 1)
        else:
            # Default to date if invalid sort_field provided
            sort_field_path = "metadata.date"
            secondary_sort = ("hash_id", 1)

        # Determine sort direction: 1 for ascending, -1 for descending
        sort_direction = -1 if sort_order.lower() == "desc" else 1

        # Build sort criteria - primary sort by requested field, secondary sort for consistency
        # Note: MongoDB handles null/missing values by placing them last in ascending, first in descending
        # When sorting by hash_id, entries without hash_id will be placed at the end (asc) or beginning (desc)
        # When sorting by date, entries without date will be handled similarly
        sort_criteria = [
            (sort_field_path, sort_direction),
            secondary_sort  # Secondary sort always ascending for consistency
        ]

        # Fetch entries with pagination, sorted by specified field
        # Note: If hash_id was assigned sequentially based on date order,
        # sorting by hash_id and date will give similar results.
        # To see different results, hash_id values would need to be assigned differently.
        entries = list(
            collection.find(query_filter)
            .sort(sort_criteria)
            .skip(skip)
            .limit(limit)
        )

        # Remove _id field from each entry (convert ObjectId to string or remove)
        for entry in entries:
            entry.pop("_id", None)

        # Close connection
        mongo_client.close()

        return {
            "success": True,
            "data": entries,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
            },
            "docket_type": docket_type if docket_type else "all",
            "sort_info": {
                "sort_field": sort_field,
                "sort_order": sort_order,
                "sort_field_path": sort_field_path,
                "note": "If hash_id was assigned sequentially by date, sorting by hash_id and date will give similar results"
            }
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "data": [],
            "pagination": {}
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Database error: {str(e)}",
            "data": [],
            "pagination": {}
        }


def update_docket_entry(
    doc_number: str,
    update_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Update a docket entry by document number.

    Args:
        doc_number: The document ID/number to update
        update_data: Dictionary of fields to update

    Returns:
        Dictionary containing success status and updated entry or error
    """
    try:
        collection, mongo_client = _get_database_connection()

        # Add updated_at timestamp
        from datetime import datetime
        update_data["updated_at"] = datetime.now().isoformat()

        # Update the entry
        result = collection.update_one(
            {"metadata.document_id": doc_number},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            mongo_client.close()
            return {
                "success": False,
                "error": f"Document with doc_number '{doc_number}' not found"
            }

        if result.modified_count == 0:
            mongo_client.close()
            return {
                "success": True,
                "message": "No changes made to the document",
                "doc_number": doc_number
            }

        # Fetch and return the updated entry
        updated_entry = collection.find_one(
            {"metadata.document_id": doc_number})
        if updated_entry:
            updated_entry.pop("_id", None)

        mongo_client.close()

        return {
            "success": True,
            "message": "Document updated successfully",
            "doc_number": doc_number,
            "entry": updated_entry
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }


def delete_docket_entry(doc_number: str) -> Dict[str, Any]:
    """
    Delete a docket entry by document number.

    Args:
        doc_number: The document ID/number to delete

    Returns:
        Dictionary containing success status and message or error
    """
    try:
        collection, mongo_client = _get_database_connection()

        result = collection.delete_one({"metadata.document_id": doc_number})

        mongo_client.close()

        if result.deleted_count == 0:
            return {
                "success": False,
                "error": f"Document with doc_number '{doc_number}' not found"
            }

        return {
            "success": True,
            "message": f"Document '{doc_number}' deleted successfully",
            "doc_number": doc_number
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }


def get_docket_by_id(doc_number: str) -> Dict[str, Any]:
    """
    Get a single docket entry by document number.

    Args:
        doc_number: The document ID/number to fetch

    Returns:
        Dictionary containing success status and entry or error
    """
    try:
        collection, mongo_client = _get_database_connection()

        entry = collection.find_one({"metadata.document_id": doc_number})

        mongo_client.close()

        if not entry:
            return {
                "success": False,
                "error": f"Document with doc_number '{doc_number}' not found"
            }

        entry.pop("_id", None)

        return {
            "success": True,
            "data": entry,
            "doc_number": doc_number
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

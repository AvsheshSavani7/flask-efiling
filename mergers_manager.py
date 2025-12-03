#!/usr/bin/env python3
"""
Mergers Manager
===============
Functions for managing merger records in the database.
Includes functions for fetching and managing merger records.
"""

import os
from typing import Dict, Any
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
    """Get MongoDB database connection for mergers collection"""
    _load_env_file(ENV_FILE)

    mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
    if not mongodb_uri:
        raise ValueError("MongoDB connection string not found in .env")

    mongo_client = MongoClient(mongodb_uri)
    db = mongo_client.get_database()
    collection = db["mergers"]

    return collection, mongo_client


def get_all_mergers() -> Dict[str, Any]:
    """
    Get all merger records from MongoDB collection "mergers".

    Returns:
        Dictionary containing success status, data list, count, and any errors.
    """
    try:
        collection, mongo_client = _get_database_connection()

        # Fetch all documents from the mergers collection
        all_mergers = list(collection.find({}))

        # Remove _id field from each document (ObjectId is not JSON serializable)
        for merger in all_mergers:
            merger.pop("_id", None)

        # Close connection
        mongo_client.close()

        return {
            "success": True,
            "data": all_mergers,
            "count": len(all_mergers)
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "data": [],
            "count": 0
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Database error: {str(e)}",
            "data": [],
            "count": 0
        }

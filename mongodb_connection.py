#!/usr/bin/env python3
"""
MongoDB Connection Manager
==========================
Provides a global MongoDB connection that can be reused across the application.
Connection is initialized once and reused for better performance.
"""

import os
from pymongo import MongoClient
from typing import Optional, Tuple

# Global connection variables
_mongo_client: Optional[MongoClient] = None
_db = None
_deals_collection = None
_mergers_collection = None


def _load_env_file(env_path: str = ".env") -> None:
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


def init_mongodb_connection(env_path: str = ".env") -> Tuple[bool, str]:
    """
    Initialize global MongoDB connection.
    
    Args:
        env_path: Path to .env file
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    global _mongo_client, _db, _deals_collection, _mergers_collection
    
    try:
        _load_env_file(env_path)
        
        mongodb_uri = os.environ.get("MONGODB_CONNECTION_STRING")
        if not mongodb_uri:
            return False, "MongoDB connection string not found in environment variables"
        
        # Create connection with timeouts
        _mongo_client = MongoClient(
            mongodb_uri,
            serverSelectionTimeoutMS=5000,  # 5 second timeout for server selection/DNS
            connectTimeoutMS=5000,  # 5 second timeout for connection
            socketTimeoutMS=10000,  # 10 second timeout for socket operations
            retryWrites=True,
            retryReads=True
        )
        
        # Test connection
        _mongo_client.admin.command('ping')
        
        # Get database and collections
        _db = _mongo_client.get_database()
        _deals_collection = _db["deals"]
        _mergers_collection = _db["mergers"]
        
        return True, "MongoDB connection established successfully"
        
    except Exception as e:
        error_msg = str(e)
        if "DNS" in error_msg or "timeout" in error_msg.lower() or "resolution" in error_msg.lower():
            return False, f"MongoDB connection failed: DNS/Network timeout. Check your connection string and network."
        else:
            return False, f"MongoDB connection failed: {error_msg[:200]}"


def get_mongo_client() -> Optional[MongoClient]:
    """Get the global MongoDB client."""
    return _mongo_client


def get_database():
    """Get the global database instance."""
    return _db


def get_deals_collection():
    """Get the deals collection."""
    return _deals_collection


def get_mergers_collection():
    """Get the mergers collection."""
    return _mergers_collection


def close_mongodb_connection():
    """Close the global MongoDB connection."""
    global _mongo_client, _db, _deals_collection, _mergers_collection
    
    if _mongo_client:
        try:
            _mongo_client.close()
        except:
            pass
    
    _mongo_client = None
    _db = None
    _deals_collection = None
    _mergers_collection = None


def is_connected() -> bool:
    """Check if MongoDB connection is active."""
    if not _mongo_client:
        return False
    try:
        _mongo_client.admin.command('ping')
        return True
    except:
        return False


def get_deal_by_id(deal_id: str) -> Optional[dict]:
    """
    Fetch a single deal document from MongoDB by its string ID.

    Returns the deal dict with 'deal_id' key set (and '_id' removed),
    or None if not found or on any error.
    """
    try:
        from bson import ObjectId
        collection = get_deals_collection()
        if collection is None:
            return None
        deal = collection.find_one({"_id": ObjectId(deal_id)})
        if not deal:
            return None
        deal["deal_id"] = str(deal["_id"])
        deal.pop("_id", None)
        return deal
    except Exception:
        return None

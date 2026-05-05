#!/usr/bin/env python3
"""
AWS S3 utility helpers.
"""

from __future__ import annotations

import os
from typing import Any


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required AWS env var: {name}")
    return value


def build_docket_key(filename: str) -> str:
    clean_name = (filename or "").strip().replace("/", "_")
    if not clean_name:
        raise ValueError("Filename is required to build S3 key")
    return f"docket/{clean_name}"


def build_public_s3_url(bucket: str, region: str, key: str) -> str:
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_bytes_to_s3(
    file_bytes: bytes,
    key: str,
    content_type: str = "application/pdf",
) -> dict[str, Any]:
    if not file_bytes:
        raise ValueError("Cannot upload empty file bytes to S3")

    aws_access_key_id = _require_env("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = _require_env("AWS_SECRET_ACCESS_KEY")
    bucket = _require_env("AWS_S3_BUCKET")
    region = _require_env("AWS_REGION")

    try:
        import boto3
    except ImportError as exc:
        raise ImportError("boto3 is required for S3 upload") from exc

    s3_client = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )

    return {
        "bucket": bucket,
        "region": region,
        "key": key,
        "url": build_public_s3_url(bucket=bucket, region=region, key=key),
    }

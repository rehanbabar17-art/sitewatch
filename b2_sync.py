#!/usr/bin/env python3
"""Sync sitewatch products and CSV history files with a private B2 prefix."""

from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PREFIX = "sitewatch/"
PRODUCTS_KEY = f"{PREFIX}products.json"
ROOT = Path.cwd()
PRODUCTS_FILE = ROOT / "products.json"


def client():
    required = ["B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing B2 configuration: {', '.join(missing)}")
    endpoint = os.environ.get("B2_ENDPOINT", "https://s3.us-east-005.backblazeb2.com").rstrip("/")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-005",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        config=Config(signature_version="s3v4"),
    )


def is_missing(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def list_keys(s3, bucket: str) -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        args = {"Bucket": bucket, "Prefix": PREFIX}
        if token:
            args["ContinuationToken"] = token
        response = s3.list_objects_v2(**args)
        keys.extend(item["Key"] for item in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")


def download() -> bool:
    s3 = client()
    bucket = os.environ["B2_BUCKET"]
    try:
        products = s3.get_object(Bucket=bucket, Key=PRODUCTS_KEY)["Body"].read()
    except ClientError as error:
        if is_missing(error):
            print("[B2] No sitewatch/ data exists yet; retaining the restored Actions cache for one-time migration.")
            return True
        raise

    data = json.loads(products)
    if not isinstance(data, list):
        raise RuntimeError("sitewatch/products.json must contain a JSON array")
    PRODUCTS_FILE.write_bytes(products)

    for csv_file in ROOT.glob("*.csv"):
        csv_file.unlink()
    for key in list_keys(s3, bucket):
        if key == PRODUCTS_KEY or not key.endswith(".csv"):
            continue
        name = key.removeprefix(PREFIX)
        if "/" in name or not name:
            continue
        (ROOT / name).write_bytes(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    print(f"[B2] Restored sitewatch products and CSV history from {PREFIX}.")
    return True


def upload() -> bool:
    if not PRODUCTS_FILE.exists():
        raise RuntimeError("products.json is missing; cannot upload sitewatch data")
    products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(products, list):
        raise RuntimeError("products.json must contain a JSON array")

    s3 = client()
    bucket = os.environ["B2_BUCKET"]
    existing = list_keys(s3, bucket)
    wanted = {PRODUCTS_KEY}
    s3.put_object(Bucket=bucket, Key=PRODUCTS_KEY, Body=PRODUCTS_FILE.read_bytes(), ContentType="application/json")
    for csv_file in sorted(ROOT.glob("*.csv")):
        key = f"{PREFIX}{csv_file.name}"
        wanted.add(key)
        s3.put_object(Bucket=bucket, Key=key, Body=csv_file.read_bytes(), ContentType="text/csv")
    for key in existing:
        if key.startswith(PREFIX) and key not in wanted:
            s3.delete_object(Bucket=bucket, Key=key)
    print(f"[B2] Uploaded sitewatch products and {len(wanted) - 1} CSV history file(s) to {PREFIX}.")
    return True


def clear_history() -> bool:
    removed = 0
    for csv_file in ROOT.glob("*.csv"):
        csv_file.unlink()
        removed += 1
    print(f"[B2] Cleared {removed} local sitewatch CSV history file(s); the next upload will remove old B2 history.")
    return True


command = os.environ.get("B2_COMMAND", "")
try:
    if command == "download":
        ok = download()
    elif command == "upload":
        ok = upload()
    elif command == "clear-history":
        ok = clear_history()
    else:
        raise RuntimeError("Set B2_COMMAND to download, upload, or clear-history")
except Exception as error:
    print(f"[B2] sitewatch sync failed: {error}")
    raise SystemExit(1)

if not ok:
    raise SystemExit(1)

# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Keep each completed verification in Neon Object Storage (opt-in, DOCUMENT_STORAGE=on).

One verification = two objects under one prefix:
  docs     users/<user id>/<UTC time>-<uuid>/document<.pdf|.docx|.txt>   (the original upload)
  reports  users/<user id>/<UTC time>-<uuid>/report.json                (the /api/verify response body)

Best effort: a failure is logged (exception class, which object, S3 error code - never the
filename or document text) and never changes the response or the credit charge. Everything
here is read at call time and boto3 is imported only when storing, so with the flag off
nothing loads (see CLAUDE.md, env import-order trap). Hashing, serialization and the S3 calls
run on a dedicated thread pool - never on the event loop, and never queued behind extraction
or the verifiers on the default executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, List, Optional

from pydantic import BaseModel

from utils.logger import get_logger

logger = get_logger()

DOCS_BUCKET = "docs"
REPORTS_BUCKET = "reports"
STORE_TIMEOUT_S = 30.0  # both uploads together; the response waits at most this long

_REQUIRED_SETTINGS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "AWS_REGION")
_TRUTHY = {"1", "true", "yes", "on"}
# From the validated extension, never from the client's Content-Type.
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}

_CLIENT: Optional[Any] = None
_CLIENT_LOCK = threading.Lock()
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def storage_enabled() -> bool:
    return os.getenv("DOCUMENT_STORAGE", "off").strip().lower() in _TRUTHY


def missing_settings() -> List[str]:
    return [name for name in _REQUIRED_SETTINGS if not os.getenv(name)]


def _client() -> Any:
    """The shared S3 client, built once. boto3's default session isn't thread-safe, so the
    client comes from a private session, built under a lock with explicit settings (no
    ~/.aws profile or session token). The finished client is safe to share across threads."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                import boto3
                from botocore.config import Config

                config = Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},  # Neon requires path-style addressing
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"mode": "standard", "total_max_attempts": 3},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                )
                _CLIENT = boto3.session.Session().client(
                    "s3",
                    endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
                    region_name=os.environ["AWS_REGION"],
                    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                    config=config,
                )
    return _CLIENT


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="object-storage")
    return _EXECUTOR


def head_bucket(bucket: str) -> None:
    """Raise if `bucket` isn't reachable with the configured credentials (check_config.py)."""
    _client().head_bucket(Bucket=bucket)


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return "-"


def _store(user_id: int, extension: str, document: bytes, report: BaseModel) -> None:
    """Runs on the storage thread pool. Logs its own outcome and never raises."""
    started = time.monotonic()
    prefix = f"users/{user_id}/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
    stage = "client"
    try:
        client = _client()
        # by_alias=True matches how FastAPI serializes the route's response_model.
        report_body = report.model_dump_json(by_alias=True).encode("utf-8")
        stage = "document"
        client.put_object(
            Bucket=DOCS_BUCKET,
            Key=f"{prefix}/document{extension}",
            Body=document,
            ContentType=_CONTENT_TYPES[extension],
            Metadata={"sha256": hashlib.sha256(document).hexdigest()},
        )
        stage = "report"
        client.put_object(
            Bucket=REPORTS_BUCKET,
            Key=f"{prefix}/report.json",
            Body=report_body,
            ContentType="application/json",
            Metadata={"sha256": hashlib.sha256(report_body).hexdigest()},
        )
    except Exception as exc:  # best effort by design
        detail = " (the document was stored without its report)" if stage == "report" else ""
        logger.error(f"Document storage failed at {stage} for {prefix}: {type(exc).__name__}, code {_error_code(exc)}{detail}")
        return
    logger.info(f"Stored verification {prefix} ({len(document)} + {len(report_body)} bytes, {(time.monotonic() - started) * 1000:.0f} ms)")


async def store_verification(user_id: int, extension: str, document: bytes, report: BaseModel) -> None:
    """Store the upload in `docs` and the report in `reports`. Never raises."""
    missing = missing_settings()
    if missing:
        logger.error(f"DOCUMENT_STORAGE is on but {', '.join(missing)} not set; nothing stored.")
        return
    if extension not in _CONTENT_TYPES:
        logger.error(f"Document storage skipped: unexpected extension {extension!r}.")
        return
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor(), _store, user_id, extension, document, report)
    try:
        # On timeout a job that hasn't started is cancelled; one already uploading runs on
        # until botocore's own timeouts end it.
        await asyncio.wait_for(future, timeout=STORE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error(f"Document storage timed out after {STORE_TIMEOUT_S:.0f} s; an upload already in progress may still complete.")
    except Exception as exc:  # best effort by design
        logger.error(f"Document storage failed: {type(exc).__name__}")

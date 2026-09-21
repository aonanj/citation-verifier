# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Cache of normalized derivatives (plan section 13), OFF by default.

Keyed by SHA-256(source bytes) + NORMALIZATION_VERSION + OCR_CONFIG_VERSION + the settings that change the
result. Per key it keeps the block sidecar (with the page metrics and the tagged DOCX text) and, when one was
created, the derivative PDF. The original upload is never cached.

Why it is off: the sidecar and any derivative contain the document's text. The Terms and FAQ tell users their
documents are not kept, and results are deliberately not persisted (CLAUDE.md, frontend conventions). Enabling
NORMALIZATION_CACHE=disk keeps that content on this server for NORMALIZATION_CACHE_TTL_S seconds (default 900),
inside a directory only this user can read, capped at NORMALIZATION_CACHE_MAX_MB, and is a decision for the
product owner. A cache error never fails a request: the document is just normalized again.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from typing import Any, Dict, Tuple

from svc.normalization.config import NormalizationConfig
from svc.normalization.models import NORMALIZATION_VERSION, OCR_CONFIG_VERSION, NormalizedDocument
from utils.logger import get_logger

logger = get_logger()


class NormalizationCache:
    def __init__(self, config: NormalizationConfig) -> None:
        self.config = config
        self.enabled = config.cache_mode == "disk"
        self.directory = config.cache_dir or os.path.join(tempfile.gettempdir(), "jurischeck-norm-cache")

    def key(self, sha256: str) -> str:
        cfg = self.config
        material = "|".join((sha256, NORMALIZATION_VERSION, OCR_CONFIG_VERSION, cfg.ocr_fingerprint(), cfg.content_fingerprint()))
        return hashlib.sha256(material.encode()).hexdigest()

    def _entry(self, key: str) -> str:
        return os.path.join(self.directory, key)

    # --- read -------------------------------------------------------------------------------

    def get(self, sha256: str, work_dir: str, source_path: str) -> NormalizedDocument | None:
        """The cached document, its derivative copied into `work_dir`, or None (miss, expired, unreadable)."""
        if not self.enabled:
            return None
        entry = self._entry(self.key(sha256))
        sidecar = os.path.join(entry, "doc.json")
        try:
            if time.time() - os.path.getmtime(sidecar) > self.config.cache_ttl_s:
                shutil.rmtree(entry, ignore_errors=True)
                return None
            with open(sidecar) as handle:
                data: Dict[str, Any] = json.load(handle)
            document = NormalizedDocument.from_dict(data["document"])
            derivative = os.path.join(entry, "derivative.pdf")
            document.source_path = source_path
            document.work_dir = work_dir
            if data.get("has_derivative"):
                copy = os.path.join(work_dir, "derivative.pdf")
                shutil.copyfile(derivative, copy)
                document.model_input_path = copy
                document.display_source_path = copy if data.get("display") == "derivative" else source_path
            else:
                document.model_input_path = source_path if document.model_input_kind == "pdf" else None
                document.display_source_path = source_path
            document.telemetry["cache_hit"] = True
            return document
        except (OSError, ValueError, KeyError):
            return None
        except Exception as exc:  # a damaged entry must never break a request
            logger.warning("Normalization cache read failed: %s", type(exc).__name__)
            return None

    # --- write ------------------------------------------------------------------------------

    def put(self, sha256: str, document: NormalizedDocument) -> None:
        if not self.enabled:
            return
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            os.chmod(self.directory, 0o700)
            self.purge()
            entry = self._entry(self.key(sha256))
            staging = tempfile.mkdtemp(prefix=".tmp-", dir=self.directory)
            derived = document.model_input_path is not None and document.model_input_path != document.source_path
            if derived:
                shutil.copyfile(document.model_input_path, os.path.join(staging, "derivative.pdf"))  # type: ignore[arg-type]
            with open(os.path.join(staging, "doc.json"), "w") as handle:
                json.dump({
                    "document": document.to_dict(), "has_derivative": derived,
                    "display": "derivative" if derived and document.display_source_path == document.model_input_path else "source",
                }, handle)
            shutil.rmtree(entry, ignore_errors=True)
            os.replace(staging, entry)
        except Exception as exc:
            logger.warning("Normalization cache write failed: %s", type(exc).__name__)

    def purge(self) -> None:
        """Delete expired entries, then the oldest ones until the directory fits its size cap."""
        try:
            now = time.time()
            entries = []
            for name in os.listdir(self.directory):
                path = os.path.join(self.directory, name)
                if not os.path.isdir(path):
                    continue
                mtime = os.path.getmtime(path)
                if name.startswith(".tmp-") or now - mtime > self.config.cache_ttl_s:
                    shutil.rmtree(path, ignore_errors=True)
                    continue
                entries.append((mtime, path, self._size(path)))
            total = sum(size for _, _, size in entries)
            for _, path, size in sorted(entries):
                if total <= self.config.cache_max_bytes:
                    break
                shutil.rmtree(path, ignore_errors=True)
                total -= size
        except OSError:
            pass

    @staticmethod
    def _size(path: str) -> int:
        total = 0
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total


def cache_stats(cache: NormalizationCache) -> Tuple[int, int]:
    """(entries, bytes) currently cached (for tests and check_config)."""
    try:
        entries = [os.path.join(cache.directory, n) for n in os.listdir(cache.directory)]
    except OSError:
        return 0, 0
    entries = [e for e in entries if os.path.isdir(e)]
    return len(entries), sum(cache._size(e) for e in entries)

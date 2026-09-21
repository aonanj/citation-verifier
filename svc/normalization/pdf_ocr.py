# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""OCR for deficient PDF pages (plan section 8.4), through the OCRmyPDF command line.

OCRmyPDF runs as a subprocess of this interpreter (`python -m ocrmypdf`), not through its Python API: the API
is not safe to call concurrently from a web server's threads, and a subprocess can be killed, with its
Tesseract children, when it overruns its timeout. The child gets a scrubbed environment (no API keys, no
database URL) and a scratch directory inside the request's own work directory.

The least destructive mode that fixes the problem is chosen by the caller:

  skip   pages with no text are OCRed, pages with text are skipped        (--skip-text)
  redo   a defective OCR layer is replaced, visible native text is kept,
         pages with no text are OCRed as well                            (--redo-ocr)
  force  the page is rasterized and OCRed: last resort                    (--force-ocr)

`--pages` limits every run to the deficient pages, so good native pages are never touched. Digital signatures
are never invalidated: `--invalidate-digital-signatures` is not used anywhere, and the caller does not run OCR
on a signed PDF at all.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Sequence

from svc.normalization.config import NormalizationConfig
from svc.normalization.models import NormalizationError
from utils.logger import get_logger

logger = get_logger()

_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TESSDATA_PREFIX", "VIRTUAL_ENV", "SYSTEMROOT")


def child_environment(scratch: str) -> Dict[str, str]:
    """A minimal environment for a converter subprocess: no secrets, temp files inside `scratch`."""
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    env["TMPDIR"] = scratch
    return env


def page_ranges(pages: Sequence[int]) -> str:
    """[1, 2, 3, 5] -> "1-3,5" (OCRmyPDF's --pages syntax)."""
    numbers = sorted(set(pages))
    parts: List[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:  # type: ignore[list-item]
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if number is not None:
            start = previous = number
    return ",".join(parts)


@dataclass
class OcrRun:
    mode: str
    pages: List[int]
    duration_ms: int


def ocr_available() -> bool:
    """Tesseract on PATH and the ocrmypdf package importable."""
    return shutil.which("tesseract") is not None and importlib.util.find_spec("ocrmypdf") is not None


class PdfOcrNormalizer:
    def __init__(self, config: NormalizationConfig) -> None:
        self.config = config

    @staticmethod
    def available() -> bool:
        return ocr_available()

    def command(self, source: str, dest: str, pages: Sequence[int], mode: str) -> List[str]:
        cfg = self.config
        rasterizer = cfg.ocr_rasterizer if importlib.util.find_spec("pypdfium2") is not None else "auto"
        args = [
            sys.executable, "-m", "ocrmypdf", "-q",
            "--output-type", "pdf",
            "-l", cfg.ocr_language,
            "--pages", page_ranges(pages),
            "--optimize", str(cfg.ocr_optimize),
            "--rasterizer", rasterizer,
            "--tagged-pdf-mode", "ignore",
            "--tesseract-timeout", str(max(int(cfg.ocr_timeout_s), 1)),
        ]
        args.append({"skip": "--skip-text", "redo": "--redo-ocr", "force": "--force-ocr"}[mode])
        if cfg.ocr_rotate_pages:
            args.append("--rotate-pages")
        if cfg.ocr_deskew and mode != "redo":  # OCRmyPDF does not combine --deskew with --redo-ocr
            args.append("--deskew")
        if cfg.ocr_jobs:
            args += ["--jobs", str(cfg.ocr_jobs)]
        return args + [source, dest]

    async def run(self, source: str, dest: str, pages: Sequence[int], mode: str, scratch: str) -> OcrRun:
        """OCR `pages` of `source` into `dest`. Raises NormalizationError (ocr_timeout / ocr_failed)."""
        if not pages:
            raise ValueError("no pages to OCR")
        args = self.command(source, dest, pages, mode)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            env=child_environment(scratch), start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self.config.ocr_timeout_s)
        except asyncio.TimeoutError as exc:
            self._kill(process)
            await process.wait()
            raise NormalizationError("ocr_timeout", "OCR did not finish in time.") from exc
        except BaseException:
            self._kill(process)
            raise
        if process.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "replace")[-600:]
            logger.warning("OCR failed (mode=%s, exit %s): %s", mode, process.returncode, tail)
            raise NormalizationError("ocr_failed", f"OCR failed (exit code {process.returncode}).")
        return OcrRun(mode, sorted(set(pages)), int((time.monotonic() - started) * 1000))

    @staticmethod
    def _kill(process: "asyncio.subprocess.Process") -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # the whole group: OCRmyPDF's Tesseract children too
        except (ProcessLookupError, PermissionError):
            pass


class OcrGate:
    """At most N OCR jobs at once per process (OCRmyPDF already uses every core); bound to the running loop."""

    def __init__(self, limit: int = 1) -> None:
        self.limit = limit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None

    def semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._semaphore is None:
            self._loop, self._semaphore = loop, asyncio.Semaphore(self.limit)
        return self._semaphore


_SHARED_GATE = OcrGate(1)


def shared_gate() -> OcrGate:
    """The process-wide gate (a DocumentNormalizer is created per request, its OCR limit must not be).

    NORMALIZATION_MAX_CONCURRENT_OCR (default 1) is read at call time.
    """
    limit = max(int(os.getenv("NORMALIZATION_MAX_CONCURRENT_OCR") or 1), 1)
    if _SHARED_GATE.limit != limit:
        _SHARED_GATE.limit = limit
        _SHARED_GATE._semaphore = None
    return _SHARED_GATE

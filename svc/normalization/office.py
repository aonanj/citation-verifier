# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""DOCX-to-PDF fallback renderer (plan section 3.3 and 7.3): LibreOffice, only for exceptional documents.

Used when a DOCX quality gate fires (a parser error, note references without bodies, text boxes and other
content the paragraph parser can't place, implausibly little text, or page numbers being required). The
rendered PDF replaces the tagged text as the model's input; the two are never sent together.

Two backends, both optional and both run as subprocesses with a timeout and a scrubbed environment:

  soffice     `soffice --headless --convert-to pdf`, one process per conversion, in a private profile directory;
  unoserver   `unoconvert`, the client of a persistent LibreOffice listener the operator runs (no start-up cost
              per document), selected with NORMALIZATION_OFFICE_BACKEND=unoserver.

LibreOffice is not installed by default (it is large); when it is missing the renderer is simply unavailable
and the caller keeps the tagged text and records a warning.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from typing import List

from svc.normalization.config import NormalizationConfig
from svc.normalization.models import NormalizationError
from svc.normalization.pdf_ocr import child_environment
from utils.logger import get_logger

logger = get_logger()

_MAC_BINARY = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


class LibreOfficeFallbackRenderer:
    def __init__(self, config: NormalizationConfig) -> None:
        self.config = config

    def _binary(self) -> str | None:
        cfg = self.config
        if cfg.office_backend == "unoserver":
            return cfg.unoconvert_bin or shutil.which("unoconvert")
        if cfg.libreoffice_bin:
            return cfg.libreoffice_bin if shutil.which(cfg.libreoffice_bin) or os.path.exists(cfg.libreoffice_bin) else None
        return shutil.which("soffice") or shutil.which("libreoffice") or (_MAC_BINARY if os.path.exists(_MAC_BINARY) else None)

    def available(self) -> bool:
        return self._binary() is not None

    def _command(self, binary: str, docx_path: str, out_dir: str, profile: str) -> List[str]:
        if self.config.office_backend == "unoserver":
            return [binary, "--convert-to", "pdf", docx_path, os.path.join(out_dir, "rendered.pdf")]
        return [
            binary, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile}", "--convert-to", "pdf", "--outdir", out_dir, docx_path,
        ]

    async def render(self, docx_path: str, out_dir: str) -> str:
        """Render `docx_path` to a PDF in `out_dir` and return its path. Raises NormalizationError."""
        binary = self._binary()
        if binary is None:
            raise NormalizationError("office_unavailable", "LibreOffice is not installed.")
        profile = os.path.join(out_dir, "lo-profile")
        os.makedirs(profile, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *self._command(binary, docx_path, out_dir, profile),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={**child_environment(out_dir), "HOME": out_dir}, start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=self.config.office_timeout_s)
        except asyncio.TimeoutError as exc:
            self._kill(process)
            await process.wait()
            raise NormalizationError("office_timeout", "Rendering the document timed out.") from exc
        except BaseException:
            self._kill(process)
            raise
        produced = os.path.join(out_dir, "rendered.pdf") if self.config.office_backend == "unoserver" else os.path.join(
            out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
        if process.returncode != 0 or not os.path.exists(produced):
            raise NormalizationError("office_failed", "The document could not be rendered to a PDF.")
        return produced

    @staticmethod
    def _kill(process: "asyncio.subprocess.Process") -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

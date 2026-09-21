# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""DocumentNormalizer: the provider-independent entry point of the normalization layer.

    normalizer = DocumentNormalizer()
    document = await normalizer.normalize_bytes(upload_bytes, filename)   # or normalize(path)
    try:
        ...                     # hand `document` to the citation extractor
    finally:
        document.close()        # removes the per-request scratch directory

One format-aware path per source (plan section 2). The original upload is read, never written or replaced;
everything derived lives in a unique 0700 scratch directory.

  DOCX  -> tagged text with individually addressable body, table, note and text-box blocks; a quality gate
           (svc.normalization.docx_normalizer) may ask for a LibreOffice-rendered PDF instead.
  PDF   -> inspected page by page. Good born-digital text passes through byte for byte; scanned pages get an
           OCR text layer (only those pages, least destructive mode); a defective OCR layer is replaced; a
           digitally signed file is never modified; an encrypted one is rejected.

Nothing here calls a model provider or judges whether an authority exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from svc.normalization.cache import NormalizationCache
from svc.normalization.config import NormalizationConfig
from svc.normalization.docx_normalizer import DocxNormalizer
from svc.normalization.models import (
    EXISTING_OCR_SUSPECT,
    IMAGE_ONLY,
    NATIVE_TEXT_GOOD,
    NATIVE_TEXT_SUSPECT,
    NORMALIZATION_VERSION,
    OCR_CONFIG_VERSION,
    DocumentBlock,
    NormalizationError,
    NormalizedDocument,
    PageMetrics,
    SourceLocation,
    UnsupportedDocumentError,
)
from svc.normalization.office import LibreOfficeFallbackRenderer
from svc.normalization.pdf_inspector import PageInspection, PdfInspector, decrypt_copy
from svc.normalization.pdf_ocr import PdfOcrNormalizer, page_ranges, shared_gate
from svc.normalization.text import normalize_for_match, sha256_file
from utils.logger import get_logger

logger = get_logger()

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MEDIA_TYPE = "application/pdf"


def sniff_format(head: bytes) -> str | None:
    """"pdf" or "docx" from the file's own bytes; the filename is never trusted."""
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "docx"  # a zip; DocxNormalizer.check_package confirms it is a Word document
    return None


@dataclass
class _PdfOutcome:
    model_path: str
    pre_ocr_metrics: List[PageMetrics]
    inspections: List[PageInspection]
    warnings: List[str] = field(default_factory=list)
    telemetry: Dict[str, Any] = field(default_factory=dict)


class DocumentNormalizer:
    def __init__(
        self,
        config: NormalizationConfig | None = None,
        *,
        office: Any = None,
        ocr: Any = None,
        cache: NormalizationCache | None = None,
    ) -> None:
        """`office` and `ocr` may be replaced (tests) by objects with the same `available()` / async `render()` / `run()`."""
        self.config = config or NormalizationConfig.from_env()
        self.office = office or LibreOfficeFallbackRenderer(self.config)
        self.inspector = PdfInspector(self.config)
        self.ocr = ocr or PdfOcrNormalizer(self.config)
        self.cache = cache or NormalizationCache(self.config)
        self._ocr_gate = shared_gate()

    # --- public API --------------------------------------------------------------------------

    async def normalize(self, source_path: str, *, source_filename: str | None = None) -> NormalizedDocument:
        """Normalize the file at `source_path` (which is only read)."""
        try:
            with open(source_path, "rb") as handle:
                kind = sniff_format(handle.read(4096))
        except OSError as exc:
            raise UnsupportedDocumentError("unreadable", "The uploaded file could not be read.") from exc
        if kind is None:
            raise UnsupportedDocumentError("unsupported_format", "Only PDF and Word (.docx) documents are supported.")
        work_dir = self._new_work_dir()
        return await self._guarded(source_path, work_dir, kind, source_filename or os.path.basename(source_path))

    async def normalize_bytes(self, data: bytes, filename: str) -> NormalizedDocument:
        """Normalize an upload held in memory: written to a private scratch directory under a name of our
        own (never the client's filename, which is only carried along as metadata)."""
        if len(data) > self.config.max_source_bytes:
            raise UnsupportedDocumentError(
                "too_large", f"This file is larger than the {self.config.max_source_bytes // (1024 * 1024)} MB limit.")
        kind = sniff_format(data[:4096])
        if kind is None:
            raise UnsupportedDocumentError("unsupported_format", "Only PDF and Word (.docx) documents are supported.")
        work_dir = self._new_work_dir()
        path = os.path.join(work_dir, f"source.{kind}")
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except BaseException:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        return await self._guarded(path, work_dir, kind, filename)

    # --- plumbing ----------------------------------------------------------------------------

    def _new_work_dir(self) -> str:
        if self.config.work_root:
            os.makedirs(self.config.work_root, mode=0o700, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="jurischeck-norm-", dir=self.config.work_root)
        os.chmod(work_dir, 0o700)
        os.makedirs(os.path.join(work_dir, "tmp"), exist_ok=True)
        return work_dir

    async def _guarded(self, path: str, work_dir: str, kind: str, filename: str) -> NormalizedDocument:
        try:
            return await asyncio.wait_for(self._run(path, work_dir, kind, filename), timeout=self.config.total_timeout_s)
        except asyncio.TimeoutError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise NormalizationError("normalization_timeout", "Preparing the document took too long.") from exc
        except BaseException:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    async def _run(self, path: str, work_dir: str, kind: str, filename: str) -> NormalizedDocument:
        started = time.monotonic()
        size = os.path.getsize(path)
        if size > self.config.max_source_bytes:
            raise UnsupportedDocumentError(
                "too_large", f"This file is larger than the {self.config.max_source_bytes // (1024 * 1024)} MB limit.")
        sha = await asyncio.to_thread(sha256_file, path)
        cached = await asyncio.to_thread(self.cache.get, sha, work_dir, path)
        if cached is not None:
            cached.source_filename = filename
            cached.telemetry["normalization_duration_ms"] = int((time.monotonic() - started) * 1000)
            self._log(cached)
            return cached

        document = await (self._pdf(path, work_dir, sha, filename) if kind == "pdf" else self._docx(path, work_dir, sha, filename))
        document.source_path = path
        document.work_dir = work_dir
        document.telemetry.update({
            "source_format": kind,
            "source_size_bytes": size,
            "model_input_kind": document.model_input_kind,
            "fallback_reason": document.fallback_reason,
            "normalization_duration_ms": int((time.monotonic() - started) * 1000),
            "cache_hit": False,
        })
        self._log(document)
        await asyncio.to_thread(self.cache.put, sha, document)
        return document

    @staticmethod
    def _log(document: NormalizedDocument) -> None:
        """One structured line of numbers and codes; never document text or the filename."""
        logger.info("Document normalization: %s", json.dumps(document.telemetry, sort_keys=True, default=str))

    # --- PDF ---------------------------------------------------------------------------------

    async def _inspect_pdf(self, path: str, work_dir: str, allow_ocr: bool) -> _PdfOutcome:
        cfg = self.config
        pre = await asyncio.to_thread(self.inspector.preflight, path)
        warnings: List[str] = []
        telemetry: Dict[str, Any] = {"page_count": pre.page_count, "pdf_signed": pre.signed}
        model_path = path
        if pre.owner_only_encrypted:
            model_path = os.path.join(work_dir, "unrestricted.pdf")
            await asyncio.to_thread(decrypt_copy, path, model_path)
            warnings.append("pdf_encrypted_owner_only")

        inspections = await asyncio.to_thread(self.inspector.inspect, model_path, pre.page_count)
        metrics = [i.metrics for i in inspections]
        image_only = [m.page for m in metrics if m.classification == IMAGE_ONLY]
        ocr_suspect = [m.page for m in metrics if m.classification == EXISTING_OCR_SUSPECT]
        native_suspect = [m.page for m in metrics if m.classification == NATIVE_TEXT_SUSPECT]
        forced = native_suspect if cfg.ocr_force_suspect_native else []
        wanted = sorted(set(image_only + ocr_suspect + forced))
        telemetry.update({
            "pages_native_good": sum(1 for m in metrics if m.classification == NATIVE_TEXT_GOOD),
            "pages_ocr_requested": 0, "pages_ocr_completed": 0, "ocr_duration_ms": 0,
        })

        if wanted and allow_ocr:
            if pre.signed:
                warnings.append("pdf_signed_not_modified")  # OCR would invalidate the signature: keep the original
            elif not self.ocr.available():
                warnings.append("ocr_unavailable")
            else:
                telemetry["pages_ocr_requested"] = len(wanted)
                try:
                    model_path, ocr_ms = await self._ocr(model_path, work_dir, image_only, ocr_suspect, forced)
                    fresh = await asyncio.to_thread(self.inspector.inspect_pages, model_path, wanted)
                    for inspection in fresh:
                        inspections[inspection.metrics.page - 1] = inspection
                    telemetry["ocr_duration_ms"] = ocr_ms
                    telemetry["pages_ocr_completed"] = sum(1 for i in fresh if i.metrics.word_count > 0)
                    telemetry["post_ocr_classes"] = {str(i.metrics.page): i.metrics.classification for i in fresh}
                except NormalizationError as exc:
                    warnings.append(f"ocr_failed:{page_ranges(wanted)}")
                    telemetry["ocr_error"] = exc.code
                except Exception as exc:  # an unreadable derivative is an OCR failure, not a crashed request
                    warnings.append(f"ocr_failed:{page_ranges(wanted)}")
                    telemetry["ocr_error"] = type(exc).__name__
        if native_suspect and not forced:
            warnings.append(f"pdf_text_layer_suspect:{page_ranges(native_suspect)}")
        telemetry["pages_classes"] = {c: sum(1 for m in metrics if m.classification == c) for c in sorted({m.classification for m in metrics})}
        return _PdfOutcome(model_path, metrics, inspections, warnings, telemetry)

    async def _ocr(self, path: str, work_dir: str, image_only: List[int], ocr_suspect: List[int], forced: List[int]) -> tuple[str, int]:
        scratch = os.path.join(work_dir, "tmp")
        total_ms = 0
        current = path
        async with self._ocr_gate.semaphore():
            first = sorted(set(image_only + ocr_suspect))
            if first:
                dest = os.path.join(work_dir, "ocr-1.pdf")
                run = await self.ocr.run(current, dest, first, "redo" if ocr_suspect else "skip", scratch)
                total_ms += run.duration_ms
                current = dest
            if forced:
                dest = os.path.join(work_dir, "ocr-2.pdf")
                run = await self.ocr.run(current, dest, forced, "force", scratch)
                total_ms += run.duration_ms
                current = dest
        final = os.path.join(work_dir, "normalized.pdf")
        os.replace(current, final)
        return final, total_ms

    @staticmethod
    def _page_blocks(outcome: _PdfOutcome) -> List[DocumentBlock]:
        blocks = []
        for inspection, before in zip(outcome.inspections, outcome.pre_ocr_metrics):
            page = inspection.metrics.page
            formatting: Dict[str, bool | str] = {"classification": before.classification}
            if inspection.metrics.columns > 1:
                formatting["two_column"] = True
            blocks.append(DocumentBlock(
                "page", inspection.text, normalize_for_match(inspection.text).text,
                SourceLocation(f"pg-{page}", page=page), None, formatting))
        return blocks

    async def _pdf(self, path: str, work_dir: str, sha: str, filename: str) -> NormalizedDocument:
        outcome = await self._inspect_pdf(path, work_dir, allow_ocr=True)
        return NormalizedDocument(
            source_filename=filename, source_media_type=PDF_MEDIA_TYPE, source_sha256=sha,
            model_input_kind="pdf", model_input_path=outcome.model_path, model_input_text=None,
            blocks=self._page_blocks(outcome), warnings=outcome.warnings,
            normalization_version=f"{NORMALIZATION_VERSION}/{OCR_CONFIG_VERSION}",
            page_metrics=outcome.pre_ocr_metrics,
            telemetry={**outcome.telemetry, "pass_through": outcome.model_path == path},
            display_source_path=outcome.model_path, display_source_kind="pdf",
        )

    # --- DOCX --------------------------------------------------------------------------------

    async def _docx(self, path: str, work_dir: str, sha: str, filename: str) -> NormalizedDocument:
        result = await asyncio.to_thread(DocxNormalizer(self.config).normalize, path)
        warnings = list(result.warnings)
        telemetry: Dict[str, Any] = dict(result.telemetry)
        version = f"{NORMALIZATION_VERSION}/{OCR_CONFIG_VERSION}"
        fallback = result.fallback_reasons[0] if result.fallback_reasons else None
        if fallback:
            telemetry["fallback_reasons"] = list(result.fallback_reasons)
            if self.office.available():
                try:
                    rendered = await self.office.render(path, work_dir)
                    outcome = await self._inspect_pdf(rendered, work_dir, allow_ocr=False)
                    return NormalizedDocument(
                        source_filename=filename, source_media_type=DOCX_MEDIA_TYPE, source_sha256=sha,
                        model_input_kind="pdf", model_input_path=outcome.model_path, model_input_text=None,
                        blocks=self._page_blocks(outcome), warnings=warnings + outcome.warnings,
                        normalization_version=version, page_metrics=outcome.pre_ocr_metrics,
                        telemetry={**telemetry, **outcome.telemetry, "rendered_by_office": True},
                        fallback_reason=fallback, display_source_path=path, display_source_kind="docx",
                    )
                except NormalizationError as exc:
                    telemetry["office_error"] = exc.code
                except Exception as exc:  # a render we can't use is a failed render, not a failed request
                    telemetry["office_error"] = type(exc).__name__
            if not result.blocks:
                raise NormalizationError("docx_unreadable", "This Word document could not be read.")
            warnings.append("office_renderer_unavailable")
        return NormalizedDocument(
            source_filename=filename, source_media_type=DOCX_MEDIA_TYPE, source_sha256=sha,
            model_input_kind="tagged_text", model_input_path=None, model_input_text=result.tagged_text,
            blocks=result.blocks, warnings=warnings, normalization_version=version, telemetry=telemetry,
            fallback_reason=fallback, display_source_path=path, display_source_kind="docx",
        )

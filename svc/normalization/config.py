# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Configuration of the document-normalization layer, read from the environment at call time.

Call-time reads matter: .env is loaded after project modules are imported (CLAUDE.md, env import-order trap).
Every setting has a default that is safe for a small server; none is required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict

_TRUE = {"1", "true", "yes", "on"}


def _env_str(name: str, default: str | None = None) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in _TRUE


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def normalization_enabled() -> bool:
    """DOCUMENT_NORMALIZATION=on turns the layer on for the LLM extractor (default off)."""
    return _env_bool("DOCUMENT_NORMALIZATION", False)


def normalization_active() -> bool:
    """The layer runs only when it is on AND the LLM extractor is selected: the rules extractor never uses it."""
    return normalization_enabled() and (os.getenv("CITATION_EXTRACTOR") or "rules").strip().lower() == "llm"


@dataclass(frozen=True)
class PdfThresholds:
    """Page-classification thresholds (plan section 8.2). Heuristic starting values: tune them from
    the component metrics stored on every page (NormalizedDocument.page_metrics), not by guessing."""

    # A page with fewer extracted words than this counts as having no meaningful text.
    min_words_for_text: int = 5
    # A page image covering at least this share of the page is a scan-like image.
    image_page_coverage: float = 0.30
    # ... and a scan-like image with fewer words than this is suspicious (text-heavy page, little text).
    full_image_min_words: int = 25
    full_image_coverage: float = 0.60
    # Dark-pixel share above which a page with no text is visibly not blank (vector-outlined text).
    ink_visible_ratio: float = 0.008
    # Pages with at most this many words are only rendered for the ink test.
    ink_test_max_words: int = 25
    # Share of U+FFFD / control / private-use characters and "(cid:N)" tokens above which text is damaged.
    max_bad_char_ratio: float = 0.04
    # Share of one-letter word tokens above which text is fragments ("S m i t h v. J o n e s").
    max_single_char_token_ratio: float = 0.30
    # Share of 4+ letter alphabetic tokens with no vowel above which text is garbled.
    max_vowelless_token_ratio: float = 0.30
    # Share of words with a usable bounding box below which coordinates are unreliable.
    min_words_with_boxes_ratio: float = 0.80
    # Minimum alphabetic tokens before the token-shape ratios are trusted.
    min_tokens_for_ratios: int = 20


@dataclass(frozen=True)
class NormalizationConfig:
    # --- resource limits (plan section 14) ---
    max_source_bytes: int = 50 * 1024 * 1024          # OpenAI's per-request file limit is also 50 MB
    max_docx_uncompressed_bytes: int = 200 * 1024 * 1024
    max_docx_entries: int = 5000
    max_compression_ratio: int = 100
    max_pdf_pages: int = 300
    # --- timeouts, seconds ---
    ocr_timeout_s: float = 300.0
    office_timeout_s: float = 120.0
    total_timeout_s: float = 420.0
    # --- OCR (plan section 8.4) ---
    ocr_language: str = "eng"
    ocr_rotate_pages: bool = True
    ocr_deskew: bool = True
    ocr_optimize: int = 0
    ocr_rasterizer: str = "pypdfium"
    ocr_jobs: int | None = None
    # Force OCR rasterizes content: last resort, off unless the operator opts in.
    ocr_force_suspect_native: bool = False
    # --- DOCX ---
    include_headers_footers: bool = False
    require_page_numbers: bool = False
    # --- LibreOffice fallback (optional) ---
    libreoffice_bin: str | None = None
    office_backend: str = "soffice"                 # "soffice" or "unoserver"
    unoconvert_bin: str | None = None
    # --- cache (off by default: cached derivatives hold document text; see CLAUDE.md item 35) ---
    cache_mode: str = "off"                          # "off" or "disk"
    cache_dir: str | None = None
    cache_ttl_s: float = 900.0
    cache_max_bytes: int = 512 * 1024 * 1024
    # --- misc ---
    work_root: str | None = None
    inspect_workers: int = 0                          # 0 or 1: sequential
    parallel_min_pages: int = 40
    pdf: PdfThresholds = field(default_factory=PdfThresholds)

    @classmethod
    def from_env(cls, **overrides: Any) -> "NormalizationConfig":
        mb = 1024 * 1024
        jobs = _env_int("NORMALIZATION_OCR_JOBS", 0)
        config = cls(
            max_source_bytes=_env_int("NORMALIZATION_MAX_SOURCE_MB", 50) * mb,
            max_docx_uncompressed_bytes=_env_int("NORMALIZATION_MAX_DOCX_UNCOMPRESSED_MB", 200) * mb,
            max_docx_entries=_env_int("NORMALIZATION_MAX_DOCX_ENTRIES", 5000),
            max_compression_ratio=_env_int("NORMALIZATION_MAX_COMPRESSION_RATIO", 100),
            max_pdf_pages=_env_int("NORMALIZATION_MAX_PDF_PAGES", 300),
            ocr_timeout_s=_env_float("NORMALIZATION_OCR_TIMEOUT_S", 300.0),
            office_timeout_s=_env_float("NORMALIZATION_OFFICE_TIMEOUT_S", 120.0),
            total_timeout_s=_env_float("NORMALIZATION_TIMEOUT_S", 420.0),
            ocr_language=_env_str("NORMALIZATION_OCR_LANGUAGE", "eng") or "eng",
            ocr_rotate_pages=_env_bool("NORMALIZATION_OCR_ROTATE", True),
            ocr_deskew=_env_bool("NORMALIZATION_OCR_DESKEW", True),
            ocr_optimize=min(max(_env_int("NORMALIZATION_OCR_OPTIMIZE", 0), 0), 1),
            ocr_jobs=jobs if jobs > 0 else None,
            ocr_force_suspect_native=_env_bool("NORMALIZATION_FORCE_OCR_SUSPECT_NATIVE", False),
            include_headers_footers=_env_bool("NORMALIZATION_INCLUDE_HEADERS_FOOTERS", False),
            require_page_numbers=_env_bool("NORMALIZATION_REQUIRE_PAGE_NUMBERS", False),
            libreoffice_bin=_env_str("NORMALIZATION_LIBREOFFICE_BIN"),
            office_backend=(_env_str("NORMALIZATION_OFFICE_BACKEND", "soffice") or "soffice").lower(),
            unoconvert_bin=_env_str("NORMALIZATION_UNOCONVERT_BIN"),
            cache_mode=(_env_str("NORMALIZATION_CACHE", "off") or "off").lower(),
            cache_dir=_env_str("NORMALIZATION_CACHE_DIR"),
            cache_ttl_s=_env_float("NORMALIZATION_CACHE_TTL_S", 900.0),
            cache_max_bytes=_env_int("NORMALIZATION_CACHE_MAX_MB", 512) * mb,
            work_root=_env_str("NORMALIZATION_WORK_DIR"),
            # Sequential unless asked: each pdfplumber worker process costs tens of MB, which a small instance may not have.
            inspect_workers=_env_int("NORMALIZATION_INSPECT_WORKERS", 0),
            parallel_min_pages=_env_int("NORMALIZATION_PARALLEL_MIN_PAGES", 40),
        )
        return replace(config, **overrides) if overrides else config

    def ocr_fingerprint(self) -> str:
        """The settings that change what an OCRed derivative contains (part of the cache key)."""
        return (
            f"{self.ocr_language}|rot={int(self.ocr_rotate_pages)}|desk={int(self.ocr_deskew)}|"
            f"opt={self.ocr_optimize}|ras={self.ocr_rasterizer}|force={int(self.ocr_force_suspect_native)}"
        )

    def content_fingerprint(self) -> str:
        """Settings that change block extraction (part of the cache key)."""
        return f"hf={int(self.include_headers_footers)}|pn={int(self.require_page_numbers)}"


def config_summary(config: NormalizationConfig) -> Dict[str, Any]:
    """Non-sensitive settings for logs and check_config.py."""
    return {
        "max_source_mb": config.max_source_bytes // (1024 * 1024),
        "max_pdf_pages": config.max_pdf_pages,
        "ocr_language": config.ocr_language,
        "ocr_timeout_s": config.ocr_timeout_s,
        "cache_mode": config.cache_mode,
        "office_backend": config.office_backend,
        "inspect_workers": config.inspect_workers,
    }

# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Document normalization for AI citation extraction.

Prepares uploaded .docx and .pdf files for the LLM citation extractor while preserving citation
characters, footnote/endnote identity, document order, page or structural location, and a reliable
mapping from every returned citation back to the source. It never verifies an authority.

Only the light modules are imported here; docx2python, pdfplumber, OCRmyPDF and friends are imported
inside the functions that use them, so nothing heavy loads unless DOCUMENT_NORMALIZATION is on.
"""

from svc.normalization.config import NormalizationConfig, normalization_enabled
from svc.normalization.models import (
    NORMALIZATION_VERSION,
    DocumentBlock,
    NormalizationError,
    NormalizedDocument,
    SourceLocation,
    UnsupportedDocumentError,
)
from svc.normalization.validator import CitationSpanValidator, ValidationStatus

__all__ = [
    "NORMALIZATION_VERSION",
    "CitationSpanValidator",
    "DocumentBlock",
    "NormalizationConfig",
    "NormalizationError",
    "NormalizedDocument",
    "SourceLocation",
    "UnsupportedDocumentError",
    "ValidationStatus",
    "normalization_enabled",
]

# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Provider-independent data model of the document-normalization layer.

A NormalizedDocument is what the citation extractor consumes: the model-facing
input (tagged text for a DOCX, a PDF for a PDF) plus a server-side sidecar of
addressable blocks used to validate what the model returns. Nothing in this
package calls a model provider or judges whether an authority exists.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Tuple

# Bump when block extraction, page classification or the tagged-text format changes:
# it is part of the cache key.
NORMALIZATION_VERSION = "norm-1"
# Bump when the OCR command line or its defaults change.
OCR_CONFIG_VERSION = "ocr-1"

# "page" is the PDF sidecar block: one per page, in reading order.
BlockKind = Literal["paragraph", "footnote", "endnote", "table_cell", "header", "footer", "page"]
ModelInputKind = Literal["tagged_text", "pdf"]

# Page classes (plan section 8.2).
NATIVE_TEXT_GOOD = "NATIVE_TEXT_GOOD"
NATIVE_TEXT_SUSPECT = "NATIVE_TEXT_SUSPECT"
IMAGE_ONLY = "IMAGE_ONLY"
EXISTING_OCR_SUSPECT = "EXISTING_OCR_SUSPECT"
EMPTY_OR_DECORATIVE = "EMPTY_OR_DECORATIVE"
PAGE_CLASSES = (NATIVE_TEXT_GOOD, NATIVE_TEXT_SUSPECT, IMAGE_ONLY, EXISTING_OCR_SUSPECT, EMPTY_OR_DECORATIVE)

# What a user is told for a warning code (see NormalizedDocument.warnings). "{param}" is what follows the colon in
# the warning ("ocr_failed:2-3" -> "2-3").
USER_MESSAGES: Dict[str, str] = {
    "office_renderer_unavailable": (
        "Some content in this Word document (such as text boxes or drawings) may not have been read reliably, and "
        "the server could not render the document as a PDF to check it; citations in that content may be missed."
    ),
    "ocr_unavailable": "OCR is unavailable on this server; scanned pages could not be read and were not checked.",
    "ocr_failed": "OCR failed for page(s) {param}; citations on those pages may be missed.",
    "pdf_signed_not_modified": (
        "This PDF is digitally signed and was not modified; pages without a usable text layer may have been missed."
    ),
    "pdf_text_layer_suspect": "Page(s) {param} have a damaged text layer; citations on those pages may be missed.",
    "pdf_encrypted_owner_only": "This PDF has owner-password restrictions; an unrestricted copy was read.",
    "citations_unmatched": (
        "{param} citation(s) reported by the AI model could not be matched to the document text and were left out."
    ),
    "citations_unplaced": (
        "{param} citation(s) were found in the document but could not be placed in its extracted text (for example, "
        "inside a text box) and were left out."
    ),
}


class NormalizationError(Exception):
    """Normalization could not produce a usable document. `code` is stable (telemetry, tests)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class UnsupportedDocumentError(NormalizationError):
    """The upload must be rejected (encrypted, oversized, malformed archive, ...).

    The message is written for the user and is safe to show.
    """


@dataclass(frozen=True)
class SourceLocation:
    source_id: str
    page: int | None = None
    paragraph_index: int | None = None
    note_id: str | None = None
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    bbox: Tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class DocumentBlock:
    kind: BlockKind
    # The extraction result before any destructive normalization: display and audit.
    raw_text: str
    # Conservative comparison form (svc.normalization.text.normalize_for_match) used to locate citations.
    match_text: str
    location: SourceLocation
    anchor_source_id: str | None = None
    formatting: Dict[str, bool | str] = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        return self.location.source_id


@dataclass
class PageMetrics:
    """The components of a PDF page's classification, kept so thresholds can be tuned from real files."""

    page: int
    classification: str
    reasons: List[str]
    char_count: int
    alpha_count: int
    word_count: int
    printable_ratio: float
    bad_char_ratio: float
    single_char_token_ratio: float
    vowelless_token_ratio: float
    words_with_boxes_ratio: float
    image_count: int
    image_coverage: float
    ink_ratio: float | None
    columns: int
    # Median rotation of the page's text (a scan's tilt), corrected before words and lines are formed.
    skew_degrees: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedDocument:
    source_filename: str
    source_media_type: str
    source_sha256: str
    model_input_kind: ModelInputKind
    # A PDF path (the source itself when it passes through, else the derivative) for "pdf".
    model_input_path: str | None
    # The tagged text for "tagged_text".
    model_input_text: str | None
    blocks: List[DocumentBlock]
    # Codes ("ocr_failed", or "ocr_failed:2,3" with a parameter); see USER_MESSAGES.
    warnings: List[str]
    normalization_version: str
    page_metrics: List[PageMetrics] = field(default_factory=list)
    # Structured, text-free measurements (plan section 15); the extractor adds its own.
    telemetry: Dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None
    # The upload itself (a copy in work_dir when normalized from bytes). Never modified.
    source_path: str | None = None
    # The file whose text layer matches what the model reads, for the existing text extractor
    # (svc.doc_processor): the derivative for an OCRed PDF, the DOCX itself for a DOCX.
    display_source_path: str | None = None
    display_source_kind: Literal["pdf", "docx"] | None = None
    # Per-request scratch directory holding the source copy and derivatives; removed by close().
    work_dir: str | None = None
    _index: Dict[str, int] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        index: Dict[str, int] = {}
        for i, block in enumerate(self.blocks):
            if block.source_id in index:
                raise ValueError(f"duplicate source_id {block.source_id!r}")
            index[block.source_id] = i
        self._index = index

    def block_index(self, source_id: str) -> int | None:
        return self._index.get(source_id)

    def block(self, source_id: str) -> DocumentBlock | None:
        i = self._index.get(source_id)
        return None if i is None else self.blocks[i]

    def model_blocks(self) -> List[DocumentBlock]:
        """The blocks the model reads (headers and footers are validated against only when included)."""
        return [b for b in self.blocks if b.kind not in ("header", "footer")]

    def close(self) -> None:
        """Delete the scratch directory (idempotent). Cached derivatives live elsewhere."""
        if self.work_dir:
            shutil.rmtree(self.work_dir, ignore_errors=True)
            self.work_dir = None

    def warning_codes(self) -> List[str]:
        return [w.split(":", 1)[0] for w in self.warnings]

    def user_warnings(self) -> List[str]:
        """One user-facing sentence per distinct warning that has one."""
        messages: List[str] = []
        for warning in dict.fromkeys(self.warnings):
            code, _, param = warning.partition(":")
            template = USER_MESSAGES.get(code)
            if template:
                messages.append(template.format(param=param))
        return messages

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe form (cache sidecar, golden comparison). Paths are not included."""
        return {
            "source_filename": self.source_filename,
            "source_media_type": self.source_media_type,
            "source_sha256": self.source_sha256,
            "model_input_kind": self.model_input_kind,
            "model_input_text": self.model_input_text,
            "blocks": [
                {
                    "kind": b.kind, "raw_text": b.raw_text, "match_text": b.match_text,
                    "location": asdict(b.location), "anchor_source_id": b.anchor_source_id, "formatting": dict(b.formatting),
                }
                for b in self.blocks
            ],
            "warnings": list(self.warnings),
            "normalization_version": self.normalization_version,
            "page_metrics": [m.to_dict() for m in self.page_metrics],
            "telemetry": dict(self.telemetry),
            "fallback_reason": self.fallback_reason,
            "display_source_kind": self.display_source_kind,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NormalizedDocument":
        blocks = []
        for b in data["blocks"]:
            location = dict(b["location"])
            if location.get("bbox") is not None:
                location["bbox"] = tuple(location["bbox"])
            blocks.append(DocumentBlock(
                kind=b["kind"], raw_text=b["raw_text"], match_text=b["match_text"],
                location=SourceLocation(**location), anchor_source_id=b.get("anchor_source_id"),
                formatting=dict(b.get("formatting") or {}),
            ))
        return cls(
            source_filename=data["source_filename"], source_media_type=data["source_media_type"],
            source_sha256=data["source_sha256"], model_input_kind=data["model_input_kind"],
            model_input_path=None, model_input_text=data.get("model_input_text"), blocks=blocks,
            warnings=list(data.get("warnings") or []), normalization_version=data["normalization_version"],
            page_metrics=[PageMetrics(**m) for m in data.get("page_metrics") or []],
            telemetry=dict(data.get("telemetry") or {}), fallback_reason=data.get("fallback_reason"),
            display_source_kind=data.get("display_source_kind"),
        )

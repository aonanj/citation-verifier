"""Extract text content from uploaded documents, including footnotes.

This module provides functions to extract text from PDF, DOCX, and TXT files
while preserving footnote citations and their content inline.
"""

import os
import re
from typing import Final

import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from werkzeug.datastructures import FileStorage
from typing import Dict, Iterable, List
from xml.etree import ElementTree as ET

from docx import Document
from docx.document import Document as DocxDocument
from docx.text.paragraph import Paragraph
from docx.table import _Cell, Table  # type: ignore
from docx.oxml.table import CT_Tbl  # type: ignore
from docx.oxml.text.paragraph import CT_P  # type: ignore
from docx.oxml.ns import qn

_HYPHEN_WRAP_RE: Final = re.compile(r"(\w)-\n(\w)")
_EXCESS_BREAKS_RE: Final = re.compile(r"\n{3,}")
_SMART_QUOTES_RE: Final = re.compile("[\u201c\u201d]")
_SMART_APOSTROPHES_RE: Final = re.compile("[\u2018\u2019]")

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _W_NS}


def _load_footnotes_map(docx: DocxDocument) -> Dict[int, str]:
    """Parse word/footnotes.xml and return {footnote_id: text}."""
    mapping: Dict[int, str] = {}
    package_part = getattr(docx, "part", None)
    if package_part is None:
        return mapping
    package = getattr(package_part, "package", None)
    if package is None:
        return mapping
    footnotes_part = None
    for part in package.iter_parts():
        if str(part.partname) == "/word/footnotes.xml":
            footnotes_part = part
            break
    if footnotes_part is None:
        return mapping
    root = ET.fromstring(footnotes_part.blob)
    for fn in root.findall("w:footnote", _NS):
        fid = int(fn.get(f"{{{_W_NS}}}id", "-1"))
        if fid < 0:
            continue  # skip separators/continuation
        # Collect text paragraph-by-paragraph to preserve basic structure
        paras: List[str] = []
        for p in fn.findall(".//w:p", _NS):
            runs = [t.text or "" for t in p.findall(".//w:t", _NS)]
            txt = "".join(runs).strip()
            if txt:
                paras.append(txt)
        if not paras:
            # Fallback: any text nodes
            paras = [t.text or "" for t in fn.findall(".//w:t", _NS)]
        mapping[fid] = "\n".join([t for t in paras if t]).strip()
    return mapping

def _iter_table_paragraphs(tbl: Table) -> Iterable[Paragraph]:
    for row in tbl.rows:
        for cell in row.cells:
            yield from _iter_cell_paragraphs(cell)


def _iter_cell_paragraphs(cell: _Cell) -> Iterable[Paragraph]:
    for p in cell.paragraphs:
        yield p
    for t in cell.tables:
        yield from _iter_table_paragraphs(t)


def _iter_block_items(container: DocxDocument | _Cell) -> Iterable[Paragraph | Table]:
    if isinstance(container, DocxDocument):
        parent_elm = container.element.body  # type: ignore[union-attr]
    elif isinstance(container, _Cell):
        parent_elm = container._tc
    else:
        parent_elm = container._element  # type: ignore[attr-defined]
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, container)  # type: ignore[arg-type]
        elif isinstance(child, CT_Tbl):
            yield Table(child, container)  # type: ignore[arg-type]


def _para_with_inline_footnotes(p: Paragraph, footnotes: Dict[int, str]) -> str:
    parts: List[str] = []
    for run in p.runs:
        r = run._r
        refs = list(r.iter(qn("w:footnoteReference")))
        if refs:
            if run.text:
                parts.append(run.text)
            for ref in refs:
                fid = int(ref.get(qn("w:id")))
                ftxt = footnotes.get(fid, "").strip()
                parts.append(f"[{fid}: {ftxt}]")
            continue
        if run.text:
            parts.append(run.text)
    return "".join(parts).strip()

def _normalize(text: str) -> str:
    """Normalize text by removing artifacts and standardizing formatting.

    Args:
        text: Raw extracted text.

    Returns:
        Normalized text with consistent line breaks and punctuation.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SMART_QUOTES_RE.sub('"', text)
    text = _SMART_APOSTROPHES_RE.sub("'", text)
    text = _HYPHEN_WRAP_RE.sub(r"\1\2", text)
    text = _EXCESS_BREAKS_RE.sub("\n\n", text)
    return text.strip()


def _extract_pdf_footnotes(page: fitz.Page) -> str:
    """Extract footnote text from a PDF page.

    Args:
        page: PyMuPDF page object.

    Returns:
        Extracted footnote text, or empty string if none found.
    """
    text_dict = page.get_text("dict")
    footnote_parts = []

    if not isinstance(text_dict, dict):
        return ""

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:  # Skip non-text blocks
            continue

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                font_size = span.get("size", 0)

                # Heuristic: footnotes typically use smaller font
                # or appear in specific page regions (bottom)
                bbox = span.get("bbox", [0, 0, 0, 0])
                y_position = bbox[1]  # Top y-coordinate
                page_height = page.rect.height

                # Consider text in bottom 20% of page with smaller font as footnote
                is_in_footnote_region = y_position > (page_height * 0.80)
                is_small_font = font_size < 10

                if text and (is_in_footnote_region or is_small_font):
                    footnote_parts.append(text)

    return " ".join(footnote_parts) if footnote_parts else ""


def extract_pdf_text(file: FileStorage) -> str:
    """Extract text from PDF file, including footnotes.

    Args:
        file: FileStorage object containing PDF data.

    Returns:
        Extracted and normalized text.

    Raises:
        ValueError: If PDF cannot be opened or processed.
    """
    file.stream.seek(0)
    text_parts = []
    footnote_parts = []

    try:
        with fitz.open(stream=file.stream.read(), filetype="pdf") as doc:
            for page_num in range(len(doc)):
                page = doc[page_num]
                # Extract main text
                main_text = page.get_text("text")
                if main_text.strip():
                    text_parts.append(main_text)
                else:
                    # Fall back to OCR if no text found
                    pix = page.get_pixmap()
                    img = Image.frombytes(
                        mode="RGB",
                        size=(pix.width, pix.height),
                        data=pix.samples,
                    )
                    ocr_text = pytesseract.image_to_string(img)
                    text_parts.append(ocr_text)

                # Extract footnotes
                footnote_text = _extract_pdf_footnotes(page)
                if footnote_text.strip():
                    footnote_parts.append(f"[Page {page_num + 1} footnotes: {footnote_text}]")

                text_parts.append("\n\n\f\n\n")  # Page break marker

    except Exception as exc:
        raise ValueError(f"Failed to extract text from PDF: {exc}") from exc

    # Combine main text and footnotes
    main_content = "\n\n\f\n\n".join(text_parts).strip()

    full_text = main_content

    return _normalize(full_text)

def _extract_docx_with_footnotes(doc: DocxDocument) -> str:
    """Return DOCX body text with footnotes inserted inline at their references.

    Footnote content is inserted at each reference as: "[n: footnote text]".
    Footnote numbering follows the order of first appearance in the document.

    Args:
        path: Filesystem path to a .docx file.

    Returns:
        A single string containing paragraph text with inline footnotes.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file does not have a .docx extension.
    """
    footnotes = _load_footnotes_map(doc)


    lines: List[str] = []

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            t = _para_with_inline_footnotes(block, footnotes)
            if t:
                lines.append(t)
        else:
            for par in _iter_table_paragraphs(block):
                t = _para_with_inline_footnotes(par, footnotes)
                if t:
                    lines.append(t)

    return "\n\n".join(lines)



def extract_docx_text(file: FileStorage) -> str:
    """Extract text from DOCX file, including footnotes inline.

    Args:
        file: FileStorage object containing DOCX data.

    Returns:
        Extracted and normalized text with footnotes inline.

    Raises:
        ValueError: If DOCX cannot be opened or processed.
    """
    file.stream.seek(0)

    try:
        doc = Document(file.stream)
    except Exception as exc:
        raise ValueError(f"Failed to open DOCX file: {exc}") from exc

    full_text = _extract_docx_with_footnotes(doc)
    return _normalize(full_text)


def extract_text(file: FileStorage) -> str:
    """Extract text from uploaded file based on file extension.

    Supports PDF, DOCX, and TXT files. Includes footnote extraction
    for PDF and DOCX formats.

    Args:
        file: FileStorage object containing the uploaded file.

    Returns:
        Extracted and normalized text content.

    Raises:
        ValueError: If file format is unsupported or extraction fails.
    """
    filename = file.filename or ""
    file.stream.seek(0)
    _, ext = os.path.splitext(filename.lower())

    if ext == ".txt":
        try:
            raw_text = file.stream.read().decode("utf-8")
            return _normalize(raw_text)
        except Exception as exc:
            raise ValueError(f"Failed to read TXT file: {exc}") from exc
    elif ext == ".pdf":
        return extract_pdf_text(file)
    elif ext == ".docx":
        return extract_docx_text(file)
    else:
        raise ValueError(
            f"Unsupported file format: {ext}. Supported formats: .pdf, .docx, .txt"
        )

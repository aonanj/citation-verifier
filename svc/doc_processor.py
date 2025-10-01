"""Extract text content from uploaded documents, including footnotes.

This module provides functions to extract text from PDF, DOCX, and TXT files
while preserving footnote citations and their content inline.
"""

import io
import os
import re
from typing import Final

import fitz  # PyMuPDF
import pytesseract
from docx import Document
from docx.document import Document as DocxDocument
from lxml import etree # type: ignore
from PIL import Image
from werkzeug.datastructures import FileStorage

_HYPHEN_WRAP_RE: Final = re.compile(r"(\w)-\n(\w)")
_EXCESS_BREAKS_RE: Final = re.compile(r"\n{3,}")
_SMART_QUOTES_RE: Final = re.compile("[\u201c\u201d]")
_SMART_APOSTROPHES_RE: Final = re.compile("[\u2018\u2019]")

# Namespace constants for Word XML
_W_NAMESPACE: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def normalize(text: str) -> str:
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
    if footnote_parts:
        footnote_section = "\n\n--- FOOTNOTES ---\n\n" + "\n\n".join(footnote_parts)
        full_text = main_content + footnote_section
    else:
        full_text = main_content

    return normalize(full_text)


def _extract_docx_with_footnotes(doc: DocxDocument) -> str:
    """Extract text from DOCX with footnotes inline.

    Args:
        doc: python-docx Document object.

    Returns:
        Text with footnotes inserted inline.
    """
    if not hasattr(doc, "part") or not hasattr(doc.part, "element"):
        # Fallback to simple text extraction
        return "\n\n".join(para.text for para in doc.paragraphs if para.text.strip())

    # Build footnote map
    footnote_map = {}
    try:
        if doc.part.package is not None:
            footnotes_part = doc.part.package.part_related_by(
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
            )
            footnotes_element = etree.fromstring(footnotes_part.blob)

            # Extract all footnotes
            for footnote in footnotes_element.findall(f".//{_W_NAMESPACE}footnote"):
                footnote_id = footnote.get(f"{_W_NAMESPACE}id")
                if footnote_id:
                    texts = []
                    for para in footnote.findall(f".//{_W_NAMESPACE}p"):
                        para_texts = []
                        for text_elem in para.findall(f".//{_W_NAMESPACE}t"):
                            if text_elem.text:
                                para_texts.append(text_elem.text)
                        if para_texts:
                            texts.append("".join(para_texts))
                    if texts:
                        footnote_map[footnote_id] = " ".join(texts)
    except (KeyError, AttributeError):
        pass

    # Extract paragraphs with inline footnotes
    text_parts = []
    body = doc.part.element.find(f".//{_W_NAMESPACE}body")

    if body is None:
        # Fallback
        return "\n\n".join(para.text for para in doc.paragraphs if para.text.strip())

    for para_element in body.findall(f".//{_W_NAMESPACE}p"):
        # Extract text and footnote references together
        para_content = []

        for run in para_element.findall(f".//{_W_NAMESPACE}r"):
            # Get text from this run
            for text_elem in run.findall(f".//{_W_NAMESPACE}t"):
                if text_elem.text:
                    para_content.append(text_elem.text)

            # Check for footnote reference in this run
            footnote_ref = run.find(f".//{_W_NAMESPACE}footnoteReference")
            if footnote_ref is not None:
                footnote_id = footnote_ref.get(f"{_W_NAMESPACE}id")
                if footnote_id and footnote_id in footnote_map:
                    # Insert footnote inline
                    para_content.append(f" [{footnote_id}] {footnote_map[footnote_id]}")

        if para_content:
            text_parts.append("".join(para_content))

    return "\n\n".join(text_parts)


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
    return normalize(full_text)


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
            return normalize(raw_text)
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
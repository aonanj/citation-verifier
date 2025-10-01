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
from docx.document import Document as docxDocument
from lxml import etree # type: ignore
from PIL import Image
from werkzeug.datastructures import FileStorage

_HYPHEN_WRAP_RE: Final = re.compile(r"(\w)-\n(\w)")
_EXCESS_BREAKS_RE: Final = re.compile(r"\n{3,}")
_SMART_QUOTES_RE: Final = re.compile("[\u201c\u201d]")
_SMART_APOSTROPHES_RE: Final = re.compile("[\u2018\u2019]")


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


def _build_footnote_map(doc: docxDocument) -> dict[str, str]:
    """Build a map of footnote IDs to their text content.

    Args:
        doc: python-docx Document object.

    Returns:
        Dictionary mapping footnote IDs to their text content.
    """
    footnote_map = {}

    if not hasattr(doc, "part") or not hasattr(doc.part, "element"):
        return footnote_map

    try:
        if doc.part.package is None:
            return footnote_map

        footnotes_part = doc.part.package.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
        )
        footnotes_element = etree.fromstring(footnotes_part.blob)

        # Extract all footnotes
        for footnote in footnotes_element.findall(
            f".//{{{footnotes_element.nsmap['w']}}}footnote"
        ):
            footnote_id = footnote.get(
                f"{{{footnotes_element.nsmap['w']}}}id"
            )
            if footnote_id:
                # Extract text from all paragraphs in this footnote
                texts = []
                for para in footnote.findall(
                    f".//{{{footnotes_element.nsmap['w']}}}p"
                ):
                    para_texts = []
                    for text_elem in para.findall(
                        f".//{{{footnotes_element.nsmap['w']}}}t"
                    ):
                        if text_elem.text:
                            para_texts.append(text_elem.text)
                    if para_texts:
                        texts.append("".join(para_texts))

                if texts:
                    footnote_map[footnote_id] = " ".join(texts)

    except (KeyError, AttributeError):
        pass

    return footnote_map


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

    # Build footnote map
    footnote_map = _build_footnote_map(doc)

    # Extract paragraphs with inline footnotes
    text_parts = []

    for para in doc.paragraphs:
        para_text = para.text.strip()
        if not para_text:
            continue

        # Check if this paragraph has footnote references
        if hasattr(para, "_element"):
            # Find footnote references in this paragraph
            footnote_refs = para._element.findall(
                ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}footnoteReference"
            )

            if footnote_refs:
                # Build paragraph with inline footnotes
                # Start with the base text
                result = para_text

                # Add footnotes at the end of the paragraph
                footnote_texts = []
                for ref in footnote_refs:
                    footnote_id = ref.get(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id"
                    )
                    if footnote_id and footnote_id in footnote_map:
                        footnote_texts.append(
                            f"[{footnote_id}] {footnote_map[footnote_id]}"
                        )

                if footnote_texts:
                    result += " " + " ".join(footnote_texts)

                text_parts.append(result)
            else:
                text_parts.append(para_text)
        else:
            text_parts.append(para_text)

    full_text = "\n\n".join(text_parts)
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
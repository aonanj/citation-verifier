import re
import io
import os
import fitz
import pytesseract
import docx
from PIL import Image
from werkzeug.datastructures import FileStorage


def ocr_page(page: fitz.Page, zoom: float = 2.0) -> str:
    mat = fitz.Matrix(zoom, zoom)

    pix = page.get_pixmap(matrix=mat, alpha=False)  # type: ignore
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(img)

def blocks_text(tp: fitz.TextPage) -> str:
    blocks = tp.extractBLOCKS() 
    blocks.sort(key=lambda b: (round(b[1], 2), round(b[0], 2)))
    parts = [(b[4] or "").strip() for b in blocks if (b[4] or "").strip()]
    return "\n\n".join(parts)

def normalize(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[‘’]", "'", s)
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)     # de-hyphenate soft wraps
    s = re.sub(r"\n{3,}", "\n\n", s)           # collapse excess breaks
    return s

def extract_pdf_text(file: FileStorage):
    file.stream.seek(0)
    text_parts = []
    with fitz.open(stream=file.stream.read(), filetype="pdf") as doc:
        for page in doc:
            text = page.get_text("text")
            if text.strip():
                text_parts.append(text)
            else:
                pix = page.get_pixmap()
                img = Image.frombytes(mode="RGB", size=(pix.width, pix.height), data=pix.samples)
                ocr_text = pytesseract.image_to_string(img)
                text_parts.append(ocr_text)
            text_parts.append("\n\n\f\n\n")  # Page break
    raw = "\n\n\f\n\n".join(text_parts).strip()
    return normalize(raw)

def extract_docx_text(file: FileStorage):
    file.stream.seek(0)
    doc = docx.Document(file.stream)
    text = [p.text for p in doc.paragraphs]
    raw = "\n".join(text)
    return normalize(raw)

def extract_text(file: FileStorage) -> str:
    filename = file.filename or ""
    file.stream.seek(0)
    _, ext = os.path.splitext(filename.lower())

    if ext == ".txt":
        return normalize(file.stream.read().decode('utf-8'))
    if ext == ".pdf":
        return extract_pdf_text(file)
    elif ext == ".docx":
        return extract_docx_text(file)
    else:
        raise ValueError("Unsupported file format")
    


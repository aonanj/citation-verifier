# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import io
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from werkzeug.datastructures import FileStorage

from svc.citations_compiler import compile_citations
from svc.doc_processor import extract_text
from utils.logger import setup_logger

logger = setup_logger()


class CitationOccurrence(BaseModel):
    citation_category: str | None
    matched_text: str | None
    span: List[int] | None
    pin_cite: str | None


class CitationEntry(BaseModel):
    resource_key: str
    type: str
    status: str
    substatus: str | None = None
    normalized_citation: str | None
    resource: Dict[str, Any]
    occurrences: List[CitationOccurrence]
    verification_details: Dict[str, Any] | None = None


class VerificationResponse(BaseModel):
    citations: List[CitationEntry]
    extracted_text: str | None = None

load_dotenv()

app = FastAPI(title="eyecite-extractor", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://localhost:3000",
        "https://127.0.0.1:3000",
        "https://localhost:5174",
        "https://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_citations(raw: Dict[str, Dict[str, Any]]) -> List[CitationEntry]:
    sanitized: List[CitationEntry] = []
    for resource_key, payload in raw.items():
        occurrences_payload = payload.get("occurrences", [])
        occurrences: List[CitationOccurrence] = []

        for occurrence in occurrences_payload:
            span = occurrence.get("span")
            span_list = list(span) if isinstance(span, tuple) else span
            occurrences.append(
                CitationOccurrence(
                    citation_category=occurrence.get("citation_category"),
                    matched_text=occurrence.get("matched_text"),
                    span=span_list,
                    pin_cite=occurrence.get("pin_cite"),
                )
            )

        sanitized.append(
            CitationEntry(
                resource_key=resource_key,
                type=payload.get("type", "unknown"),
                status=payload.get("status", "unknown"),
                substatus=payload.get("substatus"),
                normalized_citation=payload.get("normalized_citation"),
                resource=payload.get("resource", {}),
                occurrences=occurrences,
                verification_details=payload.get("verification_details"),
            )
        )

    return sanitized


@app.post("/api/verify", response_model=VerificationResponse)
async def verify_document(document: UploadFile = File(..., alias="document")) -> VerificationResponse:
    file = document

    if not file.filename:
        logger.error("Uploaded file is missing a filename.")
        raise HTTPException(status_code=400, detail="Uploaded file is missing a filename.")

    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in {".pdf", ".docx", ".txt"}:
        logger.error(f"Unsupported file format: {extension}")
        raise HTTPException(status_code=400, detail=f"Unsupported file format: {extension}")

    file_contents = await file.read()
    if not file_contents:
        logger.error("Uploaded file is empty.")
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    storage = FileStorage(
        stream=io.BytesIO(file_contents),
        filename=file.filename,
        content_type=file.content_type,
    )

    try:
        extracted_text = extract_text(storage)
    except ValueError as exc:
        logger.error(f"Error in extract_text: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error(f"Error in extract_text: {exc}")
        raise HTTPException(status_code=500, detail="Failed to extract text.") from exc

    try:
        compiled = await compile_citations(extracted_text)
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error(f"Error in compile_citations: {exc}")
        raise HTTPException(status_code=500, detail="Failed to compile citations.") from exc

    sanitized = _sanitize_citations(compiled)

    return VerificationResponse(citations=sanitized, extracted_text=extracted_text)

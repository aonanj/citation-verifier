# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

from typing import Tuple

from eyecite.models import CitationBase

from .logger import get_logger

logger = get_logger()

class Reporter:
    pass
class Edition:
    pass

def get_span(obj: CitationBase) -> Tuple[int, int] | None:
    """Get the span (start, end) of the citation in the source text."""

    try:
        span = obj.span()
        if span is not None and isinstance(span, tuple) and len(span) == 2 and span[0] is not None and span[1] is not None and span[0] > 0 and span[1] > span[0]:
            return span
    except Exception as e:
        logger.error(f"Error getting span: {e}")
        span = None

    document = obj.document if hasattr(obj, "document") else None
    citation_tokens = document.citation_tokens if document and hasattr(document, "citation_tokens") else None
    idx = obj.index if hasattr(obj, "index") else None

    if citation_tokens is None or idx is None:
        return None

    citation_dict = {key: token for key, token in citation_tokens}

    target_token = citation_dict[idx] if idx in citation_dict else None
    return (target_token.start, target_token.end) if target_token else None

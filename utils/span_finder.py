# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

from typing import Tuple


from .logger import get_logger

logger = get_logger()

class Reporter: 
    pass
class Edition: 
    pass

def get_span(obj) -> Tuple[int, int] | None:
    """Get the span (start, end) of the citation in the source text."""
    
    document = obj.document if hasattr(obj, "document") else None
    citation_tokens = document.citation_tokens if document and hasattr(document, "citation_tokens") else None
    idx = obj.index if hasattr(obj, "index") else None

    if citation_tokens is None or idx is None:
        return None
    
    citation_dict = {key: token for key, token in citation_tokens}

    target_token = citation_dict[idx] if idx in citation_dict else None
    return (target_token.start, target_token.end) if target_token else None
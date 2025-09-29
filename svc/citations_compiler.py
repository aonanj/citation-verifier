from __future__ import annotations
from typing import List, Optional, Any, Dict, Union, Tuple, cast
import re

from eyecite import get_citations, resolve_citations
from eyecite.resolve import Resolutions
from eyecite.models import FullJournalCitation, FullCaseCitation, FullLawCitation, CaseCitation
from eyecite.models

def _normalized_key(citation_obj) -> str:
    """Generate a normalized key for a citation object."""
    
    if isinstance(citation_obj, FullCaseCitation):
        volume = citation_obj.volume or ""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Tuple

import httpx
from eyecite.models import FullCitation

logger = logging.getLogger(__name__)


_space_re = re.compile(r"\s+")
_citation_marker_re = re.compile(r"\[\d+:")
_non_alphanum_re = re.compile(r"[^a-z0-9]+", re.IGNORECASE)

GOVINFO_BASE_URL = "https://www.govinfo.gov/link/"
GOVINFO_API_KEY = os.getenv("GOVINFO_API_KEY", "")

GOVINFO_REPORTER_MAP = {
    "U.S.C.": "uscode", # /uscode/{title}/{section}
    "C.F.R.": "cfr", # /cfr/{title}/{part}?sectionnum={section}
    "Stat.": "statute", # /statute/{volume}/{page}
    "Weekly Comp. Pres. Doc.": "cpd", # /cpd/{doctype}/{docnum}
    "Daily Comp. Pres. Doc.": "cpd", # /cpd/{doctype}/{docnum}
    "S.": "bills", # /bills/{congress}/{billtype}/{bill_number}
    "H.R.": "bills", # /bills/{congress}/{billtype}/{bill_number}
    "Pub. L.": "plaw", # /plaw/{congress}/{lawtype}/{lawnum} or /plaw/{statutecitation} or /plaw/{congress}/{associatedbillnum}
    "Fed. Reg.": "fr", # /fr/{volume}/{page}

}
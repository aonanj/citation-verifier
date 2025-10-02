import json
import os
from typing import Any, Dict, Tuple

from eyecite.models import FullCitation, FullLawCitation
from openai import OpenAI
from openai.types import ResponsesModel

from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

OPENAI_API_KEY = "OPENAI_API_KEY"

PROMPT = """
You are an expert in legal research, specifically in the context of state laws, codes, and regulations. You are tasked with
verifying the veracity of citations in legal documents. In this context, you should take the utmost care to ensure that the
citations you are provided currently exist and are in effect. Accordingly, you should make no assumptions. You should use
only the information explicitly provided to you when generating a response. Because accuracy is the paramount concern,
responses indicating you don't have sufficient information to provide an answer or you are unable to locate a source
corresponding to the a citation are absolutely acceptable. Moreover, you should provide a confidence score between 0.0 and
1.0 indicating how confident you are your response. You should provide your response as a JSON object,
according to this format:
    {
        "status": "verified" if citation is verified (e.g., confidence score >= 0.85), "warning" if confidence is low (e.g., 0.5 <= confidence < 0.85),
            "no_match" if no matching citation is found (e.g., confidence score < 0.5), or "error" if an error occurred,
        "citation": the standardized Bluebook citation string closest to the provided citation (if "verified" this may be the same as the provided
            citation; if "no_match" or "error" this should be null),
        "confidence": confidence score as a float between 0.0 and 1.0, indicating how confident you are that the citation is valid
    }
Do not return any text or other characters apart from the JSON object. Do not include any text or other characters outside of the JSON object. Note
that different states arrange their laws in different formats; therefore, there is no single correct format for the "section" field or the "reporter"
field. Your primary goal is to verify whether the citation you are provided corresponds to an actual, in-effect state law citation.\n\n
"""

ALLOWED_DOMAINS = [
    "law.justia.com",
    "law.cornell.edu",
    "codes.findlaw.com"
]

def _get_law_group(
    cite: FullCitation | None,
    resource_dict: Dict[str, Any] | None,
    key: str,
) -> str | None:
    if isinstance(cite, FullCitation):
        groups = getattr(cite, "groups", {}) or {}
        if key in groups:
            value = (groups.get(key))
            if value:
                return value

    if isinstance(cite, FullCitation):
        direct_value = clean_str(getattr(cite, key, None))
        if direct_value is not None:
            return direct_value

    resource_dict = resource_dict or {}
    id_tuple = resource_dict.get("id_tuple")
    if isinstance(id_tuple, tuple):
        mapping = {
            "code": 0,
            "reporter": 0,
            "section": 1,
            "page": 1,
            "year": 2,
        }
        idx = mapping.get(key)
        if idx is not None and len(id_tuple) > idx:
            value = clean_str(id_tuple[idx])
            if value:
                return value

    return None

def _get_openai_client() -> OpenAI | None:
    if OPENAI_API_KEY is None or OPENAI_API_KEY == "":
        logger.error("OPENAI_API_KEY is not set.")
        return None
    try:
        open_api_key = os.getenv(OPENAI_API_KEY, "")
        client = OpenAI(api_key=open_api_key)
        return client
    except Exception as e:
        logger.error(f"Error initializing OpenAI client: {e}")
        return None

def _clean_json_response(response_text: str) -> str:
    start_idx = response_text.find("{")
    end_idx = response_text.rfind("}") + 1
    if start_idx != -1 and end_idx != -1:
        return response_text[start_idx:end_idx]
    return response_text

def verify_state_law_citation(
    primary_full: FullCitation | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:

    if not isinstance(primary_full, FullLawCitation):
        logger.error("Primary full citation is not a FullLawCitation.")
        return "error", "unsupported_citation_type", None

    reporter = _get_law_group(primary_full, resource_dict, "reporter")
    if not reporter:
        return (
            "error",
            "missing_reporter",
            None
        )
    reporter = clean_str(reporter)

    section = _get_law_group(primary_full, resource_dict, "section")
    if not section:
        return (
            "error",
            "missing_section",
            None
        )
    section = clean_str(section)

    year = _get_law_group(primary_full, resource_dict, "year")
    year = clean_str(year)

    bluebook_citation = f"{reporter} § {section}"
    if year:
        bluebook_citation += f" ({year})"

    try:
        client = _get_openai_client()
        if client is None:
            return "error", "openai_client_init_failed", None

        input = PROMPT + f"Citation to verify: {bluebook_citation}"
        model: ResponsesModel = "gpt-5"

        response = client.responses.create(
            model = model,
            input = input,
            tools = [{
                "type": "web_search",
                "filters": { "allowed_domains": ALLOWED_DOMAINS }
            }],
            tool_choice = "auto",
            text = { "verbosity": "low" },
            reasoning = { "effort": "low"}
        )

        output_message = None
        candidate = None
        for item in response.output:
            if item.type == "message":
                output_message = item
                break
        if output_message is not None and output_message.content is not None:
            for content_item in output_message.content:
                if content_item.type == "output_text":
                    candidate = content_item.text
                    break

        logger.info(f"OpenAI response for state law citation verification: {candidate}")
        expected_keys = ["status", "citation", "confidence"]
        data = {}
        if isinstance(candidate, dict):
            data = candidate
        elif isinstance(candidate, str):
            try:
                data = json.loads(_clean_json_response(candidate))
            except Exception as e:
                logger.error(f"Error parsing JSON response: {e}")
                return "error", "state_law_search_failed", None

        logger.info(f"Parsed OpenAI response data: {data}")
        manifest = {k: (data.get(k) if isinstance(data, dict) else None) for k in expected_keys}
        for k, v in manifest.items():
            if isinstance(v, str) and v.strip().lower() in {"null", "none", ""}:
                manifest[k] = None

        logger.info(f"Manifest for state law citation verification: {manifest}")

        status = manifest.get("status") or "error"
        citation = manifest.get("citation") or None
        confidence = manifest.get("confidence") or None
        return status, f"closest_match: {citation}, confidence: {confidence}", None

    except Exception as e:
        logger.error(f"Error during state law citation verification: {e}")
        return "error", "state_law_search_failed", None

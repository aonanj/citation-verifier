import json
import math
from typing import Any, Dict, Tuple

from svc.citation_record import CitationRecord
from utils.ai_model import ai_model
from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

PROMPT = """
Below is at least one citation to a U.S. state law, statute, regulation, or similar state-level legal provision. 
You must verify that existence and accuracy of the citation. To do so, you may need to resolve the state or state abbreviation, 
reporter or other codification source (including title and section), year, and other relevant details. 
You should verify each citation part is accurate and corresponds to an actual, in-effect state law citation. 
Note that different states arrange their laws in different formats; therefore, there is no single correct format for the "section" field or the "reporter"
field. 
Your primary goal is to verify whether the citation you are provided corresponds to an actual, in-effect state law citation.
You should use only the information explicitly provided to you when generating a response. 
Because accuracy is the paramount concern, responses indicating you don't have sufficient information to provide an answer or you are unable to locate a source
corresponding to a citation are acceptable. Moreover, you should provide a confidence score between 0.0 and
1.0 indicating confidence for a citation verification. 
If you are unable to verify all parts of a citation, you should adjust your confidence score downward to reflect this uncertainty. 
Provide your response as a JSON object, according to this format:
    {
        "status": "verified" if citation is verified (confidence score >= 0.90), "warning" if confidence is low (0.70 <= confidence < 0.90),
            "no_match" if no matching citation is found (confidence score < 0.70), or "error" if an error occurred,
        "citation": the standardized Bluebook citation string closest to the provided citation (if "verified" this may be the same as the provided
            citation; if "no_match" or "error" this should be null),
        "confidence": confidence score as a float between 0.0 and 1.0, indicating how confident you are that the citation is valid
    }
Do not return any text or other characters apart from the JSON object. Do not include any text or other characters outside of the JSON object.\n\n
"""

ALLOWED_DOMAINS = [
    "law.justia.com",
    "law.cornell.edu",
    "codes.findlaw.com"
]

# Confidence bands for an AI verification (state law and the federal-law recheck).
AI_VERIFIED_MIN = 0.90
AI_WARNING_MIN = 0.70
_STATUS_RANK = {"no_match": 0, "warning": 1, "verified": 2}


def confidence_band_status(model_status: Any, confidence: Any) -> str | None:
    """The status an AI answer earns: the more conservative of the model's own status
    and the band its confidence falls in (>= 0.90 verified, >= 0.70 warning, else
    no_match), so the model can never upgrade a citation past its confidence.

    None when the status isn't verified/warning/no_match or the confidence isn't a number.
    """
    if not isinstance(model_status, str) or isinstance(confidence, bool):
        return None
    status = model_status.strip().lower().replace(" ", "_")
    if status not in _STATUS_RANK:
        return None
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    value = min(max(value, 0.0), 1.0)
    band = "verified" if value >= AI_VERIFIED_MIN else "warning" if value >= AI_WARNING_MIN else "no_match"
    return min(status, band, key=_STATUS_RANK.__getitem__)

def _get_law_group(
    cite: CitationRecord | None,
    resource_dict: Dict[str, Any] | None,
    key: str,
) -> str | None:
    if cite is not None:
        value = clean_str(cite.get(key))
        if value:
            return value

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

def _get_ai_client(model: dict) -> Any:
    """An OpenAI client; only called for an OpenAI AI_MODEL."""
    if model.get("provider") == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=model.get("ai_api_key"), organization=model.get("organization"), project=model.get("project_id"))
        return client
    else:
        logger.error("Error initializing OpenAI client")
        raise Exception(f"Unsupported AI model provider: {model.get('provider')}")

def _clean_json_response(response_text: str) -> str:
    start_idx = response_text.find("{")
    end_idx = response_text.rfind("}") + 1
    if start_idx != -1 and end_idx != -1:
        return response_text[start_idx:end_idx]
    return response_text

def verify_state_law_citation(
    primary_full: CitationRecord | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:

    if primary_full is None or primary_full.type != "law":
        logger.error("Primary full citation is not a law citation.")
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

    model = ai_model()
    if not model:
        logger.error("AI_MODEL is not set.")
        return "error", "ai_model_not_configured", None
    if model.get("provider") == "openai":
        return _verify_with_openai(model, bluebook_citation)
    logger.error(f"{model.get('model')} is not supported: only OpenAI (\"gpt...\") models are implemented.")
    return "error", "unsupported_ai_model", None


def _verify_with_openai(model: dict, bluebook_citation: str) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Ask an OpenAI model (with web search) whether the citation exists."""
    try:
        client = _get_ai_client(model)
        if client is None:
            return "error", "openai_client_init_failed", None

        input = PROMPT + f"**Citation to verify**: `{bluebook_citation}`"

        response = client.responses.create(
            model = model.get("model"),
            input = input,
            tools = [{
                "type": "web_search",
                "filters": { "allowed_domains": ALLOWED_DOMAINS }
            }],
            tool_choice = "auto",
            text = { "verbosity": "low" },
            reasoning = { "effort": "medium" }
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

        raw_status = manifest.get("status")
        citation = manifest.get("citation") or None
        confidence = manifest.get("confidence")
        substatus = f"closest_match: {citation}, confidence: {confidence}"
        if isinstance(raw_status, str) and raw_status.strip().lower() == "error":
            return "error", substatus, None
        status = confidence_band_status(raw_status, confidence)
        if status is None:
            logger.error(f"Unusable state law verification answer: status={raw_status!r}, confidence={confidence!r}")
            return "error", "state_law_search_failed", None
        return status, substatus, None

    except Exception as e:
        logger.error(f"Error during state law citation verification: {e}")
        return "error", "state_law_search_failed", None

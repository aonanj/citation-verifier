# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""The configured AI model and API key, provider-neutral.

AI_MODEL names the model (e.g. "gpt-5.6-terra"); AI_API_KEY is its provider's
key. Only OpenAI is implemented: callers import and call the OpenAI SDK only
when model_params(ai_model()).get("provider") == "openai", so another provider's model can be
configured without importing or calling OpenAI. Read at call time - .env is
loaded after project modules are imported (see CLAUDE.md, env import-order trap).
"""

from __future__ import annotations

import os
from utils.logger import get_logger

logger = get_logger()

AI_MODEL_ENV = "AI_MODEL"
AI_API_KEY_ENV = "AI_API_KEY"


def _openai_model_params(model: str) -> dict:
    """Return OpenAI model parameters for the given model name."""
    return {
        "provider": "openai",
        "model": model,
        "organization": os.getenv("OPENAI_ORG", "").strip(),
        "project_id": os.getenv("OPENAI_PROJECT", "").strip(),
        "skill_id": os.getenv("OPENAI_SKILL_ID", "").strip(),
    }

def ai_model() -> dict:
    """AI_MODEL, or "" when unset."""
    model  = {
        "ai_api_key": (os.getenv(AI_API_KEY_ENV) or "").strip(),
        "model": (os.getenv(AI_MODEL_ENV) or "").strip(),
    }
    if model["model"].startswith("gpt"):
        model.update(_openai_model_params(model["model"]))
        logger.info(f"Using OpenAI model: {model}")
    else:
        logger.error(f"Unsupported model: {model}")
        raise ValueError(f"Unsupported model: {model}")

    return model

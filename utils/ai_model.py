# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""The configured AI model and API key, provider-neutral.

AI_MODEL names the model (e.g. "gpt-5.6-terra"); AI_API_KEY is its provider's
key. Only OpenAI is implemented: callers import and call the OpenAI SDK only
when is_openai_model(ai_model()) is true, so another provider's model can be
configured without importing or calling OpenAI. Read at call time - .env is
loaded after project modules are imported (see CLAUDE.md, env import-order trap).
"""

from __future__ import annotations

import os

AI_MODEL_ENV = "AI_MODEL"
AI_API_KEY_ENV = "AI_API_KEY"


def ai_model() -> str:
    """AI_MODEL, or "" when unset."""
    return (os.getenv(AI_MODEL_ENV) or "").strip()


def ai_api_key() -> str:
    """AI_API_KEY, or "" when unset."""
    return (os.getenv(AI_API_KEY_ENV) or "").strip()


def is_openai_model(model: str) -> bool:
    """Whether `model` is served by the OpenAI API (its name starts with "gpt")."""
    return model.startswith("gpt")

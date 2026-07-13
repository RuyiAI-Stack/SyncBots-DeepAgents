"""Robust JSON extraction for LLM outputs.

Some OpenAI/Anthropic-compatible proxies do not support native structured
output (``response_format``). Instead of relying on the framework to coerce a
schema, we ask the model for JSON in the prompt and parse it defensively from
the reply text here. This keeps SyncBots working against minimal proxies.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _candidate_json_blobs(text: str):
    """Yield progressively more permissive JSON candidate substrings."""
    text = (text or "").strip()
    if not text:
        return
    # 1. The whole thing.
    yield text
    # 2. Fenced ```json ... ``` blocks.
    for m in _FENCE_RE.finditer(text):
        yield m.group(1).strip()
    # 3. First '{' ... last '}' (object) and '[' ... ']' (array).
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            yield text[start : end + 1]


def parse_json(text: str) -> Optional[Any]:
    """Best-effort parse of a JSON object/array from arbitrary LLM text."""
    for blob in _candidate_json_blobs(text):
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_model(text: str, model_cls: Type[T]) -> Optional[T]:
    """Parse *text* into a pydantic *model_cls* instance, or ``None`` on failure."""
    data = parse_json(text)
    if data is None:
        return None
    if not isinstance(data, dict):
        # Allow a bare list to map onto a single list-typed field if unambiguous.
        return None
    try:
        return model_cls(**data)
    except (ValidationError, TypeError) as e:
        logger.debug("parse_model(%s) validation failed: %s", model_cls.__name__, e)
        try:
            return model_cls.model_validate(data)
        except (ValidationError, TypeError):
            return None

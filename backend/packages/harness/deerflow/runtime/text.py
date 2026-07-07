"""Text helpers for user-visible message content."""

from __future__ import annotations

import re

_THINK_TAG_RE = re.compile(r"<think>\s*[\s\S]*?\s*</think>[ \t]*(?:\r?\n){0,2}", re.IGNORECASE)
_OPEN_THINK_TAG_RE = re.compile(r"<think>\s*([\s\S]*)$", re.IGNORECASE)


def strip_inline_thinking(text: str) -> str:
    """Remove inline ``<think>`` reasoning blocks from visible text."""
    cleaned = _THINK_TAG_RE.sub("", text)
    open_match = _OPEN_THINK_TAG_RE.search(cleaned)
    if open_match:
        cleaned = cleaned[: open_match.start()]
    return cleaned

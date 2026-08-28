"""Relevance filtering for discovered signals.

Deterministic keyword heuristic, no LLM. A signal is relevant if it
carries at least one commercial-intent term and has some substance.
Used only when discovery is run with the opt-in filter enabled.
"""

from __future__ import annotations

import re

# revenue / commercial intent terms, matched at word start (so "sell"
# also matches "sells", "selling", "seller").
COMMERCIAL_KEYWORDS: tuple[str, ...] = (
    "sell",
    "buy",
    "price",
    "pricing",
    "paid",
    "pay",
    "revenue",
    "profit",
    "monetize",
    "monetise",
    "customer",
    "subscription",
    "subscribe",
    "saas",
    "marketplace",
    "ecommerce",
    "e-commerce",
    "service",
    "freelance",
    "consulting",
    "client",
    "b2b",
    "b2c",
    "startup",
    "launch",
    "product",
    "tool",
    "platform",
    "hiring",
    "hire",
    "invoice",
    "checkout",
    "stripe",
    "waitlist",
    "mrr",
    "arr",
)

_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in COMMERCIAL_KEYWORDS) + r")",
    re.IGNORECASE,
)


def is_relevant(signal, *, min_length: int = 20) -> bool:
    text = f"{signal.title} {signal.text}".strip()
    if len(text) < min_length:
        return False
    return _PATTERN.search(text) is not None

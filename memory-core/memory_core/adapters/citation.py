"""Build a trusted injection snapshot and check a draft before delivery."""
from __future__ import annotations

from copy import deepcopy
import json

from ..citation import CHECK_UNAVAILABLE, check_reply, rejected

__all__ = ("check_reply", "citation_snapshot", "guard")


def citation_snapshot(result, injected_text):
    """Freeze the evidence that actually appeared in this turn's prompt."""
    allowed = set(result["allowed_raw_ids"])
    sources = []
    for item in result["items"]:
        source = item.get("value", item)
        quote = source.get("quote")
        if (source.get("raw_id") in allowed and isinstance(quote, str) and quote and
                (quote in injected_text or json.dumps(quote, ensure_ascii=False)[1:-1] in injected_text)):
            sources.append(deepcopy(source))
    return {"allowed_raw_ids": list(dict.fromkeys(s["raw_id"] for s in sources)), "sources": sources}


def guard(reply, snapshot, *, client=None):
    """Ask the configured backend. A failed request never permits a send."""
    from .http.client import HTTPMemoryClient
    try:
        backend = client if client is not None else HTTPMemoryClient.from_env()
        result = backend.call("citation/check", {"reply": reply, "snapshot": snapshot})
        allowed = result.get("allowed")
        violations = result.get("violations")
        if (type(allowed) is bool and isinstance(result.get("checks"), list) and isinstance(violations, list)
                and isinstance(result.get("matches"), list) and "stripped" in result
                and (result["stripped"] is None or isinstance(result["stripped"], str))
                and allowed == (not violations)
                and (not allowed or (result["stripped"] is None and
                                     all(check.get("allowed") is True for check in result["checks"])))
                and all(isinstance(item, dict) and isinstance(item.get("reason"), str)
                        and isinstance(item.get("quote"), str) for item in violations)):
            return result
    except (TypeError, ValueError, OSError, AttributeError):
        pass
    return rejected(CHECK_UNAVAILABLE)

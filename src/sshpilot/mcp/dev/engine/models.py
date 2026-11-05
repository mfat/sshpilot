"""Result conventions shared by the development-intelligence engine.

Everything here is plain data: confidence labels, the versioned result
envelope, and the deterministic output-budget fitter that keeps replies
compact. No MCP SDK dependency.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Version of the JSON result contract returned by the V2 tools. Bump when a
#: field changes meaning or disappears (adding fields does not bump it).
RESULT_SCHEMA = "sshpilot-dev-intel/1"

# --- Confidence labels ----------------------------------------------------
#: A syntactic fact resolved to an indexed definition (name bound by import or
#: module-level definition, literal registration table, ...).
VERIFIED_STATIC = "verified_static"
#: Plausible but not proven: ``self.x()`` dispatch, callbacks passed by
#: reference, re-export guesses, test-imports-module associations.
HEURISTIC = "heuristic"
#: The target could not be resolved: attribute calls on unknown objects,
#: ``getattr`` with a computed name, builtins/external libraries.
UNRESOLVED = "unresolved"

CONFIDENCE_RANK = {VERIFIED_STATIC: 0, HEURISTIC: 1, UNRESOLVED: 2}

# --- Output budget ----------------------------------------------------------
#: Approximate bytes per token used for estimates (no tokenizer dependency).
BYTES_PER_TOKEN = 4
#: Default reply budget: ~1,200 estimated tokens.
DEFAULT_BUDGET_BYTES = 4800
MIN_BUDGET_BYTES = 800
MAX_BUDGET_BYTES = 64 * 1024


class EngineError(ValueError):
    """Raised for invalid engine input (bad reference, base, path, budget)."""


def estimate_tokens(n_bytes: int) -> int:
    """Approximate token count for *n_bytes* of JSON output."""
    return int(math.ceil(n_bytes / BYTES_PER_TOKEN))


def normalize_budget(budget: Optional[int]) -> int:
    """Clamp a caller budget (bytes) into the supported range."""
    if budget is None:
        return DEFAULT_BUDGET_BYTES
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise EngineError("budget must be an integer number of bytes")
    return max(MIN_BUDGET_BYTES, min(MAX_BUDGET_BYTES, budget))


def _size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, default=str).encode("utf-8"))


def fit_to_budget(
    result: Dict[str, Any],
    budget: int,
    trim_order: Sequence[str],
    next_context_ref: Optional[str] = None,
    min_keep: int = 3,
) -> Dict[str, Any]:
    """Trim ranked lists in *result* until its JSON fits *budget* bytes.

    *trim_order* names list-valued keys (dotted for nested dicts) from the
    least to the most important; items are removed from the tail (the lowest
    ranked) of the first non-empty list in that order, one at a time, so the
    outcome is deterministic. A ``budget`` block is always attached and says
    exactly what was omitted; the result is never silently cut.
    """
    omitted: Dict[str, int] = {}

    def _list(key: str) -> Optional[list]:
        node: Any = result
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, list) else None

    def _envelope(over: bool) -> Dict[str, Any]:
        # used_bytes/estimated_tokens are sized at their maximum width while
        # fitting, so filling in the real values can never push us over.
        return {
            "limit_bytes": budget,
            "truncated": bool(omitted),
            "omitted": dict(sorted(omitted.items())),
            "more_available": bool(omitted),
            "next_context_ref": next_context_ref if omitted else None,
            "over_budget": over,
            "used_bytes": budget,
            "estimated_tokens": estimate_tokens(budget),
        }

    result["budget"] = _envelope(False)
    while _size(result) > budget:
        # First pass keeps the top ``min_keep`` items of every list, so the
        # strongest evidence of each kind survives; only then are lists
        # emptied in order.
        victim = next((key for key in trim_order if len(_list(key) or []) > min_keep), None)
        if victim is None:
            victim = next((key for key in trim_order if _list(key)), None)
        if victim is not None:
            _list(victim).pop()
            omitted[victim] = omitted.get(victim, 0) + 1
        else:
            # Nothing left to trim: report it rather than pretend it fits.
            result["budget"] = _envelope(True)
            break
        result["budget"] = _envelope(False)
    used = _size(result)
    result["budget"]["used_bytes"] = used
    result["budget"]["estimated_tokens"] = estimate_tokens(used)
    return result


def envelope(tool: str, freshness: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    """Common top-level shape for every V2 tool result."""
    return {"schema": RESULT_SCHEMA, "tool": tool, **fields, "freshness": freshness}


def ordered(items: Iterable[dict], *keys: str) -> List[dict]:
    """Deterministically sort dicts by *keys* (missing keys sort first)."""
    return sorted(items, key=lambda item: tuple(_sort_key(item.get(key)) for key in keys))


def _sort_key(value: Any) -> tuple:
    if value is None:
        return (0, "")
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, str(value))

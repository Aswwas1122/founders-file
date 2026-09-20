"""
Robust JSON extraction + numeric coercion for LLM replies.

Two failure classes this exists to kill:

1. The old fence-stripper (`text.split("```")[1]`) only handled bare JSON and
   a clean ```json fence. It failed on a preamble ("Here is the JSON:\n{...}")
   and on replies truncated mid-fence. extract_json() finds the outermost
   {...} instead, which handles all of those.

2. Nothing coerced types. A model returning {"demandScore": "85"} made
   `max(0, min(100, "85"))` raise TypeError and took /api/analyze down with a
   500. Same for {"revenueCeiling": "5,000"}, which crashed _build_reasoning's
   `:,.0f` format spec and 500'd /estimate. as_number() handles "85", "$5,000",
   "20%", "5000-8000" (takes the first number), None, and garbage.
"""

import json
import re


def extract_json(text: str) -> dict:
    """Pulls the first complete JSON object out of a model reply."""
    text = (text or "").strip()

    # Strip a fenced block if present, tolerating a missing closing fence.
    fence = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", text, re.DOTALL)
    if fence and text.startswith("```"):
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to brace matching, which survives preambles and trailing prose.
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in model reply: {text[:200]!r}")

    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])

    raise ValueError(
        f"JSON object was never closed — reply is likely truncated "
        f"({len(text)} chars): {text[:200]!r}"
    )


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def as_number(value, default=0):
    """Coerces an LLM value into a float. Returns `default` if impossible."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUM_RE.search(value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return default
    return default


def as_int(value, default=0):
    return int(round(as_number(value, default)))


def clamp_score(value, default=50):
    """A 0-100 sub-score, coerced and clamped. Never raises."""
    return int(round(max(0.0, min(100.0, as_number(value, default)))))
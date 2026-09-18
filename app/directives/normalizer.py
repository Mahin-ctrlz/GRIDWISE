"""
normalizer.py - Turn free-text spans into structured values.

Rules (canonical §3):
  * Time windows are START-INCLUSIVE, END-EXCLUSIVE.
      "1 PM to 3 PM"     -> [13, 14]
      "6 PM until 9 PM"  -> [18, 19, 20]   (last hour is the end hour, exclusive)
  * Percentages:
      "80% reduction"             -> factor = 0.2   (reduction factor = remaining usable)
      "usable solar should be 25%" -> factor = 0.25
      "50% of capacity"            -> 0.5 * capacity_kwh (resolved downstream by caller)
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple

# ----------------------- helpers -------------------------

_WORD_HOURS = {
    "midnight": 0, "noon": 12,
}
def _to_24h(token: str) -> Optional[int]:
    s = token.strip().lower().replace(".", "")
    if s in _WORD_HOURS:
        return _WORD_HOURS[s]
    m = re.match(r"^(\d{1,2})\s*(am|pm)$", s)
    if m:
        h = int(m.group(1)) % 12
        if m.group(2) == "pm":
            h += 12
        return h % 24
    if s.isdigit():
        h = int(s)
        if 0 <= h <= 23:
            return h
    return None


_HOUR_PATTERN = r"(?:\d{1,2}\s*(?:am|pm)|\d{1,2}|noon|midnight)"

_RANGE_RE = re.compile(
    rf"({_HOUR_PATTERN})\s*(?:to|until|till|-|–|—)\s*({_HOUR_PATTERN})",
    re.IGNORECASE,
)


def _normalize_window(start: int, end: int) -> List[int]:
    """
    Start-inclusive, end-exclusive. If end <= start, wrap to next day.
    Always returns a sorted list of unique hours in [0, 23].
    """
    if start == end:
        # zero-length window -> empty
        return []
    hours = []
    h = start
    while h != end:
        hours.append(h % 24)
        h = (h + 1) % 24
        if len(hours) > 48:        # safety, can't exceed 2 full cycles
            break
    return sorted(set(h for h in hours if 0 <= h <= 23))


def parse_hour_window(text: str) -> List[int]:
    """Find a time window in `text` and return [start..end-1] in 24h."""
    if not text:
        return []
    m = _RANGE_RE.search(text)
    if not m:
        return []
    s = _to_24h(m.group(1))
    e = _to_24h(m.group(2))
    if s is None or e is None:
        return []
    return _normalize_window(s, e)


def parse_factor(text: str, default: Optional[float] = None) -> Optional[float]:
    """
    Returns the USABLE fraction in [0, 1].

    Handles:
      "reduce solar by 80%"      -> 0.2
      "usable solar should be 25%" -> 0.25
      "solar at 50%"             -> 0.5
      "cut solar in half"        -> 0.5
    """
    if text is None:
        return default
    t = text.lower()

    if re.search(r"\b(in\s*half|halve|50\s*%)\b", t):
        return 0.5

    m = re.search(r"reduce.*?(\d{1,3})\s*%", t)
    if m:
        pct = max(0.0, min(100.0, float(m.group(1))))
        return max(0.0, min(1.0, (100.0 - pct) / 100.0))

    m = re.search(r"(?:usable|use|allow|cap(?: at)?|set(?: at)?|to)\s*(\d{1,3})\s*%", t)
    if m:
        pct = max(0.0, min(100.0, float(m.group(1))))
        return max(0.0, min(1.0, pct / 100.0))

    m = re.search(r"(\d{1,3})\s*%", t)
    if m:
        pct = max(0.0, min(100.0, float(m.group(1))))
        # Ambiguous: treat as "usable X%"
        return max(0.0, min(1.0, pct / 100.0))

    return default


def parse_percentage_of_capacity(text: str, capacity_kwh: float) -> Optional[float]:
    """
    Returns an absolute kWh value for "X% of capacity" style phrases.
    Returns None if no percentage phrase is found.
    """
    if not text:
        return None
    m = re.search(
        r"(\d{1,3})\s*%\s*(?:of\s*(?:the\s*)?(?:battery\s*)?capacity|of\s*capacity)",
        text.lower(),
    )
    if not m:
        return None
    pct = max(0.0, min(100.0, float(m.group(1))))
    return (pct / 100.0) * capacity_kwh


def parse_max_grid_kwh(text: str) -> Optional[float]:
    """Pull a numeric cap like 'cap grid at 5 kWh' or 'max grid 3.5'."""
    if not text:
        return None
    m = re.search(r"(?:cap(?: at)?|max(?:imum)?|limit(?: to)?)\s*(?:grid)?\s*(?:at|to)?\s*(\d+(?:\.\d+)?)",
                  text.lower())
    if m:
        return float(m.group(1))
    return None

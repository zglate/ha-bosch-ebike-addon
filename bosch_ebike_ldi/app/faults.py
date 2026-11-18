"""
Messages from the bike: Bosch fault codes, decoded with Bosch's own table
(faults.json, built by scripts/build_faults.py).

The bike reports its current issues as nested messages holding a fault code
(a real bike: allIssueVisualizationEvents -> {1: 5660676}, code 566004, the
"don't let yourself be distracted" reminder). Rather than rely on the exact
nesting, any number in the reading that is a known fault code counts.
"""
from __future__ import annotations

import json
from pathlib import Path

_TABLE: dict[str, dict] | None = None
SEVERITY = {"CRITICAL_ERROR": "Critical", "ERROR": "Error", "WARNING": "Warning", "INFO": "Info"}


def table() -> dict[str, dict]:
    global _TABLE
    if _TABLE is None:
        try:
            _TABLE = json.loads(Path(__file__).with_name("faults.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _TABLE = {}
    return _TABLE


def _numbers(value):
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _numbers(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _numbers(v)


def find(value) -> list[dict]:
    """Known fault codes anywhere in a decoded value, in order, without repeats."""
    seen, out = set(), []
    for n in _numbers(value):
        entry = table().get(str(n))
        if entry and n not in seen:
            seen.add(n)
            out.append({"code": entry.get("hex", str(n)),
                        "severity": SEVERITY.get(entry.get("severity", ""), entry.get("severity", "")),
                        "component": entry.get("component_name", ""),
                        "title": entry.get("title", ""),
                        "description": entry.get("description", ""),
                        "measures": entry.get("measures", "")})
    return out


def summary(value) -> str:
    """Short text for a Home Assistant sensor."""
    found = find(value)
    if not found:
        return "None"
    return "; ".join(f"{f['component']}: {f['title']}" for f in found)[:255]

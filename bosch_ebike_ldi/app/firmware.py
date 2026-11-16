"""
Firmware notifications: compare the versions your bike reports with the
versions Bosch publishes. Metadata only; nothing is ever installed.

Bosch's public "software update" page lists `Component name: version`
lines (for example "Performance Line CX smart system: 21.12.0"). The bike
reports each component's product name and software version over MCSP.

A published version is what Bosch lists for that product line; it does not
prove your particular bike is eligible, and the summary says so.
"""
from __future__ import annotations

import html
import re
import urllib.request

DEFAULT_PAGE = "https://www.bosch-ebike.com/ca/service/software-update"
# Bosch's site rejects script-like clients, so ask like a browser does.
USER_AGENT = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Components with an update entity: (key, title, datapoint component name).
COMPONENTS = [
    ("drive_unit", "Drive unit", "DriveUnit"),
    ("battery", "Battery", "Battery"),
    ("controller", "Controller", "RemoteControl"),
    ("head_unit", "Display", "HeadUnit"),
]

_LINE = re.compile(r"^(?P<name>[A-Za-z][^:\n]{0,80}?)\s*:\s*(?P<version>\d+(?:\.\d+){1,3})\.?$")


def release_notes_url(page_url: str) -> str:
    return page_url.replace("/service/software-update", "/connect/release-notes")


def parse_versions(page_html: str) -> dict[str, str]:
    """`Name: version` pairs from the page, keyed by name."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page_html)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    versions = {}
    for line in text.splitlines():
        match = _LINE.match(" ".join(line.split()))
        if match:
            versions[match["name"].strip()] = match["version"]
    return versions


def match(product_name: str | None, component: str,
          versions: dict[str, str]) -> tuple[str, str] | None:
    """The published (entry name, version) for a component, or None.

    Tries the exact product name, then "<name> smart system", then (for
    batteries) "Batteries smart system". Never guesses beyond that.
    """
    by_lower = {name.lower(): (name, v) for name, v in versions.items()}
    candidates = []
    if product_name:
        candidates += [product_name, f"{product_name} smart system"]
    if component.startswith("Battery"):
        candidates.append("Batteries smart system")
    for candidate in candidates:
        found = by_lower.get(candidate.strip().lower())
        if found:
            return found
    return None


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else None


def is_newer(latest: str | None, installed: str | None) -> bool | None:
    """True/False, or None when either version can't be compared."""
    a, b = version_tuple(latest), version_tuple(installed)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def update_payload(title: str, installed: str | None, published: tuple[str, str] | None,
                   page_url: str) -> dict:
    """The Home Assistant update-entity state for one component."""
    payload = {"title": f"{title} firmware", "installed_version": installed,
               "release_url": release_notes_url(page_url)}
    if published:
        entry, version = published
        payload["latest_version"] = version
        payload["release_summary"] = (
            f"Bosch lists {version} for '{entry}'. Published version; not confirmed "
            "for your specific bike. Updates are installed with the Flow app.")[:255]
    else:
        # No published version found: no latest_version at all (Home
        # Assistant rejects null); the caller marks the entity unavailable,
        # so it never claims "up to date" from a failed or unmatched check.
        payload["release_summary"] = "No published version found for this component."
    return payload


def fetch(page_url: str, timeout: float = 20) -> str:
    request = urllib.request.Request(page_url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")

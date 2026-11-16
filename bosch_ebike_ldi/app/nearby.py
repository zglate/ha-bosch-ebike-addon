"""
Bosch eBikes heard nearby, for the "Nearby Bosch eBikes" dropdown in Home
Assistant and the Setup Status. Pure Python.

Dropdown labels only change when a bike appears, disappears or changes
pairing mode (not with every signal-strength wobble), so Home Assistant
isn't flooded with updates. Signal strength is shown in Setup Status.
"""
from __future__ import annotations

import time

from ble_policy import pairing_text

NONE_OPTION = "— none selected —"
FORGET_AFTER_SEC = 300          # drop a bike not heard for 5 minutes


class Nearby:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._bikes: dict[str, dict] = {}   # address -> {path, rssi, pairable, known, seen}

    def heard(self, address: str, path: str, rssi: int | None, pairable: bool | None,
              known: bool) -> bool:
        """Record an advertisement. True if the dropdown options changed."""
        before = self.options()
        if rssi is None and address in self._bikes:    # signal not in this update
            rssi = self._bikes[address]["rssi"]
        pairable = None if pairable is None else bool(pairable)
        self._bikes[address] = {"path": path, "rssi": rssi, "pairable": pairable,
                                "known": known, "seen": self._clock()}
        return self.options() != before

    def prune(self) -> bool:
        """Forget bikes not heard recently. True if the options changed."""
        before = self.options()
        cutoff = self._clock() - FORGET_AFTER_SEC
        self._bikes = {a: b for a, b in self._bikes.items() if b["seen"] >= cutoff}
        return self.options() != before

    def get(self, address: str) -> dict | None:
        return self._bikes.get(address)

    def strangers(self) -> list[tuple[str, dict]]:
        """Bikes heard that aren't ours yet, strongest signal first."""
        items = [(a, b) for a, b in self._bikes.items() if not b["known"]]
        return sorted(items, key=lambda ab: -(ab[1]["rssi"] if ab[1]["rssi"] is not None
                                              else -999))

    @staticmethod
    def label(address: str, bike: dict) -> str:
        return f"{address} ({pairing_text(bike['pairable'])}{', yours' if bike['known'] else ''})"

    def options(self) -> list[str]:
        return [NONE_OPTION] + sorted(self.label(a, b) for a, b in self._bikes.items())

    @staticmethod
    def address_of(option: str) -> str | None:
        """The address in a dropdown label, or None for the 'none' option."""
        if not option or option == NONE_OPTION:
            return None
        return option.split(" ", 1)[0].upper()

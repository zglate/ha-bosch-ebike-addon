"""
Build the add-on's compact datapoint table from the maintainer's protocol
notes (an "implementation handoff" folder, not part of this repository).

Input:  implementation_handoff/datapoints.csv (+ telemetry-candidates.csv)
Output: bosch_ebike_ldi/app/datapoints.json

Each entry: id -> {component, name, kind, factor, readable, unit}
  kind    how the inner protobuf is decoded (see app/datapoints.py)
  factor  normalisation divisor (1 = none)
  readable  whether the add-on may ever send a READ/SUBSCRIBE to it. False for:
            - commands: any datapoint with an RPC result type (binding slot 3)
            - names that look like actions (reset, unlock, trigger, ...)
            - void datapoints and phone-side components

Usage: python scripts/build_datapoints.py <implementation_handoff dir>
"""
import csv
import json
import re
import sys
from pathlib import Path

# Datapoints that live on the phone side or are test/diagnostic tools.
NOT_ON_BIKE = {"MobileApp", "BoschDiagnoseApp", "CanTestNode"}

# Names that read like actions. Never touched, even with a READ, because the
# firmware's handling of a READ on a command address is unknown.
ACTION_NAME = re.compile(
    r"^(enable|disable|unlock|lock[A-Z]|trigger|reset|execute|start(Assist|Rider|Test|"
    r"Calibration|Update|Diagnos)|test[A-Z]|delete|clear|set[A-Z]|write|prepare|"
    r"report[A-Z]|perform|request|install|claim|waive|register|deregister|reboot|"
    r"restart|calibrat|factory|erase|cancel|confirm|acknowledge|apply|run[A-Z]|abort)")

SCALARS = {
    "s5UInt8V": "uint", "s6UInt16V": "uint", "s6UInt32V": "uint", "s6UInt64V": "uint",
    "s4Int8V": "sint", "s5Int16V": "sint", "s5Int32V": "sint", "s5Int64V": "sint",
}


def kind_of(encoding: str) -> tuple[str, int, str | None]:
    """(kind, factor, struct type name) for one Swift type encoding."""
    norm = re.search(r"(Uint|Int)(\d+)NormFactor(\d+)", encoding)
    if norm:
        return ("uint" if norm.group(1) == "Uint" else "sint", int(norm.group(3)), None)
    array = encoding.startswith("Say")
    for swift, kind in SCALARS.items():
        if swift in encoding:
            return (f"{kind}[]" if array else kind, 1, None)
    if encoding == "Sb":
        return ("bool", 1, None)
    if encoding == "SS":
        return ("string", 1, None)
    if encoding == "SaySSG":
        return ("string[]", 1, None)
    if encoding == "yt":
        return ("void", 1, None)
    return ("struct", 1, swift_type_name(encoding))


def swift_type_name(encoding: str) -> str | None:
    """Type name after 'MessageBus' in a Swift mangled name.

    Swift writes each identifier with its length in front (an optional
    leading 0 is a word-substitution marker), e.g.
    '10MessageBus21BikeSpeedWithValidityV' -> 'BikeSpeedWithValidity'.
    """
    match = re.search(r"MessageBus0?(\d+)", encoding)
    if not match:
        return None
    start = match.end()
    return encoding[start:start + int(match.group(1))]


def build(handoff: Path) -> dict:
    units = {}
    for row in csv.DictReader(open(handoff / "telemetry-candidates.csv", encoding="utf-8")):
        if row["physical_unit"]:
            units[row["id"].lower()] = row["physical_unit"]

    table = {}
    for row in csv.DictReader(open(handoff / "datapoints.csv", encoding="utf-8")):
        slots = json.loads(row["payload_types"] or "[]")
        value_types = [s["type_encoding"] for s in slots if s["binding_slot"] == 2]
        is_command = any(s["binding_slot"] == 3 for s in slots)
        kind, factor, struct = kind_of(value_types[0]) if value_types else ("void", 1, None)
        entry = {
            "component": row["component"],
            "name": row["name"],
            "kind": "rpc" if is_command else kind,
            "factor": factor,
            "readable": (kind != "void" and not is_command
                         and row["component"] not in NOT_ON_BIKE
                         and not ACTION_NAME.match(row["name"])),
        }
        if struct:
            entry["struct"] = struct
        if row["id"].lower() in units:
            entry["unit"] = units[row["id"].lower()]
        table[row["id"].lower()] = entry
    return table


if __name__ == "__main__":
    handoff = Path(sys.argv[1])
    out = Path(__file__).resolve().parents[1] / "bosch_ebike_ldi" / "app" / "datapoints.json"
    table = build(handoff)
    out.write_text(json.dumps(table, indent=0, sort_keys=True), encoding="utf-8")
    kinds = {}
    for e in table.values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"{len(table)} datapoints, {sum(e['readable'] for e in table.values())} readable")
    print(dict(sorted(kinds.items(), key=lambda kv: -kv[1])))

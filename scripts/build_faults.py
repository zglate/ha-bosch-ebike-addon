"""Build app/faults.json from the handoff's faults.csv (Bosch's own fault table).

Usage: build_faults.py FAULTS_CSV OUT_JSON

Keeps what the page needs per fault code (decimal): severity, component,
title, description and measures. "-" placeholders become empty.
"""
import csv
import json
import sys


def main(src: str, dst: str) -> None:
    table = {}
    with open(src, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                code = int(row["code_decimal_if_hex"])
            except (KeyError, ValueError):
                continue
            clean = {k: ("" if (row.get(k) or "").strip() == "-" else (row.get(k) or "").strip())
                     for k in ("severity", "component_name", "title", "description", "measures")}
            table[str(code)] = {"hex": row["code"], **clean}
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(table, fh, ensure_ascii=False, indent=0, sort_keys=True)
    print(f"{len(table)} fault codes -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

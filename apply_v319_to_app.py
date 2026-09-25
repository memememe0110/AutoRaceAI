#!/usr/bin/env python3
"""Apply Ver319 recommendation gate to a local app.py.

Usage:
  python3 apply_v319_to_app.py /path/to/app.py

What it does:
  1. Inserts `from v319_recommendation import v319_live_recommendation` near top imports
  2. Rebinds `_v305_live_recommendation` / `_v314_...` / `_v301_...` to Ver319
  3. Does NOT force APP_VERSION string change (gate is independent of display version)
  4. Writes app.py.bak backup first

Behaviour of the gate is identical to Ver314 thresholds; only audit metadata is added.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 apply_v319_to_app.py /path/to/app.py")
        sys.exit(1)
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Not found: {path}")
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_text(text, encoding="utf-8")
    print(f"Backup: {bak}")

    original = text

    # 1) Import
    if "v319_recommendation" not in text:
        m = re.search(r"(^(?:from |import ).+\n)+", text, re.M)
        insert = "from v319_recommendation import v319_live_recommendation\n"
        if m:
            text = text[: m.end()] + insert + text[m.end() :]
        else:
            text = insert + text
        print("Inserted import")

    # 2) Rebind recommendation functions at end of module
    marker = "v319_live_recommendation  # Ver319 gate"
    if marker not in text:
        text += (
            "\n\n# === Ver319 recommendation gate (same thresholds as Ver314, + audit tags) ===\n"
            "_v305_live_recommendation = v319_live_recommendation  # Ver319 gate\n"
            "try:\n"
            "    _v314_live_recommendation = v319_live_recommendation  # Ver319 gate\n"
            "except NameError:\n"
            "    pass\n"
            "try:\n"
            "    _v301_live_recommendation = v319_live_recommendation  # Ver319 gate\n"
            "except NameError:\n"
            "    pass\n"
        )
        print("Appended Ver319 gate rebind")

    if text == original:
        print("No changes applied (already patched?)")
    else:
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path}")
        print("Done. Place v319_recommendation.py next to app.py and restart the app.")
        print("Note: decision thresholds are unchanged from Ver314; only metadata/audit fields were added.")


if __name__ == "__main__":
    main()

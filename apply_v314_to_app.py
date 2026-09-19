#!/usr/bin/env python3
"""Apply Ver314 recommendation patch to a local app.py.

Usage:
  python3 apply_v314_to_app.py /path/to/app.py

What it does:
  1. Inserts `from v314_recommendation import v314_live_recommendation` near top imports
  2. Rebinds `_v305_live_recommendation` (and `_v301_...` if present) to Ver314
  3. Bumps visible version strings Ver313 -> Ver314 (best-effort)
  4. Writes app.py.bak backup first
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 apply_v314_to_app.py /path/to/app.py")
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
    if "v314_recommendation" not in text:
        m = re.search(r"(^(?:from |import ).+\n)+", text, re.M)
        insert = "from v314_recommendation import v314_live_recommendation\n"
        if m:
            text = text[: m.end()] + insert + text[m.end() :]
        else:
            text = insert + text
        print("Inserted import")

    # 2) Rebind recommendation functions at end of module
    if "v314_live_recommendation  # Ver314 override" not in text:
        text += (
            "\n\n# === Ver314 recommendation override ===\n"
            "_v305_live_recommendation = v314_live_recommendation  # Ver314 override\n"
            "try:\n"
            "    _v301_live_recommendation = v314_live_recommendation  # Ver314 override\n"
            "except NameError:\n"
            "    pass\n"
        )
        print("Appended Ver314 override rebind")

    # 3) Version bump
    text2 = re.sub(r"\bVer313\b", "Ver314", text)
    text2 = text2.replace("Ver31414", "Ver314")
    if text2 != text:
        print("Bumped Ver313 -> Ver314 in strings")
        text = text2

    text = re.sub(
        r'(APP_VERSION\s*=\s*["\'])Ver\d+(["\'])',
        r"\1Ver314\2",
        text,
        count=3,
    )

    if text == original:
        print("No changes applied (already patched?)")
    else:
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path}")
        print("Done. Place v314_recommendation.py next to app.py and restart the app.")


if __name__ == "__main__":
    main()

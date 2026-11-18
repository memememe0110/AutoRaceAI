#!/usr/bin/env python3
"""Regression tests for strict prediction-history reuse matching.

The test extracts the pure matching helpers from app.py, so Streamlit and the
production database are not imported or touched.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path


def load_helpers(app_path: Path):
    source = app_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_v319_race_key_candidates",
        "_v326_normalize_config_json",
        "_v326_config_json_fingerprint",
        "_v326_prediction_fingerprint_codes",
        "_v326_find_matching_prediction_history",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"__builtins__": __builtins__, "re": re, "sqlite3": sqlite3, "hashlib": hashlib}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(app_path), "exec"), namespace)
    return namespace


def make_db(qcfg_fp: str) -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    handle.close()
    con = sqlite3.connect(handle.name)
    con.execute("""CREATE TABLE v231_prediction_history (
        history_id INTEGER PRIMARY KEY, race_key TEXT, app_version TEXT,
        trials INTEGER, seed INTEGER, raw_text TEXT, settings_hash TEXT
    )""")
    con.execute(
        """INSERT INTO v231_prediction_history
        (history_id, race_key, app_version, trials, seed, raw_text, settings_hash)
        VALUES (?,?,?,?,?,?,?)""",
        (1, "race-test", "Ver326", 20000, 12345, "race card fixture", "q|qcfg:" + qcfg_fp),
    )
    con.commit()
    con.close()
    return handle.name


def test_strict_reuse_cases():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    ns = load_helpers(app_path)
    config = {
        "save_version": "Ver326",
        "v330a_enabled": False,
        "code_commit_sha": "code-a",
        "db_sha256": "db-a",
        "config_version": 2,
    }
    cfg_fp = ns["_v326_config_json_fingerprint"](config)
    db = make_db(cfg_fp)
    common = {
        "db_path": db,
        "race_key": "race-test",
        "save_version": "Ver326",
        "seed": 12345,
        "input_sha256": hashlib.sha256(b"race card fixture").hexdigest(),
        "config_json": config,
        "code_commit_sha": "code-a",
        "db_sha256": "db-a",
    }
    assert ns["_v326_find_matching_prediction_history"](trials=20000, **common)
    assert ns["_v326_find_matching_prediction_history"](trials=10000, **common) is None
    config["code_commit_sha"] = "code-b"
    assert ns["_v326_find_matching_prediction_history"](trials=20000, **common) is None
    config["code_commit_sha"] = "code-a"
    config["db_sha256"] = "db-b"
    common["db_sha256"] = "db-b"
    assert ns["_v326_find_matching_prediction_history"](trials=20000, **common) is None
    config["db_sha256"] = "db-a"
    common["db_sha256"] = "db-a"
    common["input_sha256"] = hashlib.sha256(b"changed card").hexdigest()
    assert ns["_v326_find_matching_prediction_history"](trials=20000, **common) is None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default=str(Path(__file__).resolve().parents[1] / "app.py"))
    args = parser.parse_args()
    # Keep the file runnable without pytest in CI/debug sessions.
    ns = load_helpers(Path(args.app))
    print(json.dumps({"loaded_helpers": sorted(k for k in ns if k.startswith("_v326_"))}))
    test_strict_reuse_cases()
    print("PASS")

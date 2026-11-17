#!/usr/bin/env python3
"""Run a few races twice and require identical Lap1 distributions.

This checks the same code path as compare mode, without Streamlit.
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np


def _load_app(app_path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("app_identity", app_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _one(app, db: Path, race_key: str, mode: str, trials: int, seed: int) -> dict:
    os.environ["AUTORACE_VER330A_COMPARE"] = "1"
    os.environ["AUTORACE_COMPARE_MODE"] = mode
    os.environ["AUTORACE_330_VALIDATION_MODE"] = "1"
    os.environ["PYTHONHASHSEED"] = "0"
    app.engine.DB_PATH = str(db)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        row = con.execute(
            "SELECT raw_text, venue_override FROM v222_prediction_restore WHERE race_key=?",
            (race_key,),
        ).fetchone()
    if not row or not str(row[0] or "").strip():
        raise SystemExit(f"raw_text missing: {race_key}")
    text = str(row[0])
    if row[1]:
        text = f"開催場: {row[1]}\n" + text
    app.random.seed(seed)
    np.random.seed(seed)
    app._V328_EXPERIMENT = False
    app._V328_WORKER_ACTIVE = False
    app._V330A_EXPERIMENT = mode == "Ver330-A"
    app._V331_LAP1_TUNE = False
    df, bets, output, entries, meta = app.engine.ver16_run_prediction(
        text, trials=trials, seed=seed, manual_excluded=None
    )
    _sim_df, _sim_bets, audit = app._v230_six_lap_simulation(
        df, bets, entries, meta, int(trials), int(seed)
    )
    counts = dict((audit or {}).get("ver330a_validation_lap_order_counts") or {}).get("1") or {}
    return {
        "race_key": race_key,
        "mode": mode,
        "sim_trials": int((audit or {}).get("v284_transition_audit", {}).get("trials") or 0),
        "lap1": counts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--app", default=str(ROOT / "app.py"))
    ap.add_argument("--mode", default="Ver326", choices=("Ver326", "Ver330-A"))
    ap.add_argument("--trials", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--races", nargs="+", required=True)
    a = ap.parse_args()
    app = _load_app(Path(a.app))
    db = Path(a.db).resolve()
    failed = 0
    for race in a.races:
        first = _one(app, db, race, a.mode, a.trials, a.seed)
        second = _one(app, db, race, a.mode, a.trials, a.seed)
        same = first["lap1"] == second["lap1"] and first["sim_trials"] == second["sim_trials"] == a.trials
        print(json.dumps({
            "race_key": race, "mode": a.mode, "identical": same,
            "patterns": len(first["lap1"]), "trials": first["sim_trials"],
        }, ensure_ascii=False))
        if not same:
            failed += 1
    raise SystemExit(failed)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Ver330-A Axis A: lap2-5 bit-identical 検証。同一レース・seed・trialsで両モードを比較。"""
from __future__ import annotations
import argparse, importlib.util, json, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('verify_ver330_validation', HERE / 'verify_ver330_validation.py')
verify = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify
spec.loader.exec_module(verify)


def run_mode(app_path, db, race_key, mode, trials, seed, timeout, tmp_dir):
    out = Path(tmp_dir) / f"{mode.replace('-','_')}.json"
    data = verify.run_one_race(app_path, db, race_key, mode, trials, seed, out, timeout)
    if not data.get("fingerprint"):
        raise RuntimeError(f"{mode}: fingerprint missing")
    return data


def compare(a, b):
    fa = a.get("fingerprint") or {}
    fb = b.get("fingerprint") or {}
    rows = []
    for lap in range(1, 7):
        x = fa.get(str(lap)) or {}
        y = fb.get(str(lap)) or {}
        rows.append({"lap": lap, "identical": x == y, "ver326": x, "ver330a": y})
    lap2_5 = all(r["identical"] for r in rows if 2 <= r["lap"] <= 5)
    lap6_diff = any(not r["identical"] for r in rows if r["lap"] == 6)
    return {
        "lap2_5_bit_identical": lap2_5,
        "lap6_diff": lap6_diff,
        "axis_a_valid": bool(lap2_5 and lap6_diff),
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--app", required=True)
    ap.add_argument("--race-key", required=True)
    ap.add_argument("--trials", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.trials != 20000 or a.seed != 12345:
        raise SystemExit("Pre-registered: trials=20000, seed=12345")
    db = Path(a.db).resolve()
    app = Path(a.app).resolve()
    out_path = Path(a.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        d326 = run_mode(app, db, a.race_key, "Ver326", a.trials, a.seed, a.timeout, td)
        d330 = run_mode(app, db, a.race_key, "Ver330-A", a.trials, a.seed, a.timeout, td)
    result = {
        "race_key": a.race_key,
        "trials": a.trials,
        "seed": a.seed,
        **compare(d326, d330),
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "axis_a_valid": result["axis_a_valid"],
        "lap2_5_bit_identical": result["lap2_5_bit_identical"],
        "lap6_diff": result["lap6_diff"],
    }, ensure_ascii=False))
    return 0 if result["axis_a_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

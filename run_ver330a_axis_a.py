#!/usr/bin/env python3
"""Ver330-A Axis A: lap2-5 bit-identical 検証。

同一 race_key / seed / trials で Ver326 と Ver330-A を実行し、
fingerprint を比較する。

合格条件（事前登録）:
  - trials=20000, seed=12345
  - lap2〜5 の fingerprint が完全一致
  - lap6 に差がある（周6抑制が効いている）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_verify():
    path = HERE / "verify_ver330_validation.py"
    if not path.exists():
        raise SystemExit(f"verify_ver330_validation.py がありません: {path}")
    spec = importlib.util.spec_from_file_location("verify_ver330_validation", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def make_snapshot(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
        f"file:{src.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0
    ) as con_src:
        con_src.execute("PRAGMA query_only=ON")
        with sqlite3.connect(str(dst), timeout=30.0) as con_dst:
            con_src.backup(con_dst)
            con_dst.commit()


def run_mode(verify, app_path, snapshot, race_key, mode, trials, seed, timeout, tmp_dir):
    out = Path(tmp_dir) / f"{mode.replace('-', '_')}.json"
    data = verify.run_one_race(
        Path(app_path),
        Path(snapshot),
        race_key,
        mode,
        int(trials),
        int(seed),
        out,
        float(timeout),
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"{mode}: invalid result type {type(data)}")
    if not data.get("fingerprint"):
        raise RuntimeError(f"{mode}: fingerprint missing (keys={list(data.keys())})")
    return data


def compare(a: dict, b: dict) -> dict:
    """Axis A 判定。

    集約hashは全試行のRNGストリームを含むため、Ver330-Aのlap6変更が
    試行1以降のlap1-5にも波及してDIFFになる。実装独立性の判定は
    「試行0だけ」の trial0_hash で行う。
    """
    fa = a.get("fingerprint") or {}
    fb = b.get("fingerprint") or {}
    rows = []
    for lap in range(1, 7):
        x = fa.get(str(lap)) or {}
        y = fb.get(str(lap)) or {}
        trial0_a = str(x.get("trial0_hash") or "")
        trial0_b = str(y.get("trial0_hash") or "")
        rows.append({
            "lap": lap,
            "identical": x == y,
            "trial0_identical": bool(trial0_a and trial0_a == trial0_b),
            "ver326": x,
            "ver330a": y,
        })
    trial0_lap2_5 = all(r["trial0_identical"] for r in rows if 2 <= r["lap"] <= 5)
    trial0_lap6_diff = any(not r["trial0_identical"] for r in rows if r["lap"] == 6)
    fingerprints_present = bool(
        rows
        and all(
            r["ver326"] and r["ver330a"] and r["ver326"].get("trial0_hash")
            for r in rows
        )
    )
    sim326 = int(a.get("sim_trials") or a.get("trials") or 0)
    sim330 = int(b.get("sim_trials") or b.get("trials") or 0)
    return {
        "trial0_lap2_5_identical": bool(trial0_lap2_5),
        "trial0_lap6_diff": bool(trial0_lap6_diff),
        "lap2_5_bit_identical": bool(trial0_lap2_5),
        "lap6_diff": bool(trial0_lap6_diff),
        "fingerprints_present": fingerprints_present,
        "sim_count_identical": bool(sim326 == sim330 and sim326 > 0),
        "ver326_sim_count": sim326,
        "ver330a_sim_count": sim330,
        "axis_a_valid": bool(
            fingerprints_present
            and trial0_lap2_5
            and trial0_lap6_diff
            and sim326 == sim330
            and sim326 > 0
        ),
        "rows": rows,
    }



def main() -> int:
    ap = argparse.ArgumentParser(description="Ver330-A Axis A (lap2-5 identical, lap6 diff)")
    ap.add_argument("--db", required=True, help="比較元DB（読み取り専用snapshotを作成）")
    ap.add_argument("--app", required=True, help="app.py パス")
    ap.add_argument("--race-key", required=True)
    ap.add_argument("--trials", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--out", required=True, help="結果JSON出力先")
    ap.add_argument(
        "--self-check",
        action="store_true",
        help="Ver326 vs Ver326 でハーネス決定性を確認（Axis A本番判定ではない）",
    )
    a = ap.parse_args()

    if int(a.trials) != 20000 or int(a.seed) != 12345:
        raise SystemExit("Pre-registered Axis A: trials=20000, seed=12345 のみ許可")

    db = Path(a.db).expanduser().resolve()
    app = Path(a.app).expanduser().resolve()
    out_path = Path(a.out).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"DBがありません: {db}")
    if not app.exists():
        raise SystemExit(f"app.pyがありません: {app}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    verify = _load_verify()

    with tempfile.TemporaryDirectory(prefix="axis_a_") as td:
        td_path = Path(td)
        snapshot = td_path / "db_snapshot.sqlite3"
        make_snapshot(db, snapshot)
        mode_b = "Ver326" if a.self_check else "Ver330-A"
        d326 = run_mode(
            verify, app, snapshot, a.race_key, "Ver326",
            a.trials, a.seed, a.timeout, td,
        )
        d330 = run_mode(
            verify, app, snapshot, a.race_key, mode_b,
            a.trials, a.seed, a.timeout, td,
        )

    result = {
        "race_key": a.race_key,
        "trials": int(a.trials),
        "seed": int(a.seed),
        "self_check": bool(a.self_check),
        "mode_a": "Ver326",
        "mode_b": ("Ver326" if a.self_check else "Ver330-A"),
        **compare(d326, d330),
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "axis_a_valid": result["axis_a_valid"],
        "trial0_lap2_5_identical": result.get("trial0_lap2_5_identical"),
        "trial0_lap6_diff": result.get("trial0_lap6_diff"),
        "lap2_5_bit_identical": result["lap2_5_bit_identical"],
        "lap6_diff": result["lap6_diff"],
        "fingerprints_present": result["fingerprints_present"],
        "race_key": result["race_key"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    for row in result["rows"]:
        t0 = "IDENTICAL" if row.get("trial0_identical") else "DIFF"
        agg = "IDENTICAL" if row.get("identical") else "DIFF"
        print(f"lap={row['lap']} trial0={t0} aggregate={agg}")

    if a.self_check:
        # self-check: 全lap一致が成功（axis_a_validの定義とは別）
        _rows = result.get("rows") or []
        if _rows and any("trial0_identical" in row for row in _rows):
            all_identical = all(bool(row.get("trial0_identical")) for row in _rows)
        else:
            all_identical = all(row.get("identical") for row in _rows)
        result["self_check_identical"] = bool(all_identical)
        out_path.write_text(
            __import__("json").dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            __import__("json").dumps(
                {
                    "self_check": True,
                    "self_check_identical": bool(all_identical),
                    "lap2_5_bit_identical": result["lap2_5_bit_identical"],
                    "lap6_diff": result["lap6_diff"],
                    "race_key": result["race_key"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if all_identical else 2

    if result["axis_a_valid"]:
        return 0
    # 周2-5一致だが周6も一致 → 抑制未効き
    if result["lap2_5_bit_identical"] and not result["lap6_diff"]:
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

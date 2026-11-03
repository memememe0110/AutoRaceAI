#!/usr/bin/env python3
"""Ver330-A専用: Ver326 vs Ver330-A の1R/378R DB非汚染比較ランナー。

実行内容:
- 元DBをSQLite backupで一時snapshot化
- Ver326 / Ver330-Aを別プロセスで実行
- Streamlit cache/session_stateを比較プロセス間で共有しない
- 予測/6周Simの保存・学習更新・GitHub同期は行わない
- 周2〜5の状態fingerprintが完全一致するか判定

例:
  python compare_ver326_ver330a.py --race-key '20260929_山陽_8R'
  python compare_ver326_ver330a.py --trials 8000 --seed 12345

DBを明示する場合:
  python compare_ver326_ver330a.py --db /path/to/autorace_players.sqlite3
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


FIXED_HEADER = [
    "lap",
    "order_hash",
    "gap_sum",
    "momentum_sum",
    "slowdown_sum",
    "pass_count",
    "sim_count",
]


def make_snapshot(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0) as con_src:
        con_src.execute("PRAGMA query_only=ON")
        with sqlite3.connect(str(dst), timeout=30.0) as con_dst:
            con_src.backup(con_dst)
            con_dst.commit()


def latest_race_key(db_path: Path) -> str:
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10.0) as con:
        con.execute("PRAGMA query_only=ON")
        row = con.execute(
            "SELECT race_key FROM v222_prediction_restore "
            "WHERE COALESCE(raw_text,'')<>'' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise RuntimeError("v222_prediction_restoreに比較可能なraw_textがありません")
    return str(row[0] or "")


def run_mode(
    *,
    app_path: Path,
    snapshot: Path,
    mode: str,
    race_key: str,
    trials: int,
    seed: int,
    out_path: Path,
    timeout: float,
) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "AUTORACE_VER330A_COMPARE": "1",
            "AUTORACE_COMPARE_MODE": mode,
            "AUTORACE_COMPARE_DB": str(snapshot),
            "AUTORACE_COMPARE_RACE_KEY": race_key,
            "AUTORACE_COMPARE_TRIALS": str(int(trials)),
            "AUTORACE_COMPARE_SEED": str(int(seed)),
            "AUTORACE_COMPARE_OUT": str(out_path),
            "AUTORACE_330_VALIDATION_MODE": "1",
            "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        }
    )

    streamlit = shutil.which("streamlit")
    if streamlit:
        cmd = [
            streamlit,
            "run",
            str(app_path),
            "--server.headless=true",
            "--server.fileWatcherType=none",
            "--browser.gatherUsageStats=false",
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.headless=true",
            "--server.fileWatcherType=none",
            "--browser.gatherUsageStats=false",
        ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(app_path.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    start = time.monotonic()
    logs = []
    try:
        while time.monotonic() - start < timeout:
            if out_path.exists():
                break
            line = proc.stdout.readline() if proc.stdout is not None else ""
            if line:
                logs.append(line.rstrip())
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.2)
        if not out_path.exists():
            raise TimeoutError(
                f"{mode}の比較実行が{timeout:.0f}秒以内に完了しませんでした。"
                + (" 最終ログ: " + " | ".join(logs[-8:]) if logs else "")
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    data = json.loads(out_path.read_text(encoding="utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"{mode}実行失敗: {data.get('error') or data}")
    return data


def write_fp_csv(path: Path, data: dict) -> None:
    fp = data.get("fingerprint") or {}
    sim_count = int(data.get("sim_trials") or 0)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIXED_HEADER)
        w.writeheader()
        for lap in range(1, 7):
            row = fp.get(str(lap)) or {}
            w.writerow(
                {
                    "lap": lap,
                    "order_hash": str(row.get("hash") or ""),
                    "gap_sum": row.get("gap_sum", ""),
                    "momentum_sum": row.get("momentum_sum", ""),
                    "slowdown_sum": row.get("slowdown_sum", ""),
                    "pass_count": row.get("pass_count", ""),
                    "sim_count": sim_count,
                }
            )


def compare(fp326: dict, fp330: dict) -> dict:
    a = fp326.get("fingerprint") or {}
    b = fp330.get("fingerprint") or {}
    rows = []
    for lap in range(1, 7):
        x = a.get(str(lap)) or {}
        y = b.get(str(lap)) or {}
        rows.append(
            {
                "lap": lap,
                "identical": x == y,
                "ver326": x,
                "ver330a": y,
            }
        )
    sim326 = int(fp326.get("sim_trials") or 0)
    sim330 = int(fp330.get("sim_trials") or 0)
    if sim326 != sim330:
        print(f"WARNING: sim_trials mismatch: 326={sim326} 330A={sim330}")
    lap2_5_identical = all(r["identical"] for r in rows if 2 <= r["lap"] <= 5)
    lap6_diff = any(not r["identical"] for r in rows if r["lap"] == 6)
    fingerprints_present = bool(
        rows
        and all(r["ver326"] and r["ver330a"] for r in rows)
    )
    axis_a_valid = bool(
        sim326 == sim330
        and sim326 > 0
        and fingerprints_present
        and lap2_5_identical
    )
    return {
        "axis_a_valid": axis_a_valid,
        "lap2_5_bit_identical": lap2_5_identical,
        "fingerprints_present": fingerprints_present,
        "lap6_diff": lap6_diff,
        "rows": rows,
        "ver326_sim_count": sim326,
        "ver330a_sim_count": sim330,
        "sim_count_identical": bool(sim326 == sim330),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="比較元DB。未指定ならこのスクリプトと同じフォルダのDB")
    ap.add_argument("--race-key", default="", help="対象race_key。未指定ならrestore最新1件")
    ap.add_argument("--trials", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out-dir", default="", help="結果保存先。未指定なら一時フォルダ")
    args = ap.parse_args()

    base = Path(__file__).resolve().parent
    app_path = base / "app.py"
    db_path = Path(args.db).expanduser().resolve() if args.db else (base / "autorace_players.sqlite3").resolve()
    if not app_path.exists():
        raise SystemExit(f"app.pyがありません: {app_path}")
    if not db_path.exists():
        raise SystemExit(f"DBがありません: {db_path}")

    out_root = Path(args.out_dir).expanduser().resolve() if args.out_dir else (base / "ver330a_compare_results")
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = out_root / f"compare_ver326_ver330a_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)

    snapshot = run_dir / "db_snapshot.sqlite3"
    make_snapshot(db_path, snapshot)
    race_key = args.race_key or latest_race_key(snapshot)

    out326 = run_dir / "ver326.json"
    out330 = run_dir / "ver330a.json"
    print(f"[Ver330-A] snapshot={snapshot}")
    print(f"[Ver330-A] race_key={race_key}")
    print(f"[Ver330-A] trials={args.trials} seed={args.seed}")

    d326 = run_mode(
        app_path=app_path, snapshot=snapshot, mode="Ver326", race_key=race_key,
        trials=args.trials, seed=args.seed, out_path=out326, timeout=args.timeout,
    )
    d330 = run_mode(
        app_path=app_path, snapshot=snapshot, mode="Ver330-A", race_key=race_key,
        trials=args.trials, seed=args.seed, out_path=out330, timeout=args.timeout,
    )

    write_fp_csv(run_dir / "ver326_fingerprint.csv", d326)
    write_fp_csv(run_dir / "ver330a_fingerprint.csv", d330)
    summary = compare(d326, d330)
    (run_dir / "comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== Ver330-A implementation check ===")
    for r in summary["rows"]:
        print(f"lap={r['lap']} {'IDENTICAL' if r['identical'] else 'DIFF'}")
    print(f"lap2-5 bit-identical: {summary['lap2_5_bit_identical']}")
    print(f"lap6 diff: {summary['lap6_diff']}")
    print(f"results: {run_dir}")

    if not summary["axis_a_valid"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

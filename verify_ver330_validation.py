#!/usr/bin/env python3
"""Ver330-A検証用スタンドアロンランナー。

確定した2軸フレームをそのまま実行する。

Phase 1: DB汚染チェック
  Ver326 / Ver328 の予測履歴に、実結果(result_laps)が存在しない race_key が混入していないか確認。

Phase 2: Ver326 Lap1単独検証
  汚染除外後の共通セットについて、Ver326のLap1を20,000 trialsで再シミュレーション。
  DB保存・学習更新・履歴登録・GitHub保存は行わない。
  主指標: pattern_rank / pattern_count / pattern_prob / uniform_ratio
  補助: top1_prob / relative_lift

Phase 3: Axis A
  compare_ver326_ver330a.py を使って、同一snapshot・同一seedでVer326/Ver330-Aを比較。
  lap2-5 bit-identical、lap6のみ差を確認する。

通常のVer326本番ロジックやDBは変更しない。検証用snapshotも読み取り専用にする。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import hashlib

SEED_DEFAULT = 12345
TRIALS_DEFAULT = 20000
EXPECTED_LAP1_COUNT = 377
EXCLUDED_NO_LAP1 = {"20260809_川口_12R"}  # 全返還でLap1順位が存在しない
VERSIONS = ("Ver326", "Ver328")


def ro_connect(path: Path):
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0)
    con.execute("PRAGMA query_only=ON")
    return con


def table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def columns(con, table: str) -> set[str]:
    return {str(r[1]) for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}


def prediction_table(con) -> str:
    for t in ("v231_prediction_history", "prediction_history", "predictions"):
        if table_exists(con, t):
            c = columns(con, t)
            if "race_key" in c and "app_version" in c:
                return t
    raise RuntimeError("予測履歴テーブル(v231_prediction_history / prediction_history / predictions)が見つかりません")


def make_snapshot(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with ro_connect(src) as con_src, sqlite3.connect(str(dst), timeout=30.0) as con_dst:
        con_src.backup(con_dst)
        con_dst.commit()
    # accidental writeを検出しやすくするため、検証中は読み取り専用にする。
    dst.chmod(0o444)


def contamination_check(db: Path, versions=VERSIONS) -> dict:
    with ro_connect(db) as con:
        ptab = prediction_table(con)
        if not table_exists(con, "result_laps"):
            raise RuntimeError("実結果テーブル result_laps が存在しません")
        pcols = columns(con, ptab)
        app_col = "app_version"
        ph_rows = con.execute(
            f'''SELECT DISTINCT race_key FROM "{ptab}" WHERE app_version IN ({','.join('?' for _ in versions)})''',
            tuple(versions),
        ).fetchall()
        predicted = {str(r[0]) for r in ph_rows if str(r[0] or "").strip()}
        actual_condition, actual_value = result_lap1_condition(con)
        actual_rows = con.execute(
            f"SELECT DISTINCT race_key FROM result_laps WHERE {actual_condition}",
            (actual_value,),
        ).fetchall()
        actual = {str(r[0]) for r in actual_rows if str(r[0] or "").strip()}
        contamination = sorted(predicted - actual)
        common = sorted(predicted & actual)
        per_version = {}
        for ver in versions:
            rows = con.execute(
                f'''SELECT DISTINCT race_key FROM "{ptab}" WHERE app_version=?''', (ver,)
            ).fetchall()
            keys = {str(r[0]) for r in rows if str(r[0] or "").strip()}
            per_version[ver] = {
                "prediction_races": len(keys),
                "with_actual_lap1": len(keys & actual),
                "without_actual_lap1": sorted(keys - actual),
            }
        # common setは両方の版に存在し、かつ実Lap1があるもの
        common326 = {
            str(r[0]) for r in con.execute(
                f'''SELECT DISTINCT race_key FROM "{ptab}" WHERE app_version=?''', ("Ver326",)
            ).fetchall() if str(r[0] or "").strip()
        }
        common328 = {
            str(r[0]) for r in con.execute(
                f'''SELECT DISTINCT race_key FROM "{ptab}" WHERE app_version=?''', ("Ver328",)
            ).fetchall() if str(r[0] or "").strip()
        }
        common_set = sorted(common326 & common328 & actual)
    return {
        "prediction_table": ptab,
        "prediction_race_count": len(predicted),
        "actual_lap1_race_count": len(actual),
        "contamination_count": len(contamination),
        "contamination_race_keys": contamination,
        "common_set_count": len(common_set),
        "common_set": common_set,
        "per_version": per_version,
    }


def result_lap1_condition(con) -> tuple[str, str]:
    cols = columns(con, "result_laps")
    if "lap_label" in cols:
        return "lap_label=?", "1周目"
    if "lap_no" in cols:
        return "lap_no=?", 1
    raise RuntimeError("result_lapsにlap_label/lap_noのいずれもありません")


def actual_lap_order(con, race_key: str) -> tuple[int, ...] | None:
    condition, value = result_lap1_condition(con)
    rows = con.execute(
        f"SELECT position, car_no FROM result_laps WHERE race_key=? AND {condition} ORDER BY position",
        (race_key, value),
    ).fetchall()
    if not rows:
        return None
    order = tuple(int(r[1]) for r in rows if r[1] is not None)
    if not order or len(set(order)) != len(order):
        return None
    return order


def run_one_race(app_path: Path, snapshot: Path, race_key: str, mode: str, trials: int, seed: int, out_path: Path, timeout: float) -> dict:
    env = os.environ.copy()
    env.update({
        "AUTORACE_VER330A_COMPARE": "1",
        "AUTORACE_COMPARE_MODE": mode,
        "AUTORACE_COMPARE_DB": str(snapshot),
        "AUTORACE_COMPARE_RACE_KEY": race_key,
        "AUTORACE_COMPARE_TRIALS": str(int(trials)),
        "AUTORACE_COMPARE_SEED": str(int(seed)),
        "AUTORACE_COMPARE_OUT": str(out_path),
        "AUTORACE_330_VALIDATION_MODE": "1",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    })
    streamlit = shutil.which("streamlit")
    cmd = ([streamlit] if streamlit else [sys.executable, "-m", "streamlit"]) + [
        "run", str(app_path), "--server.headless=true",
        "--server.fileWatcherType=none", "--browser.gatherUsageStats=false",
    ]
    proc = subprocess.Popen(cmd, cwd=str(app_path.parent), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    start = time.monotonic()
    logs: list[str] = []
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
            raise TimeoutError(f"{mode} {race_key}: timeout {timeout:.0f}s; " + " | ".join(logs[-6:]))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)
    data = json.loads(out_path.read_text(encoding="utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"{mode} {race_key}: {data.get('error') or data}")
    return data


def pattern_metrics(counts: dict[str, int], actual: tuple[int, ...], trials: int) -> dict:
    parsed = {tuple(int(x) for x in str(k).split("-") if x != ""): int(v) for k, v in counts.items()}
    n = len(actual)
    actual_count = int(parsed.get(actual, 0))
    if not parsed:
        raise RuntimeError("Lap1 pattern countが空です")
    observed_trials = sum(parsed.values())
    if observed_trials != int(trials):
        raise RuntimeError(f"Lap1 pattern count合計がtrialsと不一致: {observed_trials} != {trials}")
    ranked = sorted(parsed.values(), reverse=True)
    rank = 1 + sum(v > actual_count for v in parsed.values()) if actual_count > 0 else len(parsed) + 1
    prob = actual_count / float(trials)
    top1_prob = ranked[0] / float(trials)
    factorial = math.factorial(n)
    uniform_ratio = prob * factorial
    relative_lift = (prob / top1_prob) if top1_prob > 0 else 0.0
    return {
        "car_count": n,
        "actual_pattern": "-".join(map(str, actual)),
        "unique_pattern_count": len(parsed),
        "pattern_rank": rank,
        "pattern_count": actual_count,
        "pattern_prob": prob,
        "uniform_ratio": uniform_ratio,
        "top1_prob": top1_prob,
        "relative_lift": relative_lift,
        "observed_trials": observed_trials,
        "rank_cutoff_25pct": int(math.ceil(len(parsed) * 0.25)),
        "rank_pass": bool(rank <= math.ceil(len(parsed) * 0.25)),
        "uniform_pass": bool(uniform_ratio >= 50.0),
        "top1_pass": bool(top1_prob >= 0.02),
        "relative_lift_pass": bool(relative_lift >= 0.20),
    }


def median(values: list[float]) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    m = len(xs) // 2
    return float(xs[m]) if len(xs) % 2 else float((xs[m-1] + xs[m]) / 2.0)


def run_lap1(db: Path, app_path: Path, out_dir: Path, race_keys: list[str], trials: int, seed: int, timeout: float) -> dict:
    snapshot = out_dir / "validation_snapshot.sqlite3"
    make_snapshot(db, snapshot)
    # snapshotはread-onlyのまま。app側のDB書き込みが発生した場合は失敗させる。
    with ro_connect(snapshot) as con:
        metrics_rows = []
        for idx, race_key in enumerate(race_keys, 1):
            actual = actual_lap_order(con, race_key)
            if not actual:
                continue
            out_json = out_dir / "runs" / f"{idx:03d}.json"
            out_json.parent.mkdir(parents=True, exist_ok=True)
            data = run_one_race(app_path, snapshot, race_key, "Ver326", trials, seed, out_json, timeout)
            if not data.get("validation_full_trials"):
                raise RuntimeError(f"{race_key}: full-trials validation modeが有効になっていません")
            counts = data.get("lap_order_counts", {}).get("1", {})
            m = pattern_metrics(counts, actual, trials)
            m["race_key"] = race_key
            m["sim_trials_reported"] = int(data.get("sim_trials") or 0)
            metrics_rows.append(m)
            print(f"[{idx}/{len(race_keys)}] {race_key} rank={m['pattern_rank']}/{m['unique_pattern_count']} prob={m['pattern_prob']:.4f} uniform={m['uniform_ratio']:.1f} top1={m['top1_prob']:.4f} lift={m['relative_lift']:.3f}")
    csv_path = out_dir / "ver326_lap1_metrics.csv"
    fields = list(metrics_rows[0].keys()) if metrics_rows else []
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        if fields:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(metrics_rows)
    med = {
        "n_races": len(metrics_rows),
        "pattern_rank": median([float(r["pattern_rank"]) for r in metrics_rows]),
        "pattern_rank_cutoff_25pct_median": median([float(r["rank_cutoff_25pct"]) for r in metrics_rows]),
        "uniform_ratio": median([float(r["uniform_ratio"]) for r in metrics_rows]),
        "top1_prob": median([float(r["top1_prob"]) for r in metrics_rows]),
        "relative_lift": median([float(r["relative_lift"]) for r in metrics_rows]),
    }
    rank_pass = med["pattern_rank"] is not None and med["pattern_rank_cutoff_25pct_median"] is not None and med["pattern_rank"] <= med["pattern_rank_cutoff_25pct_median"]
    summary = {
        "criteria": {
            "pattern_rank": "median rank <= median unique_pattern_count*0.25 cutoff",
            "uniform_ratio": ">= 50",
            "top1_prob": ">= 0.02",
            "relative_lift": ">= 0.20",
        },
        "median": med,
        "pass": {
            "pattern_rank": bool(rank_pass),
            "uniform_ratio": bool(med["uniform_ratio"] is not None and med["uniform_ratio"] >= 50),
            "top1_prob": bool(med["top1_prob"] is not None and med["top1_prob"] >= 0.02),
            "relative_lift": bool(med["relative_lift"] is not None and med["relative_lift"] >= 0.20),
        },
    }
    summary["all_four_pass"] = all(summary["pass"].values())
    (out_dir / "ver326_lap1_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Ver330-A validation runner")
    ap.add_argument("--db", required=True, help="検証対象のVer326 DB")
    ap.add_argument("--app", default="app.py", help="Ver330-A検証用 app.py")
    ap.add_argument("--phase", choices=["contamination", "lap1", "axis-a"], default="contamination")
    ap.add_argument("--trials", type=int, default=TRIALS_DEFAULT)
    ap.add_argument("--seed", type=int, default=SEED_DEFAULT)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--limit", type=int, default=0, help="Lap1のレース数を制限。0=全377R")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()
    if args.trials != 20000:
        raise SystemExit("事前登録基準では trials=20000 固定です。別値では実行しません。")
    db = Path(args.db).expanduser().resolve()
    app_path = Path(args.app).expanduser().resolve()
    if not db.exists(): raise SystemExit(f"DBがありません: {db}")
    if not app_path.exists(): raise SystemExit(f"app.pyがありません: {app_path}")
    base = Path(args.out_dir).expanduser().resolve() if args.out_dir else app_path.parent / "ver330_validation_results"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = base / f"Ver330A_validation_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)

    c = contamination_check(db)
    (out_dir / "contamination_check.json").write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"contamination_count={c['contamination_count']}")
    print(f"common_set_count={c['common_set_count']}")
    if c["contamination_count"]:
        print("汚染レース:")
        for k in c["contamination_race_keys"]:
            print("  " + k)
        return 2
    if args.phase == "contamination":
        return 0
    if args.phase == "lap1":
        # 全返還レースはLap1順位が存在しないため、検証対象から明示的に除外する。
        # contamination_check() の共通セットは378Rを返すが、Lap1評価は377Rで行う。
        common_set = [k for k in c["common_set"] if k not in EXCLUDED_NO_LAP1]
        if len(common_set) != EXPECTED_LAP1_COUNT:
            print(f"警告: 除外後 {len(common_set)}R（期待{EXPECTED_LAP1_COUNT}R）")
            return 3
        summary = run_lap1(db, app_path, out_dir, common_set[:args.limit or None], args.trials, args.seed, args.timeout)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["all_four_pass"] else 4
    if args.phase == "axis-a":
        compare = app_path.parent / "compare_ver326_ver330a.py"
        if not compare.exists():
            raise SystemExit(f"Axis A runnerがありません: {compare}")
        print("Axis Aは既存のcompare_ver326_ver330a.pyを実行してください。")
        print("要件: 同一snapshot / seed / trials、lap2-5 bit-identical、lap6のみ差")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

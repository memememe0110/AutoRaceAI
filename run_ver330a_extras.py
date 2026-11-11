#!/usr/bin/env python3
"""Ver331 Phase 1 extras worker: 3R control measurement (8-car, zero handicap spread).

Runs the fixed three-race control set using Ver326, 20,000 trials, seed 12345.
The source DB is read-only; results are checkpointed to a dedicated Git branch.
"""
from __future__ import annotations

import os

import argparse
import csv
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "verify_ver330_validation", HERE / "verify_ver330_validation.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load verify_ver330_validation.py")
verify = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify
spec.loader.exec_module(verify)

# Fixed control set. Do not edit during a registered run.
EXTRA_RACES = [
    # C案: Lap1完全 + 8車spread=0 の検証済み3Rのみ
    ("20261021_伊勢崎_12R", "伊勢崎", "2026-10-21"),  # 10m
    ("20260802_浜松_10R", "浜松", "2026-08-02"),      # 10m
    ("20260802_浜松_5R", "浜松", "2026-08-02"),       # 0m
]
EXPECTED_EXTRA_COUNT = 3
LAP1_CAR_COUNT = 8
LAP1_HANDICAP_SPREAD = 0
HANDICAP_TABLE = "result_entries"
HANDICAP_COLUMN = "handicap"
PHASE1_DB_SHA256 = "d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

FIELDS = [
    "race_key", "source", "venue", "race_date",
    "car_count", "actual_pattern", "unique_pattern_count", "pattern_rank",
    "pattern_count", "pattern_prob", "uniform_ratio", "top1_prob", "relative_lift",
    "observed_trials", "rank_cutoff_25pct", "rank_pass", "uniform_pass", "top1_pass",
    "relative_lift_pass", "sim_trials_reported", "ok", "error",
]


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def read_rows(path: Path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def git_checkpoint(
    db_root: Path, branch: str, progress_path: Path, csv_path: Path, message: str
):
    rel_progress = progress_path.relative_to(db_root).as_posix()
    rel_csv = csv_path.relative_to(db_root).as_posix()

    def run(args):
        proc = subprocess.run(
            args, cwd=db_root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        if proc.returncode:
            raise RuntimeError(
                f"Git checkpoint failed ({' '.join(args)}): {proc.stdout[-2500:]}"
            )
        return proc.stdout

    run(["git", "config", "user.name", "AutoRaceAI Validation Bot"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])
    run(["git", "add", "--", rel_progress, rel_csv])
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=db_root)
    if status.returncode == 0:
        return
    if status.returncode != 1:
        raise RuntimeError("git diff --cached failed")
    run(["git", "commit", "-m", message[:180]])
    run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"])


def _read_handicap_values(con, race_key):
    """Read all eight handicap values; fail on any missing/non-numeric/non-finite value."""
    rows = con.execute(
        f'SELECT "{HANDICAP_COLUMN}" '
        f'FROM "{HANDICAP_TABLE}" WHERE race_key=?',
        (race_key,),
    ).fetchall()

    if len(rows) != LAP1_CAR_COUNT:
        raise RuntimeError(
            f"{race_key}: handicap row count={len(rows)}, "
            f"expected={LAP1_CAR_COUNT}"
        )

    values = []
    for idx, (raw,) in enumerate(rows):
        if raw is None or not str(raw).strip():
            raise RuntimeError(f"{race_key}: handicap[{idx}] is empty")
        try:
            value = float(str(raw).strip())
        except ValueError as exc:
            raise RuntimeError(
                f"{race_key}: handicap[{idx}] is not numeric: {raw!r}"
            ) from exc
        if not math.isfinite(value):
            raise RuntimeError(
                f"{race_key}: handicap[{idx}] is not finite: {raw!r}"
            )
        values.append(value)
    return values


def verify_extra_targets(db: Path):
    """Fail before simulation unless all three races satisfy the control definition."""
    report = {}
    with verify.ro_connect(db) as con:
        prediction_table = verify.prediction_table(con)
        for race_key, venue, race_date in EXTRA_RACES:
            has_prediction = con.execute(
                f'SELECT 1 FROM "{prediction_table}" '
                "WHERE race_key=? AND app_version=? LIMIT 1",
                (race_key, "Ver326"),
            ).fetchone() is not None

            actual = verify.actual_lap_order(con, race_key)
            car_count = len(actual) if actual else 0
            handicaps = _read_handicap_values(con, race_key)
            handicap_spread = max(handicaps) - min(handicaps)

            report[race_key] = {
                "venue": venue,
                "race_date": race_date,
                "has_ver326_prediction": bool(has_prediction),
                "has_lap1_actual": bool(actual),
                "lap1_car_count": car_count,
                "handicap_values": handicaps,
                "handicap_spread": handicap_spread,
            }

    failures = []
    for race_key, item in report.items():
        problems = []
        if not item["has_ver326_prediction"]:
            problems.append("no Ver326 prediction")
        if not item["has_lap1_actual"]:
            problems.append("no Lap1 actual")
        if item["lap1_car_count"] != LAP1_CAR_COUNT:
            problems.append(
                f"lap1_car_count={item['lap1_car_count']} != {LAP1_CAR_COUNT}"
            )
        if item["handicap_spread"] != LAP1_HANDICAP_SPREAD:
            problems.append(
                f"handicap_spread={item['handicap_spread']} "
                f"!= {LAP1_HANDICAP_SPREAD}"
            )
        if problems:
            failures.append((race_key, problems))

    if failures:
        raise RuntimeError(f"Extra target validation failed: {failures}")
    return report


def summary_from_rows(rows):
    ok_rows = [
        row for row in rows
        if str(row.get("ok", "")).lower() in ("true", "1")
    ]

    def numbers(key):
        values = []
        for row in ok_rows:
            try:
                values.append(float(row[key]))
            except (KeyError, TypeError, ValueError):
                pass
        return values

    def median(values):
        return verify.median(values)

    rank_cutoff = median(numbers("rank_cutoff_25pct"))
    rank = median(numbers("pattern_rank"))
    metrics = {
        "n_races": len(ok_rows),
        "pattern_rank": rank,
        "pattern_rank_cutoff_25pct_median": rank_cutoff,
        "uniform_ratio": median(numbers("uniform_ratio")),
        "top1_prob": median(numbers("top1_prob")),
        "relative_lift": median(numbers("relative_lift")),
    }
    passes = {
        "pattern_rank": (
            rank is not None and rank_cutoff is not None and rank <= rank_cutoff
        ),
        "uniform_ratio": (
            metrics["uniform_ratio"] is not None
            and metrics["uniform_ratio"] >= 50
        ),
        "top1_prob": (
            metrics["top1_prob"] is not None and metrics["top1_prob"] >= 0.02
        ),
        "relative_lift": (
            metrics["relative_lift"] is not None
            and metrics["relative_lift"] >= 0.20
        ),
    }
    return {"median": metrics, "pass": passes, "all_four_pass": all(passes.values())}


def validate_run_id(run_id: str):
    if not RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
        raise SystemExit(
            "invalid run_id: use 1-80 characters from A-Z, a-z, 0-9, '.', '_' or '-' "
            "and do not include '..'"
        )


def main():
    # C案測定: worker 子プロセスへ伝播（mode=Ver331 と二重化）
    os.environ.setdefault("AUTORACE_331_LAP1_TUNE", "1")
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--db-root", required=True)
    parser.add_argument("--results-rel", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--max-races", type=int, default=3)
    parser.add_argument("--trials", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument(
        "--expected-db-sha256", default=PHASE1_DB_SHA256,
        help="Fail if DB hash differs from Phase 1 hash."
    )
    args = parser.parse_args()

    if args.trials != 20000 or args.seed != 12345:
        raise SystemExit("Pre-registered settings are fixed: trials=20000, seed=12345")
    if not 1 <= args.max_races <= EXPECTED_EXTRA_COUNT:
        raise SystemExit(f"max-races must be 1..{EXPECTED_EXTRA_COUNT}")
    validate_run_id(args.run_id)

    db = Path(args.db).resolve()
    app = Path(args.app).resolve()
    db_root = Path(args.db_root).resolve()
    if not db.is_file():
        raise SystemExit(f"DB file not found: {db}")
    if not app.is_file():
        raise SystemExit(f"App file not found: {app}")
    if not db_root.is_dir():
        raise SystemExit(f"DB root is not a directory: {db_root}")

    actual_sha = _file_sha256(db)
    if args.expected_db_sha256 and actual_sha != args.expected_db_sha256:
        raise SystemExit(
            f"db_sha256 mismatch: {actual_sha} != {args.expected_db_sha256}"
        )

    results_rel = Path(args.results_rel)
    if results_rel.is_absolute() or ".." in results_rel.parts:
        raise SystemExit(f"invalid results-rel: {args.results_rel!r}")

    result_dir = (db_root / results_rel / args.run_id).resolve()
    expected_parent = (db_root / results_rel).resolve()
    if expected_parent not in result_dir.parents:
        raise SystemExit(f"run-id escapes results-rel: {result_dir}")

    # Verify all target races before writing checkpoints or launching simulations.
    target_report = verify_extra_targets(db)

    result_dir.mkdir(parents=True, exist_ok=True)
    csv_path = result_dir / "results.csv"
    progress_path = result_dir / "progress.json"
    progress = read_json(progress_path, {})
    rows = read_rows(csv_path)

    done = {
        str(row.get("race_key"))
        for row in rows
        if str(row.get("ok", "")).lower() in ("true", "1")
    }
    all_keys = [race_key for race_key, _, _ in EXTRA_RACES]
    started = str(progress.get("started_at") or now_iso())
    batch_started_at = now_iso()
    remaining = [key for key in all_keys if key not in done]

    progress.update({
        "run_id": args.run_id,
        "phase": "Ver331 gapC extras (3R control)",
        "status": "running",
        "total": EXPECTED_EXTRA_COUNT,
        "done_count": len(done),
        "failed_count": sum(
            1 for row in rows
            if str(row.get("ok", "")).lower() not in ("true", "1")
        ),
        "current_race": "",
        "started_at": started,
        "updated_at": now_iso(),
        "batch_started_at": batch_started_at,
        "last_error": "",
        "trials": args.trials,
        "seed": args.seed,
        "batch_limit": args.max_races,
        "db_sha256": actual_sha,
        "target_check": target_report,
    })
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_rows(csv_path, rows)
    git_checkpoint(
        db_root, args.branch, progress_path, csv_path,
        f"Ver331 extras {args.run_id}: worker started"
    )

    batch = remaining[:args.max_races]
    done_at_batch_start = len(done)
    metadata = {
        race_key: (venue, race_date)
        for race_key, venue, race_date in EXTRA_RACES
    }

    for race_key in batch:
        venue, race_date = metadata[race_key]
        progress.update({
            "current_race": race_key,
            "updated_at": now_iso(),
            "done_count": len(done),
        })
        progress_path.write_text(
            json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        git_checkpoint(
            db_root, args.branch, progress_path, csv_path,
            f"Ver331 extras {args.run_id}: {len(done)}/{EXPECTED_EXTRA_COUNT} start {race_key}"
        )

        out_json = result_dir / "runs" / f"{race_key}.json"
        try:
            # C案測定は mode=Ver331（compare経路で _V331_LAP1_TUNE=True）
            # Ver326 のままだと compare が TUNE を強制OFFするため無効になる
            data = verify.run_one_race(
                app, db, race_key, "Ver331",
                args.trials, args.seed, out_json, args.timeout
            )
            if not data.get("validation_full_trials"):
                raise RuntimeError("validation_full_trials is false")
            if int(data.get("sim_trials") or 0) != args.trials:
                raise RuntimeError(
                    f"sim_trials mismatch: {data.get('sim_trials')} != {args.trials}"
                )

            with verify.ro_connect(db) as con:
                actual = verify.actual_lap_order(con, race_key)
            if not actual or len(actual) != LAP1_CAR_COUNT:
                raise RuntimeError(
                    f"actual Lap1 order invalid: count={len(actual) if actual else 0}"
                )

            metrics = verify.pattern_metrics(
                data.get("lap_order_counts", {}).get("1", {}),
                actual,
                args.trials,
            )
            row = {
                **metrics,
                "race_key": race_key,
                "source": "extra",
                "venue": venue,
                "race_date": race_date,
                "car_count": len(actual),
                "sim_trials_reported": int(data.get("sim_trials") or 0),
                "ok": True,
                "error": "",
            }
        except Exception as exc:
            row = {
                "race_key": race_key,
                "source": "extra",
                "venue": venue,
                "race_date": race_date,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            rows = [
                existing for existing in rows
                if str(existing.get("race_key")) != race_key
            ] + [row]
            write_rows(csv_path, rows)
            progress.update({
                "status": "failed",
                "current_race": race_key,
                "done_count": len(done),
                "failed_count": 1,
                "last_error": row["error"],
                "updated_at": now_iso(),
            })
            progress_path.write_text(
                json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            git_checkpoint(
                db_root, args.branch, progress_path, csv_path,
                f"Ver331 extras {args.run_id}: failed {race_key}"
            )
            raise

        rows = [
            existing for existing in rows
            if str(existing.get("race_key")) != race_key
        ] + [row]
        done.add(race_key)
        write_rows(csv_path, rows)

        elapsed = max(
            1,
            (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(batch_started_at.replace("Z", "+00:00"))
            ).total_seconds(),
        )
        batch_done = max(1, len(done) - done_at_batch_start)
        average = elapsed / batch_done
        progress.update({
            "status": "running",
            "done_count": len(done),
            "failed_count": 0,
            "current_race": "",
            "last_completed_race": race_key,
            "updated_at": now_iso(),
            "last_error": "",
            "elapsed_seconds": int(elapsed),
            "estimated_remaining_seconds": int(
                max(0, EXPECTED_EXTRA_COUNT - len(done)) * average
            ),
            "summary_so_far": summary_from_rows(rows),
        })
        progress_path.write_text(
            json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        git_checkpoint(
            db_root, args.branch, progress_path, csv_path,
            f"Ver331 extras {args.run_id}: {len(done)}/{EXPECTED_EXTRA_COUNT} {race_key}"
        )
        print(f"CHECKPOINT {len(done)}/{EXPECTED_EXTRA_COUNT} {race_key}", flush=True)

    if len(done) == EXPECTED_EXTRA_COUNT:
        progress.update({
            "status": "completed",
            "done_count": len(done),
            "current_race": "",
            "finished_at": now_iso(),
            "updated_at": now_iso(),
            "final_summary": summary_from_rows(rows),
        })
    else:
        progress.update({
            "status": "running",
            "done_count": len(done),
            "current_race": "",
            "updated_at": now_iso(),
        })

    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_rows(csv_path, rows)
    git_checkpoint(
        db_root, args.branch, progress_path, csv_path,
        f"Ver331 extras {args.run_id}: batch complete {len(done)}/{EXPECTED_EXTRA_COUNT}"
    )
    print(json.dumps(progress, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

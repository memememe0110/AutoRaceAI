#!/usr/bin/env python3
"""Read-only duplicate/reproducibility audit for v231_prediction_history."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


REQUIRED_COLUMNS = {
    "id", "race_key", "app_version", "trials", "seed", "raw_text", "prediction_time"
}
OPTIONAL_COLUMNS = ("settings_hash",)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_input_sha(raw_text) -> str | None:
    raw = str(raw_text or "")
    if not raw.strip():
        return None
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def classify_group(rows: list[dict]) -> str:
    seeds = {str(row["seed"]) for row in rows}
    trials = {str(row["trials"]) for row in rows}
    if len(seeds) != 1 or len(trials) != 1:
        return "execution_conditions_diff"
    hashes = [row["input_sha256"] for row in rows]
    if any(value is None for value in hashes):
        return "same_seed_same_trials_input_unknown"
    if len(set(hashes)) == 1:
        return "same_seed_same_trials_and_input_sha"
    return "same_seed_same_trials_input_diff"


def parse_prediction_time(value):
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        return ("numeric", float(raw))
    except ValueError:
        pass
    try:
        normalized = raw.replace("Z", "+00:00")
        return ("datetime", datetime.fromisoformat(normalized).timestamp())
    except (ValueError, OverflowError):
        return None


def prediction_time_gap_seconds(rows: list[dict]) -> float | None:
    parsed = [parse_prediction_time(row["prediction_time"]) for row in rows]
    if len(parsed) < 2 or any(item is None for item in parsed):
        return None
    kinds = {item[0] for item in parsed}
    if len(kinds) != 1:
        return None
    values = [item[1] for item in parsed]
    return max(values) - min(values)


def audit_database(db_path: Path, db_info_path: Path, expected_generation: str, outdir: Path) -> dict:
    db_path = db_path.resolve()
    before_sha = sha256_file(db_path)
    info = json.loads(db_info_path.read_text(encoding="utf-8"))
    if str(info.get("manifest_generation")) != str(expected_generation):
        raise SystemExit(f"GENERATION_MISMATCH: expected={expected_generation} actual={info.get('manifest_generation')}")
    if not info.get("commit_sha"):
        raise SystemExit("DB_COMMIT_SHA_MISSING")
    if info.get("db_sha256") != before_sha:
        raise SystemExit(f"DB_SHA_MISMATCH: manifest={info.get('db_sha256')} actual={before_sha}")

    uri = "file:" + quote(db_path.as_posix(), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise SystemExit(f"INTEGRITY_CHECK_FAILED: {integrity}")

        cols = {row[1] for row in con.execute("PRAGMA table_info(v231_prediction_history)")}
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise SystemExit(f"REQUIRED_COLUMNS_MISSING: {sorted(missing)}")

        optional = [c for c in ("settings_hash",) if c in cols]
        required = REQUIRED_COLUMNS
        selected = sorted(required) + optional
        selected_sql = ", ".join('"' + name + '"' for name in selected)
        rows = [
            dict(row)
            for row in con.execute(
                f"SELECT {selected_sql} FROM v231_prediction_history ORDER BY race_key, id"
            )
        ]
        schema_columns = sorted(cols)
    finally:
        con.close()

    grouped = defaultdict(list)
    for row in rows:
        row["input_sha256"] = row_input_sha(row["raw_text"])
        grouped[str(row["race_key"])].append(row)
    duplicate_groups = {key: value for key, value in grouped.items() if len(value) > 1}
    class_counts = Counter()
    csv_rows = []
    row_count_distribution = Counter()
    for race_key, group in sorted(duplicate_groups.items()):
        classification = classify_group(group)
        class_counts[classification] += 1
        row_count_distribution[str(len(group))] += 1
        gap = prediction_time_gap_seconds(group)
        for row in group:
            csv_rows.append({
                "race_key": race_key,
                "row_id": row["id"],
                "app_version": row["app_version"],
                "seed": row["seed"],
                "trials": row["trials"],
                "prediction_time": row["prediction_time"],
                "prediction_time_gap_seconds": gap,
                "input_sha256": row["input_sha256"] or "",
                "classification": classification,
                "settings_hash": row.get("settings_hash", ""),
            })

    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "duplicates.csv"
    fieldnames = [
        "race_key", "row_id", "app_version", "seed", "trials", "prediction_time",
        "prediction_time_gap_seconds", "input_sha256", "classification", "settings_hash",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    summary = {
        "db_source": info.get("db_source"),
        "manifest_generation": info.get("manifest_generation"),
        "commit_sha": info.get("commit_sha"),
        "db_sha256": before_sha,
        "integrity_check": integrity,
        "schema_columns": schema_columns,
        "selected_columns": selected,
        "total_history_rows": len(rows),
        "duplicate_race_count": len(duplicate_groups),
        "duplicate_row_count": sum(len(group) for group in duplicate_groups.values()),
        "classification_group_counts": dict(sorted(class_counts.items())),
        "duplicate_row_count_distribution": dict(sorted(row_count_distribution.items(), key=lambda item: int(item[0]))),
        "raw_text_missing_rows_in_duplicates": sum(
            1 for group in duplicate_groups.values() for row in group if row["input_sha256"] is None
        ),
        "interpretation": {
            "same_seed_same_trials_and_input_sha": "この固定DB世代内で同一条件・同一入力の重複保存候補。0件ならこの世代では検出されず。",
            "same_seed_same_trials_input_diff": "seed/trialsは同一だが入力カードが異なる再実行。prediction_time_gap等で意図を確認。",
            "same_seed_same_trials_input_unknown": "raw_text欠損等で入力同一性を判定できない。",
            "execution_conditions_diff": "seedまたはtrialsが異なる再実行。条件差のある実行として扱い、直ちに破損とはしない。",
        },
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    after_sha = sha256_file(db_path)
    if after_sha != before_sha:
        raise SystemExit(f"READ_ONLY_HASH_CHANGED: before={before_sha} after={after_sha}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--db-info", required=True, type=Path)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--outdir", required=True, type=Path)
    args = parser.parse_args()
    result = audit_database(args.db, args.db_info, args.expected_generation, args.outdir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

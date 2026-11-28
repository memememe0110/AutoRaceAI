from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TABLES = ("result_races", "result_entries", "result_laps", "result_payouts")


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    return value


def _read_review_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def select_review_races(audit_dir: Path) -> list[dict[str, Any]]:
    """Select conservative flags, excluding rows explicitly marked as void."""
    merged: dict[str, dict[str, Any]] = {}
    for row in _read_review_rows(audit_dir / "result_audit_goal_line_review.csv"):
        key = (row.get("race_key") or "").strip()
        if key:
            merged[key] = {**row, "review_reason": row.get("incomplete_reason") or "incomplete"}

    for row in _read_review_rows(audit_dir / "result_audit_zero_count_review.csv"):
        key = (row.get("race_key") or "").strip()
        if not key:
            continue
        if (row.get("race_status") or "").strip() == "不成立":
            continue
        merged.setdefault(key, {**row, "review_reason": "zero_count_status_unclassified"})
    return [merged[key] for key in sorted(merged)]


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({_ident(table)})")]


def _first_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in columns), None)


def _car_set(rows: list[dict[str, Any]], car_col: str | None) -> set[str]:
    if not car_col:
        return set()
    return {
        str(row[car_col]) for row in rows
        if row.get(car_col) is not None and str(row[car_col]).strip() != ""
    }


def _sorted_cars(values: set[str]) -> list[str]:
    return sorted(values, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_audit(db_path: str | Path, audit_dir: str | Path,
              db_info_path: str | Path | None = None) -> dict[str, Any]:
    db = Path(db_path).resolve()
    out = Path(audit_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    review = select_review_races(out)
    keys = [str(row["race_key"]) for row in review]
    review_by_key = {str(row["race_key"]): row for row in review}
    db_info = (
        json.loads(Path(db_info_path).read_text(encoding="utf-8"))
        if db_info_path and Path(db_info_path).exists() else {}
    )

    flagged_rows: list[dict[str, Any]] = []
    lap_group_rows: list[dict[str, Any]] = []
    race_classification_rows: list[dict[str, Any]] = []
    table_row_counts: Counter[str] = Counter()
    rows_by_table: dict[str, list[dict[str, Any]]] = {}

    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
        con.execute("PRAGMA query_only=ON")
        if int(con.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("PRAGMA query_only did not take effect")

        available_tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_tables = sorted(set(TABLES) - available_tables)
        if missing_tables:
            raise RuntimeError("Missing required result tables: " + ", ".join(missing_tables))

        columns_by_table = {table: _table_columns(con, table) for table in TABLES}
        for table, columns in columns_by_table.items():
            if "race_key" not in columns:
                raise RuntimeError(f"{table} has no race_key column")

        for table in TABLES:
            columns = columns_by_table[table]
            query = (
                f"SELECT * FROM {_ident(table)} "
                f"WHERE race_key IN ({','.join('?' for _ in keys)})"
                if keys else f"SELECT * FROM {_ident(table)} WHERE 0"
            )
            raw = con.execute(query, keys).fetchall()
            rows = [
                {columns[i]: _json_value(value) for i, value in enumerate(values)}
                for values in raw
            ]
            rows.sort(key=lambda item: (
                str(item.get("race_key", "")),
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
            rows_by_table[table] = rows
            for index, item in enumerate(rows, start=1):
                flagged_rows.append({
                    "race_key": item.get("race_key"),
                    "source_table": table,
                    "row_number_in_table_export": index,
                    "row_json": json.dumps(
                        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                })
            table_row_counts[table] = len(rows)

        lap_columns = columns_by_table["result_laps"]
        entry_columns = columns_by_table["result_entries"]
        label_col = _first_column(lap_columns, ("lap_label", "周回"))
        lap_no_col = _first_column(lap_columns, ("lap_no",))
        lap_car_col = _first_column(lap_columns, ("car_no", "車番"))
        entry_car_col = _first_column(entry_columns, ("car_no", "車番"))
        finish_col = _first_column(entry_columns, ("finish", "着順"))
        result_status_col = _first_column(
            entry_columns, ("result_status", "結果区分", "事故情報")
        )

        entries_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows_by_table["result_entries"]:
            entries_by_key[str(row.get("race_key"))].append(row)
        laps_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows_by_table["result_laps"]:
            laps_by_key[str(row.get("race_key"))].append(row)
        races_by_key = {
            str(row.get("race_key")): row for row in rows_by_table["result_races"]
        }

        if label_col and lap_no_col and lap_car_col:
            for key in keys:
                race = review_by_key[key]
                entries = entries_by_key.get(key, [])
                laps = laps_by_key.get(key, [])
                entry_cars = _car_set(entries, entry_car_col)
                finishers = {
                    str(row[entry_car_col]) for row in entries
                    if entry_car_col and finish_col and row.get(entry_car_col) is not None
                    and row.get(finish_col) is not None
                }
                raw_groups: dict[tuple[Any, Any], list[Any]] = defaultdict(list)
                for row in laps:
                    raw_groups[(row.get(label_col), row.get(lap_no_col))].append(
                        row.get(lap_car_col)
                    )

                for (label, lap_no), cars in sorted(
                    raw_groups.items(),
                    key=lambda item: (
                        "" if item[0][0] is None else str(item[0][0]),
                        -1 if item[0][1] is None else int(item[0][1]),
                    ),
                ):
                    normalized = [str(car) for car in cars if car is not None]
                    counts = Counter(normalized)
                    unique_cars = set(normalized)
                    missing_entries = _sorted_cars(entry_cars - unique_cars)
                    missing_finishers = _sorted_cars(finishers - unique_cars)
                    extra_entries = _sorted_cars(unique_cars - entry_cars)
                    duplicates = _sorted_cars({
                        car for car, count in counts.items() if count > 1
                    })
                    lap_group_rows.append({
                        "race_key": key,
                        "review_reason": race.get("review_reason"),
                        "race_status": race.get("race_status"),
                        "lap_label": label,
                        "lap_no": lap_no,
                        "row_count": len(cars),
                        "distinct_car_count": len(unique_cars),
                        "expected_entry_count": len(entry_cars),
                        "expected_entry_car_nos_json": json.dumps(
                            _sorted_cars(entry_cars), ensure_ascii=False
                        ),
                        "expected_finisher_count": len(finishers) if finish_col else None,
                        "expected_finisher_car_nos_json": (
                            json.dumps(_sorted_cars(finishers), ensure_ascii=False)
                            if finish_col else None
                        ),
                        "actual_car_nos_json": json.dumps(
                            _sorted_cars(unique_cars), ensure_ascii=False
                        ),
                        "missing_entry_car_nos_json": json.dumps(
                            missing_entries, ensure_ascii=False
                        ),
                        "missing_finisher_car_nos_json": (
                            json.dumps(missing_finishers, ensure_ascii=False)
                            if finish_col else None
                        ),
                        "extra_entry_car_nos_json": json.dumps(
                            extra_entries, ensure_ascii=False
                        ),
                        "duplicate_car_nos_json": json.dumps(
                            duplicates, ensure_ascii=False
                        ),
                    })

                race_row = races_by_key.get(key, {})
                nonfinishers = [
                    row for row in entries
                    if finish_col and row.get(finish_col) is None
                ]
                nonfinisher_status_review = []
                for row in nonfinishers:
                    status = row.get(result_status_col) if result_status_col else None
                    normalized_status = "" if status is None else str(status).strip()
                    if normalized_status in ("", "None", "通常"):
                        nonfinisher_status_review.append(
                            str(row.get(entry_car_col)) if entry_car_col else ""
                        )

                goal_rows = [
                    row for row in laps
                    if str(row.get(label_col) or "").strip() == "ゴール線"
                ]
                goal_cars = [
                    str(row[lap_car_col]) for row in goal_rows
                    if row.get(lap_car_col) is not None
                ]
                goal_counts = Counter(goal_cars)
                goal_set = set(goal_cars)
                missing_goal_finishers = (
                    _sorted_cars(finishers - goal_set) if finish_col else None
                )
                # Goal-line passage can include a non-finisher disqualified after crossing
                # (e.g. 反妨). Only car numbers absent from result_entries are unknown extras.
                extra_goal_cars = (
                    _sorted_cars(goal_set - entry_cars) if entry_car_col else None
                )
                goal_line_nonfinishers = (
                    _sorted_cars(goal_set - finishers) if finish_col else None
                )
                duplicate_goal_cars = _sorted_cars({
                    car for car, count in goal_counts.items() if count > 1
                })

                non_goal_groups: dict[Any, list[str]] = defaultdict(list)
                for row in laps:
                    if str(row.get(label_col) or "").strip() == "ゴール線":
                        continue
                    non_goal_groups[row.get(lap_no_col)].append(
                        str(row[lap_car_col]) if row.get(lap_car_col) is not None else ""
                    )
                bad_lap_groups = []
                for lap_no, cars in non_goal_groups.items():
                    car_counts = Counter(car for car in cars if car)
                    car_set = set(car_counts)
                    missing = _sorted_cars(finishers - car_set) if finish_col else []
                    extra = _sorted_cars(car_set - entry_cars)
                    duplicates = _sorted_cars({
                        car for car, count in car_counts.items() if count > 1
                    })
                    if missing or extra or duplicates:
                        bad_lap_groups.append({
                            "lap_no": lap_no,
                            "missing_finishers": missing,
                            "extra_entry_car_nos": extra,
                            "duplicate_car_nos": duplicates,
                        })

                if not entries:
                    classification = (
                        "void_race" if str(race_row.get("race_status") or "").strip() == "不成立"
                        else "entries_empty_status_unknown"
                    )
                elif not laps:
                    classification = "laps_empty"
                elif not finish_col:
                    classification = "finisher_column_unavailable"
                elif missing_goal_finishers or extra_goal_cars or duplicate_goal_cars:
                    classification = "goal_line_finisher_mismatch"
                elif bad_lap_groups:
                    classification = "lap_finisher_mismatch"
                else:
                    classification = "lap_data_matches_finishers"

                race_classification_rows.append({
                    "race_key": key,
                    "review_reason": race.get("review_reason"),
                    "race_status": race_row.get("race_status"),
                    "entry_count": len(entries),
                    "finisher_count": len(finishers) if finish_col else None,
                    "nonfinisher_count": len(nonfinishers) if finish_col else None,
                    "nonfinisher_car_nos_json": json.dumps(
                        _sorted_cars(_car_set(nonfinishers, entry_car_col)),
                        ensure_ascii=False,
                    ),
                    "nonfinisher_status_review_car_nos_json": json.dumps(
                        _sorted_cars(set(nonfinisher_status_review)),
                        ensure_ascii=False,
                    ),
                    "goal_line_row_count": len(goal_rows),
                    "goal_line_distinct_car_count": len(goal_set),
                    "goal_line_missing_finisher_car_nos_json": (
                        json.dumps(missing_goal_finishers, ensure_ascii=False)
                        if missing_goal_finishers is not None else None
                    ),
                    "goal_line_extra_car_nos_json": (
                        json.dumps(extra_goal_cars, ensure_ascii=False)
                        if extra_goal_cars is not None else None
                    ),
                    "goal_line_nonfinisher_car_nos_json": (
                        json.dumps(goal_line_nonfinishers, ensure_ascii=False)
                        if goal_line_nonfinishers is not None else None
                    ),
                    "goal_line_duplicate_car_nos_json": json.dumps(
                        duplicate_goal_cars, ensure_ascii=False
                    ),
                    "non_goal_lap_group_count": len(non_goal_groups),
                    "non_goal_lap_groups_with_finisher_mismatch": len(bad_lap_groups),
                    "bad_lap_group_details_json": json.dumps(
                        bad_lap_groups, ensure_ascii=False, sort_keys=True
                    ),
                    "learning_eligible": race_row.get("learning_eligible"),
                    "learning_exclusion_reason": race_row.get("learning_exclusion_reason"),
                    "model_eligible": race_row.get("model_eligible"),
                    "model_exclusion_reason": race_row.get("model_exclusion_reason"),
                    "finisher_based_classification": classification,
                })

    _write_csv(
        out / "result_registration_flagged_rows.csv",
        ["race_key", "source_table", "row_number_in_table_export", "row_json"],
        flagged_rows,
    )
    _write_csv(
        out / "result_registration_lap_group_detail.csv",
        [
            "race_key", "review_reason", "race_status", "lap_label", "lap_no",
            "row_count", "distinct_car_count", "expected_entry_count",
            "expected_entry_car_nos_json", "expected_finisher_count",
            "expected_finisher_car_nos_json", "actual_car_nos_json",
            "missing_entry_car_nos_json", "missing_finisher_car_nos_json",
            "extra_entry_car_nos_json", "duplicate_car_nos_json",
        ],
        lap_group_rows,
    )
    _write_csv(
        out / "result_registration_race_classification.csv",
        [
            "race_key", "review_reason", "race_status", "entry_count",
            "finisher_count", "nonfinisher_count", "nonfinisher_car_nos_json",
            "nonfinisher_status_review_car_nos_json", "goal_line_row_count",
            "goal_line_distinct_car_count", "goal_line_missing_finisher_car_nos_json",
            "goal_line_extra_car_nos_json", "goal_line_nonfinisher_car_nos_json",
            "goal_line_duplicate_car_nos_json", "non_goal_lap_group_count", "non_goal_lap_groups_with_finisher_mismatch",
            "bad_lap_group_details_json", "learning_eligible",
            "learning_exclusion_reason", "model_eligible", "model_exclusion_reason",
            "finisher_based_classification",
        ],
        race_classification_rows,
    )

    classification_counts = Counter(
        row["finisher_based_classification"] for row in race_classification_rows
    )
    summary = {
        "db_source": db_info.get("db_source"),
        "db_commit_sha": db_info.get("commit_sha"),
        "db_generation": db_info.get("manifest_generation"),
        "db_manifest_saved_at": db_info.get("manifest_saved_at"),
        "db_sha256": db_info.get("db_sha256"),
        "review_race_count": len(review),
        "review_race_keys": keys,
        "flagged_row_count_by_table": dict(sorted(table_row_counts.items())),
        "lap_group_detail_rows": len(lap_group_rows),
        "lap_detail_available": bool(label_col and lap_no_col and lap_car_col),
        "finisher_column": finish_col,
        "finisher_based_classification_counts": dict(sorted(classification_counts.items())),
        "nonfinisher_status_review_race_count": sum(
            bool(row["nonfinisher_status_review_car_nos_json"] != "[]")
            for row in race_classification_rows
        ),
        "query_only": True,
        "database_modified": False,
        "database_in_artifact": False,
        "notes": [
            "The original workflow flags are conservative review candidates based on entrant counts.",
            "Goal-line completeness is separately checked against result_entries.finish IS NOT NULL.",
            "A non-finisher may legitimately disappear from later lap groups; only missing finishers are treated as lap-group mismatches.",
            "A non-finisher with blank, literal None, or 通常 result_status is separately flagged for source-page review.",
            "This is a read-only structural audit, not proof of parser defect or source-page truth.",
            "Only incomplete races and zero-count rows without explicit race_status='不成立' are expanded.",
            "Rows are exported as JSON inside CSV; SQLite BLOB values are represented as hexadecimal.",
        ],
    }
    (out / "result_registration_detail_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--db-info")
    args = parser.parse_args()
    print(json.dumps(
        run_audit(args.db, args.audit_dir, args.db_info),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import hashlib
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
    """Select incomplete races plus zero-count rows not explicitly marked void."""
    merged: dict[str, dict[str, Any]] = {}

    for row in _read_review_rows(audit_dir / "result_audit_goal_line_review.csv"):
        key = (row.get("race_key") or "").strip()
        if key:
            merged[key] = {**row, "review_reason": row.get("incomplete_reason") or "incomplete"}

    for row in _read_review_rows(audit_dir / "result_audit_zero_count_review.csv"):
        key = (row.get("race_key") or "").strip()
        if not key:
            continue
        status = (row.get("race_status") or "").strip()
        if status == "不成立":
            continue
        merged.setdefault(key, {**row, "review_reason": "zero_count_status_unclassified"})

    return [merged[key] for key in sorted(merged)]


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({_ident(table)})")]


def _first_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in columns), None)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_audit(db_path: str | Path, audit_dir: str | Path,
              db_info_path: str | Path | None = None) -> dict[str, Any]:
    db = Path(db_path).resolve()
    out = Path(audit_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    review = select_review_races(out)
    keys = [str(row["race_key"]) for row in review]
    metadata = {str(row["race_key"]): row for row in review}

    if db_info_path and Path(db_info_path).exists():
        db_info = json.loads(Path(db_info_path).read_text(encoding="utf-8"))
    else:
        db_info = {}

    uri = db.as_uri() + "?mode=ro"
    flagged_rows: list[dict[str, Any]] = []
    lap_group_rows: list[dict[str, Any]] = []
    table_row_counts: Counter[str] = Counter()

    with sqlite3.connect(uri, uri=True) as con:
        con.execute("PRAGMA query_only=ON")
        query_only = int(con.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
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

        if label_col and lap_no_col and lap_car_col:
            for race in review:
                key = str(race["race_key"])
                expected_cars: set[str] | None = None
                if entry_car_col:
                    expected_cars = {
                        str(row[0]) for row in con.execute(
                            f"SELECT {_ident(entry_car_col)} FROM result_entries "
                            "WHERE race_key=? AND " + _ident(entry_car_col) + " IS NOT NULL",
                            (key,),
                        )
                    }

                raw_groups = con.execute(
                    f"SELECT {_ident(label_col)}, {_ident(lap_no_col)}, "
                    f"{_ident(lap_car_col)} FROM result_laps WHERE race_key=?",
                    (key,),
                ).fetchall()
                grouped: dict[tuple[Any, Any], list[Any]] = defaultdict(list)
                for label, lap_no, car_no in raw_groups:
                    grouped[(label, lap_no)].append(car_no)

                for (label, lap_no), cars in sorted(
                    grouped.items(), key=lambda item: (
                        "" if item[0][0] is None else str(item[0][0]),
                        -1 if item[0][1] is None else int(item[0][1]),
                    )
                ):
                    normalized = [str(car) for car in cars if car is not None]
                    counts = Counter(normalized)
                    unique_cars = set(normalized)
                    missing = (
                        sorted(expected_cars - unique_cars, key=lambda x: (not x.isdigit(), x))
                        if expected_cars is not None else None
                    )
                    extra = (
                        sorted(unique_cars - expected_cars, key=lambda x: (not x.isdigit(), x))
                        if expected_cars is not None else None
                    )
                    duplicates = sorted(
                        [car for car, count in counts.items() if count > 1],
                        key=lambda x: (not x.isdigit(), x),
                    )
                    lap_group_rows.append({
                        "race_key": key,
                        "review_reason": race.get("review_reason"),
                        "race_status": race.get("race_status"),
                        "lap_label": label,
                        "lap_no": lap_no,
                        "row_count": len(cars),
                        "distinct_car_count": len(unique_cars),
                        "expected_entry_count": race.get("n_entries"),
                        "expected_car_nos_json": (
                            json.dumps(sorted(expected_cars), ensure_ascii=False)
                            if expected_cars is not None else None
                        ),
                        "actual_car_nos_json": json.dumps(
                            sorted(normalized), ensure_ascii=False
                        ),
                        "missing_car_nos_json": (
                            json.dumps(missing, ensure_ascii=False)
                            if missing is not None else None
                        ),
                        "extra_car_nos_json": (
                            json.dumps(extra, ensure_ascii=False)
                            if extra is not None else None
                        ),
                        "duplicate_car_nos_json": json.dumps(
                            duplicates, ensure_ascii=False
                        ),
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
            "expected_car_nos_json", "actual_car_nos_json", "missing_car_nos_json",
            "extra_car_nos_json", "duplicate_car_nos_json",
        ],
        lap_group_rows,
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
        "query_only": True,
        "database_modified": False,
        "database_in_artifact": False,
        "notes": [
            "Only incomplete races and zero-count rows without explicit race_status='不成立' are expanded.",
            "Rows are exported as JSON inside CSV; SQLite BLOB values are represented as hexadecimal.",
            "Lap group differences compare recorded car numbers with result_entries car numbers.",
            "This is a read-only structural audit, not a judgment that every missing lap row is a parser defect.",
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

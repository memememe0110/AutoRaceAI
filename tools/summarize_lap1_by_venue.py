#!/usr/bin/env python3
"""Summarize completed Ver326 Lap1 validation results by venue.

This is a read-only post-processing tool. It does not run predictions or
modify the validation branch. Given the validation runner's results.csv, it
writes venue_summary.csv, outliers.csv, and summary.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable

METRICS = ("pattern_rank", "uniform_ratio", "top1_prob", "relative_lift")
PASS_COLUMNS = {
    "pattern_rank": "rank_pass",
    "uniform_ratio": "uniform_pass",
    "top1_prob": "top1_pass",
    "relative_lift": "relative_lift_pass",
}


def as_float(row: dict[str, str], key: str) -> float | None:
    try:
        value = row.get(key, "")
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def as_bool(row: dict[str, str], key: str, metric: str) -> bool | None:
    value = str(row.get(key, "")).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    number = as_float(row, metric)
    if number is None:
        return None
    if metric == "pattern_rank":
        cutoff = as_float(row, "rank_cutoff_25pct")
        return cutoff is not None and number <= cutoff
    return number >= {"uniform_ratio": 50.0, "top1_prob": 0.02, "relative_lift": 0.20}[metric]


def venue_of(race_key: str) -> str:
    parts = str(race_key).split("_", 2)
    return parts[1] if len(parts) >= 3 else "(unknown)"


def median(values: Iterable[float]) -> float | None:
    values = list(values)
    return float(statistics.median(values)) if values else None


def mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return float(statistics.mean(values)) if values else None


def rate(good: int, total: int) -> float | None:
    return good / total if total else None


def metric_summary(rows: list[dict[str, str]], metric: str) -> dict[str, float | int | None]:
    values = [(as_float(row, metric), row) for row in rows]
    values = [(value, row) for value, row in values if value is not None]
    passed = sum(1 for row in rows if as_bool(row, PASS_COLUMNS[metric], metric) is True)
    nums = [value for value, _ in values]
    return {
        "n": len(nums),
        "median": median(nums),
        "mean": mean(nums),
        "min": min(nums) if nums else None,
        "max": max(nums) if nums else None,
        "pass_count": passed,
        "pass_rate": rate(passed, len(rows)),
    }


def outlier_rows(rows: list[dict[str, str]], limit: int) -> list[dict[str, str | float]]:
    output: list[dict[str, str | float]] = []
    for metric in METRICS:
        values = [(as_float(row, metric), row) for row in rows]
        values = [(value, row) for value, row in values if value is not None]
        for direction, selected in (("low", sorted(values, key=lambda x: x[0])[:limit]),
                                    ("high", sorted(values, key=lambda x: x[0], reverse=True)[:limit])):
            for rank, (value, row) in enumerate(selected, 1):
                output.append({
                    "metric": metric,
                    "direction": direction,
                    "outlier_rank": rank,
                    "value": value,
                    "race_key": row.get("race_key", ""),
                    "venue": venue_of(row.get("race_key", "")),
                    "actual_pattern": row.get("actual_pattern", ""),
                    "pattern_rank": row.get("pattern_rank", ""),
                    "rank_cutoff_25pct": row.get("rank_cutoff_25pct", ""),
                    "uniform_ratio": row.get("uniform_ratio", ""),
                    "top1_prob": row.get("top1_prob", ""),
                    "relative_lift": row.get("relative_lift", ""),
                    "ok": row.get("ok", ""),
                })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="validation runner results.csv")
    parser.add_argument("--out-dir", type=Path, default=None, help="output directory; default: CSV parent")
    parser.add_argument("--expected-count", type=int, default=377)
    parser.add_argument("--outlier-limit", type=int, default=10)
    args = parser.parse_args()
    if args.outlier_limit < 1:
        parser.error("--outlier-limit must be >= 1")
    if not args.csv.is_file():
        parser.error(f"CSV not found: {args.csv}")

    with args.csv.open("r", encoding="utf-8-sig", newline="") as handle:
        all_rows = list(csv.DictReader(handle))
    successful = [row for row in all_rows if str(row.get("ok", "")).strip().lower() in {"true", "1"}]
    failed = [row for row in all_rows if row not in successful]
    by_venue: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in successful:
        by_venue[venue_of(row.get("race_key", ""))].append(row)

    venue_rows: list[dict[str, object]] = []
    for venue in sorted(by_venue):
        rows = by_venue[venue]
        record: dict[str, object] = {"venue": venue, "n_races": len(rows)}
        for metric in METRICS:
            summary = metric_summary(rows, metric)
            for key, value in summary.items():
                record[f"{metric}_{key}"] = value
        venue_rows.append(record)

    summary = {
        "input_csv": str(args.csv),
        "expected_count": args.expected_count,
        "csv_row_count": len(all_rows),
        "successful_count": len(successful),
        "failed_count": len(failed),
        "missing_from_expected_count": max(0, args.expected_count - len(successful)),
        "venues": [row["venue"] for row in venue_rows],
        "criteria": {
            "pattern_rank": "pattern_rank <= rank_cutoff_25pct",
            "uniform_ratio": ">= 50",
            "top1_prob": ">= 0.02",
            "relative_lift": ">= 0.20",
        },
        "venue_summary": venue_rows,
        "outliers": outlier_rows(successful, args.outlier_limit),
    }

    out_dir = (args.out_dir or args.csv.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    venue_path = out_dir / "venue_summary.csv"
    if venue_rows:
        fields = list(venue_rows[0].keys())
        with venue_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(venue_rows)
    outlier_path = out_dir / "outliers.csv"
    outliers = summary["outliers"]
    outlier_fields = list(outliers[0].keys()) if outliers else ["metric", "direction", "outlier_rank", "value", "race_key", "venue"]
    with outlier_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=outlier_fields)
        writer.writeheader()
        writer.writerows(outliers)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"CSV rows: {len(all_rows)}; successful: {len(successful)}; failed: {len(failed)}")
    print(f"Venue summary: {venue_path}")
    print(f"Outliers: {outlier_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
#!/usr/bin/env python3
"""Read-only Issue #31 repeat-history audit. Never unpickles payloads."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pickletools
import sqlite3
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MAX_BLOB = 10 * 1024 * 1024
MAX_DECOMPRESSED = 100 * 1024 * 1024
TARGET_CLASS = "same_seed_same_trials_and_input_sha"
TIME_COLUMNS = ("created_at", "started_at", "finished_at", "updated_at")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_aware(value):
    if value is None or not str(value).strip():
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None or d.utcoffset() is None:
        return None
    return d.astimezone(timezone.utc).timestamp()


def safe_decompress(blob):
    if blob is None:
        return None, "blob_null"
    blob = bytes(blob)
    if len(blob) > MAX_BLOB:
        return None, "blob_too_large"
    try:
        d = zlib.decompressobj()
        out = d.decompress(blob, MAX_DECOMPRESSED + 1)
        if len(out) > MAX_DECOMPRESSED or d.unconsumed_tail:
            return None, "decompressed_too_large"
        if not d.eof or d.unused_data:
            return None, "invalid_or_trailing_data"
        return out, "ok"
    except Exception as e:
        return None, f"decompress_failed:{type(e).__name__}"


def safe_pickle_scan(decompressed):
    strings = set()
    try:
        for op, arg, _pos in pickletools.genops(decompressed):
            if op.name in ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8"):
                if isinstance(arg, str) and len(arg) < 200:
                    strings.add(arg)
    except Exception as e:
        # Discard all partial scan results on any malformed stream.
        return None, None, f"genops_failed:{type(e).__name__}"
    # genops alone cannot prove which string literals are dictionary keys.
    return [], sorted(strings), "ok"


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def classify(rows):
    if any(r["seed"] is None or r["trials"] is None for r in rows):
        return "unknown_seed_or_trials"
    if len({str(r["seed"]) for r in rows}) != 1 or len({str(r["trials"]) for r in rows}) != 1:
        return "execution_conditions_diff"
    hs = [r["raw_text_sha256"] for r in rows]
    if any(not x for x in hs):
        return "same_seed_same_trials_input_unknown"
    return TARGET_CLASS if len(set(hs)) == 1 else "same_seed_same_trials_input_diff"


def stable_sort_rows(rows):
    """Sort by normalized UTC timestamp, then history_id for stable ties."""
    return sorted(rows, key=lambda r: (parse_aware(r["prediction_time"]), int(r["history_id"])))


def job_time_values_are_offset_aware(values):
    """Blank values are allowed; every nonblank value must carry a UTC offset."""
    return all(not str(v).strip() or parse_aware(v) is not None for v in values if v is not None)


def filter_ver326_rows(rows):
    return [r for r in rows if str(r["app_version"]) == "Ver326"]


def is_strict_group(ver326_rows):
    if len(ver326_rows) < 2:
        return False
    settings = {str(r["settings_hash"] or "").strip() for r in ver326_rows}
    if len(settings) != 1 or not next(iter(settings)):
        return False
    if any(r["seed"] is None or r["trials"] is None for r in ver326_rows):
        return False
    if len({str(r["seed"]) for r in ver326_rows}) != 1:
        return False
    if len({str(r["trials"]) for r in ver326_rows}) != 1:
        return False
    try:
        if int(ver326_rows[0]["trials"]) != 20000:
            return False
    except (TypeError, ValueError):
        return False
    hashes = {r["raw_text_sha256"] for r in ver326_rows}
    return len(hashes) == 1 and bool(next(iter(hashes)))



def collect_batch_jobs_368_370(con, out):
    requested = [368, 369, 370]
    available = {r[1] for r in con.execute(
        "PRAGMA table_info(v278_background_jobs)"
    )}
    required = {"job_id"}
    if not required <= available:
        raise SystemExit("BACKGROUND_JOB_ID_COLUMN_MISSING")

    optional = [
        "job_type", "status", "total_count", "done_count",
        "cancel_requested", "pause_requested",
        "created_at", "started_at", "finished_at", "updated_at",
        "error_text",
    ]
    select_cols = ["job_id"]
    for col in optional:
        select_cols.append(
            f'"{col}"' if col in available else f'NULL AS "{col}"'
        )
    if "result_blob" in available:
        select_cols.append("LENGTH(result_blob) AS result_blob_length")
    else:
        select_cols.append("NULL AS result_blob_length")

    placeholders = ",".join("?" for _ in requested)
    sql = (
        f"SELECT {','.join(select_cols)} "
        "FROM v278_background_jobs "
        f"WHERE job_id IN ({placeholders}) ORDER BY job_id"
    )
    rows = [dict(r) for r in con.execute(sql, requested)]

    csv_fields = [
        "job_id", "job_type", "status", "total_count", "done_count",
        "cancel_requested", "pause_requested",
        "created_at", "started_at", "finished_at", "updated_at",
        "result_blob_length", "error_text", "error_text_length",
    ]
    json_rows, csv_rows = [], []
    for row in rows:
        raw_err = row.get("error_text")
        error_text = "" if raw_err is None else str(raw_err)
        item = dict(row)
        item["error_text"] = error_text[:500]
        item["error_text_length"] = None if raw_err is None else len(error_text)
        item["error_text_truncated"] = len(error_text) > 500
        json_rows.append(item)

        csv_item = dict(item)
        csv_item.pop("error_text_truncated", None)
        csv_rows.append(csv_item)

    write_csv(out / "batch_jobs_368_370.csv", csv_fields, csv_rows)
    result = {
        "job_ids_requested": requested,
        "found": [r["job_id"] for r in rows],
        "status": "found" if rows else "no_rows_found",
        "rows": json_rows,
        "result_blob_content_fetched": False,
    }
    (out / "batch_jobs_368_370.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\\n",
        encoding="utf-8",
    )
    return result

def audit(db_path, db_info_path, expected_generation, expected_commit, outdir):
    db_path, out = Path(db_path).resolve(), Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    info = json.loads(Path(db_info_path).read_text(encoding="utf-8"))
    if str(info.get("manifest_generation")) != str(expected_generation):
        raise SystemExit("GENERATION_MISMATCH")
    if str(info.get("commit_sha", "")).lower() != str(expected_commit).lower():
        raise SystemExit("DB_COMMIT_SHA_MISMATCH")
    before = sha256_file(db_path)
    if info.get("db_sha256") != before:
        raise SystemExit("DB_SHA_MISMATCH")

    uri = "file:" + quote(db_path.as_posix(), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise SystemExit(f"INTEGRITY_CHECK_FAILED:{integrity}")

        # Independent early audit, before the 48/188 and 37/109 population assertions.
        job_meta = collect_batch_jobs_368_370(con, out)

        # Inspect all four SQLite storage types and representative values first.
        job_cols = {r[1] for r in con.execute("PRAGMA table_info(v278_background_jobs)")}
        required_job_cols = set(TIME_COLUMNS) | {"job_id", "job_type", "status", "app_version", "save_version"}
        if not required_job_cols <= job_cols:
            raise SystemExit("BACKGROUND_JOB_REQUIRED_COLUMNS_MISSING")
        type_samples, sample_values, invalid_samples = {}, {}, {}
        job_time_ok = True
        for col in TIME_COLUMNS:
            type_samples[col] = [dict(r) for r in con.execute(
                f'SELECT typeof("{col}") AS storage_type, COUNT(*) AS n '
                f'FROM v278_background_jobs GROUP BY typeof("{col}")')]
            sample_values[col] = [r[0] for r in con.execute(
                f'SELECT "{col}" FROM v278_background_jobs '
                f'WHERE "{col}" IS NOT NULL ORDER BY job_id LIMIT 5')]
            invalid = con.execute(
                f'SELECT "{col}" FROM v278_background_jobs WHERE "{col}" IS NOT NULL').fetchall()
            values = [r[0] for r in invalid]
            bad = [v for v in values if v is not None and str(v).strip() and parse_aware(v) is None]
            invalid_samples[col] = bad[:5]
            if not job_time_values_are_offset_aware(values):
                job_time_ok = False
            print(json.dumps({"column": col, "typeof_counts": type_samples[col],
                              "samples": sample_values[col], "invalid_nonnull_count": len(bad)},
                             ensure_ascii=False), flush=True)

        hist_cols = {r[1] for r in con.execute("PRAGMA table_info(v231_prediction_history)")}
        required_hist = {"history_id", "race_key", "app_version", "settings_hash", "prediction_time",
                         "trials", "seed", "raw_text", "payload"}
        if not required_hist <= hist_cols:
            raise SystemExit(f"HISTORY_COLUMNS_MISSING:{sorted(required_hist-hist_cols)}")

        # Load metadata only. Payload BLOBs are fetched individually for the 188 target rows.
        hist = [dict(r) for r in con.execute(
            "SELECT history_id,race_key,app_version,settings_hash,prediction_time,trials,seed,raw_text "
            "FROM v231_prediction_history ORDER BY race_key,history_id")]
        groups = defaultdict(list)
        for r in hist:
            raw = str(r["raw_text"] or "")
            r["raw_text_sha256"] = sha(raw.encode("utf-8")) if raw.strip() else ""
            r["raw_text_length"] = len(raw)
            groups[str(r["race_key"])].append(r)

        duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
        broad = {k: v for k, v in duplicate_groups.items() if classify(v) == TARGET_CLASS}
        strict = {}
        for race_key, rows in broad.items():
            ver326_rows = filter_ver326_rows(rows)
            if is_strict_group(ver326_rows):
                strict[race_key] = ver326_rows
        broad_rows = sum(map(len, broad.values()))
        strict_rows_count = sum(map(len, strict.values()))
        if len(broad) != 48 or broad_rows != 188:
            raise SystemExit(f"PARENT_POPULATION_MISMATCH: groups={len(broad)} rows={broad_rows}")
        if len(strict) != 37 or strict_rows_count != 109:
            raise SystemExit(f"STRICT_POPULATION_MISMATCH: groups={len(strict)} rows={strict_rows_count}")
        strict_sizes = Counter(str(len(v)) for v in strict.values())
        if strict_sizes != Counter({"3": 35, "2": 2}):
            raise SystemExit(f"STRICT_GROUP_SIZE_MISMATCH:{dict(strict_sizes)}")

        for rs in broad.values():
            for r in rs:
                if parse_aware(r["prediction_time"]) is None:
                    raise SystemExit(f"PREDICTION_TIME_FORMAT_UNSUPPORTED:history_id={r['history_id']}")
        target_ids = sorted(r["history_id"] for rs in broad.values() for r in rs)
        payload_meta = {}
        for history_id in target_ids:
            blob_row = con.execute(
                "SELECT payload FROM v231_prediction_history WHERE history_id=?", (history_id,)).fetchone()
            blob = bytes(blob_row[0]) if blob_row and blob_row[0] is not None else b""
            payload_meta[history_id] = {"payload_size": len(blob), "payload_sha256": sha(blob) if blob else ""}

        all_rows, strict_csv_rows = [], []
        strict_gap_values, date_counts = [], Counter()
        for race_key, race_rows in broad.items():
            ordered = stable_sort_rows(race_rows)
            is_strict = race_key in strict
            prev_ts = first_ts = None
            prev_ts_v326 = None
            for r in ordered:
                ts = parse_aware(r["prediction_time"])
                is_ver326 = str(r["app_version"]) == "Ver326"
                gap_prev = ts - prev_ts if prev_ts is not None else None
                gap_first = ts - first_ts if first_ts is not None else None
                if first_ts is None:
                    first_ts = ts

                gap_prev_v326 = None
                if is_strict and is_ver326:
                    if prev_ts_v326 is not None:
                        gap_prev_v326 = ts - prev_ts_v326
                        strict_gap_values.append(gap_prev_v326)
                    prev_ts_v326 = ts

                prev_ts = ts
                pm = payload_meta[r["history_id"]]
                x = {"race_key": race_key, "history_id": r["history_id"],
                     "app_version": r["app_version"], "settings_hash": r["settings_hash"],
                     "seed": r["seed"], "trials": r["trials"],
                     "prediction_time": r["prediction_time"], "strict_37_flag": is_strict,
                     "raw_text_length": r["raw_text_length"], "raw_text_sha256": r["raw_text_sha256"],
                     "payload_size": pm["payload_size"], "payload_sha256": pm["payload_sha256"],
                     "gap_from_prev_seconds": gap_prev, "gap_from_first_seconds": gap_first}
                all_rows.append(x)
                if is_strict and is_ver326:
                    x_strict = dict(x)
                    x_strict["gap_from_prev_seconds"] = gap_prev_v326
                    strict_csv_rows.append(x_strict)
                    date_counts[str(r["prediction_time"])[:10]] += 1
        fields = list(all_rows[0])
        write_csv(out / "duplicates_48.csv", fields, all_rows)
        write_csv(out / "strict_37.csv", fields, strict_csv_rows)

        # Time-window correlation is optional. Never infer UTC/JST for naive timestamps.
        joins = []
        job_types, save_versions = Counter(), Counter()
        if not job_time_ok:
            joins.append({"correlation_type": "skipped_job_time_format",
                          "correlation_is_proven": False,
                          "skip_reason": "one_or_more_nonblank_job_timestamps_are_not_offset_aware"})
        else:
            jobs = [dict(r) for r in con.execute(
                'SELECT job_id,job_type,status,app_version,save_version,created_at,started_at,finished_at,updated_at '
                'FROM v278_background_jobs ORDER BY job_id')]
            for x in all_rows:
                ts = parse_aware(x["prediction_time"])
                found = False
                for j in jobs:
                    # Prefer actual start/finish; use created/updated only as fallbacks.
                    start_value = j.get("started_at") or j.get("created_at")
                    end_value = j.get("finished_at") or j.get("updated_at")
                    start_ts, end_ts = parse_aware(start_value), parse_aware(end_value)
                    if start_ts is not None and end_ts is not None and start_ts - 300 <= ts <= end_ts + 300:
                        joins.append({"race_key": x["race_key"], "history_id": x["history_id"],
                                      "prediction_time": x["prediction_time"], "job_id": j["job_id"],
                                      "job_type": j["job_type"], "status": j["status"],
                                      "app_version": j["app_version"], "save_version": j["save_version"],
                                      "created_at": j["created_at"], "started_at": j["started_at"],
                                      "finished_at": j["finished_at"], "updated_at": j["updated_at"],
                                      "correlation_type": "time_window_candidate_not_proven",
                                      "correlation_is_proven": False})
                        found = True
                        if (x["strict_37_flag"]
                                and str(x["app_version"]) == "Ver326"):
                            job_types[j.get("job_type") or "(none)"] += 1
                            save_versions[j.get("save_version") or "(blank)"] += 1
                if not found:
                    joins.append({"race_key": x["race_key"], "history_id": x["history_id"],
                                  "prediction_time": x["prediction_time"],
                                  "correlation_type": "no_job_candidate", "correlation_is_proven": False})
        write_csv(out / "background_jobs_join.csv",
                  ["race_key", "history_id", "prediction_time", "job_id", "job_type", "status",
                   "app_version", "save_version", "created_at", "started_at", "finished_at", "updated_at",
                   "correlation_type", "correlation_is_proven", "skip_reason"], joins)

        pickle_rows = []
        for race_key, race_rows in broad.items():
            for r in race_rows:
                blob_row = con.execute("SELECT payload FROM v231_prediction_history WHERE history_id=?",
                                       (r["history_id"],)).fetchone()
                blob = bytes(blob_row[0]) if blob_row and blob_row[0] is not None else None
                raw, decomp_status = safe_decompress(blob)
                if decomp_status == "ok":
                    keys, strings, parse_status = safe_pickle_scan(raw)
                else:
                    keys, strings, parse_status = None, None, decomp_status
                pm = payload_meta[r["history_id"]]
                pickle_rows.append({"history_id": r["history_id"], "race_key": race_key,
                                    "payload_size": pm["payload_size"], "payload_sha256": pm["payload_sha256"],
                                    "dict_keys": keys, "string_candidates": strings,
                                    "parse_status": parse_status, "pickle_loads_called": False})
        (out / "pickletools_keys.json").write_text(
            json.dumps(pickle_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        gap_bands = Counter()
        for rs in strict.values():
            times = sorted(parse_aware(r["prediction_time"]) for r in rs)
            for a, b in zip(times, times[1:]):
                gap = b - a
                band = ("under_60_seconds" if gap < 60 else
                        "60_seconds_to_1_hour" if gap < 3600 else
                        "5_to_6_hours" if 5 * 3600 <= gap <= 6 * 3600 else
                        "around_24_hours" if 20 * 3600 <= gap <= 28 * 3600 else
                        "multiple_days" if gap >= 2 * 86400 else "other")
                gap_bands[band] += 1

        dates = Counter(str(r["prediction_time"])[:10] for rs in strict.values() for r in rs)
        summary = {
            "db_generation": info.get("manifest_generation"), "db_commit_sha": info.get("commit_sha"),
            "db_sha256": before, "integrity_check": integrity, "database_modified": False,
            "parent_group_count": len(broad), "parent_row_count": broad_rows,
            "strict_ver326_group_count": len(strict), "strict_ver326_row_count": strict_rows_count,
            "strict_group_size_distribution": dict(strict_sizes),
            "strict_prediction_time_gap_histogram": dict(gap_bands),
            "strict_gap_seconds": {"count": len(strict_gap_values),
                                    "min": min(strict_gap_values) if strict_gap_values else None,
                                    "max": max(strict_gap_values) if strict_gap_values else None},
            "strict_job_type_candidate_distribution": dict(job_types),
            "strict_save_version_candidate_distribution": dict(save_versions),
            "strict_date_distribution": dict(dates),
            "job_time_format_status": "offset_aware" if job_time_ok else "skipped_non_offset_aware",
            "job_time_sqlite_type_samples": type_samples,
            "job_time_value_samples": sample_values,
            "job_time_invalid_samples": invalid_samples,
            "job_candidate_rows": sum(1 for j in joins if j.get("job_id") is not None),
            "job_correlation_proven": False,
            "pickle_parse_status_counts": dict(Counter(r["parse_status"] for r in pickle_rows)),
            "pickle_loads_called": False,
        }
        (out / "summary_v2.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        con.close()

    after = sha256_file(db_path)
    if after != before:
        raise SystemExit("READ_ONLY_HASH_CHANGED")
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--db-info", required=True)
    p.add_argument("--expected-generation", required=True)
    p.add_argument("--expected-commit", required=True)
    p.add_argument("--outdir", required=True)
    a = p.parse_args()
    print(json.dumps(audit(a.db, a.db_info, a.expected_generation, a.expected_commit, a.outdir),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
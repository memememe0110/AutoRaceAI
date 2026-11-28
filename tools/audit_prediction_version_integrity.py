#!/usr/bin/env python3
"""Read-only audit of prediction-version consistency across AutoRaceAI DB tables.

The source database is opened with SQLite mode=ro and PRAGMA query_only=ON.
Pickled payloads are decoded only to read their app_version metadata.
"""
from __future__ import annotations

import argparse
import csv
import json
import pickle
import sqlite3
import zlib
from collections import Counter, defaultdict
from pathlib import Path


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({_q(table)})")}


def _decode_view(payload):
    if payload is None:
        return None, "payload_null"
    try:
        raw = bytes(payload)
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"
    last_error = "payload_decode_failed"
    # 旧世代・移行経路にはzlib二重圧縮もあるため、pickle判定と解凍を段階的に試す。
    for _depth in range(4):
        try:
            value = pickle.loads(raw)
            if not isinstance(value, dict):
                return None, "payload_not_dict"
            return value, ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}:{exc}"
        try:
            raw = zlib.decompress(raw)
        except Exception:
            break
    return None, last_error


def _payload_version(payload):
    view, error = _decode_view(payload)
    if error:
        return "", error
    return str(view.get("app_version") or "").strip(), ""


def _write_csv(path: Path, columns: list[str], rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def audit_db(db_path, outdir, db_info=None, expected_generation=""):
    db_path = Path(db_path).resolve()
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    info = dict(db_info or {})
    generation = info.get("manifest_generation")
    if expected_generation not in (None, "") and str(generation) != str(expected_generation):
        raise ValueError(
            f"generation mismatch: expected={expected_generation}, actual={generation}"
        )

    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")
        tables = _tables(con)
        relevant = [
            "v231_prediction_history", "v222_prediction_restore",
            "v252_lap_prediction_snapshots", "v266_pred_time_snapshots",
            "v187_mixed_plan_runs", "v278_background_jobs",
            "v326_import_queue", "result_races",
        ]
        table_inventory = [
            (name, int(name in tables), ",".join(sorted(_columns(con, name))) if name in tables else "")
            for name in relevant
        ]

        history_latest = {}
        history_versions = defaultdict(set)
        history_counts = Counter()
        history_by_id = {}
        history_payload_mismatches = []
        if "v231_prediction_history" in tables:
            cols = _columns(con, "v231_prediction_history")
            required = {"history_id", "race_key", "app_version"}
            if required.issubset(cols):
                payload_expr = "payload" if "payload" in cols else "NULL"
                for row in con.execute(
                    f"SELECT history_id,race_key,app_version,{payload_expr} AS payload "
                    "FROM v231_prediction_history ORDER BY history_id"
                ):
                    hid = int(row["history_id"])
                    race_key = str(row["race_key"] or "")
                    column_version = str(row["app_version"] or "").strip()
                    payload_version, payload_error = _payload_version(row["payload"])
                    history_counts[column_version or "(blank)"] += 1
                    history_versions[race_key].add(column_version or "(blank)")
                    history_by_id[hid] = {
                        "race_key": race_key,
                        "app_version": column_version,
                    }
                    prev = history_latest.get(race_key)
                    if prev is None or hid > prev["history_id"]:
                        history_latest[race_key] = {
                            "history_id": hid,
                            "app_version": column_version,
                        }
                    if payload_error or payload_version != column_version:
                        history_payload_mismatches.append((
                            hid, race_key, column_version, payload_version,
                            payload_error or "payload_column_version_mismatch",
                        ))

        history_rows = []
        for race_key in sorted(history_latest):
            latest = history_latest[race_key]
            versions = sorted(history_versions[race_key])
            history_rows.append((
                race_key, 0,  # filled from SQL aggregate below
                len(versions), "|".join(versions),
                latest["history_id"], latest["app_version"],
            ))
        # Count history rows per race without retaining payloads.
        if "v231_prediction_history" in tables:
            cols = _columns(con, "v231_prediction_history")
            if {"race_key"}.issubset(cols):
                per_race_counts = {
                    str(row[0] or ""): int(row[1] or 0)
                    for row in con.execute(
                        "SELECT race_key,COUNT(*) FROM v231_prediction_history GROUP BY race_key"
                    )
                }
                history_rows = [
                    (r[0], per_race_counts.get(r[0], 0), *r[2:])
                    for r in history_rows
                ]
        _write_csv(
            out / "history_versions_by_race.csv",
            ["race_key", "history_count", "distinct_version_count", "versions",
             "latest_history_id", "latest_app_version"],
            history_rows,
        )
        _write_csv(
            out / "history_payload_mismatches.csv",
            ["history_id", "race_key", "column_app_version", "payload_app_version", "reason"],
            history_payload_mismatches,
        )

        restore_rows = []
        restore_mismatches = []
        if "v222_prediction_restore" in tables:
            cols = _columns(con, "v222_prediction_restore")
            if {"race_key", "payload"}.issubset(cols):
                select_cols = ["race_key", "payload"]
                if "updated_at" in cols:
                    select_cols.append("updated_at")
                for row in con.execute(
                    "SELECT " + ",".join(_q(c) for c in select_cols)
                    + " FROM v222_prediction_restore ORDER BY race_key"
                ):
                    race_key = str(row["race_key"] or "")
                    payload_version, payload_error = _payload_version(row["payload"])
                    latest = history_latest.get(race_key)
                    latest_version = str((latest or {}).get("app_version") or "")
                    updated_at = str(row["updated_at"] or "") if "updated_at" in cols else ""
                    restore_rows.append((race_key, payload_version, latest_version, updated_at))
                    if payload_error or latest is None or payload_version != latest_version:
                        reason = payload_error or (
                            "restore_without_history" if latest is None
                            else "restore_latest_history_version_mismatch"
                        )
                        restore_mismatches.append((
                            race_key, payload_version, latest_version, updated_at, reason,
                        ))
        _write_csv(
            out / "restore_versions.csv",
            ["race_key", "restore_app_version", "latest_history_app_version", "updated_at"],
            restore_rows,
        )
        _write_csv(
            out / "restore_mismatches.csv",
            ["race_key", "restore_app_version", "latest_history_app_version", "updated_at", "reason"],
            restore_mismatches,
        )

        v266_rows = []
        v266_mismatches = []
        v266_counts = Counter()
        v266_reasons = Counter()
        if "v266_pred_time_snapshots" in tables:
            cols = _columns(con, "v266_pred_time_snapshots")
            if {"history_id", "app_version"}.issubset(cols):
                race_expr = "race_key" if "race_key" in cols else "'' AS race_key"
                source_expr = "source_kind" if "source_kind" in cols else "'' AS source_kind"
                created_expr = "MIN(created_at) AS first_created_at,MAX(created_at) AS last_created_at" if "created_at" in cols else "'' AS first_created_at,'' AS last_created_at"
                for row in con.execute(
                    f"SELECT history_id,{race_expr},app_version,{source_expr},{created_expr},COUNT(*) AS n "
                    "FROM v266_pred_time_snapshots GROUP BY history_id,race_key,app_version,source_kind "
                    "ORDER BY history_id"
                ):
                    hid = int(row["history_id"])
                    race_key = str(row["race_key"] or "")
                    version = str(row["app_version"] or "")
                    n = int(row["n"] or 0)
                    v266_counts[version or "(blank)"] += n
                    hist = history_by_id.get(hid)
                    hist_version = str((hist or {}).get("app_version") or "")
                    reason = ""
                    if hist is None:
                        reason = "snapshot_without_history"
                    elif version != hist_version:
                        reason = "history_version_mismatch"
                    v266_rows.append((
                        hid, race_key, version, hist_version, str(row["source_kind"] or ""),
                        str(row["first_created_at"] or ""), str(row["last_created_at"] or ""), n,
                    ))
                    if reason:
                        v266_reasons[reason] += n
                        v266_mismatches.append((
                            hid, race_key, version, hist_version, str(row["source_kind"] or ""),
                            str(row["first_created_at"] or ""), n, reason,
                        ))
        _write_csv(
            out / "pred_time_snapshot_versions.csv",
            ["history_id", "race_key", "snapshot_app_version", "history_app_version",
             "source_kind", "first_created_at", "last_created_at", "row_count"],
            v266_rows,
        )
        _write_csv(
            out / "pred_time_snapshot_mismatches.csv",
            ["history_id", "race_key", "snapshot_app_version", "history_app_version",
             "source_kind", "first_created_at", "row_count", "reason"],
            v266_mismatches,
        )

        lap_rows = []
        lap_race_rows = []
        lap_counts = Counter()
        lap_match_counts = Counter()
        if "v252_lap_prediction_snapshots" in tables:
            cols = _columns(con, "v252_lap_prediction_snapshots")
            if "app_version" in cols:
                group_cols = ["app_version"]
                if "is_backtest" in cols:
                    group_cols.append("is_backtest")
                select = ",".join(_q(c) for c in group_cols)
                for row in con.execute(
                    f"SELECT {select},COUNT(*) AS n FROM v252_lap_prediction_snapshots "
                    f"GROUP BY {select} ORDER BY {select}"
                ):
                    version = str(row["app_version"] or "")
                    mode = int(row["is_backtest"] or 0) if "is_backtest" in cols else None
                    n = int(row["n"] or 0)
                    lap_rows.append((version, mode, n))
                    lap_counts[version or "(blank)"] += n
                required = {"race_date", "venue", "race_no", "app_version"}
                if required.issubset(cols):
                    mode_expr = "is_backtest" if "is_backtest" in cols else "0 AS is_backtest"
                    for row in con.execute(
                        f"SELECT race_date,venue,race_no,app_version,{mode_expr},COUNT(*) AS n "
                        "FROM v252_lap_prediction_snapshots "
                        "GROUP BY race_date,venue,race_no,app_version,is_backtest "
                        "ORDER BY race_date,venue,race_no,app_version"
                    ):
                        ymd = "".join(ch for ch in str(row["race_date"] or "") if ch.isdigit())[:8]
                        rn = "".join(ch for ch in str(row["race_no"] or "") if ch.isdigit())
                        venue = str(row["venue"] or "").strip()
                        race_key = f"{ymd}_{venue}_{rn}R" if ymd and venue and rn else ""
                        version = str(row["app_version"] or "")
                        mode = int(row["is_backtest"] or 0)
                        n = int(row["n"] or 0)
                        supported = history_versions.get(race_key, set())
                        if not supported:
                            match_status = "no_history_for_race"
                        elif version in supported:
                            match_status = "version_present_in_history"
                        else:
                            match_status = "snapshot_version_not_in_history"
                        lap_match_counts[match_status] += n
                        lap_race_rows.append((
                            race_key, version, mode, n, "|".join(sorted(supported)), match_status,
                        ))
        _write_csv(
            out / "lap_snapshot_versions.csv",
            ["app_version", "is_backtest", "row_count"],
            lap_rows,
        )
        _write_csv(
            out / "lap_snapshot_race_version_audit.csv",
            ["race_key", "snapshot_app_version", "is_backtest", "row_count",
             "history_app_versions", "match_status"],
            lap_race_rows,
        )

        plan_rows = []
        plan_columns = ["app_version", "source_prediction_version", "plan_origin"]
        if "v187_mixed_plan_runs" in tables:
            cols = _columns(con, "v187_mixed_plan_runs")
            group_cols = [c for c in plan_columns if c in cols]
            if group_cols:
                select = ",".join(_q(c) for c in group_cols)
                for row in con.execute(
                    f"SELECT {select},COUNT(*) AS n FROM v187_mixed_plan_runs "
                    f"GROUP BY {select} ORDER BY {select}"
                ):
                    plan_rows.append(tuple(row[c] for c in group_cols) + (int(row["n"] or 0),))
                plan_columns = group_cols
        _write_csv(
            out / "plan_version_pairs.csv",
            plan_columns + ["row_count"],
            plan_rows,
        )

        # Row-level plan provenance: metadata only, never prediction payloads.
        plan_detail_rows = []
        plan_review_rows = []
        if "v187_mixed_plan_runs" in tables:
            cols = _columns(con, "v187_mixed_plan_runs")
            detail_cols = [
                c for c in (
                    "race_key", "race_date", "venue", "race_no", "created_at",
                    "app_version", "source_prediction_version", "plan_origin",
                    "logic_version", "plan_hash", "include_in_live_stats",
                    "grade", "starter_count", "role_count", "cover", "cost_yen",
                    "model_expected_multiple", "model_return_rate",
                ) if c in cols
            ]
            if detail_cols:
                order_cols = [c for c in ("created_at", "race_key", "plan_origin") if c in detail_cols]
                order_sql = " ORDER BY " + ",".join(_q(c) for c in order_cols) if order_cols else ""
                for row in con.execute(
                    "SELECT " + ",".join(_q(c) for c in detail_cols)
                    + " FROM v187_mixed_plan_runs" + order_sql
                ):
                    values = {c: row[c] for c in detail_cols}
                    plan_detail_rows.append(values)
                    app_ver = str(values.get("app_version") or "").strip()
                    source_ver = str(values.get("source_prediction_version") or "").strip()
                    if not app_ver or not source_ver:
                        reasons = []
                        if not app_ver:
                            reasons.append("app_version_blank")
                        if not source_ver:
                            reasons.append("source_prediction_version_blank")
                        plan_review_rows.append({
                            **values,
                            "metadata_presence": "|".join(reasons),
                        })
        plan_detail_columns = list(dict.fromkeys(
            [c for c in (
                "race_key", "race_date", "venue", "race_no", "created_at",
                "app_version", "source_prediction_version", "plan_origin",
                "logic_version", "plan_hash", "include_in_live_stats",
                "grade", "starter_count", "role_count", "cover", "cost_yen",
                "model_expected_multiple", "model_return_rate",
            ) if any(c in row for row in plan_detail_rows)]
        ))
        _write_csv(
            out / "plan_version_rows.csv",
            plan_detail_columns,
            [tuple(row.get(c) for c in plan_detail_columns) for row in plan_detail_rows],
        )
        _write_csv(
            out / "plan_version_metadata_review.csv",
            plan_detail_columns + ["metadata_presence"],
            [tuple(row.get(c) for c in plan_detail_columns) + (row["metadata_presence"],)
             for row in plan_review_rows],
        )

        queue_rows = []
        queue_mismatches = []
        if "v326_import_queue" in tables:
            cols = _columns(con, "v326_import_queue")
            if "task_id" in cols:
                select_cols = [c for c in ("task_id", "status", "save_version", "config_json") if c in cols]
                for row in con.execute(
                    "SELECT " + ",".join(_q(c) for c in select_cols)
                    + " FROM v326_import_queue ORDER BY task_id"
                ):
                    task_id = int(row["task_id"])
                    save_version = str(row["save_version"] or "") if "save_version" in cols else ""
                    config_raw = row["config_json"] if "config_json" in cols else None
                    config = {}
                    config_error = ""
                    if config_raw:
                        try:
                            config = json.loads(config_raw) if isinstance(config_raw, str) else dict(config_raw)
                        except Exception as exc:
                            config_error = f"{type(exc).__name__}:{exc}"
                    json_version = str(config.get("save_version") or "").strip()
                    enabled = config.get("v330a_enabled")
                    status = str(row["status"] or "") if "status" in cols else ""
                    queue_rows.append((task_id, status, save_version, json_version, enabled))
                    if config_error or (json_version and save_version and json_version != save_version):
                        queue_mismatches.append((
                            task_id, status, save_version, json_version, config_error or "column_json_version_mismatch",
                        ))
        _write_csv(
            out / "queue_version_audit.csv",
            ["task_id", "status", "column_save_version", "json_save_version", "v330a_enabled"],
            queue_rows,
        )
        _write_csv(
            out / "queue_version_mismatches.csv",
            ["task_id", "status", "column_save_version", "json_save_version", "reason"],
            queue_mismatches,
        )

        job_rows = []
        if "v278_background_jobs" in tables:
            cols = _columns(con, "v278_background_jobs")
            group_cols = [c for c in ("job_type", "status", "app_version", "save_version") if c in cols]
            if group_cols:
                select = ",".join(_q(c) for c in group_cols)
                for row in con.execute(
                    f"SELECT {select},COUNT(*) AS n FROM v278_background_jobs "
                    f"GROUP BY {select} ORDER BY {select}"
                ):
                    job_rows.append(tuple(row[c] for c in group_cols) + (int(row["n"] or 0),))
        _write_csv(
            out / "background_job_version_pairs.csv",
            ["job_type", "status", "app_version", "save_version", "row_count"],
            job_rows,
        )

        # Background job metadata by job_id; result_blob is intentionally excluded.
        job_detail_rows = []
        job_review_rows = []
        if "v278_background_jobs" in tables:
            cols = _columns(con, "v278_background_jobs")
            detail_cols = [
                c for c in (
                    "job_id", "job_type", "status", "created_at", "started_at",
                    "finished_at", "updated_at", "app_version", "save_version",
                    "force_current", "total_count", "done_count", "register_result",
                    "error_text", "message",
                ) if c in cols
            ]
            if detail_cols:
                order_cols = [c for c in ("job_id",) if c in detail_cols]
                order_sql = " ORDER BY " + ",".join(_q(c) for c in order_cols) if order_cols else ""
                for row in con.execute(
                    "SELECT " + ",".join(_q(c) for c in detail_cols)
                    + " FROM v278_background_jobs" + order_sql
                ):
                    values = {c: row[c] for c in detail_cols}
                    job_detail_rows.append(values)
                    app_ver = str(values.get("app_version") or "").strip()
                    save_ver = str(values.get("save_version") or "").strip()
                    if not app_ver or not save_ver:
                        reasons = []
                        if not app_ver:
                            reasons.append("app_version_blank")
                        if not save_ver:
                            reasons.append("save_version_blank")
                        job_review_rows.append({
                            **values,
                            "metadata_presence": "|".join(reasons),
                        })
        job_detail_columns = list(dict.fromkeys(
            [c for c in (
                "job_id", "job_type", "status", "created_at", "started_at",
                "finished_at", "updated_at", "app_version", "save_version",
                "force_current", "total_count", "done_count", "register_result",
                "error_text", "message",
            ) if any(c in row for row in job_detail_rows)]
        ))
        _write_csv(
            out / "background_job_version_rows.csv",
            job_detail_columns,
            [tuple(row.get(c) for c in job_detail_columns) for row in job_detail_rows],
        )
        _write_csv(
            out / "background_job_metadata_review.csv",
            job_detail_columns + ["metadata_presence"],
            [tuple(row.get(c) for c in job_detail_columns) + (row["metadata_presence"],)
             for row in job_review_rows],
        )

        result_coverage = []
        if "result_races" in tables and "v231_prediction_history" in tables:
            result_keys = {str(row[0] or "") for row in con.execute("SELECT race_key FROM result_races")}
            for race_key in sorted(result_keys):
                versions = sorted(history_versions.get(race_key, set()))
                latest = history_latest.get(race_key) or {}
                result_coverage.append((
                    race_key, len(versions), "|".join(versions),
                    latest.get("app_version", ""), latest.get("history_id", ""),
                    1 if versions else 0,
                ))
        _write_csv(
            out / "result_prediction_version_coverage.csv",
            ["race_key", "distinct_prediction_versions", "versions", "latest_app_version",
             "latest_history_id", "has_prediction_history"],
            result_coverage,
        )

        summary = {
            "db_source": info.get("db_source"),
            "db_commit_sha": info.get("commit_sha"),
            "db_commit_date": info.get("commit_date"),
            "db_generation": generation,
            "db_manifest_saved_at": info.get("manifest_saved_at"),
            "db_sha256": info.get("db_sha256"),
            "db_integrity_check": integrity,
            "table_inventory": [
                {"table": t, "present": bool(p), "columns": c}
                for t, p, c in table_inventory
            ],
            "history_rows_by_app_version": dict(sorted(history_counts.items())),
            "history_race_count": len(history_latest),
            "history_races_with_multiple_versions": sum(
                len(v) > 1 for v in history_versions.values()
            ),
            "history_payload_mismatch_count": len(history_payload_mismatches),
            "restore_rows": len(restore_rows),
            "restore_mismatch_count": len(restore_mismatches),
            "pred_time_snapshot_rows_by_app_version": dict(sorted(v266_counts.items())),
            "pred_time_snapshot_mismatch_count": sum(v266_reasons.values()),
            "pred_time_snapshot_orphan_row_count": v266_reasons.get("snapshot_without_history", 0),
            "pred_time_snapshot_version_mismatch_row_count": v266_reasons.get("history_version_mismatch", 0),
            "lap_snapshot_rows_by_app_version": dict(sorted(lap_counts.items())),
            "lap_snapshot_version_groups": len(lap_rows),
            "lap_snapshot_rows_by_history_match_status": dict(sorted(lap_match_counts.items())),
            "lap_snapshot_race_version_groups": len(lap_race_rows),
            "plan_version_groups": len(plan_rows),
            "plan_version_row_count": len(plan_detail_rows),
            "plan_rows_missing_app_version": sum(
                not str(row.get("app_version") or "").strip() for row in plan_detail_rows
            ),
            "plan_rows_missing_source_prediction_version": sum(
                not str(row.get("source_prediction_version") or "").strip()
                for row in plan_detail_rows
            ),
            "plan_version_rows_by_origin": dict(sorted(Counter(
                str(row.get("plan_origin") or "(blank)") for row in plan_detail_rows
            ).items())),
            "queue_rows": len(queue_rows),
            "queue_version_mismatch_count": len(queue_mismatches),
            "background_job_version_groups": len(job_rows),
            "background_job_version_row_count": len(job_detail_rows),
            "background_job_rows_missing_app_version": sum(
                not str(row.get("app_version") or "").strip() for row in job_detail_rows
            ),
            "background_job_rows_missing_save_version": sum(
                not str(row.get("save_version") or "").strip() for row in job_detail_rows
            ),
            "result_race_count": len(result_coverage),
            "result_races_without_prediction_history": sum(not r[-1] for r in result_coverage),
            "database_modified": False,
            "database_in_artifact": False,
            "payload_note": "Pickle payloads were decoded only for app_version metadata; no payload was rewritten.",
        }
        (out / "prediction_version_audit_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_csv(
            out / "prediction_version_table_inventory.csv",
            ["table_name", "present", "columns"],
            table_inventory,
        )
        return summary
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--db-info", required=True)
    parser.add_argument("--expected-generation", default="")
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()
    info = json.loads(Path(args.db_info).read_text(encoding="utf-8"))
    summary = audit_db(args.db, args.outdir, info, args.expected_generation)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

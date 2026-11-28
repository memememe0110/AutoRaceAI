import csv
import json
import pickle
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.audit_prediction_version_integrity import audit_db


def payload(version):
    return sqlite3.Binary(zlib.compress(pickle.dumps({"app_version": version}, protocol=4)))


class PredictionVersionAuditTests(unittest.TestCase):
    def test_version_mismatch_detection_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "audit.sqlite3"
            out = root / "out"
            with sqlite3.connect(db) as con:
                con.executescript("""
                    CREATE TABLE v231_prediction_history(
                        history_id INTEGER PRIMARY KEY, race_key TEXT, app_version TEXT, payload BLOB
                    );
                    CREATE TABLE v222_prediction_restore(
                        race_key TEXT PRIMARY KEY, payload BLOB, updated_at TEXT
                    );
                    CREATE TABLE v319_prediction_history_archive(
                        history_id INTEGER PRIMARY KEY, race_key TEXT, app_version TEXT,
                        prediction_time TEXT
                    );
                    CREATE TABLE v266_pred_time_snapshots(
                        history_id INTEGER, app_version TEXT, car_no INTEGER
                    );
                    CREATE TABLE v252_lap_prediction_snapshots(
                        app_version TEXT, is_backtest INTEGER, race_date TEXT, venue TEXT, race_no TEXT
                    );
                    CREATE TABLE v187_mixed_plan_runs(
                        app_version TEXT, source_prediction_version TEXT, plan_origin TEXT,
                        race_key TEXT, created_at TEXT, logic_version TEXT, plan_hash TEXT,
                        include_in_live_stats INTEGER
                    );
                    CREATE TABLE v278_background_jobs(
                        job_type TEXT, status TEXT, app_version TEXT, save_version TEXT,
                        job_id INTEGER, created_at TEXT, force_current INTEGER, result_blob BLOB
                    );
                    CREATE TABLE v326_import_queue(
                        task_id INTEGER, status TEXT, save_version TEXT, config_json TEXT
                    );
                    CREATE TABLE result_races(race_key TEXT PRIMARY KEY);
                """)
                con.execute(
                    "INSERT INTO v231_prediction_history VALUES(1,?,?,?)",
                    ("20261101_川口_1R", "Ver326", payload("Ver326")),
                )
                con.execute(
                    "INSERT INTO v231_prediction_history VALUES(2,?,?,?)",
                    ("20261101_川口_1R", "Ver330-A", payload("Ver326")),
                )
                con.executemany(
                    "INSERT INTO v222_prediction_restore VALUES(?,?,?)",
                    [
                        ("20261101_川口_1R", payload("Ver326"), "2026-11-01T12:00:00"),
                        ("20261001_飯塚_1R", payload("Ver323"), "2026-10-02T12:00:00"),
                    ],
                )
                con.executemany(
                    "INSERT INTO v319_prediction_history_archive VALUES(?,?,?,?)",
                    [
                        (101, "20261101_川口_1R", "Ver326", "2026-11-01T11:00:00"),
                        (102, "20261001_飯塚_1R", "Ver323", "2026-10-02T11:00:00"),
                    ],
                )
                con.execute(
                    "INSERT INTO v266_pred_time_snapshots VALUES(?,?,?)",
                    (2, "Ver326", 1),
                )
                con.executemany(
                    "INSERT INTO v252_lap_prediction_snapshots VALUES(?,?,?,?,?)",
                    [
                        ("Ver330-A", 0, "2026-11-01", "川口", "1"),
                        ("Ver324", 0, "2026-11-01", "川口", "1"),
                    ],
                )
                con.execute(
                    """INSERT INTO v187_mixed_plan_runs
                       (app_version,source_prediction_version,plan_origin,race_key,created_at,logic_version,plan_hash,include_in_live_stats)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    ("Ver330-A", "Ver326", "odds_replan", "20261101_川口_1R",
                     "2026-11-01T12:00:00", "logic-v1", "plan-1", 1),
                )
                con.execute(
                    """INSERT INTO v187_mixed_plan_runs
                       (app_version,source_prediction_version,plan_origin,race_key,created_at,logic_version,plan_hash,include_in_live_stats)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (None, None, "live", "20261001_飯塚_1R",
                     "2026-10-01T12:00:00", "legacy", "plan-legacy", 1),
                )
                con.execute(
                    """INSERT INTO v278_background_jobs
                       (job_type,status,app_version,save_version,job_id,created_at,force_current,result_blob)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    ("official_import", "completed", "Ver326", "Ver330-A", 10,
                     "2026-11-01T12:00:00", 0, sqlite3.Binary(b"DO_NOT_EXPORT_BLOB")),
                )
                con.execute(
                    """INSERT INTO v278_background_jobs
                       (job_type,status,app_version,save_version,job_id,created_at,force_current,result_blob)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    ("batch_rerun", "cancelled", None, None, 11,
                     "2026-10-01T12:00:00", 1, sqlite3.Binary(b"PRIVATE_BLOB")),
                )
                con.execute(
                    "INSERT INTO v326_import_queue VALUES(?,?,?,?)",
                    (1, "pending", "Ver330-A", json.dumps({"save_version": "Ver326"})),
                )
                con.execute("INSERT INTO result_races VALUES(?)", ("20261101_川口_1R",))
                con.commit()

            summary = audit_db(
                db, out,
                {"manifest_generation": 705, "db_sha256": "test"},
                expected_generation=705,
            )
            self.assertEqual(summary["history_payload_mismatch_count"], 1)
            self.assertEqual(summary["restore_mismatch_count"], 2)
            self.assertTrue(summary["restore_archive_table_present"])
            self.assertEqual(summary["restore_archive_history_row_count"], 2)
            self.assertEqual(summary["restore_mismatch_races_with_archive_rows"], 2)
            self.assertEqual(summary["restore_without_history_with_matching_archive_version"], 1)
            self.assertEqual(summary["pred_time_snapshot_mismatch_count"], 1)
            self.assertEqual(summary["pred_time_snapshot_version_mismatch_row_count"], 1)
            self.assertEqual(summary["lap_snapshot_rows_by_history_match_status"]["version_present_in_history"], 1)
            self.assertEqual(summary["lap_snapshot_rows_by_history_match_status"]["snapshot_version_not_in_history"], 1)
            self.assertEqual(summary["queue_version_mismatch_count"], 1)
            self.assertEqual(summary["result_races_without_prediction_history"], 0)
            self.assertFalse(summary["database_modified"])
            self.assertEqual(summary["plan_version_row_count"], 2)
            self.assertEqual(summary["plan_rows_missing_app_version"], 1)
            self.assertEqual(summary["plan_rows_missing_source_prediction_version"], 1)
            self.assertEqual(summary["background_job_version_row_count"], 2)
            self.assertEqual(summary["background_job_rows_missing_app_version"], 1)
            self.assertEqual(summary["background_job_rows_missing_save_version"], 1)

            plan_rows = (out / "plan_version_rows.csv").read_text(encoding="utf-8-sig")
            self.assertIn("20261001_飯塚_1R", plan_rows)
            plan_review = (out / "plan_version_metadata_review.csv").read_text(encoding="utf-8-sig")
            self.assertIn("app_version_blank|source_prediction_version_blank", plan_review)

            job_rows = (out / "background_job_version_rows.csv").read_text(encoding="utf-8-sig")
            self.assertIn("official_import", job_rows)
            self.assertNotIn("result_blob", job_rows)
            self.assertNotIn("DO_NOT_EXPORT_BLOB", job_rows)
            self.assertNotIn("PRIVATE_BLOB", job_rows)

            with (out / "restore_archive_matches.csv").open(
                encoding="utf-8-sig", newline=""
            ) as f:
                archive_rows = list(csv.DictReader(f))
            orphan = next(row for row in archive_rows if row["race_key"] == "20261001_飯塚_1R")
            self.assertEqual(orphan["reason"], "restore_without_history")
            self.assertEqual(orphan["matching_restore_version_archive_rows"], "1")
            self.assertEqual(orphan["matching_archive_history_ids_json"], "[102]")

            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM v231_prediction_history").fetchone()[0], 2)

    def test_payload_decoder_handles_single_and_double_zlib_compression(self):
        from tools.audit_prediction_version_integrity import _decode_view
        raw = pickle.dumps({"app_version": "Ver330-A"}, protocol=4)
        once, err_once = _decode_view(zlib.compress(raw))
        twice, err_twice = _decode_view(zlib.compress(zlib.compress(raw)))
        self.assertEqual(err_once, "")
        self.assertEqual(err_twice, "")
        self.assertEqual(once["app_version"], "Ver330-A")
        self.assertEqual(twice["app_version"], "Ver330-A")

    def test_generation_guard_rejects_wrong_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "empty.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE marker(x)")
            with self.assertRaisesRegex(ValueError, "generation mismatch"):
                audit_db(db, Path(td) / "out", {"manifest_generation": 704}, expected_generation=705)


if __name__ == "__main__":
    unittest.main(verbosity=2)

import hashlib
import pickle
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.audit_duplicate_history_repeats import (
    classify,
    collect_batch_jobs_368_370,
    filter_ver326_rows,
    is_strict_group,
    job_time_values_are_offset_aware,
    parse_aware,
    safe_decompress,
    safe_pickle_scan,
    sha256_file,
    stable_sort_rows,
)


class RepeatAuditLogicTests(unittest.TestCase):
    def row(self, history_id, prediction_time, **overrides):
        r = {
            "history_id": history_id,
            "prediction_time": prediction_time,
            "app_version": "Ver326",
            "settings_hash": "settings-a",
            "seed": 12345,
            "trials": 20000,
            "raw_text_sha256": "a" * 64,
        }
        r.update(overrides)
        return r

    def test_three_row_adjacent_and_first_gaps(self):
        ts = [parse_aware(x) for x in (
            "2026-10-15T09:00:00+09:00",
            "2026-10-15T14:30:00+09:00",
            "2026-10-15T19:30:00+09:00",
        )]
        self.assertEqual([ts[1] - ts[0], ts[2] - ts[1]], [19800, 18000])
        self.assertEqual([ts[1] - ts[0], ts[2] - ts[0]], [19800, 37800])

    def test_two_row_gap(self):
        a = parse_aware("2026-10-15T09:00:00+09:00")
        b = parse_aware("2026-10-15T14:30:00+09:00")
        self.assertEqual(b - a, 19800)

    def test_naive_datetime_rejected(self):
        self.assertIsNone(parse_aware("2026-10-15T09:00:00"))

    def test_job_time_naive_skips_only_time_matching(self):
        self.assertTrue(job_time_values_are_offset_aware([None, "", "2026-10-15T09:00:00+09:00"]))
        self.assertFalse(job_time_values_are_offset_aware(["2026-10-15T09:00:00"]))

    def test_stable_sort_uses_utc_then_history_id(self):
        rows = [
            self.row(9, "2026-10-15T00:00:00+00:00"),
            self.row(3, "2026-10-15T09:00:00+09:00"),
            self.row(4, "2026-10-15T01:00:00+00:00"),
        ]
        self.assertEqual([r["history_id"] for r in stable_sort_rows(rows)], [3, 9, 4])

    def test_strict_ver326_criteria(self):
        rows = [
            self.row(1, "2026-10-15T09:00:00+09:00"),
            self.row(2, "2026-10-15T14:30:00+09:00"),
        ]
        self.assertTrue(is_strict_group(filter_ver326_rows(rows)))

        # A mixed-version group remains strict when two Ver326 rows qualify.
        mixed = rows + [
            self.row(3, "2026-10-15T19:30:00+09:00", app_version="Ver327")
        ]
        self.assertTrue(is_strict_group(filter_ver326_rows(mixed)))

        # One Ver326 row is insufficient, even if another version is present.
        one_ver326 = [
            rows[0],
            self.row(3, "2026-10-15T19:30:00+09:00", app_version="Ver327")
        ]
        self.assertFalse(is_strict_group(filter_ver326_rows(one_ver326)))

        self.assertFalse(is_strict_group(filter_ver326_rows([
            dict(rows[0], settings_hash=""), rows[1]
        ])))
        self.assertFalse(is_strict_group(filter_ver326_rows([
            dict(rows[0], trials=10000), rows[1]
        ])))

    def test_strict_csv_excludes_non_ver326_rows(self):
        rows = [
            self.row(1, "2026-10-15T09:00:00+09:00"),
            self.row(2, "2026-10-15T14:30:00+09:00"),
            self.row(3, "2026-10-15T19:30:00+09:00", app_version="Ver327"),
        ]
        strict_rows = filter_ver326_rows(rows)
        self.assertEqual([r["history_id"] for r in strict_rows], [1, 2])
        self.assertTrue(all(r["app_version"] == "Ver326" for r in strict_rows))

    def test_classify_same_input_and_conditions(self):
        rows = [self.row(1, "2026-10-15T09:00:00+09:00"), self.row(2, "2026-10-15T10:00:00+09:00")]
        self.assertEqual(classify(rows), "same_seed_same_trials_and_input_sha")
        self.assertEqual(classify([rows[0], dict(rows[1], raw_text_sha256="b" * 64)]),
                         "same_seed_same_trials_input_diff")
        self.assertEqual(classify([rows[0], dict(rows[1], seed=999)]), "execution_conditions_diff")

    def test_safe_zlib_and_trailing_data(self):
        raw = pickle.dumps({"a": "b"}, protocol=4)
        self.assertEqual(safe_decompress(zlib.compress(raw))[1], "ok")
        self.assertEqual(safe_decompress(zlib.compress(raw) + b"x")[1], "invalid_or_trailing_data")

    def test_pickle_scan_no_partial_results(self):
        raw = pickle.dumps({"small": "value"}, protocol=4)
        keys, strings, status = safe_pickle_scan(raw)
        self.assertEqual(status, "ok")
        self.assertEqual(keys, [])
        self.assertIn("small", strings)
        keys, strings, status = safe_pickle_scan(raw[:-2])
        self.assertTrue(status.startswith("genops_failed:"))
        self.assertIsNone(keys)
        self.assertIsNone(strings)

    def test_batch_job_metadata_found_blob_length_and_error_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("""
                CREATE TABLE v278_background_jobs (
                    job_id INTEGER, job_type TEXT, status TEXT,
                    total_count INTEGER, done_count INTEGER,
                    cancel_requested INTEGER, pause_requested INTEGER,
                    created_at TEXT, started_at TEXT, finished_at TEXT,
                    updated_at TEXT, error_text TEXT, result_blob BLOB
                )
            """)
            con.execute("""
                INSERT INTO v278_background_jobs
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (368, "batch_rerun", "completed", 35, 35, 0, 0,
                  "created", "started", "finished", "updated", "x" * 600,
                  b"\x01\x02\x03"))

            result = collect_batch_jobs_368_370(con, Path(td))
            row = result["rows"][0]
            self.assertEqual(result["status"], "found")
            self.assertEqual(result["found"], [368])
            self.assertEqual(row["result_blob_length"], 3)
            self.assertEqual(row["error_text_length"], 600)
            self.assertTrue(row["error_text_truncated"])
            self.assertEqual(len(row["error_text"]), 500)
            self.assertFalse(result["result_blob_content_fetched"])
            self.assertTrue((Path(td) / "batch_jobs_368_370.csv").exists())
            self.assertTrue((Path(td) / "batch_jobs_368_370.json").exists())
            con.close()

    def test_batch_job_metadata_no_rows_continues(self):
        with tempfile.TemporaryDirectory() as td:
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE v278_background_jobs (job_id INTEGER, result_blob BLOB)")
            result = collect_batch_jobs_368_370(con, Path(td))
            self.assertEqual(result["job_ids_requested"], [368, 369, 370])
            self.assertEqual(result["found"], [])
            self.assertEqual(result["status"], "no_rows_found")
            self.assertEqual(result["rows"], [])
            con.close()

    def test_batch_job_metadata_optional_columns_missing_and_null_error(self):
        with tempfile.TemporaryDirectory() as td:
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE v278_background_jobs (job_id INTEGER, status TEXT, error_text TEXT)")
            con.execute("INSERT INTO v278_background_jobs VALUES (369, 'failed', NULL)")
            result = collect_batch_jobs_368_370(con, Path(td))
            row = result["rows"][0]
            self.assertEqual(result["found"], [369])
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(row["result_blob_length"])
            self.assertIsNone(row["cancel_requested"])
            self.assertIsNone(row["error_text_length"])
            con.close()

    def test_sha256_file_streaming_result(self):
        data = b"audit-stream-test" * 1000
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.bin"
            p.write_bytes(data)
            self.assertEqual(sha256_file(p), hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
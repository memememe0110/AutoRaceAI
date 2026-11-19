import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.audit_prediction_history_duplicates import (
    REQUIRED_COLUMNS,
    audit_database,
    classify_group,
    row_input_sha,
    sha256_file,
)


class PredictionHistoryDuplicateAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "audit.sqlite3"
        self.info = self.root / "db_info.json"
        self.out = self.root / "out"
        con = sqlite3.connect(self.db)
        con.execute("""
            CREATE TABLE v231_prediction_history (
                id INTEGER PRIMARY KEY,
                race_key TEXT,
                app_version TEXT,
                simulation_mode TEXT,
                settings_hash TEXT,
                prediction_time TEXT,
                trials INTEGER,
                seed INTEGER,
                raw_text TEXT,
                venue_override TEXT,
                payload BLOB
            )
        """)
        con.executemany(
            """INSERT INTO v231_prediction_history
               (id, race_key, app_version, settings_hash, prediction_time, trials, seed, raw_text, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (1, "20261101_川口_1R", "Ver326", "cfg", "2026-11-01T10:00:00+09:00", 20000, 123, "card-A", b"x" * 100),
                (2, "20261101_川口_1R", "Ver326", "cfg", "2026-11-01T10:01:00+09:00", 20000, 123, "card-A", b"y" * 100),
                (3, "20261101_川口_2R", "Ver326", "cfg", "2026-11-01T10:02:00+09:00", 20000, 123, "card-B", b"z" * 100),
                (4, "20261101_川口_2R", "Ver326", "cfg", "2026-11-01T10:03:00+09:00", 20000, 123, "card-C", b"w" * 100),
                (5, "20261101_川口_3R", "Ver326", "cfg", "2026-11-01T10:04:00+09:00", 10000, 123, "card-D", b"q" * 100),
                (6, "20261101_川口_3R", "Ver326", "cfg", "2026-11-01T10:05:00+09:00", 20000, 456, "card-D", b"r" * 100),
                (7, "20261101_川口_4R", "Ver326", "cfg", "2026-11-01T10:06:00+09:00", 20000, 123, None, b"s" * 100),
                (8, "20261101_川口_4R", "Ver326", "cfg", "2026-11-01T10:07:00+09:00", 20000, 123, "card-E", b"t" * 100),
            ],
        )
        con.commit()
        con.close()
        self.info.write_text(json.dumps({
            "manifest_generation": "705",
            "commit_sha": "20c67a8450ea72d219c19601319faf558a2f93c1",
            "db_sha256": sha256_file(self.db),
            "db_source": "test-fixture",
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_row_input_sha_matches_sha256_recipe(self):
        expected = hashlib.sha256("card-A".encode("utf-8", errors="replace")).hexdigest()
        self.assertEqual(row_input_sha("card-A"), expected)

    def test_duplicate_groups_are_classified_and_database_is_unchanged(self):
        before = sha256_file(self.db)
        summary = audit_database(self.db, self.info, "705", self.out)
        self.assertEqual(summary["duplicate_race_count"], 4)
        self.assertEqual(summary["classification_group_counts"]["same_seed_same_trials_and_input_sha"], 1)
        self.assertEqual(summary["classification_group_counts"]["same_seed_same_trials_input_diff"], 1)
        self.assertEqual(summary["classification_group_counts"]["execution_conditions_diff"], 1)
        self.assertEqual(summary["classification_group_counts"]["same_seed_same_trials_input_unknown"], 1)
        self.assertEqual(sha256_file(self.db), before)
        self.assertTrue((self.out / "duplicates.csv").exists())
        self.assertTrue((self.out / "summary.json").exists())

    def test_required_columns_gate(self):
        self.assertEqual(
            REQUIRED_COLUMNS,
            {"id", "race_key", "app_version", "trials", "seed", "raw_text", "prediction_time"},
        )

    def test_classification_same_seed_trials_and_input(self):
        rows = [{"seed": 7, "trials": 20000, "input_sha256": "abc"}] * 2
        self.assertEqual(classify_group(rows), "same_seed_same_trials_and_input_sha")

    def test_classification_input_diff_and_unknown(self):
        diff = [
            {"seed": 7, "trials": 20000, "input_sha256": "abc"},
            {"seed": 7, "trials": 20000, "input_sha256": "def"},
        ]
        unknown = [
            {"seed": 7, "trials": 20000, "input_sha256": None},
            {"seed": 7, "trials": 20000, "input_sha256": "def"},
        ]
        self.assertEqual(classify_group(diff), "same_seed_same_trials_input_diff")
        self.assertEqual(classify_group(unknown), "same_seed_same_trials_input_unknown")

    def test_generation_and_manifest_sha_guards(self):
        with self.assertRaisesRegex(SystemExit, "GENERATION_MISMATCH"):
            audit_database(self.db, self.info, "704", self.out)
        data = json.loads(self.info.read_text(encoding="utf-8"))
        data["db_sha256"] = "0" * 64
        self.info.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "DB_SHA_MISMATCH"):
            audit_database(self.db, self.info, "705", self.out)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from audit_result_registration_details import run_audit


class ResultRegistrationDetailAuditTests(unittest.TestCase):
    def test_exports_flagged_rows_and_per_lap_car_differences_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "fixture.sqlite3"
            out = root / "audit"
            out.mkdir()

            with sqlite3.connect(db) as con:
                con.executescript("""
                    CREATE TABLE result_races (
                        race_key TEXT PRIMARY KEY, race_status TEXT
                    );
                    CREATE TABLE result_entries (
                        race_key TEXT, car_no INTEGER, position INTEGER,
                        result_status TEXT
                    );
                    CREATE TABLE result_laps (
                        race_key TEXT, lap_label TEXT, lap_no INTEGER,
                        car_no INTEGER, position INTEGER
                    );
                    CREATE TABLE result_payouts (
                        race_key TEXT, bet_type TEXT, combination TEXT,
                        payout_yen INTEGER
                    );
                    INSERT INTO result_races VALUES
                        ('20261101_川口_1R', NULL),
                        ('20261101_川口_2R', '不成立');
                    INSERT INTO result_entries VALUES
                        ('20261101_川口_1R', 1, 1, '通常'),
                        ('20261101_川口_1R', 2, 2, '通常'),
                        ('20261101_川口_1R', 3, 3, '通常');
                    INSERT INTO result_laps VALUES
                        ('20261101_川口_1R', '1周目', 1, 1, 1),
                        ('20261101_川口_1R', '1周目', 1, 2, 2),
                        ('20261101_川口_1R', '1周目', 1, 2, 3),
                        ('20261101_川口_1R', '2周目', 2, 1, 1),
                        ('20261101_川口_1R', '2周目', 2, 2, 2),
                        ('20261101_川口_1R', '2周目', 2, 3, 3),
                        ('20261101_川口_1R', 'ゴール線', NULL, 1, 1),
                        ('20261101_川口_1R', 'ゴール線', NULL, 2, 2);
                    INSERT INTO result_payouts VALUES
                        ('20261101_川口_1R', '3連単', '1-2-3', 1230),
                        ('20261101_川口_2R', '単勝', '1', 0);
                """)

            goal_rows = [{
                "race_key": "20261101_川口_1R", "n_entries": "3",
                "n_laps": "8", "n_payouts": "2", "race_status": "",
                "incomplete_flag": "1", "incomplete_reason": "goal_line_shortage",
            }]
            zero_rows = [
                {
                    "race_key": "20261101_川口_2R", "n_entries": "0",
                    "n_laps": "0", "n_payouts": "1", "race_status": "不成立",
                    "incomplete_flag": "0", "incomplete_reason": "entries_empty",
                },
                {
                    "race_key": "20261101_川口_1R", "n_entries": "3",
                    "n_laps": "8", "n_payouts": "2", "race_status": "",
                    "incomplete_flag": "1", "incomplete_reason": "goal_line_shortage",
                },
            ]
            for name, rows in [
                ("result_audit_goal_line_review.csv", goal_rows),
                ("result_audit_zero_count_review.csv", zero_rows),
            ]:
                with (out / name).open("w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)

            before = hashlib.sha256(db.read_bytes()).hexdigest()
            summary = run_audit(db, out)
            after = hashlib.sha256(db.read_bytes()).hexdigest()

            self.assertEqual(before, after)
            self.assertTrue(summary["query_only"])
            self.assertFalse(summary["database_modified"])
            self.assertEqual(summary["review_race_keys"], ["20261101_川口_1R"])
            self.assertEqual(summary["flagged_row_count_by_table"], {
                "result_entries": 3, "result_laps": 8,
                "result_payouts": 1, "result_races": 1,
            })

            with (out / "result_registration_lap_group_detail.csv").open(
                encoding="utf-8-sig", newline=""
            ) as f:
                lap_rows = list(csv.DictReader(f))
            first_lap = next(row for row in lap_rows if row["lap_no"] == "1")
            self.assertEqual(first_lap["missing_car_nos_json"], '["3"]')
            self.assertEqual(first_lap["duplicate_car_nos_json"], '["2"]')
            goal_line = next(row for row in lap_rows if row["lap_label"] == "ゴール線")
            self.assertEqual(goal_line["missing_car_nos_json"], '["3"]')

            with (out / "result_registration_flagged_rows.csv").open(
                encoding="utf-8-sig", newline=""
            ) as f:
                exported = list(csv.DictReader(f))
            self.assertEqual(len(exported), 13)
            self.assertTrue(all(row["race_key"] == "20261101_川口_1R" for row in exported))

            detail_summary = json.loads(
                (out / "result_registration_detail_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(detail_summary["database_in_artifact"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

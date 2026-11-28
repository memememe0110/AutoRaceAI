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
    def test_finishers_not_all_entrants_define_goal_line_completeness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "fixture.sqlite3"
            out = root / "audit"
            out.mkdir()

            with sqlite3.connect(db) as con:
                con.executescript("""
                    CREATE TABLE result_races (
                        race_key TEXT PRIMARY KEY, race_status TEXT,
                        learning_eligible INTEGER, learning_exclusion_reason TEXT,
                        model_eligible INTEGER, model_exclusion_reason TEXT
                    );
                    CREATE TABLE result_entries (
                        race_key TEXT, car_no INTEGER, finish INTEGER,
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
                        ('20261101_川口_1R', NULL, 0, '競走中止', 0, '競走中止'),
                        ('20261101_川口_2R', NULL, 1, NULL, 1, ''),
                        ('20261101_川口_3R', '不成立', 0, '不成立', 0, '不成立'),
                        ('20261101_川口_4R', NULL, 1, NULL, 1, ''),
                        ('20261101_川口_5R', NULL, 1, NULL, 1, '');
                    INSERT INTO result_entries VALUES
                        ('20261101_川口_1R', 1, 1, '通常'),
                        ('20261101_川口_1R', 2, 2, '通常'),
                        ('20261101_川口_1R', 3, 3, '通常'),
                        ('20261101_川口_1R', 4, NULL, '競走中止等'),
                        ('20261101_川口_2R', 1, 1, '通常'),
                        ('20261101_川口_2R', 2, NULL, '通常'),
                        ('20261101_川口_4R', 1, 1, '通常');
                    INSERT INTO result_laps VALUES
                        ('20261101_川口_1R', '1周目', 1, 1, 1),
                        ('20261101_川口_1R', '1周目', 1, 2, 2),
                        ('20261101_川口_1R', '1周目', 1, 3, 3),
                        ('20261101_川口_1R', '2周目', 2, 1, 1),
                        ('20261101_川口_1R', '2周目', 2, 2, 2),
                        ('20261101_川口_1R', '2周目', 2, 3, 3),
                        ('20261101_川口_1R', 'ゴール線', 99, 1, 1),
                        ('20261101_川口_1R', 'ゴール線', 99, 2, 2),
                        ('20261101_川口_2R', '1周目', 1, 1, 1),
                        ('20261101_川口_2R', 'ゴール線', 99, 1, 1);
                    INSERT INTO result_payouts VALUES
                        ('20261101_川口_1R', '3連単', '1-2-3', 1230),
                        ('20261101_川口_2R', '単勝', '1', 100),
                        ('20261101_川口_3R', '単勝', '1', 0),
                        ('20261101_川口_4R', '3連単', '1-2-3', 1230),
                        ('20261101_川口_5R', '3連単', '1-2-3', 1230);
                """)

            goal_rows = [
                {
                    "race_key": "20261101_川口_1R", "n_entries": "4",
                    "n_laps": "8", "n_payouts": "1", "race_status": "",
                    "incomplete_flag": "1", "incomplete_reason": "goal_line_shortage",
                },
                {
                    "race_key": "20261101_川口_2R", "n_entries": "2",
                    "n_laps": "2", "n_payouts": "1", "race_status": "",
                    "incomplete_flag": "1", "incomplete_reason": "goal_line_shortage",
                },
            ]
            zero_rows = [
                {
                    "race_key": "20261101_川口_3R", "n_entries": "0",
                    "n_laps": "0", "n_payouts": "1", "race_status": "不成立",
                    "incomplete_flag": "0", "incomplete_reason": "entries_empty",
                },
                {
                    "race_key": "20261101_川口_4R", "n_entries": "1",
                    "n_laps": "0", "n_payouts": "1", "race_status": "",
                    "incomplete_flag": "1", "incomplete_reason": "laps_empty",
                },
                {
                    "race_key": "20261101_川口_5R", "n_entries": "0",
                    "n_laps": "0", "n_payouts": "1", "race_status": "",
                    "incomplete_flag": "0", "incomplete_reason": "entries_empty",
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
            self.assertEqual(summary["review_race_keys"], [
                "20261101_川口_1R",
                "20261101_川口_2R",
                "20261101_川口_4R",
                "20261101_川口_5R",
            ])
            self.assertEqual(summary["flagged_row_count_by_table"], {
                "result_entries": 7, "result_laps": 10,
                "result_payouts": 4, "result_races": 4,
            })

            with (out / "result_registration_lap_group_detail.csv").open(
                encoding="utf-8-sig", newline=""
            ) as f:
                lap_rows = list(csv.DictReader(f))
            first_lap = next(
                row for row in lap_rows
                if row["race_key"] == "20261101_川口_1R" and row["lap_no"] == "1"
            )
            self.assertEqual(first_lap["missing_entry_car_nos_json"], '["4"]')
            self.assertEqual(first_lap["missing_finisher_car_nos_json"], "[]")
            goal_line = next(
                row for row in lap_rows
                if row["race_key"] == "20261101_川口_1R" and row["lap_label"] == "ゴール線"
            )
            self.assertEqual(goal_line["missing_finisher_car_nos_json"], '["3"]')

            with (out / "result_registration_race_classification.csv").open(
                encoding="utf-8-sig", newline=""
            ) as f:
                classifications = {
                    row["race_key"]: row for row in csv.DictReader(f)
                }
            self.assertEqual(
                classifications["20261101_川口_1R"]["finisher_based_classification"],
                "goal_line_finisher_mismatch",
            )
            b = classifications["20261101_川口_2R"]
            self.assertEqual(b["finisher_based_classification"], "lap_data_matches_finishers")
            self.assertEqual(b["nonfinisher_count"], "1")
            self.assertEqual(b["nonfinisher_status_review_car_nos_json"], '["2"]')
            self.assertEqual(
                b["goal_line_missing_finisher_car_nos_json"], "[]"
            )
            self.assertEqual(
                classifications["20261101_川口_4R"]["finisher_based_classification"],
                "laps_empty",
            )
            self.assertEqual(
                classifications["20261101_川口_5R"]["finisher_based_classification"],
                "entries_empty_status_unknown",
            )

            detail_summary = json.loads(
                (out / "result_registration_detail_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(detail_summary["database_in_artifact"])
            self.assertEqual(
                detail_summary["finisher_based_classification_counts"],
                {
                    "entries_empty_status_unknown": 1,
                    "goal_line_finisher_mismatch": 1,
                    "lap_data_matches_finishers": 1,
                    "laps_empty": 1,
                },
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Regression tests for the Ver35 official result race-time parser.

Run:
  python tests/test_v35_parse_entries_race_time.py
"""
import importlib.util
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = ROOT / "engine.py"
SPEC = importlib.util.spec_from_file_location("engine_result_time_test", ENGINE_PATH)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


def make_race_text(first_race_time, race_key):
    """Six-entry fixture in the same converted-text shape as the official API path."""
    rows = [
        ("A", first_race_time, "0.11"),
        ("B", "3.462", "0.08"),
        ("C", "3.471", "0.15"),
        ("D", "3.488", "0.20"),
        ("E", "3.501", "0.24"),
        ("F", "3.519", "0.31"),
    ]
    lines = [
        f"着順 車番 選手名 ({race_key})",
        "LG/ハンデ/試走T 競走T（人気） ST/事故",
    ]
    for finish, (name, race_time, st) in enumerate(rows, start=1):
        lines.extend([
            f"{finish} {finish} {name}",
            "飯塚/ 0m / 3.40",
            f"{race_time}(-)",
            st,
        ])
    lines.append("グランドノート")
    return "\n".join(lines)


class TestV35ParseEntriesRaceTime(unittest.TestCase):
    def test_hyphen_popularity_parses_race_time_st_and_nan_popularity(self):
        parsed = engine._v35_parse_entries(make_race_text("3.455", "minus"))
        first = parsed.iloc[0]
        self.assertAlmostEqual(first["競走T"], 3.455)
        self.assertAlmostEqual(first["ST"], 0.11)
        self.assertTrue(parsed["人気"].isna().all())

    def test_numeric_popularity_remains_backward_compatible(self):
        text = make_race_text("3.455", "numeric").replace("3.455(-)", "3.455(1)", 1)
        parsed = engine._v35_parse_entries(text)
        first = parsed.iloc[0]
        self.assertAlmostEqual(first["競走T"], 3.455)
        self.assertAlmostEqual(first["ST"], 0.11)
        self.assertEqual(first["人気"], 1)

    def test_race_time_without_popularity_parentheses_keeps_existing_behavior(self):
        text = make_race_text("3.455", "no-parentheses").replace("3.455(-)", "3.455", 1)
        parsed = engine._v35_parse_entries(text)
        first = parsed.iloc[0]
        self.assertTrue(math.isnan(first["競走T"]))
        self.assertTrue(math.isnan(first["ST"]))

    def test_four_six_entry_fixtures_parse_race_time_and_st(self):
        fixtures = [
            ("2026-10-20_Iizuka_1R", "3.455"),
            ("2026-10-21_Iizuka_4R", "3.650"),
            ("2026-10-21_Iizuka_5R", "3.629"),
            ("2026-10-22_Iizuka_1R", "3.463"),
        ]
        for race_key, first_time in fixtures:
            with self.subTest(race=race_key):
                parsed = engine._v35_parse_entries(make_race_text(first_time, race_key))
                self.assertEqual(len(parsed), 6)
                self.assertEqual(int(parsed["競走T"].notna().sum()), 6)
                self.assertEqual(int(parsed["ST"].notna().sum()), 6)
                self.assertEqual(int(parsed["人気"].notna().sum()), 0)
                self.assertTrue(parsed["人気"].isna().all())

    def test_nan_popularity_survives_result_save_read_and_display(self):
        parsed = engine._v35_parse_entries(make_race_text("3.455", "db-roundtrip"))
        self.assertTrue(parsed["人気"].isna().all())
        meta = {
            "開催日": "2026-10-20",
            "開催場": "飯塚",
            "レース": 1,
            "走路状態": None,
            "走路温度": None,
            "気温": None,
            "湿度": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "result.sqlite3")
            engine.v35_save_result_and_analyze(meta, parsed, db_path=db_path)
            with sqlite3.connect(db_path) as con:
                saved = con.execute(
                    "SELECT car_no, race_time, start_time FROM result_entries ORDER BY finish"
                ).fetchall()
            self.assertEqual(len(saved), 6)
            self.assertAlmostEqual(saved[0][1], 3.455)
            self.assertAlmostEqual(saved[0][2], 0.11)
        rendered = parsed[["車番", "競走T", "ST", "人気"]].to_string(index=False)
        self.assertIn("3.455", rendered)
        self.assertIn("0.11", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)

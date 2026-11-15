import sqlite3
import unittest


SQL = """
    SELECT r.race_key
    FROM result_races AS r
    LEFT JOIN v223_result_view_restore AS v
      ON v.race_key = r.race_key
    ORDER BY julianday(COALESCE(v.updated_at, r.registered_at)) DESC,
             r.race_key DESC
"""


class CoalesceTimestampSortTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE result_races (race_key TEXT PRIMARY KEY, registered_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE v223_result_view_restore (race_key TEXT PRIMARY KEY, updated_at TEXT)"
        )

    def tearDown(self):
        self.db.close()

    def add_race(self, race_key, registered_at, restored_at=None):
        self.db.execute(
            "INSERT INTO result_races (race_key, registered_at) VALUES (?, ?)",
            (race_key, registered_at),
        )
        if restored_at is not None:
            self.db.execute(
                "INSERT INTO v223_result_view_restore (race_key, updated_at) VALUES (?, ?)",
                (race_key, restored_at),
            )

    def ordered_keys(self):
        return [
            row[0]
            for row in self.db.execute(SQL).fetchall()
        ]

    def test_utc_naive_timestamps_sort_chronologically(self):
        self.add_race("older", "2026-11-15T16:49:08")
        self.add_race("newer", "2026-11-15T16:50:08")
        self.assertEqual(self.ordered_keys(), ["newer", "older"])

    def test_offset_timestamps_sort_chronologically(self):
        self.add_race("older", "2026-11-16T01:49:08+09:00",
                      "2026-11-16T01:49:08+09:00")
        self.add_race("newer", "2026-11-16T01:50:08+09:00",
                      "2026-11-16T01:50:08+09:00")
        self.assertEqual(self.ordered_keys(), ["newer", "older"])

    def test_mixed_utc_naive_and_offset_timestamps_sort_by_instant(self):
        # The restored row is one minute newer than the UTC-naive row.
        self.add_race("base_older", "2026-11-15T16:48:08")
        self.add_race("base", "2026-11-15T16:49:08")
        self.add_race("restored_newer", "2026-11-15T16:40:00",
                      "2026-11-16T01:50:08+09:00")
        self.assertEqual(
            self.ordered_keys(),
            ["restored_newer", "base", "base_older"],
        )

    def test_naive_utc_and_offset_jst_represent_same_instant(self):
        naive, offset = self.db.execute(
            "SELECT julianday(?), julianday(?)",
            ("2026-11-15T16:49:08", "2026-11-16T01:49:08+09:00"),
        ).fetchone()
        self.assertAlmostEqual(naive, offset, places=9)

    def test_null_timestamp_sorts_last_for_descending_order(self):
        self.add_race("valid", "2026-11-15T16:49:08")
        self.add_race("null_time", None)
        self.assertEqual(self.ordered_keys(), ["valid", "null_time"])


if __name__ == "__main__":
    unittest.main()

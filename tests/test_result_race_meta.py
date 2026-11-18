from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("engine_under_test", ROOT / "engine.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

META = {
    "開催日": "2026-11-18", "開催場": "川口", "レース": 5,
    "レース名": "一般戦", "レース種別": "一般戦", "距離": 3100,
    "周回数": 6, "発走時刻": "12:34", "時間帯": "昼", "天候": "晴",
    "走路状態": "良走路", "走路温度": 25.0, "気温": 20.0, "湿度": 55.0,
    "1着賞金": None,
}
ROWS = engine.pd.DataFrame([
    {"着順": 1, "車番": 1, "選手名": "選手A", "ハンデ": 0, "試走T": 3.40, "競走T": 3.48, "ST": 0.08, "結果区分": "通常"},
    {"着順": 2, "車番": 2, "選手名": "選手B", "ハンデ": 10, "試走T": 3.42, "競走T": 3.50, "ST": 0.10, "結果区分": "通常"},
    {"着順": 3, "車番": 3, "選手名": "選手C", "ハンデ": 20, "試走T": 3.45, "競走T": 3.55, "ST": 0.12, "結果区分": "通常"},
])
EXPECTED = ("一般戦", "一般戦", 3100, 6, "12:34", "昼", "晴", None)


def test_result_race_meta_and_nullable_prize():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db = f.name
    engine.v35_save_result_and_analyze(META, ROWS, db_path=db)
    with sqlite3.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(result_races)")}
        assert {"race_name", "race_type", "distance_m", "total_laps", "start_time_text", "time_band", "weather", "prize_yen"} <= cols
        row = con.execute(
            """SELECT race_name,race_type,distance_m,total_laps,start_time_text,time_band,weather,prize_yen
               FROM result_races WHERE race_key=?""", ("20261118_川口_5R",)
        ).fetchone()
        assert row == EXPECTED


def test_old_result_races_schema_is_migrated_without_losing_existing_row():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db = f.name
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE result_races (
            race_key TEXT PRIMARY KEY, race_date TEXT, venue TEXT, race_no TEXT,
            surface TEXT, track_temp REAL, air_temp REAL, humidity REAL, registered_at TEXT
        )""")
        con.execute("INSERT INTO result_races VALUES (?,?,?,?,?,?,?,?,?)", ("20261118_川口_5R", "2026-11-18", "川口", "5", "良走路", 25, 20, 55, "old"))
        con.commit()
    engine.v35_save_result_and_analyze(META, ROWS, db_path=db)
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT race_name,distance_m,prize_yen FROM result_races WHERE race_key=?", ("20261118_川口_5R",)).fetchone()
        assert row == ("一般戦", 3100, None)


def test_null_reregistration_preserves_existing_meta():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db = f.name

    engine.v35_save_result_and_analyze(META, ROWS, db_path=db)
    null_meta = {
        **META,
        "レース名": None,
        "レース種別": None,
        "距離": None,
        "周回数": None,
        "発走時刻": None,
        "時間帯": None,
        "天候": None,
        "1着賞金": None,
    }
    engine.v35_save_result_and_analyze(null_meta, ROWS, db_path=db)

    with sqlite3.connect(db) as con:
        row = con.execute(
            """SELECT race_name,race_type,distance_m,total_laps,start_time_text,time_band,weather,prize_yen
               FROM result_races WHERE race_key=?""",
            ("20261118_川口_5R",),
        ).fetchone()
    assert row == EXPECTED

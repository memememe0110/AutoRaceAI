import sqlite3

class Database:
    def __init__(self, path="autorace.db"):
        self.path = path

    def connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def initialize(self):
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS races(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_date TEXT NOT NULL DEFAULT '',
                venue TEXT NOT NULL DEFAULT '',
                race_no INTEGER,
                raw_text TEXT,
                UNIQUE(race_date, venue, race_no)
            );
            CREATE TABLE IF NOT EXISTS race_entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id INTEGER NOT NULL,
                car_no INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                handicap INTEGER,
                trial_time REAL,
                start_time REAL,
                UNIQUE(race_id, car_no)
            );
            """)

    def save_race(self, race):
        self.initialize()
        with self.connect() as con:
            con.execute("""
                INSERT INTO races(race_date,venue,race_no,raw_text)
                VALUES(?,?,?,?)
                ON CONFLICT(race_date,venue,race_no)
                DO UPDATE SET raw_text=excluded.raw_text
            """, (race.get("date",""), race.get("venue",""), race.get("race_no"), race.get("raw_text","")))
            row = con.execute(
                "SELECT id FROM races WHERE race_date=? AND venue=? AND race_no IS ?",
                (race.get("date",""), race.get("venue",""), race.get("race_no"))
            ).fetchone()
            race_id = int(row["id"])
            for p in race.get("players", []):
                con.execute("""
                    INSERT INTO race_entries(race_id,car_no,player_name,handicap,trial_time,start_time)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(race_id,car_no) DO UPDATE SET
                    player_name=excluded.player_name,
                    handicap=excluded.handicap,
                    trial_time=excluded.trial_time,
                    start_time=excluded.start_time
                """, (race_id,p["車番"],p["選手名"],p.get("ハンデ"),p.get("試走T"),p.get("ST")))
            return race_id

    def stats(self):
        self.initialize()
        with self.connect() as con:
            return {
                "races": con.execute("SELECT COUNT(*) FROM races").fetchone()[0],
                "entries": con.execute("SELECT COUNT(*) FROM race_entries").fetchone()[0],
            }

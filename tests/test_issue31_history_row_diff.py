import json, sqlite3, tempfile, unittest
from pathlib import Path
from tools.audit_issue31_history_row_diff import audit, cell_value

class HistoryRowDiffTests(unittest.TestCase):
    def _snapshot(self, root, name, generation, rows):
        db = root / f"{name}.sqlite3"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE v231_prediction_history (
            history_id INTEGER PRIMARY KEY, race_key TEXT, app_version TEXT,
            prediction_time TEXT, settings_hash TEXT, seed INTEGER, trials INTEGER,
            raw_text TEXT, payload BLOB
        )""")
        con.executemany("INSERT INTO v231_prediction_history VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.commit(); con.close()
        info = root / f"{name}.json"
        info.write_text(json.dumps({"manifest_generation":str(generation),
            "commit_sha":f"commit-{generation}","manifest_saved_at":f"2026-11-{generation:02d}T00:00:00+09:00",
            "db_sha256":f"dbsha-{generation}","manifest_blob_sha":f"manifest-{generation}"}), encoding="utf-8")
        return db, info

    def test_deleted_and_added_rows_and_root_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            a,ia=self._snapshot(root,"a",675,[
                (5856,"20260726_飯塚_1R","Ver323","2026-10-01","old",1,20000,"secret-text",b"blob"),
                (7000,"20260801_川口_1R","Ver326","2026-10-02","new",2,20000,"keep",b"x"),
                (7001,"20260801_川口_2R","Ver327","2026-10-03","new",3,20000,"delete",b"y")])
            b,ib=self._snapshot(root,"b",676,[
                (7000,"20260801_川口_1R","Ver326","2026-10-02","new",2,20000,"keep",b"x"),
                (8000,"20260801_川口_3R","Ver326","2026-10-04","new",4,20000,"add",b"z")])
            refs=root/"roots.json"
            refs.write_text(json.dumps([{"source_history_id":5856,"race_key":"20260726_飯塚_1R"}]),encoding="utf-8")
            out=root/"out"
            got=audit(str(a),str(ia),"675","commit-675",str(b),str(ib),"676","commit-676",str(refs),str(out))
            self.assertEqual(got["deleted_count"],2)
            self.assertEqual(got["added_count"],1)
            self.assertEqual(got["retained_count"],1)
            self.assertEqual(got["changed_retained_count"],0)
            self.assertEqual(got["deleted_external_root_ids"],[5856])
            csv=(out/"deleted_history_rows.csv").read_text(encoding="utf-8-sig")
            self.assertIn("is_external_root_37",csv)
            self.assertNotIn("secret-text",csv)

    def test_large_text_is_hashed_not_exported(self):
        v=cell_value("raw_text","x"*5000)
        self.assertEqual(v["text_len"],5000)
        self.assertEqual(len(v["sha256"]),64)

if __name__ == "__main__":
    unittest.main()

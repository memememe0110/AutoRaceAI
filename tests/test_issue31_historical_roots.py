import tempfile, unittest
from pathlib import Path
from tools.audit_issue31_historical_roots import compare, raw_sha

class HistoricalRootTests(unittest.TestCase):
    def test_matching_parent_fields(self):
        parent={"race_key":"r1","app_version":"Ver326","prediction_time":"2026-10-15T09:00:00+09:00",
                "settings_hash":"s","seed":20260719,"trials":20000,"raw_text":"raw"}
        expected={"race_key":"r1","child_app_version":"Ver326","child_settings_hash":"s",
                  "child_seed":"20260719","child_trials":"20000","child_raw_text_sha256":raw_sha("raw")}
        got=compare(parent,expected)
        self.assertTrue(got["source_row_found"])
        self.assertTrue(all(got[k] for k in ("same_race","same_app_version","same_settings_hash","same_seed","same_trials","same_raw_text_sha256")))
    def test_missing_parent(self):
        self.assertEqual(compare(None,{}),{"source_row_found":False})
    def test_empty_raw_text_hash(self):
        self.assertEqual(raw_sha(""),"")
if __name__=="__main__": unittest.main()

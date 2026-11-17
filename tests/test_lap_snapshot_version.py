import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from ast import Module, parse, FunctionDef


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _load_snapshot_saver():
    source = APP_PATH.read_text(encoding="utf-8")
    tree = parse(source)
    wanted = {"_v252_ensure_lap_tables", "_v252_save_lap_prediction"}
    nodes = [node for node in tree.body if isinstance(node, FunctionDef) and node.name in wanted]
    if {node.name for node in nodes} != wanted:
        raise AssertionError("Lap snapshot save functions not found")
    ns = {
        "__builtins__": __builtins__,
        "sqlite3": sqlite3,
        "Path": Path,
        "re": re,
        "threading": threading,
        "_V253_RECONSTRUCTION_MODE": False,
        "_V231_APP_VERSION": "Ver326",
        "_V252_LAP_RESIDUAL_CACHE": {},
        "_v326_effective_save_version": lambda: "Ver330-A",
    }
    exec(compile(Module(body=nodes, type_ignores=[]), str(APP_PATH), "exec"), ns)
    return ns


class LapSnapshotVersionTests(unittest.TestCase):
    def test_default_version_uses_effective_worker_version_not_module_constant(self):
        ns = _load_snapshot_saver()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.sqlite3"
            sqlite3.connect(db).close()
            result = ns["_v252_save_lap_prediction"](
                str(db),
                {"開催日": "2026-11-14", "開催場": "山陽", "R": 4},
                [
                    {"lap": 1, "order": "1-2-3-4-5-6-7-8", "support": 0.75},
                    {"lap": 2, "order": "1-2-3-4-5-6-7-8", "support": 0.60},
                ],
                False,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["app_version"], "Ver330-A")
            with sqlite3.connect(db) as con:
                versions = {
                    row[0] for row in con.execute(
                        "SELECT DISTINCT app_version FROM v252_lap_prediction_snapshots"
                    )
                }
            self.assertEqual(versions, {"Ver330-A"})

    def test_explicit_source_version_override_is_preserved(self):
        ns = _load_snapshot_saver()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.sqlite3"
            sqlite3.connect(db).close()
            result = ns["_v252_save_lap_prediction"](
                str(db),
                {"開催日": "2026-11-14", "開催場": "山陽", "R": 4},
                [{"lap": 1, "order": "1-2-3-4-5-6-7-8", "support": 0.75}],
                False,
                app_version_override="Ver326",
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["app_version"], "Ver326")

    def test_reconstruction_mode_keeps_ver253_tag(self):
        ns = _load_snapshot_saver()
        ns["_V253_RECONSTRUCTION_MODE"] = True
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.sqlite3"
            sqlite3.connect(db).close()
            result = ns["_v252_save_lap_prediction"](
                str(db),
                {"開催日": "2026-11-14", "開催場": "山陽", "R": 4},
                [{"lap": 1, "order": "1-2-3-4-5-6-7-8", "support": 0.75}],
                False,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["app_version"], "Ver253")


if __name__ == "__main__":
    unittest.main(verbosity=2)

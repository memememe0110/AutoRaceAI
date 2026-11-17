import ast
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
GLOBAL_NAMES = {
    "_V330A_EXPERIMENT",
    "_V331_LAP1_TUNE",
    "_V287_IMPORT_STATE_RESET",
    "_V287_AUTO_IMPORT_STATE",
}


def _app_source():
    return APP_PATH.read_text(encoding="utf-8")


def _top_level_nodes(source):
    tree = ast.parse(source)
    wanted = {
        "_V326_QUEUE_WORKER_LOCAL",
        "_v326_worker_config_snapshot",
        "_v326_set_worker_config_snapshot",
        "_V287_IMPORT_STATE_LOCAL",
        "_v287_set_import_state_reset",
        "_v287_get_import_state_reset",
        "_v331_lap1_tune_active",
        "_v326_ca_active",
        "_v327_active",
        "_v328_active",
        "_v326_validate_queue_config_row",
    }
    found = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for name in names:
            if name in wanted:
                found[name] = node
    missing = wanted - set(found)
    if missing:
        raise AssertionError(f"app.py definitions missing: {sorted(missing)}")
    return [found[name] for name in (
        "_V326_QUEUE_WORKER_LOCAL",
        "_v326_worker_config_snapshot",
        "_v326_set_worker_config_snapshot",
        "_V287_IMPORT_STATE_LOCAL",
        "_v287_set_import_state_reset",
        "_v287_get_import_state_reset",
        "_v331_lap1_tune_active",
        "_v326_ca_active",
        "_v327_active",
        "_v328_active",
        "_v326_validate_queue_config_row",
    )]


def _load_state_helpers(source):
    ns = {
        "__builtins__": __builtins__,
        "threading": threading,
        "os": os,
        "st": SimpleNamespace(session_state={}),
        "_V330A_EXPERIMENT": True,
        "_V331_LAP1_TUNE": True,
        "json": __import__("json"),
    }
    module = ast.Module(body=_top_level_nodes(source), type_ignores=[])
    exec(compile(module, str(APP_PATH), "exec"), ns)
    return ns


def _find_function(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _global_writes(function):
    writes = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Global):
            writes.update(set(node.names) & GLOBAL_NAMES)
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "globals"
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in GLOBAL_NAMES
            ):
                writes.add(node.slice.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id in GLOBAL_NAMES:
                if node.func.attr in {"clear", "update", "pop", "setdefault"}:
                    writes.add(owner.id)
    return writes


class WorkerGlobalIsolationTests(unittest.TestCase):
    def test_workers_do_not_write_shared_experiment_or_calibration_globals(self):
        tree = ast.parse(_app_source())
        for name in (
            "_v319_bg_import_worker",
            "_v279_bg_prediction_worker",
            "_v279_bg_prediction_start",
        ):
            with self.subTest(worker=name):
                self.assertEqual(_global_writes(_find_function(tree, name)), set())

    def test_calibration_baseline_and_worker_snapshot_are_thread_local(self):
        ns = _load_state_helpers(_app_source())
        barrier = threading.Barrier(3)
        release = threading.Event()
        seen = {}

        def worker(label, version, lap1):
            ns["_v326_set_worker_config_snapshot"]({
                "save_version": version,
                "v331_lap1_tune_enabled": lap1,
            })
            ns["_v287_set_import_state_reset"]({"owner": label})
            barrier.wait(timeout=5)
            release.wait(timeout=5)
            seen[label] = {
                "snapshot": ns["_v326_worker_config_snapshot"](),
                "baseline": ns["_v287_get_import_state_reset"](),
            }
            ns["_v326_set_worker_config_snapshot"](None)
            ns["_v287_set_import_state_reset"](None)

        threads = [
            threading.Thread(target=worker, args=("A", "Ver326", False)),
            threading.Thread(target=worker, args=("B", "Ver330-A", True)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)

        # Main/UI thread must not see either Worker snapshot or calibration baseline.
        self.assertIsNone(ns["_v326_worker_config_snapshot"]())
        self.assertEqual(ns["_v287_get_import_state_reset"](), {})
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(seen["A"]["snapshot"]["save_version"], "Ver326")
        self.assertFalse(seen["A"]["snapshot"]["v331_lap1_tune_enabled"])
        self.assertEqual(seen["A"]["baseline"], {"owner": "A"})
        self.assertEqual(seen["B"]["snapshot"]["save_version"], "Ver330-A")
        self.assertTrue(seen["B"]["snapshot"]["v331_lap1_tune_enabled"])
        self.assertEqual(seen["B"]["baseline"], {"owner": "B"})

    def test_worker_snapshot_overrides_all_model_experiment_globals(self):
        ns = _load_state_helpers(_app_source())
        ns.update({
            "_V326_ENABLE_CA": True,
            "_V327_LATE_RELEASE_EXPERIMENT": True,
            "_V328_EXPERIMENT": True,
        })
        with mock.patch.dict(os.environ, {"AUTORACE_VER330A_COMPARE": ""}, clear=False):
            ns["_v326_set_worker_config_snapshot"]({
                "save_version": "Ver326",
                "v326_ca_enabled": False,
                "v327_enabled": False,
                "v328_enabled": False,
            })
            self.assertFalse(ns["_v326_ca_active"]())
            self.assertFalse(ns["_v327_active"]())
            self.assertFalse(ns["_v328_active"]())

            ns["_v326_set_worker_config_snapshot"]({
                "save_version": "Ver328",
                "v326_ca_enabled": True,
                "v327_enabled": True,
                "v328_enabled": True,
            })
            self.assertTrue(ns["_v326_ca_active"]())
            self.assertTrue(ns["_v327_active"]())
            self.assertTrue(ns["_v328_active"]())

    def test_legacy_queue_flags_are_reconstructed_from_saved_version(self):
        ns = _load_state_helpers(_app_source())
        ver330 = ns["_v326_validate_queue_config_row"]({
            "save_version": "Ver330-A",
            "config_json": "{}",
            "seed": 123,
        })
        self.assertTrue(ver330["config_json"]["v326_ca_enabled"])
        self.assertFalse(ver330["config_json"]["v327_enabled"])
        self.assertFalse(ver330["config_json"]["v328_enabled"])
        self.assertTrue(ver330["v330a_enabled"])
        self.assertFalse(ver330["config_json"]["v331_lap1_tune_enabled"])

        ver328 = ns["_v326_validate_queue_config_row"]({
            "save_version": "Ver328",
            "config_json": "{}",
            "seed": 123,
        })
        self.assertTrue(ver328["config_json"]["v326_ca_enabled"])
        self.assertTrue(ver328["config_json"]["v328_enabled"])
        self.assertFalse(ver328["config_json"]["v330a_enabled"])

    def test_lap1_flag_uses_worker_snapshot_before_ui_global(self):
        ns = _load_state_helpers(_app_source())
        with mock.patch.dict(os.environ, {"AUTORACE_331_LAP1_TUNE": ""}, clear=False):
            ns["_V331_LAP1_TUNE"] = True
            ns["_v326_set_worker_config_snapshot"]({
                "save_version": "Ver326",
                "v331_lap1_tune_enabled": False,
            })
            self.assertFalse(ns["_v331_lap1_tune_active"]())

            ns["_v326_set_worker_config_snapshot"]({
                "save_version": "Ver331",
                "v331_lap1_tune_enabled": True,
            })
            self.assertTrue(ns["_v331_lap1_tune_active"]())

            ns["_v326_set_worker_config_snapshot"](None)
            self.assertTrue(ns["_v331_lap1_tune_active"]())

    def test_official_import_worker_forces_lap1_tune_off(self):
        source = _app_source()
        tree = ast.parse(source)
        worker = _find_function(tree, "_v319_bg_import_worker")
        worker_text = ast.get_source_segment(source, worker)
        self.assertIn('_cfg["v331_lap1_tune_enabled"] = False', worker_text)
        self.assertLess(
            worker_text.index('_cfg["v331_lap1_tune_enabled"] = False'),
            worker_text.index("_v326_set_worker_config_snapshot(_cfg)"),
        )

    def test_legacy_queue_config_defaults_lap1_flag_off(self):
        source = _app_source()
        tree = ast.parse(source)
        fn = _find_function(tree, "_v326_validate_queue_config_row")
        text = ast.get_source_segment(source, fn)
        self.assertIn('"v331_lap1_tune_enabled"] = False', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

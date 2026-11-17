"""force_history_refresh: 充足スキップ解除・結果置換との独立性・再構築・失敗時保護。"""
from __future__ import annotations

import os
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Dummy:
    def __getattr__(self, name):
        return self

    def __call__(self, *a, **k):
        if a and callable(a[0]):
            return a[0]
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __str__(self):
        return ""


class _ST(types.ModuleType):
    def __getattr__(self, name):
        d = _Dummy()
        setattr(self, name, d)
        return d


def _install_streamlit():
    if "streamlit" in sys.modules:
        return
    st = _ST("streamlit")
    st.session_state = {}
    st.query_params = {}
    st.sidebar = _Dummy()
    comps = _ST("streamlit.components")
    v1 = _ST("streamlit.components.v1")
    st.components = comps
    comps.v1 = v1
    sys.modules["streamlit"] = st
    sys.modules["streamlit.components"] = comps
    sys.modules["streamlit.components.v1"] = v1


_install_streamlit()
import app  # noqa: E402


def _old_queue(db: str) -> None:
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE v326_import_queue (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            task_mode TEXT NOT NULL DEFAULT 'normal',
            ymd TEXT NOT NULL,
            venue TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            sort_order INTEGER NOT NULL DEFAULT 0,
            message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            fetch_odds INTEGER NOT NULL DEFAULT 1,
            register_result INTEGER NOT NULL DEFAULT 1,
            skip_existing INTEGER NOT NULL DEFAULT 1,
            replace_existing INTEGER NOT NULL DEFAULT 0,
            predict_if_missing INTEGER NOT NULL DEFAULT 1,
            force_history_refresh INTEGER NOT NULL DEFAULT 0,
            UNIQUE (job_type, ymd, venue, race_no)
        )
        """
    )
    con.execute(
        """
        INSERT INTO v326_import_queue
            (job_type, task_mode, ymd, venue, race_no, created_at, updated_at, force_history_refresh)
        VALUES ('official_import', 'normal', '20261114', '山陽', 10, 't', 't', 1)
        """
    )
    con.commit()
    con.close()


def test_step2_rebuild_keeps_force_history_refresh(tmp_path):
    db = str(tmp_path / "q.db")
    _old_queue(db)
    app._v326_ensure_import_queue(db)
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(v326_import_queue)")]
    val = con.execute(
        "SELECT force_history_refresh FROM v326_import_queue WHERE race_no=10"
    ).fetchone()[0]
    idxs = con.execute("PRAGMA index_list(v326_import_queue)").fetchall()
    uniq_cols = []
    for idx in idxs:
        if int(idx[2] or 0) != 1:
            continue
        uniq_cols = [
            str(r[2] or "")
            for r in con.execute(f'PRAGMA index_info("{idx[1]}")')
        ]
        break
    con.close()
    assert "force_history_refresh" in cols
    assert int(val) == 1
    assert uniq_cols == ["job_type", "task_mode", "ymd", "venue", "race_no"]


def test_fill_failure_keeps_existing_history(tmp_path, monkeypatch):
    db = str(tmp_path / "h.db")
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE v15_player_history_imports (
            history_key TEXT PRIMARY KEY,
            player_name TEXT,
            race_date TEXT,
            venue TEXT,
            race_no INTEGER
        )
        """
    )
    con.execute(
        "INSERT INTO v15_player_history_imports VALUES ('k1','山田','2026-11-01','山陽',1)"
    )
    con.commit()
    con.close()

    def boom(*a, **k):
        raise RuntimeError("official down")

    monkeypatch.setattr(app, "_v319_fetch_player_history_df", boom)
    monkeypatch.setattr(app, "_v319_history_from_results", lambda *a, **k: None)
    monkeypatch.setattr(
        app,
        "_v319_player_history_latest_fast",
        lambda *a, **k: (15, "20261110"),
    )
    monkeypatch.setattr(
        app,
        "_v319_player_history_state",
        lambda *a, **k: {"keys": {("20261110", "山陽", 1)}},
    )
    monkeypatch.setattr(app, "_v319_persistent_skip_lookup", lambda *a, **k: None)

    out = app._v319_fill_player_histories(
        db,
        [{"name": "山田", "player_cd": "123"}],
        min_rows=5,
        before_ymd="20261114",
        before_venue="山陽",
        before_race=10,
        force_refresh=True,
    )
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM v15_player_history_imports").fetchone()[0]
    con.close()
    assert n == 1
    assert int(out.get("errors") or 0) >= 1
    assert any("取得失敗" in str(d) or "取得開始" in str(d) for d in out.get("details") or [])


def _run_import(monkeypatch, *, force: bool, replace: bool, counts: int = 15):
    calls = {"fill": 0, "fill_force": None, "v70": 0, "register": 0}

    def fill(*a, **k):
        calls["fill"] += 1
        calls["fill_force"] = bool(k.get("force_refresh"))
        return {"added": 1, "skipped": 0, "errors": 0, "details": ["取得実行"]}

    monkeypatch.setattr(app, "_v319_fetch_sprace_html", lambda *a, **k: "<html>card</html>")
    monkeypatch.setattr(
        app,
        "_v319_parse_sprace_car_rows",
        lambda *a, **k: [{
            "name": "山田", "player_cd": "123", "hand": "0",
            "car": 1, "trial": "3.40", "st": "0.15",
        }],
    )
    monkeypatch.setattr(app, "_v319_parse_sprace_players", lambda *a, **k: [])
    monkeypatch.setattr(app, "_v319_build_prediction_text_from_sprace", lambda *a, **k: "1車")
    monkeypatch.setattr(app, "_v319_detect_kessha_from_players", lambda *a, **k: [])
    monkeypatch.setattr(app, "_v319_detect_kessha_from_card_text", lambda *a, **k: [])
    monkeypatch.setattr(app, "_v319_autorace_other_race_info", lambda *a, **k: {})
    monkeypatch.setattr(app, "_v319_http_get", lambda *a, **k: "")
    monkeypatch.setattr(app, "_v319_autorace_api_post", lambda *a, **k: {})
    monkeypatch.setattr(app, "_v326_queue_bind_input_sha256", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(app, "_v319_autorace_program_body", lambda *a, **k: {})
    monkeypatch.setattr(app, "_v319_is_void_or_cancelled_result", lambda *a, **k: {"void": False})
    monkeypatch.setattr(app, "_v319_card_fill_note", lambda *a, **k: "")
    monkeypatch.setattr(app, "_v319_player_history_counts_batch", lambda *a, **k: {"山田": counts})
    monkeypatch.setattr(app, "_v319_fill_player_histories", fill)
    monkeypatch.setattr(app, "_v319_result_exists", lambda *a, **k: True)
    monkeypatch.setattr(app, "_v319_prediction_exists", lambda *a, **k: True)
    monkeypatch.setattr(app, "_v319_prediction_entry_count", lambda *a, **k: 1)
    monkeypatch.setattr(app, "_v319_prediction_matches_current_version", lambda *a, **k: True)
    monkeypatch.setattr(app, "_v326_find_matching_prediction_history", lambda *a, **k: None)
    monkeypatch.setattr(app, "_v319_apply_odds_and_plan", lambda *a, **k: {"odds": 3, "plan_hash": "p"})
    monkeypatch.setattr(app, "_v319_fetch_oddspark_result", lambda *a, **k: "1R\n確定\n着順\n払戻金\n")

    def reg(*a, **k):
        calls["register"] += 1
        if k.get("replace") or (len(a) >= 4 and a[3]):
            calls["v70"] += 1
        return {"key": "20261114_山陽_10R"}

    monkeypatch.setattr(app, "_v319_register_fetched_result", reg)
    monkeypatch.setattr(
        app.engine,
        "v70_replace_registered_result",
        lambda *a, **k: calls.__setitem__("v70", calls["v70"] + 1) or ("k", None, {}, {}, {"replaced": True}),
    )

    out = app._v319_import_one_race(
        ":memory:",
        "20261114",
        "山陽",
        10,
        skip_existing=not replace,
        replace=replace,
        predict_if_missing=False,
        trials=1000,
        seed=12345,
        fetch_odds=True,
        register_result=True,
        force_history_refresh=force,
    )
    return out, calls


def test_off_sufficiency_skips(monkeypatch):
    out, calls = _run_import(monkeypatch, force=False, replace=False)
    assert calls["fill"] == 0
    assert "充足スキップ" in str(out.get("message") or "")


def test_on_fetches_history(monkeypatch):
    out, calls = _run_import(monkeypatch, force=True, replace=False)
    assert calls["fill"] == 1
    assert calls["fill_force"] is True
    assert calls["v70"] == 0


def test_on_does_not_force_result_replace(monkeypatch):
    out, calls = _run_import(monkeypatch, force=True, replace=False)
    assert calls["fill"] == 1
    assert calls["v70"] == 0


def test_replace_on_does_not_force_history(monkeypatch):
    out, calls = _run_import(monkeypatch, force=False, replace=True)
    assert calls["fill"] == 0
    assert calls["v70"] >= 1 or calls["register"] >= 1

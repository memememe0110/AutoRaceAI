"""Regression tests for fail-closed prediction-history input SHA matching.

Uses Env/SNAP to execute the real app.py helpers and history-save path against a
temporary SQLite database. The production database and race measurements are untouched.
"""
import contextlib
import hashlib
import io
import json
import sqlite3

from qver_realcode import Env, SNAP


def _config(e):
    return e.ns["_v326_normalize_config_json"](SNAP["config_json"])


def _save_history(e, raw_text, config_json):
    snap = dict(SNAP)
    snap["config_json"] = config_json
    seed = int(snap["seed"])
    e.ns["_v326_set_worker_config_snapshot"](snap)
    try:
        _, key, _, _, view = e.save_args(1)
        view["seed"] = seed
        history_id = e.ns["_v231_save_prediction_history"](
            e.db, key, raw_text, "川口", view, 20, seed
        )
        return key, history_id, seed
    finally:
        e.ns["_v326_set_worker_config_snapshot"](None)


def _match(e, key, seed, config_json, sha):
    return e.ns["_v326_find_matching_prediction_history"](
        e.db,
        race_key=key,
        save_version=SNAP["save_version"],
        trials=20,
        seed=seed,
        input_sha256=sha,
        config_json=config_json,
        code_commit_sha=SNAP["code_commit_sha"],
        db_sha256=SNAP["db_sha256"],
    )


def _history_count(e, key):
    with sqlite3.connect(e.db) as con:
        return int(con.execute(
            "SELECT COUNT(*) FROM v231_prediction_history WHERE race_key=?", (key,)
        ).fetchone()[0])


def _sha(raw_text):
    return hashlib.sha256(
        str(raw_text or "").encode("utf-8", errors="replace")
    ).hexdigest()


def test_valid_input_sha_allows_reuse():
    e = Env("app.py", "engine.py")
    try:
        cfg = _config(e)
        raw = "fingerprint valid SHA fixture"
        key, history_id, seed = _save_history(e, raw, cfg)
        before = _history_count(e, key)
        result = _match(e, key, seed, cfg, _sha(raw))
        assert result is not None
        assert result["history_id"] == history_id
        assert result["matched_input_hash"] is True
        assert _history_count(e, key) == before == 1
    finally:
        e.close()


def test_missing_input_sha_is_rejected():
    e = Env("app.py", "engine.py")
    try:
        cfg = _config(e)
        raw = "fingerprint missing SHA fixture"
        key, _, seed = _save_history(e, raw, cfg)
        before = _history_count(e, key)
        for missing in (None, "", " "):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = _match(e, key, seed, cfg, missing)
            assert result is None
            log = output.getvalue()
            assert "reason=input_sha_missing" in log
            assert f"race_key={key}" in log
            assert f"save_version={SNAP['save_version']}" in log
            assert f"trials=20 seed={seed}" in log
            fp = e.ns["_v326_prediction_fingerprint_codes"](
                race_key=key,
                save_version=SNAP["save_version"],
                trials=20,
                seed=seed,
                input_sha256=missing,
                config_json=cfg,
                code_commit_sha=SNAP["code_commit_sha"],
                db_sha256=SNAP["db_sha256"],
            )
            assert fp["complete"] is False
        assert _history_count(e, key) == before == 1
    finally:
        e.close()


def test_different_config_is_not_reused():
    e = Env("app.py", "engine.py")
    try:
        cfg_a = _config(e)
        cfg_obj_b = json.loads(cfg_a)
        cfg_obj_b["test_case"] = "different-config"
        cfg_b = e.ns["_v326_normalize_config_json"](cfg_obj_b)
        raw = "fingerprint config mismatch fixture"
        key, _, seed = _save_history(e, raw, cfg_a)
        before = _history_count(e, key)
        assert e.ns["_v326_config_json_fingerprint"](cfg_a) != (
            e.ns["_v326_config_json_fingerprint"](cfg_b)
        )
        assert _match(e, key, seed, cfg_b, _sha(raw)) is None
        assert _history_count(e, key) == before == 1
    finally:
        e.close()


def test_different_raw_text_sha_is_not_reused():
    e = Env("app.py", "engine.py")
    try:
        cfg = _config(e)
        raw = "fingerprint original raw text"
        key, _, seed = _save_history(e, raw, cfg)
        before = _history_count(e, key)
        assert _match(e, key, seed, cfg, _sha("fingerprint altered raw text")) is None
        assert _history_count(e, key) == before == 1
    finally:
        e.close()

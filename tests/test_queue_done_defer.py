"""Regression tests for deferred queue completion and status failure classification.

All tests use temporary SQLite databases and the real extracted app.py functions.
"""
import os
import sqlite3
import time
import hashlib
import json

from qver_realcode import Env, SNAP


def _fresh():
    e = Env("app.py", "engine.py")
    e.enqueue([("川口", 1)])
    task = e.pickup("owner-A")
    return e, task


def p5_26():
    e, task = _fresh()
    try:
        result, key = e.owner_save(task["task_id"], "owner-A", 1)
        row = e.row(task["task_id"])
        counts = e.counts(key)
        ok = result.get("ok") and row["status"] == "running" and all(v > 0 for v in counts.values())
        return ok, {"save_ok": result.get("ok"), "queue_status": row["status"], "saved_tables": counts}
    finally:
        e.close()


def p5_27():
    e, task = _fresh()
    try:
        result, key = e.owner_save(task["task_id"], "owner-A", 1)
        hb = e.ns["_v326_queue_heartbeat"](e.db, task["task_id"], "owner-A")
        row = e.row(task["task_id"])
        ok = result.get("ok") and hb == "ok" and row["status"] == "running"
        return ok, {"save_ok": result.get("ok"), "heartbeat": hb, "queue_status": row["status"]}
    finally:
        e.close()


def p5_28():
    e, task = _fresh()
    try:
        result, key = e.owner_save(task["task_id"], "owner-A", 1)
        done = e.ns["_v326_update_task_status_ex"](
            e.db, task["task_id"], "done", "", owner_token="owner-A")
        row = e.row(task["task_id"])
        ok = result.get("ok") and done == (True, "ok") and row["status"] == "done"
        return ok, {"save_ok": result.get("ok"), "finalize": done, "queue_status": row["status"]}
    finally:
        e.close()


def p5_31():
    e, task = _fresh()
    try:
        e.owner_save(task["task_id"], "owner-A", 1)
        with sqlite3.connect(e.db) as con:
            con.execute("UPDATE v326_import_queue SET owner_token='owner-B' WHERE task_id=?",
                        (task["task_id"],))
        result = e.ns["_v326_update_task_status_ex"](
            e.db, task["task_id"], "done", "", owner_token="owner-A")
        row = e.row(task["task_id"])
        ok = result == (False, "ownership_lost") and row["owner_token"] == "owner-B" and row["status"] == "running"
        return ok, {"finalize": result, "owner": row["owner_token"], "status": row["status"]}
    finally:
        e.close()


def p5_32():
    e, task = _fresh()
    lock = None
    try:
        # Reduce only this test's SQLite busy timeout so the retry exhaustion is deterministic and fast.
        real_connect = e.ns["_v326_queue_connect"]
        def fast_connect(path, timeout=30.0):
            return sqlite3.connect(str(path), timeout=0.01)
        e.ns["_v326_queue_connect"] = fast_connect
        lock = sqlite3.connect(e.db, timeout=1, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        result = e.ns["_v326_update_task_status_ex"](
            e.db, task["task_id"], "done", "", owner_token="owner-A")
        elapsed = time.monotonic() - started
        lock.execute("ROLLBACK")
        lock.close()
        lock = None
        row = e.row(task["task_id"])
        ok = result == (False, "db_error") and row["status"] == "running" and elapsed < 8
        return ok, {"result": result, "elapsed_s": round(elapsed, 3), "status": row["status"]}
    finally:
        if lock is not None:
            try:
                lock.execute("ROLLBACK")
            except Exception:
                pass
            lock.close()
        e.close()


def p5_33():
    e, task = _fresh()
    try:
        with sqlite3.connect(e.db) as con:
            con.execute("UPDATE v326_import_queue SET status='done' WHERE task_id=?", (task["task_id"],))
        result = e.ns["_v326_update_task_status_ex"](
            e.db, task["task_id"], "done", "", owner_token="owner-A")
        ok = result == (False, "state_not_running")
        return ok, {"result": result, "status": e.row(task["task_id"])["status"]}
    finally:
        e.close()


def p5_34():
    e, task = _fresh()
    try:
        with sqlite3.connect(e.db) as con:
            con.execute("UPDATE v326_import_queue SET owner_token='owner-B' WHERE task_id=?", (task["task_id"],))
        result = e.ns["_v326_update_task_status_ex"](
            e.db, task["task_id"], "error", "old owner failed", owner_token="owner-A")
        row = e.row(task["task_id"])
        ok = result == (False, "ownership_lost") and row["status"] == "running" and row["owner_token"] == "owner-B"
        return ok, {"result": result, "status": row["status"], "owner": row["owner_token"]}
    finally:
        e.close()


# ---- P5-29 / 29b / 30: prediction-history fingerprint reuse (temporary SQLite only)
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


def _history_count(e, race_key):
    with sqlite3.connect(e.db) as con:
        return int(con.execute(
            "SELECT COUNT(*) FROM v231_prediction_history WHERE race_key=?", (race_key,)
        ).fetchone()[0])


def _matcher(e, key, seed, config_json, input_sha256=None):
    return e.ns["_v326_find_matching_prediction_history"](
        e.db, race_key=key, save_version=SNAP["save_version"], seed=seed,
        input_sha256=input_sha256, config_json=config_json,
    )


def p5_29():
    e = Env("app.py", "engine.py")
    try:
        cfg = SNAP["config_json"]
        raw = "P5-29 identical input"
        key, saved_id, seed = _save_history(e, raw, cfg)
        before = _history_count(e, key)
        fp = e.ns["_v326_prediction_fingerprint_codes"](
            race_key=key, save_version=SNAP["save_version"], seed=seed,
            input_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(), config_json=cfg,
        )
        found = _matcher(e, key, seed, cfg, fp["input_sha256"])
        after = _history_count(e, key)
        ok = bool(found) and found["history_id"] == saved_id and after == before == 1
        return ok, {
            "saved_history_id": saved_id,
            "matched_history_id": found.get("history_id") if found else None,
            "rows_before_after": [before, after],
            "fingerprint": fp,
        }
    finally:
        e.close()


def p5_29b():
    e = Env("app.py", "engine.py")
    try:
        cfg_a = SNAP["config_json"]
        cfg_obj_b = json.loads(cfg_a)
        cfg_obj_b["p5_29b_case"] = "different-config"
        cfg_b = json.dumps(cfg_obj_b, ensure_ascii=False, sort_keys=True)
        key, old_id, seed = _save_history(e, "P5-29b original input", cfg_a)
        before = _history_count(e, key)
        fp_a = e.ns["_v326_prediction_fingerprint_codes"](
            race_key=key, save_version=SNAP["save_version"], seed=seed,
            input_sha256=None, config_json=cfg_a,
        )
        reused = _matcher(e, key, seed, cfg_a, None)
        after_reuse = _history_count(e, key)
        fp_b = e.ns["_v326_prediction_fingerprint_codes"](
            race_key=key, save_version=SNAP["save_version"], seed=seed,
            input_sha256=None, config_json=cfg_b,
        )
        mismatch = _matcher(e, key, seed, cfg_b, None)
        before_insert = _history_count(e, key)
        _, new_id, _ = _save_history(e, "P5-29b changed config input", cfg_b)
        after_insert = _history_count(e, key)
        ok = (
            bool(reused) and reused["history_id"] == old_id
            and after_reuse == before == 1
            and mismatch is None
            and new_id > old_id
            and after_insert == before_insert + 1 == 2
        )
        return ok, {
            "reuse_history_id": reused.get("history_id") if reused else None,
            "old_history_id": old_id,
            "mismatch_result": mismatch,
            "new_history_id": new_id,
            "rows": {"before": before, "after_reuse": after_reuse,
                     "before_insert": before_insert, "after_insert": after_insert},
            "fingerprint_without_sha256": {
                "matching": {k: fp_a.get(k) for k in ("race_key", "save_version", "seed", "input_sha256", "config_fp", "complete")},
                "mismatch": {k: fp_b.get(k) for k in ("race_key", "save_version", "seed", "input_sha256", "config_fp", "complete")},
            },
        }
    finally:
        e.close()


def p5_30():
    e = Env("app.py", "engine.py")
    try:
        cfg = SNAP["config_json"]
        raw_saved = "P5-30 saved raw input A"
        raw_new = "P5-30 different raw input B"
        key, saved_id, seed = _save_history(e, raw_saved, cfg)
        before = _history_count(e, key)
        wrong_sha = hashlib.sha256(raw_new.encode("utf-8")).hexdigest()
        fp = e.ns["_v326_prediction_fingerprint_codes"](
            race_key=key, save_version=SNAP["save_version"], seed=seed,
            input_sha256=wrong_sha, config_json=cfg,
        )
        found = _matcher(e, key, seed, cfg, wrong_sha)
        after = _history_count(e, key)
        ok = found is None and after == before == 1
        return ok, {
            "saved_history_id": saved_id,
            "matcher_result": found,
            "rows_before_after": [before, after],
            "fingerprint": fp,
            "worker_fallback": "未検証・既存挙動",
        }
    finally:
        e.close()


TESTS = [
    ("P5-26", p5_26), ("P5-27", p5_27), ("P5-28", p5_28),
    ("P5-29", p5_29), ("P5-29b", p5_29b), ("P5-30", p5_30),
    ("P5-31", p5_31), ("P5-32", p5_32), ("P5-33", p5_33), ("P5-34", p5_34),
]


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in TESTS:
        try:
            ok, info = fn()
        except Exception as exc:
            ok, info = False, {"exception": f"{type(exc).__name__}: {exc}"}
        print(f"{name} {'PASS' if ok else 'FAIL'} {info}", flush=True)
        passed += int(bool(ok))
        failed += int(not ok)
    print(f"--- summary: PASS={passed} FAIL={failed} SKIP=0 ---", flush=True)
    raise SystemExit(1 if failed else 0)

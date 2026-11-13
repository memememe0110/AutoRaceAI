"""Regression tests for deferred queue completion and status failure classification.

All tests use temporary SQLite databases and the real extracted app.py functions.
"""
import os
import sqlite3
import time

from qver_realcode import Env


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


TESTS = [
    ("P5-26", p5_26), ("P5-27", p5_27), ("P5-28", p5_28),
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

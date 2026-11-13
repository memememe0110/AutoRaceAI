#!/usr/bin/env python3
"""Phase 3 Worker ownership/concurrency tests (SQLite mirror only).

Safety boundary:
- Does not import, read, or modify app.py.
- Uses only a temporary SQLite database created by this script.
- Does not access production DB/config, GitHub Actions, GitHub, push, or deploy.
- Uses independent SQLite connections and deterministic Barrier/Event coordination.

This is a mirror test, not an app.py integration test.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def connect(
    path: str,
    timeout: float = 0.0,
    trace_log: list[tuple[int, str]] | None = None,
) -> sqlite3.Connection:
    con = sqlite3.connect(
        path,
        timeout=timeout,
        isolation_level=None,
        check_same_thread=True,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    if trace_log is not None:
        con.set_trace_callback(
            lambda statement: trace_log.append((threading.get_ident(), statement))
        )
    return con


def begin_immediate(
    con: sqlite3.Connection,
    *,
    retries: int = 20,
    base_sleep: float = 0.01,
    on_busy: Callable[[], None] | None = None,
) -> None:
    """Acquire the SQLite writer reservation, retrying only BUSY/LOCKED."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            con.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last = exc
            if on_busy is not None:
                on_busy()
            if attempt + 1 < retries:
                time.sleep(base_sleep * min(2**attempt, 8))
    raise RuntimeError(
        f"BEGIN IMMEDIATE remained BUSY/LOCKED after {retries} attempts: {last}"
    )


def init_db(path: str) -> None:
    con = connect(path, timeout=2.0)
    try:
        con.execute(
            """CREATE TABLE queue (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                save_version TEXT NOT NULL,
                seed INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                owner_token TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
    finally:
        con.close()


def add_task(
    path: str,
    race_key: str,
    version: str = "Ver330-A",
    seed: int = 123,
) -> int:
    config = json.dumps(
        {"save_version": version, "seed": seed},
        sort_keys=True,
        separators=(",", ":"),
    )
    con = connect(path, timeout=2.0)
    try:
        cur = con.execute(
            """INSERT INTO queue(
                   race_key, status, save_version, seed, config_json,
                   owner_token, updated_at
               ) VALUES (?, 'pending', ?, ?, ?, NULL, ?)""",
            (race_key, version, seed, config, now_iso()),
        )
        return int(cur.lastrowid)
    finally:
        con.close()


class WorkerMirror:
    """Small mirror of the agreed queue ownership/transaction rules."""

    def __init__(self, path: str, trace_log: list[tuple[int, str]] | None = None):
        self.path = path
        self.trace_log = trace_log

    def _connect(self, timeout: float = 0.0) -> sqlite3.Connection:
        return connect(self.path, timeout=timeout, trace_log=self.trace_log)

    @staticmethod
    def _before_commit(after_update: Callable[[], None] | None) -> None:
        if after_update is not None:
            after_update()

    def pickup(
        self,
        owner: str,
        *,
        race_key: str | None = None,
        after_read: Callable[[], None] | None = None,
        after_update: Callable[[], None] | None = None,
    ) -> dict[str, Any] | None:
        con = self._connect(timeout=0.0)
        try:
            begin_immediate(con)
            try:
                if race_key is None:
                    row = con.execute(
                        "SELECT * FROM queue WHERE status='pending' "
                        "ORDER BY task_id LIMIT 1"
                    ).fetchone()
                else:
                    row = con.execute(
                        "SELECT * FROM queue WHERE status='pending' AND race_key=? "
                        "ORDER BY task_id LIMIT 1",
                        (race_key,),
                    ).fetchone()
                if row is None:
                    con.execute("COMMIT")
                    return None
                if after_read is not None:
                    after_read()
                cur = con.execute(
                    """UPDATE queue
                       SET status='running', owner_token=?, updated_at=?
                       WHERE task_id=? AND status='pending'""",
                    (owner, now_iso(), int(row["task_id"])),
                )
                if cur.rowcount != 1:
                    con.execute("COMMIT")
                    return None
                self._before_commit(after_update)
                result = dict(row)
                result.update(status="running", owner_token=owner)
                con.execute("COMMIT")
                return result
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def reset_running(
        self,
        owner: str,
        *,
        stale_before: str | None = None,
        after_update: Callable[[], None] | None = None,
    ) -> int:
        con = self._connect(timeout=0.0)
        try:
            begin_immediate(con)
            try:
                if stale_before is None:
                    cur = con.execute(
                        """UPDATE queue
                           SET status='pending', owner_token=NULL, updated_at=?
                           WHERE status='running'
                             AND (owner_token=? OR owner_token IS NULL)""",
                        (now_iso(), owner),
                    )
                else:
                    cur = con.execute(
                        """UPDATE queue
                           SET status='pending', owner_token=NULL, updated_at=?
                           WHERE status='running'
                             AND (owner_token=? OR owner_token IS NULL OR updated_at < ?)""",
                        (now_iso(), owner, stale_before),
                    )
                self._before_commit(after_update)
                count = int(cur.rowcount)
                con.execute("COMMIT")
                return count
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def cancel_owned(
        self,
        task_id: int,
        owner: str,
        *,
        after_update: Callable[[], None] | None = None,
    ) -> bool:
        con = self._connect(timeout=0.0)
        try:
            begin_immediate(con)
            try:
                cur = con.execute(
                    """UPDATE queue
                       SET status='pending', owner_token=NULL, updated_at=?
                       WHERE task_id=? AND status='running' AND owner_token=?""",
                    (now_iso(), task_id, owner),
                )
                self._before_commit(after_update)
                changed = cur.rowcount == 1
                con.execute("COMMIT")
                return changed
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def finish_owned(
        self,
        task_id: int,
        owner: str,
        status: str = "done",
        *,
        after_update: Callable[[], None] | None = None,
    ) -> bool:
        if status not in ("done", "error", "pending"):
            raise ValueError(status)
        con = self._connect(timeout=0.0)
        try:
            begin_immediate(con)
            try:
                cur = con.execute(
                    """UPDATE queue
                       SET status=?, owner_token=NULL, updated_at=?
                       WHERE task_id=? AND status='running' AND owner_token=?""",
                    (status, now_iso(), task_id, owner),
                )
                self._before_commit(after_update)
                changed = cur.rowcount == 1
                con.execute("COMMIT")
                return changed
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def get(self, task_id: int) -> dict[str, Any]:
        con = connect(self.path, timeout=2.0)
        try:
            row = con.execute(
                "SELECT * FROM queue WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"task_id={task_id} not found")
            return dict(row)
        finally:
            con.close()


@dataclass
class Results:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(f"{name} PASS {detail}".rstrip())

    def fail(self, name: str, detail: str) -> None:
        self.failed.append(f"{name} FAIL {detail}".rstrip())


def run(verbose: bool = False) -> tuple[Results, str]:
    out = Results()
    root = tempfile.mkdtemp(prefix="autorace_phase3_")
    db_path = os.path.join(root, "phase3.sqlite3")
    init_db(db_path)
    trace_log: list[tuple[int, str]] = []
    worker = WorkerMirror(db_path, trace_log=trace_log)

    def db_state() -> str:
        con = connect(db_path, timeout=2.0)
        try:
            rows = [dict(row) for row in con.execute(
                "SELECT * FROM queue ORDER BY task_id"
            )]
            return json.dumps(rows, ensure_ascii=False, sort_keys=True)
        finally:
            con.close()

    def check(name: str, reproduce: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            detail = (
                f"REPRO={reproduce}; ERROR={type(exc).__name__}: {exc}; "
                f"DB_STATE={db_state()}"
            )
            if verbose:
                detail += "\nTRACEBACK=" + traceback.format_exc()
            out.fail(name, detail)

    # W01: two independent workers race for the same pending task.
    def test_double_pickup() -> None:
        task_id = add_task(db_path, "race-double")
        gate = threading.Barrier(3)
        results: list[tuple[str, Any]] = []
        errors: list[str] = []
        result_lock = threading.Lock()

        def run_worker(token: str) -> None:
            try:
                gate.wait(timeout=5)
                picked = worker.pickup(token, race_key="race-double")
                with result_lock:
                    results.append((token, picked))
            except Exception as exc:
                with result_lock:
                    errors.append(f"{token}: {type(exc).__name__}: {exc}")

        a = threading.Thread(target=run_worker, args=("worker-A",), name="worker-A")
        b = threading.Thread(target=run_worker, args=("worker-B",), name="worker-B")
        a.start()
        b.start()
        gate.wait(timeout=5)
        a.join(10)
        b.join(10)
        if a.is_alive() or b.is_alive():
            raise AssertionError("worker thread did not finish within 10 seconds")
        winners = [(token, row) for token, row in results if row is not None]
        final = worker.get(task_id)
        if errors or len(winners) != 1:
            raise AssertionError(f"errors={errors}; winners={winners}; row={final}")
        if final["status"] != "running" or final["owner_token"] != winners[0][0]:
            raise AssertionError(f"winner={winners[0][0]}; final_row={final}")
        out.ok("W01", f"single pickup winner={winners[0][0]}; owner_token matches")

    check(
        "W01",
        "one pending race_key=race-double; two threads released by Barrier(3)",
        test_double_pickup,
    )

    # W02: another worker cannot reset a fresh task owned by A.
    def test_foreign_reset() -> None:
        task_id = add_task(db_path, "race-foreign-reset")
        picked = worker.pickup("owner-A", race_key="race-foreign-reset")
        if not picked or picked["task_id"] != task_id:
            raise AssertionError(f"pickup failed: {picked}")
        changed = worker.reset_running("owner-B")
        final = worker.get(task_id)
        if changed != 0 or final["status"] != "running" or final["owner_token"] != "owner-A":
            raise AssertionError(f"changed={changed}; final_row={final}")
        out.ok("W02", "foreign reset did not change fresh running task")

    check(
        "W02",
        "task picked by owner-A; owner-B calls reset_running without stale cutoff",
        test_foreign_reset,
    )

    # W03: only the current owner can cancel its own task back to pending.
    def test_owner_cancel() -> None:
        task_id = add_task(db_path, "race-owner-cancel")
        picked = worker.pickup("owner-C", race_key="race-owner-cancel")
        if not picked or picked["task_id"] != task_id:
            raise AssertionError(f"pickup failed: {picked}")
        if not worker.cancel_owned(task_id, "owner-C"):
            raise AssertionError("owner cancellation returned false")
        final = worker.get(task_id)
        if final["status"] != "pending" or final["owner_token"] is not None:
            raise AssertionError(f"unexpected final row={final}")
        out.ok("W03", "owner cancellation safely returned task to pending")

    check(
        "W03",
        "task running with owner_token=owner-C; cancel_owned uses same token",
        test_owner_cancel,
    )

    # W04: an old owner token cannot mutate a task after it is reacquired.
    def test_stale_token() -> None:
        task_id = add_task(db_path, "race-stale-token")
        first = worker.pickup("old-owner", race_key="race-stale-token")
        if not first or first["task_id"] != task_id:
            raise AssertionError(f"first pickup failed: {first}")
        if not worker.cancel_owned(task_id, "old-owner"):
            raise AssertionError("old owner could not release task")
        second = worker.pickup("new-owner", race_key="race-stale-token")
        if not second or second["task_id"] != task_id:
            raise AssertionError(f"second pickup failed: {second}")
        stale_finish = worker.finish_owned(task_id, "old-owner", "done")
        stale_cancel = worker.cancel_owned(task_id, "old-owner")
        final = worker.get(task_id)
        if stale_finish or stale_cancel:
            raise AssertionError(
                f"stale_finish={stale_finish}; stale_cancel={stale_cancel}; row={final}"
            )
        if final["status"] != "running" or final["owner_token"] != "new-owner":
            raise AssertionError(f"new owner's task changed: {final}")
        out.ok("W04", "stale token could not finish or cancel new owner's task")

    check(
        "W04",
        "old-owner releases task; new-owner repicks it; old token attempts finish and cancel",
        test_stale_token,
    )

    # W05: UI bulk-fix semantics update pending rows only; running snapshot is immutable.
    def test_snapshot_stable() -> None:
        task_id = add_task(db_path, "race-snapshot", "Ver330-A", 11)
        picked = worker.pickup("snapshot-owner", race_key="race-snapshot")
        if not picked or picked["task_id"] != task_id:
            raise AssertionError(f"pickup failed: {picked}")
        ui_version, ui_seed = "Ver326", 999
        con = connect(db_path, timeout=0.0)
        try:
            begin_immediate(con)
            try:
                cur = con.execute(
                    """UPDATE queue SET save_version=?, seed=?, config_json=?
                       WHERE task_id=? AND status='pending'""",
                    (
                        ui_version,
                        ui_seed,
                        json.dumps(
                            {"save_version": ui_version, "seed": ui_seed},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        task_id,
                    ),
                )
                con.execute("COMMIT")
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        final = worker.get(task_id)
        if cur.rowcount != 0:
            raise AssertionError(f"pending-only UPDATE changed {cur.rowcount} row(s)")
        if final["save_version"] != "Ver330-A" or final["seed"] != 11:
            raise AssertionError(f"snapshot overwritten: {final}")
        out.ok("W05", "BEGIN IMMEDIATE pending-only UI update left running snapshot unchanged")

    check(
        "W05",
        "running snapshot Ver330-A/seed=11; UI bulk-fix attempts Ver326/seed=999",
        test_snapshot_stable,
    )

    # W06: injected post-UPDATE exceptions roll back every Worker write transaction.
    def test_all_worker_rollbacks() -> None:
        def inject_failure() -> None:
            raise RuntimeError("injected failure after UPDATE, before COMMIT")

        def expect_rollback(label: str, operation: Callable[[], Any]) -> None:
            trace_log.clear()
            try:
                operation()
            except RuntimeError as exc:
                if "injected failure" not in str(exc):
                    raise
            else:
                raise AssertionError(f"{label}: fault injection did not raise")
            statements = [sql.strip().upper() for _, sql in trace_log]
            begins = sum(sql.startswith("BEGIN IMMEDIATE") for sql in statements)
            rollbacks = sum(sql == "ROLLBACK" for sql in statements)
            commits = sum(sql == "COMMIT" for sql in statements)
            if (begins, rollbacks, commits) != (1, 1, 0):
                raise AssertionError(
                    f"{label}: expected BEGIN IMMEDIATE/ROLLBACK/COMMIT=1/1/0; "
                    f"got {begins}/{rollbacks}/{commits}; SQL={statements}"
                )

        # pickup rollback
        pickup_id = add_task(db_path, "race-rollback-pickup")
        expect_rollback(
            "pickup",
            lambda: worker.pickup(
                "rollback-pickup-owner",
                race_key="race-rollback-pickup",
                after_update=inject_failure,
            ),
        )
        pickup_row = worker.get(pickup_id)
        if pickup_row["status"] != "pending" or pickup_row["owner_token"] is not None:
            raise AssertionError(f"pickup rollback failed: {pickup_row}")

        # reset_running rollback
        reset_id = add_task(db_path, "race-rollback-reset")
        if not worker.pickup("rollback-reset-owner", race_key="race-rollback-reset"):
            raise AssertionError("reset setup pickup failed")
        expect_rollback(
            "reset_running",
            lambda: worker.reset_running("rollback-reset-owner", after_update=inject_failure),
        )
        reset_row = worker.get(reset_id)
        if reset_row["status"] != "running" or reset_row["owner_token"] != "rollback-reset-owner":
            raise AssertionError(f"reset rollback failed: {reset_row}")

        # cancel_owned rollback
        cancel_id = add_task(db_path, "race-rollback-cancel")
        if not worker.pickup("rollback-cancel-owner", race_key="race-rollback-cancel"):
            raise AssertionError("cancel setup pickup failed")
        expect_rollback(
            "cancel_owned",
            lambda: worker.cancel_owned(
                cancel_id, "rollback-cancel-owner", after_update=inject_failure
            ),
        )
        cancel_row = worker.get(cancel_id)
        if cancel_row["status"] != "running" or cancel_row["owner_token"] != "rollback-cancel-owner":
            raise AssertionError(f"cancel rollback failed: {cancel_row}")

        # finish_owned rollback
        finish_id = add_task(db_path, "race-rollback-finish")
        if not worker.pickup("rollback-finish-owner", race_key="race-rollback-finish"):
            raise AssertionError("finish setup pickup failed")
        expect_rollback(
            "finish_owned",
            lambda: worker.finish_owned(
                finish_id,
                "rollback-finish-owner",
                "done",
                after_update=inject_failure,
            ),
        )
        finish_row = worker.get(finish_id)
        if finish_row["status"] != "running" or finish_row["owner_token"] != "rollback-finish-owner":
            raise AssertionError(f"finish rollback failed: {finish_row}")
        out.ok("W06", "pickup/reset/cancel/finish each used BEGIN IMMEDIATE and rolled back injected failure")

    check(
        "W06",
        "inject exception after UPDATE and before COMMIT in pickup, reset_running, cancel_owned, finish_owned",
        test_all_worker_rollbacks,
    )

    # W07: real independent-connection writer contention triggers retry and then succeeds.
    def test_busy_retry() -> None:
        task_id = add_task(db_path, "race-busy-retry")
        lock_held = threading.Event()
        busy_seen = threading.Event()
        release_lock = threading.Event()
        errors: list[str] = []
        result: dict[str, int] = {}
        errors_lock = threading.Lock()

        def record_error(label: str, exc: Exception) -> None:
            with errors_lock:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")

        def holder() -> None:
            con = connect(db_path, timeout=0.0)
            try:
                begin_immediate(con)
                lock_held.set()
                if not release_lock.wait(5):
                    raise TimeoutError("main thread did not release writer lock")
                con.execute("COMMIT")
            except Exception as exc:
                record_error("holder", exc)
                if con.in_transaction:
                    con.execute("ROLLBACK")
            finally:
                con.close()

        def contender() -> None:
            con = connect(db_path, timeout=0.0)
            try:
                if not lock_held.wait(5):
                    raise TimeoutError("holder did not acquire BEGIN IMMEDIATE")
                begin_immediate(
                    con,
                    retries=30,
                    base_sleep=0.01,
                    on_busy=busy_seen.set,
                )
                cur = con.execute(
                    """UPDATE queue SET status='running', owner_token=?, updated_at=?
                       WHERE task_id=? AND status='pending'""",
                    ("retry-worker", now_iso(), task_id),
                )
                result["updated"] = int(cur.rowcount)
                con.execute("COMMIT")
            except Exception as exc:
                record_error("contender", exc)
                if con.in_transaction:
                    con.execute("ROLLBACK")
            finally:
                con.close()

        holder_thread = threading.Thread(target=holder, name="writer-lock-holder")
        contender_thread = threading.Thread(target=contender, name="writer-lock-contender")
        holder_thread.start()
        contender_thread.start()
        if not busy_seen.wait(5):
            release_lock.set()
            holder_thread.join(5)
            contender_thread.join(5)
            raise AssertionError("contender did not observe SQLITE_BUSY/LOCKED")
        release_lock.set()
        holder_thread.join(10)
        contender_thread.join(10)
        final = worker.get(task_id)
        if holder_thread.is_alive() or contender_thread.is_alive():
            raise AssertionError("lock test thread timeout")
        if errors or result.get("updated") != 1:
            raise AssertionError(f"errors={errors}; result={result}; final_row={final}")
        if final["status"] != "running" or final["owner_token"] != "retry-worker":
            raise AssertionError(f"retry result not committed: {final}")
        out.ok("W07", "independent connection observed BUSY, retried, then committed update")

    check(
        "W07",
        "connection A holds BEGIN IMMEDIATE behind Event; connection B retries until A commits",
        test_busy_retry,
    )

    # W08: stale cutoff cannot reset a fresh task owned by another worker.
    def test_stale_cutoff() -> None:
        task_id = add_task(db_path, "race-stale-cutoff")
        picked = worker.pickup("fresh-owner", race_key="race-stale-cutoff")
        if not picked or picked["task_id"] != task_id:
            raise AssertionError(f"pickup failed: {picked}")
        changed = worker.reset_running("janitor", stale_before="2000-01-01T00:00:00+00:00")
        final = worker.get(task_id)
        if changed != 0 or final["status"] != "running" or final["owner_token"] != "fresh-owner":
            raise AssertionError(f"changed={changed}; final_row={final}")
        out.ok("W08", "fresh running task was not reset by stale cutoff")

    check(
        "W08",
        "fresh task owned by fresh-owner; janitor uses cutoff 2000-01-01T00:00:00+00:00",
        test_stale_cutoff,
    )

    # W09: every Worker mutation method begins with BEGIN IMMEDIATE and commits normally.
    def test_all_worker_transactions_begin_immediate() -> None:
        trace_log.clear()
        task_id = add_task(db_path, "race-transaction-audit")
        if not worker.pickup("audit-A", race_key="race-transaction-audit"):
            raise AssertionError("audit pickup A failed")
        if worker.reset_running("audit-A") != 1:
            raise AssertionError("audit reset_running did not update own task")
        if not worker.pickup("audit-B", race_key="race-transaction-audit"):
            raise AssertionError("audit pickup B failed")
        if not worker.cancel_owned(task_id, "audit-B"):
            raise AssertionError("audit cancel_owned failed")
        if not worker.pickup("audit-C", race_key="race-transaction-audit"):
            raise AssertionError("audit pickup C failed")
        if not worker.finish_owned(task_id, "audit-C", "done"):
            raise AssertionError("audit finish_owned failed")
        statements = [sql.strip().upper() for _, sql in trace_log]
        begins = [sql for sql in statements if sql.startswith("BEGIN IMMEDIATE")]
        commits = [sql for sql in statements if sql == "COMMIT"]
        rollbacks = [sql for sql in statements if sql == "ROLLBACK"]
        if len(begins) != 6 or len(commits) != 6 or rollbacks:
            raise AssertionError(
                f"expected 6 BEGIN IMMEDIATE + 6 COMMIT + 0 ROLLBACK; "
                f"got BEGIN={len(begins)}, COMMIT={len(commits)}, "
                f"ROLLBACK={len(rollbacks)}, SQL={statements}"
            )
        final = worker.get(task_id)
        if final["status"] != "done" or final["owner_token"] is not None:
            raise AssertionError(f"unexpected final row={final}")
        out.ok("W09", "pickup/reset/cancel/finish all used BEGIN IMMEDIATE and committed")

    check(
        "W09",
        "trace six normal Worker mutations: pickup, reset, pickup, cancel, pickup, finish",
        test_all_worker_transactions_begin_immediate,
    )

    if verbose:
        print(f"Temporary DB: {db_path}")
    return out, db_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 3 Worker ownership/concurrency SQLite mirror tests"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    result, db_path = run(args.verbose)
    print("=== Phase 3 Worker competition tests (SQLite mirror only) ===")
    for line in result.passed:
        print(line)
    for line in result.failed:
        print(line)
    for line in result.skipped:
        print(line)
    print(
        f"--- summary: PASS={len(result.passed)} "
        f"FAIL={len(result.failed)} SKIP={len(result.skipped)} ---"
    )
    print(f"Temporary DB: {db_path}")
    print(
        "Verified: concurrent single-winner pickup; owner_token binding; foreign reset "
        "protection; owner-only cancel; stale-token protection; pending-only snapshot "
        "update; rollback for all four Worker mutation methods; BUSY retry on independent "
        "connections; stale-cutoff protection; BEGIN IMMEDIATE/COMMIT audit."
    )
    print(
        "Not verified: app.py integration/runtime behavior; actual queue schema/migrations; "
        "periodic heartbeat; Streamlit/UI serialization; cross-process behavior; production "
        "timestamp parsing/stale detection; history-save transaction coupling."
    )
    print(
        "Safety: app.py was not imported/read/edited; no production DB/config, GitHub Actions, "
        "GitHub push, or deployment was accessed."
    )
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

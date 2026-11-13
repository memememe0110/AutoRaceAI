#!/usr/bin/env python3
"""Phase 2 Queue integration tests (SQLite only).

Mirrors AutoRaceAI queue version-lock behavior as of build 20261113_qver_lock7_pred.
Does NOT import app.py / Streamlit / production DB.

Usage:
  python test_queue_version_lock_phase2.py
  python test_queue_version_lock_phase2.py --verbose

Exit code 0 = all PASS, 1 = any FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))
JOB_TYPE = "official_import"


def now_iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Minimal queue layer (mirrors app.py ensure / enqueue / pickup / bulk_fix)
# ---------------------------------------------------------------------------

class QueueLayer:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.RLock()
        self._snapshot_override: dict | None = None  # test control of "UI snapshot"

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def ensure(self) -> None:
        with self.lock:
            with self.connect() as con:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS v326_import_queue (
                        task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_type TEXT NOT NULL,
                        task_mode TEXT NOT NULL DEFAULT 'normal',
                        ymd TEXT NOT NULL,
                        venue TEXT NOT NULL,
                        race_no INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        fetch_odds INTEGER DEFAULT 1,
                        register_result INTEGER DEFAULT 1,
                        skip_existing INTEGER DEFAULT 1,
                        replace_existing INTEGER DEFAULT 0,
                        predict_if_missing INTEGER DEFAULT 1,
                        UNIQUE(job_type, task_mode, ymd, venue, race_no)
                    )
                    """
                )
                cols = {str(r[1]) for r in con.execute("PRAGMA table_info(v326_import_queue)")}
                for col, typ in [
                    ("save_version", "TEXT"),
                    ("seed", "INTEGER"),
                    ("code_commit_sha", "TEXT"),
                    ("config_version", "INTEGER"),
                    ("config_json", "TEXT"),
                    ("input_sha256", "TEXT"),
                    ("owner_token", "TEXT"),
                ]:
                    if col not in cols:
                        con.execute(f"ALTER TABLE v326_import_queue ADD COLUMN {col} {typ}")
                con.commit()

    def set_snapshot(self, snap: dict | None) -> None:
        self._snapshot_override = snap

    def build_snapshot(
        self,
        *,
        save_version: str = "Ver326",
        seed: int = 20260719,
        v330a_enabled: bool = False,
        code_commit_sha: str | None = "deadbeef",
        extra: dict | None = None,
    ) -> dict:
        if self._snapshot_override is not None:
            return dict(self._snapshot_override)
        cfg = {
            "save_version": save_version,
            "v330a_enabled": bool(v330a_enabled),
            "seed": int(seed),
            "extra": extra or {},
        }
        return {
            "save_version": save_version,
            "seed": int(seed),
            "code_commit_sha": code_commit_sha,
            "config_version": 1,
            "config_json": json.dumps(cfg, ensure_ascii=False, sort_keys=True),
            "input_sha256": None,
            "v330a_enabled": bool(v330a_enabled),
        }

    def validate_config(self, row: dict) -> tuple[bool, str]:
        sv = str(row.get("save_version") or "").strip()
        if not sv:
            return False, "save_version empty"
        raw = row.get("config_json")
        if not raw:
            return False, "config_json empty"
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            return False, f"config_json invalid: {e}"
        if str(obj.get("save_version") or "").strip() != sv:
            return False, "config_json.save_version mismatch"
        return True, ""

    def enqueue(
        self,
        *,
        ymd: str,
        venue: str,
        race_no: int,
        task_mode: str = "normal",
        insert_first: bool = False,
        opts: dict | None = None,
        snap: dict | None = None,
    ) -> dict:
        """INSERT / pending opts-only / requeue full snapshot. Mirrors _v326_enqueue_tasks."""
        self.ensure()
        snap = snap or self.build_snapshot()
        opts = opts or {}
        now = now_iso()
        with self.lock:
            with self.connect() as con:
                try:
                    con.execute("BEGIN IMMEDIATE")
                except Exception:
                    pass
                row = con.execute(
                    """
                    SELECT task_id, status, save_version, seed, config_json
                      FROM v326_import_queue
                     WHERE job_type=? AND task_mode=? AND ymd=? AND venue=? AND race_no=?
                     LIMIT 1
                    """,
                    (JOB_TYPE, task_mode, ymd, venue, int(race_no)),
                ).fetchone()
                if row is None:
                    # insert
                    so = 0
                    if insert_first:
                        # shift others
                        con.execute(
                            """
                            UPDATE v326_import_queue
                               SET sort_order = sort_order + 1, updated_at = ?
                             WHERE job_type=? AND status='pending'
                            """,
                            (now, JOB_TYPE),
                        )
                    else:
                        mx = con.execute(
                            "SELECT COALESCE(MAX(sort_order), -1) FROM v326_import_queue WHERE job_type=?",
                            (JOB_TYPE,),
                        ).fetchone()
                        so = int(mx[0] or -1) + 1
                    con.execute(
                        """
                        INSERT INTO v326_import_queue(
                            job_type, task_mode, ymd, venue, race_no, status, sort_order,
                            message, created_at, updated_at,
                            fetch_odds, register_result, skip_existing, replace_existing, predict_if_missing,
                            save_version, seed, code_commit_sha, config_version, config_json, input_sha256
                        ) VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            JOB_TYPE, task_mode, ymd, venue, int(race_no), so,
                            f"enqueue:{snap['save_version']}", now, now,
                            int(opts.get("fetch_odds", 1)),
                            int(opts.get("register_result", 1)),
                            int(opts.get("skip_existing", 1)),
                            int(opts.get("replace_existing", 0)),
                            int(opts.get("predict_if_missing", 1)),
                            snap["save_version"],
                            snap["seed"],
                            snap.get("code_commit_sha"),
                            int(snap.get("config_version") or 1),
                            snap["config_json"],
                            snap.get("input_sha256"),
                        ),
                    )
                    con.commit()
                    return {"action": "insert", "save_version": snap["save_version"]}

                st = str(row["status"] or "")
                tid = int(row["task_id"])
                if st == "pending":
                    # opts only — never touch snapshot
                    con.execute(
                        """
                        UPDATE v326_import_queue
                           SET task_mode=?, updated_at=?,
                               fetch_odds=?, register_result=?,
                               skip_existing=?, replace_existing=?, predict_if_missing=?
                         WHERE task_id=? AND status='pending'
                        """,
                        (
                            task_mode, now,
                            int(opts.get("fetch_odds", 1)),
                            int(opts.get("register_result", 1)),
                            int(opts.get("skip_existing", 1)),
                            int(opts.get("replace_existing", 0)),
                            int(opts.get("predict_if_missing", 1)),
                            tid,
                        ),
                    )
                    con.commit()
                    return {
                        "action": "pending_opts",
                        "save_version": row["save_version"],
                        "seed": row["seed"],
                        "config_json": row["config_json"],
                    }
                if st in ("done", "skipped", "error", "failed"):
                    # requeue: full new snapshot
                    con.execute(
                        """
                        UPDATE v326_import_queue
                           SET status='pending', message=?, updated_at=?,
                               save_version=?, seed=?, code_commit_sha=?,
                               config_version=?, config_json=?,
                               owner_token=NULL,
                               fetch_odds=?, register_result=?,
                               skip_existing=?, replace_existing=?, predict_if_missing=?
                         WHERE task_id=?
                        """,
                        (
                            f"requeue:{snap['save_version']}", now,
                            snap["save_version"], snap["seed"], snap.get("code_commit_sha"),
                            int(snap.get("config_version") or 1), snap["config_json"],
                            int(opts.get("fetch_odds", 1)),
                            int(opts.get("register_result", 1)),
                            int(opts.get("skip_existing", 1)),
                            int(opts.get("replace_existing", 0)),
                            int(opts.get("predict_if_missing", 1)),
                            tid,
                        ),
                    )
                    con.commit()
                    return {"action": "requeue", "save_version": snap["save_version"]}
                # running etc.
                con.commit()
                return {"action": "skip_running", "status": st}

    def pickup(self, owner_token: str | None = None) -> dict:
        self.ensure()
        now = now_iso()
        with self.lock:
            with self.connect() as con:
                try:
                    con.execute("BEGIN IMMEDIATE")
                except Exception:
                    pass
                row = con.execute(
                    """
                    SELECT * FROM v326_import_queue
                     WHERE job_type=? AND status='pending'
                       AND save_version IS NOT NULL
                       AND TRIM(COALESCE(save_version,'')) != ''
                     ORDER BY sort_order ASC, task_id ASC
                     LIMIT 1
                    """,
                    (JOB_TYPE,),
                ).fetchone()
                if not row:
                    con.commit()
                    return {"kind": "empty"}
                d = dict(row)
                ok, reason = self.validate_config(d)
                if not ok:
                    con.execute(
                        """
                        UPDATE v326_import_queue
                           SET status='error', message=?, updated_at=?
                         WHERE task_id=? AND status='pending'
                        """,
                        (f"invalid config: {reason}", now, int(d["task_id"])),
                    )
                    con.commit()
                    return {"kind": "skipped_invalid", "task_id": int(d["task_id"]), "reason": reason}
                ot = str(owner_token or "").strip() or None
                if ot:
                    cur = con.execute(
                        """
                        UPDATE v326_import_queue
                           SET status='running', updated_at=?, owner_token=?
                         WHERE task_id=? AND status='pending'
                        """,
                        (now, ot, int(d["task_id"])),
                    )
                else:
                    cur = con.execute(
                        """
                        UPDATE v326_import_queue
                           SET status='running', updated_at=?
                         WHERE task_id=? AND status='pending'
                        """,
                        (now, int(d["task_id"])),
                    )
                if int(cur.rowcount or 0) <= 0:
                    con.commit()
                    return {"kind": "empty"}
                con.commit()
                d["kind"] = "task"
                d["owner_token"] = ot
                return d

    def count_unfixed(self) -> int:
        self.ensure()
        with self.connect() as con:
            r = con.execute(
                """
                SELECT COUNT(*) FROM v326_import_queue
                 WHERE job_type=? AND status='pending'
                   AND (save_version IS NULL OR TRIM(COALESCE(save_version,''))='')
                """,
                (JOB_TYPE,),
            ).fetchone()
            return int(r[0] or 0)

    def list_unfixed(self, limit: int = 100) -> list[dict]:
        self.ensure()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT task_id, ymd, venue, race_no FROM v326_import_queue
                 WHERE job_type=? AND status='pending'
                   AND (save_version IS NULL OR TRIM(COALESCE(save_version,''))='')
                 ORDER BY sort_order, task_id LIMIT ?
                """,
                (JOB_TYPE, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def bulk_fix(self, snap: dict, *, confirm: bool = False) -> dict:
        if not confirm:
            return {"ok": False, "updated": 0, "reason": "confirm required"}
        self.ensure()
        now = now_iso()
        sv = str(snap.get("save_version") or "").strip()
        if not sv:
            return {"ok": False, "updated": 0, "reason": "empty save_version"}
        with self.lock:
            with self.connect() as con:
                try:
                    con.execute("BEGIN IMMEDIATE")
                except Exception:
                    pass
                # snapshot fixed rows before
                before_fixed = con.execute(
                    """
                    SELECT task_id, save_version, status FROM v326_import_queue
                     WHERE job_type=? AND save_version IS NOT NULL
                       AND TRIM(COALESCE(save_version,'')) != ''
                    """,
                    (JOB_TYPE,),
                ).fetchall()
                before_map = {int(r[0]): (r[1], r[2]) for r in before_fixed}
                cur = con.execute(
                    """
                    UPDATE v326_import_queue
                       SET save_version=?, seed=?, code_commit_sha=?,
                           config_version=?, config_json=?, updated_at=?,
                           message=CASE WHEN message IS NULL OR TRIM(message)='' THEN ? ELSE message END
                     WHERE job_type=? AND status='pending'
                       AND (save_version IS NULL OR TRIM(COALESCE(save_version,''))='')
                    """,
                    (
                        sv, snap.get("seed"), snap.get("code_commit_sha"),
                        int(snap.get("config_version") or 1), snap.get("config_json"),
                        now, f"一括固定:{sv}", JOB_TYPE,
                    ),
                )
                updated = int(cur.rowcount or 0)
                # verify previously fixed unchanged
                after = con.execute(
                    """
                    SELECT task_id, save_version, status FROM v326_import_queue
                     WHERE job_type=? AND task_id IN ({})
                    """.format(",".join("?" * len(before_map)) if before_map else "NULL"),
                    (JOB_TYPE, *before_map.keys()) if before_map else (JOB_TYPE,),
                ).fetchall() if before_map else []
                for r in after:
                    tid = int(r[0])
                    if before_map[tid][0] != r[1] or before_map[tid][1] != r[2]:
                        con.execute("ROLLBACK")
                        return {"ok": False, "updated": 0, "reason": f"mutated fixed row {tid}"}
                con.commit()
                left = self.count_unfixed()
                return {"ok": True, "updated": updated, "left_unfixed": left}

    def insert_legacy_null_pending(self, ymd: str, venue: str, race_no: int) -> int:
        """Simulate pre-migration row (NULL save_version)."""
        self.ensure()
        now = now_iso()
        with self.connect() as con:
            cur = con.execute(
                """
                INSERT INTO v326_import_queue(
                    job_type, task_mode, ymd, venue, race_no, status, sort_order,
                    message, created_at, updated_at
                ) VALUES (?,?,?,?,?,'pending',0,'legacy',?,?)
                """,
                (JOB_TYPE, "normal", ymd, venue, int(race_no), now, now),
            )
            con.commit()
            return int(cur.lastrowid)

    def get_row(self, ymd: str, venue: str, race_no: int) -> dict | None:
        with self.connect() as con:
            r = con.execute(
                """
                SELECT * FROM v326_import_queue
                 WHERE job_type=? AND task_mode='normal' AND ymd=? AND venue=? AND race_no=?
                """,
                (JOB_TYPE, ymd, venue, int(race_no)),
            ).fetchone()
            return dict(r) if r else None

    def set_status(self, task_id: int, status: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE v326_import_queue SET status=?, updated_at=? WHERE task_id=?",
                (status, now_iso(), int(task_id)),
            )
            con.commit()


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@dataclass
class Result:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def ok(self, tid: str, msg: str = "") -> None:
        self.passed.append(f"{tid} PASS {msg}".strip())

    def fail(self, tid: str, msg: str) -> None:
        self.failed.append(f"{tid} FAIL {msg}")

    def skip(self, tid: str, msg: str) -> None:
        self.skipped.append(f"{tid} SKIP {msg}")


def run_phase2(verbose: bool = False) -> Result:
    res = Result()
    td = tempfile.mkdtemp(prefix="autorace_qver_phase2_")
    db = os.path.join(td, "test.db")
    q = QueueLayer(db)

    def log(s: str) -> None:
        if verbose:
            print(s)

    log(f"DB={db}")

    # --- migration twice ---
    try:
        q.ensure()
        q.ensure()
        cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(v326_import_queue)")}
        need = {"save_version", "seed", "config_json", "owner_token", "input_sha256"}
        if need <= cols:
            res.ok("MIG01", "double ensure ok")
        else:
            res.fail("MIG01", f"missing cols {need - cols}")
    except Exception as e:
        res.fail("MIG01", str(e))

    # --- Q01 existing NULL preserved ---
    try:
        tid = q.insert_legacy_null_pending("20261101", "飯塚", 1)
        q.ensure()  # migration again
        row = q.get_row("20261101", "飯塚", 1)
        if row and row.get("save_version") is None:
            res.ok("Q01", f"task_id={tid} NULL retained")
        else:
            res.fail("Q01", f"save_version={row.get('save_version') if row else None}")
    except Exception as e:
        res.fail("Q01", str(e))

    # --- V01 ON enqueue ---
    try:
        snap330 = q.build_snapshot(save_version="Ver330-A", seed=12345, v330a_enabled=True)
        r = q.enqueue(ymd="20261102", venue="川口", race_no=3, snap=snap330)
        row = q.get_row("20261102", "川口", 3)
        if r["action"] == "insert" and row and row["save_version"] == "Ver330-A":
            res.ok("V01", f"save_version={row['save_version']}")
        else:
            res.fail("V01", str(r) + str(row and row.get("save_version")))
    except Exception as e:
        res.fail("V01", str(e))

    # --- V02 OFF enqueue ---
    try:
        snap326 = q.build_snapshot(save_version="Ver326", seed=99, v330a_enabled=False)
        r = q.enqueue(ymd="20261102", venue="川口", race_no=4, snap=snap326)
        row = q.get_row("20261102", "川口", 4)
        if row and row["save_version"] == "Ver326":
            res.ok("V02", "Ver326")
        else:
            res.fail("V02", str(row))
    except Exception as e:
        res.fail("V02", str(e))

    # --- V03 pending opts keep snapshot ---
    try:
        before = q.get_row("20261102", "川口", 3)
        r = q.enqueue(
            ymd="20261102", venue="川口", race_no=3,
            opts={"fetch_odds": 0},
            snap=q.build_snapshot(save_version="Ver326", seed=1),  # different snap must be ignored
        )
        after = q.get_row("20261102", "川口", 3)
        if (
            r["action"] == "pending_opts"
            and after["save_version"] == before["save_version"]
            and after["seed"] == before["seed"]
            and after["config_json"] == before["config_json"]
            and int(after["fetch_odds"] or 0) == 0
        ):
            res.ok("V03", "snapshot held, opts updated")
        else:
            res.fail("V03", f"before={before} after={after} r={r}")
    except Exception as e:
        res.fail("V03", str(e))

    # --- V04 requeue updates snapshot ---
    try:
        row = q.get_row("20261102", "川口", 4)
        q.set_status(int(row["task_id"]), "done")
        new_snap = q.build_snapshot(save_version="Ver330-A", seed=777, v330a_enabled=True)
        r = q.enqueue(ymd="20261102", venue="川口", race_no=4, snap=new_snap)
        after = q.get_row("20261102", "川口", 4)
        if (
            r["action"] == "requeue"
            and after["status"] == "pending"
            and after["save_version"] == "Ver330-A"
            and int(after["seed"]) == 777
        ):
            res.ok("V04", "requeue snapshot refreshed")
        else:
            res.fail("V04", f"r={r} after={dict(after)}")
    except Exception as e:
        res.fail("V04", str(e))

    # --- V06 seed=0 ---
    try:
        snap0 = q.build_snapshot(save_version="Ver326", seed=0)
        r = q.enqueue(ymd="20261103", venue="浜松", race_no=1, snap=snap0)
        row = q.get_row("20261103", "浜松", 1)
        if row is not None and row["seed"] == 0:
            res.ok("V06", "seed=0 retained")
        else:
            res.fail("V06", f"seed={row and row.get('seed')}")
    except Exception as e:
        res.fail("V06", str(e))

    # --- V07 invalid config: pickup must reject (no silent fallback to current ver) ---
    try:
        # drain all pickable (fixed) pending so invalid is next
        for _ in range(50):
            p0 = q.pickup(owner_token="w-drain")
            if p0.get("kind") == "empty":
                break
            if p0.get("kind") == "task":
                q.set_status(int(p0["task_id"]), "done")
            # skipped_invalid already error
        with q.connect() as con:
            now = now_iso()
            con.execute(
                """
                INSERT INTO v326_import_queue(
                    job_type, task_mode, ymd, venue, race_no, status, sort_order,
                    message, created_at, updated_at, save_version, seed, config_json
                ) VALUES (?,?,?,?,?,'pending',-100,'bad',?,?,?,?,?)
                """,
                (
                    JOB_TYPE, "normal", "20261103", "浜松", 9, now, now,
                    "Ver330-A", 1, json.dumps({"save_version": "Ver326"}, ensure_ascii=False),
                ),
            )
            con.commit()
        p = q.pickup(owner_token="w-test")
        if p.get("kind") == "skipped_invalid":
            res.ok("V07", f"rejected: {p.get('reason')}")
        else:
            res.fail("V07", f"got kind={p.get('kind')} ymd={p.get('ymd')} rn={p.get('race_no')}")
    except Exception as e:
        res.fail("V07", str(e))

    # --- Q02 unfixed not picked ---
    try:
        p = q.pickup(owner_token="w-test")
        # should pick fixed rows only, never 飯塚 1R legacy
        if p.get("kind") == "empty":
            # maybe all fixed already picked? enqueue another fixed and check
            q.enqueue(ymd="20261104", venue="山陽", race_no=2, snap=q.build_snapshot(save_version="Ver326"))
            p = q.pickup(owner_token="w-test")
        if p.get("kind") == "task":
            if p.get("ymd") == "20261101" and p.get("venue") == "飯塚":
                res.fail("Q02", "picked unfixed legacy")
            else:
                res.ok("Q02", f"picked fixed {p.get('ymd')}_{p.get('venue')}_{p.get('race_no')}")
                q.set_status(int(p["task_id"]), "done")
        else:
            # still ok if only unfixed left and empty
            if q.count_unfixed() > 0 and p.get("kind") == "empty":
                res.ok("Q02", "empty while unfixed remain")
            else:
                res.fail("Q02", str(p))
    except Exception as e:
        res.fail("Q02", str(e))

    # --- Q03 unfixed UI counts ---
    try:
        n = q.count_unfixed()
        rows = q.list_unfixed()
        if n == len(rows) and n >= 1:
            res.ok("Q03", f"count={n}")
        else:
            res.fail("Q03", f"count={n} list={len(rows)}")
    except Exception as e:
        res.fail("Q03", str(e))

    # --- Q04 bulk without confirm ---
    try:
        r = q.bulk_fix(q.build_snapshot(save_version="Ver326"), confirm=False)
        if not r.get("ok") and r.get("updated", 0) == 0:
            res.ok("Q04", r.get("reason", ""))
        else:
            res.fail("Q04", str(r))
    except Exception as e:
        res.fail("Q04", str(e))

    # --- Q05 bulk with confirm ---
    try:
        n_before = q.count_unfixed()
        # mark a running/done with fixed version to ensure not touched
        q.enqueue(ymd="20261105", venue="伊勢崎", race_no=5, snap=q.build_snapshot(save_version="Ver330-A", v330a_enabled=True))
        fixed_row = q.get_row("20261105", "伊勢崎", 5)
        r = q.bulk_fix(q.build_snapshot(save_version="Ver326", seed=42), confirm=True)
        fixed_after = q.get_row("20261105", "伊勢崎", 5)
        n_after = q.count_unfixed()
        legacy = q.get_row("20261101", "飯塚", 1)
        if (
            r.get("ok")
            and r.get("updated", 0) >= 1
            and n_after == 0
            and fixed_after["save_version"] == "Ver330-A"
            and legacy and legacy["save_version"] == "Ver326"
        ):
            res.ok("Q05", f"updated={r['updated']} left={n_after}")
        else:
            res.fail("Q05", f"r={r} left={n_after} fixed={fixed_after} legacy={legacy}")
    except Exception as e:
        res.fail("Q05", traceback.format_exc() if verbose else str(e))

    # --- Q06 fixed/running/done untouched ---
    try:
        # create done row with snap
        q.enqueue(ymd="20261106", venue="川口", race_no=8, snap=q.build_snapshot(save_version="Ver330-A", seed=5, v330a_enabled=True))
        done_row = q.get_row("20261106", "川口", 8)
        q.set_status(int(done_row["task_id"]), "done")
        # insert new unfixed
        q.insert_legacy_null_pending("20261106", "川口", 9)
        r = q.bulk_fix(q.build_snapshot(save_version="Ver326"), confirm=True)
        done_after = q.get_row("20261106", "川口", 8)
        if done_after["status"] == "done" and done_after["save_version"] == "Ver330-A":
            res.ok("Q06", "done row unchanged")
        else:
            res.fail("Q06", str(done_after))
    except Exception as e:
        res.fail("Q06", str(e))

    # --- Q07 concurrent-ish: bulk while status flip (simulate race) ---
    try:
        q.insert_legacy_null_pending("20261107", "浜松", 2)
        # In real life another worker could flip to running between SELECT and UPDATE.
        # Our UPDATE has status='pending' AND unfixed in WHERE — flipping first prevents overwrite.
        row = q.get_row("20261107", "浜松", 2)
        q.set_status(int(row["task_id"]), "running")
        r = q.bulk_fix(q.build_snapshot(save_version="Ver326"), confirm=True)
        after = q.get_row("20261107", "浜松", 2)
        if after["status"] == "running" and after.get("save_version") is None:
            res.ok("Q07", "running unfixed not overwritten by bulk_fix")
        else:
            res.fail("Q07", f"r={r} after={after}")
    except Exception as e:
        res.fail("Q07", str(e))

    # --- V05: pickup captures row snapshot; UI snap change must not alter running row ---
    try:
        # drain pickable first
        for _ in range(50):
            p0 = q.pickup(owner_token="w-drain2")
            if p0.get("kind") == "empty":
                break
            if p0.get("kind") == "task":
                q.set_status(int(p0["task_id"]), "done")
        q.enqueue(
            ymd="20261108", venue="飯塚", race_no=7,
            snap=q.build_snapshot(save_version="Ver330-A", seed=11, v330a_enabled=True),
        )
        p = q.pickup(owner_token="w-A")
        captured = p.get("save_version")
        q.set_snapshot(q.build_snapshot(save_version="Ver326", v330a_enabled=False))
        still = q.get_row("20261108", "飯塚", 7)
        if (
            p.get("kind") == "task"
            and p.get("ymd") == "20261108"
            and int(p.get("race_no") or 0) == 7
            and captured == "Ver330-A"
            and still["status"] == "running"
            and still["save_version"] == "Ver330-A"
            and int(still["seed"]) == 11
        ):
            res.ok("V05", "running row snapshot stable under UI snap change")
        else:
            res.fail(
                "V05",
                f"kind={p.get('kind')} captured={captured} "
                f"still_sv={still and still.get('save_version')} st={still and still.get('status')}",
            )
        q.set_snapshot(None)
    except Exception as e:
        res.fail("V05", str(e))

    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 Queue version-lock SQLite tests")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    res = run_phase2(verbose=args.verbose)
    print("=== Phase 2 Queue tests ===")
    for line in res.passed:
        print(line)
    for line in res.skipped:
        print(line)
    for line in res.failed:
        print(line)
    print(
        f"--- summary: PASS={len(res.passed)} FAIL={len(res.failed)} SKIP={len(res.skipped)} ---"
    )
    print(
        "Note: This script mirrors queue SQL/logic for lock7; it does not import app.py. "
        "W07 mid-race heartbeat and Streamlit paths remain out of scope."
    )
    return 0 if not res.failed else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 4 fingerprint / history dedupe / mid-crash resume (SQLite mirror only).

Does NOT import app.py. No production DB / Actions / push / deploy.

Covers (mirror of lock7 intent):
  F01 first input_sha256 conditional save
  F02 existing hash not overwritten
  F03 hash mismatch recorded, prediction may continue
  F04 same fingerprint -> skip re-predict (pred_ok)
  F05 save_version mismatch -> not same
  F06 seed mismatch -> not same
  F07 config_json mismatch -> not same
  F08 legacy history without |qcfg: -> not same
  F09 incomplete fingerprint -> no unconditional skip
  J05 history saved but queue done failed -> re-run uses fingerprint, no duplicate
  J06 identical history already present -> no second insert
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256(str(s or "").encode("utf-8", errors="replace")).hexdigest()


def normalize_config(cfg) -> str:
    if cfg is None or cfg == "":
        return ""
    if isinstance(cfg, str):
        try:
            obj = json.loads(cfg)
        except Exception:
            return str(cfg).strip()
    else:
        obj = cfg
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_fp(cfg) -> str | None:
    n = normalize_config(cfg)
    if not n:
        return None
    return hashlib.sha256(n.encode("utf-8")).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con


def init_db(path: str) -> None:
    with connect(path) as con:
        con.execute(
            """
            CREATE TABLE queue (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                save_version TEXT,
                seed INTEGER,
                config_json TEXT,
                input_sha256 TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_key TEXT NOT NULL,
                app_version TEXT NOT NULL,
                seed INTEGER NOT NULL,
                settings_hash TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def fingerprint_codes(
    *,
    race_key: str,
    save_version: str,
    seed: int | None,
    input_sha256: str | None,
    config_json=None,
) -> dict:
    sv = str(save_version or "").strip()
    rk = str(race_key or "").strip()
    try:
        sd = int(seed) if seed is not None else None
    except Exception:
        sd = None
    ih = str(input_sha256 or "").strip() or None
    cfp = config_fp(config_json)
    complete = bool(rk and sv and sd is not None and cfp)
    return {
        "race_key": rk,
        "save_version": sv,
        "seed": sd,
        "input_sha256": ih,
        "config_fp": cfp,
        "complete": complete,
    }


def find_matching_history(path: str, fp: dict) -> dict | None:
    """Same rules as app mirror: require qcfg in settings_hash when config required."""
    if not fp.get("complete"):
        return None
    with connect(path) as con:
        rows = con.execute(
            """
            SELECT history_id, race_key, app_version, seed, raw_text, settings_hash
              FROM history
             WHERE race_key=? AND app_version=? AND seed=?
             ORDER BY history_id DESC LIMIT 30
            """,
            (fp["race_key"], fp["save_version"], int(fp["seed"])),
        ).fetchall()
    want_h = fp.get("input_sha256")
    want_cfg = fp.get("config_fp")
    for r in rows:
        sh = str(r["settings_hash"] or "")
        if "|qcfg:" not in sh:
            continue
        if sh.split("|qcfg:")[-1].strip() != want_cfg:
            continue
        if want_h:
            if sha256_text(r["raw_text"]) != want_h:
                continue
        return {
            "history_id": int(r["history_id"]),
            "matched_config": True,
            "matched_input_hash": bool(want_h),
        }
    return None


def bind_input_sha256(path: str, task_id: int, card_text: str) -> dict:
    h = sha256_text(card_text)
    out = {"hash": h, "mismatch": False, "codes": [], "stored": None}
    with connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """
                UPDATE queue SET input_sha256=?, updated_at=?
                 WHERE task_id=?
                   AND (input_sha256 IS NULL OR TRIM(COALESCE(input_sha256,''))='')
                """,
                (h, now_iso(), int(task_id)),
            )
            row = con.execute(
                "SELECT input_sha256 FROM queue WHERE task_id=?", (int(task_id),)
            ).fetchone()
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    stored = str(row[0] or "") if row else ""
    out["stored"] = stored or None
    if stored and stored != h:
        out["mismatch"] = True
        out["codes"] = ["input_hash_mismatch", "reproducibility_not_guaranteed"]
    return out


def save_history(
    path: str,
    *,
    race_key: str,
    app_version: str,
    seed: int,
    raw_text: str,
    config_json,
    base_settings_hash: str = "basehash",
) -> int:
    cfp = config_fp(config_json)
    sh = f"{base_settings_hash}|qcfg:{cfp}" if cfp else base_settings_hash
    with connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                """
                INSERT INTO history(race_key, app_version, seed, settings_hash, raw_text, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (race_key, app_version, int(seed), sh, raw_text, now_iso()),
            )
            hid = int(cur.lastrowid)
            con.execute("COMMIT")
            return hid
        except Exception:
            con.execute("ROLLBACK")
            raise


def process_one(
    path: str,
    task_id: int,
    *,
    card_text: str,
    force_skip_done_update: bool = False,
) -> dict:
    """Simulate worker race: bind hash -> fingerprint match? -> save history -> done."""
    with connect(path) as con:
        row = dict(con.execute("SELECT * FROM queue WHERE task_id=?", (task_id,)).fetchone())

    bind = bind_input_sha256(path, task_id, card_text)
    # re-read stored hash
    with connect(path) as con:
        row = dict(con.execute("SELECT * FROM queue WHERE task_id=?", (task_id,)).fetchone())

    fp = fingerprint_codes(
        race_key=row["race_key"],
        save_version=row["save_version"],
        seed=row["seed"],
        input_sha256=row.get("input_sha256"),
        config_json=row.get("config_json"),
    )
    match = find_matching_history(path, fp)
    if match:
        # skip predict, mark done
        if not force_skip_done_update:
            with connect(path) as con:
                con.execute(
                    "UPDATE queue SET status='done', updated_at=? WHERE task_id=?",
                    (now_iso(), task_id),
                )
        return {
            "pred_ok": True,
            "skipped_predict": True,
            "history_id": match["history_id"],
            "bind": bind,
            "done_updated": not force_skip_done_update,
        }

    if not fp["complete"]:
        # incomplete: must NOT skip; run predict path
        hid = save_history(
            path,
            race_key=row["race_key"],
            app_version=str(row["save_version"] or "Ver326"),
            seed=int(row["seed"] or 0),
            raw_text=card_text,
            config_json=row.get("config_json"),
        )
        if not force_skip_done_update:
            with connect(path) as con:
                con.execute(
                    "UPDATE queue SET status='done', updated_at=? WHERE task_id=?",
                    (now_iso(), task_id),
                )
        return {
            "pred_ok": True,
            "skipped_predict": False,
            "history_id": hid,
            "bind": bind,
            "incomplete_fp": True,
            "done_updated": not force_skip_done_update,
        }

    hid = save_history(
        path,
        race_key=row["race_key"],
        app_version=row["save_version"],
        seed=int(row["seed"]),
        raw_text=card_text,
        config_json=row["config_json"],
    )
    if not force_skip_done_update:
        with connect(path) as con:
            con.execute(
                "UPDATE queue SET status='done', updated_at=? WHERE task_id=?",
                (now_iso(), task_id),
            )
    return {
        "pred_ok": True,
        "skipped_predict": False,
        "history_id": hid,
        "bind": bind,
        "done_updated": not force_skip_done_update,
    }


def add_task(
    path: str,
    *,
    race_key: str,
    save_version: str,
    seed: int,
    config: dict,
    status: str = "pending",
    input_sha256: str | None = None,
) -> int:
    cfg = json.dumps(config, ensure_ascii=False, sort_keys=True)
    with connect(path) as con:
        cur = con.execute(
            """
            INSERT INTO queue(race_key,status,save_version,seed,config_json,input_sha256,updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (race_key, status, save_version, seed, cfg, input_sha256, now_iso()),
        )
        return int(cur.lastrowid)


def history_count(path: str, race_key: str) -> int:
    with connect(path) as con:
        r = con.execute(
            "SELECT COUNT(*) FROM history WHERE race_key=?", (race_key,)
        ).fetchone()
        return int(r[0] or 0)


@dataclass
class Results:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(f"{name} PASS {detail}".rstrip())

    def fail(self, name: str, detail: str) -> None:
        self.failed.append(f"{name} FAIL {detail}".rstrip())


def run(verbose: bool = False) -> Results:
    out = Results()
    root = tempfile.mkdtemp(prefix="autorace_phase4_")
    path = os.path.join(root, "phase4.sqlite3")
    init_db(path)

    def check(name, fn):
        try:
            fn()
        except Exception as e:
            out.fail(name, traceback.format_exc() if verbose else str(e))

    # F01 first bind
    def t_f01():
        tid = add_task(
            path,
            race_key="rk-f01",
            save_version="Ver330-A",
            seed=1,
            config={"save_version": "Ver330-A", "seed": 1},
        )
        b = bind_input_sha256(path, tid, "card-text-A")
        with connect(path) as con:
            row = con.execute("SELECT input_sha256 FROM queue WHERE task_id=?", (tid,)).fetchone()
        if b["hash"] and row[0] == b["hash"] and not b["mismatch"]:
            out.ok("F01", f"stored={row[0][:12]}…")
        else:
            raise AssertionError(f"b={b} row={row}")

    check("F01", t_f01)

    # F02 no overwrite
    def t_f02():
        tid = add_task(
            path,
            race_key="rk-f02",
            save_version="Ver326",
            seed=2,
            config={"save_version": "Ver326", "seed": 2},
            input_sha256=sha256_text("original"),
        )
        b = bind_input_sha256(path, tid, "different-card")
        with connect(path) as con:
            row = con.execute("SELECT input_sha256 FROM queue WHERE task_id=?", (tid,)).fetchone()
        if row[0] == sha256_text("original") and b["mismatch"]:
            out.ok("F02", "existing hash kept; mismatch flagged")
        else:
            raise AssertionError(f"row={row} b={b}")

    check("F02", t_f02)

    # F03 mismatch codes, continue allowed (process still returns pred_ok path)
    def t_f03():
        tid = add_task(
            path,
            race_key="rk-f03",
            save_version="Ver326",
            seed=3,
            config={"save_version": "Ver326", "seed": 3},
            input_sha256=sha256_text("old"),
        )
        b = bind_input_sha256(path, tid, "new-text")
        if not b["mismatch"] or "input_hash_mismatch" not in b["codes"]:
            raise AssertionError(str(b))
        # continue = we still call process which will save new history
        r = process_one(path, tid, card_text="new-text")
        if r.get("pred_ok"):
            out.ok("F03", f"mismatch codes={b['codes']}; pred continued hid={r.get('history_id')}")
        else:
            raise AssertionError(str(r))

    check("F03", t_f03)

    # F04 same fingerprint skips second predict
    def t_f04():
        cfg = {"save_version": "Ver330-A", "seed": 4, "k": "x"}
        rk = "rk-f04"
        tid = add_task(path, race_key=rk, save_version="Ver330-A", seed=4, config=cfg)
        r1 = process_one(path, tid, card_text="same-card")
        # requeue as pending without clearing history
        with connect(path) as con:
            con.execute("UPDATE queue SET status='pending' WHERE task_id=?", (tid,))
        n1 = history_count(path, rk)
        r2 = process_one(path, tid, card_text="same-card")
        n2 = history_count(path, rk)
        if r1["skipped_predict"] is False and r2["skipped_predict"] is True and n2 == n1:
            out.ok("F04", f"second skipped; history stays {n2}")
        else:
            raise AssertionError(f"r1={r1} r2={r2} n1={n1} n2={n2}")

    check("F04", t_f04)

    # F05 version differs
    def t_f05():
        cfg = {"save_version": "Ver326", "seed": 5}
        rk = "rk-f05"
        tid = add_task(path, race_key=rk, save_version="Ver326", seed=5, config=cfg)
        process_one(path, tid, card_text="c5")
        with connect(path) as con:
            con.execute(
                "UPDATE queue SET status='pending', save_version='Ver330-A', config_json=? WHERE task_id=?",
                (json.dumps({"save_version": "Ver330-A", "seed": 5}, sort_keys=True), tid),
            )
        n1 = history_count(path, rk)
        r2 = process_one(path, tid, card_text="c5")
        n2 = history_count(path, rk)
        if r2["skipped_predict"] is False and n2 == n1 + 1:
            out.ok("F05", "version change forces new history")
        else:
            raise AssertionError(f"r2={r2} n1={n1} n2={n2}")

    check("F05", t_f05)

    # F06 seed differs
    def t_f06():
        cfg = {"save_version": "Ver326", "seed": 6}
        rk = "rk-f06"
        tid = add_task(path, race_key=rk, save_version="Ver326", seed=6, config=cfg)
        process_one(path, tid, card_text="c6")
        with connect(path) as con:
            con.execute(
                "UPDATE queue SET status='pending', seed=60, config_json=? WHERE task_id=?",
                (json.dumps({"save_version": "Ver326", "seed": 60}, sort_keys=True), tid),
            )
        n1 = history_count(path, rk)
        r2 = process_one(path, tid, card_text="c6")
        n2 = history_count(path, rk)
        if r2["skipped_predict"] is False and n2 == n1 + 1:
            out.ok("F06", "seed change forces new history")
        else:
            raise AssertionError(f"r2={r2} n={n1}->{n2}")

    check("F06", t_f06)

    # F07 config differs
    def t_f07():
        cfg = {"save_version": "Ver326", "seed": 7, "flag": False}
        rk = "rk-f07"
        tid = add_task(path, race_key=rk, save_version="Ver326", seed=7, config=cfg)
        process_one(path, tid, card_text="c7")
        with connect(path) as con:
            con.execute(
                "UPDATE queue SET status='pending', config_json=? WHERE task_id=?",
                (json.dumps({"save_version": "Ver326", "seed": 7, "flag": True}, sort_keys=True), tid),
            )
        n1 = history_count(path, rk)
        r2 = process_one(path, tid, card_text="c7")
        n2 = history_count(path, rk)
        if r2["skipped_predict"] is False and n2 == n1 + 1:
            out.ok("F07", "config_json change forces new history")
        else:
            raise AssertionError(f"r2={r2} n={n1}->{n2}")

    check("F07", t_f07)

    # F08 legacy no qcfg
    def t_f08():
        rk = "rk-f08"
        with connect(path) as con:
            con.execute(
                """
                INSERT INTO history(race_key,app_version,seed,settings_hash,raw_text,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (rk, "Ver326", 8, "legacy-no-qcfg", "c8", now_iso()),
            )
        tid = add_task(
            path,
            race_key=rk,
            save_version="Ver326",
            seed=8,
            config={"save_version": "Ver326", "seed": 8},
        )
        n1 = history_count(path, rk)
        r = process_one(path, tid, card_text="c8")
        n2 = history_count(path, rk)
        if r["skipped_predict"] is False and n2 == n1 + 1:
            out.ok("F08", "legacy without qcfg not treated as match")
        else:
            raise AssertionError(f"r={r} n={n1}->{n2}")

    check("F08", t_f08)

    # F09 incomplete fingerprint (no config_json) -> no unconditional skip
    def t_f09():
        rk = "rk-f09"
        # insert history that would match if incomplete were ignored
        with connect(path) as con:
            con.execute(
                """
                INSERT INTO history(race_key,app_version,seed,settings_hash,raw_text,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (rk, "Ver326", 9, "base|qcfg:dead", "c9", now_iso()),
            )
            cur = con.execute(
                """
                INSERT INTO queue(race_key,status,save_version,seed,config_json,updated_at)
                VALUES (?,?,?,?,NULL,?)
                """,
                (rk, "pending", "Ver326", 9, now_iso()),
            )
            tid = int(cur.lastrowid)
        fp = fingerprint_codes(
            race_key=rk, save_version="Ver326", seed=9, input_sha256=None, config_json=None
        )
        if fp["complete"]:
            raise AssertionError("expected incomplete")
        match = find_matching_history(path, fp)
        if match is not None:
            raise AssertionError("incomplete must not match")
        n1 = history_count(path, rk)
        r = process_one(path, tid, card_text="c9")
        n2 = history_count(path, rk)
        if r.get("incomplete_fp") and r["skipped_predict"] is False and n2 == n1 + 1:
            out.ok("F09", "incomplete fp did not skip; new history written")
        else:
            raise AssertionError(f"r={r} n={n1}->{n2}")

    check("F09", t_f09)

    # J05: history saved, done update skipped -> re-run no duplicate
    def t_j05():
        cfg = {"save_version": "Ver330-A", "seed": 50}
        rk = "rk-j05"
        tid = add_task(path, race_key=rk, save_version="Ver330-A", seed=50, config=cfg)
        r1 = process_one(path, tid, card_text="crash-card", force_skip_done_update=True)
        with connect(path) as con:
            st = con.execute("SELECT status FROM queue WHERE task_id=?", (tid,)).fetchone()[0]
        if st != "pending":
            # leave as pending to simulate crash before done
            pass
        n1 = history_count(path, rk)
        r2 = process_one(path, tid, card_text="crash-card")
        n2 = history_count(path, rk)
        if r1["skipped_predict"] is False and r2["skipped_predict"] is True and n2 == n1:
            out.ok("J05", f"resume skipped predict; history={n2}")
        else:
            raise AssertionError(f"r1={r1} r2={r2} n={n1}->{n2} status={st}")

    check("J05", t_j05)

    # J06 identical already present
    def t_j06():
        cfg = {"save_version": "Ver326", "seed": 60}
        rk = "rk-j06"
        tid = add_task(path, race_key=rk, save_version="Ver326", seed=60, config=cfg)
        process_one(path, tid, card_text="dup")
        with connect(path) as con:
            con.execute("UPDATE queue SET status='pending' WHERE task_id=?", (tid,))
        n1 = history_count(path, rk)
        r2 = process_one(path, tid, card_text="dup")
        n2 = history_count(path, rk)
        if r2["skipped_predict"] and n2 == n1 == 1:
            out.ok("J06", "no duplicate history row")
        else:
            raise AssertionError(f"r2={r2} n={n1}->{n2}")

    check("J06", t_j06)

    if verbose:
        print(f"DB={path}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 4 fingerprint/history SQLite mirror tests")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    res = run(args.verbose)
    print("=== Phase 4 fingerprint / history tests (SQLite mirror) ===")
    for line in res.passed:
        print(line)
    for line in res.failed:
        print(line)
    print(f"--- summary: PASS={len(res.passed)} FAIL={len(res.failed)} ---")
    print(
        "Scope: mirror of lock7 fingerprint rules only. Not app.py integration. "
        "No production resources accessed."
    )
    return 0 if not res.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

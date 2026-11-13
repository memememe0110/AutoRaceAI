"""Phase 5: 1レース保存TX・所有権・heartbeat・shutdown（P5-01〜P5-09, P5-12〜P5-20）。

ミラーではなく実コードを動かす。app.py は import しない（Streamlit を起動しない）。
必要な関数を AST で取り出し、実際の SQL・実際の engine 保存関数を一時 SQLite に対して実行する。
Worker 本体（_v319_bg_import_worker）も実関数で、DB 以外の周辺（取込処理など）だけ差し替える。
本番 DB・Actions・push・deploy には触れない。P5-10/P5-11 は既存定義が無いため欠番。

  python tests/test_queue_version_lock_phase5.py [--app app.py] [--engine engine.py]
"""
import argparse, ast, json, os, sqlite3, sys, tempfile, threading, time, traceback
from unittest import mock

MODULE_NAMES = [
    "_V326OwnershipLost", "_V326SafeStop", "_V326HeartbeatJoinTimeout",
    "_V326_HB_LOCK", "_V326_HB_INFLIGHT", "_V326_HB_ERROR_STREAK_LIMIT",
    "_V326_HB_CONNECT_TIMEOUT_SEC", "_V326_HB_WRITE_RETRIES", "_V326_HB_JOIN_TIMEOUT_SEC",
    "_V326_TX_ACTIVE_TLS", "_v326_set_worker_tx_active_event", "_v326_get_worker_tx_active_event",
    "_v326_queue_heartbeat", "_v326_hb_timer_loop", "_v326_raise_hb_zombie",
    "_v326_queue_connect", "_v326_queue_write_retry",
    "_V326_QUEUE_WORKER_LOCAL", "_v326_worker_config_snapshot", "_v326_set_worker_config_snapshot",
    "_v319_bg_import_worker",
]


class _Missing(dict):
    def __missing__(self, k):
        import builtins
        if hasattr(builtins, k):
            raise KeyError(k)
        return mock.MagicMock(name=k)


def load(app_path):
    src = open(app_path, encoding="utf-8").read()
    tree = ast.parse(src)
    segs = {}
    for n in tree.body:
        names = []
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = [t.id for t in tg if isinstance(t, ast.Name)]
        for nm in names:
            if nm in MODULE_NAMES:
                segs[nm] = (n.lineno, ast.get_source_segment(src, n))
    missing = [m for m in MODULE_NAMES if m not in segs]
    if missing:
        raise RuntimeError(f"app.py に無い: {missing}")
    import zlib, pickle, json, urllib.request
    from datetime import datetime, timezone, timedelta
    g = _Missing()
    g.update({"__builtins__": __builtins__, "sqlite3": sqlite3, "threading": threading, "zlib": zlib,
              "pickle": pickle, "json": json, "time_module": time, "urllib": urllib,
              "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
              "_V287_IMPORT_STATE_RESET": {}, "_V287_AUTO_IMPORT_STATE": {},
              "_v228_now_jst_iso": lambda: datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="microseconds")})
    for _, (ln, seg) in sorted(segs.items(), key=lambda kv: kv[1][0]):
        exec(compile(seg, app_path, "exec"), g)
    return g


class Harness:
    """Worker を差し替え環境で 1 回動かす。"""

    def __init__(self, app_path, *, tasks, behavior="ok", hang=False, join_timeout=0.3, hb_interval=0.02,
                 work_sleep=0.1, run_in_thread=False):
        self.g = g = load(app_path)
        self.logs = []
        g["print"] = lambda *args, **kwargs: self.logs.append(" ".join(str(x) for x in args))
        g["_V326_QUEUE_HEARTBEAT_SEC"] = hb_interval
        g["_V326_HB_JOIN_TIMEOUT_SEC"] = join_timeout
        self.tasks = list(tasks)
        self.picked = []
        self.status_updates = []
        self.bg_updates = []
        self.hb_calls = []          # (time, task_id, owner)
        self.work_starts = []       # (time, task_id)
        self.release = threading.Event()
        self.failed_exc = None
        self.zombie_items = []
        self.tls_after = None
        self.behavior = behavior
        self.hang = hang
        self.work_sleep = work_sleep
        self.run_in_thread = run_in_thread
        real_loop = g["_v326_hb_timer_loop"]
        h = self

        def loop(db, tid, ot, stop, lost, safe, txa, interval, limit):
            if h.hang:
                h.release.wait(10)   # stop を無視して居座る（ゾンビ）
                return
            if h.behavior == "safe_stop_hang":
                safe.set()
                h.release.wait(10)   # SafeStop と join timeout の競合を再現
                return
            return real_loop(db, tid, ot, stop, lost, safe, txa, interval, limit)

        def pickup(*a, **k):
            if not h.tasks:
                return None
            t = h.tasks.pop(0)
            h.picked.append(t["task_id"])
            return t

        def hb(db, tid, ot, tx_active=None):
            h.hb_calls.append((time.monotonic(), tid, ot))
            n = sum(1 for _, x, _ in h.hb_calls if x == tid)
            if h.behavior in ("safe_stop", "safe_stop_hang"):
                return "ok" if n == 1 else "error"
            if h.behavior == "ownership_lost":
                return "lost" if len(h.hb_calls) == 2 else "ok"
            return "ok"

        def work(*a, **k):
            cur = h.picked[-1]
            h.work_starts.append((time.monotonic(), cur))
            time.sleep(h.work_sleep)
            b = h.behavior
            if b == "raise":
                raise RuntimeError("boom")
            if b == "status_error":
                return {"status": "error", "message": "x"}
            if b in ("safe_stop", "safe_stop_hang", "ownership_lost"):
                k["on_step"]("phase5 heartbeat test")
            return {"status": "ok"}

        def upd(db, tid, status, message="", *, owner_token):
            h.status_updates.append((tid, status, message, owner_token))
            return True

        def upd_ex(db, tid, status, message="", *, owner_token):
            h.status_updates.append((tid, status, message, owner_token))
            if h.behavior == "ownership_lost" and tid == 1:
                return False, "ownership_lost"
            return True, "ok"

        def bg_update(db, job_id, **kw):
            h.bg_updates.append(kw)
            if kw.get("status") == "failed":
                h.failed_exc = sys.exc_info()[1]

        real_zombie = g["_v326_raise_hb_zombie"]

        def zombie(db, state, item, ot):
            h.zombie_items.append(dict(item) if isinstance(item, dict) else None)
            return real_zombie(db, state, item, ot)

        g.update({
            "_v326_hb_timer_loop": loop, "_v326_pickup_next": pickup, "_v326_queue_heartbeat": hb,
            "_v319_import_one_race": work, "_v319_import_player_history_one_race": work,
            "_v326_validate_queue_config_row": mock.MagicMock(side_effect=ValueError("bad config")),
            "_v326_update_task_status": upd, "_v326_update_task_status_ex": upd_ex, "_v278_bg_update": bg_update,
            "_v326_queue_summary": lambda *a, **k: {"pending": 1, "running": 0},
            "_v278_bg_cancel_requested": lambda *a, **k: False,
            "_v278_bg_pause_loop": lambda *a, **k: None,
            "_v326_raise_hb_zombie": zombie,
        })
        # _v326_raise_hb_zombie はモジュール関数から呼ぶので、Worker が見る名前を差し替えれば足りる

    def run(self):
        g = self.g

        def body():
            with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                 mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("no db in test")):
                g["_v319_bg_import_worker"]("/nonexistent.sqlite3", 1, {"ymd": "20261113"})
            self.tls_after = {
                "tx_active": g["_v326_get_worker_tx_active_event"](),
                "config": g["_v326_worker_config_snapshot"](),
            }

        t0 = time.monotonic()
        if self.run_in_thread:
            th = threading.Thread(target=body, name="worker-under-test")
            th.start(); th.join(30)
        else:
            body()
        self.elapsed = time.monotonic() - t0
        return self

    def cleanup(self):
        self.release.set()
        time.sleep(0.05)

    def alive_hb(self):
        return [t.name for t in threading.enumerate() if t.name.startswith("qhb-") and t.is_alive()]


def task(tid, mode="normal", cfg=True):
    return {"kind": "task", "task_id": tid, "venue": "川口", "race_no": tid % 12 + 1, "ymd": "20261113",
            "task_mode": mode, "config": ({"seed": 1, "save_version": "Ver326"} if cfg else None)}


# ------------------------------------------------------------------ tests
def p5_12(app):
    """shutdown 二重呼び出しの冪等性：正常レースで 3 か所から呼ばれてもエラー無く停止、done は 1 回。"""
    h = Harness(app, tasks=[task(1)]).run()
    joins = []
    ok = (not h.alive_hb() and [s[1] for s in h.status_updates] == ["done"]
          and not any(b.get("status") == "failed" for b in h.bg_updates))
    h.cleanup()
    return ok, {"status_updates": [s[1] for s in h.status_updates], "alive": h.alive_hb()}


def p5_13(app):
    """join timeout → task error、job failed、次レースへ進まない。"""
    h = Harness(app, tasks=[task(1), task(2)], hang=True).run()
    st = [(s[0], s[1]) for s in h.status_updates]
    failed = [b for b in h.bg_updates if b.get("status") == "failed"]
    ok = ((1, "error") in st and (1, "done") not in st and failed and h.picked == [1]
          and isinstance(h.failed_exc, h.g["_V326HeartbeatJoinTimeout"]))
    h.cleanup()
    return ok, {"status_updates": st, "picked": h.picked, "job_failed": bool(failed),
                "failed_exc": type(h.failed_exc).__name__ if h.failed_exc else None}


def p5_14(app):
    """長時間レース＋停止しない heartbeat でも、shutdown 期限（初回呼び出しから join timeout）を守る。"""
    jt = 0.4
    h = Harness(app, tasks=[task(1)], hang=True, join_timeout=jt, work_sleep=1.0).run()
    after_work = h.elapsed - 1.0
    ok = after_work < jt * 1.8   # 3 か所 × timeout（=1.2 秒）にならない
    h.cleanup()
    return ok, {"elapsed_s": round(h.elapsed, 3), "after_work_s": round(after_work, 3), "join_timeout_s": jt}


def p5_15(app):
    """busy / in_flight で err_streak が 0 に戻る。error が連続した時だけ safe_stop。"""
    g = load(app)
    res = {}
    for name, seq in {
        "err,err,busy,err,err,busy,err,err": ["error", "error", "busy", "error", "error", "busy", "error", "error"],
        "err,err,in_flight,err,err": ["error", "error", "in_flight", "error", "error"],
        "err,err,err": ["error", "error", "error"],
    }.items():
        it = iter(seq)
        stop, lost, safe, txa = (threading.Event() for _ in range(4))

        def hb(db, tid, ot, tx_active=None, _it=it, _stop=stop):
            try:
                return next(_it)
            except StopIteration:
                _stop.set(); return "ok"
        g["_v326_queue_heartbeat"] = hb
        g["_v326_hb_timer_loop"]("x", 1, "t", stop, lost, safe, txa, 0.001, 3)
        res[name] = safe.is_set()
    ok = (res["err,err,busy,err,err,busy,err,err"] is False and res["err,err,in_flight,err,err"] is False
          and res["err,err,err"] is True)
    return ok, res


def p5_16(app):
    """前レースの heartbeat が次レースの task を更新しない（全呼び出しが自分の task_id、次レース開始後に前 task なし）。"""
    h = Harness(app, tasks=[task(1), task(2), task(3)], work_sleep=0.15).run()
    starts = {tid: t for t, tid in h.work_starts}
    bad = []
    for (t, tid, ot) in h.hb_calls:
        for nxt in (2, 3):
            if tid < nxt and nxt in starts and t > starts[nxt]:
                bad.append((round(t, 3), tid, nxt))
    per_task = {tid: sum(1 for _, x, _ in h.hb_calls if x == tid) for tid in (1, 2, 3)}
    ok = not bad and all(v > 1 for v in per_task.values()) and not h.alive_hb()
    h.cleanup()
    return ok, {"hb_calls_per_task": per_task, "late_calls_from_prev_task": bad}


def p5_17(app):
    """TX active 中は heartbeat が書かない（in_flight）。解除後は更新。実 SQLite（一時）で確認。"""
    g = load(app)
    d = tempfile.mkdtemp(); db = os.path.join(d, "q.sqlite3")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE v326_import_queue(task_id INTEGER PRIMARY KEY, status TEXT, owner_token TEXT, updated_at TEXT)")
        c.execute("INSERT INTO v326_import_queue VALUES(5,'running','tok','old')")
    evt = threading.Event(); evt.set()
    r1 = g["_v326_queue_heartbeat"](db, 5, "tok", tx_active=evt)
    u1 = sqlite3.connect(db).execute("SELECT updated_at FROM v326_import_queue").fetchone()[0]
    evt.clear()
    r2 = g["_v326_queue_heartbeat"](db, 5, "tok", tx_active=evt)
    u2 = sqlite3.connect(db).execute("SELECT updated_at FROM v326_import_queue").fetchone()[0]
    r3 = g["_v326_queue_heartbeat"](db, 5, "other", tx_active=evt)
    inflight_left = 5 in g["_V326_HB_INFLIGHT"]
    ok = r1 == "in_flight" and u1 == "old" and r2 == "ok" and u2 != "old" and r3 == "lost" and not inflight_left
    return ok, {"tx_active_set": [r1, u1], "tx_active_clear": [r2, u2 != "old"], "other_token": r3,
                "inflight_released": not inflight_left}


def p5_18(app):
    """finally でスレッドが残ったら Worker 停止：版設定エラー（continue 経路）でも次レースへ進まない。"""
    h = Harness(app, tasks=[task(1, cfg=False), task(2)], hang=True).run()
    failed = [b for b in h.bg_updates if b.get("status") == "failed"]
    ok = h.picked == [1] and bool(failed) and isinstance(h.failed_exc, h.g["_V326HeartbeatJoinTimeout"])
    h.cleanup()
    return ok, {"picked": h.picked, "job_failed": bool(failed)}


def p5_19(app):
    """Worker 終了後に thread-local（TX active Event・版設定）が解除されている（正常／失敗の両方）。"""
    a = Harness(app, tasks=[task(1)], run_in_thread=True).run()
    b = Harness(app, tasks=[task(1)], hang=True, run_in_thread=True).run()
    ok = a.tls_after == {"tx_active": None, "config": None} and b.tls_after == {"tx_active": None, "config": None}
    a.cleanup(); b.cleanup()
    return ok, {"normal": a.tls_after, "failed": b.tls_after}


def p5_20(app):
    """本体例外と停止 timeout が同時：item に元例外、Job は failed、例外チェーンで元例外を追える、finally から raise しない。"""
    h = Harness(app, tasks=[task(1), task(2)], hang=True, behavior="raise").run()
    item = h.zombie_items[-1] if h.zombie_items else {}
    exc = h.failed_exc
    cause = getattr(exc, "__cause__", None)
    ok = (isinstance(exc, h.g["_V326HeartbeatJoinTimeout"]) and isinstance(cause, RuntimeError)
          and "RuntimeError: boom" in str(item.get("message")) and "[heartbeat join timeout]" in str(item.get("message"))
          and h.picked == [1])
    h.cleanup()
    # finally 内に raise 文が無いこと（静的確認）
    src = open(app, encoding="utf-8").read()
    tree = ast.parse(src)
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_v319_bg_import_worker")
    raise_in_finally = []
    for n in ast.walk(f):
        if isinstance(n, ast.Try):
            for st in n.finalbody:
                for m in ast.walk(st):
                    if isinstance(m, ast.Raise):
                        raise_in_finally.append(m.lineno)
    ok = ok and not raise_in_finally
    return ok, {"item_message": item.get("message"), "exc": type(exc).__name__ if exc else None,
                "cause": repr(cause), "picked": h.picked, "raise_in_finally": raise_in_finally}


# ------------------------------------------------------------------ P5-01〜09（実コード・一時 DB）
from qver_realcode import Env, ConProxy, iso_ago  # noqa: E402


def _env(app, eng):
    return Env(app, eng)


def p5_01(app, eng):
    """1レース単位の保存TX：成功で6表が揃いqueueはrunningを維持。途中失敗は全体ROLLBACK。"""
    e = _env(app, eng)
    try:
        e.enqueue([("川口", 1), ("川口", 2)])
        t1 = e.pickup("A"); r1, k1 = e.owner_save(t1["task_id"], "A", 1)
        c1 = e.counts(k1); s1 = e.row(t1["task_id"])["status"]
        e.trigger("CREATE TRIGGER f40 BEFORE INSERT ON v40_prediction_feature_snapshots BEGIN SELECT RAISE(ABORT,'inj'); END")
        t2 = e.pickup("A"); r2, k2 = e.owner_save(t2["task_id"], "A", 2)
        c2 = e.counts(k2); s2 = e.row(t2["task_id"])["status"]
        ok = (r1["ok"] and all(v > 0 for v in c1.values()) and s1 == "running"
              and not r2["ok"] and all(v == 0 for v in c2.values()) and s2 == "running")
        return ok, {"success": [c1, s1], "v40_fail": [r2.get("reason"), c2, s2]}
    finally:
        e.close()


def p5_02(app, eng):
    """Worker A/B 同時 pickup（Barrier で同時開始）：1 件なら勝者 1、2 件なら別 task。owner_token は勝者のもの。"""
    e = _env(app, eng)
    try:
        res = {}
        for n in (1, 2):
            with sqlite3.connect(e.db) as c:
                c.execute("DELETE FROM v326_import_queue")
            e.enqueue([("川口", i) for i in range(1, n + 1)])
            bar = threading.Barrier(2); got = {}

            def w(tok):
                bar.wait(); got[tok] = e.pickup(tok)
            ths = [threading.Thread(target=w, args=(t,)) for t in ("A", "B")]
            [t.start() for t in ths]; [t.join(30) for t in ths]
            tasks = {tok: r["task_id"] for tok, r in got.items() if r and r.get("kind") == "task"}
            owners = {tid: e.row(tid)["owner_token"] for tid in tasks.values()}
            res[n] = {"winners": tasks, "db_owner": owners}
        ok = (len(res[1]["winners"]) == 1 and all(res[1]["db_owner"][tid] == tok for tok, tid in res[1]["winners"].items())
              and len(res[2]["winners"]) == 2 and len(set(res[2]["winners"].values())) == 2
              and all(res[2]["db_owner"][tid] == tok for tok, tid in res[2]["winners"].items()))
        return ok, res
    finally:
        e.close()


def p5_03(app, eng):
    """stale 復旧：古い running（他 token・2時間前）だけ pending に戻し、新しい running（他 token）は触らない。"""
    e = _env(app, eng)
    try:
        e.enqueue([("川口", 1), ("川口", 2)])
        a = e.pickup("X"); b = e.pickup("Y")
        e.set_updated_at(a["task_id"], iso_ago(120))
        n = e.ns["_v326_reset_running_to_pending"](e.db, "official_import", "stale", stale_minutes=30, owner_token="Z")
        ra, rb = e.row(a["task_id"]), e.row(b["task_id"])
        ok = n == 1 and ra["status"] == "pending" and ra["owner_token"] is None and rb["status"] == "running" and rb["owner_token"] == "Y"
        return ok, {"reset": n, "stale": [ra["status"], ra["owner_token"]], "fresh": [rb["status"], rb["owner_token"]]}
    finally:
        e.close()


def p5_04(app, eng):
    """heartbeat 中の DB ロック：別接続が書込ロック保持中は busy（lost ではない）。解除後 ok。別 token は lost。"""
    e = _env(app, eng)
    try:
        e.ns["_V326_HB_CONNECT_TIMEOUT_SEC"] = 0.2
        e.enqueue([("川口", 1)]); t = e.pickup("A")
        lock = sqlite3.connect(e.db, timeout=1, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        t0 = time.monotonic()
        r_busy = e.ns["_v326_queue_heartbeat"](e.db, t["task_id"], "A")
        el = time.monotonic() - t0
        lock.execute("ROLLBACK"); lock.close()
        r_ok = e.ns["_v326_queue_heartbeat"](e.db, t["task_id"], "A")
        r_lost = e.ns["_v326_queue_heartbeat"](e.db, t["task_id"], "B")
        jt = float(e.ns["_V326_HB_JOIN_TIMEOUT_SEC"])
        ok = r_busy == "busy" and r_ok == "ok" and r_lost == "lost" and el < jt
        return ok, {"locked": r_busy, "elapsed_s": round(el, 3), "unlocked": r_ok, "other_token": r_lost}
    finally:
        e.close()


def p5_05(app, eng):
    """古い owner A の heartbeat / 状態更新 / 保存TX が、再取得した新 owner B の task を変えない。"""
    e = _env(app, eng)
    try:
        e.enqueue([("川口", 1)])
        t = e.pickup("A"); tid = t["task_id"]
        e.set_updated_at(tid, iso_ago(120))
        e.ns["_v326_reset_running_to_pending"](e.db, "official_import", "stale", stale_minutes=30, owner_token="B")
        t2 = e.pickup("B")
        before = e.row(tid)
        hb = e.ns["_v326_queue_heartbeat"](e.db, tid, "A")
        st = e.ns["_v326_update_task_status"](e.db, tid, "done", "old", owner_token="A")
        sv, key = e.owner_save(tid, "A", 1)
        after = e.row(tid); cnt = e.counts(key)
        ok = (t2["task_id"] == tid and hb == "lost" and st is False and not sv["ok"] and sv.get("reason") == "ownership_lost"
              and after == before and all(v == 0 for v in cnt.values()))
        return ok, {"hb": hb, "status_update": st, "save": sv.get("reason"), "row_unchanged": after == before, "rows": cnt}
    finally:
        e.close()


def p5_06(app, eng):
    """v266 だけ失敗：v266 は SAVEPOINT で巻き戻し、他 5 表は確定しqueueはrunningを維持。"""
    e = _env(app, eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("A")
        e.trigger("CREATE TRIGGER f266 BEFORE INSERT ON v266_pred_time_snapshots BEGIN SELECT RAISE(ABORT,'inj'); END")
        r, k = e.owner_save(t["task_id"], "A", 1)
        c = e.counts(k); s = e.row(t["task_id"])["status"]
        ok = r["ok"] and c["v266"] == 0 and all(c[x] > 0 for x in ("v34", "v40", "v67", "v231", "v222")) and s == "running"
        return ok, {"result": r.get("ok"), "rows": c, "queue": s}
    finally:
        e.close()


def p5_07(app, eng):
    """v34 失敗：全体 ROLLBACK（6 表すべて 0 行）、queue は running のまま、ok=False。"""
    e = _env(app, eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("A")
        e.trigger("CREATE TRIGGER f34 BEFORE INSERT ON prediction_snapshots WHEN NEW.car_no=3 BEGIN SELECT RAISE(ABORT,'inj'); END")
        r, k = e.owner_save(t["task_id"], "A", 1)
        c = e.counts(k); s = e.row(t["task_id"])["status"]
        ok = (not r["ok"]) and all(v == 0 for v in c.values()) and s == "running"
        return ok, {"reason": r.get("reason"), "rows": c, "queue": s}
    finally:
        e.close()


def p5_08(app, eng):
    """SAVEPOINT 巻き戻し失敗（v266 失敗＋ROLLBACK TO 失敗を注入）：成功扱いせず全体 ROLLBACK、queue は running。"""
    e = _env(app, eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("A")
        e.trigger("CREATE TRIGGER f266 BEFORE INSERT ON v266_pred_time_snapshots BEGIN SELECT RAISE(ABORT,'inj'); END")
        proxies = []

        def wrap(con):
            p = ConProxy(con, ["ROLLBACK TO V266_BEST_EFFORT"]); proxies.append(p); return p
        r, k = e.owner_save(t["task_id"], "A", 1, con_wrapper=wrap)
        c = e.counts(k); s = e.row(t["task_id"])["status"]
        ok = (not r["ok"]) and "savepoint_rollback_failed" in str(r.get("reason")) and all(v == 0 for v in c.values()) and s == "running"
        return ok, {"reason": r.get("reason"), "rows": c, "queue": s}
    finally:
        e.close()


def p5_09(app, eng):
    """Streamlit 並行操作：Worker スレッドの版設定は thread-local で UI 側に見えない。
    UI の一括固定（別 snapshot）は未固定 pending だけを変え、Worker の running 行は変えない。"""
    e = _env(app, eng)
    try:
        ns = e.ns
        e.enqueue([("川口", 1)]); t = e.pickup("W")
        with sqlite3.connect(e.db) as c:
            c.execute("INSERT INTO v326_import_queue(job_type,task_mode,ymd,venue,race_no,status,created_at,updated_at) "
                      "VALUES('official_import','normal','20261113','川口',9,'pending',?,?)", (iso_ago(0), iso_ago(0)))
            unfixed = c.execute("SELECT task_id FROM v326_import_queue WHERE race_no=9").fetchone()[0]
        running_before = e.row(t["task_id"])
        cfg_w = {"save_version": "Ver326", "seed": 1, "tag": "worker"}
        ready, done = threading.Event(), threading.Event(); seen = {}

        def worker():
            ns["_v326_set_worker_config_snapshot"](cfg_w)
            ready.set(); done.wait(10)
            seen["worker_after_ui"] = ns["_v326_worker_config_snapshot"]()
            ns["_v326_set_worker_config_snapshot"](None)
        th = threading.Thread(target=worker); th.start(); ready.wait(10)
        seen["ui_view"] = ns["_v326_worker_config_snapshot"]()
        snap_ui = dict(e.ns["_v326_build_queue_config_snapshot"]())
        snap_ui["save_version"] = "Ver330-A"
        snap_ui["config_json"] = json.dumps({"save_version": "Ver330-A", "config_version": 1}, sort_keys=True)
        fix = ns["_v326_bulk_fix_unfixed_pending"](e.db, "official_import", snap_ui)
        done.set(); th.join(10)
        running_after = e.row(t["task_id"]); fixed = e.row(unfixed)
        ok = (seen["ui_view"] is None and seen["worker_after_ui"] == cfg_w and running_after == running_before
              and fixed["save_version"] == "Ver330-A")
        return ok, {"ui_sees_worker_cfg": seen["ui_view"], "worker_cfg_kept": seen["worker_after_ui"] == cfg_w,
                    "bulk_fix": fix, "running_unchanged": running_after == running_before,
                    "unfixed_now": fixed["save_version"]}
    finally:
        e.close()


REAL_TESTS = [("P5-01", p5_01), ("P5-02", p5_02), ("P5-03", p5_03), ("P5-04", p5_04), ("P5-05", p5_05),
              ("P5-06", p5_06), ("P5-07", p5_07), ("P5-08", p5_08), ("P5-09", p5_09)]


def p5_21(app):
    """SafeStop は Worker 全体を停止し、現在 task=error / Job=failed。"""
    h = Harness(app, tasks=[task(1), task(2)], behavior="safe_stop",
                hb_interval=0.01, work_sleep=0.12).run()
    st = [(x[0], x[1]) for x in h.status_updates]
    failed = [x for x in h.bg_updates if x.get("status") == "failed"]
    ok = h.picked == [1] and (1, "error") in st and bool(failed) and not h.alive_hb()
    h.cleanup()
    return ok, {"picked": h.picked, "task_status": st, "job_failed": bool(failed),
                "alive_hb": h.alive_hb()}


def p5_22(app):
    """heartbeat error streak が limit=3 に到達すると safe_stop Event と stop Event が立つ。"""
    g = load(app)
    stop, lost, safe, txa = (threading.Event() for _ in range(4))
    calls = []
    def hb(*a, **k):
        calls.append(1)
        return "error"
    g["_v326_queue_heartbeat"] = hb
    g["_v326_hb_timer_loop"]("x", 1, "tok", stop, lost, safe, txa, 0.001, 3)
    ok = len(calls) == 3 and safe.is_set() and stop.is_set() and not lost.is_set()
    return ok, {"heartbeat_errors": len(calls), "safe_stop": safe.is_set(),
                "stop": stop.is_set(), "ownership_lost": lost.is_set()}


def p5_23(app):
    """SafeStop 後の次 task は pickup されず pending のまま残る。"""
    h = Harness(app, tasks=[task(1), task(2)], behavior="safe_stop",
                hb_interval=0.01, work_sleep=0.12).run()
    pending_left = [x["task_id"] for x in h.tasks]
    ok = h.picked == [1] and pending_left == [2]
    h.cleanup()
    return ok, {"picked": h.picked, "pending_unpicked": pending_left}


def p5_24(app):
    """SafeStop と heartbeat join timeout が競合しても SafeStop のみを処理し、Job failed は一度。"""
    h = Harness(app, tasks=[task(1), task(2)], behavior="safe_stop_hang",
                join_timeout=0.03, hb_interval=0.01, work_sleep=0.04).run()
    failed = [x for x in h.bg_updates if x.get("status") == "failed"]
    st = [(x[0], x[1]) for x in h.status_updates]
    safe_exc = h.g["_V326SafeStop"]
    timeout_exc = h.g["_V326HeartbeatJoinTimeout"]
    ok = (h.picked == [1] and bool(failed) and len(failed) == 1 and (1, "error") in st
          and isinstance(h.failed_exc, safe_exc) and not isinstance(h.failed_exc, timeout_exc))
    h.cleanup()
    return ok, {"picked": h.picked, "task_status": st, "job_failed_updates": len(failed),
                "failed_exception": type(h.failed_exc).__name__ if h.failed_exc else None}


def p5_25(app):
    """OwnershipLost は現仕様どおり次 task へ進み、race ごとに一度ログカウントする。"""
    h = Harness(app, tasks=[task(1), task(2)], behavior="ownership_lost",
                hb_interval=0.01, work_sleep=0.06).run()
    st = [(x[0], x[1]) for x in h.status_updates]
    logs = [line for line in h.logs if "[queue] ownership_lost task=" in line]
    ok = h.picked == [1, 2] and (1, "error") not in st and (2, "done") in st
    ok = ok and len(logs) == 1 and "count=1" in logs[0]
    h.cleanup()
    return ok, {"picked": h.picked, "task_status": st, "ownership_logs": logs}


TESTS = [("P5-12", p5_12), ("P5-13", p5_13), ("P5-14", p5_14), ("P5-15", p5_15), ("P5-16", p5_16),
         ("P5-17", p5_17), ("P5-18", p5_18), ("P5-19", p5_19), ("P5-20", p5_20),
         ("P5-21", p5_21), ("P5-22", p5_22), ("P5-23", p5_23), ("P5-24", p5_24), ("P5-25", p5_25)]

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--app", default="app.py"); ap.add_argument("--engine", default="engine.py")
    a = ap.parse_args()
    npass = nfail = 0
    allt = [(t, (lambda f: (lambda app: f(app, a.engine)))(f)) for t, f in REAL_TESTS] + TESTS
    for tid, fn in allt:
        try:
            ok, info = fn(a.app)
        except Exception as e:
            ok, info = False, {"error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-600:]}
        print(f"{tid} {'PASS' if ok else 'FAIL'} {info}", flush=True)
        npass += bool(ok); nfail += (not ok)
    print(f"--- summary: PASS={npass} FAIL={nfail} SKIP=0 ---", flush=True)
    sys.stdout.flush()
    os._exit(0 if nfail == 0 else 1)

#!/usr/bin/env python3
"""Queue version lock 統合テスト：実際の app.py / engine.py の関数・SQL・呼び出し構造との接続を確認する。

- app.py は import しない（Streamlit を起動しない）。AST で構造を検査し、実関数を一時 SQLite で動かす。
- 本番 DB・Actions・push・deploy には触れない。一時ディレクトリは終了時に削除する。

  python tests/test_queue_version_lock_integration.py [--app app.py] [--engine engine.py]

A 所有権   A1 pickup で owner_token 設定 / A2 heartbeat UPDATE の owner_token 条件 / A3 状態更新の引数・SQL・全呼び出し元
           A4 owner_token 不一致は rowcount=0（False）
B 寿命     B1 Event をレースごとに生成 / B2 _hb_ctx をレースごとに生成 / B3 hb thread は固定引数・daemon=False
           B4 停止できなければ次 task を pickup しない（順序）/ B5 1 回の heartbeat 上限 < join timeout
C 例外保存 C1 abort_cb が保存TXまで伝播（構造＋実行）/ C2 保存直前の所有権再確認 / C3 6 保存関数＋done が同一接続・commit=False
           C4 保存失敗で rollback / C5 finally 内で raise しない（元例外を置き換えない）/ C6 保存TX中は tx_active が立つ
D 後処理   D1 停止後の heartbeat thread は DB を書かない / D2 Worker 終了時の thread-local 解除
           D3 古い Worker が新 owner の履歴・queue を変えない
"""
import argparse, ast, os, re, sqlite3, sys, threading, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qver_realcode import Env, iso_ago  # noqa: E402


def fn_node(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def src_of(src, node):
    return ast.get_source_segment(src, node)


def calls(node, name):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if nm == name:
                out.append(n)
    return out


def kw(call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


class Ctx:
    def __init__(self, app, eng):
        self.app, self.eng = app, eng
        self.src = open(app, encoding="utf-8").read()
        self.tree = ast.parse(self.src)
        self.worker = fn_node(self.tree, "_v319_bg_import_worker")
        self.loop = next(n for n in ast.walk(self.worker) if isinstance(n, ast.While))


# ---------------------------------------------------------------- A
def a1(c):
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("TOK-A")
        r = e.row(t["task_id"])
        return (t["owner_token"] == "TOK-A" and r["status"] == "running" and r["owner_token"] == "TOK-A"), \
            {"returned": t["owner_token"], "db": [r["status"], r["owner_token"]]}
    finally:
        e.close()


def a2(c):
    s = src_of(c.src, fn_node(c.tree, "_v326_queue_heartbeat"))
    m = re.search(r"UPDATE v326_import_queue(.*?)\"\"\"", s, re.S)
    sql = " ".join(m.group(1).split()) if m else ""
    ok = "task_id = ?" in sql and "status = 'running'" in sql and "owner_token = ?" in sql and "rowcount" in s
    return ok, {"sql": sql}


def a3(c):
    f = fn_node(c.tree, "_v326_update_task_status")
    kwonly = [a.arg for a in f.args.kwonlyargs]
    no_default = f.args.kw_defaults[kwonly.index("owner_token")] is None if "owner_token" in kwonly else False
    s = src_of(c.src, f)
    sql_ok = bool(re.search(r"WHERE[^\"]*status\s*=\s*'running'[^\"]*owner_token\s*=\s*\?", s, re.S))
    returns_bool = "-> bool" in s.split(":\n", 1)[0] and "rowcount" in s
    callers = []
    for n in ast.walk(c.tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_v326_update_task_status":
            callers.append((n.lineno, kw(n, "owner_token") is not None))
    ok = no_default and sql_ok and returns_bool and callers and all(x for _, x in callers)
    return ok, {"owner_token_kwonly_required": no_default, "sql_running_and_owner": sql_ok,
                "returns_bool": returns_bool, "callers": callers}


def a4(c):
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("A")
        wrong = e.ns["_v326_update_task_status"](e.db, t["task_id"], "done", "x", owner_token="B")
        st1 = e.row(t["task_id"])["status"]
        right = e.ns["_v326_update_task_status"](e.db, t["task_id"], "done", "x", owner_token="A")
        st2 = e.row(t["task_id"])["status"]
        return (wrong is False and st1 == "running" and right is True and st2 == "done"), \
            {"wrong_token": [wrong, st1], "right_token": [right, st2]}
    finally:
        e.close()


# ---------------------------------------------------------------- B
def _assigned_in_loop(c, pred):
    names = []
    for n in ast.walk(c.loop):
        if isinstance(n, ast.Assign) and pred(n.value):
            names += [t.id for t in n.targets if isinstance(t, ast.Name)]
    return names


def b1(c):
    ev = _assigned_in_loop(c, lambda v: isinstance(v, ast.Call) and getattr(v.func, "attr", "") == "Event")
    need = {"_ownership_lost", "_safe_stop", "_hb_stop", "_hb_tx_active"}
    outside = [n.lineno for n in ast.walk(c.worker) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
               and getattr(n.value.func, "attr", "") == "Event"
               and not (c.loop.lineno <= n.lineno <= c.loop.end_lineno)]
    return need <= set(ev) and not outside, {"events_in_loop": ev, "events_outside_loop": outside}


def b2(c):
    ctx = _assigned_in_loop(c, lambda v: isinstance(v, ast.Dict))
    closures = [n for n in ast.walk(c.loop) if isinstance(n, ast.FunctionDef)]
    bound = {f.name: [a.arg for a in f.args.args] for f in closures}
    ok = "_hb_ctx" in ctx and "_hb_shutdown" in bound and any(a.startswith("_") for a in bound.get("_abort_cb", []))
    return ok, {"dicts_in_loop": ctx, "closure_default_args": bound}


def b3(c):
    th = [n for n in calls(c.loop, "Thread") if isinstance(kw(n, "target"), ast.Name) and kw(n, "target").id == "_v326_hb_timer_loop"]
    if not th:
        return False, {"error": "hb thread not found"}
    t = th[0]
    args = kw(t, "args")
    daemon = kw(t, "daemon")
    loop_fn = fn_node(c.tree, "_v326_hb_timer_loop")
    # 関数外（Worker のローカル）を参照していないこと：読込名 − 引数 − 関数内代入 − モジュール名 − 組込
    import builtins
    params = {a.arg for a in loop_fn.args.args + loop_fn.args.kwonlyargs}
    stores = {n.id for n in ast.walk(loop_fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    module = {n.name for n in c.tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for n in c.tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            module |= {t.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            module |= {(x.asname or x.name).split(".")[0] for x in n.names}
    loads = {n.id for n in ast.walk(loop_fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    free = loads - params - stores - module - set(dir(builtins))
    if loop_fn in [n for n in ast.walk(c.worker)]:
        free.add("<nested in worker>")
    ok = (isinstance(args, ast.Tuple) and isinstance(daemon, ast.Constant) and daemon.value is False
          and not free and len(loop_fn.args.args) >= 8)
    return ok, {"args": src_of(c.src, args), "daemon": src_of(c.src, daemon), "loop_free_refs": sorted(free)}


def b4(c):
    body_first = c.loop.body
    zcalls = sorted(n.lineno for n in calls(c.loop, "_v326_raise_hb_zombie"))
    pick = sorted(n.lineno for n in calls(c.loop, "_v326_pickup_next"))
    ok = zcalls and pick and zcalls[0] < pick[0]
    return ok, {"zombie_check_lines": zcalls, "pickup_lines": pick}


def b5(c):
    e = Env(c.app, c.eng)
    try:
        ns = e.ns
        worst = float(ns["_V326_HB_CONNECT_TIMEOUT_SEC"]) * int(ns["_V326_HB_WRITE_RETRIES"]) + 1.0
        jt = float(ns["_V326_HB_JOIN_TIMEOUT_SEC"])
        return worst < jt, {"hb_worst_case_s": worst, "join_timeout_s": jt}
    finally:
        e.close()


# ---------------------------------------------------------------- C
def c1(c):
    imp = fn_node(c.tree, "_v319_import_one_race")
    pre = fn_node(c.tree, "_v319_run_prerace_prediction")
    w_pass = any(kw(n, "abort_cb") is not None for n in calls(c.loop, "_v319_import_one_race"))
    i_pass = any(kw(n, "abort_cb") is not None for n in calls(imp, "_v319_run_prerace_prediction"))
    p_pass = any(kw(n, "check_abort") is not None for n in calls(pre, "_v326_owner_save_tx"))
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("A")
        Lost = e.ns["_V326OwnershipLost"]

        def abort():
            raise Lost()
        r, k = e.owner_save(t["task_id"], "A", 1, abort=abort)
        cnt = e.counts(k); st = e.row(t["task_id"])["status"]
        dyn = r.get("reason") == "ownership_lost" and all(v == 0 for v in cnt.values()) and st == "running"
    finally:
        e.close()
    ok = w_pass and i_pass and p_pass and dyn
    return ok, {"worker->import_one_race": w_pass, "import->prerace": i_pass, "prerace->save_tx": p_pass,
                "abort_runtime": [r.get("reason"), cnt, st]}


def c2(c):
    s = src_of(c.src, fn_node(c.tree, "_v326_owner_save_tx"))
    i_begin = s.find('BEGIN IMMEDIATE')
    i_sel = s.find("WHERE task_id=? AND status='running' AND owner_token=?")
    i_v34 = s.find("v34_save_prediction_snapshot")
    i_done = s.find("SET status='done'")
    ok = 0 <= i_begin < i_sel < i_v34 < i_done and "rowcount" in s[i_done:]
    return ok, {"order_BEGIN<ownerSELECT<v34<done": [i_begin, i_sel, i_v34, i_done]}


def c3(c):
    f = fn_node(c.tree, "_v326_owner_save_tx")
    names = ["v34_save_prediction_snapshot", "_v231_save_prediction_history", "_v222_save_prediction_restore",
             "_v266_save_pred_time_snapshot", "v67_save_ticket_snapshot", "v40_save_prediction_features"]
    res = {}
    for nm in names:
        cs = calls(f, nm)
        res[nm] = bool(cs) and all(
            isinstance(kw(x, "con"), ast.Name) and kw(x, "con").id == "con"
            and isinstance(kw(x, "commit"), ast.Constant) and kw(x, "commit").value is False for x in cs)
    commits = [n.lineno for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "commit"]
    return all(res.values()) and not commits, {"con=con,commit=False": res, "con.commit()_calls": commits}


def c4(c):
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1), ("川口", 2)])
        res = {}
        for tbl, trig in [("v222", "CREATE TRIGGER f BEFORE INSERT ON v222_prediction_restore BEGIN SELECT RAISE(ABORT,'inj'); END"),
                          ("v67", "CREATE TRIGGER f BEFORE INSERT ON v67_prediction_tickets BEGIN SELECT RAISE(ABORT,'inj'); END")]:
            t = e.pickup("A")
            e.trigger(trig)
            r, k = e.owner_save(t["task_id"], "A", t["race_no"])
            e.trigger("DROP TRIGGER f")
            res[tbl] = [r["ok"], e.counts(k), e.row(t["task_id"])["status"]]
        ok = all((not v[0]) and all(x == 0 for x in v[1].values()) and v[2] == "running" for v in res.values())
        return ok, res
    finally:
        e.close()


def c5(c):
    bad = {}
    for nm in ("_v319_bg_import_worker", "_v326_owner_save_tx"):
        f = fn_node(c.tree, nm)
        lines = []
        for n in ast.walk(f):
            if isinstance(n, ast.Try):
                for st in n.finalbody:
                    lines += [m.lineno for m in ast.walk(st) if isinstance(m, ast.Raise)]
        bad[nm] = lines
    s = src_of(c.src, c.worker)
    keeps = "_orig_msg" in s and "raise _V326HeartbeatJoinTimeout" not in s  # 送出は関数側で from cause
    z = src_of(c.src, fn_node(c.tree, "_v326_raise_hb_zombie"))
    ok = not any(bad.values()) and keeps and "raise exc from cause" in z
    return ok, {"raise_in_finally": bad, "orig_msg_kept": keeps, "raise_from_cause": "raise exc from cause" in z}


def c6(c):
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1), ("川口", 2)])
        evt = threading.Event(); seen = []
        orig = e.eng.v34_save_prediction_snapshot

        def spy(*a, **k):
            seen.append(evt.is_set())
            hb = e.ns["_v326_queue_heartbeat"](e.db, cur["task_id"], "A", tx_active=evt)
            seen.append(hb)
            return orig(*a, **k)
        e.eng.v34_save_prediction_snapshot = spy
        cur = e.pickup("A")
        r1, _ = e.owner_save(cur["task_id"], "A", 1, tx_evt=evt)
        after_ok = evt.is_set()
        # BEGIN 失敗でも解除される
        from qver_realcode import ConProxy
        cur = e.pickup("A")
        r2, _ = e.owner_save(cur["task_id"], "A", 2, tx_evt=evt, con_wrapper=lambda con: ConProxy(con, ["BEGIN IMMEDIATE"]))
        after_fail = evt.is_set()
        e.eng.v34_save_prediction_snapshot = orig
        ok = seen[:2] == [True, "in_flight"] and r1["ok"] and not after_ok and not r2["ok"] and not after_fail
        return ok, {"during_tx": seen[:2], "cleared_after_commit": not after_ok,
                    "begin_fail": r2.get("reason"), "cleared_after_begin_fail": not after_fail}
    finally:
        e.close()


# ---------------------------------------------------------------- D
def d1(c):
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1)]); t = e.pickup("A"); tid = t["task_id"]
        stop, lost, safe, txa = (threading.Event() for _ in range(4))
        th = threading.Thread(target=e.ns["_v326_hb_timer_loop"],
                              args=(e.db, tid, "A", stop, lost, safe, txa, 0.02, 3), daemon=False)
        th.start(); time.sleep(0.15)
        stop.set(); th.join(5)
        e.set_updated_at(tid, "MARK")
        time.sleep(0.2)
        v = e.row(tid)["updated_at"]
        return (not th.is_alive() and v == "MARK"), {"thread_alive": th.is_alive(), "updated_after_stop": v != "MARK"}
    finally:
        e.close()


def d2(c):
    fin = [n for n in ast.walk(c.worker) if isinstance(n, ast.Try) and n.finalbody
           and n.lineno < c.loop.lineno and n.end_lineno > c.loop.end_lineno]
    txt = "".join(src_of(c.src, st) for t in fin for st in t.finalbody)
    ok = "_v326_set_worker_tx_active_event(None)" in txt and "_v326_set_worker_config_snapshot(None)" in txt
    return ok, {"outer_finally_clears_tx_event": "_v326_set_worker_tx_active_event(None)" in txt,
                "outer_finally_clears_config": "_v326_set_worker_config_snapshot(None)" in txt,
                "runtime_check": "P5-19"}


def d3(c):
    e = Env(c.app, c.eng)
    try:
        e.enqueue([("川口", 1)])
        t = e.pickup("OLD"); tid = t["task_id"]
        e.set_updated_at(tid, iso_ago(120))
        e.ns["_v326_reset_running_to_pending"](e.db, "official_import", "stale", stale_minutes=30, owner_token="NEW")
        e.pickup("NEW")
        r_new, k = e.owner_save(tid, "NEW", 1)
        snap = (e.counts(k), e.row(tid))
        r_old, _ = e.owner_save(tid, "OLD", 1)
        st = e.ns["_v326_update_task_status"](e.db, tid, "error", "old worker", owner_token="OLD")
        after = (e.counts(k), e.row(tid))
        ok = r_new["ok"] and not r_old["ok"] and st is False and after == snap
        return ok, {"new_owner_save": r_new["ok"], "old_owner_save": r_old.get("reason"), "old_status_update": st,
                    "history_and_queue_unchanged": after == snap}
    finally:
        e.close()


TESTS = [("A1", a1), ("A2", a2), ("A3", a3), ("A4", a4), ("B1", b1), ("B2", b2), ("B3", b3), ("B4", b4), ("B5", b5),
         ("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4), ("C5", c5), ("C6", c6), ("D1", d1), ("D2", d2), ("D3", d3)]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="app.py"); ap.add_argument("--engine", default="engine.py")
    a = ap.parse_args()
    ctx = Ctx(a.app, a.engine)
    npass = nfail = 0
    for tid, fn in TESTS:
        try:
            ok, info = fn(ctx)
        except Exception as e:
            ok, info = False, {"error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-800:]}
        print(f"{tid} {'PASS' if ok else 'FAIL'} {info}", flush=True)
        npass += bool(ok); nfail += (not ok)
    print(f"--- summary: PASS={npass} FAIL={nfail} SKIP=0 ---", flush=True)
    print("Scope: real app.py/engine.py functions via AST extraction on temporary SQLite. No production DB/Actions/push/deploy.")
    os._exit(0 if nfail == 0 else 1)

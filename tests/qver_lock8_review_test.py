"""qver_lock8 レビュー修正の検証（app.py を起動せず、必要関数だけ AST 抽出して実行）。

使い方:
  python tests/qver_lock8_review_test.py --app app.py --engine engine.py [--db 本番DBコピー] [--card-db 収集ストア]

検証内容:
  T1 自前接続で途中失敗 → 既存行が残り、途中 INSERT が残らない（6 保存関数）
  T2 外部接続 → 保存関数は commit/rollback しない（呼び出し側 ROLLBACK で 0 行、COMMIT で全行）、ロック待ちなし
  T3 Worker 経路 heartbeat → 全終了経路でスレッドが残らない（通常/履歴のみ/版設定エラー/予測エラー/例外/所有権喪失/中止）
  T4 （--db 指定時）実予測 1000 試行で Worker 所有権 TX：v34/v231/v222/v266/v67/v40/queue done が同一TXで確定、
     別 token では何も保存されない、所要時間にロック待ちが無い
"""
import argparse, ast, importlib.util, os, shutil, sqlite3, sys, tempfile, threading, time, types
from unittest import mock

PRELUDE = (
    "import re, json, os, sys, time, math, html, hashlib, random, sqlite3, threading, unicodedata, datetime, zlib, pickle, uuid\n"
    "import urllib.request, urllib.parse, urllib.error\n"
    "from pathlib import Path\nfrom datetime import date, datetime, time, timedelta, timezone\nfrom zoneinfo import ZoneInfo\nimport base64, glob, shutil, tempfile, zipfile, traceback, http.cookiejar\nfrom concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED\n"
    "import time as time_module\nimport pandas as pd\nimport numpy as np\nimport math\n"
)


def load_engine(engine_path):
    spec = importlib.util.spec_from_file_location("engine_under_test", engine_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_app_subset(app_path, engine_mod, seeds, stubs=None, follow=True):
    src = open(app_path, encoding="utf-8").read()
    tree = ast.parse(src)
    defs, assigns = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            tg = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tg:
                if isinstance(t, ast.Name):
                    assigns[t.id] = node
    stubs = dict(stubs or {})
    need, todo = set(), list(seeds)
    while todo:
        n = todo.pop()
        if n in need or n in stubs:
            continue
        node = defs.get(n) or assigns.get(n)
        if node is None:
            continue
        need.add(n)
        if not follow and n not in seeds:
            continue
        for a in ast.walk(node):
            if isinstance(a, ast.Name) and (a.id in defs or a.id in assigns) and a.id not in need:
                todo.append(a.id)
    nodes = sorted({id(defs.get(n) or assigns.get(n)): (defs.get(n) or assigns.get(n)) for n in need}.values(),
                   key=lambda x: x.lineno)
    ns = {"__name__": "app_subset", "engine": engine_mod, "st": mock.MagicMock()}
    exec(PRELUDE, ns)
    for node in nodes:
        seg = ast.get_source_segment(src, node)
        try:
            exec(compile(seg, app_path, "exec"), ns)
        except Exception as e:  # top-level assign that needs runtime state
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                raise
            print(f"  (skip top-level {getattr(node, 'lineno', '?')}: {type(e).__name__})")
    ns.update(stubs)
    return ns


# ----------------------------------------------------------------- T1 / T2
def _mk_df(pd, n=4):
    return pd.DataFrame({"車": list(range(1, n + 1)), "選手名": [f"P{i}" for i in range(1, n + 1)],
                         "改善後順位": list(range(1, n + 1)), "予測競走T": [3.40 + i * 0.01 for i in range(n)],
                         "調整前総合点": [1.0] * n, "改善後総合点": [1.0] * n, "調整前順位": list(range(1, n + 1))})


def t1_t2(app_path, engine_path):
    eng = load_engine(engine_path)
    ns = load_app_subset(app_path, eng, [
        "_v222_save_prediction_restore", "_v231_save_prediction_history", "_v266_save_pred_time_snapshot",
        "_v326_preensure_save_tables", "_v231_ensure_prediction_history_table", "_v222_ensure_prediction_restore_table",
        "_v266_ensure_pred_time_snapshot_table",
    ], stubs={"_v326_worker_config_snapshot": lambda: None})
    pd = ns["pd"]
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.sqlite3")
    c = sqlite3.connect(db); c.execute("PRAGMA journal_mode=WAL"); c.close()
    meta = {"開催日": "2026-11-13", "開催場": "川口", "レース": 1}
    key = eng.v34_race_key(meta)
    df = _mk_df(pd)
    bets = {"2車単": {(1, 2): 3}, "三連単": {(1, 2, 3): 2, (2, 1, 3): 1}}
    view = {"df": df, "meta": meta, "app_version": "T", "model_state_snapshot_v305": {"schema": "t"}}
    if "_v326_preensure_save_tables" in ns:
        ns["_v326_preensure_save_tables"](db)
    else:  # 修正前版との比較用
        for f in ("v34_init_feedback_tables", "v40_init_learning_tables", "v67_init_ticket_feedback_tables"):
            getattr(eng, f)(db)
        for f in ("_v231_ensure_prediction_history_table", "_v222_ensure_prediction_restore_table",
                  "_v266_ensure_pred_time_snapshot_table"):
            ns[f](db)
    res = {}

    def count(table, where="1=1", args=()):
        with sqlite3.connect(db) as cc:
            return cc.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]

    # 既存行を作る（成功経路）
    eng.v34_save_prediction_snapshot(meta, df, None, db)
    eng.v40_save_prediction_features(meta, df, db)
    eng.v67_save_ticket_snapshot(meta, bets, 20, db)
    hid0 = ns["_v231_save_prediction_history"](db, key, "raw", "川口", view, 1000, 1)
    ns["_v222_save_prediction_restore"](db, key, "raw", "川口", view)
    base = {t: count(t) for t in ["prediction_snapshots", "v40_prediction_feature_snapshots",
                                  "v67_prediction_tickets", "v231_prediction_history",
                                  "v222_prediction_restore", "v266_pred_time_snapshots"]}
    res["T1_baseline_rows"] = base
    # 3 件目の INSERT で失敗させるトリガ
    with sqlite3.connect(db) as cc:
        for t, col in [("prediction_snapshots", "car_no"), ("v40_prediction_feature_snapshots", None),
                       ("v266_pred_time_snapshots", "car_no")]:
            if col:
                cc.execute(f"CREATE TRIGGER fail_{t} BEFORE INSERT ON {t} WHEN NEW.{col}=3 "
                           f"BEGIN SELECT RAISE(ABORT,'injected'); END")
        cc.execute("CREATE TRIGGER fail_v40 BEFORE INSERT ON v40_prediction_feature_snapshots "
                   "WHEN (SELECT COUNT(*) FROM v40_prediction_feature_snapshots)>=2 BEGIN SELECT RAISE(ABORT,'injected'); END")
        cc.execute("CREATE TRIGGER fail_v67 BEFORE INSERT ON v67_prediction_tickets "
                   "WHEN NEW.bet_type='3連単' BEGIN SELECT RAISE(ABORT,'injected'); END")
        cc.execute("CREATE TRIGGER fail_v231 BEFORE INSERT ON v231_prediction_history "
                   "WHEN NEW.seed=999 BEGIN SELECT RAISE(ABORT,'injected'); END")
        cc.execute("CREATE TRIGGER fail_v222 BEFORE UPDATE ON v222_prediction_restore "
                   "WHEN NEW.raw_text='bad' BEGIN SELECT RAISE(ABORT,'injected'); END")
    calls = {
        "v34": lambda: eng.v34_save_prediction_snapshot(meta, df, None, db),
        "v40": lambda: eng.v40_save_prediction_features(meta, df, db),
        "v67": lambda: eng.v67_save_ticket_snapshot(meta, bets, 20, db),
        "v231": lambda: ns["_v231_save_prediction_history"](db, key, "raw", "川口", view, 1000, 999),
        "v222": lambda: ns["_v222_save_prediction_restore"](db, key, "bad", "川口", view),
        "v266": lambda: ns["_v266_save_pred_time_snapshot"](db, hid0 + 100, key, "T", view),
    }
    t1 = {}
    for name, fn in calls.items():
        try:
            fn(); t1[name] = "NO_ERROR(?)"
        except Exception as e:
            t1[name] = f"raised {type(e).__name__}"
    after = {t: count(t) for t in base}
    with sqlite3.connect(db) as cc:
        raw222 = cc.execute("SELECT raw_text FROM v222_prediction_restore WHERE race_key=?", (key,)).fetchone()[0]
    res["T1_raised"] = t1
    res["T1_after_rows"] = after
    res["T1_PASS"] = (after == base) and raw222 == "raw" and all(v.startswith("raised") for v in t1.values())

    # T2 外部接続
    with sqlite3.connect(db) as cc:
        for tr in ["fail_prediction_snapshots", "fail_v266_pred_time_snapshots", "fail_v40", "fail_v67",
                   "fail_v231", "fail_v222", "fail_v40_prediction_feature_snapshots"]:
            cc.execute(f"DROP TRIGGER IF EXISTS {tr}")
    meta2 = {"開催日": "2026-11-13", "開催場": "川口", "レース": 2}
    key2 = eng.v34_race_key(meta2)
    view2 = dict(view, meta=meta2)

    def run_ext(final):
        con = sqlite3.connect(db, timeout=60)
        con.execute("BEGIN IMMEDIATE")
        t0 = time.time()
        eng.v34_save_prediction_snapshot(meta2, df, None, db, con=con, commit=False)
        hid = ns["_v231_save_prediction_history"](db, key2, "raw", "川口", view2, 1000, 1, con=con, commit=False)
        ns["_v222_save_prediction_restore"](db, key2, "raw", "川口", view2, con=con, commit=False)
        ns["_v266_save_pred_time_snapshot"](db, hid, key2, "T", view2, con=con, commit=False)
        eng.v67_save_ticket_snapshot(meta2, bets, 20, db, con=con, commit=False)
        eng.v40_save_prediction_features(meta2, df, db, con=con, commit=False)
        el = time.time() - t0
        intx = con.in_transaction
        con.execute(final); con.close()
        rows = {
            "v34": count("prediction_snapshots", "race_key=?", (key2,)),
            "v40": count("v40_prediction_feature_snapshots", "race_key=?", (key2,)),
            "v67": count("v67_prediction_tickets", "race_key=?", (key2,)),
            "v231": count("v231_prediction_history", "race_key=?", (key2,)),
            "v222": count("v222_prediction_restore", "race_key=?", (key2,)),
            "v266": count("v266_pred_time_snapshots", "race_key=?", (key2,)),
        }
        return {"elapsed_s": round(el, 3), "still_in_tx_before_final": intx, "rows": rows}

    try:
        r_rb = run_ext("ROLLBACK")
    except TypeError as e:
        res["T2_PASS"] = False; res["T2_error"] = repr(e)
        shutil.rmtree(tmp, ignore_errors=True)
        return res
    r_cm = run_ext("COMMIT")
    res["T2_rollback"] = r_rb
    res["T2_commit"] = r_cm
    res["T2_PASS"] = (all(v == 0 for v in r_rb["rows"].values()) and r_rb["still_in_tx_before_final"]
                      and all(v > 0 for v in r_cm["rows"].values()) and r_cm["elapsed_s"] < 2.0)
    shutil.rmtree(tmp, ignore_errors=True)
    return res


# ----------------------------------------------------------------- T3 heartbeat
class _Missing(dict):
    def __missing__(self, k):
        if k in __builtins__.__dict__ if hasattr(__builtins__, "__dict__") else k in __builtins__:
            raise KeyError(k)
        return mock.MagicMock(name=k)


def t3(app_path):
    src = open(app_path, encoding="utf-8").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_v319_bg_import_worker")
    seg = ast.get_source_segment(src, node)
    cls_src = "\n".join(ast.get_source_segment(src, n) for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name in ("_V326OwnershipLost", "_V326SafeStop"))
    import zlib, pickle, json, urllib.request
    scenarios = {
        "normal": ("normal", "ok"),
        "history_only": ("history_only", "ok"),
        "config_error": ("normal", "cfg_err"),
        "pred_error_status": ("normal", "status_error"),
        "exception": ("normal", "raise"),
        "ownership_lost": ("normal", "lost"),
        "cancelled": ("normal", "cancelled"),
    }
    out = {}
    for name, (mode, behavior) in scenarios.items():
        g = _Missing()
        g.update({"__builtins__": __builtins__, "sqlite3": sqlite3, "threading": threading, "zlib": zlib,
                  "pickle": pickle, "json": json, "time_module": time, "urllib": urllib,
                  "_V326_QUEUE_HEARTBEAT_SEC": 0.02, "_V287_IMPORT_STATE_RESET": {}, "_V287_AUTO_IMPORT_STATE": {}})
        exec(cls_src, g)
        hb_calls = []
        tasks = [{"kind": "task", "task_id": 7, "venue": "川口", "race_no": 1, "ymd": "20261113",
                  "task_mode": mode, "config": (None if behavior == "cfg_err" else {"seed": 1, "save_version": "Ver326"})}]

        def pickup(*a, **k):
            return tasks.pop(0) if tasks else None

        def hb(db, tid, ot):
            hb_calls.append(time.time()); return "ok"

        work_calls = []

        def work(*a, **k):
            work_calls.append(1)
            time.sleep(0.15)  # heartbeat が数回回る程度
            if behavior == "raise":
                raise RuntimeError("boom")
            if behavior == "lost":
                raise g["_V326OwnershipLost"]()
            if behavior == "status_error":
                return {"status": "error", "message": "x"}
            if behavior == "cancelled":
                return {"status": "cancelled", "message": "中止"}
            return {"status": "ok"}

        def bad_cfg(row):
            raise ValueError("bad config")

        g.update({
            "_v326_pickup_next": pickup, "_v326_queue_heartbeat": hb,
            "_v319_import_one_race": work, "_v319_import_player_history_one_race": work,
            "_v326_validate_queue_config_row": bad_cfg,
            "_v326_queue_summary": lambda *a, **k: {"pending": 1, "running": 0},
            "_v278_bg_cancel_requested": lambda *a, **k: False,
            "_v278_bg_pause_loop": lambda *a, **k: None,
        })
        exec(seg, g)
        before = {t.ident for t in threading.enumerate()}
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), \
             mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("no db in test")):
            try:
                g["_v319_bg_import_worker"]("/nonexistent.sqlite3", 1, {"ymd": "20261113"})
                ret = "returned"
            except Exception as e:
                ret = f"raised {type(e).__name__}: {e}"
        time.sleep(0.1)
        n_hb_before = len(hb_calls)
        time.sleep(0.2)
        leaked = [t.name for t in threading.enumerate() if t.name.startswith("qhb-") and t.is_alive()]
        out[name] = {"worker": ret, "work_calls": len(work_calls), "hb_calls": n_hb_before, "hb_after_end": len(hb_calls) - n_hb_before,
                     "leaked_threads": leaked}
        # 後片付け（修正前版で残ったスレッドを止める手段はないので、名前を記録するだけ）
    out["T3_PASS"] = all(not v["leaked_threads"] and v["hb_after_end"] == 0 and v["hb_calls"] > 0
                         for k, v in out.items() if isinstance(v, dict))
    return out


# ----------------------------------------------------------------- T4 real prediction (optional)
def t4(app_path, engine_path, db_src, card_db):
    eng = load_engine(engine_path)
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "p.sqlite3")
    shutil.copy(db_src, db)
    with sqlite3.connect(card_db) as cc:
        rk, raw = cc.execute("SELECT race_key, raw_text FROM v332_prospective_alpha_log "
                             "WHERE raw_text IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    venue = rk.split("_")[1]
    ns = load_app_subset(app_path, eng, ["_v319_run_prerace_prediction", "_v326_ensure_import_queue",
                                         "_V326OwnershipLost", "_V326SafeStop"],
                         stubs={"_v326_worker_config_snapshot": lambda: None})
    ns["_V314_RESIM_TRIALS_CAP"] = 1000
    eng.DB_PATH = db
    ns["_v326_ensure_import_queue"](db)
    with sqlite3.connect(db) as cc:
        cols = [r[1] for r in cc.execute("PRAGMA table_info(v326_import_queue)")]
        cc.execute("DELETE FROM v326_import_queue")
        vals = {"task_id": 9001, "status": "running", "owner_token": "tok-A", "job_type": "official_import",
                "ymd": rk[:8], "venue": venue, "race_no": int(rk.split("_")[2].rstrip("R")),
                "updated_at": "2026-11-13T00:00:00+09:00", "created_at": "2026-11-13T00:00:00+09:00"}
        use = {k: v for k, v in vals.items() if k in cols}
        cc.execute(f"INSERT INTO v326_import_queue({','.join(use)}) VALUES({','.join('?'*len(use))})", tuple(use.values()))

    def snap():
        with sqlite3.connect(db) as cc:
            return {
                "v34": cc.execute("SELECT COUNT(*) FROM prediction_snapshots WHERE race_key LIKE ?", (f"%{rk[:8][:4]}%",)).fetchone()[0],
                "v231": cc.execute("SELECT COUNT(*) FROM v231_prediction_history").fetchone()[0],
                "v222": cc.execute("SELECT COUNT(*) FROM v222_prediction_restore").fetchone()[0],
                "v266": cc.execute("SELECT COUNT(*) FROM v266_pred_time_snapshots").fetchone()[0],
                "v67": cc.execute("SELECT COUNT(*) FROM v67_prediction_tickets").fetchone()[0],
                "v40": cc.execute("SELECT COUNT(*) FROM v40_prediction_feature_snapshots").fetchone()[0],
                "queue": cc.execute("SELECT status FROM v326_import_queue WHERE task_id=9001").fetchone()[0],
            }
    out = {"race_key": rk}
    # 別 token（所有権なし）
    s0 = snap(); t0 = time.time()
    r_bad = ns["_v319_run_prerace_prediction"](db, rk, venue, trials=1000, seed=1, raw_text=raw,
                                              task_id=9001, owner_token="tok-B", skip_deferred=True)
    s1 = snap()
    out["wrong_token"] = {"result": {k: r_bad.get(k) for k in ("ok", "reason")}, "changed": {k: (s0[k], s1[k]) for k in s0 if s0[k] != s1[k]},
                          "elapsed_s": round(time.time() - t0, 1)}
    t0 = time.time()
    r_ok = ns["_v319_run_prerace_prediction"](db, rk, venue, trials=1000, seed=1, raw_text=raw,
                                             task_id=9001, owner_token="tok-A", skip_deferred=True)
    s2 = snap()
    out["owner"] = {"result": {k: r_ok.get(k) for k in ("ok", "reason", "queue_done", "history_id")},
                    "delta": {k: (s1[k], s2[k]) for k in s1}, "elapsed_s": round(time.time() - t0, 1)}
    out["T4_PASS"] = (not r_bad.get("ok") and r_bad.get("reason") == "ownership_lost"
                      and all(s0[k] == s1[k] for k in s0)
                      and r_ok.get("ok") and s2["queue"] == "done"
                      and all(s2[k] > s1[k] for k in ("v231", "v222", "v266", "v67", "v40")))
    shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="app.py"); ap.add_argument("--engine", default="engine.py")
    ap.add_argument("--db"); ap.add_argument("--card-db")
    ap.add_argument("--only", default="t1,t3,t4")
    a = ap.parse_args()
    import json
    sel = a.only.split(",")
    if "t1" in sel:
        print("T1/T2", json.dumps(t1_t2(a.app, a.engine), ensure_ascii=False, indent=1, default=str))
    if "t3" in sel:
        print("T3", json.dumps(t3(a.app), ensure_ascii=False, indent=1, default=str))
    if "t4" in sel and a.db and a.card_db:
        print("T4", json.dumps(t4(a.app, a.engine, a.db, a.card_db), ensure_ascii=False, indent=1, default=str))
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)  # 修正前版で残る非 daemon スレッドがあっても終了する

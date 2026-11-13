"""実コード試験の共通部品（Phase 5 の P5-01〜09 と統合テストで使う）。

app.py を import しない（Streamlit を起動しない）。必要な関数を AST で取り出し、
実際の SQL・実際の engine 保存関数で一時 SQLite に対して動かす。本番 DB には触れない。
"""
import json, os, shutil, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qver_lock8_review_test import load_engine, load_app_subset, _mk_df  # noqa: E402

SNAP = {
    "save_version": "Ver326", "seed": 20260719, "code_commit_sha": "test", "config_version": 1,
    "config_json": json.dumps({"save_version": "Ver326", "config_version": 1, "v330a_enabled": False,
                               "v331_lap1_tune_enabled": False, "v331_gap_mode": "C"}, sort_keys=True),
    "input_sha256": None, "v330a_enabled": False,
}

SEEDS = [
    "_v326_ensure_import_queue", "_v326_enqueue_tasks", "_v326_pickup_next", "_v326_reset_running_to_pending",
    "_v326_queue_heartbeat", "_v326_hb_timer_loop", "_v326_queue_connect", "_v326_queue_write_retry",
    "_V326_QUEUE_LOCK", "_v326_update_task_status_ex", "_v326_update_task_status", "_v326_owner_save_tx",
    "_v326_preensure_save_tables", "_v326_bulk_fix_unfixed_pending", "_v326_worker_config_snapshot",
    "_v326_set_worker_config_snapshot", "_v326_set_worker_tx_active_event", "_v326_get_worker_tx_active_event",
    "_V326OwnershipLost", "_V326SafeStop", "_V326HeartbeatJoinTimeout",
    "_V326_HB_JOIN_TIMEOUT_SEC", "_V326_HB_CONNECT_TIMEOUT_SEC", "_V326_HB_WRITE_RETRIES",
]

SAVE_TABLES = {
    "v34": "prediction_snapshots", "v40": "v40_prediction_feature_snapshots", "v67": "v67_prediction_tickets",
    "v231": "v231_prediction_history", "v222": "v222_prediction_restore", "v266": "v266_pred_time_snapshots",
}


class Env:
    def __init__(self, app_path, engine_path):
        self.eng = load_engine(engine_path)
        self.ns = load_app_subset(app_path, self.eng, SEEDS,
                                  stubs={"_v326_build_queue_config_snapshot": lambda: dict(SNAP)})
        self.tmp = tempfile.mkdtemp(prefix="qver_real_")
        self.db = os.path.join(self.tmp, "q.sqlite3")
        self.ns["_v326_ensure_import_queue"](self.db)
        self.ns["_v326_preensure_save_tables"](self.db)
        self.pd = self.ns["pd"]

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- queue helpers（実関数）
    def enqueue(self, races, job_type="official_import", mode="normal"):
        return self.ns["_v326_enqueue_tasks"](self.db, job_type, "20261113", races, task_mode=mode)

    def pickup(self, token, job_type="official_import"):
        return self.ns["_v326_pickup_next"](self.db, job_type, owner_token=token)

    def row(self, task_id):
        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM v326_import_queue WHERE task_id=?", (int(task_id),)).fetchone()
            return dict(r) if r else None

    def set_updated_at(self, task_id, value):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE v326_import_queue SET updated_at=? WHERE task_id=?", (value, int(task_id)))

    def counts(self, race_key):
        out = {}
        with sqlite3.connect(self.db) as c:
            for k, t in SAVE_TABLES.items():
                out[k] = c.execute(f"SELECT COUNT(*) FROM {t} WHERE race_key=?", (race_key,)).fetchone()[0]
        return out

    def trigger(self, sql):
        with sqlite3.connect(self.db) as c:
            c.execute(sql)

    # ---- 1 レース保存 TX（実関数）
    def save_args(self, race_no):
        meta = {"開催日": "2026-11-13", "開催場": "川口", "レース": int(race_no)}
        key = self.eng.v34_race_key(meta)
        df = _mk_df(self.pd)
        bets = {"2車単": {(1, 2): 3}, "三連単": {(1, 2, 3): 2, (2, 1, 3): 1}}
        view = {"df": df, "meta": meta, "app_version": "Ver326", "model_state_snapshot_v305": {"schema": "t"},
                "bets": bets, "race_key": key, "trials": 20, "seed": 1, "prediction_time": "2026-11-13T10:00:00+09:00"}
        return meta, key, df, bets, view

    def owner_save(self, task_id, token, race_no, *, abort=None, tx_evt=None, con_wrapper=None):
        meta, key, df, bets, view = self.save_args(race_no)
        ns = self.ns
        orig_connect = ns["_v326_queue_connect"]
        if con_wrapper is not None:
            ns["_v326_queue_connect"] = lambda p, t=30.0: con_wrapper(orig_connect(p, t))
        ns["_v326_set_worker_tx_active_event"](tx_evt)
        try:
            return ns["_v326_owner_save_tx"](
                self.db, task_id=int(task_id), owner_token=token, race_key=key, key=key, meta=meta, df=df,
                finish_prob=None, bets=bets, entries=None, text="raw", venue="川口", view=view, trials=20, seed=1,
                check_abort=(abort or (lambda: None)), is_slim_recoverable=lambda e: False,
            ), key
        finally:
            ns["_v326_queue_connect"] = orig_connect
            ns["_v326_set_worker_tx_active_event"](None)


def iso_ago(minutes):
    return (datetime.now(timezone(timedelta(hours=9))) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class ConProxy:
    """sqlite3.Connection の execute を差し替えるための薄い代理。"""

    def __init__(self, con, fail_on):
        self._con, self._fail_on = con, fail_on
        self.log = []

    def execute(self, sql, *a, **k):
        s = " ".join(str(sql).split())
        self.log.append(s[:60])
        for pat in self._fail_on:
            if s.upper().startswith(pat):
                raise sqlite3.OperationalError(f"injected failure on {pat}")
        return self._con.execute(sql, *a, **k)

    def __getattr__(self, n):
        return getattr(self._con, n)

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return self._con.__exit__(*e)

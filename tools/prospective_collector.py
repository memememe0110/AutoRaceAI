#!/usr/bin/env python3
"""AutoRaceAI prospective collector (pilot, design v0.3).

Method L: before the vote deadline, build the pre-race card, run the pinned
app.py in compare mode with full fixed trials, and append the output to a
collection-only SQLite. Results are fetched after the race into a separate
table. The production DB, app.py and engine.py are never modified.

Subcommands:
  fetch-db   download the autorace-db chunked DB pinned to a commit SHA
  init-store create the collection-only SQLite (idempotent)
  run        poll and record one meeting day
  selftest   local test on a past race (input build -> compare -> insert -> checks -> repro)
"""
from __future__ import annotations

import argparse, base64, datetime as dt, gzip, hashlib, json, os, pickle, platform, re
import shutil, sqlite3, subprocess, sys, time, traceback, urllib.parse, urllib.request, zlib
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
VENUES = ["川口", "伊勢崎", "浜松", "飯塚", "山陽"]
SEED_DEFAULT = 20260719
MARGIN_SEC = 180
POLL_LEAD_SEC = 40 * 60
POLL_INTERVAL_SEC = 60
NO_TRIAL_CUTOFF_SEC = 5 * 60
INITIAL_ESTIMATE_SEC = 360
RESULT_DELAY_SEC = 20 * 60
RESULT_RETRY_SEC = 10 * 60
RESULT_MAX_TRIES = 4  # first try + 3 retries

# ⑤ config_id (v332 evaluation id) -> compare-mode settings understood by app.py
CONFIGS = {
    "ver326_base": {"compare_mode": "Ver326", "env": {"AUTORACE_331_LAP1_TUNE": "0"}},
    "ver326_base_dryrun": {"compare_mode": "Ver326", "env": {"AUTORACE_331_LAP1_TUNE": "0"}},
    # Ver326.1 = app 65c62b9 (weather / track temperature kept in the card, NaN temperature -> 30C)
    "ver326_1_base": {"compare_mode": "Ver326", "env": {"AUTORACE_331_LAP1_TUNE": "0"}, "reattach_meta": True},
    "ver326_1_base_dryrun": {"compare_mode": "Ver326", "env": {"AUTORACE_331_LAP1_TUNE": "0"}, "reattach_meta": True},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS v332_prospective_alpha_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  race_key TEXT NOT NULL, config_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('actions','app')),
  telvote_time TEXT, race_start_time TEXT, telvote_time_raw TEXT, race_start_time_raw TEXT,
  change_start_flg INTEGER,
  input_fetched_at TEXT NOT NULL, sim_started_at TEXT NOT NULL, sim_finished_at TEXT NOT NULL,
  inserted_at TEXT NOT NULL,
  requested_trials INTEGER NOT NULL, planned_trials INTEGER, sim_trials INTEGER NOT NULL,
  validation_full_trials INTEGER NOT NULL, seed INTEGER NOT NULL,
  raw_text TEXT NOT NULL, raw_text_sha256 TEXT NOT NULL, api_bodies_json TEXT NOT NULL,
  car_set_json TEXT NOT NULL, st_default_flags_json TEXT, trial_missing_flags_json TEXT,
  divergence_flags_json TEXT,
  input_builder TEXT NOT NULL, input_builder_manifest_json TEXT NOT NULL,
  fingerprint TEXT, lap_order_counts_json TEXT NOT NULL,
  lap0_counts_json TEXT, per_trial_pairs_blob BLOB,
  code_commit TEXT NOT NULL, db_source TEXT NOT NULL, db_sha256 TEXT NOT NULL, db_fetched_at TEXT NOT NULL,
  target_result_absent INTEGER NOT NULL,
  workdb_integrity_before TEXT, workdb_integrity_after TEXT,
  runner_info_json TEXT, elapsed_json TEXT,
  status TEXT NOT NULL, error_text TEXT,
  UNIQUE (race_key, config_id, source)
);
CREATE TABLE IF NOT EXISTS v332_commit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alpha_log_id INTEGER NOT NULL, committed_at TEXT NOT NULL,
  UNIQUE (alpha_log_id)
);
CREATE TABLE IF NOT EXISTS v332_prospective_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  race_key TEXT NOT NULL, fetched_at TEXT NOT NULL, result_body TEXT NOT NULL,
  lap1_order_json TEXT, finish_order_json TEXT, car_set_json TEXT,
  change_start_flg INTEGER, telvote_time_at_result TEXT, race_start_time_at_result TEXT,
  UNIQUE (race_key, fetched_at)
);
CREATE TABLE IF NOT EXISTS v332_poll_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  race_key TEXT, polled_at TEXT NOT NULL, endpoint TEXT, http_status TEXT,
  trial_ready INTEGER, missing_cars_json TEXT,
  status TEXT,  -- not_ready / ready / skipped_late / skipped_no_trial / sim_trials_mismatch / error / trials_change / git_push / info
  detail_json TEXT
);
CREATE TABLE IF NOT EXISTS v332_repro_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alpha_log_id INTEGER NOT NULL, rerun_at TEXT NOT NULL, requested_trials INTEGER NOT NULL,
  identical INTEGER NOT NULL, detail_json TEXT
);
"""


# ---------------------------------------------------------------- utilities
def now() -> dt.datetime:
    return dt.datetime.now(JST)


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(JST).isoformat(timespec="seconds") if t else None


def hhmm_to_dt(date_s: str, hhmm: str | None) -> dt.datetime | None:
    """'HH:MM' on the meeting date (JST). '24:30' -> next day 00:30."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(hhmm or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    base = dt.datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=JST)
    return base + dt.timedelta(hours=h, minutes=mi)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def pack(obj) -> str:
    return base64.b64encode(gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))).decode()


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True)


def log(msg: str) -> None:
    print(f"[{iso(now())}] {msg}", flush=True)


# ---------------------------------------------------------------- DB fetch
def gh_get(url: str, token: str, accept: str = "application/vnd.github+json") -> bytes:
    hdr = {"Accept": accept, "User-Agent": "autorace-prospective-collector"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=hdr)
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception:
            if i == 3:
                raise
            time.sleep(3 * (i + 1))
    raise RuntimeError("unreachable")


def cmd_fetch_db(a) -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo, branch = a.repo, a.branch
    head = json.loads(gh_get(f"https://api.github.com/repos/{repo}/commits/{branch}", token))
    sha = head["sha"]
    # raw.githubusercontent.com pinned to the commit SHA (no REST rate limit for 115 parts)
    raw = lambda path: gh_get(f"https://raw.githubusercontent.com/{repo}/{sha}/{urllib.parse.quote(path)}", token,
                              accept="*/*")
    mf = json.loads(raw("autorace_players.sqlite3.chunks.json"))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with open(out, "wb") as f:
        for i, part in enumerate(mf["parts"]):
            data = raw(part)
            if len(data) != int(mf["part_sizes"][i]) or hashlib.sha256(data).hexdigest() != mf["part_sha256"][i]:
                raise SystemExit(f"part {i} verification failed: {part}")
            f.write(data)
            h.update(data)
    if h.hexdigest() != mf["sha256"] or out.stat().st_size != int(mf["size"]):
        raise SystemExit("whole-DB sha256/size mismatch")
    with sqlite3.connect(f"file:{out.resolve().as_posix()}?mode=ro", uri=True) as con:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
    if str(integ).lower() != "ok":
        raise SystemExit(f"integrity_check failed: {integ}")
    info = {"db_source": f"{repo}@{branch}@{sha}", "commit_sha": sha, "commit_date": head["commit"]["committer"]["date"],
            "manifest_generation": mf.get("generation"), "manifest_saved_at": mf.get("saved_at"),
            "db_sha256": mf["sha256"], "db_size": int(mf["size"]), "db_fetched_at": iso(now()),
            "integrity_check": integ}
    Path(a.info_out).write_text(jdump(info), encoding="utf-8")
    log(f"DB ready: {info}")


# ---------------------------------------------------------------- store
def open_store(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    con.commit()
    return con


def poll_log(con, race_key, status, endpoint=None, trial_ready=None, missing=None, detail=None, http_status=None):
    con.execute("INSERT INTO v332_poll_log (race_key, polled_at, endpoint, http_status, trial_ready, missing_cars_json, status, detail_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (race_key, iso(now()), endpoint, http_status, trial_ready,
                 jdump(missing) if missing is not None else None, status, jdump(detail) if detail is not None else None))
    con.commit()


# ---------------------------------------------------------------- card builder (decision 3: plan A)
class CardBuilder:
    def __init__(self, app_path: str, workdb_for_fill: str):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pc_extract  # noqa
        import types
        self.ns, self.manifest = pc_extract.load(app_path)
        # _v319_build_prediction_text_from_sprace reads getattr(engine, "DB_PATH") for history fill.
        self.ns["engine"] = types.SimpleNamespace(DB_PATH=str(workdb_for_fill))
        self._orig_post = self.ns["_v319_autorace_api_post"]
        self.bodies: list = []
        self.forbid_result = True
        self.reattach_meta = False

        def recording_post(path, ymd, venue, race_no=1, timeout=20, cache=True):
            if self.forbid_result and "RaceResult" in str(path):
                raise RuntimeError("RaceResult must not be called while building a pre-race card")
            body = self._orig_post(path, ymd, venue, race_no, timeout=timeout, cache=cache)
            self.bodies.append({"path": path, "race_no": int(race_no), "at": iso(now()), "body": body})
            return body
        self.ns["_v319_autorace_api_post"] = recording_post

    def set_fill_db(self, p: str):
        self.ns["engine"].DB_PATH = str(p)

    def api(self, path, ymd, venue, race_no, record=True):
        self.forbid_result = False
        try:
            return self._orig_post(path, ymd, venue, race_no, cache=False)
        finally:
            self.forbid_result = True

    def build(self, ymd: str, venue: str, race_no: int) -> dict:
        """Replicates the pre-race part of _v319_import_one_race without DB writes or RaceResult."""
        ns = self.ns
        ns["_V319_AR_PROGRAM_CACHE"].clear()
        try:
            ns["_V319_LAST_CARD_FILL"].clear()
        except Exception:
            pass
        self.bodies = []
        out = {"ready": False, "missing_cars": [], "error": None, "path": []}
        try:
            sp_html = ns["_v319_fetch_sprace_html"](ymd, venue, race_no)
        except ValueError as e:
            if "試走T" not in str(e):
                raise
            # trial times not yet published: list the missing cars from the Program body
            prog = ns["_v319_autorace_program_body"](ymd, venue, race_no, use_cache=False)
            pl = ns["_v319_program_players_from_body"](prog, venue) if isinstance(prog, dict) else []
            out["missing_cars"] = sorted(int(p.get("car") or 0) for p in (pl or [])
                                         if not p.get("kessha") and not re.match(r"^\d+\.\d+$", str(p.get("trial") or "").strip()))
            out["error"] = "trial_missing"
            return out
        out["sp_html"] = sp_html
        if not str(sp_html or "").strip():
            out["error"] = "card_not_published"
            return out
        players = ns["_v319_parse_sprace_car_rows"](sp_html) or ns["_v319_parse_sprace_players"](sp_html)
        out["players"] = players
        missing = [int(p.get("car") or 0) for p in (players or [])
                   if isinstance(p, dict) and not p.get("kessha") and not re.match(r"^\d+\.\d+$", str(p.get("trial") or "").strip())]
        out["missing_cars"] = missing
        if missing:
            out["error"] = "trial_missing"
            return out
        ns["_v319_validate_card_players"](players, venue, race_no)
        card = ns["_v319_build_prediction_text_from_sprace"](sp_html, venue, race_no, ymd)
        out["path"].append("sprace")
        kessha = sorted(set(ns["_v319_detect_kessha_from_players"](players) + ns["_v319_detect_kessha_from_card_text"](card)))
        ml_keep: list = []
        try:
            ori = ns["_v319_autorace_other_race_info"](ymd, venue, race_no)
            if isinstance(ori, dict):
                ml = ns["_v319_other_info_to_meta_lines"](ori)
                ml_keep = list(ml or [])
                if ml:
                    body = str(card or "").strip()
                    card = "\n".join(ml) + ("\n" + body if body else "")
                    out["path"].append("meta_lines")
        except Exception as e:
            out["path"].append(f"meta_lines_error:{type(e).__name__}")
        st_default = []
        try:
            prog = ns["_v319_autorace_program_body"](ymd, venue, race_no)
            if isinstance(prog, dict):
                # decision 4: latest10 history is NOT saved to the working DB.
                pl2 = ns["_v319_program_players_from_body"](prog, venue)
                # production would fill empty ST from RaceResult here; pre-race we only flag it.
                st_default = [int(x.get("car") or 0) for x in (pl2 or []) if not str(x.get("st") or "").strip()]
                if pl2:
                    card = ns["_v319_build_prediction_text_from_sprace"](
                        ns["_v319_synthesize_sprace_html_from_program"](prog, venue, race_no, ymd), venue, race_no, ymd)
                    out["path"].append("program_rebuild")
                    # Ver326.1 (app 65c62b9): re-attach the OtherRaceInfo lines the rebuild drops.
                    # Only for configs that pin a fixed app; ver326_base keeps the 4d19bcd8 behaviour.
                    if self.reattach_meta and ml_keep:
                        body = str(card or "").strip()
                        card = "\n".join(ml_keep) + ("\n" + body if body else "")
                        out["path"].append("meta_lines_reattached")
        except Exception as e:
            out["path"].append(f"program_error:{type(e).__name__}:{e}")
        out.update({"ready": True, "card_text": card, "kessha": kessha, "st_default": st_default,
                    "fill_note": ns["_v319_card_fill_note"]() if "_v319_card_fill_note" in ns else "",
                    "car_set": sorted(int(p.get("car") or 0) for p in players if isinstance(p, dict) and not p.get("kessha"))})
        return out


# ---------------------------------------------------------------- compare-mode runner
def run_compare(app_path: Path, workdb: Path, race_key: str, cfg: dict, trials: int, seed: int,
                out_path: Path, timeout: float, port: int = 8501) -> tuple[dict, dict]:
    from playwright.sync_api import sync_playwright
    env = os.environ.copy()
    env.update({
        "AUTORACE_VER330A_COMPARE": "1", "AUTORACE_COMPARE_MODE": cfg["compare_mode"],
        "AUTORACE_COMPARE_DB": str(workdb), "AUTORACE_COMPARE_RACE_KEY": race_key,
        "AUTORACE_COMPARE_TRIALS": str(int(trials)), "AUTORACE_COMPARE_SEED": str(int(seed)),
        "AUTORACE_COMPARE_OUT": str(out_path), "AUTORACE_330_VALIDATION_MODE": "1",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false", "PYTHONHASHSEED": "0",
    })
    env.update(cfg.get("env") or {})
    streamlit = shutil.which("streamlit")
    cmd = ([streamlit] if streamlit else [sys.executable, "-m", "streamlit"]) + [
        "run", str(app_path), "--server.headless=true", "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false", f"--server.port={port}"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    log_path = out_path.with_suffix(".log")
    el = {}
    t0 = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as lf:
        proc = subprocess.Popen(cmd, cwd=str(app_path.parent), env=env, stdout=lf, stderr=subprocess.STDOUT, text=True)
        try:
            booted = False
            while time.monotonic() - t0 < 120:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2).read()
                    booted = True
                    break
                except Exception:
                    if proc.poll() is not None:
                        raise RuntimeError("streamlit exited during boot")
                    time.sleep(0.5)
            if not booted:
                raise RuntimeError("streamlit boot timeout")
            el["boot_s"] = round(time.monotonic() - t0, 2)
            t1 = time.monotonic()
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    pg = b.new_page()
                    pg.goto(f"http://127.0.0.1:{port}/", timeout=60000, wait_until="domcontentloaded")
                    while time.monotonic() - t0 < timeout:
                        if out_path.exists() or proc.poll() is not None:
                            break
                        time.sleep(0.5)
                finally:
                    b.close()
            el["predict_and_sim_s"] = round(time.monotonic() - t1, 2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    if not out_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2500:]
        raise TimeoutError(f"no compare output after {time.monotonic()-t0:.1f}s; {tail}")
    data = json.loads(out_path.read_text(encoding="utf-8"))
    el["compare_total_s"] = round(time.monotonic() - t0, 2)
    return data, el


def check_output(data: dict, n: int) -> tuple[bool, dict]:
    """C: the three pre-INSERT conditions."""
    lap1 = dict((data.get("lap_order_counts") or {}).get("1") or {})
    s = int(sum(int(v) for v in lap1.values()))
    st = int(data.get("sim_trials") or 0)
    full = bool(data.get("validation_full_trials"))
    d = {"ok": bool(data.get("ok")), "sum_lap1": s, "sim_trials": st, "requested": n, "validation_full_trials": full,
         "error": data.get("error")}
    return bool(data.get("ok")) and s == st and st == n and full, d


def drop_db(p: Path) -> None:
    for suf in ("", "-wal", "-shm", "-journal"):
        Path(str(p) + suf).unlink(missing_ok=True)


def prepare_workdb(master: Path, work: Path, race_key: str, venue: str, raw_text: str) -> dict:
    drop_db(work)
    shutil.copyfile(master, work)
    with sqlite3.connect(str(work), timeout=30) as con:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        absent = 1
        for t in ("result_races", "result_laps", "result_entries"):
            if t in tabs and con.execute(f"SELECT 1 FROM {t} WHERE race_key=? LIMIT 1", (race_key,)).fetchone():
                absent = 0
        ts = iso(now())
        con.execute("INSERT OR REPLACE INTO v222_prediction_restore (race_key, race_label, raw_text, venue_override, payload, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (race_key, race_key, raw_text, venue, zlib.compress(pickle.dumps({"source": "prospective_collector"})), ts, ts))
        con.commit()
    return {"integrity_before": integ, "target_result_absent": absent}


def integrity(p: Path) -> str:
    with sqlite3.connect(str(p), timeout=30) as con:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])


# ---------------------------------------------------------------- git push (results branch)
def git_root(p: Path) -> Path | None:
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return d
    return None


def git_push(store_dir: Path, store_file: str, msg: str, con) -> bool:
    root = git_root(store_dir)
    if root is None:
        poll_log(con, None, "git_push", detail={"ok": False, "msg": msg, "errors": ["no git repository above store"]})
        return False
    store_file = str((store_dir / store_file).resolve())
    store_dir = root
    ok = False
    detail = []
    for i in range(4):
        try:
            subprocess.run(["git", "-C", str(store_dir), "add", store_file], check=True, capture_output=True)
            r = subprocess.run(["git", "-C", str(store_dir), "commit", "-m", msg], capture_output=True, text=True)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                raise RuntimeError(r.stderr[-400:])
            r = subprocess.run(["git", "-C", str(store_dir), "pull", "--rebase", "-q"], capture_output=True, text=True)
            if r.returncode != 0:
                subprocess.run(["git", "-C", str(store_dir), "rebase", "--abort"], capture_output=True)
                raise RuntimeError("pull --rebase conflict: " + r.stderr[-400:])
            r = subprocess.run(["git", "-C", str(store_dir), "push", "-q"], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-400:])
            ok = True
            break
        except Exception as e:
            detail.append(f"try{i+1}: {e}")
            if "conflict" in str(e):
                break
            time.sleep(5 * (i + 1))
    poll_log(con, None, "git_push", detail={"ok": ok, "msg": msg, "errors": detail})
    return ok


# ---------------------------------------------------------------- one race
def runner_info(a) -> dict:
    import numpy, pandas
    me = Path(__file__).resolve()
    try:
        cc = subprocess.run(["git", "-C", str(me.parent), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        cc = ""
    return {"actions_run_id": os.environ.get("GITHUB_RUN_ID"), "actions_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "collector_commit": os.environ.get("COLLECTOR_COMMIT") or cc,
            "collector_sha256": sha256_file(me), "extract_sha256": sha256_file(me.parent / "pc_extract.py"),
            "python": platform.python_version(), "numpy": numpy.__version__, "pandas": pandas.__version__,
            "platform": platform.platform()}


def record_race(a, con, builder: CardBuilder, dbinfo: dict, race: dict, card: dict, trials: int,
                input_fetched_at: dt.datetime, tmp: Path) -> dict:
    cfg = CONFIGS[a.config_id]
    rk = race["race_key"]
    raw_text = card["card_text"]
    el = {}
    t0 = time.monotonic()
    work = tmp / "work.sqlite3"
    prep = prepare_workdb(Path(a.db), work, rk, race["venue"], raw_text)
    el["prepare_s"] = round(time.monotonic() - t0, 2)
    sim_started = now()
    err = None
    data = {}
    try:
        data, el2 = run_compare(Path(a.app), work, rk, cfg, trials, a.seed, tmp / "out.json", a.timeout)
        el.update(el2)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    sim_finished = now()
    integ_after = integrity(work) if work.exists() else None
    drop_db(work)
    ok3, chk = check_output(data, trials) if not err else (False, {"error": err})
    divergence = {"kessha_cars": card.get("kessha") or [], "retrial_text": bool(re.search(r"再試走|再試", raw_text)),
                  "st_filled_from_history": card.get("st_default") or [], "fill_note": card.get("fill_note") or "",
                  "build_path": card.get("path")}
    if not ok3:
        poll_log(con, rk, "sim_trials_mismatch" if not err and data.get("ok") else "error", detail={**chk, "elapsed": el})
        return {"recorded": False, "elapsed": el, "check": chk}
    late = not (race["telvote_dt"] and sim_finished < race["telvote_dt"])
    inserted_at = now()
    el["total_s"] = round(time.monotonic() - t0, 2)
    cur = con.execute(
        "INSERT OR IGNORE INTO v332_prospective_alpha_log (race_key, config_id, source, telvote_time, race_start_time,"
        " telvote_time_raw, race_start_time_raw, change_start_flg, input_fetched_at, sim_started_at, sim_finished_at, inserted_at,"
        " requested_trials, planned_trials, sim_trials, validation_full_trials, seed, raw_text, raw_text_sha256, api_bodies_json,"
        " car_set_json, st_default_flags_json, trial_missing_flags_json, divergence_flags_json, input_builder, input_builder_manifest_json,"
        " fingerprint, lap_order_counts_json, lap0_counts_json, per_trial_pairs_blob, code_commit, db_source, db_sha256, db_fetched_at,"
        " target_result_absent, workdb_integrity_before, workdb_integrity_after, runner_info_json, elapsed_json, status, error_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rk, a.config_id, "actions", iso(race["telvote_dt"]), iso(race["start_dt"]), race["telvote_raw"], race["start_raw"],
         race["change_flg"], iso(input_fetched_at), iso(sim_started), iso(sim_finished), iso(inserted_at),
         trials, None, int(data.get("sim_trials") or 0), int(bool(data.get("validation_full_trials"))), a.seed,
         raw_text, hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
         pack({"bodies": builder.bodies, "sp_html": card.get("sp_html")}),
         jdump(card.get("car_set")), jdump(card.get("st_default") or []), jdump([]), jdump(divergence),
         "extract_v1", jdump(builder.manifest), jdump(data.get("fingerprint")), jdump(data.get("lap_order_counts")),
         None, None, a.code_commit, dbinfo["db_source"], dbinfo["db_sha256"], dbinfo["db_fetched_at"],
         prep["target_result_absent"], prep["integrity_before"], integ_after, jdump(a._runner_info), jdump(el),
         "late" if late else "ok", None))
    if cur.rowcount == 0:
        con.commit()
        poll_log(con, rk, "info", detail={"duplicate_key_not_inserted": [rk, a.config_id, "actions"]})
        return {"recorded": False, "elapsed": el, "check": chk, "duplicate": True}
    aid = cur.lastrowid
    con.commit()
    committed_at = now()
    con.execute("INSERT INTO v332_commit_log (alpha_log_id, committed_at) VALUES (?,?)", (aid, iso(committed_at)))
    con.commit()
    complete = (not late) and race["telvote_dt"] and committed_at < race["telvote_dt"]
    log(f"recorded {rk} N={trials} status={'late' if late else 'ok'} complete={bool(complete)} el={el}")
    return {"recorded": True, "id": aid, "elapsed": el, "late": late, "complete": bool(complete), "check": chk}


# ---------------------------------------------------------------- schedule
def discover(builder: CardBuilder, date_s: str, venues: list[str]) -> list[dict]:
    ymd = date_s.replace("-", "")
    races = []
    for v in venues:
        miss = 0
        for r in range(1, 13):
            try:
                ori = builder.api("/race_info/OtherRaceInfo", ymd, v, r)
            except Exception:
                ori = None
            body = (ori or {}).get("body") if isinstance(ori, dict) and "body" in ori else ori
            if not isinstance(body, dict) or not body.get("raceStartTime"):
                miss += 1
                if r == 1 or miss >= 2:
                    break
                continue
            races.append(race_from_ori(date_s, v, r, body))
    return sorted(races, key=lambda x: x["telvote_dt"] or x["start_dt"])


def race_from_ori(date_s, v, r, body) -> dict:
    tv_raw, st_raw = body.get("telvoteTime"), body.get("raceStartTime")
    tv, sdt = hhmm_to_dt(date_s, tv_raw), hhmm_to_dt(date_s, st_raw)
    return {"race_key": f"{date_s.replace('-', '')}_{v}_{r}R", "venue": v, "race_no": r, "date": date_s,
            "telvote_raw": tv_raw, "start_raw": st_raw, "telvote_dt": tv or sdt, "start_dt": sdt,
            "change_flg": int(body.get("changeStartFlg") or 0) if str(body.get("changeStartFlg") or "0").isdigit() else None,
            "state": "pending", "first_ready_at": None, "result_tries": 0, "next_result_at": None, "recorded_id": None}


def ori_body(builder, race):
    ori = builder.api("/race_info/OtherRaceInfo", race["date"].replace("-", ""), race["venue"], race["race_no"])
    return (ori or {}).get("body") if isinstance(ori, dict) and "body" in ori else ori


def fetch_result(a, con, builder, race) -> bool:
    ymd = race["date"].replace("-", "")
    rb = builder.api("/race_info/RaceResult", ymd, race["venue"], race["race_no"])
    body = None
    if isinstance(rb, dict):
        body = rb.get("body") if isinstance(rb.get("body"), dict) else rb  # builder.api already unwraps body
    lap1 = fin = cars = None
    if isinstance(body, dict):
        for g in body.get("grandNoteList") or []:
            if int(g.get("lapNo") or 0) == 1:
                lap1 = [int(x["carNo"]) for x in sorted(g.get("rankList") or [], key=lambda x: int(x.get("rank") or 99))]
        rr = body.get("raceResult") or []
        if rr:
            fin = [{"order": x.get("order"), "car": x.get("carNo"), "accident": x.get("accidentName")} for x in rr]
            cars = sorted(int(x.get("carNo")) for x in rr if x.get("carNo") is not None)
    try:
        ob = ori_body(builder, race) or {}
    except Exception:
        ob = {}
    con.execute("INSERT OR IGNORE INTO v332_prospective_results (race_key, fetched_at, result_body, lap1_order_json, finish_order_json,"
                " car_set_json, change_start_flg, telvote_time_at_result, race_start_time_at_result) VALUES (?,?,?,?,?,?,?,?,?)",
                (race["race_key"], iso(now()), jdump(rb), jdump(lap1) if lap1 else None, jdump(fin) if fin else None,
                 jdump(cars) if cars else None, ob.get("changeStartFlg"), ob.get("telvoteTime"), ob.get("raceStartTime")))
    con.commit()
    return bool(lap1)


# ---------------------------------------------------------------- run (one meeting day)
def cmd_run(a) -> None:
    a._runner_info = runner_info(a)
    dbinfo = json.loads(Path(a.db_info).read_text(encoding="utf-8"))
    con = open_store(a.store)
    store_dir = Path(a.store).resolve().parent
    tmp = Path(a.tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    builder = CardBuilder(a.app, a.db)  # history fill reads the master copy read-only path
    builder.reattach_meta = bool(CONFIGS[a.config_id].get("reattach_meta"))
    ladder = [int(x) for x in a.trials_ladder.split(",")]
    li = 0
    venues = [v for v in (a.venues.split(",") if a.venues else VENUES) if v]
    races = discover(builder, a.date, venues)
    poll_log(con, None, "info", detail={"job_start": iso(now()), "races": [r["race_key"] for r in races], "ladder": ladder,
                                        "config_id": a.config_id, "runner": a._runner_info, "db": dbinfo})
    # resume: races already recorded by an earlier job of the same day/config
    for r in races:
        row = con.execute("SELECT id FROM v332_prospective_alpha_log WHERE race_key=? AND config_id=? AND source='actions'",
                          (r["race_key"], a.config_id)).fetchone()
        if row:
            r["state"], r["recorded_id"] = "recorded", row[0]
            if con.execute("SELECT 1 FROM v332_prospective_results WHERE race_key=? AND lap1_order_json IS NOT NULL",
                           (r["race_key"],)).fetchone():
                r["result_tries"] = RESULT_MAX_TRIES
            else:
                r["result_tries"] = con.execute("SELECT COUNT(*) FROM v332_prospective_results WHERE race_key=?",
                                                (r["race_key"],)).fetchone()[0]
        elif r["telvote_dt"] and r["telvote_dt"] < now():
            r["state"] = "passed"
    # resume the trials ladder position from an earlier job of the same day
    for row in con.execute("SELECT detail_json FROM v332_poll_log WHERE status='trials_change' AND polled_at LIKE ?",
                           (a.date + "%",)):
        to = json.loads(row[0]).get("to")
        if to in ladder:
            li = max(li, ladder.index(to))
    log(f"{len(races)} races: {[ (r['race_key'], r['telvote_raw'], r['state']) for r in races ]} N={ladder[li]}")
    stop_at = now() + dt.timedelta(minutes=a.max_minutes)
    elapsed_hist: list[float] = []
    lead_hist: list[float] = []
    pushes_ok = pushes = 0
    while now() < stop_at:
        t = now()
        pending = [r for r in races if r["state"] in ("pending", "polling")]
        awaiting_res = [r for r in races if r["state"] == "recorded" and r["result_tries"] < RESULT_MAX_TRIES]
        if not pending and not awaiting_res:
            break
        did = False
        # results first (cheap)
        for r in awaiting_res:
            due = r["next_result_at"] or (r["start_dt"] + dt.timedelta(seconds=RESULT_DELAY_SEC))
            if t >= due:
                r["result_tries"] += 1
                try:
                    got = fetch_result(a, con, builder, r)
                except Exception as e:
                    got = False
                    poll_log(con, r["race_key"], "error", endpoint="RaceResult", detail={"error": str(e)})
                r["next_result_at"] = now() + dt.timedelta(seconds=RESULT_RETRY_SEC)
                if got:
                    r["result_tries"] = RESULT_MAX_TRIES
                pushes += 1
                pushes_ok += git_push(store_dir, Path(a.store).name, f"result {r['race_key']}", con)
                did = True
        for r in pending:
            tv = r["telvote_dt"]
            if not tv:
                r["state"] = "skipped"
                continue
            if t < tv - dt.timedelta(seconds=POLL_LEAD_SEC):
                continue
            if r.get("last_poll") and (t - r["last_poll"]).total_seconds() < POLL_INTERVAL_SEC:
                continue
            r["last_poll"] = t
            r["state"] = "polling"
            did = True
            est = (sorted(elapsed_hist)[-3:][-1] if elapsed_hist else INITIAL_ESTIMATE_SEC)
            remaining = (tv - now()).total_seconds()
            try:
                ob = ori_body(builder, r) or {}
                ntv = hhmm_to_dt(r["date"], ob.get("telvoteTime"))
                if ntv and ntv != tv:
                    poll_log(con, r["race_key"], "info", detail={"telvote_changed": [r["telvote_raw"], ob.get("telvoteTime")],
                                                                 "changeStartFlg": ob.get("changeStartFlg")})
                    r.update(race_from_ori(r["date"], r["venue"], r["race_no"], ob) | {"state": "polling", "last_poll": t})
                    tv = r["telvote_dt"]
                    remaining = (tv - now()).total_seconds()
                fetched_at = now()
                card = builder.build(r["date"].replace("-", ""), r["venue"], r["race_no"])
            except Exception as e:
                poll_log(con, r["race_key"], "error", endpoint="build", detail={"error": f"{type(e).__name__}: {e}",
                                                                                "tb": traceback.format_exc()[-1500:]})
                card = {"ready": False, "missing_cars": None, "error": str(e)}
            if not card.get("ready"):
                if remaining < NO_TRIAL_CUTOFF_SEC:
                    r["state"] = "skipped"
                    poll_log(con, r["race_key"], "skipped_no_trial", endpoint="Program", trial_ready=0,
                             missing=card.get("missing_cars"), detail={"error": card.get("error")})
                else:
                    poll_log(con, r["race_key"], "not_ready", endpoint="Program", trial_ready=0,
                             missing=card.get("missing_cars"), detail={"error": card.get("error")})
                continue
            if r["first_ready_at"] is None:
                r["first_ready_at"] = fetched_at
                lead_hist.append((tv - fetched_at).total_seconds())
            if remaining < est + MARGIN_SEC:
                r["state"] = "skipped"
                poll_log(con, r["race_key"], "skipped_late", trial_ready=1,
                         detail={"remaining_s": remaining, "estimate_s": est, "N": ladder[li]})
                continue
            poll_log(con, r["race_key"], "ready", trial_ready=1, missing=[], detail={"remaining_s": remaining, "N": ladder[li]})
            res = record_race(a, con, builder, dbinfo, r, card, ladder[li], fetched_at, tmp)
            if res.get("recorded"):
                r["state"] = "recorded"
                r["recorded_id"] = res["id"]
                elapsed_hist.append(float(res["elapsed"].get("total_s") or 0) + (now() - fetched_at).total_seconds() * 0)
                pushes += 1
                pushes_ok += git_push(store_dir, Path(a.store).name, f"record {r['race_key']} N={ladder[li]}", con)
            else:
                r["state"] = "failed"
            # D: step down the ladder when p95(elapsed)+margin does not fit the typical lead time
            if elapsed_hist and lead_hist and li + 1 < len(ladder) and ladder[li] == (res.get("check") or {}).get("requested", ladder[li]):
                xs = sorted(elapsed_hist)
                p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
                lead_med = sorted(lead_hist)[len(lead_hist) // 2]
                if p95 + MARGIN_SEC > lead_med:
                    poll_log(con, None, "trials_change", detail={"from": ladder[li], "to": ladder[li + 1], "p95_s": p95,
                                                                  "lead_median_s": lead_med})
                    li += 1
                    elapsed_hist = []
            break  # re-evaluate the queue after a (long) prediction
        if not did:
            time.sleep(5)
    # reproducibility check on the first record made at the final N (④)
    final_n = ladder[li]
    day_like = a.date.replace("-", "") + "_%"  # repro is per (date, config_id, final N)
    row = con.execute("SELECT id, race_key, raw_text, lap_order_counts_json FROM v332_prospective_alpha_log "
                      "WHERE config_id=? AND requested_trials=? AND race_key LIKE ? ORDER BY id LIMIT 1",
                      (a.config_id, final_n, day_like)).fetchone()
    done_repro = con.execute("SELECT 1 FROM v332_repro_log r JOIN v332_prospective_alpha_log l ON l.id = r.alpha_log_id "
                             "WHERE r.requested_trials=? AND r.identical IN (0,1) AND l.config_id=? AND l.race_key LIKE ?",
                             (final_n, a.config_id, day_like)).fetchone()
    if row and a.repro and not done_repro:
        repro(a, con, row, final_n, tmp)
    summary = {"job_end": iso(now()), "final_N": final_n, "pushes": pushes, "pushes_ok": pushes_ok,
               "states": {s: sum(1 for r in races if r["state"] == s) for s in {r["state"] for r in races}},
               "lead_hist_s": lead_hist}
    poll_log(con, None, "info", detail=summary)
    git_push(store_dir, Path(a.store).name, "job end summary", con)
    log(f"summary {summary}")


def repro(a, con, row, n, tmp: Path) -> bool:
    aid, rk, raw_text, counts_json = row
    venue = rk.split("_")[1]
    work = tmp / "repro.sqlite3"
    prepare_workdb(Path(a.db), work, rk, venue, raw_text)
    try:
        data, el = run_compare(Path(a.app), work, rk, CONFIGS[a.config_id], n, a.seed, tmp / "repro.json", a.timeout)
        same = jdump(data.get("lap_order_counts")) == counts_json
        det = {"elapsed": el, "fingerprint": data.get("fingerprint")}
    except Exception as e:
        same, det = False, {"error": str(e)}
    finally:
        drop_db(work)
    con.execute("INSERT INTO v332_repro_log (alpha_log_id, rerun_at, requested_trials, identical, detail_json) VALUES (?,?,?,?,?)",
                (aid, iso(now()), n, int(same), jdump(det)))
    con.commit()
    log(f"repro {rk} N={n} identical={same}")
    return same


# ---------------------------------------------------------------- selftest (past race, local)
def cmd_selftest(a) -> None:
    """Past race: build card -> compare (N) -> INSERT -> 3 checks -> rerun -> identical. Not an evaluation record."""
    a._runner_info = runner_info(a)
    dbinfo = json.loads(Path(a.db_info).read_text(encoding="utf-8")) if a.db_info else {
        "db_source": f"local:{a.db}", "db_sha256": sha256_file(Path(a.db)), "db_fetched_at": iso(now())}
    con = open_store(a.store)
    tmp = Path(a.tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    builder = CardBuilder(a.app, a.db)
    builder.reattach_meta = bool(CONFIGS[a.config_id].get("reattach_meta"))
    date_s, venue, rno = a.date, a.venue, a.race_no
    ob = builder.api("/race_info/OtherRaceInfo", date_s.replace("-", ""), venue, rno)
    ob = (ob or {}).get("body") if isinstance(ob, dict) and "body" in ob else ob
    race = race_from_ori(date_s, venue, rno, ob or {})
    # past race: shift the deadline to "now + 1h" only for the self-test status flag
    race["telvote_dt"] = now() + dt.timedelta(hours=1)
    t = now()
    card = builder.build(date_s.replace("-", ""), venue, rno)
    print(jdump({k: card.get(k) for k in ("ready", "error", "missing_cars", "kessha", "st_default", "path", "car_set")}))
    if not card.get("ready"):
        raise SystemExit("card not ready")
    Path(tmp / "card.txt").write_text(card["card_text"], encoding="utf-8")
    res = record_race(a, con, builder, dbinfo, race, card, a.trials, t, tmp)
    print(jdump(res))
    if not res.get("recorded"):
        raise SystemExit("selftest: not recorded")
    row = con.execute("SELECT id, race_key, raw_text, lap_order_counts_json FROM v332_prospective_alpha_log WHERE id=?",
                      (res["id"],)).fetchone()
    same = repro(a, con, row, a.trials, tmp)
    print(jdump({"selftest_ok": bool(res["recorded"]) and same, "repro_identical": same}))
    if not same:
        raise SystemExit(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch-db")
    f.add_argument("--repo", default="memememe0110/AutoRaceAI")
    f.add_argument("--branch", default="autorace-db")
    f.add_argument("--out", required=True)
    f.add_argument("--info-out", required=True)
    i = sp.add_parser("init-store")
    i.add_argument("--store", required=True)
    for name in ("run", "selftest"):
        r = sp.add_parser(name)
        r.add_argument("--app", required=True)
        r.add_argument("--db", required=True)
        r.add_argument("--db-info", default="")
        r.add_argument("--store", required=True)
        r.add_argument("--tmp", default="/tmp/pc_tmp")
        r.add_argument("--config-id", default="ver326_1_base_dryrun", choices=sorted(CONFIGS))
        r.add_argument("--seed", type=int, default=SEED_DEFAULT)
        r.add_argument("--timeout", type=float, default=1500)
        r.add_argument("--code-commit", required=True)
        r.add_argument("--date", required=True, help="YYYY-MM-DD (JST meeting date)")
        if name == "run":
            r.add_argument("--venues", default="")
            r.add_argument("--trials-ladder", default="20000,10000,5000")
            r.add_argument("--max-minutes", type=int, default=340)
            r.add_argument("--repro", type=int, default=1)
        else:
            r.add_argument("--venue", required=True)
            r.add_argument("--race-no", type=int, required=True)
            r.add_argument("--trials", type=int, default=1000)
    a = ap.parse_args()
    for k in ("app", "db", "db_info", "store", "tmp", "out", "info_out"):
        if getattr(a, k, None):
            setattr(a, k, str(Path(getattr(a, k)).resolve()))
    if a.cmd == "fetch-db":
        cmd_fetch_db(a)
    elif a.cmd == "init-store":
        open_store(a.store).close()
    elif a.cmd == "run":
        cmd_run(a)
    else:
        cmd_selftest(a)


if __name__ == "__main__":
    main()

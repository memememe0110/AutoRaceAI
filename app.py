from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import pickle
import re
import shutil
import sqlite3
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import streamlit as st
import streamlit.components.v1 as components

import engine

# ---------------------------------------------------------------------------
# AutoRaceAI runtime configuration / state
# Ver265 refactor: version, modes and mutable caches are initialized in one
# place so maintenance/reconstruction paths cannot fail from definition order.
# Prediction formulas are intentionally unchanged by this refactor.
# ---------------------------------------------------------------------------
APP_VERSION = "Ver267"
SIMULATION_MODE = "6周内蔵型壁展開"

# Backward-compatible aliases used throughout the existing code.
_V231_APP_VERSION = APP_VERSION
_V231_SIMULATION_MODE = SIMULATION_MODE

# Mutable runtime state.  Keep initialization centralized.

def _runtime_current_version() -> str:
    """Return the single source of truth for the current app version."""
    return APP_VERSION

def _runtime_clear_prediction_caches() -> None:
    """Clear model/calibration caches after DB-changing maintenance."""
    for cache in (
        _V248_WALL_CALIBRATION_CACHE,
        _V251_LAP_ALIGNMENT_CACHE,
        _V252_LAP_RESIDUAL_CACHE,
        _V256_ACTUAL_LAP_CACHE,
        _V254_PLAYER_LAP_CACHE,
        _V258_PLAYER_ACTUAL_CACHE,
        _V250_FLOW_CALIBRATION_CACHE,
        _V263_SCENARIO_PRIOR_CACHE,
        _V264_SCENARIO_FEEDBACK_CACHE,
    ):
        try:
            cache.clear()
        except Exception:
            pass
    try:
        _V256_SETTINGS_SYNC_CACHE.clear()
    except Exception:
        pass

def _runtime_exception_text(exc: BaseException) -> str:
    """Compact, consistent error text for Streamlit maintenance actions."""
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Ver266: 基礎予測 誤差解析モード
# 目的:
#   - 実結果があるレースについて、予測競走Tと実競走Tのズレを可視化
#   - ST / ハンデ / 試走 / 熱走路 / 開催場 / 選手別の偏りを候補原因として集計
#   - 予測ロジック自体はこの画面では変更しない
# ---------------------------------------------------------------------------

def _v266_num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None

def _v266_guess_col(cols, *names):
    low = {str(c).strip().lower(): c for c in cols}
    for n in names:
        k = str(n).strip().lower()
        if k in low:
            return low[k]
    return None

def _v266_table_columns(conn, table):
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    except Exception:
        return []

def _v266_find_table(conn, preferred):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in preferred:
        if t in tables:
            return t
    return None


def _v266_repair_missing_result_player_names(db_path: str) -> dict:
    """
    result_entries.player_name が NULL/None/空文字の行を、
    v238_result_raw_archive の公式結果テキストから race_key+car_no で補完する。
    欠車/欠責等でも選手名だけは補完し、競走T=0などの結果値は変更しない。
    """
    out = {"checked": 0, "repaired": 0, "unresolved": 0, "errors": []}
    if not db_path or not os.path.exists(str(db_path)):
        return out

    def _norm_name(s):
        s = str(s or "")
        s = re.sub(r"[　\s]+", " ", s).strip()
        return s

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT re.race_key, re.car_no, re.player_name, a.raw_result_text
              FROM result_entries re
              LEFT JOIN v238_result_raw_archive a ON a.race_key=re.race_key
             WHERE re.player_name IS NULL
                OR TRIM(CAST(re.player_name AS TEXT))=''
                OR LOWER(TRIM(CAST(re.player_name AS TEXT)))='none'
        """).fetchall()

        for row in rows:
            out["checked"] += 1
            try:
                raw = str(row["raw_result_text"] or "")
                car_no = int(row["car_no"])
                if not raw:
                    out["unresolved"] += 1
                    continue

                # Official result block:
                # <finish>\t<car_no>\n<player name>\n<LG/handicap/...>
                # finish can also be '-' for scratches.
                pat = re.compile(
                    rf"(?m)^(?:\d+|-)\s*\t\s*{car_no}\s*$\s*\n([^\n]+)",
                    re.MULTILINE,
                )
                m = pat.search(raw)
                if not m:
                    # More tolerant fallback around the car number.
                    pat2 = re.compile(
                        rf"(?m)^(?:\d+|-)\s+{car_no}\s*$\s*\n([^\n]+)",
                        re.MULTILINE,
                    )
                    m = pat2.search(raw)

                name = _norm_name(m.group(1)) if m else ""
                if not name or name.lower() == "none":
                    out["unresolved"] += 1
                    continue

                con.execute("""
                    UPDATE result_entries
                       SET player_name=?
                     WHERE race_key=? AND car_no=?
                       AND (
                            player_name IS NULL
                         OR TRIM(CAST(player_name AS TEXT))=''
                         OR LOWER(TRIM(CAST(player_name AS TEXT)))='none'
                       )
                """, (name, str(row["race_key"]), car_no))
                out["repaired"] += 1
            except Exception as exc:
                out["errors"].append(
                    f"{row['race_key']} {row['car_no']}号車: {type(exc).__name__}: {exc}"
                )
        con.commit()
    return out


def _v266_coalesce_player_name_columns(df):
    """Merge後の player_name を予測保存値→実結果値の順で補完する。"""
    if df is None or getattr(df, "empty", True):
        return df
    import pandas as pd
    candidates = [
        c for c in ("player_name", "player_name_pred", "player_name_x", "player_name_y")
        if c in df.columns
    ]
    if not candidates:
        return df

    def valid(v):
        if pd.isna(v):
            return False
        s = str(v).strip()
        return bool(s and s.lower() != "none")

    vals = []
    # Prefer the prediction snapshot's saved name when available, then actual result.
    preferred = [c for c in ("player_name_pred", "player_name_x", "player_name", "player_name_y") if c in df.columns]
    for _, row in df.iterrows():
        chosen = ""
        for c in preferred:
            v = row.get(c)
            if valid(v):
                chosen = str(v).strip()
                break
        vals.append(chosen if chosen else None)
    df["player_name"] = vals
    return df


def _v266_ensure_pred_time_snapshot_table(db_path: str) -> None:
    """Ver266: 車番別の予測競走Tを履歴ID単位で永続保存する。"""
    with sqlite3.connect(str(db_path)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS v266_pred_time_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id INTEGER NOT NULL,
                race_key TEXT,
                app_version TEXT NOT NULL,
                car_no INTEGER NOT NULL,
                player_name TEXT,
                predicted_race_time REAL NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'saved_view',
                created_at TEXT NOT NULL,
                UNIQUE(history_id, car_no)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_v266_pred_time_race
            ON v266_pred_time_snapshots(race_key, app_version, car_no)
        """)
        con.commit()


def _v266_extract_pred_time_rows_from_view(view: dict) -> list[dict]:
    """保存済みprediction_viewのdfから、その時点の予測競走Tを取り出す。再計算はしない。"""
    out = []
    if not isinstance(view, dict):
        return out
    df = view.get("df")
    if df is None:
        return out
    try:
        rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    except Exception:
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        car = row.get("車", row.get("車番", row.get("car_no")))
        name = row.get("選手名", row.get("player_name", ""))
        pred = None
        # その履歴に実際に保存されている列を優先。
        for key in (
            "Ver266補正後予測競走T",
            "Ver265補正後予測競走T",
            "予測競走T",
            "予測競走タイム",
            "pred_race_time",
            "predicted_race_time",
        ):
            if key not in row:
                continue
            try:
                val = float(row.get(key))
                if np.isfinite(val):
                    pred = val
                    break
            except Exception:
                pass
        if car is None or pred is None:
            continue
        try:
            car = int(float(car))
        except Exception:
            continue
        out.append({
            "car_no": car,
            "player_name": str(name or ""),
            "predicted_race_time": float(pred),
        })
    return out


def _v266_save_pred_time_snapshot(
    db_path: str,
    history_id: int,
    race_key: str,
    app_version: str,
    view: dict,
    source_kind: str = "saved_view",
) -> int:
    """prediction_viewに保存されている値を、そのまま車番別スナップショットへ保存する。"""
    if not history_id:
        return 0
    rows = _v266_extract_pred_time_rows_from_view(view)
    if not rows:
        return 0
    _v266_ensure_pred_time_snapshot_table(db_path)
    now = _v228_now_jst_iso()
    saved = 0
    with sqlite3.connect(str(db_path)) as con:
        for row in rows:
            con.execute("""
                INSERT INTO v266_pred_time_snapshots
                (history_id,race_key,app_version,car_no,player_name,predicted_race_time,source_kind,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(history_id,car_no) DO UPDATE SET
                    race_key=excluded.race_key,
                    app_version=excluded.app_version,
                    player_name=excluded.player_name,
                    predicted_race_time=excluded.predicted_race_time,
                    source_kind=excluded.source_kind,
                    created_at=excluded.created_at
            """, (
                int(history_id), str(race_key or ""), str(app_version or ""),
                int(row["car_no"]), str(row["player_name"]),
                float(row["predicted_race_time"]), str(source_kind), now,
            ))
            saved += 1
        con.commit()
    return saved


def _v266_backfill_pred_time_snapshots(db_path: str, limit: int = 500) -> dict:
    """
    過去のv231_prediction_history payloadを展開し、
    当時保存されたview['df']の予測競走Tをそのまま補完する。
    現在コードで再計算しないため、旧Verの値を偽装しない。
    """
    out = {"checked": 0, "histories_saved": 0, "cars_saved": 0, "already": 0, "no_time": 0, "errors": []}
    _v231_ensure_prediction_history_table(db_path)
    _v266_ensure_pred_time_snapshot_table(db_path)

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        histories = con.execute("""
            SELECT history_id,race_key,app_version,payload
            FROM v231_prediction_history
            ORDER BY history_id DESC
            LIMIT ?
        """, (int(limit),)).fetchall()

    for h in histories:
        out["checked"] += 1
        hid = int(h["history_id"])
        try:
            with sqlite3.connect(str(db_path)) as con:
                existing = int(con.execute(
                    "SELECT COUNT(*) FROM v266_pred_time_snapshots WHERE history_id=?",
                    (hid,)
                ).fetchone()[0] or 0)
            if existing > 0:
                out["already"] += 1
                continue

            view = pickle.loads(zlib.decompress(bytes(h["payload"])))
            if not isinstance(view, dict):
                out["no_time"] += 1
                continue

            n = _v266_save_pred_time_snapshot(
                db_path=db_path,
                history_id=hid,
                race_key=str(h["race_key"] or ""),
                app_version=str(h["app_version"] or view.get("app_version") or ""),
                view=view,
                source_kind="historical_saved_view",
            )
            if n > 0:
                out["histories_saved"] += 1
                out["cars_saved"] += int(n)
            else:
                out["no_time"] += 1
        except Exception as exc:
            out["errors"].append(f"履歴ID{hid}: {type(exc).__name__}: {exc}")
    return out



def _v266_load_error_rows(db_path, limit_rows=20000):
    """
    AutoRaceAIの実DB構造に合わせて、保存済み予測と実結果を結合する。
    実結果:
      result_races(race_key, race_date, venue, race_no, ...)
        JOIN result_entries(race_key, car_no, player_name, handicap,
                            trial_time, race_time, start_time, ...)
    予測:
      v231_prediction_history を優先し、JSON内の車番別予測競走Tを展開する。
    """
    import pandas as pd

    if not db_path or not os.path.exists(str(db_path)):
        return pd.DataFrame(), "DBファイルが見つかりません"

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        # ---------- 実結果 ----------
        if "result_races" not in tables or "result_entries" not in tables:
            return pd.DataFrame(), "result_races / result_entries が見つかりません"

        rr_cols = _v266_table_columns(con, "result_races")
        re_cols = _v266_table_columns(con, "result_entries")

        # AutoRaceAIの標準スキーマ
        required_rr = {"race_key", "race_date", "venue", "race_no"}
        required_re = {"race_key", "car_no", "race_time"}
        if not required_rr.issubset(set(rr_cols)) or not required_re.issubset(set(re_cols)):
            return pd.DataFrame(), (
                "実結果テーブルの必要カラム不足: "
                f"result_races={rr_cols} / result_entries={re_cols}"
            )

        select_parts = [
            "rr.race_key AS race_key",
            "rr.race_date AS date",
            "rr.venue AS venue",
            "rr.race_no AS race_no",
            "re.car_no AS car_no",
            "re.race_time AS actual_race_time",
        ]
        optional_map = {
            "player_name": "re.player_name AS player_name",
            "trial_time": "re.trial_time AS trial_time",
            "start_time": "re.start_time AS st",
            "handicap": "re.handicap AS handicap",
            "finish": "re.finish AS finish",
            "result_status": "re.result_status AS result_status",
        }
        for c, sql in optional_map.items():
            if c in re_cols:
                select_parts.append(sql)

        # 開催条件はresult_races側にあれば拾う
        rr_optional = {
            "track_temp": "rr.track_temp AS track_temp",
            "weather": "rr.weather AS weather",
            "track_condition": "rr.track_condition AS track_condition",
            "temperature": "rr.temperature AS temperature",
            "humidity": "rr.humidity AS humidity",
            "start_time": "rr.start_time AS race_start_time",
        }
        for c, sql in rr_optional.items():
            if c in rr_cols:
                select_parts.append(sql)

        result_sql = f"""
            SELECT {", ".join(select_parts)}
              FROM result_races rr
              JOIN result_entries re ON re.race_key = rr.race_key
             WHERE re.race_time IS NOT NULL
               AND CAST(re.race_time AS REAL) BETWEEN 3.20 AND 4.50
               AND COALESCE(re.result_status, '通常') NOT IN (
                   '欠車','出走取消','発走除外','競走除外',
                   '落車','競走中止','失格','反則','反妨','周誤','周回誤認'
               )
             LIMIT {int(limit_rows)}
        """
        rdf = pd.read_sql_query(result_sql, con)
        if rdf.empty:
            return pd.DataFrame(), "競走T付きの実結果がありません"

        # ---------- 保存済み予測 ----------
        # Ver266専用テーブルに保存された「当時の予測T」を最優先する。
        _v266_ensure_pred_time_snapshot_table(db_path)
        sdf = pd.read_sql_query("""
            SELECT s.history_id,s.race_key,s.app_version AS version,s.car_no,
                   s.player_name,s.predicted_race_time AS pred_race_time,
                   s.source_kind,h.prediction_time
            FROM v266_pred_time_snapshots s
            LEFT JOIN v231_prediction_history h ON h.history_id=s.history_id
        """, con)
        if not sdf.empty:
            sdf["car_no"] = sdf["car_no"].astype(str).str.replace(".0","",regex=False).str.strip()
            rdf2 = rdf.copy()
            rdf2["car_no"] = rdf2["car_no"].astype(str).str.replace(".0","",regex=False).str.strip()
            m = sdf.merge(rdf2, on=["race_key","car_no"], how="inner", suffixes=("_pred",""))
            if not m.empty:
                m = _v266_coalesce_player_name_columns(m)
                m["pred_race_time"] = pd.to_numeric(m["pred_race_time"], errors="coerce")
                m["actual_race_time"] = pd.to_numeric(m["actual_race_time"], errors="coerce")
                m = m.dropna(subset=["pred_race_time","actual_race_time"])
                m["error_sec"] = m["actual_race_time"] - m["pred_race_time"]
                m["abs_error_sec"] = m["error_sec"].abs()
                return m, ""

        # 専用テーブルが空なら、旧履歴payloadから直接抽出を試す。
        pred_table = None
        for t in ("v231_prediction_history", "prediction_history", "predictions"):
            if t in tables:
                pred_table = t
                break
        if not pred_table:
            return pd.DataFrame(), "保存済み予測テーブルが見つかりません"

        pcols = _v266_table_columns(con, pred_table)

        # 既存DBで使われている候補名を幅広く許容
        p_date = _v266_guess_col(pcols, "race_date", "date", "開催日", "日付")
        p_venue = _v266_guess_col(pcols, "venue", "track", "開催場", "場")
        p_race = _v266_guess_col(pcols, "race_no", "race", "r", "レース")
        p_ver = _v266_guess_col(pcols, "version", "app_version", "ver")
        p_payload = _v266_guess_col(
            pcols,
            "payload_json", "prediction_json", "data_json", "payload",
            "json_data", "snapshot_json", "state_json", "prediction_state"
        )

        # race_keyがある履歴なら開催キーの復元にも使う
        p_race_key = _v266_guess_col(pcols, "race_key")

        cols = []
        for c in (p_date, p_venue, p_race, p_ver, p_payload, p_race_key):
            if c and c not in cols:
                cols.append(c)

        if not cols:
            return pd.DataFrame(), f"{pred_table} の予測保存カラムを特定できません: {pcols}"

        pq = (
            "SELECT rowid AS _rowid, "
            + ", ".join([f'"{c}"' for c in cols])
            + f' FROM "{pred_table}" ORDER BY rowid DESC LIMIT 3000'
        )
        pdf = pd.read_sql_query(pq, con)
        rename = {}
        if p_date: rename[p_date] = "date"
        if p_venue: rename[p_venue] = "venue"
        if p_race: rename[p_race] = "race_no"
        if p_ver: rename[p_ver] = "version"
        if p_payload: rename[p_payload] = "payload"
        if p_race_key: rename[p_race_key] = "race_key"
        pdf = pdf.rename(columns=rename)

        # race_keyしか無い場合はresult_racesから開催情報を補完
        if "race_key" in pdf.columns and any(c not in pdf.columns for c in ("date","venue","race_no")):
            key_map = pd.read_sql_query(
                "SELECT race_key, race_date AS date, venue, race_no FROM result_races",
                con,
            )
            pdf = pdf.merge(key_map, on="race_key", how="left", suffixes=("", "_rr"))
            for c in ("date","venue","race_no"):
                if c not in pdf.columns and f"{c}_rr" in pdf.columns:
                    pdf[c] = pdf[f"{c}_rr"]

        # JSONの中から車番別予測Tを再帰的に探索
        def _walk(obj):
            if isinstance(obj, dict):
                yield obj
                for v in obj.values():
                    yield from _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from _walk(v)

        car_keys = ("車番", "car_no", "number", "car", "枠番")
        pred_keys = (
            "Ver266補正後予測競走T",
            "Ver265補正後予測競走T",
            "予測競走T",
            "予測競走タイム",
            "pred_race_time",
            "predicted_race_time",
            "prediction_time",
            "pred_time",
        )

        exploded = []
        if "payload" in pdf.columns:
            for _, row in pdf.iterrows():
                raw = row.get("payload")
                if raw is None:
                    continue
                obj = raw
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        raw = raw.decode("utf-8")
                    except Exception:
                        continue
                if isinstance(raw, str):
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue

                seen = set()
                for d in _walk(obj):
                    car = None
                    pred = None
                    for k in car_keys:
                        if k in d:
                            car = d.get(k)
                            break
                    for k in pred_keys:
                        if k in d and _v266_num(d.get(k)) is not None:
                            pred = _v266_num(d.get(k))
                            break
                    if car is None or pred is None:
                        continue
                    key = (str(car), float(pred))
                    if key in seen:
                        continue
                    seen.add(key)
                    exploded.append({
                        "date": row.get("date"),
                        "venue": row.get("venue"),
                        "race_no": row.get("race_no"),
                        "race_key": row.get("race_key"),
                        "version": row.get("version", ""),
                        "car_no": car,
                        "pred_race_time": pred,
                        "_rowid": row.get("_rowid"),
                    })

        if not exploded:
            return pd.DataFrame(), (
                "保存済み予測はありますが、車番別の予測競走Tが履歴JSONに保存されていません。"
                " 今後の予測保存時にVer266の予測競走Tスナップショットを保存する必要があります。"
            )

        epdf = pd.DataFrame(exploded)

        # 正規化
        for df in (epdf, rdf):
            if "date" in df.columns:
                df["date"] = df["date"].astype(str).str.slice(0, 10)
            if "venue" in df.columns:
                df["venue"] = df["venue"].astype(str).str.strip()
            if "race_no" in df.columns:
                df["race_no"] = (
                    df["race_no"].astype(str)
                    .str.replace("R", "", regex=False)
                    .str.replace(".0", "", regex=False)
                    .str.strip()
                )
            df["car_no"] = (
                df["car_no"].astype(str)
                .str.replace(".0", "", regex=False)
                .str.strip()
            )

        # race_key優先、無ければ日付+場+R+車番
        if "race_key" in epdf.columns and epdf["race_key"].notna().any():
            m = epdf.merge(rdf, on=["race_key","car_no"], how="inner", suffixes=("_pred",""))
            # 表示用キーを実結果側に統一
            for c in ("date","venue","race_no"):
                pc = f"{c}_pred"
                if pc in m.columns and c in m.columns:
                    m.drop(columns=[pc], inplace=True)
        else:
            needed = {"date","venue","race_no","car_no"}
            if not needed.issubset(epdf.columns):
                return pd.DataFrame(), "予測履歴からレース結合キーを復元できません"
            m = epdf.merge(rdf, on=["date","venue","race_no","car_no"], how="inner")

        if m.empty:
            return m, "予測履歴と実結果を同一レース・車番で結合できません"

        m = _v266_coalesce_player_name_columns(m)
        m["pred_race_time"] = pd.to_numeric(m["pred_race_time"], errors="coerce")
        m["actual_race_time"] = pd.to_numeric(m["actual_race_time"], errors="coerce")
        m = m.dropna(subset=["pred_race_time","actual_race_time"])

        # 同じVer・同じレース・同じ車番の重複は最新予測だけ残す
        dedup_cols = [c for c in ("version","race_key","date","venue","race_no","car_no") if c in m.columns]
        if "_rowid" in m.columns and dedup_cols:
            m = m.sort_values("_rowid").drop_duplicates(dedup_cols, keep="last")

        m["error_sec"] = m["actual_race_time"] - m["pred_race_time"]
        m["abs_error_sec"] = m["error_sec"].abs()
        return m, ""
    finally:
        con.close()


def _v267_prepare_unique_actual_runs(rows):
    """
    学習・原因分析用。
    同一 race_key(or date+venue+race_no)+car_no を1実走に限定する。
    複数Verがある場合は最新保存履歴を代表値として採用し、
    同じ実走をVer数だけ重複カウントしない。
    """
    import pandas as pd
    if rows is None or rows.empty:
        return rows
    d = rows.copy()
    keys = ["race_key", "car_no"] if "race_key" in d.columns else ["date","venue","race_no","car_no"]
    order = []
    if "prediction_time" in d.columns:
        order.append("prediction_time")
    if "_rowid" in d.columns:
        order.append("_rowid")
    if order:
        d = d.sort_values(order)
    return d.drop_duplicates(keys, keep="last").copy()


def _v267_version_summary(rows):
    """バージョン評価用。実走重複は許さず、Verごとに別集計する。"""
    import pandas as pd
    if rows is None or rows.empty or "version" not in rows.columns:
        return pd.DataFrame()
    d = rows.copy()
    keys = ["version"]
    if "race_key" in d.columns:
        keys += ["race_key","car_no"]
    else:
        keys += ["date","venue","race_no","car_no"]
    d = d.drop_duplicates(keys, keep="last")
    g = d.groupby("version").agg(
        比較走数=("error_sec","size"),
        平均誤差秒=("error_sec","mean"),
        平均絶対誤差秒=("abs_error_sec","mean"),
    ).reset_index()
    g["90%誤差秒"] = d.groupby("version")["abs_error_sec"].quantile(0.90).values
    return g.sort_values(["平均絶対誤差秒","比較走数"], ascending=[True,False])


def _v267_handicap_error_curve(unique_rows):
    """
    ハンデ別の実測誤差を診断する。
    補正はまだ自動適用せず、サンプル数と縮小推定値を表示する。
    """
    import pandas as pd
    if unique_rows is None or unique_rows.empty or "handicap" not in unique_rows.columns:
        return pd.DataFrame()
    d = unique_rows.copy()
    d["handicap_num"] = pd.to_numeric(d["handicap"], errors="coerce")
    d = d.dropna(subset=["handicap_num","error_sec","abs_error_sec"])
    if d.empty:
        return pd.DataFrame()

    overall = float(d["error_sec"].mean())
    g = d.groupby("handicap_num").agg(
        走数=("error_sec","size"),
        平均誤差秒=("error_sec","mean"),
        平均絶対誤差秒=("abs_error_sec","mean"),
    ).reset_index().sort_values("handicap_num")

    # 少数サンプルをそのまま補正値にしないため20走相当の縮小。
    prior_n = 20.0
    g["縮小平均誤差秒"] = (
        g["走数"] * g["平均誤差秒"] + prior_n * overall
    ) / (g["走数"] + prior_n)
    # 実測誤差を打ち消す方向の参考補正値。診断表示のみ。
    g["参考補正秒"] = -g["縮小平均誤差秒"]
    return g


def _v266_render_error_analysis(db_path):
    import pandas as pd
    st.subheader("🔬 Ver267 基礎予測・誤差解析")
    st.caption(
        "①実走学習用は同一レース×車番を1走に重複排除、"
        "②バージョン比較用はVerごとに分離します。"
        "欠車・取消・落車・中止・失格・周誤などは除外します。"
    )
    rows, err = _v266_load_error_rows(db_path)
    if err:
        st.info(f"誤差解析データ未準備: {err}")
        return
    if rows.empty:
        st.info("比較できる予測×実結果がありません。")
        return

    unique = _v267_prepare_unique_actual_runs(rows)

    st.markdown("### ① 全実走・原因分析用")
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("実走数", f"{len(unique):,}")
    c2.metric("平均絶対誤差", f"{unique['abs_error_sec'].mean():.4f}秒")
    c3.metric("平均誤差", f"{unique['error_sec'].mean():+.4f}秒")
    c4.metric("90%誤差", f"{unique['abs_error_sec'].quantile(0.90):.4f}秒")
    if len(rows) != len(unique):
        st.caption(f"元の比較 {len(rows):,}件 → 同一実走のVer重複を除外して {len(unique):,}走")

    if "venue" in unique.columns:
        g = unique.groupby("venue").agg(
            走数=("error_sec","size"),
            平均誤差秒=("error_sec","mean"),
            平均絶対誤差秒=("abs_error_sec","mean"),
        ).reset_index().sort_values("平均絶対誤差秒", ascending=False)
        st.markdown("**開催場別**")
        st.dataframe(g, use_container_width=True, hide_index=True)

    if "player_name" in unique.columns:
        pg = unique.groupby("player_name").agg(
            走数=("error_sec","size"),
            平均誤差秒=("error_sec","mean"),
            平均絶対誤差秒=("abs_error_sec","mean"),
        ).reset_index()
        pg = pg[pg["走数"] >= 3].sort_values("平均絶対誤差秒", ascending=False).head(50)
        if not pg.empty:
            st.markdown("**選手別 誤差大きめ（3走以上）**")
            st.dataframe(pg, use_container_width=True, hide_index=True)

    curve = _v267_handicap_error_curve(unique)
    if not curve.empty:
        st.markdown("### 🎯 ハンデ別・実測誤差カーブ")
        st.caption(
            "参考補正秒は、同じ実走を複数Verで水増しせず算出しています。"
            "少数サンプルは全体平均へ縮小して暴れにくくしています。"
            "このVer267ではまだ予測式へ自動適用しません。"
        )
        st.dataframe(curve, use_container_width=True, hide_index=True)

    factors = []
    for col,label in [("trial_time","試走T"),("st","ST"),("handicap","ハンデ"),("track_temp","走路温度")]:
        if col in unique.columns:
            x = pd.to_numeric(unique[col], errors="coerce")
            y = pd.to_numeric(unique["error_sec"], errors="coerce")
            valid = x.notna() & y.notna()
            if valid.sum() >= 10 and x[valid].nunique() > 1:
                factors.append({
                    "候補要因":label,
                    "比較数":int(valid.sum()),
                    "誤差との相関":float(x[valid].corr(y[valid])),
                })
    if factors:
        fdf = pd.DataFrame(factors)
        fdf["絶対相関"] = fdf["誤差との相関"].abs()
        fdf = fdf.sort_values("絶対相関", ascending=False).drop(columns=["絶対相関"])
        st.markdown("**誤差原因候補（相関は因果ではありません）**")
        st.dataframe(fdf, use_container_width=True, hide_index=True)

    st.markdown("**実走ベース・誤差TOP30**")
    show_cols = [c for c in [
        "date","venue","race_no","car_no","player_name","version",
        "pred_race_time","actual_race_time","error_sec","abs_error_sec",
        "trial_time","st","handicap","track_temp","track_condition"
    ] if c in unique.columns]
    st.dataframe(
        unique.sort_values("abs_error_sec", ascending=False)[show_cols].head(30),
        use_container_width=True, hide_index=True
    )

    st.markdown("### ② バージョン比較用")
    vg = _v267_version_summary(rows)
    if vg.empty:
        st.info("バージョン別に比較できるデータがありません。")
    else:
        st.dataframe(vg, use_container_width=True, hide_index=True)
        st.caption(
            "ここはVerごとの評価専用です。ハンデ/ST/選手学習の統計には混ぜません。"
        )


# Ver266: 予測競走Tスナップショット管理
try:
    with st.expander("💾 Ver266 予測競走Tスナップショット", expanded=False):
        _v266_ensure_pred_time_snapshot_table(engine.DB_PATH)
        with sqlite3.connect(str(engine.DB_PATH)) as _c:
            _v266_snap_count = int(_c.execute("SELECT COUNT(*) FROM v266_pred_time_snapshots").fetchone()[0] or 0)
            _v266_hist_count = int(_c.execute("SELECT COUNT(*) FROM v231_prediction_history").fetchone()[0] or 0)
            _v266_snap_histories = int(_c.execute("SELECT COUNT(DISTINCT history_id) FROM v266_pred_time_snapshots").fetchone()[0] or 0)
        a,b,c = st.columns(3)
        a.metric("予測履歴", f"{_v266_hist_count}件")
        b.metric("予測T保存済み履歴", f"{_v266_snap_histories}件")
        c.metric("車番別予測T", f"{_v266_snap_count}走")
        st.caption(
            "過去履歴のprediction_viewに保存されているdfから、当時の予測競走Tをそのまま補完します。"
            "現在Verでの再計算値ではありません。"
        )
        if st.button("🔧 過去の保存済み予測Tを一括補完", key="v266_backfill_pred_time"):
            with st.spinner("保存済み履歴から予測競走Tを補完しています..."):
                _bf = _v266_backfill_pred_time_snapshots(engine.DB_PATH, 1000)
            st.success(
                f"確認{_bf['checked']}件 / 補完{_bf['histories_saved']}履歴・{_bf['cars_saved']}走 / "
                f"既登録{_bf['already']}件 / 予測Tなし{_bf['no_time']}件 / エラー{len(_bf['errors'])}件"
            )
            if _bf["errors"]:
                st.warning(" / ".join(_bf["errors"][:10]))
except Exception as _v266_snap_exc:
    st.warning("Ver266予測Tスナップショット管理エラー: " + _runtime_exception_text(_v266_snap_exc))


# Ver266: None選手名の診断・補修
try:
    with st.expander("🧩 Ver266 選手名None補修", expanded=False):
        with sqlite3.connect(str(engine.DB_PATH)) as _v266_name_con:
            _v266_missing_names = int(_v266_name_con.execute("""
                SELECT COUNT(*)
                  FROM result_entries
                 WHERE player_name IS NULL
                    OR TRIM(CAST(player_name AS TEXT))=''
                    OR LOWER(TRIM(CAST(player_name AS TEXT)))='none'
            """).fetchone()[0] or 0)
        st.metric("実結果の選手名未登録", f"{_v266_missing_names}件")
        st.caption(
            "公式結果の保存原文(v238_result_raw_archive)から、同じレース・車番の選手名だけを補完します。"
            "競走Tや着順など他の結果値は変更しません。"
        )
        if st.button("🔧 Noneの選手名を公式結果原文から補修", key="v266_repair_none_player_names"):
            _v266_name_result = _v266_repair_missing_result_player_names(engine.DB_PATH)
            st.success(
                f"確認{_v266_name_result['checked']}件 / 補修{_v266_name_result['repaired']}件 / "
                f"未解決{_v266_name_result['unresolved']}件 / エラー{len(_v266_name_result['errors'])}件"
            )
            if _v266_name_result["errors"]:
                st.warning(" / ".join(_v266_name_result["errors"][:10]))
except Exception as _v266_name_exc:
    st.warning("Ver266選手名補修エラー: " + _runtime_exception_text(_v266_name_exc))

# Ver266 diagnostic panel
try:
    with st.expander("🔬 Ver266 基礎予測・誤差解析", expanded=False):
        _v266_render_error_analysis(engine.DB_PATH)
except Exception as _v266_exc:
    st.warning("Ver266誤差解析の表示に失敗しました: " + _runtime_exception_text(_v266_exc))

from __future__ import annotations
_V303_MIXED_PLAN_FUTURE_CUTOFF = "2026-08-15-v1"

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
import zipfile
import threading
import traceback
import http.cookiejar
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
import math

# ---------------------------------------------------------------------------
# AutoRaceAI runtime configuration / state
# Ver265 refactor: version, modes and mutable caches are initialized in one
# place so maintenance/reconstruction paths cannot fail from definition order.
# Prediction formulas are intentionally unchanged by this refactor.
# ---------------------------------------------------------------------------
APP_VERSION = "Ver319"
SIMULATION_MODE = "6周内蔵型壁展開"

# Backward-compatible aliases used throughout the existing code.
_V231_APP_VERSION = "Ver319"  # Ver319: 壁は予測日前のみ（少件数は過去分で学習）。同ハンデ寄せは外す。時間補正は良/湿分離。

# Ver284 DB safety patch: protected fingerprint v3 / current+previous rollback guard
_V284_DB_GUARD_PATCH = "2026-08-09-v5-row-containment-sync"
_V284_TRANSITION_AUDIT_PATCH = "2026-08-09-v1"
_V284_DOWNLOAD_SNAPSHOT_PATCH = "2026-08-09-v1"
_V284_GITHUB_RECOVERY_PUSH_PATCH = "2026-08-09-v1"
_V284_UPLOAD_MASTER_PIN_PATCH = "2026-08-09-v1"
_V284_GITHUB_READBACK_VERIFY_PATCH = "2026-08-09-v1"
_V284_GITHUB_RAW_READBACK_PATCH = "2026-08-09-v1"
_V284_GITHUB_RAW_TOKEN_FIX = "2026-08-09-v1"
_V284_GITHUB_RELOAD_UNIFIED_VERIFY = "2026-08-09-v1"
_V284_BOOT_GITHUB_CANONICAL_RESTORE = "2026-08-09-v1"
_V284_DERIVED_TABLE_GUARD_FIX = "2026-08-09-v1"
_V284_DIVERGED_SAFE_AUTO_MERGE = "2026-08-09-v1"
_V302_DIVERGED_TABLE_MERGE_FIX = "2026-08-15-v1"
_V302_WAL_MERGE_CHECKPOINT_FIX = "2026-08-15-v1"
_V284_V252_SEMANTIC_CONTAINMENT = "2026-08-10-v3"
_V231_SIMULATION_MODE = SIMULATION_MODE

# Ver314: 再シミュレーション中のGitHub自動途中保存間隔（レース数）
# 50R前後は落ちにくい実績があるため、余裕を見て15Rごとに保存する。
_V314_AUTOSAVE_EVERY_N = 40

# Ver319: 起動時の事故監査は重いので既定スキップ（必要時のみ手動）。
# 古い全バージョンを毎回走査すると起動ループ落ちの原因になる。
V302_STARTUP_ACCIDENT_REPAIR = {"skipped": True, "reason": "起動時スキップ(Ver319)"}

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
        _V318_SAMEH_FLOW_CACHE,
        _V264_SCENARIO_FEEDBACK_CACHE,
        _V292_TRIAL_GAP_GATE_CACHE,
        _V294_FRONT_ST_GUARD_CACHE,
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
            "Ver268補正後予測競走T",
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
    """原因分析用: 同一レース×車番は1実走にする。複数Verで水増ししない。"""
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
    """Ver比較用: Verごとに同一レース×車番を1件へ整理して集計する。"""
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
    grouped = []
    for ver, g in d.groupby("version"):
        grouped.append({
            "version": ver,
            "比較走数": int(len(g)),
            "平均誤差秒": float(g["error_sec"].mean()),
            "平均絶対誤差秒": float(g["abs_error_sec"].mean()),
            "90%誤差秒": float(g["abs_error_sec"].quantile(0.90)),
        })
    return pd.DataFrame(grouped).sort_values(
        ["平均絶対誤差秒","比較走数"], ascending=[True,False]
    )


def _v267_handicap_error_curve(unique_rows):
    """ハンデ別の系統誤差を診断。少数データは全体平均へ縮小する。"""
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

    prior_n = 20.0
    g["縮小平均誤差秒"] = (
        g["走数"] * g["平均誤差秒"] + prior_n * overall
    ) / (g["走数"] + prior_n)
    g["参考補正秒"] = -g["縮小平均誤差秒"]
    return g


def _v266_render_error_analysis(db_path):
    import pandas as pd
    st.subheader("🔬 Ver268 基礎予測・誤差解析")
    st.caption(
        "原因分析では同一レース×車番を1実走として扱い、Ver違いの重複を除外します。"
        "バージョン評価は別枠で集計します。"
        "欠車・取消・落車・中止・失格・周誤などの異常結果は除外します。"
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
        st.caption(f"元の比較 {len(rows):,}件 → Ver重複排除後 {len(unique):,}実走")

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
            "同じ実走を複数Verで水増しせず算出。少数サンプルは20走相当で全体平均へ縮小。"
            "参考補正秒は診断表示のみで、まだ予測ロジックへ自動適用しません。"
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
                    "候補要因": label,
                    "比較数": int(valid.sum()),
                    "誤差との相関": float(x[valid].corr(y[valid])),
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
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("### ② バージョン比較用")
    vg = _v267_version_summary(rows)
    if vg.empty:
        st.info("バージョン別に比較できるデータがありません。")
    else:
        st.dataframe(vg, use_container_width=True, hide_index=True)
        st.caption("この表はVer評価専用です。原因学習の統計には混ぜません。")

st.set_page_config(page_title="AutoRaceAI スマホ本予測", page_icon="🏁", layout="wide")

# Ver290 hotfix2:
# 起動高速化（manifest-first / DB identity cache）で画面表示が十分軽くなったため、
# 「画面を開くたびにbatch_rerunを一時停止する」処理は廃止。
# バックグラウンド再シミュレーションは画面表示中も継続する。
# Ver267 refactor: runtime-state-centralized


# Ver228: DB保存時刻・画面表示時刻を日本時間へ統一。
_V228_JST = ZoneInfo("Asia/Tokyo")

def _v228_now_jst_iso() -> str:
    return datetime.now(_V228_JST).isoformat(timespec="seconds")

def _v228_format_saved_time(value) -> str:
    """新規のJST時刻と、旧版のUTC時刻を日本時間表示へそろえる。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        # 旧DBのタイムゾーンなし値は、従来の保存仕様に合わせてUTCとして扱う。
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_V228_JST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text.replace("T", " ")[:19]


# Ver227: 発走前除外と発走後事故を分離。
# Ver225: 欠車・発走前除外は「事故」ではなく事前除外として扱う。
# 比較用の行は補うが、事故判定に使われる欄へ「欠車」等の語を残さない。


# Ver229: 壁で詰まる時間と前残りを、イベントシミュレーション後の着順分布へ再配分する。
# Ver230: 6周内蔵型ベータ。過去周回結果と直接対戦履歴を事前学習して各試行へ反映。
# engine.pyを差し替えずに使えるよう、三連単カウントを穏やかに補正し、他券種も整合再集計する。
def _v229_num(value, default=0.0) -> float:
    try:
        n = float(value)
        return n if pd.notna(n) else float(default)
    except Exception:
        return float(default)


def _v229_wall_profile(df: pd.DataFrame, entries: pd.DataFrame) -> dict[int, dict]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    work = df.copy()
    car_col = "車" if "車" in work.columns else ("車番" if "車番" in work.columns else None)
    if not car_col:
        return {}
    work["_car"] = pd.to_numeric(work[car_col], errors="coerce")
    work = work.dropna(subset=["_car"]).copy()
    work["_car"] = work["_car"].astype(int)

    entry_map = {}
    if isinstance(entries, pd.DataFrame) and not entries.empty:
        ec = "車番" if "車番" in entries.columns else ("車" if "車" in entries.columns else None)
        if ec:
            for _, r in entries.iterrows():
                try:
                    entry_map[int(r[ec])] = r
                except Exception:
                    pass

    handicaps = {}
    for _, r in work.iterrows():
        car = int(r["_car"])
        er = entry_map.get(car)
        h = None
        for source in (r, er):
            if source is None:
                continue
            for key in ("ハンデ", "H", "handicap"):
                if key in source.index:
                    m = re.search(r"-?\d+", str(source.get(key, "")))
                    if m:
                        h = int(m.group())
                        break
            if h is not None:
                break
        handicaps[car] = int(h or 0)

    cars = sorted(handicaps, key=lambda c: (handicaps[c], c))
    if not cars:
        return {}
    min_h, max_h = min(handicaps.values()), max(handicaps.values())
    spread = max(10.0, float(max_h - min_h))
    same_counts = {c: sum(1 for x in cars if handicaps[x] == handicaps[c]) for c in cars}

    raw_break = {}
    for _, r in work.iterrows():
        car = int(r["_car"])
        vals = []
        for key in ("混戦突破適性", "展開適性点", "実戦能力点", "スタート伸び指数"):
            if key in r.index:
                vals.append(_v229_num(r.get(key), 0.0))
        raw_break[car] = sum(vals) / len(vals) if vals else 0.0
    if raw_break:
        lo, hi = min(raw_break.values()), max(raw_break.values())
    else:
        lo = hi = 0.0

    profile = {}
    for pos, car in enumerate(cars):
        h = handicaps[car]
        same_ahead = sum(1 for x in cars[:pos] if handicaps[x] == h)
        lower_ahead = sum(1 for x in cars[:pos] if handicaps[x] < h)
        density = max(0, same_counts[car] - 1) / max(1, len(cars) - 1)
        # 同ハンデの外枠、前ハンデ車の多さを壁リスクへ。上限を抑え、極端な改変を避ける。
        wall_risk = min(1.0, 0.16 * same_ahead + 0.085 * lower_ahead + 0.34 * density)
        bnorm = 0.5 if hi <= lo else (raw_break.get(car, lo) - lo) / (hi - lo)
        breakthrough = min(1.0, max(0.0, 0.25 + 0.75 * bnorm))
        frontness = 1.0 - (h - min_h) / spread
        inner_same = 1.0 - same_ahead / max(1, same_counts[car] - 1) if same_counts[car] > 1 else 0.5
        escape = max(0.0, min(1.0, 0.62 * frontness + 0.38 * inner_same))
        effective_loss = wall_risk * (1.0 - 0.68 * breakthrough)
        profile[car] = {
            "handicap": h, "wall_risk": wall_risk, "breakthrough": breakthrough,
            "escape": escape, "effective_loss": effective_loss, "initial_pos": pos + 1,
        }
    return profile


def _v229_apply_wall_distribution(df: pd.DataFrame, bets: dict, entries: pd.DataFrame, trials: int):
    if not isinstance(bets, dict) or not isinstance(bets.get("三連単"), dict) or not bets.get("三連単"):
        return df, bets, {"enabled": False, "reason": "三連単分布なし"}
    profile = _v229_wall_profile(df, entries)
    if len(profile) < 3:
        return df, bets, {"enabled": False, "reason": "壁判定データ不足"}

    tri = bets["三連単"]
    weighted = {}
    for combo, count in tri.items():
        try:
            a, b, c = map(int, combo)
            pa, pb, pc = profile[a], profile[b], profile[c]
        except Exception:
            weighted[combo] = float(count)
            continue
        # 先頭候補は逃げやすさを加点。後方候補は壁ロスを減点。
        # ただし捌き力が高い選手は減点を大幅に緩和する。
        factor = 1.0
        factor *= 1.0 + 0.16 * pa["escape"] - 0.12 * pa["effective_loss"]
        factor *= 1.0 - 0.11 * pb["effective_loss"]
        factor *= 1.0 - 0.07 * pc["effective_loss"]
        # 前ハンデ車が上位に残る自然な展開を少し優遇。
        if pa["handicap"] < pb["handicap"]:
            factor *= 1.045
        if pb["handicap"] <= pc["handicap"]:
            factor *= 1.015
        factor = max(0.76, min(1.24, factor))
        weighted[(a, b, c)] = max(0.0, float(count) * factor)

    total_w = sum(weighted.values())
    if total_w <= 0:
        return df, bets, {"enabled": False, "reason": "再配分失敗"}
    target = max(1, int(trials))
    scaled = {k: v / total_w * target for k, v in weighted.items()}
    ints = {k: int(v) for k, v in scaled.items()}
    remain = target - sum(ints.values())
    if remain > 0:
        for k, _ in sorted(scaled.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:remain]:
            ints[k] += 1

    new_bets = dict(bets)
    new_bets["三連単"] = ints
    trifuku, nitan, nifuku = {}, {}, {}
    for (a, b, c), cnt in ints.items():
        trifuku[tuple(sorted((a, b, c)))] = trifuku.get(tuple(sorted((a, b, c))), 0) + cnt
        nitan[(a, b)] = nitan.get((a, b), 0) + cnt
        nifuku[tuple(sorted((a, b)))] = nifuku.get(tuple(sorted((a, b))), 0) + cnt
    new_bets["三連複"] = trifuku
    new_bets["2連単"] = nitan
    new_bets["2連複"] = nifuku

    out_df = df.copy()
    car_col = "車" if "車" in out_df.columns else ("車番" if "車番" in out_df.columns else None)
    if car_col:
        out_df["壁リスク"] = out_df[car_col].map(lambda x: profile.get(int(x), {}).get("wall_risk", 0.0) * 100 if pd.notna(x) else 0.0)
        out_df["壁突破力"] = out_df[car_col].map(lambda x: profile.get(int(x), {}).get("breakthrough", 0.0) * 100 if pd.notna(x) else 0.0)
        out_df["前残り指数"] = out_df[car_col].map(lambda x: profile.get(int(x), {}).get("escape", 0.0) * 100 if pd.notna(x) else 0.0)
        out_df["壁ロス推定"] = out_df[car_col].map(lambda x: profile.get(int(x), {}).get("effective_loss", 0.0) * 100 if pd.notna(x) else 0.0)
    top_risk = sorted(profile.items(), key=lambda kv: kv[1]["effective_loss"], reverse=True)[:3]
    audit = {
        "enabled": True,
        "high_risk": [{"car": c, **v} for c, v in top_risk],
        "message": "同ハンデ密集・前車数・捌き力から、壁で失う時間と前残りを着順分布へ穏やかに反映",
    }
    return out_df, new_bets, audit



# Ver230 beta: 壁・飛び出し・追い抜きを6周イベント本体に組み込む。
# 既存の能力シミュレーションを土台として、過去周回結果と直接対戦履歴を事前学習に利用する。
def _v230_db_path() -> str:
    try:
        return str(engine.DB_PATH)
    except Exception:
        return "autorace_players.sqlite3"


def _v230_norm_name(value) -> str:
    return re.sub(r"[\s　]+", "", str(value or "")).strip()


def _v230_hist_profiles(venue: str, names: list[str]) -> dict[str, dict]:
    """過去結果から1周目飛び出し・前残り・追い上げ・抜き実績を縮小推定する。"""
    out = {n: {"starts": 0.0, "first_gain": 0.0, "hold": 0.5, "chase": 0.5, "overtake": 0.5, "sample": 0} for n in names}
    if not names:
        return out
    try:
        con = sqlite3.connect(_v230_db_path(), timeout=15)
        placeholders = ",".join("?" for _ in names)
        rows = con.execute(
            f"""
            SELECT p.player_name, lf.first_lap_pos, lf.final_pos, lf.net_gain,
                   lf.overtakes, lf.passed_by, lf.lead_laps, r.venue
            FROM lap_features lf
            JOIN race_entries re ON re.race_id=lf.race_id AND re.car_no=lf.car_no
            LEFT JOIN players p ON p.player_id=re.player_id
            JOIN races r ON r.race_id=lf.race_id
            WHERE p.player_name IN ({placeholders})
            """, names
        ).fetchall()
        stats = {n: [] for n in names}
        for name, first, final, gain, overtakes, passed_by, lead_laps, row_venue in rows:
            key = _v230_norm_name(name)
            if key not in stats:
                continue
            stats[key].append((first, final, gain, overtakes, passed_by, lead_laps, row_venue))
        for n, vals in stats.items():
            if not vals:
                continue
            # 開催場一致を1.35倍で重み付け。少数データは0.5へ縮小。
            wsum=0.0; first_gain=0.0; hold=0.0; chase=0.0; over=0.0
            for first, final, gain, ov, pb, lead, rv in vals:
                w=1.35 if venue and str(rv)==str(venue) else 1.0
                first=float(first or 0); final=float(final or first or 0); gain=float(gain or (first-final))
                ov=float(ov or 0); pb=float(pb or 0); lead=float(lead or 0)
                wsum += w
                first_gain += w * max(-4.0, min(4.0, gain))
                hold += w * (1.0 if first>0 and final<=first else 0.0)
                chase += w * (1.0 if first>0 and final<first else 0.0)
                over += w * ((ov+1.0)/(ov+pb+2.0))
            conf=min(1.0, wsum/18.0)
            out[n]={
                "starts": min(1.0, max(0.0, 0.5 + 0.08*(first_gain/max(wsum,1.0)))),
                "first_gain": first_gain/max(wsum,1.0),
                "hold": 0.5 + conf*((hold/max(wsum,1.0))-0.5),
                "chase": 0.5 + conf*((chase/max(wsum,1.0))-0.5),
                "overtake": 0.5 + conf*((over/max(wsum,1.0))-0.5),
                "sample": len(vals),
            }
        con.close()
    except Exception:
        pass
    return out




# Ver241: グランドノートから周回別の追い抜き・被追い抜き後失速を学習する。
def _v240_transition_profiles(venue: str, names: list[str]) -> dict[str, dict]:
    """player_lap_historyから周回別追抜傾向と、抜かれた直後の連鎖後退を縮小推定。"""
    base = {n: {
        "lap_attack": [0.0]*7, "lap_sample": [0]*7,
        "passed_slowdown": 0.18, "cascade_risk": 0.16,
        "pass_momentum": 0.14, "sample": 0,
    } for n in names}
    if not names:
        return base
    try:
        con = sqlite3.connect(_v230_db_path(), timeout=15)
        placeholders = ",".join("?" for _ in names)
        rows = con.execute(
            f"""
            SELECT plh.race_key, plh.player_name, plh.lap_no, plh.position,
                   COALESCE(r.venue, '')
            FROM player_lap_history plh
            LEFT JOIN races r ON r.race_key = plh.race_key
            WHERE REPLACE(REPLACE(plh.player_name,' ',''),'　','') IN ({placeholders})
              AND plh.lap_no IS NOT NULL
            ORDER BY plh.player_name, plh.race_key, plh.lap_no
            """, names
        ).fetchall()
        grouped = {}
        for race_key, name, lap_no, pos, row_venue in rows:
            key = _v230_norm_name(name)
            if key not in base:
                continue
            grouped.setdefault((key, race_key), []).append((int(lap_no or 0), int(pos or 0), str(row_venue or '')))
        accum = {n: {"w":0.0,"passed":0.0,"cascade":0.0,"momentum":0.0,"events":0.0,
                     "lap_gain":[0.0]*7,"lap_w":[0.0]*7} for n in names}
        for (name, _rk), vals in grouped.items():
            vals = sorted(vals)
            if len(vals) < 2:
                continue
            venue_w = 1.35 if venue and vals[0][2] == venue else 1.0
            for idx in range(1, len(vals)):
                lap, pos, _ = vals[idx]
                prev_pos = vals[idx-1][1]
                if prev_pos <= 0 or pos <= 0:
                    continue
                delta = prev_pos - pos  # +なら順位上昇
                li = max(1, min(6, lap))
                accum[name]["lap_gain"][li] += venue_w * max(-3, min(3, delta))
                accum[name]["lap_w"][li] += venue_w
                accum[name]["w"] += venue_w
                if delta > 0:
                    accum[name]["momentum"] += venue_w * min(2, delta)
                elif delta < 0:
                    accum[name]["passed"] += venue_w * min(2, -delta)
                    # 抜かれた次の周にも後退したら連鎖失速。
                    if idx + 1 < len(vals):
                        next_pos = vals[idx+1][1]
                        if next_pos > pos:
                            accum[name]["cascade"] += venue_w * min(2, next_pos-pos)
                accum[name]["events"] += venue_w
        for n, a in accum.items():
            ev = max(1.0, a["events"]); conf = min(1.0, ev/28.0)
            lap_attack=[0.0]*7; lap_sample=[0]*7
            for li in range(1,7):
                lw=a["lap_w"][li]
                lap_sample[li]=int(round(lw))
                if lw>0:
                    # 平均順位上昇を穏やかなlogit加点へ。
                    lap_attack[li]=max(-0.18,min(0.24,(a["lap_gain"][li]/lw)*0.075))*conf
            passed_rate=a["passed"]/ev
            cascade_rate=a["cascade"]/max(1.0,a["passed"])
            momentum_rate=a["momentum"]/ev
            base[n]={
                "lap_attack":lap_attack, "lap_sample":lap_sample,
                "passed_slowdown":0.12 + conf*max(0.0,min(0.22,passed_rate*0.10)),
                "cascade_risk":0.08 + conf*max(0.0,min(0.28,cascade_rate*0.16)),
                "pass_momentum":0.10 + conf*max(0.0,min(0.24,momentum_rate*0.10)),
                "sample":int(round(ev)),
            }
        con.close()
    except Exception:
        pass
    return base

def _v230_matchup_map(names: list[str]) -> dict[tuple[str,str], tuple[float,float]]:
    """直接対戦の追い抜き優位を返す。値は(優位度,信頼度)。"""
    result={}
    if not names:
        return result
    try:
        con=sqlite3.connect(_v230_db_path(), timeout=15)
        rows=con.execute(
            "SELECT player_a_name,player_b_name,shrunk_a_advantage,confidence FROM v152_player_overtake_matchups"
        ).fetchall()
        wanted=set(names)
        for a,b,adv,conf in rows:
            a=_v230_norm_name(a); b=_v230_norm_name(b)
            if a in wanted and b in wanted:
                result[(a,b)]=(float(adv or 0.0), float(conf or 0.0))
                result[(b,a)]=(-float(adv or 0.0), float(conf or 0.0))
        con.close()
    except Exception:
        pass
    return result


def _v230_num(value, default=0.0) -> float:
    """Ver230用の安全な数値変換。既存Ver229実装を再利用する。"""
    return _v229_num(value, default)


def _v230_col_num(row, keys, default=0.0):
    for k in keys:
        try:
            if k in row.index and pd.notna(row[k]):
                return float(row[k])
        except Exception:
            pass
    return float(default)




# Ver248: 実測の周回順位遷移から、開催場・周回別の追い抜き基準を時系列検証して校正する。
_V248_WALL_CALIBRATION_CACHE = {}

def _v248_logit(p: float) -> float:
    p = max(1e-5, min(1.0 - 1e-5, float(p)))
    return float(np.log(p / (1.0 - p)))

def _v248_wall_calibration(db_path: str | None = None, cutoff_date: str = "") -> dict:
    path = str(db_path or _v230_db_path())
    cutoff = str(cutoff_date or "").strip()[:10]
    try:
        stamp = (path, int(Path(path).stat().st_mtime_ns), cutoff)
    except Exception:
        stamp = (path, 0, cutoff)
    if stamp in _V248_WALL_CALIBRATION_CACHE:
        return dict(_V248_WALL_CALIBRATION_CACHE[stamp])
    result = {
        "enabled": False, "global_logit": 0.0, "venue_delta": {}, "lap_delta": {},
        "sample_transitions": 0, "validation_transitions": 0, "baseline_logloss": None,
        "calibrated_logloss": None, "reason": "周回履歴不足のため固定係数を使用",
    }
    try:
        with sqlite3.connect(path) as con:
            laps = pd.read_sql_query(
                "SELECT race_key,lap_no,position,car_no FROM result_laps", con
            )
            races = pd.read_sql_query(
                "SELECT race_key,race_date,venue,race_no FROM result_races "
                "WHERE COALESCE(learning_eligible,1)=1", con
            )
        if laps.empty or races.empty:
            _V248_WALL_CALIBRATION_CACHE.clear(); _V248_WALL_CALIBRATION_CACHE[stamp] = dict(result); return result
        for c in ("lap_no", "position", "car_no"):
            laps[c] = pd.to_numeric(laps[c], errors="coerce")
        laps = laps.dropna(subset=["lap_no", "position", "car_no"]).copy()
        laps[["lap_no", "position", "car_no"]] = laps[["lap_no", "position", "car_no"]].astype(int)
        rows = []
        for race_key, g in laps.groupby("race_key"):
            lap_values = sorted(g["lap_no"].unique())
            for lap in lap_values:
                cur = g[g["lap_no"] == lap].sort_values("position")
                nxt = g[g["lap_no"] == lap + 1].sort_values("position")
                if len(cur) < 3 or nxt.empty:
                    continue
                next_pos = dict(zip(nxt["car_no"].astype(int), nxt["position"].astype(int)))
                order = cur["car_no"].astype(int).tolist()
                for i in range(1, len(order)):
                    front, chaser = order[i - 1], order[i]
                    if front not in next_pos or chaser not in next_pos:
                        continue
                    rows.append({
                        "race_key": str(race_key), "lap": int(lap),
                        "passed": int(next_pos[chaser] < next_pos[front]),
                    })
        data = pd.DataFrame(rows)
        if data.empty:
            _V248_WALL_CALIBRATION_CACHE.clear(); _V248_WALL_CALIBRATION_CACHE[stamp] = dict(result); return result
        data = data.merge(races, on="race_key", how="inner")
        data["race_date"] = pd.to_datetime(data["race_date"], errors="coerce")
        if cutoff:
            data = data[data["race_date"] < pd.Timestamp(cutoff)].copy()
        data = data.sort_values(["race_date", "race_no", "race_key", "lap"])
        race_order = data[["race_key", "race_date", "race_no"]].drop_duplicates().sort_values(["race_date", "race_no", "race_key"])
        # 日付カット後は件数が減る。未来は使わず、過去が少ないときは検証分割せずに学習する。
        if len(race_order) < 8 or len(data) < 80:
            result["sample_transitions"] = int(len(data))
            result["reason"] = "予測日前の周回履歴が少なすぎるため固定係数"
            _V248_WALL_CALIBRATION_CACHE.clear(); _V248_WALL_CALIBRATION_CACHE[stamp] = dict(result); return result
        use_holdout = bool(len(race_order) >= 40 and len(data) >= 1200)
        if not use_holdout:
            train = data.copy()
            gp = float((train["passed"].sum() + 20.0) / (len(train) + 40.0))
            gl = _v248_logit(gp)
            def _group_delta_small(col: str, alpha: float) -> dict:
                agg = train.groupby(col)["passed"].agg(["sum", "count"])
                out = {}
                for key, row in agg.iterrows():
                    p = float((row["sum"] + alpha * gp) / (row["count"] + alpha))
                    out[key] = float(np.clip(_v248_logit(p) - gl, -0.28, 0.28))
                return out
            venue_delta = _group_delta_small("venue", 80.0)
            lap_delta = _group_delta_small("lap", 100.0)
            result.update({
                "enabled": True, "global_logit": gl, "venue_delta": venue_delta,
                "lap_delta": lap_delta, "sample_transitions": int(len(data)),
                "validation_transitions": 0, "baseline_logloss": None,
                "calibrated_logloss": None,
                "reason": f"予測日前{len(race_order)}R/{int(len(data))}遷移を検証なしで学習（未来は未使用）",
            })
            _V248_WALL_CALIBRATION_CACHE.clear(); _V248_WALL_CALIBRATION_CACHE[stamp] = dict(result)
            return result
        split = max(25, min(len(race_order) - 12, int(round(len(race_order) * 0.70))))
        train_keys = set(race_order.iloc[:split]["race_key"].astype(str))
        valid_keys = set(race_order.iloc[split:]["race_key"].astype(str))
        train = data[data["race_key"].astype(str).isin(train_keys)].copy()
        valid = data[data["race_key"].astype(str).isin(valid_keys)].copy()
        gp = float((train["passed"].sum() + 20.0) / (len(train) + 40.0))
        gl = _v248_logit(gp)
        def _group_delta(col: str, alpha: float) -> dict:
            agg = train.groupby(col)["passed"].agg(["sum", "count"])
            out = {}
            for key, row in agg.iterrows():
                p = float((row["sum"] + alpha * gp) / (row["count"] + alpha))
                out[key] = float(np.clip(_v248_logit(p) - gl, -0.45, 0.45))
            return out
        venue_delta = _group_delta("venue", 80.0)
        lap_delta = _group_delta("lap", 100.0)
        y = valid["passed"].to_numpy(dtype=float)
        base_p = np.full(len(valid), gp, dtype=float)
        z = np.asarray([gl + venue_delta.get(r["venue"], 0.0) + lap_delta.get(int(r["lap"]), 0.0) for _, r in valid.iterrows()], dtype=float)
        cal_p = 1.0 / (1.0 + np.exp(-z))
        def _ll(prob):
            prob = np.clip(prob, 1e-5, 1 - 1e-5)
            return float(-np.mean(y * np.log(prob) + (1 - y) * np.log(1 - prob)))
        base_ll, cal_ll = _ll(base_p), _ll(cal_p)
        enabled = bool(cal_ll + 0.001 < base_ll)
        result.update({
            "enabled": enabled, "global_logit": gl, "venue_delta": venue_delta if enabled else {},
            "lap_delta": lap_delta if enabled else {}, "sample_transitions": int(len(data)),
            "validation_transitions": int(len(valid)), "baseline_logloss": base_ll,
            "calibrated_logloss": cal_ll,
            "reason": (
                f"時系列検証でLogLossが{base_ll:.4f}→{cal_ll:.4f}へ改善したため反映"
                if enabled else f"時系列検証で改善幅不足（{base_ll:.4f}→{cal_ll:.4f}）のため停止"
            ),
        })
    except Exception as exc:
        result["reason"] = f"校正失敗: {exc}"
    _V248_WALL_CALIBRATION_CACHE.clear(); _V248_WALL_CALIBRATION_CACHE[stamp] = dict(result)
    return result




# Ver253: 実際のグランドノートを1周ごとに照合し、開催場・周回・ハンデ差別の
# 追い抜き発生率を時系列で学習する。予測対象日以降の結果は使わない。
_V251_LAP_ALIGNMENT_CACHE = {}

def _v251_logit(p: float) -> float:
    p = max(0.015, min(0.985, float(p)))
    return float(np.log(p / (1.0 - p)))

def _v251_gap_bucket(gap: int) -> str:
    g = max(0, int(gap or 0))
    if g <= 0: return "0m"
    if g <= 10: return "10m"
    if g <= 20: return "20m"
    return "30m+"

def _v251_lap_alignment_calibration(db_path: str | None, venue: str, cutoff_date: str) -> dict:
    """実測グランドノートの隣接入替を周回・ハンデ差別に縮小推定する。"""
    result = {
        "enabled": False, "sample_pairs": 0, "races": 0,
        "global_pass_rate": 0.0, "lap_bucket_delta": {},
        "lap_pass_rate": {}, "reason": "グランドノート履歴不足",
    }
    if not db_path or not Path(db_path).exists():
        return result
    try:
        stamp=(str(db_path), Path(db_path).stat().st_mtime_ns, str(venue), str(cutoff_date)[:10])
    except Exception:
        stamp=(str(db_path), str(venue), str(cutoff_date)[:10])
    if stamp in _V251_LAP_ALIGNMENT_CACHE:
        return dict(_V251_LAP_ALIGNMENT_CACHE[stamp])
    try:
        con=sqlite3.connect(str(db_path))
        q="""
        SELECT rl.race_key, rr.race_date, rr.venue, rl.lap_no, rl.lap_label,
               rl.position, rl.car_no,
               COALESCE(CAST(REPLACE(REPLACE(re.handicap,'m',''),'Ｍ','') AS INTEGER),0) AS handicap
        FROM result_laps rl
        JOIN result_races rr ON rr.race_key=rl.race_key
        LEFT JOIN races r ON r.race_key=rl.race_key
        LEFT JOIN race_entries re ON re.race_id=r.race_id AND re.car_no=rl.car_no
        WHERE COALESCE(rr.learning_eligible,1)=1
          AND (?='' OR rr.venue=?)
          AND (?='' OR substr(rr.race_date,1,10) < substr(?,1,10))
        ORDER BY rl.race_key, COALESCE(rl.lap_no,999), rl.position
        """
        rows=pd.read_sql_query(q, con, params=(venue,venue,cutoff_date,cutoff_date))
        con.close()
    except Exception as e:
        result["reason"]=f"周回履歴読込失敗: {e}"
        return result
    if rows.empty:
        return result
    events=[]
    race_count=0
    for race_key, grp in rows.groupby('race_key'):
        laps=[]
        for lap_key, lg in grp.groupby(['lap_no','lap_label'], dropna=False, sort=False):
            lg=lg.sort_values('position')
            order=[int(x) for x in lg['car_no'].tolist()]
            hmap={int(r.car_no): int(r.handicap or 0) for r in lg.itertuples()}
            lap_no=lap_key[0]
            try: lap_no=int(lap_no)
            except Exception: lap_no=len(laps)+1
            laps.append((lap_no, order, hmap))
        laps.sort(key=lambda x:x[0])
        if len(laps)<2: continue
        race_count += 1
        for idx in range(1,len(laps)):
            lap_no, cur, hmap=laps[idx]
            prev=laps[idx-1][1]
            cur_pos={c:i for i,c in enumerate(cur)}
            for j in range(1,len(prev)):
                front, chaser=prev[j-1],prev[j]
                if front not in cur_pos or chaser not in cur_pos: continue
                success=1 if cur_pos[chaser] < cur_pos[front] else 0
                gap=max(0, int(hmap.get(chaser,0))-int(hmap.get(front,0)))
                events.append((int(lap_no), _v251_gap_bucket(gap), success))
    if len(events)<120:
        result.update({"sample_pairs":len(events),"races":race_count})
        _V251_LAP_ALIGNMENT_CACHE[stamp]=dict(result)
        return result
    ev=pd.DataFrame(events,columns=['lap','bucket','success'])
    global_rate=float(ev.success.mean())
    deltas={}; lap_rates={}
    for lap, lg in ev.groupby('lap'):
        lap_rates[int(lap)]={"rate":float(lg.success.mean()),"n":int(len(lg))}
        for bucket,bg in lg.groupby('bucket'):
            n=len(bg); raw=float(bg.success.mean())
            # 少数データは全体へ戻す。n=35で半分程度の反映。
            w=n/(n+35.0)
            shrunk=global_rate + w*(raw-global_rate)
            delta=float(np.clip(_v251_logit(shrunk)-_v251_logit(global_rate),-0.42,0.42))
            deltas[f"{int(lap)}|{bucket}"]={"delta":delta,"rate":raw,"n":int(n),"shrunk_rate":shrunk}
    result={
        "enabled":True,"sample_pairs":int(len(ev)),"races":int(race_count),
        "global_pass_rate":global_rate,"lap_bucket_delta":deltas,
        "lap_pass_rate":lap_rates,"reason":"開催日前の実測グランドノートから周回別に学習",
    }
    _V251_LAP_ALIGNMENT_CACHE.clear(); _V251_LAP_ALIGNMENT_CACHE[stamp]=dict(result)
    return result

def _v251_actual_lap_orders(db_path: str | None, meta: dict) -> list[tuple[str, tuple[int,...]]]:
    """同一レースの実測グランドノートが登録済みなら周回順を返す。"""
    if not db_path or not Path(db_path).exists(): return []
    race_date=str((meta or {}).get('開催日') or (meta or {}).get('race_date') or '')[:10]
    venue=str((meta or {}).get('開催場') or (meta or {}).get('venue') or '')
    race_no=str((meta or {}).get('R') or (meta or {}).get('レース') or (meta or {}).get('race_no') or '').strip().replace('R','')
    try:
        con=sqlite3.connect(str(db_path))
        q="""
        SELECT rl.lap_label, rl.lap_no, rl.position, rl.car_no
        FROM result_laps rl JOIN result_races rr ON rr.race_key=rl.race_key
        WHERE substr(rr.race_date,1,10)=? AND rr.venue=?
          AND REPLACE(COALESCE(rr.race_no,''),'R','')=?
        ORDER BY COALESCE(rl.lap_no,999), rl.position
        """
        d=pd.read_sql_query(q,con,params=(race_date,venue,race_no)); con.close()
    except Exception:
        return []
    if d.empty:return []
    out=[]
    for (label,lap_no),g in d.groupby(['lap_label','lap_no'],dropna=False,sort=False):
        out.append((str(label),tuple(int(x) for x in g.sort_values('position').car_no.tolist())))
    return out

def _v251_pairwise_accuracy(pred: tuple[int,...], actual: tuple[int,...]) -> float:
    common=[c for c in actual if c in pred]
    if len(common)<2:return 0.0
    pp={c:i for i,c in enumerate(pred)}; ap={c:i for i,c in enumerate(actual)}
    ok=tot=0
    for i,a in enumerate(common):
        for b in common[i+1:]:
            tot+=1; ok += int((pp[a]<pp[b])==(ap[a]<ap[b]))
    return ok/tot if tot else 0.0



# Ver253: 予測時に代表隊列を保存し、後日登録された実測グランドノートとの差を
# 開催場・周回・ハンデ差別に学習する。結果登録済みレースの再計算はバックテスト扱いで学習除外。
# Ver262 bugfix: 一括補完から通常保存関数が直接呼ばれても NameError にならないよう初期化。
_V253_RECONSTRUCTION_MODE = False
_V252_LAP_RESIDUAL_CACHE = {}

def _v252_ensure_lap_tables(con: sqlite3.Connection) -> None:
    con.execute("""
    CREATE TABLE IF NOT EXISTS v252_lap_prediction_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_date TEXT NOT NULL,
        venue TEXT NOT NULL,
        race_no TEXT NOT NULL,
        lap_no INTEGER NOT NULL,
        predicted_order TEXT NOT NULL,
        support REAL,
        app_version TEXT NOT NULL,
        is_backtest INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """)
    con.execute("""
    CREATE INDEX IF NOT EXISTS idx_v252_lap_pred_race
    ON v252_lap_prediction_snapshots(race_date, venue, race_no, lap_no, created_at)
    """)

# Ver253 uses the same snapshot schema introduced in Ver252.
# Keep a dedicated wrapper so Ver253 maintenance actions do not fail on a missing symbol.
def _v253_ensure_lap_tables(con: sqlite3.Connection) -> None:
    _v252_ensure_lap_tables(con)

def _v252_save_lap_prediction(db_path: str | None, meta: dict, modal_laps: list[dict], is_backtest: bool, app_version_override: str = '') -> dict:
    """Save one race's representative lap orders and verify that all laps were persisted.

    Ver260 storage fix: this used to fail silently, which made the precision screen show
    fewer races than were actually predicted.  Return a status object so the caller can
    surface failures instead of quietly losing the comparison snapshot.
    """
    status={"ok":False,"saved_laps":0,"expected_laps":len(modal_laps or []),"reason":""}
    if not db_path or not Path(db_path).exists():
        status["reason"]="DBファイルが見つかりません"; return status
    if not modal_laps:
        status["reason"]="代表周回隊列が0件です"; return status
    m=meta or {}
    race_date=str(m.get('開催日') or m.get('日付') or m.get('race_date') or m.get('date') or '')[:10]
    venue=str(m.get('開催場') or m.get('場') or m.get('venue') or m.get('track') or '').strip()
    race_no=str(m.get('R') or m.get('レース') or m.get('レース番号') or m.get('race_no') or m.get('race') or '').strip()
    race_no=re.sub(r'[^0-9]', '', race_no) or race_no.replace('R','').replace('r','').strip()
    status.update({"race_date":race_date,"venue":venue,"race_no":race_no})
    if not race_date or not venue or not race_no:
        status["reason"]=f"レースキー不足: date={race_date or '-'} venue={venue or '-'} R={race_no or '-'}"; return status
    # 再構成は従来どおりVer253の検証材料、通常予測は現在の実行版として保存する。
    app_version=(str(app_version_override or '').strip() or ('Ver253' if _V253_RECONSTRUCTION_MODE else str(globals().get('_V231_APP_VERSION') or 'Ver265')))
    bt=(2 if _V253_RECONSTRUCTION_MODE else (1 if is_backtest else 0))
    status.update({"app_version":app_version,"is_backtest":bt})
    try:
        with sqlite3.connect(str(db_path), timeout=30) as con:
            _v252_ensure_lap_tables(con)
            con.execute("""
                DELETE FROM v252_lap_prediction_snapshots
                WHERE race_date=? AND venue=? AND race_no=? AND app_version=?
            """, (race_date,venue,race_no,app_version))
            inserted=0
            for row in modal_laps:
                lap_no=int(row.get('lap',0) or 0)
                order=str(row.get('order','') or '').strip()
                if lap_no<=0 or not order:
                    continue
                con.execute("""
                    INSERT INTO v252_lap_prediction_snapshots
                    (race_date,venue,race_no,lap_no,predicted_order,support,app_version,is_backtest)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (race_date,venue,race_no,lap_no,order,float(row.get('support',0.0) or 0.0),app_version,bt))
                inserted+=1
            con.commit()
            saved=int(con.execute("""
                SELECT COUNT(*) FROM v252_lap_prediction_snapshots
                WHERE race_date=? AND venue=? AND race_no=? AND app_version=?
            """,(race_date,venue,race_no,app_version)).fetchone()[0])
        status["saved_laps"]=saved
        status["ok"]=(saved==inserted and saved>0)
        status["reason"]=("保存・検証OK" if status["ok"] else f"保存件数不一致: insert={inserted}, db={saved}")
        _V252_LAP_RESIDUAL_CACHE.clear()
        return status
    except Exception as exc:
        status["reason"]=f"{type(exc).__name__}: {exc}"
        return status


def _v260_snapshot_from_saved_view(db_path: str, view: dict, source_app_version: str = '') -> dict:
    """Register lap snapshots from an already-saved prediction without rerunning it.

    Important: preserve the source prediction version. Restoring a Ver253 prediction must
    remain Ver253 and must not create a new Ver260 ROI/prediction record.
    """
    status={"ok":False,"saved_laps":0,"reason":"保存済み予測に周回隊列がありません"}
    if not isinstance(view,dict) or not view:
        status["reason"]="保存済み予測を読み込めません"; return status
    meta=dict(view.get("meta") or {})
    audit=meta.get("6周展開シミュレーション") or meta.get("壁補正監査") or {}
    laps=list(audit.get("predicted_lap_orders") or []) if isinstance(audit,dict) else []
    if not laps:
        return status
    ver=str(source_app_version or view.get("app_version") or "").strip() or str(globals().get('_V231_APP_VERSION') or 'Ver265')
    # This is a stored prediction generated at its original time, not a result-aware rerun.
    return _v252_save_lap_prediction(db_path,meta,laps,False,app_version_override=ver)


def _v260_repair_saved_snapshot_history(db_path: str, app_version: str = '', limit: int = 200) -> dict:
    """Backfill missing lap snapshots directly from saved prediction payloads."""
    out={"checked":0,"repaired_races":0,"repaired_laps":0,"already_ok":0,"no_laps":0,"errors":[],"labels":[]}
    histories=_v231_list_prediction_histories(db_path,max(1,int(limit)))
    target=str(app_version or '').strip()
    for h in histories:
        if target and str(h.get('app_version') or '') != target:
            continue
        out["checked"]+=1
        try:
            view,_,_,hm=_v231_load_prediction_history(db_path,int(h.get('history_id') or 0))
            if not view:
                out["errors"].append(f"履歴ID{h.get('history_id')}: 読込失敗"); continue
            meta=view.get('meta') or {}
            race_date=str(meta.get('開催日') or meta.get('日付') or meta.get('race_date') or meta.get('date') or '')[:10]
            venue=str(meta.get('開催場') or meta.get('場') or meta.get('venue') or meta.get('track') or '').strip()
            race_no=str(meta.get('R') or meta.get('レース') or meta.get('レース番号') or meta.get('race_no') or meta.get('race') or '').strip()
            race_no=re.sub(r'[^0-9]','',race_no) or race_no.replace('R','').replace('r','').strip()
            ver=str(hm.get('app_version') or h.get('app_version') or view.get('app_version') or target or _V231_APP_VERSION)
            if race_date and venue and race_no:
                with sqlite3.connect(str(db_path)) as con:
                    _v252_ensure_lap_tables(con)
                    n=int(con.execute("SELECT COUNT(*) FROM v252_lap_prediction_snapshots WHERE race_date=? AND venue=? AND race_no=? AND app_version=?",(race_date,venue,race_no,ver)).fetchone()[0] or 0)
                if n>=6:
                    out["already_ok"]+=1; continue
            audit=(meta.get('6周展開シミュレーション') or meta.get('壁補正監査') or {}) if isinstance(meta,dict) else {}
            laps=(audit.get('predicted_lap_orders') or []) if isinstance(audit,dict) else []
            if not laps:
                out["no_laps"]+=1; continue
            stt=_v260_snapshot_from_saved_view(db_path,view,ver)
            if stt.get('ok'):
                out["repaired_races"]+=1
                out["repaired_laps"]+=int(stt.get('saved_laps',0) or 0)
                out["labels"].append(str(h.get('race_label') or f"履歴ID{h.get('history_id')}"))
            else:
                out["errors"].append(f"{h.get('race_label')}: {stt.get('reason','保存失敗')}")
        except Exception as exc:
            out["errors"].append(f"{h.get('race_label') or h.get('history_id')}: {type(exc).__name__}: {exc}")
    out["message"]=(f"確認{out['checked']}件 / 修復{out['repaired_races']}レース・{out['repaired_laps']}周 / "
                    f"既登録{out['already_ok']}件 / 周回材料なし{out['no_laps']}件")
    return out


# Ver264 maintenance: 一括再シミュレーションを現行Ver264でも継承し、周回スナップショットまで原子的に保存する。
# Ver262: 保存済み予測を現在バージョンで一括再シミュレーションする。
# 同一レース・現行Verの履歴が既に存在する場合は重複作成せずスキップする。

# ---------------------------------------------------------------------------
# Ver272: 時系列再シミュレーション後の「仮想100円均等」回収率バックテスト
# 実購入処理ではなく、保存済み過去レースの評価専用。
# オッズは同一レースに保存されている最古スナップショットを全Ver共通で使う。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Ver272評価機能: バージョン横並び・共通レース回収率比較
# 予測ロジックは変更しない。
# 全選択Verに保存予測がある共通race_keyだけを使い、
# 同一レースでは同一の最古オッズスナップショットで買い目を再生成して採点する。
# ---------------------------------------------------------------------------

def _v272_version_race_histories(db_path: str, versions: list[str]) -> dict:
    out = {str(v): {} for v in versions}
    if not versions:
        return out
    _v231_ensure_prediction_history_table(db_path)
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        qs = ",".join(["?"] * len(versions))
        rows = con.execute(f"""
            SELECT history_id, race_key, app_version, prediction_time
            FROM v231_prediction_history
            WHERE app_version IN ({qs})
            ORDER BY datetime(prediction_time) ASC, history_id ASC
        """, tuple(str(v) for v in versions)).fetchall()
    for r in rows:
        ver = str(r["app_version"] or "")
        rk = str(r["race_key"] or "")
        if ver in out and rk:
            out[ver][rk] = int(r["history_id"])
    return out


def _v280_saved_plan_exact_same_odds_score(
    db_path: str, race_key: str, version: str,
    shared_odds_maps: dict, shared_odds_run: dict,
) -> dict:
    """同レース・同Verに、共通最古オッズと完全一致する照合済みプランがあればそのまま採点値を返す。"""
    out = {}
    bet_key_map = {
        "三連単":"3tan", "三連複":"3fuku",
        "2連単":"2tansho", "2車単":"2tansho",
        "2連複":"2fuku", "2車複":"2fuku",
        "単勝":"tansho", "複勝":"fukusho", "ワイド":"wide",
    }
    try:
        with sqlite3.connect(str(db_path), timeout=30.0) as con:
            con.row_factory = sqlite3.Row
            plans = con.execute("""
                SELECT r.plan_hash,r.points,r.cost_yen,
                       f.hit,f.payout_yen,f.return_rate,
                       f.evaluated_at,r.created_at
                FROM v187_mixed_plan_runs r
                JOIN v187_mixed_plan_feedback f
                  ON f.race_key=r.race_key AND f.plan_hash=r.plan_hash
                WHERE r.race_key=?
                  AND COALESCE(NULLIF(r.app_version,''),'Unknown')=?
                  AND f.return_rate IS NOT NULL
                ORDER BY datetime(f.evaluated_at) DESC,
                         datetime(r.created_at) DESC
            """, (str(race_key), str(version))).fetchall()

            for p in plans:
                tickets = con.execute("""
                    SELECT bet_type,combination,odds
                    FROM v187_mixed_plan_tickets
                    WHERE race_key=? AND plan_hash=?
                """, (str(race_key), str(p["plan_hash"]))).fetchall()
                if not tickets:
                    continue
                same = True
                for tk in tickets:
                    key = bet_key_map.get(str(tk["bet_type"] or ""))
                    if not key:
                        same = False
                        break
                    combo = str(tk["combination"] or "").strip()
                    saved = (shared_odds_maps.get(key,{}) or {}).get(combo)
                    try:
                        if saved is None or abs(float(saved)-float(tk["odds"] or 0)) > 1e-6:
                            same = False
                            break
                    except Exception:
                        same = False
                        break
                if same:
                    cost = int(p["cost_yen"] or int(p["points"] or 0)*100)
                    payout = int(p["payout_yen"] or 0)
                    return {
                        "version": str(version), "race_key": str(race_key),
                        "evaluated": True, "points": int(p["points"] or 0),
                        "cost_yen": cost, "payout_yen": payout,
                        "return_rate": float(p["return_rate"] or 0.0),
                        "hit": bool(int(p["hit"] or 0)),
                        "reason": "同一最古オッズの保存済み照合プラン",
                        "odds_created_at": str((shared_odds_run or {}).get("created_at") or ""),
                        "odds_snapshot_id": str((shared_odds_run or {}).get("snapshot_id") or ""),
                    }
    except Exception:
        pass
    return out


def _v272_score_saved_history_same_odds(
    db_path: str,
    history_id: int,
    race_key: str,
    version: str,
    shared_odds_maps: dict,
    shared_odds_run: dict,
) -> dict:
    out = {
        "version": str(version), "race_key": str(race_key), "history_id": int(history_id),
        "evaluated": False, "points": 0, "cost_yen": 0, "payout_yen": 0,
        "return_rate": None, "hit": False, "reason": "",
        "odds_created_at": str((shared_odds_run or {}).get("created_at") or ""),
        "odds_snapshot_id": str((shared_odds_run or {}).get("snapshot_id") or ""),
    }
    try:
        _saved_same280 = _v280_saved_plan_exact_same_odds_score(
            db_path, str(race_key), str(version), shared_odds_maps or {}, shared_odds_run or {}
        )
        if _saved_same280.get("evaluated"):
            _saved_same280["history_id"] = int(history_id)
            return _saved_same280

        view, raw_text, venue_override, hist_meta = _v231_load_prediction_history(db_path, int(history_id))
        if not isinstance(view, dict):
            out["reason"] = "保存予測を復元できません"
            return out

        bets = view.get("bets")
        meta = view.get("meta") or {}
        trials = int(view.get("trials") or 0)
        if not isinstance(bets, dict) or not bets:
            out["reason"] = "保存予測にbetsなし"
            return out
        if sum(len(v) for v in (shared_odds_maps or {}).values()) <= 0:
            out["reason"] = "共通オッズなし"
            return out

        result = v184_eight_car_mixed_plan(bets, trials, meta, shared_odds_maps)
        if not isinstance(result, dict) or not result.get("available"):
            out["reason"] = str((result or {}).get("reason") or "買い目再生成不可")
            return out

        plan_hash = _v187_save_mixed_plan(
            db_path, str(race_key), result, app_version=str(version)
        )
        if not plan_hash:
            out["reason"] = "プラン保存失敗"
            return out

        _v212_recalculate_plan_feedback(db_path, str(race_key), str(plan_hash))

        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("""
                SELECT r.points, r.cost_yen,
                       f.hit, f.payout_yen, f.return_rate
                FROM v187_mixed_plan_runs r
                LEFT JOIN v187_mixed_plan_feedback f
                  ON f.race_key=r.race_key AND f.plan_hash=r.plan_hash
                WHERE r.race_key=? AND r.plan_hash=?
                LIMIT 1
            """, (str(race_key), str(plan_hash))).fetchone()

        if row is None:
            out["reason"] = "採点レコードなし"
            return out

        out["points"] = int(row["points"] or 0)
        out["cost_yen"] = int(row["cost_yen"] or out["points"] * 100)
        if row["return_rate"] is None:
            out["reason"] = "払戻未登録"
            return out

        out["evaluated"] = True
        out["hit"] = bool(int(row["hit"] or 0))
        out["payout_yen"] = int(row["payout_yen"] or 0)
        out["return_rate"] = float(row["return_rate"] or 0.0)
        out["reason"] = "採点済み"

        try:
            _extra276=_v276_virtual_extra_all_odds_score(
                db_path,str(race_key),bets,int(trials or 0),shared_odds_maps,result or {},
            )
            if _extra276.get("available"):
                out["virtual_extra_points"]=int(_extra276.get("points",0) or 0)
                out["virtual_extra_cost_yen"]=int(_extra276.get("cost_yen",0) or 0)
                out["virtual_extra_payout_yen"]=int(_extra276.get("payout_yen",0) or 0)
                out["virtual_extra_hits"]=int(_extra276.get("hits",0) or 0)
                out["virtual_extra_rows"]=list(_extra276.get("rows") or [])
                out["virtual_combined_model_return_rate"]=_extra276.get("combined_model_return_rate")
            out["virtual_combined_cost_yen"]=int(out["cost_yen"])+int(out["virtual_extra_cost_yen"])
            out["virtual_combined_payout_yen"]=int(out["payout_yen"])+int(out["virtual_extra_payout_yen"])
            out["virtual_combined_return_rate"]=(
                float(out["virtual_combined_payout_yen"])/float(out["virtual_combined_cost_yen"])*100.0
                if int(out["virtual_combined_cost_yen"])>0 else None
            )
        except Exception:
            out["virtual_combined_cost_yen"]=int(out["cost_yen"])
            out["virtual_combined_payout_yen"]=int(out["payout_yen"])
            out["virtual_combined_return_rate"]=out["return_rate"]
        return out
    except Exception as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"
        return out


def _v272_common_race_roi_compare(db_path: str, versions: list[str]) -> dict:
    versions = [str(v) for v in versions if str(v)]
    out = {
        "versions": versions, "common_races": [], "rows": [], "summary": [],
        "missing_odds": 0, "missing_payout": 0,
    }
    if len(versions) < 2:
        return out

    hist = _v272_version_race_histories(db_path, versions)
    sets = [set(hist[v].keys()) for v in versions]
    common = sorted(set.intersection(*sets)) if sets else []
    out["common_races"] = common

    for race_key in common:
        odds_maps, odds_run = _v273_load_earliest_saved_odds(db_path, race_key)
        if sum(len(v) for v in odds_maps.values()) <= 0:
            out["missing_odds"] += 1
            continue

        for ver in versions:
            hid = hist[ver].get(race_key)
            if not hid:
                continue
            row = _v272_score_saved_history_same_odds(
                db_path, hid, race_key, ver, odds_maps, odds_run
            )
            out["rows"].append(row)
            if (not row.get("evaluated")) and "払戻" in str(row.get("reason") or ""):
                out["missing_payout"] += 1

    for ver in versions:
        vr = [r for r in out["rows"] if r.get("version") == ver and r.get("evaluated")]
        cost = sum(int(r.get("cost_yen",0) or 0) for r in vr)
        payout = sum(int(r.get("payout_yen",0) or 0) for r in vr)
        hits = sum(int(bool(r.get("hit"))) for r in vr)
        rate = (payout / cost * 100.0) if cost > 0 else None
        out["summary"].append({
            "version": ver,
            "対象R": len(vr),
            "的中R": hits,
            "投資円": cost,
            "払戻円": payout,
            "回収率": rate,
        })
    return out


def _v273_load_earliest_saved_odds(db_path: str, race_key: str) -> tuple[dict, dict]:
    empty = {'3tan': {}, '3fuku': {}, '2tansho': {}, '2fuku': {}, 'tansho': {}, 'fukusho': {}, 'wide': {}}
    try:
        _v221_ensure_odds_tables(db_path)
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            run = con.execute("""
                SELECT * FROM v221_odds_runs
                WHERE race_key=?
                ORDER BY datetime(created_at) ASC, rowid ASC
                LIMIT 1
            """, (str(race_key or ''),)).fetchone()
            if not run:
                return empty, {}
            rows = con.execute("""
                SELECT bet_key, combination, odds
                FROM v221_odds_values
                WHERE race_key=? AND snapshot_id=?
            """, (str(race_key or ''), str(run["snapshot_id"]))).fetchall()
        out = {k: {} for k in empty}
        for r in rows:
            k = str(r["bet_key"] or "")
            if k in out:
                try:
                    odd = float(r["odds"])
                    if odd > 0:
                        out[k][str(r["combination"])] = odd
                except Exception:
                    pass
        return out, dict(run)
    except Exception:
        return empty, {}



def _v276_virtual_all_odds_candidates(
    bets: dict,
    trials: int,
    odds_maps: dict,
) -> list[dict]:
    """
    過去バックテスト専用。
    保存オッズにある全対応券種
    3連単 / 3連複 / 2連単 / 2連複 / 単勝 / ワイド
    を同じ基準でモデル確率・単体期待値へ変換する。
    """
    tri_counter=(bets or {}).get("三連単",{}) or {}
    if not tri_counter or int(trials or 0)<=0:
        return []

    outcomes=[]
    for combo,count in tri_counter.items():
        vals=tuple(int(v) for v in (tuple(combo) if isinstance(combo,(tuple,list)) else (combo,)))
        if len(vals)==3:
            outcomes.append((vals,float(count)/max(int(trials),1)*100.0))
    if not outcomes:
        return []

    probs={
        "三連単":{},
        "三連複":{},
        "2連単":{},
        "2連複":{},
        "単勝":{},
        "ワイド":{},
    }
    for (a,b,c),prob in outcomes:
        k3=f"{a}-{b}-{c}"
        probs["三連単"][k3]=probs["三連単"].get(k3,0.0)+prob

        k3f="-".join(map(str,sorted((a,b,c))))
        probs["三連複"][k3f]=probs["三連複"].get(k3f,0.0)+prob

        k2=f"{a}-{b}"
        probs["2連単"][k2]=probs["2連単"].get(k2,0.0)+prob

        k2f="-".join(map(str,sorted((a,b))))
        probs["2連複"][k2f]=probs["2連複"].get(k2f,0.0)+prob

        probs["単勝"][str(a)]=probs["単勝"].get(str(a),0.0)+prob

        for x,y in ((a,b),(a,c),(b,c)):
            kw="-".join(map(str,sorted((x,y))))
            probs["ワイド"][kw]=probs["ワイド"].get(kw,0.0)+prob

    key_map={
        "三連単":"3tan",
        "三連複":"3fuku",
        "2連単":"2tansho",
        "2連複":"2fuku",
        "単勝":"tansho",
        "ワイド":"wide",
    }

    rows=[]
    for typ,odd_key in key_map.items():
        for combo,odds in (odds_maps.get(odd_key,{}) or {}).items():
            try:
                odd=float(odds)
                combo_text=str(combo).strip()
                if typ in ("三連複","2連複","ワイド"):
                    nums=[int(x) for x in re.findall(r"\d+",combo_text)]
                    if nums:
                        combo_text="-".join(map(str,sorted(nums)))
                prob=float(probs[typ].get(combo_text,0.0))
            except Exception:
                continue
            if odd<=0 or prob<=0:
                continue
            ev=prob/100.0*odd*100.0
            rows.append({
                "type":typ,
                "combo":combo_text,
                "probability":prob,
                "odds":odd,
                "ev":ev,
            })

    rows.sort(
        key=lambda r:(float(r.get("ev",0)),float(r.get("probability",0))),
        reverse=True,
    )
    return rows


def _v276_virtual_extra_all_odds_score(
    db_path: str,
    race_key: str,
    bets: dict,
    trials: int,
    odds_maps: dict,
    base_result: dict,
) -> dict:
    """
    過去バックテスト専用。
    既存4券種合成プランを基準に、保存オッズに存在する全対応券種から、
    追加後の合成モデル期待回収率が上昇する候補だけを仮想追加する。
    現在レースの候補表示・実購入処理には使用しない。
    """
    out={
        "available":False,"points":0,"cost_yen":0,"payout_yen":0,"hits":0,
        "rows":[],"base_model_return_rate":None,"combined_model_return_rate":None,
        "reason":"",
    }

    try:
        base_n=int((base_result or {}).get("points",0) or 0)
        base_rate=float((base_result or {}).get("model_return_rate"))
    except Exception:
        base_n=0
        base_rate=None
    if base_n<=0 or base_rate is None or not np.isfinite(base_rate):
        out["reason"]="基準プランのモデル期待回収率なし"
        return out

    candidates=_v276_virtual_all_odds_candidates(bets,int(trials or 0),odds_maps or {})
    if not candidates:
        out["reason"]="保存オッズから仮想候補を作れませんでした"
        return out

    # 既存プランにすでに含まれる券は追加対象から除外。
    base_ids=set()
    for t in (base_result or {}).get("tickets",[]) or []:
        typ=str(t.get("type") or "")
        combo=str(t.get("combo") or "").strip()
        if typ in ("三連複","2連複","ワイド"):
            nums=[int(x) for x in re.findall(r"\d+",combo)]
            if nums:
                combo="-".join(map(str,sorted(nums)))
        base_ids.add((typ,combo))

    base_cost=float(base_n*100)
    expected_payout=base_cost*base_rate/100.0
    current_cost=base_cost
    current_rate=base_rate
    selected=[]

    for cand in candidates:
        ident=(str(cand.get("type") or ""),str(cand.get("combo") or ""))
        if ident in base_ids:
            continue
        try:
            ev=float(cand.get("ev",0) or 0)
        except Exception:
            continue
        if ev<=0:
            continue
        next_expected=expected_payout+ev
        next_cost=current_cost+100.0
        next_rate=next_expected/next_cost*100.0 if next_cost>0 else 0.0
        # 追加後の合成モデル期待回収率が上がるものだけ。
        if next_rate>current_rate+1e-9:
            selected.append(dict(cand))
            expected_payout=next_expected
            current_cost=next_cost
            current_rate=next_rate

    if not selected:
        out["reason"]="全券種を比較したが合成モデル期待回収率を上げる追加候補なし"
        return out

    # 候補選択後にだけ実払戻で採点する。
    try:
        with sqlite3.connect(str(db_path),timeout=30.0) as con:
            con.row_factory=sqlite3.Row
            payout_rows=con.execute("""
                SELECT bet_type, combination, payout_yen
                FROM result_payouts
                WHERE race_key=?
            """,(str(race_key or ""),)).fetchall()
        payout_map={}
        for r in payout_rows:
            typ=_v212_norm_bet_type(str(r["bet_type"] or ""))
            combo=_v187_norm_combo(str(r["bet_type"] or ""),str(r["combination"] or ""))
            payout_map[(typ,combo)]=int(r["payout_yen"] or 0)
    except Exception as exc:
        out["reason"]=f"払戻読込失敗: {type(exc).__name__}: {exc}"
        return out

    actual_payout=0
    hit_count=0
    detail=[]
    for cand in selected:
        typ=str(cand.get("type") or "")
        combo=str(cand.get("combo") or "").strip()
        norm_typ=_v212_norm_bet_type(typ)
        norm_combo=_v187_norm_combo(typ,combo)
        payout=int(payout_map.get((norm_typ,norm_combo),0) or 0)
        hit=bool(payout>0)
        actual_payout+=payout
        hit_count+=int(hit)
        detail.append({
            "type":typ,
            "combo":combo,
            "model_probability":float(cand.get("probability",0) or 0),
            "saved_odds":float(cand.get("odds",0) or 0),
            "model_ev":float(cand.get("ev",0) or 0),
            "hit":hit,
            "payout_yen":payout,
        })

    out.update({
        "available":True,
        "points":len(selected),
        "cost_yen":len(selected)*100,
        "payout_yen":int(actual_payout),
        "hits":int(hit_count),
        "rows":detail,
        "base_model_return_rate":float(base_rate),
        "combined_model_return_rate":float(current_rate),
        "reason":"全対応券種から合成モデル期待回収率が上がる候補だけ仮想追加",
    })
    return out



def _v273_virtual_roi_score(
    db_path: str,
    race_key: str,
    bets: dict,
    trials: int,
    meta: dict,
    app_version: str,
) -> dict:
    """
    保存済みの最古オッズ + 登録済み払戻で、再シミュレーション結果を仮想採点する。
    1点100円均等。オッズ未保存/払戻未登録なら対象外。
    """
    out = {
        "available": False, "evaluated": False, "race_key": str(race_key or ""),
        "cost_yen": 0, "payout_yen": 0, "return_rate": None,
        "points": 0, "hit": False, "odds_snapshot_id": "", "odds_created_at": "",
        "reason": "",
        "virtual_extra_points":0,
        "virtual_extra_cost_yen":0,
        "virtual_extra_payout_yen":0,
        "virtual_extra_hits":0,
        "virtual_extra_rows":[],
        "virtual_combined_cost_yen":0,
        "virtual_combined_payout_yen":0,
        "virtual_combined_return_rate":None,
        "virtual_combined_model_return_rate":None,
    }
    if not race_key:
        out["reason"] = "race_keyなし"
        return out

    odds_maps, odds_run = _v273_load_earliest_saved_odds(db_path, race_key)
    if sum(len(v) for v in odds_maps.values()) <= 0:
        out["reason"] = "保存オッズなし"
        return out

    try:
        result = v184_eight_car_mixed_plan(bets, int(trials or 0), meta or {}, odds_maps)
    except Exception as exc:
        out["reason"] = f"プラン生成失敗: {type(exc).__name__}: {exc}"
        return out

    if not isinstance(result, dict) or not result.get("available"):
        out["reason"] = str((result or {}).get("reason") or "回収率プラン生成不可")
        return out

    out["available"] = True
    out["odds_snapshot_id"] = str(odds_run.get("snapshot_id") or "")
    out["odds_created_at"] = str(odds_run.get("created_at") or "")
    try:
        plan_hash = _v187_save_mixed_plan(
            db_path, str(race_key), result, app_version=str(app_version or APP_VERSION)
        )
        if not plan_hash:
            out["reason"] = "プラン保存失敗"
            return out
        _v212_recalculate_plan_feedback(db_path, str(race_key), str(plan_hash))

        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("""
                SELECT r.points, r.cost_yen,
                       f.hit, f.payout_yen, f.return_rate
                FROM v187_mixed_plan_runs r
                LEFT JOIN v187_mixed_plan_feedback f
                  ON f.race_key=r.race_key AND f.plan_hash=r.plan_hash
                WHERE r.race_key=? AND r.plan_hash=?
                LIMIT 1
            """, (str(race_key), str(plan_hash))).fetchone()

        if row is None:
            out["reason"] = "採点レコードなし"
            return out

        out["points"] = int(row["points"] or 0)
        out["cost_yen"] = int(row["cost_yen"] or out["points"] * 100)
        if row["return_rate"] is None:
            out["reason"] = "払戻未登録"
            return out

        out["evaluated"] = True
        out["hit"] = bool(int(row["hit"] or 0))
        out["payout_yen"] = int(row["payout_yen"] or 0)
        out["return_rate"] = float(row["return_rate"] or 0.0)
        out["reason"] = "採点済み"
        return out
    except Exception as exc:
        out["reason"] = f"採点失敗: {type(exc).__name__}: {exc}"
        return out



# Ver278: Streamlit画面を占有しない再シミュレーション用バックグラウンドジョブ。
# 状態はst.session_stateではなくSQLiteへ保存するため、画面移動・再描画後も確認できる。
_V278_BG_THREADS = globals().get("_V278_BG_THREADS", {})
_V278_BG_LOCK = globals().get("_V278_BG_LOCK", threading.Lock())

def _v278_bg_ensure_table(db_path: str) -> None:
    with sqlite3.connect(str(db_path), timeout=30.0) as con:
        con.execute("PRAGMA busy_timeout=30000")
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS v278_background_jobs(
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                app_version TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT,
                finished_at TEXT,
                limit_count INTEGER NOT NULL DEFAULT 0,
                force_current INTEGER NOT NULL DEFAULT 0,
                done_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                current_label TEXT DEFAULT '',
                message TEXT DEFAULT '',
                result_blob BLOB,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                error_text TEXT DEFAULT ''
            )
        """)
        try:
            cols={str(r[1]) for r in con.execute("PRAGMA table_info(v278_background_jobs)").fetchall()}
            if "pause_requested" not in cols:
                con.execute("ALTER TABLE v278_background_jobs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        con.commit()

def _v278_bg_update(db_path: str, job_id: int, **fields) -> None:
    if not fields:
        return
    _v278_bg_ensure_table(db_path)
    allowed={
        "status","started_at","updated_at","finished_at","done_count","total_count",
        "current_label","message","result_blob","cancel_requested","pause_requested","error_text"
    }
    fields={k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"]=_v228_now_jst_iso()
    keys=list(fields.keys())
    sql="UPDATE v278_background_jobs SET "+",".join(f"{k}=?" for k in keys)+" WHERE job_id=?"
    vals=[fields[k] for k in keys]+[int(job_id)]
    with sqlite3.connect(str(db_path), timeout=30.0) as con:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute(sql, vals)
        con.commit()

def _v278_bg_get_job(db_path: str, job_id: int | None = None) -> dict:
    try:
        _v278_bg_ensure_table(db_path)
        with sqlite3.connect(str(db_path), timeout=30.0) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=30000")
            if job_id:
                row=con.execute(
                    "SELECT * FROM v278_background_jobs WHERE job_id=?",
                    (int(job_id),)
                ).fetchone()
            else:
                row=con.execute("""
                    SELECT * FROM v278_background_jobs
                    WHERE status IN ('queued','running','pause_requested','paused','cancel_requested')
                      AND job_type IN ('batch_rerun','official_import','normal_prediction')
                    ORDER BY job_id DESC LIMIT 1
                """).fetchone()
                if not row:
                    row=con.execute("""
                        SELECT * FROM v278_background_jobs
                        WHERE job_type IN ('batch_rerun','official_import')
                        ORDER BY job_id DESC LIMIT 1
                    """).fetchone()
        if not row:
            return {}
        d=dict(row)
        blob=d.get("result_blob")
        if blob:
            try:
                d["result"]=pickle.loads(zlib.decompress(bytes(blob)))
            except Exception:
                d["result"]={}
        else:
            d["result"]={}
        return d
    except Exception:
        return {}

def _v278_bg_has_running_job(db_path: str) -> bool:
    try:
        _v278_bg_ensure_table(db_path)
        with sqlite3.connect(str(db_path), timeout=30.0) as con:
            con.execute("PRAGMA busy_timeout=30000")
            row=con.execute("""
                SELECT COUNT(*) FROM v278_background_jobs
                WHERE job_type IN ('batch_rerun','official_import') AND status IN ('queued','running','pause_requested','paused','cancel_requested')
            """).fetchone()
        return bool(int(row[0] or 0))
    except Exception:
        return False

def _v278_bg_cancel_requested(db_path: str, job_id: int) -> bool:
    try:
        with sqlite3.connect(str(db_path), timeout=30.0) as con:
            con.execute("PRAGMA busy_timeout=30000")
            row=con.execute(
                "SELECT cancel_requested FROM v278_background_jobs WHERE job_id=?",
                (int(job_id),)
            ).fetchone()
        return bool(row and int(row[0] or 0))
    except Exception:
        return False

def _v278_bg_request_cancel(db_path: str, job_id: int) -> None:
    _v278_bg_update(
        db_path, job_id,
        status="cancel_requested",
        cancel_requested=1,
        message="現在のレース処理が終わり次第停止します。"
    )

def _v278_bg_pause_requested(db_path: str, job_id: int) -> bool:
    try:
        with sqlite3.connect(str(db_path), timeout=30.0) as con:
            con.execute("PRAGMA busy_timeout=30000")
            row=con.execute(
                "SELECT pause_requested FROM v278_background_jobs WHERE job_id=?",
                (int(job_id),)
            ).fetchone()
        return bool(row and int(row[0] or 0))
    except Exception:
        return False

def _v278_bg_set_pause(db_path: str, job_id: int, paused: bool, message: str = "") -> None:
    _v278_bg_update(
        db_path, job_id,
        pause_requested=1 if paused else 0,
        status="pause_requested" if paused else "running",
        message=message or ("他のDB登録を待っています。" if paused else "再シミュレーションを再開しました。"),
    )

def _v278_bg_pause_loop(db_path: str, job_id: int) -> None:
    if not _v278_bg_pause_requested(db_path, job_id):
        return
    _v278_bg_update(
        db_path, job_id,
        status="paused",
        message="選手履歴・結果などのDB登録を優先するため一時停止中です。"
    )
    while _v278_bg_pause_requested(db_path, job_id):
        if _v278_bg_cancel_requested(db_path, job_id):
            return
        time_module.sleep(0.25)
    _v278_bg_update(db_path, job_id, status="running", message="バックグラウンド再シミュレーションを再開しました。")

def _v278_pause_background_for_foreground(db_path: str, timeout_sec: float = 180.0) -> dict:
    """DB登録前にバックグラウンド処理をレース境界で止める。

    Ver314:
    - 最大180秒まで「paused」を待つ
    - タイムアウトしても pause 要求は残したまま登録を続行可能にする
      （再シム側は現在レース終了後に止まる。登録を拒否しない）
    """
    job=_v278_bg_get_job(db_path)
    if not job or str(job.get("status") or "") not in ("queued","running","pause_requested","paused"):
        return {"paused":False,"job_id":0,"reason":"no_running_job"}
    job_id=int(job.get("job_id") or 0)
    _v278_bg_set_pause(db_path,job_id,True,"DB登録を優先するため、現在レース終了後に一時停止します。")
    deadline=time_module.time()+float(timeout_sec)
    while time_module.time()<deadline:
        cur=_v278_bg_get_job(db_path,job_id)
        status=str(cur.get("status") or "")
        if status=="paused":
            return {"paused":True,"job_id":job_id}
        if status in ("completed","cancelled","failed"):
            return {"paused":False,"job_id":job_id,"reason":status}
        time_module.sleep(0.25)
    # タイムアウト: 拒否せず続行。pause要求は有効のまま（次のレース境界で停止）
    return {
        "paused": False,
        "job_id": job_id,
        "reason": "timeout_proceed",
        "pause_requested": True,
    }

def _v278_resume_background_after_foreground(db_path: str, pause_info: dict) -> None:
    try:
        if pause_info and pause_info.get("job_id"):
            _v278_bg_set_pause(db_path,int(pause_info["job_id"]),False,"DB登録完了。再シミュレーションを再開します。")
    except Exception:
        pass


class _V276ForegroundPredictionPriority:
    """通常予測中はバックグラウンド再シミュレーションへ一時停止要求を出す。

    停止完了を待たないため通常予測の開始は遅らせない。バックグラウンド側は
    現在レース終了後の境界で停止し、通常予測終了後に自動再開する。
    """
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.job_id = 0

    def __enter__(self):
        try:
            job = _v278_bg_get_job(self.db_path)
            if job and str(job.get("status") or "") in ("queued", "running", "pause_requested", "paused"):
                self.job_id = int(job.get("job_id") or 0)
                if self.job_id:
                    _v278_bg_set_pause(
                        self.db_path, self.job_id, True,
                        "通常予測を優先するため、現在レース終了後に一時停止します。"
                    )
        except Exception:
            self.job_id = 0
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.job_id:
                _v278_bg_set_pause(
                    self.db_path, self.job_id, False,
                    "通常予測完了。バックグラウンド再シミュレーションを再開します。"
                )
        except Exception:
            pass
        return False

def _v276_send_rerun_complete_notification(result: dict) -> None:
    """バックグラウンド再シミュレーション正常完了時だけntfyへ即時通知する。

    通知失敗は再シミュレーション結果へ影響させない。
    通知先は既存の一般予定通知と同じ既定トピック notify を使用し、
    Streamlit Cloud等では AUTORACE_NTFY_TOPIC 環境変数で上書きできる。
    """
    try:
        topic = str(os.environ.get("AUTORACE_NTFY_TOPIC", "notify") or "notify").strip()
        if not topic or any(ch in topic for ch in "/?# "):
            return
        rerun = int((result or {}).get("rerun") or 0)
        checked = int((result or {}).get("checked") or 0)
        message = f"再シミュレーションが完了しました。再計算 {rerun}R / 確認 {checked}R"
        payload = {
            "topic": topic,
            "title": "AutoRaceAI 再シミュレーション完了",
            "message": message,
            "priority": 4,
            "tags": ["bell"],
        }
        request = urllib.request.Request(
            "https://ntfy.sh",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="strict"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except Exception:
        # 通知経路の一時障害で、完了済みジョブをfailed扱いにしない。
        pass



def _v284_backfill_transition_audit_from_latest_histories(db_path: str, app_version: str = "") -> dict:
    """最新予測payloadに既に入っているVer284展開監査をDB監査3表へ復元する。
    再シミュレーションは行わない。batch保存経路の保険として完了時に必ず実行する。
    """
    result={"ok":False,"checked":0,"saved_races":0,"errors":[]}
    ver=str(app_version or globals().get("_V231_APP_VERSION") or "Ver284")
    try:
        _v284_ensure_transition_audit_tables(db_path)
        with sqlite3.connect(str(db_path),timeout=60.0) as con:
            con.execute("PRAGMA busy_timeout=60000")
            rows=con.execute("""
                SELECT h.race_key,h.prediction_time,h.trials,h.payload
                FROM v231_prediction_history h
                JOIN (
                    SELECT race_key,MAX(history_id) AS mid
                    FROM v231_prediction_history
                    WHERE app_version=?
                    GROUP BY race_key
                ) x ON h.history_id=x.mid
                ORDER BY h.race_key
            """,(ver,)).fetchall()
        for race_key,prediction_time,trials,payload in rows:
            result["checked"]+=1
            try:
                obj=pickle.loads(zlib.decompress(payload))
                meta=(obj or {}).get("meta") or {}
                wall=meta.get("6周展開シミュレーション") or meta.get("壁補正監査") or {}
                detail=wall.get("v284_transition_audit") or {}
                if not detail:
                    continue
                # 既にこのrace_keyの最新監査が6周あるなら重複保存しない。
                with sqlite3.connect(str(db_path),timeout=30.0) as con:
                    n=int(con.execute(
                        "SELECT COUNT(DISTINCT lap_no) FROM v284_transition_audit WHERE race_key=? AND app_version=?",
                        (str(race_key),ver)
                    ).fetchone()[0] or 0)
                if n>=6:
                    result["saved_races"]+=1
                    continue
                save=_v284_save_transition_audit(db_path,str(race_key),wall)
                if save.get("ok"):
                    result["saved_races"]+=1
                elif len(result["errors"])<8:
                    result["errors"].append(f"{race_key}: {save.get('reason','保存失敗')}")
            except Exception as exc:
                if len(result["errors"])<8:
                    result["errors"].append(f"{race_key}: {type(exc).__name__}: {exc}")
        result["ok"]=(len(result["errors"])==0)
        return result
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result


def _v278_bg_worker(db_path: str, job_id: int, limit_count: int, force_current: bool, resume_done_offset: int = 0, resume_total_target: int = 0, diagnostic_auto: bool = False) -> None:
    try:
        _v278_bg_update(
            db_path, job_id,
            status="running",
            started_at=_v228_now_jst_iso(),
            message="対象レースを整理しています。"
        )
        # Ver314: 一定レースごとにGitHubへ自動保存（クラッシュ時の消失を防ぐ）
        _autosave_every = int(globals().get("_V314_AUTOSAVE_EVERY_N") or 15)
        _last_gh_save_at = [0]  # list for closure mutability
        _gh_autosave_log = []

        def _progress(done,total,label):
            _done_raw=int(done or 0)
            _total_raw=int(total or 0)
            _done_show=int(resume_done_offset or 0)+_done_raw
            _total_show=int(resume_total_target or 0)
            if _total_show <= 0:
                _total_show=int(resume_done_offset or 0)+_total_raw
            _total_show=max(_total_show,_done_show)
            _msg = f"{_done_show}/{_total_show}｜{str(label or '')}"
            # 途中自動保存: N件処理ごと（例: 15, 30, 45...）
            if (
                _autosave_every > 0
                and _done_show > 0
                and (_done_show - int(_last_gh_save_at[0])) >= _autosave_every
            ):
                try:
                    _v278_bg_update(
                        db_path, job_id,
                        done_count=_done_show,
                        total_count=_total_show,
                        current_label=str(label or ""),
                        message=_msg + f"｜GitHub自動保存中({_done_show}R)...",
                    )
                    _ok_as, _msg_as = push_db_to_github(
                        f"AutoRaceAI: 再シム途中自動保存 {_done_show}/{_total_show}",
                        _allow_during_resimulation=True,
                        _lightweight=True,
                    )
                    try:
                        import gc as _gc_as
                        _gc_as.collect()
                    except Exception:
                        pass
                    if _ok_as:
                        _last_gh_save_at[0] = int(_done_show)
                        _gh_autosave_log.append({
                            "at": int(_done_show),
                            "ok": True,
                            "message": str(_msg_as or "")[:200],
                        })
                        _msg = _msg + f"｜✓自動保存済({_done_show}R)"
                    else:
                        _gh_autosave_log.append({
                            "at": int(_done_show),
                            "ok": False,
                            "message": str(_msg_as or "")[:200],
                        })
                        _msg = _msg + "｜⚠自動保存失敗(後で再試行)"
                except Exception as _as_exc:
                    _gh_autosave_log.append({
                        "at": int(_done_show),
                        "ok": False,
                        "message": f"{type(_as_exc).__name__}: {_as_exc}"[:200],
                    })
                    _msg = _msg + "｜⚠自動保存エラー"
            _v278_bg_update(
                db_path, job_id,
                done_count=_done_show,
                total_count=_total_show,
                current_label=str(label or ""),
                message=_msg,
            )

        result=_v262_batch_rerun_saved_histories(
            db_path,
            int(limit_count),
            _progress,
            force_current=bool(force_current),
            diagnostic_auto=bool(diagnostic_auto),
            cancel_cb=lambda: _v278_bg_cancel_requested(db_path,job_id),
            pause_cb=lambda: _v278_bg_pause_loop(db_path,job_id),
        )
        try:
            result["github_autosave_count"] = sum(1 for x in _gh_autosave_log if x.get("ok"))
            result["github_autosave_fail_count"] = sum(1 for x in _gh_autosave_log if not x.get("ok"))
            result["github_autosave_log"] = list(_gh_autosave_log)
            result["github_autosave_every_n"] = int(_autosave_every)
        except Exception:
            pass
        # Ver284 hotfix: 各レースの最新payloadには監査集計が保存されているため、
        # batch終了時（停止時を含む）に必ず監査3表へ同期する。
        _audit_backfill284=_v284_backfill_transition_audit_from_latest_histories(
            db_path,str(globals().get("_V231_APP_VERSION") or "Ver284")
        )
        result["v284_audit_backfill"]=_audit_backfill284
        result["v284_transition_audit_saved_races"]=int(_audit_backfill284.get("saved_races") or 0)
        cancelled=bool(result.get("cancelled")) or _v278_bg_cancel_requested(db_path,job_id)
        blob=zlib.compress(pickle.dumps(result,protocol=pickle.HIGHEST_PROTOCOL),level=6)
        _final_done278=int(resume_done_offset or 0)+int(result.get("checked",0) or 0)
        _final_total278=int(resume_done_offset or 0)+int(result.get("candidate_total",result.get("checked",0

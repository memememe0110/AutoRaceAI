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
APP_VERSION = "Ver272"
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

def _v248_wall_calibration(db_path: str | None = None) -> dict:
    path = str(db_path or _v230_db_path())
    try:
        stamp = (path, int(Path(path).stat().st_mtime_ns))
    except Exception:
        stamp = (path, 0)
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
        data = data.sort_values(["race_date", "race_no", "race_key", "lap"])
        race_order = data[["race_key", "race_date", "race_no"]].drop_duplicates().sort_values(["race_date", "race_no", "race_key"])
        if len(race_order) < 40 or len(data) < 1200:
            result["sample_transitions"] = int(len(data))
            result["reason"] = "時系列検証に必要な周回履歴が不足"
            _V248_WALL_CALIBRATION_CACHE.clear(); _V248_WALL_CALIBRATION_CACHE[stamp] = dict(result); return result
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

def _v273_load_earliest_saved_odds(db_path: str, race_key: str) -> tuple[dict, dict]:
    empty = {'3tan': {}, '3fuku': {}, '2tansho': {}, '2fuku': {}, 'tansho': {}, 'wide': {}}
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


def _v262_batch_rerun_saved_histories(db_path: str, limit: int = 120, progress_cb=None) -> dict:
    out={
        "checked":0,"rerun":0,"skipped_current":0,"no_text":0,"errors":[],"labels":[],
        "roi_evaluated":0,"roi_no_odds":0,"roi_no_payout":0,
        "roi_cost_yen":0,"roi_payout_yen":0,"roi_hits":0,"roi_rows":[],
    }
    histories=_v231_list_prediction_histories(db_path,max(1,int(limit)))
    # 同じレースの旧バージョンが複数あっても、入力復元元は最新1件だけ使う。
    unique=[]; seen=set()
    for h in histories:
        rk=str(h.get('race_key') or '').strip()
        if not rk or rk in seen:
            continue
        seen.add(rk); unique.append(h)
    # Ver271: 本来の時系列順で再シミュレーションする。
    # 各保存履歴から開催日・場・Rを読み、古いレース→新しいレースへ並べる。
    chronological=[]
    for h in unique:
        try:
            _v, _raw, _vo, _hm = _v231_load_prediction_history(db_path, int(h.get('history_id') or 0))
            _mm = (_v or {}).get('meta') or {}
            _date = str(_mm.get('開催日') or _mm.get('日付') or _mm.get('race_date') or _mm.get('date') or '')[:10]
            _venue = str(_mm.get('開催場') or _mm.get('場') or _mm.get('venue') or _mm.get('track') or '')
            _rraw = str(_mm.get('R') or _mm.get('レース') or _mm.get('レース番号') or _mm.get('race_no') or _mm.get('race') or '')
            _rm = re.search(r'\d+', _rraw)
            _rno = int(_rm.group()) if _rm else 999
            chronological.append((_date, _venue, _rno, int(h.get('history_id') or 0), h))
        except Exception:
            chronological.append(('9999-99-99','',999,int(h.get('history_id') or 0),h))
    chronological.sort(key=lambda x:(x[0],x[1],x[2],x[3]))
    unique=[x[4] for x in chronological]

    total=len(unique)
    current_ver=str(globals().get('_V231_APP_VERSION') or 'Ver271')
    # 現行Verで「予測履歴＋6周スナップショット」まで揃っているレースだけスキップする。
    # 履歴だけ存在して周回保存が欠けている場合は、一括再シミュレーションで自動修復する。
    current_keys=set()
    current_complete_keys=set()
    try:
        _v231_ensure_prediction_history_table(db_path)
        with sqlite3.connect(str(db_path)) as con:
            _v252_ensure_lap_tables(con)
            rows=con.execute("SELECT DISTINCT race_key FROM v231_prediction_history WHERE app_version=?",(current_ver,)).fetchall()
            current_keys={str(r[0]) for r in rows if r and r[0]}
            # race_keyの表記と周回テーブルのキーが完全一致しない旧データもあるため、
            # 履歴を読み込んで日付・場・Rで6周保存済みか確認する。
            cur_hist=_v231_list_prediction_histories(db_path,10000)
            for ch in cur_hist:
                if str(ch.get('app_version') or '') != current_ver:
                    continue
                try:
                    cv,_,_,cm=_v231_load_prediction_history(db_path,int(ch.get('history_id') or 0))
                    mm=(cv or {}).get('meta') or {}
                    cd=str(mm.get('開催日') or mm.get('日付') or mm.get('race_date') or mm.get('date') or '')[:10]
                    cvn=str(mm.get('開催場') or mm.get('場') or mm.get('venue') or mm.get('track') or '').strip()
                    cr=str(mm.get('R') or mm.get('レース') or mm.get('レース番号') or mm.get('race_no') or mm.get('race') or '').strip()
                    cr=re.sub(r'[^0-9]','',cr) or cr.replace('R','').replace('r','').strip()
                    if cd and cvn and cr:
                        n=int(con.execute("SELECT COUNT(*) FROM v252_lap_prediction_snapshots WHERE race_date=? AND venue=? AND race_no=? AND app_version=?",(cd,cvn,cr,current_ver)).fetchone()[0] or 0)
                        if n>=6:
                            current_complete_keys.add(str(ch.get('race_key') or ''))
                except Exception:
                    pass
    except Exception:
        current_keys=set(); current_complete_keys=set()
    for idx,h in enumerate(unique,1):
        out['checked']+=1
        label=str(h.get('race_label') or h.get('race_key') or f'履歴{idx}')
        try:
            if callable(progress_cb):
                progress_cb(idx-1,total,label)
            race_key0=str(h.get('race_key') or '').strip()
            if race_key0 in current_complete_keys:
                out['skipped_current']+=1
                continue
            view, raw_text, venue_override, hm=_v231_load_prediction_history(db_path,int(h.get('history_id') or 0))
            if not view or not str(raw_text or '').strip():
                out['no_text']+=1
                continue
            src_ver=str((hm or {}).get('app_version') or h.get('app_version') or view.get('app_version') or 'Unknown')
            trials=int(h.get('trials') or view.get('trials') or 20000)
            seed=int(h.get('seed') or view.get('seed') or 42)
            excluded=[int(x) for x in (view.get('excluded') or [])]
            prediction_text=str(raw_text)
            if str(venue_override or '').strip():
                prediction_text=f"開催場: {str(venue_override).strip()}\n"+prediction_text
            df,bets,output,entries,meta=engine.ver16_run_prediction(prediction_text,trials,seed,manual_excluded=excluded)
            meta=dict(meta or {})
            df,bets,wall_audit=_v230_six_lap_simulation(df,bets,entries,meta,trials,seed)
            meta['壁補正監査']=wall_audit
            meta['6周展開シミュレーション']=wall_audit
            finish_prob=engine.v30_finish_probabilities(df,bets,trials)
            df=engine.v196_apply_probability_aligned_ranks(df,finish_prob)
            race_key=engine.v34_save_prediction_snapshot(meta,df,finish_prob,engine.DB_PATH)
            prediction_view={
                'df':df,'bets':bets,'output':output,'entries':entries,'meta':meta,
                'finish_prob':finish_prob,'race_key':race_key,'trials':trials,'excluded':excluded,
                'learning_boundary':{},'future_audit':{},'day_trend':{},'prediction_timing':{},
                'app_version':current_ver,'simulation_mode':_V231_SIMULATION_MODE,
                'settings_hash':_v231_settings_hash(trials,seed,excluded),
                'prediction_time':_v228_now_jst_iso(),'seed':seed,
                'rerun_from_restored':True,'rerun_source_version':src_ver,
                'rerun_source_history_id':int(h.get('history_id') or 0),
                'batch_rerun':True,'walk_forward_v269':True,
            }
            hid=_v231_save_prediction_history(db_path,race_key,raw_text,venue_override,prediction_view,trials,seed)
            _v222_save_prediction_restore(db_path,race_key,raw_text,venue_override,prediction_view)
            # 一括再シミュレーションした予測は、同じ現行Verで1〜6周の代表隊列も必ず保存する。
            snap=_v260_snapshot_from_saved_view(db_path,prediction_view,current_ver)
            if not snap.get('ok'):
                raise RuntimeError(f"周回スナップショット保存失敗: {snap.get('reason','不明')}")

            # Ver272: 過去レースの仮想100円均等・回収率採点。
            # 保存済み最古オッズを全バージョン共通で使い、結果/払戻は採点にだけ使用する。
            _roi273=_v273_virtual_roi_score(
                db_path, race_key, bets, trials, meta, current_ver
            )
            if _roi273.get("evaluated"):
                out["roi_evaluated"]+=1
                out["roi_cost_yen"]+=int(_roi273.get("cost_yen",0) or 0)
                out["roi_payout_yen"]+=int(_roi273.get("payout_yen",0) or 0)
                out["roi_hits"]+=int(bool(_roi273.get("hit")))
            else:
                _reason273=str(_roi273.get("reason") or "")
                if "オッズ" in _reason273:
                    out["roi_no_odds"]+=1
                elif "払戻" in _reason273:
                    out["roi_no_payout"]+=1
            out["roi_rows"].append({
                "race": label,
                "race_key": race_key,
                "evaluated": bool(_roi273.get("evaluated")),
                "points": int(_roi273.get("points",0) or 0),
                "cost_yen": int(_roi273.get("cost_yen",0) or 0),
                "payout_yen": int(_roi273.get("payout_yen",0) or 0),
                "return_rate": _roi273.get("return_rate"),
                "hit": bool(_roi273.get("hit")),
                "reason": str(_roi273.get("reason") or ""),
                "odds_created_at": str(_roi273.get("odds_created_at") or ""),
            })

            out['rerun']+=1
            out['labels'].append(f"{label} → {current_ver} (ID:{hid}, {int(snap.get('saved_laps',0) or 0)}周)")
            current_keys.add(str(race_key0 or race_key))
            current_complete_keys.add(str(race_key0 or race_key))
        except Exception as exc:
            out['errors'].append(f"{label}: {type(exc).__name__}: {exc}")
        finally:
            if callable(progress_cb):
                progress_cb(idx,total,label)
    if int(out.get("roi_cost_yen",0) or 0) > 0:
        out["roi_return_rate"]=float(out["roi_payout_yen"])/float(out["roi_cost_yen"])*100.0
    else:
        out["roi_return_rate"]=None
    out['message']=(f"確認{out['checked']}レース / {current_ver}再シミュレーション{out['rerun']} / "
                    f"現行版済み{out['skipped_current']} / 入力材料なし{out['no_text']} / エラー{len(out['errors'])}")
    return out

def _v252_lap_residual_calibration(db_path: str | None, venue: str, cutoff_date: str) -> dict:
    """予測代表隊列と後日判明した実測隊列の追越し差を時系列で縮小学習する。"""
    result={"enabled":False,"samples":0,"races":0,"delta":{},"reason":"予測・実測の周回ペア不足"}
    if not db_path or not Path(db_path).exists():
        return result
    try:
        stamp=(str(db_path),Path(db_path).stat().st_mtime_ns,str(venue),str(cutoff_date)[:10])
    except Exception:
        stamp=(str(db_path),str(venue),str(cutoff_date)[:10])
    if stamp in _V252_LAP_RESIDUAL_CACHE:
        return dict(_V252_LAP_RESIDUAL_CACHE[stamp])
    try:
        with sqlite3.connect(str(db_path)) as con:
            _v252_ensure_lap_tables(con)
            pred=pd.read_sql_query("""
                SELECT p.race_date,p.venue,p.race_no,p.lap_no,p.predicted_order,p.support,p.is_backtest,p.created_at
                FROM v252_lap_prediction_snapshots p
                WHERE p.is_backtest IN (0,2) AND p.app_version IN ('Ver253','Ver254','Ver256','Ver257','Ver259','Ver260')
                  AND (?='' OR p.venue=?)
                  AND (?='' OR substr(p.race_date,1,10)<substr(?,1,10))
                ORDER BY p.created_at
            """,con,params=(venue,venue,cutoff_date,cutoff_date))
            actual=pd.read_sql_query("""
                SELECT rr.race_date,rr.venue,REPLACE(COALESCE(rr.race_no,''),'R','') AS race_no,
                       rl.lap_no,rl.position,rl.car_no,
                       COALESCE(CAST(REPLACE(REPLACE(re.handicap,'m',''),'Ｍ','') AS INTEGER),0) AS handicap
                FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                LEFT JOIN races r ON r.race_key=rr.race_key
                LEFT JOIN race_entries re ON re.race_id=r.race_id AND re.car_no=rl.car_no
                WHERE COALESCE(rr.learning_eligible,1)=1
                  AND (?='' OR rr.venue=?)
                  AND (?='' OR substr(rr.race_date,1,10)<substr(?,1,10))
                ORDER BY rr.race_date,rr.venue,rr.race_no,rl.lap_no,rl.position
            """,con,params=(venue,venue,cutoff_date,cutoff_date))
    except Exception as exc:
        result['reason']=f'周回差分読込失敗: {exc}'
        return result
    if pred.empty or actual.empty:
        return result
    # 同一レース・周回は最新の予測だけ採用。
    pred=pred.sort_values('created_at').drop_duplicates(['race_date','venue','race_no','lap_no'],keep='last')
    amap={}
    hmaps={}
    for key,g in actual.groupby(['race_date','venue','race_no','lap_no'],dropna=False):
        gg=g.sort_values('position')
        amap[(str(key[0])[:10],str(key[1]),str(key[2]),int(key[3]))]=tuple(int(x) for x in gg.car_no.tolist())
        hmaps[(str(key[0])[:10],str(key[1]),str(key[2]),int(key[3]))]={int(r.car_no):int(r.handicap or 0) for r in gg.itertuples()}
    events=[]; race_keys=set()
    for row in pred.itertuples():
        key=(str(row.race_date)[:10],str(row.venue),str(row.race_no),int(row.lap_no))
        actual_order=amap.get(key)
        try: pred_order=tuple(int(x) for x in str(row.predicted_order).split('-') if str(x).strip())
        except Exception: continue
        if not actual_order or len(pred_order)<3: continue
        race_keys.add(key[:3]); hp=hmaps.get(key,{})
        # 各隣接ペアで「予測は抜く/実際は抜く」を比較する。
        # 前周の実測隊列を基準にするため、lap1は初期車列（ハンデ・車番順）を近似。
        prev_key=(key[0],key[1],key[2],key[3]-1)
        prev=amap.get(prev_key)
        if not prev:
            prev=tuple(sorted(actual_order,key=lambda c:(hp.get(c,0),c)))
        pp={c:i for i,c in enumerate(pred_order)}; ap={c:i for i,c in enumerate(actual_order)}
        for j in range(1,len(prev)):
            front,chaser=prev[j-1],prev[j]
            if front not in pp or chaser not in pp or front not in ap or chaser not in ap: continue
            pred_pass=int(pp[chaser]<pp[front]); actual_pass=int(ap[chaser]<ap[front])
            gap=max(0,hp.get(chaser,0)-hp.get(front,0))
            events.append((key[3],_v251_gap_bucket(gap),actual_pass-pred_pass,0.35 if int(getattr(row,'is_backtest',0) or 0)==2 else 1.0))
    if len(events)<60:
        result.update({'samples':len(events),'races':len(race_keys)})
        _V252_LAP_RESIDUAL_CACHE[stamp]=dict(result)
        return result
    ev=pd.DataFrame(events,columns=['lap','bucket','residual','weight'])
    deltas={}
    for (lap,bucket),g in ev.groupby(['lap','bucket']):
        n=len(g); effective_n=float(g['weight'].sum()); raw=float(np.average(g['residual'],weights=g['weight']))
        # 再構成データは0.35重み。正なら予測より実際の追越しが多い。
        shrunk=(effective_n/(effective_n+45.0))*raw
        deltas[f'{int(lap)}|{bucket}']={
            'delta':float(np.clip(shrunk*0.70,-0.24,0.24)),
            'raw_residual':raw,'n':int(n),'effective_n':round(effective_n,2)
        }
    result={'enabled':True,'samples':int(len(ev)),'races':int(len(race_keys)),
            'delta':deltas,'reason':'予測代表隊列と後日実測グランドノートの周回差を縮小学習'}
    _V252_LAP_RESIDUAL_CACHE.clear(); _V252_LAP_RESIDUAL_CACHE[stamp]=dict(result)
    return result


# Backward-compatible name used by the Ver253/Ver254 status panel.
# The implementation was renamed to *_calibration, but some UI code still called *_learning.
def _v252_lap_residual_learning(db_path: str | None, venue: str = "", cutoff_date: str = "") -> dict:
    return _v252_lap_residual_calibration(db_path, venue, cutoff_date)



# Ver257: Ver256の全実測周回学習に加え、race_entries→playersの選手名JOINを修正。
# 開催場×周回の実際の順位入替率を時系列で学習する。
# 少数開催場は全場平均へ縮小し、予測対象日以降の結果は混ぜない。
_V256_ACTUAL_LAP_CACHE = {}
_V256_SETTINGS_SYNC_CACHE = set()

def _v256_logit(p: float) -> float:
    p=max(0.02,min(0.98,float(p)))
    return float(np.log(p/(1.0-p)))

def _v256_actual_lap_calibration(db_path: str | None, venue: str = "", cutoff_date: str = "") -> dict:
    result={"enabled":False,"races":0,"pairs":0,"lap_delta":{},"rates":{},
            "reason":"実測グランドノート不足"}
    if not db_path or not Path(db_path).exists():
        return result
    try:
        stamp=(str(db_path),Path(db_path).stat().st_mtime_ns,str(venue),str(cutoff_date)[:10])
    except Exception:
        stamp=(str(db_path),str(venue),str(cutoff_date)[:10])
    if stamp in _V256_ACTUAL_LAP_CACHE:
        return dict(_V256_ACTUAL_LAP_CACHE[stamp])
    try:
        with sqlite3.connect(str(db_path)) as con:
            d=pd.read_sql_query("""
                SELECT substr(rr.race_date,1,10) AS race_date, rr.venue,
                       REPLACE(COALESCE(rr.race_no,''),'R','') AS race_no,
                       rl.lap_no, rl.position, rl.car_no
                FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                WHERE COALESCE(rr.learning_eligible,1)=1
                  AND rl.lap_no BETWEEN 1 AND 6
                  AND (?='' OR substr(rr.race_date,1,10)<substr(?,1,10))
                ORDER BY rr.race_date,rr.venue,rr.race_no,rl.lap_no,rl.position
            """,con,params=(cutoff_date,cutoff_date))
    except Exception as exc:
        result['reason']=f'全実測周回読込失敗: {exc}'
        return result
    if d.empty:
        return result
    events=[]; race_keys=set()
    for (race_date,v,race_no),g in d.groupby(['race_date','venue','race_no'],dropna=False):
        orders={int(l):tuple(int(x) for x in xg.sort_values('position').car_no.tolist())
                for l,xg in g.groupby('lap_no')}
        prev=None
        for lap in range(1,7):
            cur=orders.get(lap)
            if not cur: continue
            if prev and len(prev)>=3 and len(cur)>=3:
                pos={c:i for i,c in enumerate(cur)}
                inv=pairs=0
                for i in range(len(prev)-1):
                    front,chaser=prev[i],prev[i+1]
                    if front in pos and chaser in pos:
                        pairs+=1; inv += int(pos[chaser] < pos[front])
                if pairs:
                    events.append((str(v),int(lap),int(inv),int(pairs)))
                    race_keys.add((str(race_date),str(v),str(race_no)))
            prev=cur
    if not events:
        return result
    ev=pd.DataFrame(events,columns=['venue','lap','passes','pairs'])
    global_stats=ev.groupby('lap',as_index=False)[['passes','pairs']].sum()
    global_rate={int(r.lap):(float(r.passes)+3.0)/(float(r.pairs)+12.0) for r in global_stats.itertuples()}
    use=ev[ev['venue'].astype(str)==str(venue)] if venue else ev
    local_stats=use.groupby('lap',as_index=False)[['passes','pairs']].sum()
    local_map={int(r.lap):(float(r.passes),float(r.pairs)) for r in local_stats.itertuples()}
    deltas={}; rates={}
    for lap in range(2,7):
        gp=float(global_rate.get(lap,0.22))
        passes,pairs=local_map.get(lap,(0.0,0.0))
        raw=(passes+3.0)/(pairs+12.0)
        shrink=pairs/(pairs+180.0)
        blended=shrink*raw+(1.0-shrink)*gp
        # ロジット差の35%だけ使い、1要素で展開を支配しない。
        delta=float(np.clip((_v256_logit(blended)-_v256_logit(gp))*0.35,-0.16,0.16))
        deltas[int(lap)]=delta
        rates[int(lap)]={'local_rate':round(raw,5),'blended_rate':round(blended,5),
                         'global_rate':round(gp,5),'pairs':int(pairs),'delta':round(delta,5)}
    result={'enabled':int(ev['pairs'].sum())>=300,'races':len(race_keys),
            'pairs':int(use['pairs'].sum()),'lap_delta':deltas,'rates':rates,
            'reason':'実測グランドノート全体の開催場×周回入替率を全場平均へ縮小学習'}
    _V256_ACTUAL_LAP_CACHE[stamp]=dict(result)
    return result

def _v256_refresh_learning_settings(db_path: str | None) -> dict:
    """周回実測で直接測れる設定だけを更新。未検証項目を推測で動かさない。"""
    out={'updated':0,'races':0,'pairs':0}
    if not db_path or not Path(db_path).exists(): return out
    try:
        sig=(str(db_path),Path(db_path).stat().st_mtime_ns)
    except Exception:
        sig=(str(db_path),)
    if sig in _V256_SETTINGS_SYNC_CACHE: return out
    cal=_v256_actual_lap_calibration(db_path,"","")
    if not cal.get('enabled'): return out
    rates=cal.get('rates') or {}
    early=np.mean([float((rates.get(l) or {}).get('blended_rate',0.22)) for l in (2,3)])
    late=np.mean([float((rates.get(l) or {}).get('blended_rate',0.18)) for l in (5,6)])
    vals={
        '前団維持': float(np.clip(0.68*(1.0+(0.24-early)*0.45),0.52,0.82)),
        '追い上げ': float(np.clip(0.62*(1.0+(early-0.24)*0.55),0.48,0.78)),
        '終盤': float(np.clip(0.58*(1.0+(late-0.18)*0.65),0.44,0.74)),
        '安定性': float(np.clip(0.55*(1.0-(np.std([float((rates.get(l) or {}).get('blended_rate',0.2)) for l in range(2,7)]))*0.8),0.45,0.66)),
    }
    try:
        with sqlite3.connect(str(db_path)) as con:
            race_count=con.execute("SELECT COUNT(*) FROM result_races WHERE COALESCE(learning_eligible,1)=1").fetchone()[0]
            for name,value in vals.items():
                cur=con.execute("SELECT initial_value FROM learning_settings WHERE setting_name=?",(name,)).fetchone()
                if not cur: continue
                con.execute("""UPDATE learning_settings
                               SET current_value=?,updated_at=CURRENT_TIMESTAMP,sample_races=?,reason=?
                               WHERE setting_name=?""",
                            (round(value,6),int(race_count),'Ver260: 全実測グランドノートの周回入替率＋選手別実測追抜学習を利用',name))
                out['updated']+=1
            con.commit()
        out.update({'races':int(race_count),'pairs':int(cal.get('pairs',0))})
        _V256_SETTINGS_SYNC_CACHE.add(sig)
    except Exception as exc:
        out['error']=str(exc)
    return out

# Ver254: 予測と実測の周回差を、選手別・周回別にも縮小学習する。
# 少数データは開催場全体へ強く縮小し、過学習と補正暴走を防ぐ。
_V254_PLAYER_LAP_CACHE = {}

def _v254_player_lap_calibration(db_path: str | None, venue: str, cutoff_date: str) -> dict:
    result={"enabled":False,"samples":0,"races":0,"player_delta":{},"reason":"選手別の予測・実測周回ペア不足"}
    if not db_path or not Path(db_path).exists():
        return result
    try:
        stamp=(str(db_path),Path(db_path).stat().st_mtime_ns,str(venue),str(cutoff_date)[:10])
    except Exception:
        stamp=(str(db_path),str(venue),str(cutoff_date)[:10])
    if stamp in _V254_PLAYER_LAP_CACHE:
        return dict(_V254_PLAYER_LAP_CACHE[stamp])
    try:
        with sqlite3.connect(str(db_path)) as con:
            _v252_ensure_lap_tables(con)
            pred=pd.read_sql_query("""
                SELECT p.race_date,p.venue,p.race_no,p.lap_no,p.predicted_order,
                       p.is_backtest,p.created_at
                FROM v252_lap_prediction_snapshots p
                WHERE p.is_backtest IN (0,2)
                  AND p.app_version IN ('Ver253','Ver254','Ver256','Ver257','Ver259','Ver260')
                  AND (?='' OR p.venue=?)
                  AND (?='' OR substr(p.race_date,1,10)<substr(?,1,10))
                ORDER BY p.created_at
            """,con,params=(venue,venue,cutoff_date,cutoff_date))
            actual=pd.read_sql_query("""
                SELECT rr.race_date,rr.venue,REPLACE(COALESCE(rr.race_no,''),'R','') AS race_no,
                       rl.lap_no,rl.position,rl.car_no,
                       COALESCE(re.player_name,'') AS player_name,
                       COALESCE(CAST(REPLACE(REPLACE(re.handicap,'m',''),'Ｍ','') AS INTEGER),0) AS handicap
                FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                LEFT JOIN result_entries re ON re.race_key=rr.race_key AND re.car_no=rl.car_no
                WHERE COALESCE(rr.learning_eligible,1)=1
                  AND (?='' OR rr.venue=?)
                  AND (?='' OR substr(rr.race_date,1,10)<substr(?,1,10))
                ORDER BY rr.race_date,rr.venue,rr.race_no,rl.lap_no,rl.position
            """,con,params=(venue,venue,cutoff_date,cutoff_date))
    except Exception as exc:
        result['reason']=f'選手別周回差分読込失敗: {exc}'
        return result
    if pred.empty or actual.empty:
        return result
    pred=pred.sort_values('created_at').drop_duplicates(['race_date','venue','race_no','lap_no'],keep='last')
    amap={}; nmap={}; hmap={}
    for key,g in actual.groupby(['race_date','venue','race_no','lap_no'],dropna=False):
        gg=g.sort_values('position')
        k=(str(key[0])[:10],str(key[1]),str(key[2]),int(key[3]))
        amap[k]=tuple(int(x) for x in gg.car_no.tolist())
        nmap[k]={int(r.car_no):str(r.player_name or '').strip() for r in gg.itertuples()}
        hmap[k]={int(r.car_no):int(r.handicap or 0) for r in gg.itertuples()}
    events=[]; race_keys=set()
    for row in pred.itertuples():
        key=(str(row.race_date)[:10],str(row.venue),str(row.race_no),int(row.lap_no))
        actual_order=amap.get(key)
        try: pred_order=tuple(int(x) for x in str(row.predicted_order).split('-') if str(x).strip())
        except Exception: continue
        if not actual_order or len(pred_order)<3: continue
        prev=amap.get((key[0],key[1],key[2],key[3]-1))
        hp=hmap.get(key,{})
        if not prev:
            prev=tuple(sorted(actual_order,key=lambda c:(hp.get(c,0),c)))
        pp={c:i for i,c in enumerate(pred_order)}; ap={c:i for i,c in enumerate(actual_order)}
        names=nmap.get(key,{})
        for j in range(1,len(prev)):
            front,chaser=prev[j-1],prev[j]
            name=str(names.get(chaser,'')).strip()
            if not name or front not in pp or chaser not in pp or front not in ap or chaser not in ap:
                continue
            pred_pass=int(pp[chaser]<pp[front]); actual_pass=int(ap[chaser]<ap[front])
            weight=0.35 if int(getattr(row,'is_backtest',0) or 0)==2 else 1.0
            events.append((name,key[3],actual_pass-pred_pass,weight))
            race_keys.add(key[:3])
    if len(events)<40:
        result.update({'samples':len(events),'races':len(race_keys)})
        _V254_PLAYER_LAP_CACHE[stamp]=dict(result)
        return result
    ev=pd.DataFrame(events,columns=['player','lap','residual','weight'])
    deltas={}
    for (player,lap),g in ev.groupby(['player','lap']):
        effective_n=float(g['weight'].sum())
        if effective_n < 1.0:
            continue
        raw=float(np.average(g['residual'],weights=g['weight']))
        # 選手別はデータが薄いため、場・周回補正よりさらに強く縮小。
        shrunk=(effective_n/(effective_n+18.0))*raw
        deltas[f'{player}|{int(lap)}']={
            'delta':float(np.clip(shrunk*0.42,-0.10,0.10)),
            'raw_residual':raw,
            'n':int(len(g)),
            'effective_n':round(effective_n,2),
        }
    result={
        'enabled':bool(deltas),'samples':int(len(ev)),'races':int(len(race_keys)),
        'player_delta':deltas,
        'reason':'予測と実測の周回差を選手別・周回別に強く縮小学習'
    }
    _V254_PLAYER_LAP_CACHE.clear(); _V254_PLAYER_LAP_CACHE[stamp]=dict(result)
    return result



# Ver260: 保存予測がある7レースだけに依存せず、実測グランドノート全体から
# 選手×周回の「前車を実際に入れ替えた率」を学習する。
# 開催場データが薄い場合は全場の選手傾向へ縮小し、さらに周回全体平均との差だけを小さく反映する。
_V258_PLAYER_ACTUAL_CACHE = {}

def _v258_player_actual_lap_calibration(db_path: str | None, venue: str = "", cutoff_date: str = "") -> dict:
    result={"enabled":False,"races":0,"pairs":0,"players":0,"linked_laps":0,"total_laps":0,
            "player_delta":{},"reason":"実測グランドノートの選手別周回データ不足"}
    if not db_path or not Path(db_path).exists():
        return result
    try:
        stamp=(str(db_path),Path(db_path).stat().st_mtime_ns,str(venue),str(cutoff_date)[:10])
    except Exception:
        stamp=(str(db_path),str(venue),str(cutoff_date)[:10])
    if stamp in _V258_PLAYER_ACTUAL_CACHE:
        return dict(_V258_PLAYER_ACTUAL_CACHE[stamp])
    try:
        with sqlite3.connect(str(db_path)) as con:
            total_laps=int(con.execute("""
                SELECT COUNT(*) FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                WHERE COALESCE(rr.learning_eligible,1)=1
                  AND (?='' OR substr(rr.race_date,1,10)<substr(?,1,10))
            """,(cutoff_date,cutoff_date)).fetchone()[0] or 0)
            linked_laps=int(con.execute("""
                SELECT COUNT(*) FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                JOIN result_entries re ON re.race_key=rr.race_key AND re.car_no=rl.car_no
                WHERE COALESCE(rr.learning_eligible,1)=1
                  AND COALESCE(TRIM(re.player_name),'')<>''
                  AND (?='' OR substr(rr.race_date,1,10)<substr(?,1,10))
            """,(cutoff_date,cutoff_date)).fetchone()[0] or 0)
            d=pd.read_sql_query("""
                SELECT substr(rr.race_date,1,10) AS race_date,rr.venue,
                       REPLACE(COALESCE(rr.race_no,''),'R','') AS race_no,
                       rl.lap_no,rl.position,rl.car_no,COALESCE(re.player_name,'') AS player_name
                FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                LEFT JOIN result_entries re ON re.race_key=rr.race_key AND re.car_no=rl.car_no
                WHERE COALESCE(rr.learning_eligible,1)=1
                  AND rl.lap_no BETWEEN 1 AND 6
                  AND (?='' OR substr(rr.race_date,1,10)<substr(?,1,10))
                ORDER BY rr.race_date,rr.venue,rr.race_no,rl.lap_no,rl.position
            """,con,params=(cutoff_date,cutoff_date))
    except Exception as exc:
        result['reason']=f'Ver260選手別実測周回読込失敗: {exc}'
        return result
    result['total_laps']=total_laps; result['linked_laps']=linked_laps
    if d.empty:
        return result
    events=[]; race_keys=set()
    for (race_date,v,race_no),g in d.groupby(['race_date','venue','race_no'],dropna=False):
        orders={int(l):tuple(int(x) for x in xg.sort_values('position').car_no.tolist()) for l,xg in g.groupby('lap_no')}
        names={int(r.car_no):str(r.player_name or '').strip() for r in g.itertuples() if str(r.player_name or '').strip()}
        prev=orders.get(1)
        for lap in range(2,7):
            cur=orders.get(lap)
            if not prev or not cur:
                prev=cur or prev; continue
            pos={c:i for i,c in enumerate(cur)}
            for i in range(1,len(prev)):
                front,chaser=prev[i-1],prev[i]
                name=names.get(chaser,'')
                if not name or front not in pos or chaser not in pos:
                    continue
                passed=int(pos[chaser] < pos[front])
                events.append((str(v),name,int(lap),passed))
                race_keys.add((str(race_date),str(v),str(race_no)))
            prev=cur
    if len(events)<100:
        result.update({'pairs':len(events),'races':len(race_keys)})
        _V258_PLAYER_ACTUAL_CACHE[stamp]=dict(result)
        return result
    ev=pd.DataFrame(events,columns=['venue','player','lap','passed'])
    # 全場の周回基準率。選手補正はこの基準との差だけを使う。
    lap_base={int(l):(float(g['passed'].sum())+4.0)/(len(g)+16.0) for l,g in ev.groupby('lap')}
    global_pg={(str(p),int(l)):(int(len(g)),float(g['passed'].sum())) for (p,l),g in ev.groupby(['player','lap'])}
    local=ev[ev['venue'].astype(str)==str(venue)] if venue else ev
    local_pg={(str(p),int(l)):(int(len(g)),float(g['passed'].sum())) for (p,l),g in local.groupby(['player','lap'])}
    deltas={}; used_players=set(); used_pairs=0
    for key,(gn,gpass) in global_pg.items():
        player,lap=key
        ln,lpass=local_pg.get(key,(0,0.0))
        # 全場の選手率はBeta平滑化、開催場固有は全場選手率へ縮小。
        gp=(gpass+2.0)/(gn+8.0)
        if venue:
            lp=(lpass+2.0)/(ln+8.0)
            local_conf=ln/(ln+18.0)
            player_rate=local_conf*lp+(1.0-local_conf)*gp
            n_for_conf=ln + 0.35*gn
        else:
            player_rate=gp; n_for_conf=gn
        base=float(lap_base.get(lap,0.20))
        confidence=float(n_for_conf/(n_for_conf+35.0))
        if n_for_conf < 2.0:
            continue
        # データが増えるほど0.08→0.24程度へ緩やかに強める。最大±0.14logitに制限。
        gain=0.08+0.16*confidence
        delta=float(np.clip((_v256_logit(player_rate)-_v256_logit(base))*gain,-0.14,0.14))
        if abs(delta)<0.004:
            continue
        deltas[f'{player}|{lap}']={
            'delta':delta,'rate':round(player_rate,5),'base_rate':round(base,5),
            'global_n':int(gn),'venue_n':int(ln),'confidence':round(confidence,4)
        }
        used_players.add(player); used_pairs += int(ln if venue else gn)
    result.update({
        'enabled':bool(deltas),'races':len(race_keys),'pairs':int(len(local)),
        'players':len(used_players),'player_delta':deltas,
        'reason':'Ver260: 全実測グランドノートから選手×周回の追抜率を学習し、開催場・全場へ階層縮小'
    })
    _V258_PLAYER_ACTUAL_CACHE.clear(); _V258_PLAYER_ACTUAL_CACHE[stamp]=dict(result)
    return result

# Ver253: 保存済み予測履歴の出走表を使い、各レースを現在の6周モデルで再構成する。
# 実結果は順位生成に使わず、生成後の周回残差学習だけに低い重みで利用する。
def _v253_backfill_saved_lap_predictions(db_path: str, limit: int = 80) -> dict:
    global _V253_RECONSTRUCTION_MODE
    result={"processed":0,"saved":0,"skipped":0,"already_done":0,"missing_meta":0,"errors":[],"saved_races":[],"message":""}
    if not db_path or not Path(db_path).exists():
        result["message"]="DBが見つかりません。"; return result
    try:
        with sqlite3.connect(str(db_path)) as con:
            _v253_ensure_lap_tables(con)
            rows=con.execute("""
                SELECT history_id,race_key,race_label,prediction_time,trials,seed,raw_text,venue_override
                FROM v231_prediction_history
                WHERE COALESCE(raw_text,'')<>''
                ORDER BY prediction_time ASC, history_id ASC
                LIMIT ?
            """,(max(1,int(limit)),)).fetchall()
            existing=set((str(a),str(b),str(c)) for a,b,c in con.execute("""
                SELECT race_date,venue,race_no FROM v252_lap_prediction_snapshots
                WHERE app_version IN ('Ver253','Ver254','Ver256','Ver257','Ver259','Ver260') AND is_backtest=2 GROUP BY race_date,venue,race_no
            """).fetchall())
        for row in rows:
            result["processed"]+=1
            hid,race_key,label,pred_time,trials,seed,raw_text,venue_override=row
            try:
                prediction_text=str(raw_text or '')
                if str(venue_override or '').strip():
                    prediction_text=f"開催場: {str(venue_override).strip()}\n"+prediction_text
                # パーサーと基礎予測は保存当時の入力だけから再実行。
                df,bets,output,entries,meta=engine.ver16_run_prediction(
                    prediction_text,max(1000,min(int(trials or 2000),4000)),int(seed or 42),manual_excluded=[]
                )
                meta=dict(meta or {})
                race_date=str(meta.get('開催日') or meta.get('race_date') or '')[:10]
                venue=str(meta.get('開催場') or meta.get('venue') or '').strip()
                race_no=str(meta.get('R') or meta.get('レース') or meta.get('race_no') or '').strip().replace('R','')
                if not race_date or not venue or not race_no:
                    result["missing_meta"]+=1
                    result["skipped"]+=1
                    continue
                if (race_date,venue,race_no) in existing:
                    result["already_done"]+=1
                    result["skipped"]+=1
                    continue
                _V253_RECONSTRUCTION_MODE=True
                try:
                    _v230_six_lap_simulation(df,bets,entries,meta,max(1000,min(int(trials or 2000),4000)),int(seed or 42))
                finally:
                    _V253_RECONSTRUCTION_MODE=False
                existing.add((race_date,venue,race_no)); result["saved"]+=1
                result["saved_races"].append(f"{race_date} {venue}{race_no}R")
            except Exception as exc:
                _V253_RECONSTRUCTION_MODE=False
                if len(result["errors"])<8: result["errors"].append(f"履歴{hid}: {type(exc).__name__}: {exc}")
        _V252_LAP_RESIDUAL_CACHE.clear()
        result["message"]=(
            f"確認 {result['processed']}件｜新規再構成 {result['saved']}レース｜"
            f"再構成済み {result['already_done']}件｜材料不足 {result['missing_meta']}件｜"
            f"処理エラー {len(result['errors'])}件"
        )
    except Exception as exc:
        result["message"]=f"再構成失敗: {type(exc).__name__}: {exc}"
    return result


def _v253_reconstruction_status(db_path: str) -> dict:
    """Ver253の再構成保存状況と、実測グランドノートとの照合精度を返す。"""
    out={"races":0,"lap_rows":0,"paired_laps":0,"position_match":None,
         "pair_match":None,"position_mae":None,"by_lap":[],"race_labels":[],
         "learning_enabled":False,"learning_samples":0,"learning_races":0,"reason":""}
    if not db_path or not Path(db_path).exists():
        out["reason"]="DBが見つかりません。"; return out
    try:
        with sqlite3.connect(str(db_path)) as con:
            _v253_ensure_lap_tables(con)
            pred=pd.read_sql_query("""
                SELECT race_date,venue,race_no,lap_no,predicted_order,support,created_at
                FROM v252_lap_prediction_snapshots
                WHERE app_version IN ('Ver253','Ver254','Ver256','Ver257','Ver259','Ver260') AND is_backtest=2
                ORDER BY race_date,venue,CAST(race_no AS INTEGER),lap_no,created_at
            """,con)
            actual=pd.read_sql_query("""
                SELECT substr(rr.race_date,1,10) AS race_date,rr.venue,
                       CAST(rr.race_no AS TEXT) AS race_no,rl.lap_no,rl.position,rl.car_no
                FROM result_laps rl JOIN result_races rr ON rr.race_key=rl.race_key
                WHERE COALESCE(rr.learning_eligible,1)=1
                ORDER BY rr.race_date,rr.venue,CAST(rr.race_no AS INTEGER),rl.lap_no,rl.position
            """,con)
        if pred.empty:
            out["reason"]="再構成データはまだありません。"; return out
        pred=pred.sort_values('created_at').drop_duplicates(
            ['race_date','venue','race_no','lap_no'],keep='last')
        race_groups=pred[['race_date','venue','race_no']].drop_duplicates()
        out['races']=int(len(race_groups)); out['lap_rows']=int(len(pred))
        out['race_labels']=[f"{r.race_date} {r.venue}{r.race_no}R" for r in race_groups.itertuples()]
        amap={}
        for key,g in actual.groupby(['race_date','venue','race_no','lap_no'],dropna=False):
            amap[(str(key[0])[:10],str(key[1]),str(key[2]),int(key[3]))]=tuple(
                int(x) for x in g.sort_values('position').car_no.tolist())
        metrics=[]
        for row in pred.itertuples():
            key=(str(row.race_date)[:10],str(row.venue),str(row.race_no),int(row.lap_no))
            act=amap.get(key)
            try: prd=tuple(int(x) for x in str(row.predicted_order).split('-') if str(x).strip())
            except Exception: continue
            common=sorted(set(act or ()) & set(prd))
            if not act or len(common)<3: continue
            ap={c:i for i,c in enumerate(act)}; pp={c:i for i,c in enumerate(prd)}
            pos_match=sum(1 for i,c in enumerate(act) if i<len(prd) and prd[i]==c)/max(len(act),len(prd))
            pair_total=pair_ok=0
            for i in range(len(common)):
                for j in range(i+1,len(common)):
                    a,b=common[i],common[j]; pair_total+=1
                    pair_ok += int((ap[a]<ap[b])==(pp[a]<pp[b]))
            mae=float(np.mean([abs(ap[c]-pp[c]) for c in common]))
            metrics.append({'lap':int(row.lap_no),'position_match':pos_match,
                            'pair_match':pair_ok/pair_total if pair_total else 0.0,
                            'position_mae':mae})
        if metrics:
            md=pd.DataFrame(metrics); out['paired_laps']=int(len(md))
            out['position_match']=float(md.position_match.mean())
            out['pair_match']=float(md.pair_match.mean())
            out['position_mae']=float(md.position_mae.mean())
            for lap,g in md.groupby('lap'):
                out['by_lap'].append({'lap':int(lap),'paired':int(len(g)),
                    'position_match':float(g.position_match.mean()),
                    'pair_match':float(g.pair_match.mean()),
                    'position_mae':float(g.position_mae.mean())})
        learning=_v252_lap_residual_learning(str(db_path))
        out['learning_enabled']=bool(learning.get('enabled'))
        out['learning_samples']=int(learning.get('samples',0) or 0)
        out['learning_races']=int(learning.get('races',0) or 0)
        out['reason']=str(learning.get('reason',''))
    except Exception as exc:
        out['reason']=f"状況確認失敗: {type(exc).__name__}: {exc}"
    return out


def _v254_saved_version_lap_comparison(db_path: str) -> pd.DataFrame:
    """Compare saved lap snapshots by app_version against actual grand-note laps.

    This is a stored-data comparison only. It does not pretend to rerun historical
    engines that are no longer present in the current source tree.
    """
    columns=['バージョン','対象レース','照合周回','位置一致率','前後関係一致率','平均順位誤差']
    if not db_path or not Path(db_path).exists():
        return pd.DataFrame(columns=columns)
    try:
        with sqlite3.connect(str(db_path)) as con:
            _v252_ensure_lap_tables(con)
            pred=pd.read_sql_query("""
                SELECT race_date,venue,race_no,lap_no,predicted_order,app_version,created_at
                FROM v252_lap_prediction_snapshots
                ORDER BY created_at
            """,con)
            actual=pd.read_sql_query("""
                SELECT substr(rr.race_date,1,10) AS race_date,rr.venue,
                       REPLACE(CAST(COALESCE(rr.race_no,'') AS TEXT),'R','') AS race_no,
                       rl.lap_no,rl.position,rl.car_no
                FROM result_laps rl
                JOIN result_races rr ON rr.race_key=rl.race_key
                WHERE COALESCE(rr.learning_eligible,1)=1
                ORDER BY rr.race_date,rr.venue,rr.race_no,rl.lap_no,rl.position
            """,con)
    except Exception:
        return pd.DataFrame(columns=columns)
    if pred.empty or actual.empty:
        return pd.DataFrame(columns=columns)
    pred['race_date']=pred['race_date'].astype(str).str[:10]
    pred['race_no']=pred['race_no'].astype(str).str.replace('R','',regex=False)
    pred=pred.sort_values('created_at').drop_duplicates(
        ['app_version','race_date','venue','race_no','lap_no'],keep='last')
    amap={}
    for key,g in actual.groupby(['race_date','venue','race_no','lap_no'],dropna=False):
        amap[(str(key[0])[:10],str(key[1]),str(key[2]),int(key[3]))]=tuple(
            int(x) for x in g.sort_values('position').car_no.tolist())
    rows=[]
    for version,gver in pred.groupby('app_version',dropna=False):
        metrics=[]; races=set()
        for row in gver.itertuples():
            key=(str(row.race_date)[:10],str(row.venue),str(row.race_no),int(row.lap_no))
            act=amap.get(key)
            try:
                prd=tuple(int(x) for x in str(row.predicted_order).split('-') if str(x).strip())
            except Exception:
                continue
            common=sorted(set(act or ()) & set(prd))
            if not act or len(common)<3:
                continue
            ap={c:i for i,c in enumerate(act)}; pp={c:i for i,c in enumerate(prd)}
            pos_match=sum(1 for i,c in enumerate(act) if i<len(prd) and prd[i]==c)/max(len(act),len(prd))
            pair_total=pair_ok=0
            for i in range(len(common)):
                for j in range(i+1,len(common)):
                    a,b=common[i],common[j]; pair_total+=1
                    pair_ok += int((ap[a]<ap[b])==(pp[a]<pp[b]))
            mae=float(np.mean([abs(ap[c]-pp[c]) for c in common]))
            metrics.append((pos_match,pair_ok/pair_total if pair_total else 0.0,mae))
            races.add(key[:3])
        if metrics:
            arr=np.asarray(metrics,dtype=float)
            rows.append({
                'バージョン':str(version),'対象レース':len(races),'照合周回':len(metrics),
                '位置一致率':round(float(arr[:,0].mean())*100,1),
                '前後関係一致率':round(float(arr[:,1].mean())*100,1),
                '平均順位誤差':round(float(arr[:,2].mean()),2),
            })
    return pd.DataFrame(rows,columns=columns).sort_values(
        ['前後関係一致率','位置一致率','平均順位誤差'],ascending=[False,False,True],ignore_index=True)


def _v260_snapshot_storage_status(db_path: str, app_version: str = "") -> dict:
    """Show prediction-history count separately from lap-snapshot count."""
    out={"history_races":0,"snapshot_races":0,"snapshot_laps":0,"waiting_or_missing":0,"reason":""}
    if not db_path or not Path(db_path).exists():
        out["reason"]="DBなし"; return out
    ver=str(app_version or globals().get('_V231_APP_VERSION') or '').strip()
    try:
        with sqlite3.connect(str(db_path)) as con:
            _v252_ensure_lap_tables(con)
            try:
                row=con.execute("SELECT COUNT(DISTINCT race_key) FROM v231_prediction_history WHERE app_version=?",(ver,)).fetchone()
                out["history_races"]=int(row[0] or 0)
            except Exception:
                out["history_races"]=0
            row=con.execute("""
                SELECT COUNT(DISTINCT race_date||'|'||venue||'|'||race_no), COUNT(*)
                FROM v252_lap_prediction_snapshots WHERE app_version=?
            """,(ver,)).fetchone()
            out["snapshot_races"]=int(row[0] or 0); out["snapshot_laps"]=int(row[1] or 0)
        out["waiting_or_missing"]=max(0,out["history_races"]-out["snapshot_races"])
        out["reason"]="OK"
    except Exception as exc:
        out["reason"]=f"{type(exc).__name__}: {exc}"
    return out


# Ver255: 保存済みバージョンを同一の実測グランドノートで比較し、
# データ量と改善幅を加味して「採用候補 / 保留」を自動判定する。
def _v255_backtest_center(db_path: str) -> dict:
    out={
        'comparison':pd.DataFrame(), 'best_version':'', 'decision':'比較データ不足',
        'decision_reason':'比較可能な保存済み周回予測がありません。',
        'learning':{}, 'player_learning':{}, 'report_id':None,
    }
    cmp_df=_v254_saved_version_lap_comparison(db_path)
    out['comparison']=cmp_df
    try:
        out['learning']=_v252_lap_residual_learning(db_path)
    except Exception as exc:
        out['learning']={'enabled':False,'reason':f'{type(exc).__name__}: {exc}'}
    try:
        out['player_learning']=_v254_player_lap_calibration(db_path,'','')
    except Exception as exc:
        out['player_learning']={'enabled':False,'reason':f'{type(exc).__name__}: {exc}'}
    if cmp_df.empty:
        return out
    scored=cmp_df.copy()
    mae_score=(100.0-(scored['平均順位誤差'].astype(float)*22.0)).clip(lower=0,upper=100)
    scored['総合スコア']=(
        scored['前後関係一致率'].astype(float)*0.45+
        scored['位置一致率'].astype(float)*0.35+
        mae_score*0.20
    ).round(1)
    scored=scored.sort_values(
        ['総合スコア','対象レース','照合周回'],ascending=[False,False,False],ignore_index=True)
    out['comparison']=scored
    best=scored.iloc[0]
    out['best_version']=str(best['バージョン'])
    enough=bool(int(best['対象レース'])>=3 and int(best['照合周回'])>=12)
    if len(scored)==1:
        out['decision']='保留'
        out['decision_reason']='比較できるバージョンが1種類だけです。別バージョンの未来予測を保存してから比較してください。'
    else:
        second=scored.iloc[1]
        margin=float(best['総合スコア'])-float(second['総合スコア'])
        if enough and margin>=1.0:
            out['decision']='採用候補'
            out['decision_reason']=(
                f"{best['バージョン']}が次点より総合スコアで{margin:.1f}点上回りました。"
                '保存済みデータ内の比較なので、未来レースでの確認後に正式採用してください。'
            )
        elif not enough:
            out['decision']='保留'
            out['decision_reason']='対象レースまたは照合周回が少ないため、補正は自動採用しません。'
        else:
            out['decision']='保留'
            out['decision_reason']=f'上位2版の差が{margin:.1f}点で小さいため、追加データを待ちます。'
    return out


def _v255_save_backtest_report(db_path: str, result: dict) -> int | None:
    """Store only the evaluation summary. Prediction weights are never rewritten here."""
    if not db_path or not Path(db_path).exists():
        return None
    cmp_df=result.get('comparison')
    payload=[] if not isinstance(cmp_df,pd.DataFrame) else cmp_df.to_dict(orient='records')
    try:
        import json as _json
        with sqlite3.connect(str(db_path)) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS v255_backtest_reports(
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    app_version TEXT NOT NULL,
                    best_version TEXT,
                    decision TEXT,
                    decision_reason TEXT,
                    comparison_json TEXT
                )
            """)
            cur=con.execute("""
                INSERT INTO v255_backtest_reports(
                    app_version,best_version,decision,decision_reason,comparison_json
                ) VALUES(?,?,?,?,?)
            """,(
                _V231_APP_VERSION,str(result.get('best_version') or ''),
                str(result.get('decision') or ''),str(result.get('decision_reason') or ''),
                _json.dumps(payload,ensure_ascii=False)
            ))
            con.commit()
            return int(cur.lastrowid)
    except Exception:
        return None


# Ver250: 前方集団残存・周回内連続追抜制限・ハンデ差別の壁を開催日前実績だけで自動学習する。
_V250_FLOW_CALIBRATION_CACHE = {}

def _v250_flow_calibration(db_path: str | None = None, venue: str = "", cutoff_date: str = "") -> dict:
    path = str(db_path or _v230_db_path())
    venue = str(venue or "").strip()
    cutoff = str(cutoff_date or "").strip()[:10]
    try:
        stamp = (path, int(Path(path).stat().st_mtime_ns), venue, cutoff)
    except Exception:
        stamp = (path, 0, venue, cutoff)
    if stamp in _V250_FLOW_CALIBRATION_CACHE:
        return dict(_V250_FLOW_CALIBRATION_CACHE[stamp])
    result = {
        "enabled": False, "leader_hold_delta": 0.0, "front_survival_delta": 0.0,
        "queue_wall_delta": 0.0, "races": 0, "venue_races": 0,
        "reason": "展開学習データ不足のため中立",
    }
    try:
        with sqlite3.connect(path) as con:
            races = pd.read_sql_query(
                "SELECT race_key,race_date,venue FROM result_races WHERE COALESCE(learning_eligible,1)=1", con
            )
            entries = pd.read_sql_query(
                "SELECT race_key,car_no,finish,handicap FROM result_entries WHERE finish IS NOT NULL", con
            )
            laps = pd.read_sql_query(
                "SELECT race_key,lap_no,position,car_no FROM result_laps", con
            )
        if cutoff:
            races = races[pd.to_datetime(races["race_date"], errors="coerce") < pd.to_datetime(cutoff, errors="coerce")]
        if races.empty or entries.empty:
            _V250_FLOW_CALIBRATION_CACHE[stamp] = dict(result); return result
        keys = set(races["race_key"].astype(str))
        entries = entries[entries["race_key"].astype(str).isin(keys)].copy()
        laps = laps[laps["race_key"].astype(str).isin(keys)].copy()
        entries["finish"] = pd.to_numeric(entries["finish"], errors="coerce")
        entries["h"] = pd.to_numeric(entries["handicap"].astype(str).str.extract(r"(-?\d+)")[0], errors="coerce").fillna(0)
        # 初周先頭が最終3着内へ残る率。少数開催場は全場平均へ強く縮小する。
        first = laps[pd.to_numeric(laps["lap_no"], errors="coerce") == 1].copy()
        first["position"] = pd.to_numeric(first["position"], errors="coerce")
        leaders = first[first["position"] == 1][["race_key","car_no"]].merge(
            entries[["race_key","car_no","finish"]], on=["race_key","car_no"], how="inner"
        ).merge(races[["race_key","venue"]], on="race_key", how="left")
        global_leader = float((leaders["finish"].le(3).sum() + 12) / (len(leaders) + 24)) if len(leaders) else 0.5
        vlead = leaders[leaders["venue"].astype(str) == venue] if venue else leaders.iloc[0:0]
        venue_leader = float((vlead["finish"].le(3).sum() + 40*global_leader) / (len(vlead) + 40))
        # 最前ハンデ群の3着内率。選手単位ではなく車群単位で学習する。
        front_rows=[]
        for rk,g in entries.groupby("race_key"):
            if g.empty: continue
            mh=float(g["h"].min())
            z=g[g["h"]==mh]
            for _,r in z.iterrows(): front_rows.append((str(rk), float(r["finish"]<=3)))
        front=pd.DataFrame(front_rows, columns=["race_key","top3"]) if front_rows else pd.DataFrame(columns=["race_key","top3"])
        front=front.merge(races[["race_key","venue"]],on="race_key",how="left") if not front.empty else front
        global_front=float((front["top3"].sum()+20)/(len(front)+40)) if len(front) else 0.35
        vf=front[front["venue"].astype(str)==venue] if venue and not front.empty else front.iloc[0:0]
        venue_front=float((vf["top3"].sum()+60*global_front)/(len(vf)+60))
        # 周回中の隣接入替率が低い開催場ほど、密集壁を少し強くする。
        pass_rows=[]
        for rk,g in laps.groupby("race_key"):
            for lap in sorted(pd.to_numeric(g["lap_no"],errors="coerce").dropna().astype(int).unique()):
                a=g[pd.to_numeric(g["lap_no"],errors="coerce")==lap].sort_values("position")
                b=g[pd.to_numeric(g["lap_no"],errors="coerce")==lap+1]
                if len(a)<4 or b.empty: continue
                npmap=dict(zip(pd.to_numeric(b["car_no"],errors="coerce"),pd.to_numeric(b["position"],errors="coerce")))
                order=pd.to_numeric(a["car_no"],errors="coerce").dropna().astype(int).tolist()
                for i in range(1,len(order)):
                    f,c=order[i-1],order[i]
                    if f in npmap and c in npmap: pass_rows.append((str(rk), float(npmap[c] < npmap[f])))
        passes=pd.DataFrame(pass_rows,columns=["race_key","passed"]) if pass_rows else pd.DataFrame(columns=["race_key","passed"])
        passes=passes.merge(races[["race_key","venue"]],on="race_key",how="left") if not passes.empty else passes
        gp=float((passes["passed"].sum()+30)/(len(passes)+100)) if len(passes) else 0.20
        vp=passes[passes["venue"].astype(str)==venue] if venue and not passes.empty else passes.iloc[0:0]
        vpass=float((vp["passed"].sum()+120*gp)/(len(vp)+120))
        result.update({
            "enabled": bool(len(races)>=50),
            "leader_hold_delta": float(np.clip(_v248_logit(venue_leader)-_v248_logit(global_leader),-0.22,0.22)),
            "front_survival_delta": float(np.clip(_v248_logit(venue_front)-_v248_logit(global_front),-0.18,0.18)),
            "queue_wall_delta": float(np.clip((gp-vpass)*0.85,-0.12,0.12)),
            "races": int(races["race_key"].nunique()), "venue_races": int((races["venue"].astype(str)==venue).sum()),
            "reason": f"{cutoff or '最新'}より前の{int(races['race_key'].nunique())}R（{venue or '全場'} {int((races['venue'].astype(str)==venue).sum())}R）から先頭維持・前ハンデ残り・隣接入替率を縮小学習",
        })
    except Exception as exc:
        result["reason"] = f"展開学習失敗: {exc}"
    _V250_FLOW_CALIBRATION_CACHE[stamp] = dict(result)
    return result


# Ver263: 実測グランドノートから「展開タイプ」を学習し、
# シミュレーション内の複数展開ルートを弱く確率校正する。
_V263_SCENARIO_PRIOR_CACHE = {}

def _v263_scenario_type_from_laps(laps):
    try:
        seq=[tuple(int(x) for x in row) for row in (laps or []) if row]
    except Exception:
        seq=[]
    if len(seq)<2:
        return '不明'
    leaders=[row[0] for row in seq if row]
    leader_changes=sum(1 for a,b in zip(leaders,leaders[1:]) if a!=b)
    first_change=next((i+2 for i,(a,b) in enumerate(zip(leaders,leaders[1:])) if a!=b),99)
    p0={c:i for i,c in enumerate(seq[0])}
    plast={c:i for i,c in enumerate(seq[-1])}
    common=set(p0)&set(plast)
    avg_move=sum(abs(p0[c]-plast[c]) for c in common)/max(1,len(common))
    mid=seq[min(2,len(seq)-1)]
    pmid={c:i for i,c in enumerate(mid)}
    late_gain=sum(1 for c in set(pmid)&set(plast) if pmid[c]-plast[c]>=2)
    if leaders[0]==leaders[-1] and leader_changes<=1 and avg_move<1.25:
        return '前残り型'
    if leaders[0]!=leaders[-1] and first_change<=2:
        return '早仕掛け型'
    if leader_changes>=3 or avg_move>=2.0:
        return '波乱型'
    if first_change>=4 or late_gain>=1:
        return '後半追込型'
    return '中盤入替型'

def _v263_route_similarity(pred_laps, actual_laps):
    vals=[]
    for p,a in zip(pred_laps or [], actual_laps or []):
        try:
            pp=tuple(int(x) for x in p); aa=tuple(int(x) for x in a)
        except Exception:
            continue
        if not pp or not aa:
            continue
        pos=sum(1 for x,y in zip(pp,aa) if x==y)/max(1,len(aa))
        vals.append(0.35*pos + 0.65*_v251_pairwise_accuracy(pp,aa))
    return float(sum(vals)/len(vals)) if vals else 0.0

def _v263_scenario_prior(db_path, venue='', cutoff=''):
    key=(str(db_path or ''),str(venue or ''),str(cutoff or '')[:10])
    if key in _V263_SCENARIO_PRIOR_CACHE:
        return dict(_V263_SCENARIO_PRIOR_CACHE[key])
    out={'enabled':False,'samples':0,'venue_samples':0,'prior':{},'reason':'実測展開データ不足'}
    if not db_path or not Path(db_path).exists():
        return out
    try:
        con=sqlite3.connect(db_path)
        params=[]; wh=['COALESCE(rr.learning_eligible,1)=1']
        if cutoff:
            wh.append('substr(rr.race_date,1,10)<substr(?,1,10)'); params.append(str(cutoff)[:10])
        q=f'''SELECT rr.race_key,rr.venue,rl.lap_no,rl.position,rl.car_no
              FROM result_laps rl JOIN result_races rr ON rr.race_key=rl.race_key
              WHERE {' AND '.join(wh)} ORDER BY rr.race_key,rl.lap_no,rl.position'''
        rows=pd.read_sql_query(q,con,params=params)
        con.close()
        if rows.empty:
            return out
        all_counts={}; venue_counts={}; n_all=n_venue=0
        for _,g in rows.groupby('race_key'):
            laps=[]
            for _,lg in g.groupby('lap_no',sort=True):
                order=tuple(int(x) for x in lg.sort_values('position')['car_no'].tolist())
                if order: laps.append(order)
            typ=_v263_scenario_type_from_laps(laps)
            if typ=='不明':
                continue
            all_counts[typ]=all_counts.get(typ,0)+1; n_all+=1
            v=str(g['venue'].iloc[0] or '')
            if venue and v==venue:
                venue_counts[typ]=venue_counts.get(typ,0)+1; n_venue+=1
        if n_all<8:
            return out
        types=['前残り型','早仕掛け型','中盤入替型','後半追込型','波乱型']
        alpha=min(0.80,n_venue/(n_venue+24.0)) if venue else 0.0
        prior={}
        for t in types:
            pg=(all_counts.get(t,0)+1.0)/(n_all+len(types))
            pv=(venue_counts.get(t,0)+1.0)/(n_venue+len(types)) if n_venue else pg
            prior[t]=(1-alpha)*pg+alpha*pv
        z=sum(prior.values()) or 1.0
        prior={k:v/z for k,v in prior.items()}
        out={'enabled':True,'samples':n_all,'venue_samples':n_venue,'prior':prior,
             'reason':f'{cutoff or "最新"}より前の実測{n_all}R（{venue or "全場"} {n_venue}R）から展開タイプ頻度を縮小学習'}
    except Exception as exc:
        out['reason']=f'展開タイプ学習失敗: {exc}'
    _V263_SCENARIO_PRIOR_CACHE[key]=dict(out)
    return out

def _v263_save_scenario_feedback(db_path, meta, dist, actual_type, closest_similarity, closest_route):
    if not db_path or not Path(db_path).exists() or not actual_type or actual_type=='不明':
        return
    try:
        con=sqlite3.connect(db_path)
        con.execute('''CREATE TABLE IF NOT EXISTS v263_scenario_feedback(
            race_date TEXT,venue TEXT,race_no TEXT,app_version TEXT,actual_scenario TEXT,
            predicted_json TEXT,closest_similarity REAL,closest_route TEXT,created_at TEXT)''')
        _rd=str((meta or {}).get('開催日') or (meta or {}).get('race_date') or '')[:10]
        _vv=str((meta or {}).get('開催場') or (meta or {}).get('venue') or '')
        _rr=str((meta or {}).get('R') or (meta or {}).get('レース') or (meta or {}).get('race_no') or '')
        _av=str(globals().get('_V231_APP_VERSION') or 'Ver265')
        con.execute('DELETE FROM v263_scenario_feedback WHERE race_date=? AND venue=? AND race_no=? AND app_version=?',(_rd,_vv,_rr,_av))
        con.execute('''INSERT INTO v263_scenario_feedback VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))''',
                    (_rd,_vv,_rr,_av,actual_type,json.dumps(dist or {},ensure_ascii=False),float(closest_similarity or 0.0),str(closest_route or '')))
        con.commit(); con.close()
    except Exception:
        pass


# Ver264: Ver258以降で強くなりすぎた補正を縮小し、
# 実測に近い展開が候補集合へ入りやすくなるよう「展開分岐」自体を弱く校正する。
_V264_SCENARIO_FEEDBACK_CACHE = {}

def _v264_feedback_scenario_adjustment(db_path, venue='', cutoff=''):
    key=(str(db_path or ''),str(venue or ''),str(cutoff or '')[:10])
    if key in _V264_SCENARIO_FEEDBACK_CACHE:
        return dict(_V264_SCENARIO_FEEDBACK_CACHE[key])
    out={'enabled':False,'samples':0,'factor':{},'reason':'展開フィードバック不足'}
    if not db_path or not Path(db_path).exists():
        return out
    try:
        con=sqlite3.connect(db_path)
        con.execute('''CREATE TABLE IF NOT EXISTS v263_scenario_feedback(
            race_date TEXT,venue TEXT,race_no TEXT,app_version TEXT,actual_scenario TEXT,
            predicted_json TEXT,closest_similarity REAL,closest_route TEXT,created_at TEXT)''')
        con.execute('''DELETE FROM v263_scenario_feedback
                       WHERE rowid NOT IN (
                         SELECT MAX(rowid) FROM v263_scenario_feedback
                         GROUP BY race_date,venue,race_no,app_version
                       )''')
        try:
            con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS ux_v263_scenario_feedback_race_ver
                           ON v263_scenario_feedback(race_date,venue,race_no,app_version)''')
        except Exception:
            pass
        con.commit()
        wh=[]; params=[]
        if venue:
            wh.append('venue=?'); params.append(str(venue))
        if cutoff:
            wh.append('substr(race_date,1,10)<substr(?,1,10)'); params.append(str(cutoff)[:10])
        q='SELECT actual_scenario,predicted_json,closest_similarity FROM v263_scenario_feedback'
        if wh: q += ' WHERE ' + ' AND '.join(wh)
        rows=con.execute(q,params).fetchall(); con.close()
        if not rows:
            return out
        types=['前残り型','早仕掛け型','中盤入替型','後半追込型','波乱型']
        obs={t:0.0 for t in types}; pred={t:0.0 for t in types}; wsum=0.0
        for actual,pj,sim in rows:
            try: dist=json.loads(pj or '{}')
            except Exception: dist={}
            ss=float(sim or 0.0); w=float(np.clip(1.0-0.45*ss,0.60,1.00))
            if actual in obs: obs[actual]+=w
            for t in types:
                pred[t]+=w*float(dist.get(t,0.0) or 0.0)/100.0
            wsum+=w
        if wsum<=0: return out
        n=len(rows); shrink=n/(n+28.0)
        factor={}
        for t in types:
            o=obs[t]/wsum; pp=pred[t]/wsum
            f=float(np.exp(np.clip((o-pp)*0.75*shrink,-0.10,0.10)))
            factor[t]=float(np.clip(f,0.92,1.08))
        out={'enabled':n>=4,'samples':n,'factor':factor,
             'reason':f'過去{n}Rの実測展開と予測展開比率の差を縮小校正'}
    except Exception as exc:
        out['reason']=f'展開フィードバック校正失敗: {exc}'
    _V264_SCENARIO_FEEDBACK_CACHE[key]=dict(out)
    return out

def _v264_blended_scenario_prior(empirical, feedback):
    types=['前残り型','早仕掛け型','中盤入替型','後半追込型','波乱型']
    base=(empirical or {}).get('prior') or {}
    fac=(feedback or {}).get('factor') or {}
    if not base:
        base={t:1.0/len(types) for t in types}
    raw={t:max(1e-6,float(base.get(t,0.0) or 0.0))*float(fac.get(t,1.0) or 1.0) for t in types}
    z=sum(raw.values()) or 1.0
    return {t:v/z for t,v in raw.items()}


# Ver265: 予測タイムの土台を、過去の「競走T－試走T」実測残差で校正する。
# 同日以降は使わず、開催場→選手→ハンデ帯の順で縮小して過学習を抑える。
def _v265_time_residual_calibration(db_path: str | None, venue: str = "", cutoff_date: str = "") -> dict:
    empty={"enabled":False,"venue":str(venue or ""),"samples":0,"player_expected":{},"handicap_expected":{},"venue_expected":None,"global_expected":None}
    if not db_path or not os.path.exists(str(db_path)):
        return empty
    try:
        with sqlite3.connect(str(db_path)) as con:
            q='''
                SELECT h.race_date,h.venue,h.handicap,h.trial_time,h.race_time,p.player_name
                FROM race_history h
                LEFT JOIN players p ON p.player_id=h.player_id
                WHERE COALESCE(h.use_for_model,1)=1
                  AND COALESCE(h.result_status,'通常')='通常'
                  AND h.trial_time IS NOT NULL AND h.race_time IS NOT NULL
                  AND h.trial_time BETWEEN 3.20 AND 4.20
                  AND h.race_time BETWEEN 3.20 AND 4.50
            '''
            params=[]
            if cutoff_date:
                q+=' AND h.race_date < ?'; params.append(str(cutoff_date)[:10])
            rows=con.execute(q,params).fetchall()
    except Exception as e:
        out=dict(empty); out['error']=str(e); return out
    vals=[]
    for rd,v,h,tt,rt,nm in rows:
        try:
            d=float(rt)-float(tt)
            if not (0.015 <= d <= 0.220):
                continue
            m=re.search(r'-?\d+',str(h or '0'))
            hv=int(m.group()) if m else 0
            bucket=int(round(hv/10.0)*10)
            vals.append((str(v or ''),_v230_norm_name(nm),bucket,d,str(rd or '')))
        except Exception:
            continue
    if len(vals)<20:
        out=dict(empty); out['samples']=len(vals); return out
    import statistics
    all_d=[x[3] for x in vals]
    global_med=float(statistics.median(all_d))
    vv=[x for x in vals if str(x[0])==str(venue)] if venue else vals
    venue_med=float(statistics.median([x[3] for x in vv])) if vv else global_med
    venue_n=len(vv)
    venue_w=venue_n/(venue_n+80.0)
    venue_expected=global_med+(venue_med-global_med)*venue_w
    by_player={}; by_h={}
    for v,n,b,d,rd in vv:
        if n: by_player.setdefault(n,[]).append(d)
        by_h.setdefault(b,[]).append(d)
    player_expected={}
    for n,ds in by_player.items():
        if len(ds)<3: continue
        med=float(statistics.median(ds)); w=len(ds)/(len(ds)+14.0)
        exp=venue_expected+(med-venue_expected)*w
        player_expected[n]={"expected_delta":float(exp),"samples":len(ds),"weight":float(w)}
    handicap_expected={}
    for b,ds in by_h.items():
        if len(ds)<8: continue
        med=float(statistics.median(ds)); w=len(ds)/(len(ds)+35.0)
        exp=venue_expected+(med-venue_expected)*w
        handicap_expected[int(b)]={"expected_delta":float(exp),"samples":len(ds),"weight":float(w)}
    return {"enabled":True,"venue":str(venue or ''),"samples":len(vals),"venue_samples":venue_n,
            "global_expected":global_med,"venue_expected":venue_expected,
            "player_expected":player_expected,"handicap_expected":handicap_expected,
            "note":"過去日の通常結果のみ。競走T−試走Tを中央値＋縮小で校正。"}


def _v265_time_adjustment_seconds(cal: dict, player_name: str, handicap_value: float, current_trial: float, current_pred: float) -> tuple[float,float,int]:
    if not cal or not cal.get('enabled'):
        return 0.0,0.0,0
    try:
        base=float(cal.get('venue_expected') if cal.get('venue_expected') is not None else cal.get('global_expected'))
        p=(cal.get('player_expected') or {}).get(_v230_norm_name(player_name)) or {}
        hb=int(round(float(handicap_value or 0)/10.0)*10)
        h=(cal.get('handicap_expected') or {}).get(hb) or {}
        expected=base; conf=0.12; samples=0
        if h:
            hw=min(0.35,float(h.get('weight',0.0))*0.35)
            expected=(1-hw)*expected+hw*float(h.get('expected_delta',base)); conf=max(conf,hw); samples+=int(h.get('samples',0))
        if p:
            pw=min(0.62,float(p.get('weight',0.0))*0.62)
            expected=(1-pw)*expected+pw*float(p.get('expected_delta',base)); conf=max(conf,pw); samples+=int(p.get('samples',0))
        pred_delta=float(current_pred)-float(current_trial)
        learn_rate=min(0.45,max(0.22,0.20+0.35*conf))
        adj=(expected-pred_delta)*learn_rate
        adj=float(max(-0.012,min(0.012,adj)))
        return adj,float(expected),int(samples)
    except Exception:
        return 0.0,0.0,0


# ---------------------------------------------------------------------------
# Ver268: ハンデ残差学習
# 保存済み予測Tと実競走Tの actual-predicted 残差を、ハンデに対する
# 滑らかな線形カーブとして学習する。未来日の結果は使わず、
# 5-fold CVでMAEが3%以上改善した場合のみ予測へ自動採用する。
# ---------------------------------------------------------------------------

_V268_HANDICAP_MODEL_CACHE = {}

def _v268_handicap_bias_model(db_path: str | None, venue: str = "", cutoff_date: str = "", cutoff_race_no: int = 0) -> dict:
    empty = {
        "enabled": False, "samples": 0, "venue": str(venue or ""),
        "slope": 0.0, "intercept": 0.0, "shrink": 0.0,
        "mae_before": None, "mae_after_cv": None, "improvement_pct": 0.0,
        "reason": "データ不足",
    }
    if not db_path or not os.path.exists(str(db_path)):
        return empty

    key = (str(db_path), str(venue or ""), str(cutoff_date or "")[:10], int(cutoff_race_no or 0))
    if key in _V268_HANDICAP_MODEL_CACHE:
        return dict(_V268_HANDICAP_MODEL_CACHE[key])

    try:
        with sqlite3.connect(str(db_path)) as con:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            needed = {"v266_pred_time_snapshots","result_races","result_entries"}
            if not needed.issubset(tables):
                out = dict(empty)
                out["reason"] = "予測Tスナップショット未準備"
                _V268_HANDICAP_MODEL_CACHE[key] = dict(out)
                return out

            q = """
                WITH latest AS (
                    SELECT s.race_key, s.car_no, MAX(s.history_id) AS history_id
                    FROM v266_pred_time_snapshots s
                    JOIN result_races rr ON rr.race_key=s.race_key
                    WHERE 1=1
            """
            params = []
            if cutoff_date:
                if int(cutoff_race_no or 0) > 0:
                    q += """ AND (
                        substr(rr.race_date,1,10) < substr(?,1,10)
                        OR (
                            substr(rr.race_date,1,10) = substr(?,1,10)
                            AND CAST(REPLACE(REPLACE(COALESCE(rr.race_no,''),'R',''),'r','') AS INTEGER) < ?
                        )
                    )"""
                    params.extend([str(cutoff_date)[:10], str(cutoff_date)[:10], int(cutoff_race_no)])
                else:
                    q += " AND substr(rr.race_date,1,10) < substr(?,1,10)"
                    params.append(str(cutoff_date)[:10])
            if venue:
                q += " AND rr.venue = ?"
                params.append(str(venue))
            q += """
                    GROUP BY s.race_key, s.car_no
                )
                SELECT rr.race_date, rr.venue, re.car_no, re.handicap,
                       CAST(re.race_time AS REAL) AS actual_time,
                       CAST(s.predicted_race_time AS REAL) AS pred_time,
                       COALESCE(re.result_status,'通常') AS result_status,
                       rr.race_key
                FROM latest l
                JOIN v266_pred_time_snapshots s
                  ON s.history_id=l.history_id
                 AND s.race_key=l.race_key
                 AND s.car_no=l.car_no
                JOIN result_races rr ON rr.race_key=s.race_key
                JOIN result_entries re
                  ON re.race_key=s.race_key
                 AND CAST(re.car_no AS INTEGER)=CAST(s.car_no AS INTEGER)
                WHERE re.race_time IS NOT NULL
                  AND CAST(re.race_time AS REAL) BETWEEN 3.20 AND 4.50
                  AND COALESCE(re.result_status,'通常') NOT IN (
                      '欠車','出走取消','発走除外','競走除外',
                      '落車','競走中止','失格','反則','反妨','周誤','周回誤認'
                  )
            """
            rows = con.execute(q, params).fetchall()
    except Exception as exc:
        out = dict(empty)
        out["reason"] = f"読込失敗: {type(exc).__name__}: {exc}"
        _V268_HANDICAP_MODEL_CACHE[key] = dict(out)
        return out

    xs, ys, group_keys = [], [], []
    for rd, vv, car, h, actual, pred, status, race_key in rows:
        try:
            m = re.search(r"-?\d+", str(h if h is not None else "0"))
            hv = float(m.group()) if m else 0.0
            a = float(actual)
            p = float(pred)
            err = a - p
            if not np.isfinite(hv) or not np.isfinite(err) or abs(err) > 0.20:
                continue
            xs.append(hv)
            ys.append(err)
            group_keys.append(str(race_key or f"{rd}|{vv}"))
        except Exception:
            continue

    n = len(xs)
    if n < 30 or len(set(xs)) < 3:
        out = dict(empty)
        out["samples"] = n
        out["reason"] = f"学習対象{n}走（30走未満またはハンデ種類不足）"
        _V268_HANDICAP_MODEL_CACHE[key] = dict(out)
        return out

    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)

    def _fit(xtr, ytr):
        xm = float(np.mean(xtr))
        ym = float(np.mean(ytr))
        den = float(np.sum((xtr-xm)**2))
        raw_slope = float(np.sum((xtr-xm)*(ytr-ym))/den) if den > 1e-12 else 0.0
        raw_intercept = ym - raw_slope*xm
        shrink = float(min(0.78, max(0.35, len(xtr)/(len(xtr)+40.0))))
        slope = raw_slope * shrink
        intercept = raw_intercept * min(0.55, shrink)
        return slope, intercept, raw_slope, raw_intercept, shrink

    slope, intercept, raw_slope, raw_intercept, shrink = _fit(x, y)

    # Stable deterministic 5-fold grouped by race_key.
    import hashlib as _hashlib
    folds = np.asarray(
        [int(_hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16) % 5 for g in group_keys],
        dtype=int
    )
    before, after = [], []
    for f in range(5):
        tr = folds != f
        te = folds == f
        if int(np.sum(tr)) < 20 or int(np.sum(te)) < 3:
            continue
        ss, ii, _, _, _ = _fit(x[tr], y[tr])
        pred_err = np.clip(ii + ss*x[te], -0.032, 0.018)
        before.extend(np.abs(y[te]).tolist())
        after.extend(np.abs(y[te] - pred_err).tolist())

    mae_before = float(np.mean(before)) if before else float(np.mean(np.abs(y)))
    mae_after = float(np.mean(after)) if after else None
    improvement = (
        (mae_before - mae_after) / mae_before * 100.0
        if mae_after is not None and mae_before > 1e-12 else 0.0
    )
    enabled = bool(mae_after is not None and improvement >= 3.0)

    out = {
        "enabled": enabled,
        "samples": n,
        "venue": str(venue or ""),
        "slope": float(slope),
        "intercept": float(intercept),
        "raw_slope": float(raw_slope),
        "raw_intercept": float(raw_intercept),
        "shrink": float(shrink),
        "mae_before": float(mae_before),
        "mae_after_cv": float(mae_after) if mae_after is not None else None,
        "improvement_pct": float(improvement),
        "reason": (
            f"CV改善{improvement:.1f}%で採用"
            if enabled else
            f"CV改善{improvement:.1f}%のため保留"
        ),
    }
    _V268_HANDICAP_MODEL_CACHE[key] = dict(out)
    return out



def _v270_chase_gate(
    handicap_m: float,
    trial_time: float | None,
    field_trial_median: float | None,
    v265_adjust_sec: float,
    v265_samples: int,
) -> tuple[float, dict]:
    """
    後方ハンデの一律持ち上げを抑えるゲート。
    Ver268ハンデ補正のうち「速くする方向(負秒)」だけを0.45～1.00倍する。

    判断材料:
      1) Ver265の選手別/条件別残差補正
         - 過去に予測より速く走る傾向が十分あれば追い切り側
         - 逆なら後方ハンデ補正を弱める
      2) 当日の試走Tがメンバー中央値より良いか
         - 後方から追う根拠として小さく加点

    未来結果は使わず、既存の時系列cutoffをそのまま利用する。
    """
    try:
        h = float(handicap_m or 0.0)
    except Exception:
        h = 0.0

    # 20m以下は従来補正をほぼそのまま。問題になりやすい30m以上を主対象。
    if h < 30.0:
        return 1.0, {"gate": 1.0, "reason": "front_or_mid"}

    gate = 0.72
    reasons = []

    try:
        adj = float(v265_adjust_sec or 0.0)
    except Exception:
        adj = 0.0
    try:
        sn = int(v265_samples or 0)
    except Exception:
        sn = 0

    # 十分な履歴がある時だけ個人残差を使う。
    if sn >= 5:
        # 負の残差補正 = 過去に予測より速く走る傾向。
        if adj <= -0.018:
            gate += 0.18; reasons.append("strong_chaser_history")
        elif adj <= -0.008:
            gate += 0.10; reasons.append("chaser_history")
        elif adj >= 0.012:
            gate -= 0.15; reasons.append("weak_chase_history")
        elif adj >= 0.006:
            gate -= 0.08; reasons.append("slightly_weak_history")

    # 当日の試走優位は補助材料。極端に効かせない。
    try:
        tt = float(trial_time)
        med = float(field_trial_median)
        if np.isfinite(tt) and np.isfinite(med):
            adv = med - tt  # positive = faster trial
            if adv >= 0.045:
                gate += 0.10; reasons.append("strong_trial")
            elif adv >= 0.025:
                gate += 0.06; reasons.append("good_trial")
            elif adv <= -0.035:
                gate -= 0.08; reasons.append("weak_trial")
    except Exception:
        pass

    # 60m+は渋滞・捌きの不確実性が大きいので、根拠なしの全開補正を抑える。
    if h >= 60.0 and not any(r in reasons for r in ("strong_chaser_history","chaser_history")):
        gate -= 0.08
        reasons.append("deep_handicap_uncertainty")

    gate = float(np.clip(gate, 0.45, 1.00))
    return gate, {"gate": gate, "reason": ",".join(reasons) if reasons else "neutral"}


def _v268_handicap_bias_seconds(model: dict, handicap_value: float) -> float:
    if not model or not model.get("enabled"):
        return 0.0
    try:
        h = float(handicap_value or 0.0)
        # error = actual - predicted。これを予測Tへそのまま加える。
        adj = float(model.get("intercept",0.0)) + float(model.get("slope",0.0))*h
        return float(np.clip(adj, -0.032, 0.018))
    except Exception:
        return 0.0



# ---------------------------------------------------------------------------
# Ver271: 3～4周目の追い抜き・壁突破を保守的に調整
# Ver271の「追い切りゲート」を利用し、後方ハンデ勢の中盤だけを微調整する。
# 新しい大きな独立補正は作らず、序盤1～2周と終盤5～6周は原則そのまま。
# ---------------------------------------------------------------------------

def _v271_mid_lap_pass_factor(
    lap_no: int,
    handicap_m: float,
    chase_gate: float,
    trial_time: float | None,
    field_trial_median: float | None,
) -> tuple[float, str]:
    try:
        lap = int(lap_no)
    except Exception:
        lap = 0
    if lap not in (3, 4):
        return 1.0, "outside_mid_lap"

    try:
        h = float(handicap_m or 0.0)
    except Exception:
        h = 0.0
    try:
        gate = float(chase_gate or 1.0)
    except Exception:
        gate = 1.0

    # 0～20mは中盤捌き補正の対象外。
    if h < 30.0:
        return 1.0, "front_or_mid"

    factor = 1.0
    reasons = []

    # Ver271で「追える根拠」が弱い後方車は3～4周目の追い抜きを抑える。
    if gate < 0.60:
        factor *= 0.84
        reasons.append("weak_chase_gate")
    elif gate < 0.75:
        factor *= 0.92
        reasons.append("moderate_chase_gate")
    elif gate >= 0.92:
        factor *= 1.05
        reasons.append("strong_chase_gate")

    # 当日の試走が良い場合だけ、中盤の捌き成功率を小さく上乗せ。
    try:
        tt = float(trial_time)
        med = float(field_trial_median)
        if np.isfinite(tt) and np.isfinite(med):
            adv = med - tt
            if adv >= 0.040:
                factor *= 1.05
                reasons.append("strong_trial")
            elif adv <= -0.035:
                factor *= 0.94
                reasons.append("weak_trial")
    except Exception:
        pass

    # 60m以上は中盤の渋滞・複数捌き不確実性を追加で抑える。
    if h >= 60.0 and gate < 0.90:
        factor *= 0.93
        reasons.append("deep_handicap")

    # 過学習防止。±18%以内。
    factor = float(np.clip(factor, 0.82, 1.08))
    return factor, ",".join(reasons) if reasons else "neutral"



def _v272_late_chase_release(lap_no, handicap_m, chase_gate, trial_time, field_trial_median, residual_adjust_sec, residual_samples):
    """5～6周目だけ、追える根拠が複数ある後方車へ小さな再加速を許す。"""
    try:
        lap=int(lap_no); h=float(handicap_m or 0); gate=float(chase_gate or 1)
        adj=float(residual_adjust_sec or 0); sn=int(residual_samples or 0)
    except Exception:
        return 1.0
    if lap not in (5,6) or h < 30:
        return 1.0
    score=0
    if sn>=5 and adj<=-0.012: score+=2
    elif sn>=5 and adj<=-0.006: score+=1
    if gate>=0.90: score+=2
    elif gate>=0.80: score+=1
    try:
        tt=float(trial_time); med=float(field_trial_median)
        if np.isfinite(tt) and np.isfinite(med):
            adv=med-tt
            if adv>=0.035: score+=1
            elif adv<=-0.040: score-=1
    except Exception:
        pass
    threshold=4 if h>=60 else 3
    if score<threshold:
        return 1.0
    if score>=threshold+2:
        return 1.08 if lap==6 else 1.06
    return 1.05 if lap==6 else 1.035


def _v230_six_lap_simulation(df: pd.DataFrame, bets: dict, entries: pd.DataFrame, meta: dict, trials: int, seed: int):
    """1試行ごとにスタートと6周の壁・追い抜きを枝分かれさせるベータ版。"""
    if not isinstance(df,pd.DataFrame) or df.empty or not isinstance(entries,pd.DataFrame) or entries.empty:
        return df,bets,{"enabled":False,"reason":"入力不足"}
    car_col="車" if "車" in df.columns else ("車番" if "車番" in df.columns else None)
    ec="車番" if "車番" in entries.columns else ("車" if "車" in entries.columns else None)
    if not car_col or not ec:
        return df,bets,{"enabled":False,"reason":"車番列なし"}
    work=df.copy(); work["_car"]=pd.to_numeric(work[car_col],errors="coerce")
    work=work.dropna(subset=["_car"]).copy(); work["_car"]=work["_car"].astype(int)
    emap={}
    for _,r in entries.iterrows():
        try: emap[int(r[ec])]=r
        except Exception: pass
    cars=work["_car"].astype(int).tolist()
    if len(cars)<3: return df,bets,{"enabled":False,"reason":"3車未満"}
    names=[]; handicap={}; stmean={}; trial={}; strength={}; breakthrough={}
    # 既存三連単分布の1着周辺確率を基礎能力にする。
    tri=(bets or {}).get("三連単",{}) or {}
    win_counts={c:0.0 for c in cars}
    total=max(1.0,float(sum(tri.values()) or trials or 1))
    for combo,cnt in tri.items():
        try: win_counts[int(combo[0])] += float(cnt)
        except Exception: pass
    for _,r in work.iterrows():
        c=int(r["_car"]); er=emap.get(c)
        name=""
        for src in (r,er):
            if src is not None:
                for k in ("選手名","名前","player_name"):
                    if k in src.index and str(src.get(k,"")).strip(): name=_v230_norm_name(src.get(k)); break
            if name: break
        names.append(name or str(c))
        h=0
        if er is not None:
            m=re.search(r"-?\d+",str(er.get("ハンデ",er.get("H",0))))
            h=int(m.group()) if m else 0
        handicap[c]=h
        st=_v230_col_num(er,("ST","平均ST","st"),0.16) if er is not None else 0.16
        if st<=0 or st>0.5: st=0.16
        stmean[c]=st
        tt=_v230_col_num(er,("試走T","試走タイム","trial_time"),3.40) if er is not None else 3.40
        if tt<=0: tt=3.40
        trial[c]=tt
        base=max(0.01,win_counts.get(c,0.0)/total)
        # 既存の実戦・展開点も少量混ぜる。
        bonus=0.0
        for k,w in (("実戦能力点",0.002),("展開適性点",0.0015),("基礎スピード点",0.001)):
            if k in r.index: bonus += _v230_num(r.get(k),0.0)*w
        # 旧モデルの勝率は土台として使うが、6周展開の結果を自己増幅しないよう圧縮する。
        # これにより、以前の前残り評価が高い車を新シミュレーションでも過剰固定するのを防ぐ。
        # 旧予測確率を強く再利用すると、誤った本命が6周すべてで自己増幅する。
        # Ver239では旧確率を弱い事前分布へ落とし、試走・履歴・各試行の出来で展開を決める。
        strength[c]=0.30*np.log(base+0.040)+bonus-(tt-3.40)*1.85
        raw=np.mean([_v230_num(r.get(k),0.0) for k in ("混戦突破適性","展開適性点","実戦能力点") if k in r.index] or [0.0])
        breakthrough[c]=raw
    # 正規化
    vals=np.array(list(strength.values()),dtype=float); mu=float(vals.mean()); sd=float(vals.std() or 1.0)
    # 少数車の小差を標準化だけで巨大差へしない。縮小して上限を設ける。
    strength={c:max(-1.55,min(1.55,0.72*((v-mu)/sd))) for c,v in strength.items()}
    bvals=np.array(list(breakthrough.values()),dtype=float); blo=float(bvals.min()); bhi=float(bvals.max())
    breakthrough={c:(0.5 if bhi<=blo else (v-blo)/(bhi-blo)) for c,v in breakthrough.items()}
    venue=str((meta or {}).get("開催場") or (meta or {}).get("venue") or "")
    _v242_prepare_started=time_module.perf_counter()
    profiles=_v230_hist_profiles(venue,names)
    transition_profiles=_v240_transition_profiles(venue,names)
    matchups=_v230_matchup_map(names)
    wall_calibration=_v248_wall_calibration(_v230_db_path())
    venue_wall_delta=float((wall_calibration.get("venue_delta") or {}).get(venue,0.0)) if wall_calibration.get("enabled") else 0.0
    lap_wall_delta=wall_calibration.get("lap_delta") or {}
    race_date=str((meta or {}).get("開催日") or (meta or {}).get("race_date") or "")[:10]
    flow_calibration=_v250_flow_calibration(_v230_db_path(), venue, race_date)
    leader_hold_delta=float(flow_calibration.get("leader_hold_delta",0.0)) if flow_calibration.get("enabled") else 0.0
    front_survival_delta=float(flow_calibration.get("front_survival_delta",0.0)) if flow_calibration.get("enabled") else 0.0
    queue_wall_delta=float(flow_calibration.get("queue_wall_delta",0.0)) if flow_calibration.get("enabled") else 0.0
    lap_alignment=_v251_lap_alignment_calibration(_v230_db_path(), venue, race_date)
    lap_bucket_delta=lap_alignment.get("lap_bucket_delta") or {}
    lap_residual=_v252_lap_residual_calibration(_v230_db_path(), venue, race_date)
    lap_residual_delta=lap_residual.get("delta") or {}
    actual_lap_calibration=_v256_actual_lap_calibration(_v230_db_path(), venue, race_date)
    actual_lap_delta=actual_lap_calibration.get("lap_delta") or {}
    player_lap_calibration=_v254_player_lap_calibration(_v230_db_path(), venue, race_date)
    player_lap_delta=player_lap_calibration.get("player_delta") or {}
    player_actual_calibration=_v258_player_actual_lap_calibration(_v230_db_path(), venue, race_date)
    player_actual_delta=player_actual_calibration.get("player_delta") or {}
    time_residual_v265=_v265_time_residual_calibration(_v230_db_path(), venue, race_date)
    _race_no_v269 = 0
    try:
        _race_no_raw = str(meta.get("R") or meta.get("レース") or meta.get("レース番号") or meta.get("race_no") or meta.get("race") or "")
        _race_no_m = re.search(r"\d+", _race_no_raw)
        _race_no_v269 = int(_race_no_m.group()) if _race_no_m else 0
    except Exception:
        _race_no_v269 = 0
    handicap_bias_v268=_v268_handicap_bias_model(_v230_db_path(), venue, race_date, _race_no_v269)
    time_adjust_v265={}
    time_expected_v265={}
    time_samples_v265={}
    handicap_adjust_v268={}
    chase_gate_v270={}
    chase_reason_v270={}
    try:
        _trial_vals_v270 = [
            float(v) for v in trial_map.values()
            if v is not None and np.isfinite(float(v))
        ]
        _trial_median_v270 = float(np.median(_trial_vals_v270)) if _trial_vals_v270 else None
    except Exception:
        _trial_median_v270 = None
    for _,_rr in work.iterrows():
        _c=int(_rr["_car"])
        _nm=next((n for c,n in zip(cars,names) if c==_c),'')
        _pred=_v230_num(_rr.get("予測競走T"),0.0) if "予測競走T" in _rr.index else 0.0
        _trial=trial.get(_c,0.0)
        if _pred>0 and _trial>0:
            _adj,_exp,_sn=_v265_time_adjustment_seconds(time_residual_v265,_nm,handicap.get(_c,0),_trial,_pred)
            time_adjust_v265[_c]=_adj; time_expected_v265[_c]=_exp; time_samples_v265[_c]=_sn
            _h_adj=_v268_handicap_bias_seconds(handicap_bias_v268, handicap.get(_c,0))
            _gate270,_gate_meta270=_v270_chase_gate(
                handicap.get(_c,0), _trial, _trial_median_v270, _adj, _sn
            )
            # 後方ハンデを速くする補正だけゲート。遅くする補正はそのまま。
            _h_adj_gated = float(_h_adj) * float(_gate270) if float(_h_adj) < 0 else float(_h_adj)
            handicap_adjust_v268[_c]=_h_adj_gated
            chase_gate_v270[_c]=float(_gate270)
            chase_reason_v270[_c]=str(_gate_meta270.get("reason",""))
            _total_adj=float(_adj)+float(_h_adj_gated)
            strength[_c]=max(-1.70,min(1.70,strength[_c]-12.0*_total_adj))
            # Ver271の中盤補正値は周回イベント側で参照するため保持。
    actual_lap_orders=_v251_actual_lap_orders(_v230_db_path(), meta)
    scenario_prior=_v263_scenario_prior(_v230_db_path(), venue, race_date)
    scenario_feedback_v264=_v264_feedback_scenario_adjustment(_v230_db_path(), venue, race_date)
    scenario_branch_prior_v264=_v264_blended_scenario_prior(scenario_prior, scenario_feedback_v264)
    _v242_prepare_seconds=time_module.perf_counter()-_v242_prepare_started
    name_by_car={c:n for c,n in zip(cars,names)}
    rng=np.random.default_rng(int(seed)+230)
    _v242_sim_started=time_module.perf_counter()
    requested_trials=max(1000,min(int(trials),20000))
    # Ver237: 20,000回すべてで重い6周処理を行わず、車立てと旧分布の集中度から
    # 必要な展開試行数を自動決定する。最終確率は要求試行数へ再スケールするため、
    # UI・DB上の確率母数は従来設定を維持する。
    base_budget={6:1900,7:2500,8:3200}.get(len(cars),2700)
    prior_ranked=sorted((float(v) for v in tri.values()), reverse=True)
    top_mass=(sum(prior_ranked[:12])/max(1.0,float(sum(prior_ranked)))) if prior_ranked else 0.0
    if top_mass < 0.35:
        base_budget += 700  # 混戦だけ追加試行
    elif top_mass > 0.65:
        base_budget -= 350  # 強く集中したレースは早めに収束
    sim_trials=max(1400,min(requested_trials,base_budget))
    counts={}; wall_events={c:0 for c in cars}; pass_events={c:0 for c in cars}; start_front={c:0 for c in cars}
    lap_order_counts={lap:{} for lap in range(1,7)}
    scenario_counts={}
    scenario_combo_counts={}
    route_counts={}
    # 追い抜き成功後の勢い。壁を抜いた車が次の車にも迫る展開を試行ごとに保持する。
    chain_events={c:0 for c in cars}
    # 初期の物理位置。10mを約0.17秒差へ換算し、同ハンデは内枠優先。
    base_order=sorted(cars,key=lambda c:(handicap[c],c))
    
    # Ver243: 上位展開の分布が十分に安定したら早期終了する。
    # 本命集中レースを無駄に最後まで回さず、混戦時は設定上限まで継続する。
    planned_trials = sim_trials
    convergence_checks = 0
    previous_signature = None
    completed_trials = 0
    _scenario_types=['標準型']
    _scenario_probs=np.array([1.0],dtype=float)
    for sim_index in range(planned_trials):
        # Ver264: 各試行で展開型を先にサンプルし、その型に応じて各周の分岐確率を小さく変える。
        target_scenario='標準型'
        race_noise=rng.normal(0,0.10)
        indiv_sd=0.36
        perf={c:max(-2.2,min(2.2,strength[c]+race_noise+rng.normal(0,indiv_sd))) for c in cars}
        min_handicap=min(handicap.values()) if handicap else 0
        # 前ハンデ残りは開催日前の実績だけを使い、1試行の能力へ小さく反映。
        for c in cars:
            if handicap[c] == min_handicap:
                perf[c]=max(-2.2,min(2.2,perf[c]+0.34*front_survival_delta))
        # スタート反応はST・履歴・ランダムで毎試行変える。
        start_score={}
        for c in cars:
            hp=profiles.get(name_by_car[c],{})
            # 内枠は同ハンデ時だけ僅かに有利。ただしSTと当試行の出来で十分逆転する。
            same_group=sorted([x for x in cars if handicap[x]==handicap[c]])
            lane_bonus=(len(same_group)-same_group.index(c)-1)*0.018 if c in same_group else 0.0
            start_score[c]=(-stmean[c]*4.7 + hp.get("first_gain",0.0)*0.085 + 0.10*perf[c] + lane_bonus + rng.normal(0,0.48))
        # 同ハンデ内だけスタートで並び替え。ハンデ差は初期距離として保持。
        order=[]
        for h in sorted(set(handicap.values())):
            group=[c for c in cars if handicap[c]==h]
            group.sort(key=lambda c:start_score[c],reverse=True)
            order.extend(group)
        start_front[order[0]]+=1
        gaps=[0.0]
        for i in range(1,len(order)):
            prev,cur=order[i-1],order[i]
            dh=max(0,handicap[cur]-handicap[prev])
            gaps.append(0.11+0.017*dh+rng.uniform(0.00,0.08))
        # 6周。後車から前車へ隣接追い抜き判定。
        sim_lap_path=[]
        momentum={c:0.0 for c in cars}
        # ギリギリ横並びで抜かれた車の一時失速。次周以降へ減衰して残す。
        slowdown={c:0.0 for c in cars}
        for lap in range(1,7):
            # Ver250: 1周で何台も連続して抜く展開を抑える。
            # 速い車でも進路変更と立て直しが必要なため、周回内の追抜回数を保持する。
            lap_pass_count={c:0 for c in cars}
            i=1
            while i<len(order):
                front=order[i-1]; chaser=order[i]
                fn=name_by_car[front]; cn=name_by_car[chaser]
                hp=profiles.get(cn,{}); fprof=profiles.get(fn,{})
                adv,conf=matchups.get((cn,fn),(0.0,0.0))
                density=max(0,len(order)-i-1)/max(1,len(order)-1)
                # 静的能力ではなく、その試行で発揮された能力差を使う。
                ability=((perf[chaser]+momentum.get(chaser,0.0))-(perf[front]-slowdown.get(front,0.0)))*0.46
                tp=transition_profiles.get(cn,{})
                ftp=transition_profiles.get(fn,{})
                lap_attack=(tp.get("lap_attack") or [0.0]*7)[lap] if lap < len(tp.get("lap_attack") or []) else 0.0
                hist=(hp.get("overtake",0.5)-0.5)*1.0 + (hp.get("chase",0.5)-0.5)*0.55 + lap_attack
                direct=max(-0.5,min(0.5,adv))*min(1.0,conf)*0.85
                # 壁は残すが、前車が明確に遅い場合まで一律に詰まらせない。
                speed_edge=max(-2.5,min(2.5,perf[chaser]-perf[front]))
                wall=0.34 + 0.15*density + 0.10*(1-breakthrough[chaser]) - 0.08*max(0.0,speed_edge)
                # Ver250: 同ハンデ群が前に重なる隊列と、前後とも間隔が狭い三台密集を強い壁として扱う。
                same_ahead=sum(1 for x in order[:i] if handicap[x] == handicap[chaser])
                same_group=sum(1 for x in order if handicap[x] == handicap[chaser])
                wall += min(0.18, 0.055*same_ahead + (0.045 if same_group >= 3 else 0.0))
                if i+1<len(order) and gaps[i+1]<0.20: wall += 0.10
                if gaps[i] < 0.22 and i+1 < len(order) and gaps[i+1] < 0.22:
                    wall += 0.08 + queue_wall_delta
                # 前車が履歴上よく粘るほど突破しにくい。終盤は少し追い抜きやすくする。
                # 先頭・前方にいるだけの最低保証は与えない。後車の速度優位と残り周回を強めに反映。
                front_hold=(fprof.get("hold",0.5)-0.5)*0.48
                # 先頭車だけに開催場別の維持補正を反映。終盤ほど効果を弱め、永久壁にはしない。
                if i == 1:
                    front_hold += leader_hold_delta * max(0.25, 1.0 - 0.13*(lap-1))
                weak_front_bonus=0.34*max(0.0,speed_edge-0.25)
                late_pressure=0.03*(lap-1)
                empirical_pass_delta = venue_wall_delta + float(lap_wall_delta.get(lap, 0.0))
                # 直前の追い抜き成功は次の壁突破を少し後押しする。ただし毎周減衰させる。
                chain_bonus=min(0.42, momentum.get(chaser,0.0))
                # Ver250: ハンデ差が大きい追込みほど、追い付いてから抜くまでの余白を必要とする。
                handicap_gap=max(0, handicap[chaser]-handicap[front])
                # Ver253: 固定のハンデ壁を弱め、実際の周回順位入替率を主に使う。
                handicap_wall=min(0.12, 0.0022*handicap_gap)
                bucket_key=f"{lap}|{_v251_gap_bucket(handicap_gap)}"
                learned_transition=float((lap_bucket_delta.get(bucket_key) or {}).get("delta",0.0)) if lap_alignment.get("enabled") else 0.0
                # Ver253: 過去予測が実測より抜き過ぎ/抜かな過ぎだった残差を直接補正。
                residual_transition=float((lap_residual_delta.get(bucket_key) or {}).get("delta",0.0)) if lap_residual.get("enabled") else 0.0
                # Ver256: 保存予測の有無に依存せず、全実測グランドノートの周回入替率を反映。
                actual_transition=float(actual_lap_delta.get(lap,0.0)) if actual_lap_calibration.get("enabled") else 0.0
                # Ver254: 同じ選手が同じ周回で一貫して予測より追い上げる/追い上げない残差を小さく反映。
                player_lap_key=f"{cn}|{lap}"
                player_transition=float((player_lap_delta.get(player_lap_key) or {}).get("delta",0.0)) if player_lap_calibration.get("enabled") else 0.0
                # Ver260: 保存予測の有無に依存しない全実測の選手×周回追抜率。
                # Ver254残差学習と同時に効き過ぎないよう、合算後も小さく制限する。
                player_actual_transition=float((player_actual_delta.get(player_lap_key) or {}).get("delta",0.0)) if player_actual_calibration.get("enabled") else 0.0
                player_total_transition=float(np.clip(player_transition,-0.08,0.08))
                # 2台目までは現実に起こり得るため軽く、3台目以降だけ強く抑える。
                same_lap_passes=lap_pass_count.get(chaser,0)
                chain_fatigue=(0.16 if same_lap_passes==1 else (0.52 if same_lap_passes>=2 else 0.0))
                # 前方3台に最前ハンデ群が複数残る場合、集団全体を一枚の壁として扱う。
                front_pack=sum(1 for x in order[:min(3,len(order))] if handicap[x]==min_handicap)
                pack_wall=(0.07*max(0,front_pack-1))*max(0.30,1.0-0.10*(lap-1))
                scenario_branch=0.0
                logit=-0.28 + ability*0.90 + hist*0.82 + direct*0.82 + late_pressure + empirical_pass_delta + learned_transition + residual_transition + actual_transition + player_total_transition + weak_front_bonus + chain_bonus - wall - front_hold - handicap_wall - chain_fatigue - pack_wall
                _branch_noise=0.16
                logit += rng.normal(0,_branch_noise)
                p=1/(1+np.exp(-logit))
                p=max(0.035,min(0.88,p))
                # Ver272: Ver271の3～4周目補正を実際の追い抜き確率へ接続。
                try:
                    _mid_factor272,_=_v271_mid_lap_pass_factor(
                        lap, handicap.get(chaser,0), chase_gate_v270.get(chaser,1.0),
                        trial_map.get(chaser), _trial_median_v270)
                    p=float(np.clip(p*_mid_factor272,0.035,0.88))
                except Exception:
                    pass
                # Ver272: 5～6周目は追える根拠が複数ある後方車だけ再加速。
                try:
                    _late_factor272=_v272_late_chase_release(
                        lap, handicap.get(chaser,0), chase_gate_v270.get(chaser,1.0),
                        trial_map.get(chaser), _trial_median_v270,
                        time_adjust_v265.get(chaser,0.0), time_samples_v265.get(chaser,0))
                    p=float(np.clip(p*_late_factor272,0.035,0.88))
                except Exception:
                    pass
                # 差が開きすぎていればまず追いつく必要がある。
                # 大差なら即追越しは難しいが、速度優位車はまず差を詰められる。
                catch_factor=max(0.10,1.0-min(0.78,gaps[i]*1.12))
                catch_factor=min(1.15,catch_factor+0.10*max(0.0,speed_edge))
                p*=catch_factor
                if rng.random()<p:
                    order[i-1],order[i]=order[i],order[i-1]
                    gaps[i]=max(0.07,gaps[i]*0.45)
                    pass_events[chaser]+=1
                    lap_pass_count[chaser]=lap_pass_count.get(chaser,0)+1
                    if momentum.get(chaser,0.0)>0.02:
                        chain_events[chaser]+=1
                    # 差が小さい横並びの追越しほど、抜かれた側がラインを外して一時失速しやすい。
                    close_pass=max(0.0, min(1.0, (0.24-gaps[i])/0.18))
                    pass_boost=float(tp.get("pass_momentum",0.14))
                    loss_base=float(ftp.get("passed_slowdown",0.18))
                    cascade=float(ftp.get("cascade_risk",0.16))
                    momentum[chaser]=min(0.48, momentum.get(chaser,0.0)+pass_boost*(0.65+0.55*close_pass))
                    if lap_pass_count.get(chaser,0)>=2:
                        momentum[chaser]*=0.58
                    slowdown[front]=min(0.50, slowdown.get(front,0.0)+loss_base*(0.45+0.90*close_pass)+0.08*cascade)
                    momentum[front]=max(0.0, momentum.get(front,0.0)-0.10*(0.5+close_pass))
                    i=max(1,i-1)
                else:
                    wall_events[chaser]+=1
                    momentum[chaser]*=0.55
                    # 壁で止まった間に先頭側との差が広がる。能力差があれば少し縮む。
                    delta=0.040+0.026*density-0.024*max(-1.5,min(1.5,ability))
                    # 前車が遅い場合は「壁で永久に止まる」のではなく、失敗周でも距離を詰める。
                    if speed_edge>0.45:
                        delta-=0.020*min(2.0,speed_edge)
                    gaps[i]=min(1.8,max(0.05,gaps[i]+delta+rng.normal(0,0.012)))
                    i+=1
            # 周回ごとの純粋な伸びで差を更新。位置交換は追い抜き判定だけで行う。
            for c in cars:
                momentum[c]*=0.72
                slowdown[c]*=0.58
            for j in range(1,len(order)):
                front,cur=order[j-1],order[j]
                rel=((perf[cur]+momentum.get(cur,0.0))-(perf[front]-slowdown.get(front,0.0)))*0.026 + rng.normal(0,0.022)
                # 弱い先頭車が捕まった後に後続も連続して迫る現象を反映。
                if j==1 and perf[cur]-perf[front]>0.35:
                    rel += 0.012*(1.0+0.15*lap)
                gaps[j]=min(2.0,max(0.04,gaps[j]-rel))
            lap_tuple=tuple(order)
            sim_lap_path.append(lap_tuple)
            lap_order_counts[lap][lap_tuple]=lap_order_counts[lap].get(lap_tuple,0)+1
        combo=tuple(order[:3]); counts[combo]=counts.get(combo,0)+1
        scenario_type=_v263_scenario_type_from_laps(sim_lap_path)
        scenario_counts[scenario_type]=scenario_counts.get(scenario_type,0)+1
        sc=scenario_combo_counts.setdefault(scenario_type,{})
        sc[combo]=sc.get(combo,0)+1
        route_key=tuple(sim_lap_path)
        route_counts[route_key]=route_counts.get(route_key,0)+1
        completed_trials = sim_index + 1
        if completed_trials >= 1200 and completed_trials % 400 == 0:
            ranked_now = sorted(counts.values(), reverse=True)[:12]
            denom_now = max(1, completed_trials)
            signature = tuple(round(v / denom_now, 4) for v in ranked_now)
            if previous_signature is not None and len(signature) == len(previous_signature):
                drift = sum(abs(a-b) for a,b in zip(signature, previous_signature))
                if drift < 0.012:
                    convergence_checks += 1
                else:
                    convergence_checks = 0
            previous_signature = signature
            if convergence_checks >= 2 and top_mass >= 0.48:
                break
    sim_trials=max(1, completed_trials)
    _v242_sim_seconds=time_module.perf_counter()-_v242_sim_started
    # Ver263: 実測から学んだ展開タイプ頻度へ弱く校正。
    weighted_counts=dict(counts)
    scenario_weights={}
    if scenario_prior.get('enabled') and scenario_counts:
        learned=scenario_prior.get('prior') or {}
        weighted_counts={k:0.0 for k in counts}
        for typ,cc in scenario_combo_counts.items():
            generated=float(scenario_counts.get(typ,0))/max(1,sim_trials)
            target_prior=float(learned.get(typ,generated or 0.01))
            ratio=(target_prior/max(0.01,generated))**0.12
            w=float(np.clip(ratio,0.92,1.08))
            scenario_weights[typ]=w
            for combo,n in cc.items():
                weighted_counts[combo]=weighted_counts.get(combo,0.0)+float(n)*w
    # 元のtrial数へ整数スケール。シミュレーションだけで0回になった着順も、
    # 旧モデル分布を少量混ぜて極端な消失を防ぎ、全組み合わせを必ず保存する。
    target=max(1,int(trials))
    all_combos=[(a,b,c) for a in cars for b in cars for c in cars if a!=b and a!=c and b!=c]
    prior_total=max(1.0,float(sum(tri.values()) or 1.0))
    # Ver241: 失速連鎖を含む6周結果を尊重しつつ、有限試行の偶然と過信を温度校正する。
    sim_mix=0.88
    floor_mass=0.03 / max(1,len(all_combos))
    scaled={}
    for combo in all_combos:
        sim_p=float(weighted_counts.get(combo,0))/max(1.0,float(sum(weighted_counts.values()) or sim_trials))
        prior_p=float(tri.get(combo,0))/prior_total
        # 7%だけ旧能力分布を残し、未知着順にもごく小さい裾を与える。
        p=max(floor_mass, sim_mix*sim_p + (1.0-sim_mix)*prior_p)
        # 温度校正。上位の山を少し低くし、現実的な着順違いの裾を残す。
        scaled[combo]=p**0.86
    norm=sum(scaled.values()) or 1.0
    scaled={k:(v/norm)*target for k,v in scaled.items()}
    ints={k:int(v) for k,v in scaled.items()}
    remain=target-sum(ints.values())
    if remain>0:
        ranked=sorted(scaled.items(),key=lambda kv:kv[1]-int(kv[1]),reverse=True)
        for k,_ in ranked[:remain]: ints[k]+=1
    new_bets=dict(bets or {}); new_bets["三連単"]=ints
    tf={}; nt={}; nf={}
    for (a,b,c),cnt in ints.items():
        tf[tuple(sorted((a,b,c)))]=tf.get(tuple(sorted((a,b,c))),0)+cnt
        nt[(a,b)]=nt.get((a,b),0)+cnt
        nf[tuple(sorted((a,b)))]=nf.get(tuple(sorted((a,b))),0)+cnt
    new_bets["三連複"]=tf; new_bets["2連単"]=nt; new_bets["2連複"]=nf
    out=df.copy()
    out["6周壁遭遇率"]=out[car_col].map(lambda x: wall_events.get(int(x),0)/(sim_trials*6)*100 if pd.notna(x) else 0.0)
    out["6周追抜成功回数"]=out[car_col].map(lambda x: pass_events.get(int(x),0)/sim_trials if pd.notna(x) else 0.0)
    out["1周目先頭率"]=out[car_col].map(lambda x: start_front.get(int(x),0)/sim_trials*100 if pd.notna(x) else 0.0)
    out["連続追抜発生回数"]=out[car_col].map(lambda x: chain_events.get(int(x),0)/sim_trials if pd.notna(x) else 0.0)
    out["Ver265タイム残差補正秒"]=out[car_col].map(lambda x: time_adjust_v265.get(int(x),0.0) if pd.notna(x) else 0.0)
    out["Ver268ハンデ残差補正秒"]=out[car_col].map(lambda x: handicap_adjust_v268.get(int(x),0.0) if pd.notna(x) else 0.0)
    out["Ver271追い切りゲート"]=out[car_col].map(lambda x: chase_gate_v270.get(int(x),1.0) if pd.notna(x) else 1.0)
    out["Ver271追い切り根拠"]=out[car_col].map(lambda x: chase_reason_v270.get(int(x),"") if pd.notna(x) else "")

    out["Ver271中盤補正対象"]=out[car_col].map(
        lambda x: "3-4周目" if (pd.notna(x) and float(handicap.get(int(x),0) or 0)>=30.0) else "対象外"
    )
    if "予測競走T" in out.columns:
        out["Ver265補正後予測競走T"]=pd.to_numeric(out["予測競走T"],errors="coerce") + out["Ver265タイム残差補正秒"]
        out["Ver268補正後予測競走T"]=(
            pd.to_numeric(out["予測競走T"],errors="coerce")
            + out["Ver265タイム残差補正秒"]
            + out["Ver268ハンデ残差補正秒"]
        )
    out["Ver265タイム学習件数"]=out[car_col].map(lambda x: time_samples_v265.get(int(x),0) if pd.notna(x) else 0)
    out["Ver268ハンデ学習件数"]=int(handicap_bias_v268.get("samples",0) or 0)
    top=sorted(ints.items(),key=lambda kv:kv[1],reverse=True)[:5]
    modal_laps=[]
    for lap in range(1,7):
        counter=lap_order_counts.get(lap) or {}
        if counter:
            order_mode,n=max(counter.items(),key=lambda kv:kv[1])
            modal_laps.append({"lap":lap,"order":"-".join(map(str,order_mode)),"support":n/max(1,sim_trials)*100})
    lap_comparison=[]
    if actual_lap_orders:
        for idx,(label,actual) in enumerate(actual_lap_orders[:6],start=1):
            pred=tuple(int(x) for x in modal_laps[idx-1]["order"].split('-')) if idx<=len(modal_laps) else tuple()
            pos_acc=(sum(1 for a,b in zip(pred,actual) if a==b)/max(1,len(actual))) if pred else 0.0
            lap_comparison.append({"lap":idx,"label":label,"actual":"-".join(map(str,actual)),"predicted":"-".join(map(str,pred)),"position_accuracy":pos_acc*100,"pairwise_accuracy":_v251_pairwise_accuracy(pred,actual)*100})
    # Ver263: 複数展開ルートと、実測に最も近かったルートを監査。
    scenario_distribution={k:float(v)/max(1,sim_trials)*100 for k,v in sorted(scenario_counts.items(),key=lambda kv:kv[1],reverse=True)}
    top_routes=[]
    actual_scenario='不明'; closest_similarity=0.0; closest_route=''
    actual_orders_only=[tuple(x[1]) for x in actual_lap_orders[:6]] if actual_lap_orders else []
    if actual_orders_only:
        actual_scenario=_v263_scenario_type_from_laps(actual_orders_only)
    for route,n in sorted(route_counts.items(),key=lambda kv:kv[1],reverse=True)[:12]:
        sim=float(_v263_route_similarity(route,actual_orders_only)) if actual_orders_only else 0.0
        route_text=' / '.join('-'.join(map(str,row)) for row in route)
        top_routes.append({'support':n/max(1,sim_trials)*100,'scenario':_v263_scenario_type_from_laps(route),'similarity':sim*100,'route':route_text})
        if sim>closest_similarity:
            closest_similarity=sim; closest_route=route_text
    if actual_orders_only:
        pass  # Ver265: 展開フィードバックは評価表示のみ
    # 実測が既にある再シミュレーションはバックテストとして保存し、未来学習には混ぜない。
    lap_snapshot_save=_v252_save_lap_prediction(_v230_db_path(), meta, modal_laps, bool(actual_lap_orders))
    audit={
        "enabled":True,"mode":"6周内蔵Ver265・Ver257基準＋タイム残差学習","sim_trials":sim_trials,"planned_trials":planned_trials,"requested_trials":requested_trials,
        "history_players":sum(1 for n in names if profiles.get(n,{}).get("sample",0)>0),
        "matchups":len(matchups)//2,
        "transition_players":sum(1 for n in names if transition_profiles.get(n,{}).get("sample",0)>0),
        "wall_calibration": wall_calibration,
        "flow_calibration": flow_calibration,
        "lap_alignment": lap_alignment,
        "lap_residual_learning": lap_residual,
        "actual_lap_learning_v256": actual_lap_calibration,
        "player_actual_learning_v258": {"enabled":False,"reason":"Ver265ではVer257基準へ戻すため予測反映停止"},
        "time_residual_learning_v265": time_residual_v265,
        "handicap_bias_v268": handicap_bias_v268,
        "walk_forward_cutoff_v269": {"date": race_date, "race_no": _race_no_v269},
        "chase_gate_v270": chase_gate_v270,
        "chase_reason_v270": chase_reason_v270,
        "time_adjustments_v265": {str(k):round(float(v),5) for k,v in time_adjust_v265.items()},
        "predicted_lap_orders": modal_laps,
        "actual_lap_comparison": lap_comparison,
        "lap_snapshot_save": lap_snapshot_save,
        "scenario_prior_v263": scenario_prior,
        "scenario_feedback_v264": scenario_feedback_v264,
        "scenario_branch_prior_v264": scenario_branch_prior_v264,
        "scenario_distribution_v263": scenario_distribution,
        "scenario_weights_v263": scenario_weights,
        "actual_scenario_v263": actual_scenario,
        "closest_route_similarity_v263": closest_similarity*100,
        "closest_route_v263": closest_route,
        "top_routes_v263": top_routes[:5],
        "top_scenarios":[{"combo":"-".join(map(str,k)),"prob":v/target*100} for k,v in top],
        "all_trifecta_combinations":len(ints),
        "prepare_seconds":round(_v242_prepare_seconds,3), "simulation_seconds":round(_v242_sim_seconds,3),
        "message":f"Ver265ではVer257相当の展開係数へ戻し、実測の試走→競走タイム変換残差を開催場・選手・ハンデ帯で縮小学習して基礎能力へ小さく反映します。要求{requested_trials:,}回、計画{planned_trials:,}回、実行{sim_trials:,}回。準備{_v242_prepare_seconds:.2f}秒／6周計算{_v242_sim_seconds:.2f}秒。全3連単を保存",
    }
    return out,new_bets,audit


def _v224_restore_nonstarter_rows(result_text: str, meta: dict, rows: pd.DataFrame) -> tuple[dict, pd.DataFrame, list[int]]:
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        return meta, rows, []

    raw = str(result_text or "")
    if not raw:
        return meta, rows, []

    # 公式結果では「- 4 選手名 ... /欠車」のように掲載される。
    # 改行・タブ・全角空白の揺れを許容して、事故語の直前にある車番を拾う。
    normalized = re.sub(r"[\t\u3000]+", " ", raw)
    accident_words = r"欠車|発走除外|競走除外|出走取消|出走取り消し"
    detected: list[tuple[int, str]] = []

    # まず事故語を含む周辺ブロックから「着順 - の次にある車番」を優先取得。
    block_pattern = re.compile(
        rf"(?:^|\n)\s*-\s*(?:\n|\s)+([1-8])(?:\s|\n)+(.{{0,160}}?)(?:/\s*)?({accident_words})(?=\s|$)",
        re.MULTILINE | re.DOTALL,
    )
    for m in block_pattern.finditer(normalized):
        car_no = int(m.group(1))
        reason = str(m.group(3))
        detected.append((car_no, reason))

    # 保存テキストの整形によって1行化されている場合の補助。
    if not detected:
        line_pattern = re.compile(rf"-\s*([1-8])\b[^\n]{{0,220}}?({accident_words})")
        for m in line_pattern.finditer(normalized):
            detected.append((int(m.group(1)), str(m.group(2))))

    if not detected:
        return meta, rows, []

    existing = set()
    if "車番" in rows.columns:
        existing = set(pd.to_numeric(rows["車番"], errors="coerce").dropna().astype(int).tolist())

    added: list[int] = []
    out = rows.copy()
    for car_no, reason in detected:
        if car_no in existing:
            # 既に行がある場合も事故欄だけ補完する。
            mask = pd.to_numeric(out["車番"], errors="coerce") == car_no if "車番" in out.columns else None
            if mask is not None:
                for col in ("事故", "異常", "異", "備考", "事故内容"):
                    if col in out.columns:
                        out.loc[mask, col] = "事前除外"
            continue

        row = {col: None for col in out.columns}
        if "車番" in row:
            row["車番"] = car_no
        if "着順" in row:
            row["着順"] = 999
        for col in ("試走T", "競走T", "ST"):
            if col in row:
                row[col] = 0.0
        for col in ("事故", "異常", "異", "備考", "事故内容"):
            if col in row:
                row[col] = "事前除外"
        # 事故欄が元DataFrameにない場合でも、engine側が参照できる共通列を追加する。
        if not any(col in out.columns for col in ("事故", "異常", "異", "備考", "事故内容")):
            out["事故"] = ""
            row["事故"] = "事前除外"
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)
        existing.add(car_no)
        added.append(car_no)

    meta_out = dict(meta or {})
    detected_numbers = sorted({int(car_no) for car_no, _reason in detected})
    if detected_numbers:
        meta_out["事前除外車番"] = detected_numbers
        meta_out["欠車車番"] = detected_numbers
        meta_out["比較対象外車番"] = detected_numbers
        meta_out["事前除外理由"] = {str(int(car_no)): str(reason) for car_no, reason in detected}
        meta_out["実出走数"] = int(len(out) - len(detected_numbers))
        meta_out["予測照合用出走数"] = int(len(out))
    return meta_out, out, sorted(set(added))

# Ver227: 発走後の事故・反則は、結果と回収率だけ保存し、予測精度・AI学習から除外する。
def _v227_detect_poststart_incidents(result_text: str, meta: dict) -> tuple[dict, list[dict]]:
    raw = str(result_text or "")
    if not raw:
        return dict(meta or {}), []
    normalized = re.sub(r"[\t\u3000]+", " ", raw)
    words = r"反妨|反則妨害|妨害失格|反則失格|落車|競走中止|周回誤認|周誤|失格"
    found: list[dict] = []
    # 公式結果の「- 6 選手名 ... /反妨」形式を優先。
    pat = re.compile(
        rf"(?:^|\n)\s*-?\s*(?:\n|\s)+([1-8])(?:\s|\n)+(.{{0,180}}?)(?:/\s*)?({words})(?=\s|$)",
        re.MULTILINE | re.DOTALL,
    )
    for m in pat.finditer(normalized):
        found.append({"車番": int(m.group(1)), "理由": str(m.group(3))})
    if not found:
        line_pat = re.compile(rf"(?:^|\n)\s*-?\s*([1-8])\b[^\n]{{0,240}}?({words})", re.MULTILINE)
        for m in line_pat.finditer(normalized):
            found.append({"車番": int(m.group(1)), "理由": str(m.group(2))})
    unique=[]
    seen=set()
    for item in found:
        key=(int(item["車番"]), str(item["理由"]))
        if key not in seen:
            seen.add(key); unique.append(item)
    meta_out=dict(meta or {})
    if unique:
        reasons = {str(int(x["車番"])): str(x["理由"]) for x in unique}
        reason_text = " / ".join(f"{k}番 {v}" for k, v in reasons.items())
        # engineの旧版・新版で参照名が異なっても、学習側へ流れないよう共通ゲートを多重指定する。
        meta_out.update({
            "発走後事故": True,
            "発走後事故車番": sorted({int(x["車番"]) for x in unique}),
            "発走後事故理由": reasons,
            "事故レース": True,
            "事故あり": True,
            "レース状態": "発走後事故",
            "予測精度評価対象": False,
            "予測精度評価対象外": True,
            "AI学習対象": False,
            "学習対象外": True,
            "学習除外": True,
            "選手履歴学習対象": False,
            "展開学習対象": False,
            "追い抜き相性学習対象": False,
            "開催場補正学習対象": False,
            "壁補正学習対象": False,
            "重み更新対象": False,
            "learning_excluded": True,
            "learning_exclusion_reason": reason_text or "発走後事故・反則",
        })
    return meta_out, unique


# Ver148: Streamlit fragment互換デコレーター
# st.fragment が利用できる環境では部分再実行、未対応環境では通常関数として動作します。
_v146_fragment = getattr(st, "fragment", lambda func: func)


# Ver217: 予測後の重いDB保存を待たず、オッズ入力を先に表示する。
_v217_db_save_lock = threading.Lock()


# Ver222: 一度実行した予測を、出走表・計算結果ごとDBへ保存して再利用する。
def _v222_ensure_prediction_restore_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS v222_prediction_restore (
                race_key TEXT PRIMARY KEY,
                race_label TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                venue_override TEXT,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_v222_prediction_restore_latest ON v222_prediction_restore(updated_at DESC)")
        con.commit()


def _v222_race_label(meta: dict, race_key: str) -> str:
    meta = meta or {}
    race_date = str(meta.get("開催日") or meta.get("日付") or "").strip()
    venue = str(meta.get("開催場") or "").strip()
    race_no = str(meta.get("R") or meta.get("レース") or meta.get("レース番号") or "").strip()
    parts = [x for x in (race_date, venue, f"{race_no}R" if race_no and not race_no.endswith("R") else race_no) if x]
    return " ".join(parts) if parts else str(race_key or "保存済み予測")


def _v222_save_prediction_restore(db_path: str, race_key: str, raw_text: str, venue_override: str, view: dict) -> None:
    race_key = str(race_key or "").strip()
    if not race_key or not isinstance(view, dict):
        return
    _v222_ensure_prediction_restore_table(db_path)
    payload = zlib.compress(pickle.dumps(view, protocol=pickle.HIGHEST_PROTOCOL), level=6)
    now = _v228_now_jst_iso()
    label = _v222_race_label(view.get("meta") or {}, race_key)
    with sqlite3.connect(db_path) as con:
        con.execute("""
            INSERT INTO v222_prediction_restore
            (race_key,race_label,raw_text,venue_override,payload,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(race_key) DO UPDATE SET
                race_label=excluded.race_label, raw_text=excluded.raw_text,
                venue_override=excluded.venue_override, payload=excluded.payload,
                updated_at=excluded.updated_at
        """, (race_key, label, str(raw_text or ""), str(venue_override or ""), sqlite3.Binary(payload), now, now))
        con.commit()


def _v222_list_prediction_restores(db_path: str, limit: int = 60) -> list[dict]:
    try:
        _v222_ensure_prediction_restore_table(db_path)
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT race_key,race_label,updated_at FROM v222_prediction_restore
                ORDER BY updated_at DESC LIMIT ?
            """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _v222_load_prediction_restore(db_path: str, race_key: str) -> tuple[dict, str, str]:
    try:
        _v222_ensure_prediction_restore_table(db_path)
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM v222_prediction_restore WHERE race_key=?", (str(race_key or ""),)).fetchone()
        if not row:
            return {}, "", ""
        view = pickle.loads(zlib.decompress(bytes(row["payload"])))
        return view if isinstance(view, dict) else {}, str(row["raw_text"] or ""), str(row["venue_override"] or "")
    except Exception:
        return {}, "", ""



# Ver235: 新旧の保存済み予測を常に統合表示し、旧予測が一覧から消えないよう修正。
# Ver234: 回収率プランにも現在版を保存し、6周展開の先頭残り過多を調整。
# Ver231: 予測をレース単位で上書きせず、バージョン別履歴として保存する。

def _v231_settings_hash(trials: int, seed: int, excluded: list[int] | None = None) -> str:
    payload = {
        "app_version": _V231_APP_VERSION,
        "simulation_mode": _V231_SIMULATION_MODE,
        "trials": int(trials or 0),
        "seed": int(seed or 0),
        "excluded": sorted(int(x) for x in (excluded or [])),
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

def _v231_ensure_prediction_history_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS v231_prediction_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_key TEXT NOT NULL,
                race_label TEXT NOT NULL,
                app_version TEXT NOT NULL,
                simulation_mode TEXT NOT NULL,
                settings_hash TEXT NOT NULL,
                prediction_time TEXT NOT NULL,
                trials INTEGER,
                seed INTEGER,
                raw_text TEXT NOT NULL,
                venue_override TEXT,
                payload BLOB NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_v231_prediction_history_race ON v231_prediction_history(race_key, history_id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_v231_prediction_history_latest ON v231_prediction_history(prediction_time DESC, history_id DESC)")
        con.commit()


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


def _v231_save_prediction_history(db_path: str, race_key: str, raw_text: str, venue_override: str, view: dict, trials: int, seed: int) -> int:
    race_key = str(race_key or "").strip()
    if not race_key or not isinstance(view, dict):
        return 0
    _v231_ensure_prediction_history_table(db_path)
    now = _v228_now_jst_iso()
    app_version = str(view.get("app_version") or _V231_APP_VERSION)
    simulation_mode = str(view.get("simulation_mode") or _V231_SIMULATION_MODE)
    settings_hash = str(view.get("settings_hash") or _v231_settings_hash(trials, seed, view.get("excluded") or []))
    label = _v222_race_label(view.get("meta") or {}, race_key)
    payload = zlib.compress(pickle.dumps(view, protocol=pickle.HIGHEST_PROTOCOL), level=6)
    with sqlite3.connect(db_path) as con:
        cur = con.execute("""
            INSERT INTO v231_prediction_history
            (race_key,race_label,app_version,simulation_mode,settings_hash,prediction_time,trials,seed,raw_text,venue_override,payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (race_key,label,app_version,simulation_mode,settings_hash,now,int(trials or 0),int(seed or 0),str(raw_text or ""),str(venue_override or ""),sqlite3.Binary(payload)))
        con.commit()
        history_id = int(cur.lastrowid or 0)

    # Ver266: 履歴保存時点のdfから車番別予測競走Tを同時保存。
    # 失敗しても本体予測履歴の保存は成功扱いにする。
    try:
        _v266_save_pred_time_snapshot(
            db_path=db_path,
            history_id=history_id,
            race_key=race_key,
            app_version=app_version,
            view=view,
            source_kind="saved_view",
        )
    except Exception:
        pass
    return history_id

def _v231_list_prediction_histories(db_path: str, limit: int = 120) -> list[dict]:
    try:
        _v231_ensure_prediction_history_table(db_path)
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT history_id,race_key,race_label,app_version,simulation_mode,settings_hash,prediction_time,trials,seed
                FROM v231_prediction_history
                ORDER BY prediction_time DESC, history_id DESC LIMIT ?
            """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

def _v231_load_prediction_history(db_path: str, history_id: int) -> tuple[dict, str, str, dict]:
    try:
        _v231_ensure_prediction_history_table(db_path)
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM v231_prediction_history WHERE history_id=?", (int(history_id),)).fetchone()
        if not row:
            return {}, "", "", {}
        view = pickle.loads(zlib.decompress(bytes(row["payload"])))
        meta = {k: row[k] for k in row.keys() if k != "payload"}
        return view if isinstance(view, dict) else {}, str(row["raw_text"] or ""), str(row["venue_override"] or ""), meta
    except Exception:
        return {}, "", "", {}



# Ver225: 欠車・発走前除外を事故レースから分離。
# Ver223: 結果登録後の解析表示をDBへ保存し、再描画・画面移動後も復元する。
def _v223_ensure_result_view_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS v223_result_view_restore (
                race_key TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_v223_result_view_latest ON v223_result_view_restore(updated_at DESC)")
        con.commit()


def _v223_save_result_view(db_path: str, race_key: str, view: dict) -> None:
    race_key = str(race_key or "").strip()
    if not race_key or not isinstance(view, dict):
        return
    _v223_ensure_result_view_table(db_path)
    payload = zlib.compress(pickle.dumps(view, protocol=pickle.HIGHEST_PROTOCOL), level=6)
    now = _v228_now_jst_iso()
    with sqlite3.connect(db_path) as con:
        con.execute("""
            INSERT INTO v223_result_view_restore (race_key,payload,created_at,updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(race_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
        """, (race_key, sqlite3.Binary(payload), now, now))
        con.commit()


def _v223_load_latest_result_view(db_path: str) -> dict:
    try:
        _v223_ensure_result_view_table(db_path)
        with sqlite3.connect(db_path) as con:
            row = con.execute("SELECT payload FROM v223_result_view_restore ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row:
            return {}
        view = pickle.loads(zlib.decompress(bytes(row[0])))
        return view if isinstance(view, dict) else {}
    except Exception:
        return {}


# Ver232: 保存済み予測のレースキーに対応する登録済み結果を同時復元する。
def _v232_load_result_view_for_race(db_path: str, race_key: str) -> dict:
    race_key = str(race_key or "").strip()
    if not race_key:
        return {}
    try:
        _v223_ensure_result_view_table(db_path)
        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "SELECT payload FROM v223_result_view_restore WHERE race_key=? LIMIT 1",
                (race_key,),
            ).fetchone()
        if not row:
            return {}
        view = pickle.loads(zlib.decompress(bytes(row[0])))
        return view if isinstance(view, dict) else {}
    except Exception:
        return {}




# Ver238: 結果の元本文を構造化結果とは別に保管し、再構成本文による誤上書きを防ぐ。
def _v238_ensure_raw_result_archive(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS v238_result_raw_archive (
                race_key TEXT PRIMARY KEY,
                raw_result_text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                saved_at TEXT NOT NULL
            )
            """
        )
        con.commit()


def _v238_save_exact_raw_result(db_path: str, race_key: str, raw_text: str, source: str = "manual") -> None:
    raw = str(raw_text or "").strip()
    if not race_key or not raw:
        return
    _v238_ensure_raw_result_archive(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO v238_result_raw_archive(race_key, raw_result_text, source, saved_at)
            VALUES(?,?,?,?)
            ON CONFLICT(race_key) DO UPDATE SET
              raw_result_text=excluded.raw_result_text,
              source=excluded.source,
              saved_at=excluded.saved_at
            """,
            (race_key, raw, str(source or "manual"), _v228_now_jst_iso()),
        )
        con.commit()


def _v238_load_exact_raw_result(db_path: str, race_key: str) -> str:
    try:
        _v238_ensure_raw_result_archive(db_path)
        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "SELECT raw_result_text FROM v238_result_raw_archive WHERE race_key=? LIMIT 1",
                (race_key,),
            ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


def _v238_col(df: pd.DataFrame, *names: str):
    for name in names:
        if name in df.columns:
            return name
    return None


def _v238_result_safety_check(db_path: str, race_key: str, rows: pd.DataFrame) -> tuple[list[str], list[str]]:
    """置換前に、順位重複・異常車の通常化・詳細値消失を検査する。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        return ["解析済みの着順データがありません。"], warnings
    car_c = _v238_col(rows, "車番", "car_no")
    finish_c = _v238_col(rows, "着順", "finish")
    status_c = _v238_col(rows, "異常", "事故", "result_status")
    race_c = _v238_col(rows, "競走T", "競走タイム", "race_time")
    st_c = _v238_col(rows, "ST", "start_time")
    if not car_c or not finish_c:
        return ["車番または着順を確認できません。"], warnings
    work = rows.copy()
    work[car_c] = pd.to_numeric(work[car_c], errors="coerce")
    work[finish_c] = pd.to_numeric(work[finish_c], errors="coerce")
    normal = work.copy()
    if status_c:
        s = normal[status_c].fillna("").astype(str)
        abnormal = s.str.contains(r"欠車|出走取消|発走除外|競走除外|反妨|反則|失格|落車|競走中止|周誤", regex=True)
        normal = normal[~abnormal]
    ranked = normal[normal[finish_c].notna()]
    dup = ranked[ranked.duplicated(subset=[finish_c], keep=False)]
    if not dup.empty:
        vals = sorted({int(x) for x in dup[finish_c].dropna().tolist()})
        errors.append(f"通常車に同じ着順が重複しています：{vals}")
    if ranked[car_c].duplicated().any():
        errors.append("同じ車番が複数の通常結果として解析されています。")
    if not race_key:
        return errors, warnings
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            old = [dict(x) for x in con.execute(
                "SELECT car_no, finish, race_time, start_time, result_status FROM result_entries WHERE race_key=?",
                (race_key,),
            ).fetchall()]
        if old:
            old_by = {int(x["car_no"]): x for x in old if x.get("car_no") is not None}
            new_cars = {int(x) for x in work[car_c].dropna().tolist()}
            old_cars = set(old_by)
            removed = sorted(old_cars - new_cars)
            added = sorted(new_cars - old_cars)
            if removed or added:
                warnings.append(f"車番構成が変わります（削除 {removed or 'なし'}／追加 {added or 'なし'}）。")
            for _, r in work.iterrows():
                if pd.isna(r[car_c]):
                    continue
                car = int(r[car_c])
                old_r = old_by.get(car)
                if not old_r:
                    continue
                if race_c and old_r.get("race_time") is not None and pd.isna(pd.to_numeric(pd.Series([r[race_c]]), errors="coerce").iloc[0]):
                    errors.append(f"{car}番の競走タイムが既存データから消えます。")
                if st_c and old_r.get("start_time") is not None and pd.isna(pd.to_numeric(pd.Series([r[st_c]]), errors="coerce").iloc[0]):
                    errors.append(f"{car}番のSTが既存データから消えます。")
                old_status = str(old_r.get("result_status") or "通常")
                new_status = str(r[status_c] if status_c else "通常")
                if old_status != "通常" and not any(k in new_status for k in ["欠車","取消","除外","反妨","反則","失格","落車","中止","周誤"]):
                    errors.append(f"{car}番の異常情報「{old_status}」が通常扱いへ変わります。")
    except Exception as exc:
        warnings.append(f"既存結果との詳細比較を完了できませんでした：{type(exc).__name__}")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


# Ver233: 保存済み結果を結果登録画面から選択し、元本文またはDB再構成本文を復元する。
def _v233_list_saved_results(db_path: str, limit: int = 200) -> list[dict]:
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT r.race_key, r.race_date, r.venue, r.race_no, r.registered_at,
                       COALESCE(v.updated_at, r.registered_at) AS updated_at
                FROM result_races r
                LEFT JOIN v223_result_view_restore v ON v.race_key = r.race_key
                ORDER BY COALESCE(v.updated_at, r.registered_at) DESC, r.race_key DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(x) for x in rows]
    except Exception:
        return []


def _v233_load_result_payload(db_path: str, race_key: str) -> dict:
    view = _v232_load_result_view_for_race(db_path, race_key)
    return view if isinstance(view, dict) else {}


def _v233_fmt_num(value, digits: int = 2) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
        v = float(value)
        return f"{v:.{digits}f}"
    except Exception:
        return str(value or "")


def _v233_build_result_text_from_db(db_path: str, race_key: str) -> str:
    """旧保存分でも上書き編集できるよう、登録済み構造化データから再解析可能な本文を作る。"""
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            race = con.execute("SELECT * FROM result_races WHERE race_key=?", (race_key,)).fetchone()
            entries = con.execute("SELECT * FROM result_entries WHERE race_key=? ORDER BY CASE WHEN finish IS NULL THEN 999 ELSE finish END, car_no", (race_key,)).fetchall()
            laps = con.execute("SELECT * FROM result_laps WHERE race_key=? ORDER BY COALESCE(lap_no,999), position", (race_key,)).fetchall()
            payouts = con.execute("SELECT * FROM result_payouts WHERE race_key=? ORDER BY rowid", (race_key,)).fetchall()
        if not race:
            return ""
        race_no = str(race['race_no'] or '').strip()
        date_text = str(race['race_date'] or '').replace('-', '/')
        lines = [
            f"{race_no}R" if race_no else "結果",
            "確定",
            date_text,
            f"{race['venue'] or ''}オート",
        ]
        cond=[]
        if race['surface']:
            cond.append(str(race['surface']))
        if race['track_temp'] is not None:
            cond.append(f"/{_v233_fmt_num(race['track_temp'],0)}℃")
        if cond:
            lines.append(' '.join(cond))
        if race['air_temp'] is not None:
            lines.append(f"気温：{_v233_fmt_num(race['air_temp'],0)}℃")
        if race['humidity'] is not None:
            lines.append(f"湿度：{_v233_fmt_num(race['humidity'],0)}%")
        lines += ["着順 車番 選手名", "LG/ハンデ/試走T 競走T（人気） ST/事故"]
        for e in entries:
            status=str(e['result_status'] or '通常')
            finish='-' if e['finish'] is None else str(int(e['finish']))
            trial=_v233_fmt_num(e['trial_time'],2) or '0.00'
            race_t=_v233_fmt_num(e['race_time'],3) or '0.000'
            stt=_v233_fmt_num(e['start_time'],2) or '0.00'
            handicap=str(e['handicap'] or '0').replace('m','')
            lines += [
                f"{finish} {int(e['car_no'])}",
                str(e['player_name'] or ''),
                f"{race['venue'] or ''}/{handicap}m/{trial}",
                race_t,
                f"{stt}" + (f" /{status}" if status and status != '通常' else ''),
            ]
        if laps:
            lines.append('グランドノート')
            grouped={}
            for x in laps:
                grouped.setdefault(str(x['lap_label']), []).append((int(x['position']), int(x['car_no'])))
            for label, vals in grouped.items():
                vals=sorted(vals)
                lines.append(label + '\t' + '\t'.join(str(car) for _,car in vals))
        if payouts:
            lines.append('払戻金')
            for x in payouts:
                pop = f" {int(x['popularity'])}人気" if x['popularity'] is not None else ''
                lines.append(f"{x['bet_type']}\t{x['combination']}\t{int(x['payout_yen'] or 0)}円{pop}")
        return '\n'.join(lines).strip()
    except Exception:
        return ""


def _v217_deferred_prediction_db_save(meta, bets, trials, df) -> None:
    """全買い目確率・特徴量をバックグラウンド保存する。

    予測スナップショットは呼び出し元で先に保存済み。SQLiteの同時書込を避けるため
    この処理内は単一ロックで直列化する。
    """
    try:
        with _v217_db_save_lock:
            engine.v67_save_ticket_snapshot(meta, bets, int(trials), engine.DB_PATH)
            engine.v40_save_prediction_features(meta, df, engine.DB_PATH)
    except Exception:
        # 表示を止めないことを最優先。次回予測・結果分析で再保存できる。
        return


# Ver164: 全開催場条件別補正と中止・全返還形式対応。
# Ver163: 画面切替で非表示になったウィジェット値をStreamlitに削除されないよう、
# 通常のウィジェットキーとは別の永続キーへ退避します。
def _v163_restore_input(widget_key: str, saved_key: str, default=None) -> None:
    if widget_key not in st.session_state:
        if saved_key in st.session_state:
            st.session_state[widget_key] = st.session_state[saved_key]
        elif default is not None:
            st.session_state[widget_key] = default


def _v163_save_input(widget_key: str, saved_key: str) -> None:
    st.session_state[saved_key] = st.session_state.get(widget_key)


def _v163_clear_saved_inputs(*saved_keys: str) -> None:
    for saved_key in saved_keys:
        st.session_state.pop(saved_key, None)


st.title("🏁 AutoRaceAI スマホ本予測")
st.caption("Ver263｜複数展開ルートを確率化し、実測グランドノートから展開タイプと近似ルートを学習。")
try:
    _v256_refresh_learning_settings(_v230_db_path())
except Exception:
    pass

# Ver241: iPhone Safariでselectbox選択時に画面が自動拡大（フォーカスイン）するのを抑止。
# 16px未満のフォーム部品へフォーカスするとSafariが自動ズームするため、
# モバイル時だけプルダウン本体・検索入力・表示値を16px以上に固定する。
st.markdown(
    """
    <style>
    @media (max-width: 768px) {
      div[data-baseweb="select"] > div,
      div[data-baseweb="select"] input,
      div[data-baseweb="select"] span,
      div[role="listbox"],
      div[role="option"] {
        font-size: 16px !important;
      }
      div[data-baseweb="select"] input {
        min-height: 24px !important;
      }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div id="page-top"></div>', unsafe_allow_html=True)
st.markdown(
    """
    <style>
    .v73-float-top {
        position: fixed;
        right: 16px;
        bottom: 82px;
        z-index: 999999;
        background: rgba(31, 41, 55, 0.92);
        color: white !important;
        text-decoration: none !important;
        padding: 10px 14px;
        border-radius: 999px;
        font-size: 14px;
        box-shadow: 0 4px 14px rgba(0,0,0,.22);
    }
    .v73-nav {
        display:flex;
        gap:8px;
        flex-wrap:wrap;
        margin:8px 0 16px 0;
    }
    .v73-nav a {
        display:inline-block;
        padding:7px 11px;
        border:1px solid #d1d5db;
        border-radius:999px;
        text-decoration:none !important;
        font-size:13px;
    }
    </style>
    <a class="v73-float-top" href="#main-tabs">↑ 上へ</a>
    """,
    unsafe_allow_html=True,
)


def v73_section_nav() -> None:
    st.markdown(
        """
        <div class="v73-nav">
          <a href="#prediction-summary">予測概要</a>
          <a href="#finish-probability">着順確率</a>
          <a href="#ticket-probability">券種別確率</a>
          <a href="#cover-line">強調ライン</a>
          <a href="#copy-all-formations">一括コピー</a>
          <a href="#main-tabs">メインタブへ</a>
        </div>
        """,
        unsafe_allow_html=True,
    )


def v73_copy_box(title: str, text: str, key: str, height: int = 145) -> None:
    """スマホでも一括コピーしやすい読み取り専用欄を表示する。"""
    safe_title = json.dumps(str(title), ensure_ascii=False)
    safe_text = json.dumps(str(text), ensure_ascii=False)
    element_id = "v73_copy_" + re.sub(r"[^0-9A-Za-z_-]+", "_", str(key))
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <div style="font-weight:700;margin:0 0 7px 0;">{title}</div>
      <textarea id="{element_id}" readonly
        style="width:100%;height:{height}px;box-sizing:border-box;border:1px solid #d1d5db;border-radius:10px;padding:10px;font-size:16px;line-height:1.55;background:#f8fafc;color:#111827;">{text}</textarea>
      <button id="{element_id}_btn"
        style="width:100%;margin-top:7px;padding:10px;border:0;border-radius:9px;background:#2563eb;color:white;font-weight:700;font-size:15px;">
        まとめてコピー
      </button>
      <div id="{element_id}_msg" style="height:20px;margin-top:5px;font-size:13px;color:#15803d;"></div>
    </div>
    <script>
      const area = document.getElementById({json.dumps(element_id)});
      const btn = document.getElementById({json.dumps(element_id + '_btn')});
      const msg = document.getElementById({json.dumps(element_id + '_msg')});
      btn.addEventListener('click', async () => {{
        try {{
          await navigator.clipboard.writeText({safe_text});
          msg.textContent = 'コピーしました';
        }} catch (e) {{
          area.focus(); area.select();
          document.execCommand('copy');
          msg.textContent = 'コピーしました';
        }}
      }});
    </script>
    """
    components.html(html, height=height + 105, scrolling=False)



# ============================================================
# Ver124: 一般予定専用 10分前プッシュ通知（Unicode送信修正）
# ============================================================
GENERAL_REMINDER_JST = ZoneInfo("Asia/Tokyo")


RESULT_VENUES = ["飯塚", "山陽", "浜松", "川口", "伊勢崎"]

def _detect_result_venue_from_title(text: str) -> str:
    """結果ページの開催タイトル部分だけから開催場を判定する。

    選手名・所属LGが並ぶ「着順」以降は対象外。タイトルで確定できない場合は
    空文字を返し、画面側で手動選択させる。
    """
    header = str(text or "").split("着順", 1)[0]
    normalized = re.sub(r"[\s　]+", "", header)
    venue_patterns = [
        ("山陽", ["山陽小野田市営", "山陽市営", "山陽ミッドナイト", "山陽オーバーミッドナイト"]),
        ("飯塚", ["飯塚市営", "飯塚ミッドナイト", "飯塚オーバーミッドナイト"]),
        ("浜松", ["浜松市営", "浜松記念", "Ｇ２浜松記念", "G2浜松記念"]),
        ("川口", ["川口市営", "川口ナイター"]),
        ("伊勢崎", ["伊勢崎市営", "伊勢崎ナイター"]),
    ]
    for venue, patterns in venue_patterns:
        if any(pattern in normalized for pattern in patterns):
            return venue

    # 「開催名＋場名」がタイトルに明記されている場合だけ補助判定する。
    title_words = ("記念", "市営", "開催", "ミッドナイト", "ナイター", "普通開催", "オーバーミッドナイト")
    candidates = [venue for venue in RESULT_VENUES if venue in normalized]
    if len(candidates) == 1 and any(word in normalized for word in title_words):
        return candidates[0]
    return ""

def _format_jst(value) -> str:
    if value is None or str(value).strip() in {"", "None", "NaT"}:
        return ""
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return str(value)
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("Asia/Tokyo").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)

def _jst_datetime_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        return frame
    out = frame.copy()
    for col in out.columns:
        if any(token in str(col) for token in ["登録日時", "保存日時", "更新日時", "作成日時", "分析日時", "started_at", "finished_at"]):
            out[col] = out[col].map(_format_jst)
    return out
GENERAL_REMINDER_NTFY_BASE = "https://ntfy.sh"


def v123_parse_general_schedule_text(text_value: str) -> tuple[date | None, time | None, str | None]:
    """貼り付け文から日付・締切時刻・短い予定名を抽出する。"""
    source = str(text_value or "")

    parsed_date = None
    date_match = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", source)
    if date_match:
        try:
            parsed_date = date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
        except ValueError:
            parsed_date = None

    # 出走時刻より締切時刻を優先する。公式ページでは「19:51 締切」などの形が多い。
    parsed_time = None
    deadline_patterns = [
        r"(\d{1,2}):(\d{2})\s*(?:投票)?締切(?:時刻)?",
        r"(?:投票)?締切(?:予定|時刻)?\s*[:：]?\s*(\d{1,2}):(\d{2})",
        r"締切まで[^\n]*?(\d{1,2}):(\d{2})",
    ]
    for pattern in deadline_patterns:
        match = re.search(pattern, source)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed_time = time(hour=hour, minute=minute)
            break

    # 通知タイトルは出走表ヘッダーの開催名とレース番号から「川口3R」の形にする。
    # 選手欄の所属LGを開催場として誤使用しないよう、「予想印」「着順」より前だけを見る。
    schedule_header = re.split(r"(?:予想印|着順\s*車番|選手名\(LG\))", source, maxsplit=1)[0]
    normalized_header = re.sub(r"[\s　]+", "", schedule_header)
    venue_patterns = [
        ("川口", ["川口市営", "川口ナイトレース", "川口ナイター", "川口開催"]),
        ("伊勢崎", ["伊勢崎市営", "伊勢崎ナイトレース", "伊勢崎ナイター", "伊勢崎開催"]),
        ("浜松", ["浜松市営", "浜松記念", "浜松開催"]),
        ("山陽", ["山陽小野田市営", "山陽市営", "山陽ミッドナイト", "山陽開催"]),
        ("飯塚", ["飯塚市営", "飯塚ミッドナイト", "飯塚開催"]),
    ]
    parsed_venue = ""
    for venue, patterns in venue_patterns:
        if any(pattern in normalized_header for pattern in patterns):
            parsed_venue = venue
            break
    if not parsed_venue:
        header_candidates = [venue for venue in RESULT_VENUES if venue in normalized_header]
        if len(header_candidates) == 1:
            parsed_venue = header_candidates[0]

    race_match = re.search(r"(?m)^\s*(\d{1,2})\s*[RＲ]\s*$", schedule_header)
    if race_match is None:
        race_match = re.search(r"(?:^|[^0-9])(\d{1,2})\s*[RＲ](?:[^0-9]|$)", schedule_header)

    parsed_title = None
    if parsed_venue and race_match:
        parsed_title = f"{parsed_venue}{int(race_match.group(1))}R"
    else:
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.search(r"20\d{2}年|\d{1,2}:\d{2}|発走|開始|締切", line):
                continue
            if len(line) <= 60:
                parsed_title = line
                break

    return parsed_date, parsed_time, parsed_title


def v123_schedule_ntfy_reminder(title: str, event_date: date, event_time: time, topic: str) -> tuple[datetime, datetime]:
    """締切10分前通知をntfyへ予約する。"""
    clean_title = str(title or "").strip()
    clean_topic = str(topic or "").strip()
    if not clean_title:
        raise ValueError("予定名を入力してください。")
    if not clean_topic:
        raise ValueError("ntfyトピック名を入力してください。")
    if any(ch in clean_topic for ch in "/?# "):
        raise ValueError("トピック名には空白や / ? # を使わないでください。")

    event_dt = datetime.combine(event_date, event_time, tzinfo=GENERAL_REMINDER_JST)
    notify_dt = event_dt - timedelta(minutes=10)
    now = datetime.now(GENERAL_REMINDER_JST)
    if notify_dt <= now:
        raise ValueError("通知予定時刻が過ぎています。締切時刻を10分以上先にしてください。")
    if notify_dt - now > timedelta(days=3):
        raise ValueError("ntfy.shの予約通知は最大3日先です。3日以内の予定を指定してください。")

    endpoint = f"{GENERAL_REMINDER_NTFY_BASE}/{urllib.parse.quote(clean_topic, safe='')}"
    message = f"{clean_title}\n締切時刻: {event_dt.strftime('%Y/%m/%d %H:%M')}"
    # HTTPヘッダーはASCIIのみ。日本語タイトルはURLエンコードして送る。
    # 本文はUTF-8バイト列にすることでUnicodeEncodeErrorを防ぐ。
    headers = {
        "At": str(int(notify_dt.timestamp())),
        "Title": urllib.parse.quote("締切の10分前です", safe=""),
        "Priority": "high",
        "Tags": "bell",
        "Content-Type": "text/plain; charset=utf-8",
    }
    request = urllib.request.Request(
        endpoint,
        data=message.encode("utf-8", errors="strict"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if int(getattr(response, "status", 200)) >= 400:
                raise RuntimeError(f"ntfy応答エラー: {response.status}")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "通知送信時の文字コード変換に失敗しました。日本語はUTF-8本文として送信する必要があります。"
        ) from exc
    return event_dt, notify_dt


def _v139_ntfy_request(
    topic: str,
    message: str,
    *,
    title: str = "一般予定通知",
    notify_at: datetime | None = None,
) -> dict:
    """ntfyへサーバー側から送信する。ブラウザCORSやiOS WebViewの影響を受けない。"""
    clean_topic = str(topic or "").strip()
    if not clean_topic:
        raise ValueError("ntfyトピック名を入力してください。")
    if any(ch in clean_topic for ch in "/?# "):
        raise ValueError("トピック名には空白や / ? # を使わないでください。")

    # 日本語タイトルをHTTPヘッダーへ入れると、環境によってLatin-1変換や
    # URLエンコード文字列の表示が起きるため、UTF-8のJSON本文で送信する。
    endpoint = GENERAL_REMINDER_NTFY_BASE
    payload = {
        "topic": clean_topic,
        "title": str(title or "一般予定通知").strip() or "一般予定通知",
        "message": str(message),
        "priority": 4,
        "tags": ["bell"],
    }
    if notify_at is not None:
        # ntfyのJSON Publish APIでは予約指定は `delay` を使用する。
        # `at` はJSON項目として解釈されず即時配信になる環境があるため使用しない。
        scheduled_unix = int(notify_at.astimezone(timezone.utc).timestamp())
        payload["delay"] = str(scheduled_unix)

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="strict"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if int(getattr(response, "status", 200)) >= 400:
                raise RuntimeError(f"ntfy応答エラー: {response.status} {raw[:160]}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ntfy HTTP {exc.code}: {body[:180]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ntfyへ接続できませんでした: {exc.reason}") from exc

    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {"raw": raw[:200]}


def _v139_general_reminder_defaults() -> None:
    base = datetime.now(GENERAL_REMINDER_JST) + timedelta(minutes=30)
    st.session_state.setdefault("v139_reminder_source", "")
    st.session_state.setdefault("v139_reminder_title", "")
    st.session_state.setdefault("v139_reminder_date", base.date())
    st.session_state.setdefault("v139_reminder_time", base.time().replace(second=0, microsecond=0))
    # 初期トピックはユーザー指定の notify。既存セッションが空欄でも補完する。
    if not str(st.session_state.get("v139_reminder_topic", "")).strip():
        st.session_state["v139_reminder_topic"] = "notify"
    st.session_state.setdefault("v150_reminder_timing", "10分前")


def v123_render_general_reminder_tab() -> None:
    """Streamlit標準UIで一般予定通知を表示する。ダイアログ内だけ再実行される。"""
    _v139_general_reminder_defaults()

    st.caption("貼り付け文の締切時刻を読み取り、締切30分前・10分前・5分前、または組み合わせて通知します。")
    st.text_area(
        "予定情報を貼り付け（任意）",
        key="v139_reminder_source",
        height=115,
        placeholder="例：19:51 締切\n2026年8月1日(土)\n一般戦 3100m",
    )
    if st.button("日付・締切時刻を自動入力", use_container_width=True, key="v139_parse_reminder"):
        parsed_date, parsed_time, parsed_title = v123_parse_general_schedule_text(
            st.session_state.get("v139_reminder_source", "")
        )
        if parsed_date:
            st.session_state["v139_reminder_date"] = parsed_date
        if parsed_time:
            st.session_state["v139_reminder_time"] = parsed_time
        if parsed_title:
            # 開催場＋レース番号を読めた場合は、古い予定名が残っていても更新する。
            st.session_state["v139_reminder_title"] = parsed_title
        if parsed_date or parsed_time:
            st.session_state["v139_reminder_message"] = ("success", "日付・締切時刻を自動入力しました。")
        else:
            st.session_state["v139_reminder_message"] = ("warning", "日付または締切時刻を読み取れませんでした。")
        st.rerun(scope="fragment")

    st.text_input("予定名", key="v139_reminder_title", placeholder="例：オンライン面談")
    col1, col2 = st.columns(2)
    with col1:
        st.date_input("予定日", key="v139_reminder_date")
    with col2:
        st.time_input("締切時刻", key="v139_reminder_time", step=60)
    st.text_input(
        "ntfyトピック名",
        key="v139_reminder_topic",
        placeholder="推測されにくい長い文字列",
        help="iPhoneのntfyアプリで同じトピックを購読してください。",
    )
    st.segmented_control(
        "通知タイミング",
        options=["30分前", "10分前", "5分前", "30分前と10分前", "10分前と5分前", "30分前・10分前・5分前"],
        key="v150_reminder_timing",
        selection_mode="single",
    )

    test_col, reserve_col = st.columns(2)
    with test_col:
        if st.button("今すぐテスト", use_container_width=True, key="v139_test_ntfy"):
            try:
                with st.spinner("テスト通知を送信中…"):
                    test_title = str(st.session_state.get("v139_reminder_title", "")).strip() or "通知テスト"
                    reply = _v139_ntfy_request(
                        st.session_state.get("v139_reminder_topic", ""),
                        "一般予定通知の接続テストです。",
                        title=test_title,
                    )
                receipt = f" 受付ID: {reply.get('id')}" if reply.get("id") else ""
                st.session_state["v139_reminder_message"] = ("success", f"テスト通知を送信しました。{receipt}")
            except Exception as exc:
                st.session_state["v139_reminder_message"] = ("error", str(exc))
            st.rerun(scope="fragment")

    with reserve_col:
        timing_label = st.session_state.get("v150_reminder_timing", "10分前") or "10分前"
        if st.button("通知を予約", type="primary", use_container_width=True, key="v139_reserve_ntfy"):
            try:
                title = str(st.session_state.get("v139_reminder_title", "")).strip()
                if not title:
                    raise ValueError("予定名を入力してください。")
                event_dt = datetime.combine(
                    st.session_state["v139_reminder_date"],
                    st.session_state["v139_reminder_time"],
                    tzinfo=GENERAL_REMINDER_JST,
                )
                selected = st.session_state.get("v150_reminder_timing", "10分前") or "10分前"
                lead_minutes = {
                    "30分前": [30],
                    "10分前": [10],
                    "5分前": [5],
                    "30分前と10分前": [30, 10],
                    "10分前と5分前": [10, 5],
                    "30分前・10分前・5分前": [30, 10, 5],
                }.get(selected, [10])
                now = datetime.now(GENERAL_REMINDER_JST)
                notify_times = [(minutes, event_dt - timedelta(minutes=minutes)) for minutes in lead_minutes]
                expired = [minutes for minutes, dt in notify_times if dt <= now]
                if expired:
                    minimum = max(lead_minutes)
                    raise ValueError(f"通知時刻が過ぎています。締切時刻を{minimum}分以上先にしてください。")
                if any(dt - now > timedelta(days=3) for _, dt in notify_times):
                    raise ValueError("ntfy.shの予約通知は最大3日先です。")

                receipts = []
                with st.spinner("通知を予約中…"):
                    for minutes, notify_dt in notify_times:
                        message = (
                            f"締切{minutes}分前です。\n"
                            f"締切時刻: {event_dt.strftime('%Y/%m/%d %H:%M')}"
                        )
                        reply = _v139_ntfy_request(
                            st.session_state.get("v139_reminder_topic", ""),
                            message,
                            title=title,
                            notify_at=notify_dt,
                        )
                        receipt = f" / 受付ID: {reply.get('id')}" if reply.get("id") else ""
                        receipts.append(f"{notify_dt.strftime('%Y/%m/%d %H:%M')}（{minutes}分前）{receipt}")
                st.session_state["v139_reminder_message"] = (
                    "success",
                    "通知を予約しました：" + " ／ ".join(receipts),
                )
            except Exception as exc:
                st.session_state["v139_reminder_message"] = ("error", str(exc))
            st.rerun(scope="fragment")

    message = st.session_state.pop("v139_reminder_message", None)
    if message:
        kind, text = message
        getattr(st, kind)(text)

    st.caption("通知が届かない場合は、ntfyアプリの購読トピックとiPhoneの通知許可を確認してください。")

def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def db_summary(db_path: str) -> dict:
    path = Path(db_path)
    info = {"exists": path.exists(), "size": path.stat().st_size if path.exists() else 0, "tables": []}
    if not path.exists():
        return info
    with sqlite3.connect(path) as con:
        info["tables"] = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        if "players" in info["tables"]:
            info["players"] = int(con.execute("SELECT COUNT(*) FROM players").fetchone()[0])
        if "race_history" in info["tables"]:
            info["history"] = int(con.execute("SELECT COUNT(*) FROM race_history").fetchone()[0])
        if "v15_player_history_imports" in info["tables"]:
            info["import_players"] = int(con.execute(
                "SELECT COUNT(DISTINCT player_name) FROM v15_player_history_imports WHERE player_name IS NOT NULL AND player_name <> ''"
            ).fetchone()[0])
            info["import_history"] = int(con.execute("SELECT COUNT(*) FROM v15_player_history_imports").fetchone()[0])
    return info





def _v138_db_token(db_path: str) -> tuple[str, int, int]:
    """DB内容が変わった時だけキャッシュを更新する軽量キー。"""
    path = Path(db_path)
    if not path.exists():
        return (str(path), 0, 0)
    stat = path.stat()
    return (str(path), int(stat.st_mtime_ns), int(stat.st_size))


@st.cache_data(show_spinner=False, max_entries=16)
def _v138_cached_db_summary(db_path: str, token: tuple) -> dict:
    del token
    return db_summary(db_path)


@st.cache_data(show_spinner=False, max_entries=16)
def _v138_cached_adjustment_log(db_path: str, token: tuple) -> pd.DataFrame:
    del token
    return engine.v36_get_adjustment_log(db_path)


@st.cache_data(show_spinner=False, max_entries=16)
def _v138_cached_venue_analysis(db_path: str, token: tuple) -> pd.DataFrame:
    del token
    return engine.v103_load_venue_analysis_cache(db_path)


@st.cache_data(show_spinner=False, max_entries=8)
def _v138_cached_database_health(db_path: str, token: tuple) -> dict:
    del token
    return engine.v97_database_health(db_path)


@st.cache_data(show_spinner=False, max_entries=16)
def _v138_player_registration_index(db_path: str, token: tuple) -> dict:
    """全選手集計はDB更新時に一度だけ作る。名前入力ごとに全件SQLを実行しない。"""
    del token
    names = {}
    if not Path(db_path).exists():
        return names
    with sqlite3.connect(db_path) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if {"players", "race_history"}.issubset(tables):
            rows = con.execute(
                """
                SELECT p.player_name, COUNT(h.history_id),
                       SUM(CASE WHEN COALESCE(h.use_for_model, 1)=1 THEN 1 ELSE 0 END),
                       MAX(NULLIF(h.race_date, ''))
                FROM players p
                LEFT JOIN race_history h ON h.player_id=p.player_id
                GROUP BY p.player_id, p.player_name
                """
            ).fetchall()
            for player_name, count_all, count_use, latest in rows:
                pkey = normalize_player_key(player_name)
                item = names.setdefault(pkey, {"names": [], "canonical": 0, "model": 0, "detail": 0, "dates": []})
                item["names"].append(str(player_name))
                item["canonical"] += int(count_all or 0)
                item["model"] += int(count_use or 0)
                if latest:
                    item["dates"].append(str(latest))
        if "v15_player_history_imports" in tables:
            rows = con.execute(
                """
                SELECT player_name, COUNT(*), MAX(NULLIF(race_date, ''))
                FROM v15_player_history_imports
                WHERE player_name IS NOT NULL AND TRIM(player_name)<>''
                GROUP BY player_name
                """
            ).fetchall()
            for player_name, count_all, latest in rows:
                pkey = normalize_player_key(player_name)
                item = names.setdefault(pkey, {"names": [], "canonical": 0, "model": 0, "detail": 0, "dates": []})
                item["names"].append(str(player_name))
                item["detail"] += int(count_all or 0)
                if latest:
                    item["dates"].append(str(latest))
    return names

def normalize_player_key(name: str) -> str:
    """DB照合用に空白と所属表記を除去した選手名キーを返す。"""
    value = str(name or "").strip()
    value = re.sub(r"[（(](?:川口|伊勢崎|浜松|飯塚|山陽)[）)]\s*$", "", value)
    return re.sub(r"[\s　]+", "", value)


def player_data_coverage(entries: pd.DataFrame, db_path: str) -> pd.DataFrame:
    """解析対象選手ごとのDB登録量を、重複加算せずに集計する。"""
    columns = ["車", "選手名", "正規履歴", "条件詳細あり", "実質登録数", "予測利用可", "最新日", "データ量"]
    if entries is None or entries.empty or "選手名" not in entries.columns:
        return pd.DataFrame(columns=columns)

    targets = []
    for _, row in entries.iterrows():
        targets.append({
            "車": int(row.get("車番", row.get("車", 0)) or 0),
            "選手名": str(row.get("選手名", "")).strip(),
            "key": normalize_player_key(row.get("選手名", "")),
        })

    canonical = {}
    imported = {}
    path = Path(db_path)
    if path.exists():
        with sqlite3.connect(path) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if {"players", "race_history"}.issubset(tables):
                rows = con.execute(
                    """
                    SELECT p.player_name,
                           COUNT(h.history_id),
                           SUM(CASE WHEN COALESCE(h.use_for_model, 1)=1 THEN 1 ELSE 0 END),
                           MAX(NULLIF(h.race_date, ''))
                    FROM players p
                    LEFT JOIN race_history h ON h.player_id=p.player_id
                    GROUP BY p.player_id, p.player_name
                    """
                ).fetchall()
                for name, count_all, count_use, latest in rows:
                    key = normalize_player_key(name)
                    prev = canonical.get(key, (0, 0, None))
                    dates = [d for d in (prev[2], latest) if d]
                    canonical[key] = (prev[0] + int(count_all or 0), prev[1] + int(count_use or 0), max(dates) if dates else None)
            if "v15_player_history_imports" in tables:
                rows = con.execute(
                    """
                    SELECT player_name, COUNT(*), MAX(NULLIF(race_date, ''))
                    FROM v15_player_history_imports
                    WHERE player_name IS NOT NULL AND TRIM(player_name)<>''
                    GROUP BY player_name
                    """
                ).fetchall()
                for name, count_all, latest in rows:
                    key = normalize_player_key(name)
                    prev = imported.get(key, (0, None))
                    dates = [d for d in (prev[1], latest) if d]
                    imported[key] = (prev[0] + int(count_all or 0), max(dates) if dates else None)

    out = []
    for item in targets:
        c_all, c_use, c_latest = canonical.get(item["key"], (0, 0, None))
        i_all, i_latest = imported.get(item["key"], (0, None))
        # 詳細取込は正規履歴のミラーなので合算しない。正規履歴が実質ユニーク件数。
        total = c_all
        latest_candidates = [d for d in (c_latest, i_latest) if d]
        latest = max(latest_candidates) if latest_candidates else "未登録"
        if total >= 20:
            level = "十分"
        elif total >= 10:
            level = "標準"
        elif total >= 5:
            level = "少なめ"
        elif total >= 1:
            level = "不足"
        else:
            level = "0件"
        out.append({
            "車": item["車"],
            "選手名": item["選手名"],
            "正規履歴": c_all,
            "条件詳細あり": i_all,
            "実質登録数": total,
            "予測利用可": c_use,
            "最新日": latest,
            "データ量": level,
        })
    return pd.DataFrame(out, columns=columns).sort_values("車").reset_index(drop=True)


def show_player_data_coverage(entries: pd.DataFrame) -> None:
    coverage = player_data_coverage(entries, engine.DB_PATH)
    st.subheader("📚 解析対象選手の登録データ量")
    if coverage.empty:
        st.info("解析対象選手の登録状況を確認できませんでした。")
        return
    st.dataframe(
        coverage,
        use_container_width=True,
        hide_index=True,
        column_config={
            "正規履歴": st.column_config.NumberColumn(format="%d件"),
            "条件詳細あり": st.column_config.NumberColumn(format="%d件"),
            "実質登録数": st.column_config.NumberColumn(format="%d件"),
            "予測利用可": st.column_config.NumberColumn(format="%d件"),
        },
    )
    zero_names = coverage.loc[coverage["実質登録数"] == 0, "選手名"].tolist()
    if zero_names:
        st.warning("DB履歴0件: " + "、".join(zero_names) + "。名前の照合または履歴登録を確認してください。")
    st.caption("正規履歴が実質的な登録件数です。『条件詳細あり』は、その正規履歴のうち走路温度・湿度・レース種別などの詳細条件も保存されている件数で、別レースとしては加算しません。実質登録数20件以上を『十分』の目安にしています。")



def lookup_player_registration(name: str, db_path: str) -> dict:
    """入力した選手名がDBに登録済みか、キャッシュ索引から確認する。"""
    key = normalize_player_key(name)
    result = {
        "found": False, "matched_name": "", "canonical_count": 0,
        "model_count": 0, "detail_count": 0, "latest": None, "candidates": [],
    }
    if not key or not Path(db_path).exists():
        return result
    names = _v138_player_registration_index(db_path, _v138_db_token(db_path))
    if key in names:
        item = names[key]
        display_names = sorted(set(item["names"]), key=lambda x: (len(x), x))
        result.update({
            "found": True,
            "matched_name": display_names[0] if display_names else str(name).strip(),
            "canonical_count": item["canonical"],
            "model_count": item["model"],
            "detail_count": item["detail"],
            "latest": max(item["dates"]) if item["dates"] else None,
        })
        return result
    candidates = []
    for pkey, item in names.items():
        if key in pkey or pkey in key:
            display_names = sorted(set(item["names"]), key=lambda x: (len(x), x))
            if display_names:
                candidates.append(display_names[0])
    result["candidates"] = sorted(set(candidates))[:8]
    return result


def show_player_registration_status(name: str) -> None:
    """選手名入力直後にDB登録状況を表示する。"""
    if not str(name or "").strip():
        st.caption("選手名を入力すると、DB登録状況をここで確認できます。")
        return
    try:
        status = lookup_player_registration(name, engine.DB_PATH)
    except Exception as exc:
        st.warning(f"登録状況を確認できませんでした: {type(exc).__name__}: {exc}")
        return

    if status["found"]:
        latest = status["latest"] or "日付なし"
        st.success(
            f"✅ 登録済み: {status['matched_name']}｜正規履歴 {status['canonical_count']}件"
            f"（予測利用可 {status['model_count']}件）｜条件詳細 {status['detail_count']}件｜最新 {latest}"
        )
        if status["canonical_count"] == 0 and status["detail_count"] > 0:
            st.warning("条件詳細データはありますが、正規履歴が0件です。予測用データへの反映状態を確認してください。")
    else:
        st.warning("⚠️ この選手名はDBに未登録です。履歴を貼り付けて登録してください。")
        if status["candidates"]:
            st.caption("近い登録名: " + "、".join(status["candidates"]))

def ticket_probability_table(bets: dict, key: str, trials: int, top_n: int = 20) -> pd.DataFrame:
    """シミュレーションの券種別カウントを、表示用の確率表へ変換する。"""
    counter = bets.get(key, {}) if isinstance(bets, dict) else {}
    total = max(int(trials), 1)
    rows = []
    for rank, (combo, count) in enumerate(
        sorted(counter.items(), key=lambda item: item[1], reverse=True)[:top_n], 1
    ):
        if not isinstance(combo, (tuple, list)):
            combo = (combo,)
        rows.append({
            "順位": rank,
            "組み合わせ": "-".join(map(str, combo)),
            "確率": float(count) / total * 100.0,
            "的中回数": int(count),
        })
    return pd.DataFrame(rows, columns=["順位", "組み合わせ", "確率", "的中回数"])


def prediction_confidence_summary(finish_prob: pd.DataFrame, bets: dict, trials: int) -> dict:
    """予測分布の集中度から、今回の予測自信度を診断する。"""
    empty = {
        "level": "判定不能", "score": 0, "icon": "⚪", "top_car": None,
        "top1": 0.0, "gap": 0.0, "top3": 0.0, "top_trifecta": 0.0,
        "comment": "着順確率を取得できないため、自信度を判定できません。",
    }
    if finish_prob is None or finish_prob.empty or "1着率" not in finish_prob.columns:
        return empty

    work = finish_prob.copy()
    work["1着率"] = pd.to_numeric(work["1着率"], errors="coerce").fillna(0.0)
    if "3着内率" in work.columns:
        work["3着内率"] = pd.to_numeric(work["3着内率"], errors="coerce").fillna(0.0)
    else:
        cols = [c for c in ["1着率", "2着率", "3着率"] if c in work.columns]
        work["3着内率"] = work[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(axis=1)
    work = work.sort_values("1着率", ascending=False).reset_index(drop=True)
    top1 = float(work.loc[0, "1着率"])
    second = float(work.loc[1, "1着率"]) if len(work) > 1 else 0.0
    gap = max(top1 - second, 0.0)
    top3 = float(work.loc[0, "3着内率"])
    top_car = work.loc[0, "車"] if "車" in work.columns else None

    tri = ticket_probability_table(bets, "三連単", trials, 1)
    top_trifecta = float(tri.iloc[0]["確率"]) if not tri.empty else 0.0

    score = round(
        min(top1 / 35.0, 1.0) * 30.0
        + min(gap / 15.0, 1.0) * 35.0
        + min(top3 / 75.0, 1.0) * 20.0
        + min(top_trifecta / 8.0, 1.0) * 15.0
    )
    if score >= 70:
        level, icon = "高", "🔥"
        comment = "上位候補が比較的はっきりしています。今回の予測は普段よりまとまりがあります。"
    elif score >= 48:
        level, icon = "中", "🟡"
        comment = "中心候補はありますが、相手や着順にはまだ揺れがあります。"
    else:
        level, icon = "低", "🌫️"
        comment = "上位確率が接近しています。展開次第で順位が入れ替わりやすい予測です。"

    return {
        "level": level, "score": int(score), "icon": icon, "top_car": top_car,
        "top1": top1, "gap": gap, "top3": top3, "top_trifecta": top_trifecta,
        "comment": comment,
    }


def show_prediction_confidence(finish_prob: pd.DataFrame, bets: dict, trials: int) -> None:
    info = prediction_confidence_summary(finish_prob, bets, trials)
    st.subheader(f"{info['icon']} 今回の予測自信度：{info['level']}（{info['score']}/100）")
    if info["level"] == "高":
        st.success(info["comment"])
    elif info["level"] == "中":
        st.warning(info["comment"])
    elif info["level"] == "低":
        st.info(info["comment"])
    else:
        st.info(info["comment"])
    c1, c2, c3, c4, c5 = st.columns(5)
    car_text = f"{int(info['top_car'])}番" if pd.notna(info.get("top_car")) else "不明"
    c1.metric("1着中心", car_text, f"1着率 {info['top1']:.2f}%")
    c2.metric("1位と2位の差", f"{info['gap']:.2f}pt")
    c3.metric("中心車の3着内率", f"{info['top3']:.2f}%")
    c4.metric("三連単1位の確率", f"{info['top_trifecta']:.2f}%")
    st.caption("自信度は、1着率の高さ・次点との差・3着内率・三連単確率の集中度をまとめた診断です。的中を保証する数値ではありません。")



def v180_trifecta_tight_recommendation(bets: dict, trials: int, meta: dict) -> dict:
    """三連単1〜10点の推奨。

    6車立ては同じ6車立ての確率分布が近い過去レースで点数別に検証する。
    7車は十分な履歴と強い確率集中が揃う場合だけ参考表示、8車は原則非推奨。
    通常カバーラインやフォーメーション生成には影響しない。
    """
    empty = {
        "points": 10, "level": "判定不能", "icon": "⚪", "hit_rate": None,
        "lower_bound": None, "samples": 0, "topn_cover": 0.0, "top10_cover": 0.0,
        "reason": "三連単の確率分布を取得できません。", "combos": [],
    }
    counter = bets.get("三連単", {}) if isinstance(bets, dict) else {}
    if not counter or int(trials or 0) <= 0:
        return empty

    ordered = sorted(counter.items(), key=lambda item: item[1], reverse=True)
    rows, cumulative = [], 0.0
    for rank, (combo, count) in enumerate(ordered[:10], 1):
        probability = float(count) / max(int(trials), 1) * 100.0
        cumulative += probability
        combo_tuple = tuple(combo) if isinstance(combo, (tuple, list)) else (combo,)
        rows.append({
            "rank": rank, "combo": "-".join(map(str, combo_tuple)),
            "probability": probability, "cumulative": cumulative,
        })
    if not rows:
        return empty

    current_cum = {r["rank"]: r["cumulative"] for r in rows}
    starter_count = engine.v102_starter_count_for_meta(meta, engine.DB_PATH)
    top10_cover = float(rows[-1]["cumulative"])

    # 8車は過去検証で少点数との相性が弱いため、原則として出さない。
    if starter_count and int(starter_count) >= 8:
        return {
            "points": 10, "level": "非推奨", "icon": "🌫️", "hit_rate": None,
            "lower_bound": None, "samples": 0, "topn_cover": top10_cover,
            "top10_cover": top10_cover,
            "reason": "8車立ては確率が広がりやすく、DB67の過去検証でも10点以内の再現性が低いため、激絞りは原則非推奨です。",
            "combos": [],
        }

    historical = pd.DataFrame()
    try:
        with sqlite3.connect(engine.DB_PATH) as con:
            historical = pd.read_sql_query(
                """
                WITH starters AS (
                    SELECT race_key, COUNT(DISTINCT car_no) AS starters
                    FROM result_entries
                    WHERE COALESCE(result_status, '') NOT IN ('欠車','出走取消','競走除外')
                    GROUP BY race_key
                )
                SELECT f.race_key, f.predicted_rank AS actual_rank,
                       s.starters, t.predicted_rank AS n,
                       t.cumulative_probability AS cumulative_probability
                FROM v67_ticket_feedback f
                JOIN starters s ON s.race_key=f.race_key
                JOIN v67_prediction_tickets t
                  ON t.race_key=f.race_key
                 AND t.bet_type='3連単'
                 AND t.predicted_rank BETWEEN 1 AND 10
                WHERE f.bet_type='3連単' AND f.predicted_rank IS NOT NULL
                """, con,
            )
    except Exception:
        historical = pd.DataFrame()

    if historical.empty or not starter_count:
        return {
            **empty, "points": min(10, len(rows)), "level": "履歴不足", "icon": "⚪",
            "topn_cover": top10_cover, "top10_cover": top10_cover,
            "reason": "同じ車立ての検証履歴が不足しているため、激絞り点数は推奨しません。",
        }

    for col in ("n", "actual_rank", "cumulative_probability", "starters"):
        historical[col] = pd.to_numeric(historical[col], errors="coerce")
    same = historical[historical["starters"] == int(starter_count)].dropna().copy()
    pivot = same.pivot_table(
        index=["race_key", "actual_rank"], columns="n",
        values="cumulative_probability", aggfunc="first"
    ).reset_index()
    needed = list(range(1, min(10, len(rows)) + 1))
    if any(n not in pivot.columns for n in needed) or len(pivot) < 15:
        return {
            **empty, "points": min(10, len(rows)), "level": "履歴不足", "icon": "⚪",
            "topn_cover": top10_cover, "top10_cover": top10_cover,
            "reason": f"{int(starter_count)}車立ての完全な比較履歴が15レース未満のため、激絞り点数は推奨しません。",
        }

    # 7車は履歴15件以上かつ上位10点累積45%以上のときだけ参考判定。
    if int(starter_count) == 7 and (len(pivot) < 15 or top10_cover < 45.0):
        return {
            "points": 10, "level": "非推奨", "icon": "🌫️", "hit_rate": None,
            "lower_bound": None, "samples": int(len(pivot)), "topn_cover": top10_cover,
            "top10_cover": top10_cover,
            "reason": "7車立ては履歴または確率集中が不足しています。データが増えるまでは激絞り非推奨です。",
            "combos": [],
        }

    import math
    import numpy as np

    current_vector = np.array([float(current_cum[n]) for n in needed], dtype=float)
    matrix = pivot[needed].to_numpy(dtype=float)
    scale = np.nanstd(matrix, axis=0)
    scale[~np.isfinite(scale) | (scale < 1.0)] = 1.0
    distances = np.sqrt(np.nanmean(((matrix - current_vector) / scale) ** 2, axis=1))
    nearest_count = min(20, len(pivot))
    peer = pivot.iloc[np.argsort(distances)[:nearest_count]].copy()

    def wilson_lower(hits: int, samples: int, z: float = 1.2815515655) -> float:
        if samples <= 0:
            return 0.0
        p = hits / samples
        denominator = 1.0 + z * z / samples
        centre = p + z * z / (2.0 * samples)
        spread = z * math.sqrt(p * (1.0 - p) / samples + z * z / (4.0 * samples * samples))
        return max(0.0, (centre - spread) / denominator)

    candidates = []
    for n in needed:
        hits = int((peer["actual_rank"] <= n).sum())
        samples = int(len(peer))
        hit_rate = hits / samples if samples else 0.0
        lower = wilson_lower(hits, samples)
        # 信頼区間下限を中心にし、点数増加へ小さなペナルティを付ける。
        score = lower - 0.015 * n
        candidates.append({
            "n": n, "hits": hits, "samples": samples,
            "hit_rate": hit_rate, "lower": lower, "score": score,
            "cover": float(current_cum[n]),
        })

    best_score = max(c["score"] for c in candidates)
    # ほぼ同等なら少ない点数を優先。点数を増やしても改善しない膨張を防ぐ。
    near_best = [c for c in candidates if c["score"] >= best_score - 0.02]
    selected = min(near_best, key=lambda c: c["n"])

    # 6車でも信頼区間下限25%未満、または上位10点累積28%未満は非推奨。
    recommended = selected["lower"] >= 0.25 and top10_cover >= 28.0
    if not recommended:
        return {
            "points": int(selected["n"]), "level": "非推奨", "icon": "🌫️",
            "hit_rate": selected["hit_rate"] * 100.0,
            "lower_bound": selected["lower"] * 100.0,
            "samples": int(selected["samples"]),
            "topn_cover": float(selected["cover"]), "top10_cover": top10_cover,
            "reason": (
                f"近い過去{selected['samples']}レースを比較しましたが、"
                f"信頼区間下限が{selected['lower']*100:.1f}%のため激絞り非推奨です。"
            ), "combos": [],
        }

    level, icon = ("高", "🎯") if selected["lower"] >= 0.45 else ("中", "🟡")
    reason = (
        f"{int(starter_count)}車立ての確率分布が近い過去{selected['samples']}レースで、"
        f"上位{selected['n']}点以内が{selected['hits']}件。"
        f"実績{selected['hit_rate']*100:.1f}%、80%信頼区間下限{selected['lower']*100:.1f}%です。"
    )
    return {
        "points": int(selected["n"]), "level": level, "icon": icon,
        "hit_rate": selected["hit_rate"] * 100.0,
        "lower_bound": selected["lower"] * 100.0,
        "samples": int(selected["samples"]),
        "topn_cover": float(selected["cover"]), "top10_cover": top10_cover,
        "reason": reason, "combos": [r["combo"] for r in rows[:int(selected["n"])]],
    }

def show_v180_trifecta_tight_recommendation(bets: dict, trials: int, meta: dict) -> None:
    info = v180_trifecta_tight_recommendation(bets, trials, meta)
    st.subheader(f"{info['icon']} 三連単 激絞り推奨：{info['points']}点")
    c1, c2, c3 = st.columns(3)
    c1.metric("推奨点数", f"{info['points']}点")
    c2.metric("推奨範囲の累積", f"{info['topn_cover']:.2f}%")
    c3.metric("上位10点の累積", f"{info['top10_cover']:.2f}%")
    if info.get("hit_rate") is not None:
        lower_text = (
            f"・80%信頼区間下限 {info['lower_bound']:.1f}%"
            if info.get("lower_bound") is not None else ""
        )
        st.caption(
            f"近い過去条件の上位{info['points']}点以内率：{info['hit_rate']:.1f}% "
            f"（{info['samples']}レース）{lower_text}"
        )
    if info["level"] == "非推奨":
        st.warning(info["reason"])
    elif info["level"] == "高":
        st.success(info["reason"])
    else:
        st.info(info["reason"])
    if info["combos"]:
        st.code("\n".join(info["combos"]), language=None)
    st.caption("通常のカバーラインとは別の参考表示です。少点数を常に推奨するものではありません。")

def ticket_point_heading(bet_type: str, points: int) -> str:
    """券種と点数を全画面で同じ書式にそろえる。"""
    return f"【{bet_type}】({int(points)}点)"


def show_ticket_table(bet_type: str, bets: dict, key: str, trials: int, top_n: int = 20) -> None:
    table = ticket_probability_table(bets, key, trials, top_n)
    if table.empty:
        st.info(f"【{bet_type}】の集計結果がありません。")
        return
    st.subheader(ticket_point_heading(bet_type, len(table)))
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "確率": st.column_config.NumberColumn("確率", format="%.2f%%"),
            "的中回数": st.column_config.NumberColumn("的中回数", format="%d回"),
        },
    )



@_v146_fragment
def show_v67_self_evaluation(meta: dict) -> None:
    """全結果ラインと、大外しを分離した実用ラインを表示する。"""
    st.markdown('<div id="cover-line"></div>', unsafe_allow_html=True)
    st.subheader("🎯 AI自己評価・上位累積確率ライン")

    st.markdown("#### 実用ラインの大外し設定")
    outlier_cutoff = st.slider(
        "三連単の大外し判定（上位累積確率）",
        min_value=50,
        max_value=99,
        value=85,
        step=1,
        key="v78_coverline_outlier_cutoff",
        help="三連単の的中位置がこの値以上だったレースは、レース単位で全券種の実用カバーライン計算から除外します。全結果ラインと学習データには残ります。",
    )
    st.caption(
        f"現在の設定：三連単が上位累積{int(outlier_cutoff)}%以上だったレースを、実用ラインだけから除外"
    )

    starter_count = engine.v102_starter_count_for_meta(meta, engine.DB_PATH)
    stats = engine.v72_ticket_feedback_stats(
        engine.DB_PATH,
        trifecta_outlier_cutoff=float(outlier_cutoff),
        starter_count=starter_count,
    )
    if starter_count:
        st.info(f"今回と同じ **{int(starter_count)}車立て** の過去レースだけで累積確率ラインを計算しています。")
    else:
        st.warning("今回の出走数を特定できないため、出走数を混ぜた集計になっています。出走表の『○車』表記を確認してください。")
    if stats.empty:
        if starter_count:
            st.info(f"{int(starter_count)}車立ての結果照合データがまだありません。結果登録が増えると専用ラインが育ちます。")
        else:
            st.info("結果照合データがまだありません。今後、予測後に結果を登録すると券種別の平均と強調ラインが育ちます。")
        return

    summary = stats[[
        "券種", "出走数", "レース数", "大外し除外",
        "90%カバー", "実用90%カバー",
        "95%カバー", "実用95%カバー",
        "20点以内レース数", "20点以内90%カバー",
    ]].copy()
    with st.expander("過去成績とカバーライン一覧", expanded=False):
        st.dataframe(
            summary,
            use_container_width=True,
            hide_index=True,
            column_config={
                "90%カバー": st.column_config.NumberColumn("全結果90%", format="%.2f%%"),
                "実用90%カバー": st.column_config.NumberColumn("実用90%", format="%.2f%%"),
                "95%カバー": st.column_config.NumberColumn("全結果95%", format="%.2f%%"),
                "実用95%カバー": st.column_config.NumberColumn("実用95%", format="%.2f%%"),
                "20点以内レース数": st.column_config.NumberColumn("三連単20点内", format="%d件"),
                "20点以内90%カバー": st.column_config.NumberColumn("20点内90%", format="%.2f%%"),
            },
        )

    coverage = st.selectbox(
        "強調ライン", [80, 90, 95], index=1,
        format_func=lambda x: f"過去{x}%の結果を含む範囲",
    )
    line_mode = st.radio(
        "ライン計算", ["実用ライン", "全結果ライン"], horizontal=True,
        help=f"実用ラインは、三連単の的中位置が上位累積{int(outlier_cutoff)}%以上だったレースを、全券種の計算から除外します。全結果ラインはそのレースも含みます。",
    )

    st.markdown("#### 強調する最大点数")
    cap_enabled = st.checkbox(
        "点数上限を使う",
        value=True,
        key="v75_highlight_cap_enabled",
        help="有効にすると、カバーラインに必要な点数が多くても上位から指定点数までに絞ります。",
    )
    cap_points = st.select_slider(
        "全券種共通の最大点数",
        options=[5, 10, 15, 20, 30, 50, 100],
        value=20,
        disabled=not cap_enabled,
        key="v75_highlight_cap_points",
        help="フォーメーションと一括コピーも、この点数以内の組み合わせだけで作成します。",
    )
    if cap_enabled:
        st.caption(f"現在の設定：各券種とも最大{int(cap_points)}点まで強調")
    else:
        st.caption("現在の設定：点数制限なし。選択したカバーラインまで強調")

    bet_types = ["2連単", "2連複", "3連複", "3連単"]
    selected_bet_type = st.radio(
        "表示する券種", bet_types, horizontal=True,
        key=f"v155_cover_bet_type_{coverage}_{line_mode}",
        help="選んだ券種だけを計算・表示するため、ライン変更時の再処理を軽くします。",
    )
    all_formation_text: dict[str, str] = {}
    all_formation_points: dict[str, int] = {}
    selected_trifecta_line_mode = "通常選択"
    for bet_type in [selected_bet_type]:
        row = stats[stats["券種"] == bet_type]
        if row.empty:
            st.info(f"{bet_type}は結果照合がまだありません。")
            continue
        r = row.iloc[0]
        sample = int(r["レース数"])
        excluded = int(r["大外し除外"])

        # 三連単だけは、過去に上位20点以内で的中したレース群の
        # 累積確率分布を強調基準として選べる。
        trifecta_line_mode = "通常選択"
        if bet_type == "3連単":
            within20_count = int(r.get("20点以内レース数", 0) or 0)
            st.markdown("#### 三連単の強調基準")
            trifecta_line_mode = st.radio(
                "三連単ライン",
                ["通常選択", "過去20点以内的中ライン"],
                horizontal=True,
                key=f"v81_trifecta_line_mode_{coverage}_{line_mode}",
                help="『過去20点以内的中ライン』は、三連単が予測上位20点以内で当たった過去レースだけを集め、その累積確率の分位点を使います。全レースのカバー率ではありません。",
            )
            selected_trifecta_line_mode = trifecta_line_mode
            if within20_count > 0:
                w20_value = float(r[f"20点以内{coverage}%カバー"])
                st.caption(
                    f"過去20点以内的中：{within20_count}レース ／ "
                    f"{coverage}%地点の累積確率：{w20_value:.2f}%"
                )
                with st.expander(f"過去20点以内で的中した{within20_count}レースを確認"):
                    w20_details = engine.v81_trifecta_within20_details(
                        engine.DB_PATH, starter_count=starter_count
                    )
                    st.dataframe(
                        w20_details,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "個別確率": st.column_config.NumberColumn(format="%.3f%%"),
                            "上位累積確率": st.column_config.NumberColumn(format="%.2f%%"),
                        },
                    )
            else:
                st.caption("三連単が上位20点以内で的中した過去データはまだありません。")

        use_within20 = (
            bet_type == "3連単"
            and trifecta_line_mode == "過去20点以内的中ライン"
            and int(r.get("20点以内レース数", 0) or 0) > 0
        )
        if use_within20:
            cutoff = float(r[f"20点以内{coverage}%カバー"])
            used = int(r["20点以内レース数"])
            st.success(
                f"{int(starter_count) if starter_count else '同'}車立て・過去20点以内的中{coverage}%ライン：上位累積 {cutoff:.2f}%まで "
                f"（同じ出走数で三連単が上位20点以内だった過去{used}レースから計算）"
            )
            st.caption("このラインは『20点以内で当たるレースの累積位置』を見る指標で、全レースの的中率を表すものではありません。")
        elif line_mode == "実用ライン":
            cutoff = float(r[f"実用{coverage}%カバー"])
            used = int(r["実用レース数"])
            st.success(
                f"{int(starter_count) if starter_count else '同'}車立て・実用{coverage}%カバーライン：上位累積 {cutoff:.2f}%まで "
                f"（全{sample}レース中 {used}レース使用・三連単{int(outlier_cutoff)}%以上のレース{excluded}件を除外）"
            )
        else:
            cutoff = float(r[f"{coverage}%カバー"])
            st.info(f"{int(starter_count) if starter_count else '同'}車立て・全結果{coverage}%カバーライン：上位累積 {cutoff:.2f}%まで（{sample}レースすべて使用）")

        if excluded > 0:
            with st.expander(f"三連単{int(outlier_cutoff)}%以上で除外した{excluded}レースを確認"):
                details = engine.v72_ticket_outlier_details(
                    bet_type,
                    engine.DB_PATH,
                    trifecta_outlier_cutoff=float(outlier_cutoff),
                    starter_count=starter_count,
                )
                st.dataframe(
                    details,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "上位累積確率": st.column_config.NumberColumn(format="%.2f%%"),
                        "三連単上位累積確率": st.column_config.NumberColumn(format="%.2f%%"),
                    },
                )

        highlighted_full = engine.v67_ticket_highlight_table(meta, bet_type, cutoff, engine.DB_PATH)
        if highlighted_full.empty:
            st.caption("現在の予測分布を取得できませんでした。予測をもう一度実行してください。")
            continue

        highlighted = highlighted_full.copy()
        if cap_enabled:
            highlighted = highlighted.head(int(cap_points)).copy()

        actual_points = len(highlighted)
        original_points = len(highlighted_full)
        actual_cover = float(pd.to_numeric(highlighted["累積確率"], errors="coerce").dropna().max()) if not highlighted.empty else 0.0

        st.markdown(f"### {ticket_point_heading(bet_type, actual_points)}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("強調点数", f"{actual_points}点")
        m2.metric("強調範囲の累積", f"{actual_cover:.2f}%")
        m3.metric("ライン要求", f"{cutoff:.2f}%")
        if cap_enabled and original_points > actual_points:
            st.warning(
                f"{ticket_point_heading(bet_type, original_points)}が{coverage}%カバーラインに必要ですが、"
                f"{ticket_point_heading(bet_type, actual_points)}へ制限しました。"
                f" 現在の強調範囲は累積{actual_cover:.2f}%です。"
            )
        else:
            st.success(f"{ticket_point_heading(bet_type, actual_points)}・累積{actual_cover:.2f}%で選択ラインをカバーしています。")

        # ダークモードでも埋もれないよう、色だけでなく記号・太字・境界線を併用する。
        display_highlighted = highlighted.copy().reset_index(drop=True)
        # 保存済み予測の形式によっては、すでに「強調」「順位」列を持つことがある。
        # insert() の重複エラーを避け、表示用の列を毎回安全に作り直す。
        display_highlighted = display_highlighted.drop(
            columns=[c for c in ["強調", "順位"] if c in display_highlighted.columns],
            errors="ignore",
        )
        display_highlighted.insert(0, "強調", ["★" if i < len(display_highlighted) - 1 else "★ ここまで" for i in range(len(display_highlighted))])
        display_highlighted.insert(1, "順位", [f"{i + 1}位" for i in range(len(display_highlighted))])

        def _v79_row_style(row):
            is_last = row.name == display_highlighted.index[-1]
            base = (
                "background-color:#FFF3B0;color:#111827;font-weight:800;"
                "border-left:6px solid #F59E0B;"
            )
            if is_last:
                base += "border-top:3px solid #F59E0B;border-bottom:4px solid #F59E0B;"
            else:
                base += "border-bottom:1px solid #D97706;"
            return [base] * len(row)

        styled = display_highlighted.style.apply(_v79_row_style, axis=1).format({
            "確率": "{:.3f}%",
            "累積確率": "{:.2f}%",
        })
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            column_config={
                "強調": st.column_config.TextColumn(width="small"),
                "順位": st.column_config.TextColumn(width="small"),
                "確率": st.column_config.NumberColumn(format="%.3f%%"),
                "累積確率": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
        st.caption("★付きの黄色い行が強調対象です。『★ ここまで』が現在の強調境界です。")

        formations = engine.v67_compress_formations(highlighted["組み合わせ"].tolist(), bet_type)
        if formations:
            st.markdown("#### 強調範囲のまとめ・一括コピー")
            formation_text = "\n".join(str(line) for line in formations)
            all_formation_text[bet_type] = formation_text
            all_formation_points[bet_type] = actual_points
            v73_copy_box(
                ticket_point_heading(bet_type, actual_points),
                formation_text,
                f"{bet_type}_{coverage}_{line_mode}_{trifecta_line_mode}_{cap_enabled}_{cap_points}",
                height=max(105, min(260, 44 + 28 * len(formations))),
            )
            st.caption("共通部分だけをまとめた簡易表記です。正確な対象は上の一覧表でも確認できます。")

    # Ver159: 通常表示は選択中の1券種だけ。4券種はボタンを押した時だけ生成する。
    st.markdown('<div id="copy-all-formations"></div>', unsafe_allow_html=True)
    st.markdown("### 📋 全券種を一括コピー")
    st.caption("普段は選択中の1券種だけを計算します。下のボタンを押した時だけ4券種をまとめて生成します。")

    meta_signature = hashlib.sha256(repr(meta).encode("utf-8", errors="ignore")).hexdigest()[:16]
    all_copy_signature = (
        f"{meta_signature}|{starter_count}|{coverage}|{line_mode}|{int(outlier_cutoff)}|"
        f"{cap_enabled}|{int(cap_points)}|{selected_trifecta_line_mode}"
    )
    cache_key = "v159_all_ticket_copy_cache"

    if st.button("📋 全券種のフォーメーションを生成", key="v159_generate_all_ticket_copy", use_container_width=True):
        generated_text: dict[str, str] = {}
        generated_points: dict[str, int] = {}
        generated_notes: list[str] = []

        with st.spinner("4券種をまとめています…"):
            for copy_bet_type in bet_types:
                copy_row = stats[stats["券種"] == copy_bet_type]
                if copy_row.empty:
                    generated_notes.append(f"{copy_bet_type}: 結果照合データなし")
                    continue
                copy_r = copy_row.iloc[0]

                use_copy_within20 = (
                    copy_bet_type == "3連単"
                    and selected_trifecta_line_mode == "過去20点以内的中ライン"
                    and int(copy_r.get("20点以内レース数", 0) or 0) > 0
                )
                if use_copy_within20:
                    copy_cutoff = float(copy_r[f"20点以内{coverage}%カバー"])
                elif line_mode == "実用ライン":
                    copy_cutoff = float(copy_r[f"実用{coverage}%カバー"])
                else:
                    copy_cutoff = float(copy_r[f"{coverage}%カバー"])

                copy_table = engine.v67_ticket_highlight_table(
                    meta, copy_bet_type, copy_cutoff, engine.DB_PATH
                )
                if copy_table.empty:
                    generated_notes.append(f"{copy_bet_type}: 現在の予測分布なし")
                    continue
                if cap_enabled:
                    copy_table = copy_table.head(int(cap_points)).copy()

                copy_formations = engine.v67_compress_formations(
                    copy_table["組み合わせ"].tolist(), copy_bet_type
                )
                if not copy_formations:
                    generated_notes.append(f"{copy_bet_type}: フォーメーション作成不可")
                    continue

                generated_text[copy_bet_type] = "\n".join(str(line) for line in copy_formations)
                generated_points[copy_bet_type] = len(copy_table)

        st.session_state[cache_key] = {
            "signature": all_copy_signature,
            "text": generated_text,
            "points": generated_points,
            "notes": generated_notes,
        }

    cached_all = st.session_state.get(cache_key)
    if isinstance(cached_all, dict) and cached_all.get("signature") == all_copy_signature:
        cached_text = cached_all.get("text") or {}
        cached_points = cached_all.get("points") or {}
        if cached_text:
            all_text = "\n\n".join(
                f"{ticket_point_heading(copy_bet_type, cached_points.get(copy_bet_type, 0))}\n{cached_text[copy_bet_type]}"
                for copy_bet_type in bet_types
                if copy_bet_type in cached_text
            )
            v73_copy_box(
                "全券種の強調フォーメーション",
                all_text,
                f"v159_all_{coverage}_{line_mode}_{cap_enabled}_{cap_points}_{meta_signature}",
                height=max(220, min(520, 90 + 26 * all_text.count("\n"))),
            )
        for note in cached_all.get("notes") or []:
            st.caption(note)
    else:
        st.caption("ラインや点数設定を変えた場合は、もう一度生成ボタンを押してください。")

    st.markdown('<div class="v73-nav"><a href="#ticket-probability">券種別確率へ</a><a href="#prediction-summary">予測概要へ</a><a href="#page-top">ページ上部へ</a></div>', unsafe_allow_html=True)

def show_v67_result_analysis(ticket_analysis: pd.DataFrame) -> None:
    st.subheader("🎯 実結果は予測の上位累積何%地点だったか")
    if ticket_analysis is None or ticket_analysis.empty:
        st.info("同じレースの券種別予測確率が保存されていないため、照合できませんでした。")
        return
    display = ticket_analysis.copy()
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "個別確率": st.column_config.NumberColumn(format="%.3f%%"),
            "上位累積確率": st.column_config.NumberColumn(format="%.2f%%"),
                            "三連単上位累積確率": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    stats = engine.v67_ticket_feedback_stats(engine.DB_PATH)
    if not stats.empty:
        st.markdown("#### これまでの平均")
        st.dataframe(
            stats,
            use_container_width=True,
            hide_index=True,
            column_config={
                "平均": st.column_config.NumberColumn(format="%.2f%%"),
                "中央値": st.column_config.NumberColumn(format="%.2f%%"),
                "80%カバー": st.column_config.NumberColumn(format="%.2f%%"),
                "90%カバー": st.column_config.NumberColumn(format="%.2f%%"),
                "95%カバー": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )


def normalize_ticket_combo(value: str, unordered: bool = False) -> str:
    """入力された車番組み合わせを、確率表と照合できる形式へ正規化する。"""
    nums = [int(x) for x in re.findall(r"\d+", str(value or ""))]
    if unordered:
        nums = sorted(nums)
    return "-".join(map(str, nums))


def parse_manual_odds(text: str, unordered: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """1行1組の『組み合わせ オッズ』を読み取る。"""
    rows = []
    errors = []
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        # 最後の数値をオッズ、その前を組み合わせとして扱う。
        match = re.match(r"^(.*?)[\s,，:：]+([0-9]+(?:\.[0-9]+)?)\s*(?:倍)?$", line)
        if not match:
            errors.append(f"{line_no}行目: {line}")
            continue
        combo = normalize_ticket_combo(match.group(1), unordered=unordered)
        try:
            odds = float(match.group(2))
        except ValueError:
            odds = 0.0
        if not combo or odds <= 0:
            errors.append(f"{line_no}行目: {line}")
            continue
        rows.append({"組み合わせ": combo, "入力オッズ": odds})
    if not rows:
        return pd.DataFrame(columns=["組み合わせ", "入力オッズ"]), errors
    return pd.DataFrame(rows).drop_duplicates("組み合わせ", keep="last"), errors


def show_odds_comparison(title: str, bets: dict, key: str, trials: int, widget_key: str, unordered: bool = False, namespace: str = "current", show_bulk: bool = True) -> None:
    """各確率表の直下でオッズを入力し、再描画後も入力値を保持する。"""
    st.markdown(f"#### {title} オッズ入力")
    st.caption("各行へ直接入力できます。公式の4券種横並び表は、下の一括貼り付けから読み込めます。")

    if show_bulk:
        bulk_key = f"bulk_odds_text_{namespace}"
        with st.expander("公式オッズ表を一括貼り付け"):
            bulk_text = st.text_area(
                "3連単人気・3連複人気・2連単人気・2連複人気の表",
                key=bulk_key, height=180,
                placeholder="公式オッズ表を見出しからそのまま貼り付け",
            )
            if st.button("4券種のオッズを読み込む", key=f"load_bulk_odds_{namespace}", use_container_width=True):
                parsed = v182_parse_four_block_odds(bulk_text)
                total = sum(len(v) for v in parsed.values())
                if total <= 0:
                    st.error("オッズを読み取れませんでした。タブ区切りの表をそのまま貼り付けてください。")
                else:
                    for parsed_key, values in parsed.items():
                        st.session_state[f"saved_odds_{namespace}_{parsed_key}"] = values
                    st.success(
                        f"読込完了：三連単{len(parsed['3tan'])}件、三連複{len(parsed['3fuku'])}件、"
                        f"2連単{len(parsed['2tansho'])}件、2連複{len(parsed['2fuku'])}件"
                    )
                    st.rerun()

    prob_df = ticket_probability_table(bets, key, trials, top_n=40).drop(columns=["的中回数"], errors="ignore")
    if prob_df.empty:
        st.info("確率データがありません。")
        return
    if unordered:
        prob_df["組み合わせ"] = prob_df["組み合わせ"].map(lambda x: normalize_ticket_combo(x, unordered=True))
        prob_df = prob_df.groupby("組み合わせ", as_index=False)["確率"].sum().sort_values("確率", ascending=False).head(40)
        prob_df.insert(0, "順位", range(1, len(prob_df) + 1))
    prob_df["公平倍率"] = prob_df["確率"].map(lambda p: (100.0 / p) if p > 0 else None)

    store_key = f"saved_odds_{namespace}_{widget_key}"
    saved = st.session_state.setdefault(store_key, {})
    prob_df["入力オッズ"] = prob_df["組み合わせ"].map(lambda combo: saved.get(str(combo)))

    editor_key = f"odds_editor_{namespace}_{widget_key}"
    edited = st.data_editor(
        prob_df[["順位", "組み合わせ", "確率", "公平倍率", "入力オッズ"]],
        key=editor_key,
        use_container_width=True,
        hide_index=True,
        disabled=["順位", "組み合わせ", "確率", "公平倍率"],
        column_config={
            "確率": st.column_config.NumberColumn("モデル確率", format="%.2f%%"),
            "公平倍率": st.column_config.NumberColumn("公平倍率", format="%.1f倍"),
            "入力オッズ": st.column_config.NumberColumn("入力オッズ", min_value=0.0, step=0.1, format="%.1f倍"),
        },
    )

    # data_editorは入力のたびに再実行されるため、組み合わせ単位で明示保存する。
    updated = {}
    for _, row in edited.iterrows():
        val = pd.to_numeric(pd.Series([row.get("入力オッズ")]), errors="coerce").iloc[0]
        if pd.notna(val) and float(val) > 0:
            updated[str(row["組み合わせ"])] = float(val)
    st.session_state[store_key] = updated

    entered = edited[pd.to_numeric(edited["入力オッズ"], errors="coerce").notna()].copy()
    if entered.empty:
        st.caption("入力した行だけ、倍率差と市場比較が下に表示されます。")
        return
    entered["入力オッズ"] = pd.to_numeric(entered["入力オッズ"], errors="coerce")
    entered = entered[entered["入力オッズ"] > 0].copy()
    if entered.empty:
        st.caption("入力した行だけ、倍率差と市場比較が下に表示されます。")
        return
    entered["倍率差"] = entered["入力オッズ"] / entered["公平倍率"]
    entered["比較"] = entered["倍率差"].map(
        lambda r: "市場よりモデル評価が高い" if r >= 1.15 else ("市場よりモデル評価が低い" if r <= 0.85 else "ほぼ同水準")
    )
    st.dataframe(
        entered[["組み合わせ", "確率", "公平倍率", "入力オッズ", "倍率差", "比較"]].sort_values("倍率差", ascending=False),
        use_container_width=True,
        hide_index=True,
        column_config={
            "確率": st.column_config.NumberColumn("モデル確率", format="%.2f%%"),
            "公平倍率": st.column_config.NumberColumn("公平倍率", format="%.1f倍"),
            "入力オッズ": st.column_config.NumberColumn("入力オッズ", format="%.1f倍"),
            "倍率差": st.column_config.NumberColumn("オッズ÷公平倍率", format="%.2f倍"),
        },
    )
    st.caption("倍率差は入力オッズ÷公平倍率です。市場とモデルの評価差を見るための参考値です。")



def v182_parse_four_block_odds(text: str) -> dict:
    """公式オッズの4券種横並び表を解析する。

    期待する列は、三連単4列・三連複4列・2連単3列・2連複3列。
    タブ区切りを優先し、空欄を保持する。
    """
    result = {"3tan": {}, "3fuku": {}, "2tansho": {}, "2fuku": {}, "tansho": {}, "wide": {}}
    for raw in str(text or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip() or "3連単人気" in line:
            continue
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        cols += [""] * (14 - len(cols))

        def num(index):
            value = str(cols[index]).strip().replace(",", "")
            return value if re.fullmatch(r"\d+", value) else ""

        def odd(index):
            value = str(cols[index]).strip().replace(",", "")
            try:
                parsed = float(value)
                return parsed if parsed > 0 else None
            except Exception:
                return None

        a, b, c, o = num(0), num(1), num(2), odd(3)
        if a and b and c and o:
            result["3tan"][f"{a}-{b}-{c}"] = o
        a, b, c, o = num(4), num(5), num(6), odd(7)
        if a and b and c and o:
            result["3fuku"]["-".join(sorted((a, b, c), key=int))] = o
        a, b, o = num(8), num(9), odd(10)
        if a and b and o:
            result["2tansho"][f"{a}-{b}"] = o
        a, b, o = num(11), num(12), odd(13)
        if a and b and o:
            result["2fuku"]["-".join(sorted((a, b), key=int))] = o
    return result




def v218_parse_autorace_odds_html(text: str) -> dict:
    """AutoRace.JPの保存済みオッズHTMLから4券種の全オッズを読み取る。

    BeautifulSoupに依存せず、標準ライブラリだけで保存HTMLを解析する。
    AutoRace.JPの実ページで使われる、軸ごとに2表へ分割された形式にも対応。
    """
    from html.parser import HTMLParser

    result = {"3tan": {}, "3fuku": {}, "2tansho": {}, "2fuku": {}}
    raw = str(text or "")
    target_ids = {
        "live-odds-rt3-container",
        "live-odds-rf3-container",
        "live-odds-rt2-container",
        "live-odds-rf2-container",
        "live-odds-pop-container",
        "live-odds-tns-container",
        "live-odds-wid-container",
    }
    if not any(target_id in raw for target_id in target_ids):
        return result

    class _OddsHTMLParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.current_target = None
            self.div_target_stack = []
            self.tables = {target_id: [] for target_id in target_ids}
            self.current_table = None
            self.current_row = None
            self.current_cell = None

        @staticmethod
        def _attrs(attrs):
            return {str(k): str(v or "") for k, v in attrs}

        def handle_starttag(self, tag, attrs):
            attrs_dict = self._attrs(attrs)
            if tag == "div":
                self.div_target_stack.append(self.current_target)
                element_id = attrs_dict.get("id", "")
                if element_id in target_ids:
                    self.current_target = element_id
            if self.current_target is None:
                return
            if tag == "table":
                self.current_table = {
                    "attrs": attrs_dict,
                    "classes": set(attrs_dict.get("class", "").split()),
                    "rows": [],
                }
            elif tag == "tr" and self.current_table is not None:
                self.current_row = []
            elif tag in {"td", "th"} and self.current_row is not None:
                self.current_cell = {
                    "attrs": attrs_dict,
                    "classes": set(attrs_dict.get("class", "").split()),
                    "text": [],
                }

        def handle_data(self, data):
            if self.current_cell is not None:
                self.current_cell["text"].append(data)

        def handle_endtag(self, tag):
            if tag in {"td", "th"} and self.current_cell is not None:
                self.current_cell["text"] = " ".join(
                    " ".join(self.current_cell["text"]).split()
                )
                if self.current_row is not None:
                    self.current_row.append(self.current_cell)
                self.current_cell = None
            elif tag == "tr" and self.current_row is not None:
                if self.current_table is not None and self.current_row:
                    self.current_table["rows"].append(self.current_row)
                self.current_row = None
            elif tag == "table" and self.current_table is not None:
                if self.current_target in self.tables:
                    self.tables[self.current_target].append(self.current_table)
                self.current_table = None
            if tag == "div" and self.div_target_stack:
                self.current_target = self.div_target_stack.pop()

    parser = _OddsHTMLParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return result

    def _number(value):
        match = re.search(r"(?:^|\s)([1-8])(?:\s|$)", str(value or ""))
        return match.group(1) if match else None

    def _odd(value):
        value = str(value or "").replace(",", "").replace("〜", "～").strip()
        if not value or value in {"-", "―", "発売なし"}:
            return None
        # ワイドは「2.2～2.6」の範囲表示。長期回収率判定では安全側の下限を使う。
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if not match:
            return None
        try:
            parsed = float(match.group(1))
            return parsed if parsed > 0 else None
        except Exception:
            return None

    def _usable_tables(container_id: str):
        return [
            table for table in parser.tables.get(container_id, [])
            if "liveTable-Info" not in table.get("classes", set())
            and table.get("rows")
        ]

    def _parse_two(container_id: str, ordered: bool) -> dict:
        values = {}
        for table in _usable_tables(container_id):
            rows = table["rows"]
            headers = [_number(cell["text"]) for cell in rows[0]]
            headers = [value for value in headers if value]
            if not headers:
                continue
            for row in rows[1:]:
                cells = row
                for column_index, first in enumerate(headers):
                    base = column_index * 2
                    if base + 1 >= len(cells):
                        continue
                    second = _number(cells[base]["text"])
                    price = _odd(cells[base + 1]["text"])
                    if not second or price is None or first == second:
                        continue
                    combo = (first, second)
                    if not ordered:
                        combo = tuple(sorted(combo, key=int))
                    values["-".join(combo)] = price
        return values

    def _parse_three(container_id: str, ordered: bool) -> dict:
        values = {}
        tables = _usable_tables(container_id)
        # 1着軸（3連複では基準車）ごとに、1～4号車側と5～8号車側の2表で構成される。
        for pair_start in range(0, len(tables), 2):
            pair = tables[pair_start:pair_start + 2]
            axis = None
            for table in pair:
                first_row = table["rows"][0]
                for cell in first_row:
                    if "live-oddsTable__name" in cell.get("classes", set()):
                        axis = _number(cell["text"])
                        break
                if axis:
                    break
            if not axis:
                continue

            for table in pair:
                rows = table["rows"]
                first_row = rows[0]
                header_cells = [
                    cell for cell in first_row
                    if "live-oddsTable__name" not in cell.get("classes", set())
                ]
                headers = [_number(cell["text"]) for cell in header_cells]
                headers = [value for value in headers if value]
                if not headers:
                    continue
                for row in rows[1:]:
                    cells = row
                    for column_index, second in enumerate(headers):
                        base = column_index * 2
                        if base + 1 >= len(cells):
                            continue
                        third = _number(cells[base]["text"])
                        price = _odd(cells[base + 1]["text"])
                        if not third or price is None or len({axis, second, third}) != 3:
                            continue
                        combo = (axis, second, third)
                        if not ordered:
                            combo = tuple(sorted(combo, key=int))
                        values["-".join(combo)] = price
        return values

    def _parse_single(container_id: str) -> dict:
        values = {}
        for table in _usable_tables(container_id):
            rows = table.get("rows", [])
            if len(rows) < 2:
                continue
            cars = [_number(cell.get("text")) for cell in rows[0]]
            odds_cells = rows[1]
            for idx, car in enumerate(cars):
                if not car or idx >= len(odds_cells):
                    continue
                price = _odd(odds_cells[idx].get("text"))
                if price is not None:
                    values[str(car)] = price
        return values

    def _parse_wide(container_id: str) -> dict:
        values = {}
        for table in _usable_tables(container_id):
            rows = table.get("rows", [])
            if not rows:
                continue
            axes = [_number(cell.get("text")) for cell in rows[0]]
            axes = [x for x in axes if x]
            if not axes:
                continue
            for row in rows[1:]:
                for column_index, axis in enumerate(axes):
                    base = column_index * 2
                    if base + 1 >= len(row):
                        continue
                    other = _number(row[base].get("text"))
                    price = _odd(row[base + 1].get("text"))
                    if not other or price is None or other == axis:
                        continue
                    combo = "-".join(sorted((axis, other), key=int))
                    values[combo] = price
        return values

    result["3tan"] = _parse_three("live-odds-rt3-container", True)
    result["3fuku"] = _parse_three("live-odds-rf3-container", False)
    result["2tansho"] = _parse_two("live-odds-rt2-container", True)
    result["2fuku"] = _parse_two("live-odds-rf2-container", False)
    result["tansho"] = _parse_single("live-odds-tns-container")
    result["wide"] = _parse_wide("live-odds-wid-container")

    # 人気表だけが保存された簡易HTMLにも対応する。
    if sum(len(values) for values in result.values()) == 0:
        popular_tables = _usable_tables("live-odds-pop-container")
        if popular_tables:
            lines = ["3連単人気\t\t\t\t3連複人気\t\t\t\t2連単人気\t\t\t2連複人気"]
            for row in popular_tables[0]["rows"]:
                cols = [cell["text"] for cell in row]
                if cols:
                    lines.append("\t".join(cols))
            result = v182_parse_four_block_odds("\n".join(lines))
    return result

def v218_store_parsed_odds(namespace: str, parsed: dict) -> int:
    total = sum(len(values) for values in parsed.values())
    if total > 0:
        for parsed_key, values in parsed.items():
            st.session_state[f"saved_odds_{namespace}_{parsed_key}"] = values
    return total


# Ver221: 読み込んだ4券種の全オッズを時刻別スナップショットとして保存し、再入力なしで復元する。
def _v221_ensure_odds_tables(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS v221_odds_runs (
            race_key TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL,
            count_3tan INTEGER NOT NULL DEFAULT 0,
            count_3fuku INTEGER NOT NULL DEFAULT 0,
            count_2tansho INTEGER NOT NULL DEFAULT 0,
            count_2fuku INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (race_key, snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS v221_odds_values (
            race_key TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            bet_key TEXT NOT NULL,
            combination TEXT NOT NULL,
            odds REAL NOT NULL,
            PRIMARY KEY (race_key, snapshot_id, bet_key, combination)
        );
        CREATE INDEX IF NOT EXISTS idx_v221_odds_runs_latest
          ON v221_odds_runs(race_key, created_at DESC);
        """)
        con.commit()


def _v221_save_all_odds(db_path: str, race_key: str, parsed: dict, source: str) -> str:
    """4券種の全オッズを重複排除しつつ履歴保存する。"""
    race_key = str(race_key or '').strip()
    if not race_key:
        return ''
    clean = {}
    for bet_key in ('3tan', '3fuku', '2tansho', '2fuku', 'tansho', 'wide'):
        values = parsed.get(bet_key, {}) or {}
        clean[bet_key] = {str(k): float(v) for k, v in values.items() if v is not None and float(v) > 0}
    if sum(len(v) for v in clean.values()) <= 0:
        return ''
    payload = [(bk, combo, round(odd, 4)) for bk in clean for combo, odd in sorted(clean[bk].items())]
    snapshot_id = hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode('utf-8')).hexdigest()[:20]
    now = _v228_now_jst_iso()
    _v221_ensure_odds_tables(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute("""
            INSERT OR IGNORE INTO v221_odds_runs
            (race_key,snapshot_id,source,created_at,count_3tan,count_3fuku,count_2tansho,count_2fuku)
            VALUES (?,?,?,?,?,?,?,?)
        """, (race_key, snapshot_id, str(source or ''), now,
              len(clean['3tan']), len(clean['3fuku']), len(clean['2tansho']), len(clean['2fuku'])))
        exists = con.execute(
            'SELECT 1 FROM v221_odds_values WHERE race_key=? AND snapshot_id=? LIMIT 1',
            (race_key, snapshot_id),
        ).fetchone()
        if not exists:
            rows = [(race_key, snapshot_id, bk, combo, odd)
                    for bk, vals in clean.items() for combo, odd in vals.items()]
            con.executemany("""
                INSERT OR REPLACE INTO v221_odds_values
                (race_key,snapshot_id,bet_key,combination,odds) VALUES (?,?,?,?,?)
            """, rows)
        con.commit()
    return snapshot_id


def _v221_load_odds_snapshot(db_path: str, race_key: str, snapshot_id: str = '') -> tuple[dict, dict]:
    empty = {'3tan': {}, '3fuku': {}, '2tansho': {}, '2fuku': {}, 'tansho': {}, 'wide': {}}
    race_key = str(race_key or '').strip()
    if not race_key:
        return empty, {}
    try:
        _v221_ensure_odds_tables(db_path)
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            if snapshot_id:
                run = con.execute(
                    'SELECT * FROM v221_odds_runs WHERE race_key=? AND snapshot_id=?',
                    (race_key, snapshot_id),
                ).fetchone()
            else:
                run = con.execute(
                    'SELECT * FROM v221_odds_runs WHERE race_key=? ORDER BY created_at DESC LIMIT 1',
                    (race_key,),
                ).fetchone()
            if not run:
                return empty, {}
            rows = con.execute("""
                SELECT bet_key, combination, odds FROM v221_odds_values
                WHERE race_key=? AND snapshot_id=?
            """, (race_key, run['snapshot_id'])).fetchall()
        parsed = {k: {} for k in empty}
        for row in rows:
            if row['bet_key'] in parsed:
                parsed[row['bet_key']][str(row['combination'])] = float(row['odds'])
        return parsed, dict(run)
    except Exception:
        return empty, {}


def _v221_list_odds_snapshots(db_path: str, race_key: str, limit: int = 8) -> list[dict]:
    try:
        _v221_ensure_odds_tables(db_path)
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT * FROM v221_odds_runs WHERE race_key=?
                ORDER BY created_at DESC LIMIT ?
            """, (str(race_key or ''), int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def v202_quick_bulk_odds_input(namespace: str, race_key: str = '') -> None:
    """重い診断より先に、HTMLまたは公式4券種表からオッズを読み込む。"""
    st.markdown('<div id="quick-odds-input"></div>', unsafe_allow_html=True)
    st.subheader("オッズ一括入力")
    st.caption("AutoRace.JPの保存HTMLなら、3連単・3連複・2連単・2連複に加えて単勝・ワイドも自動入力できます。読み込んだ全オッズはDBへ保存されます。")

    # セッションにオッズがない場合は、このレースの最新保存分を自動復元する。
    odds_store_keys = [f"saved_odds_{namespace}_{k}" for k in ('3tan','3fuku','2tansho','2fuku','tansho','wide')]
    has_session_odds = any(bool(st.session_state.get(k)) for k in odds_store_keys)
    if race_key and not has_session_odds:
        restored, run = _v221_load_odds_snapshot(engine.DB_PATH, race_key)
        if sum(len(v) for v in restored.values()) > 0:
            v218_store_parsed_odds(namespace, restored)
            st.session_state[f"v221_restored_snapshot_{namespace}"] = run.get('snapshot_id', '')
            has_session_odds = True

    snapshots = _v221_list_odds_snapshots(engine.DB_PATH, race_key) if race_key else []
    if has_session_odds and st.session_state.get(f"v221_restored_snapshot_{namespace}"):
        st.success("保存済みの全オッズを自動復元しました。再入力は不要です。")
    if len(snapshots) > 1:
        labels = []
        by_label = {}
        for run in snapshots:
            created = _v228_format_saved_time(run.get('created_at',''))
            label = f"{created} / {run.get('source') or '保存オッズ'} / {run.get('count_3tan',0)+run.get('count_3fuku',0)+run.get('count_2tansho',0)+run.get('count_2fuku',0)}件"
            labels.append(label); by_label[label] = run
        c_restore, c_button = st.columns([3,1])
        with c_restore:
            selected_label = st.selectbox("保存済みオッズ履歴", labels, key=f"v221_odds_history_{namespace}")
        with c_button:
            st.write('')
            if st.button("復元", key=f"v221_restore_odds_{namespace}", use_container_width=True):
                run = by_label[selected_label]
                restored, _ = _v221_load_odds_snapshot(engine.DB_PATH, race_key, run.get('snapshot_id',''))
                v218_store_parsed_odds(namespace, restored)
                st.session_state[f"v221_restored_snapshot_{namespace}"] = run.get('snapshot_id','')
                st.rerun()

    html_file = st.file_uploader(
        "AutoRace.JPのオッズHTML／テキストファイル",
        type=["html", "htm", "txt"],
        key=f"v218_odds_html_file_{namespace}",
        help="ブラウザで保存したHTML、またはページ内容を保存したテキストを選択してください。",
    )
    html_paste_key = f"v218_odds_html_text_{namespace}"
    with st.expander("HTMLを直接貼り付ける"):
        html_paste = st.text_area(
            "HTMLソース",
            key=html_paste_key,
            height=140,
            placeholder="AutoRace.JPのオッズページHTMLを貼り付け",
        )

    if st.button("HTMLから6券種の全オッズを読み込む", key=f"v218_load_html_odds_{namespace}", use_container_width=True):
        source = str(html_paste or "")
        if html_file is not None:
            try:
                source = html_file.getvalue().decode("utf-8", errors="replace")
            except Exception:
                source = ""
        parsed = v218_parse_autorace_odds_html(source)
        total = v218_store_parsed_odds(namespace, parsed)
        if total <= 0:
            st.error("HTMLからオッズを読み取れませんでした。オッズ表が表示された状態で保存したHTMLを使用してください。")
        else:
            if race_key:
                _v221_save_all_odds(engine.DB_PATH, race_key, parsed, f"HTML/TXT:{getattr(html_file, 'name', '') or '貼付'}")
            st.success(
                f"HTML読込完了・DB保存済み：3連単{len(parsed['3tan'])}件、3連複{len(parsed['3fuku'])}件、"
                f"2連単{len(parsed['2tansho'])}件、2連複{len(parsed['2fuku'])}件"
            )
            st.rerun()

    st.markdown("##### 表をコピーして読み込む場合")
    bulk_key = f"bulk_odds_text_{namespace}"
    bulk_text = st.text_area(
        "3連単人気・3連複人気・2連単人気・2連複人気の表",
        key=bulk_key,
        height=140,
        placeholder="公式オッズ表を見出しからそのまま貼り付け",
    )
    if st.button("人気表の4券種オッズを読み込む", key=f"load_bulk_odds_{namespace}", use_container_width=True):
        parsed = v182_parse_four_block_odds(bulk_text)
        total = v218_store_parsed_odds(namespace, parsed)
        if total <= 0:
            st.error("オッズを読み取れませんでした。タブ区切りの表をそのまま貼り付けてください。")
        else:
            if race_key:
                _v221_save_all_odds(engine.DB_PATH, race_key, parsed, "人気表貼付")
            st.success(
                f"読込完了・DB保存済み：3連単{len(parsed['3tan'])}件、3連複{len(parsed['3fuku'])}件、"
                f"2連単{len(parsed['2tansho'])}件、2連複{len(parsed['2fuku'])}件"
            )
            st.rerun()


@st.cache_data(ttl=600, show_spinner=False)
def v202_cached_probability_rank_validation(db_path: str, db_mtime: float):
    return engine.v196_probability_rank_validation(db_path)


@st.cache_data(ttl=600, show_spinner=False)
def v202_cached_position_bias_profile(db_path: str, db_mtime: float):
    return engine.v198_position_bias_profile(db_path)


@st.cache_data(ttl=600, show_spinner=False)
def v202_cached_weight_validation_profile(db_path: str, db_mtime: float):
    return engine.v190_weight_validation_profile(db_path)


def v203_standard_trifecta_formations(formations, combos):
    """三連単をBOX・通常ハイフン・片折り返し（=は最大1個）だけで正確に圧縮する。"""
    from itertools import combinations, permutations, product

    target = engine._v165_normalize_combo_texts(combos, "3連単")
    if not target:
        return []
    target = set(target)
    cars = sorted({x for combo in target for x in combo})
    candidates = {}

    def add_candidate(line):
        text = str(line or "").strip().upper()
        if not text or text.count("=") > 1:
            return
        expanded = engine._v165_expand_formation_line(text, "3連単")
        if len(expanded) < 2 or not expanded.issubset(target):
            return
        frozen = frozenset(expanded)
        old = candidates.get(frozen)
        if old is None or (len(text), text) < (len(old), old):
            candidates[frozen] = text

    # 既存圧縮のうち、BOX・通常表記・片折り返しだけを再利用する。
    for line in formations or []:
        add_candidate(line)

    # 3車6通りがすべてある場合はABCBOX候補。
    for trio in combinations(cars, 3):
        if set(permutations(trio, 3)).issubset(target):
            add_candidate("".join(map(str, trio)) + "BOX")

    # 片折り返し候補 A=B-C を生成する。
    # A/Bの各集合を折り返し、成立する3着集合Cを最大限まとめる。
    nonempty_subsets = []
    for size in range(1, len(cars) + 1):
        nonempty_subsets.extend(combinations(cars, size))
    for left in nonempty_subsets:
        for middle in nonempty_subsets:
            valid_thirds = []
            for third in cars:
                expanded = set()
                for a, b in product(left, middle):
                    if len({a, b, third}) != 3:
                        continue
                    expanded.add((a, b, third))
                    expanded.add((b, a, third))
                if expanded and expanded.issubset(target):
                    valid_thirds.append(third)
            if valid_thirds:
                add_candidate(
                    "".join(map(str, left)) + "=" +
                    "".join(map(str, middle)) + "-" +
                    "".join(map(str, valid_thirds))
                )

    # 2・3着の片折り返し候補 A-B=C も、正確に成立する場合だけ許可する。
    for firsts in nonempty_subsets:
        for middle in nonempty_subsets:
            valid_thirds = []
            for third in cars:
                expanded = set()
                for a, b in product(firsts, middle):
                    if len({a, b, third}) != 3:
                        continue
                    expanded.add((a, b, third))
                    expanded.add((a, third, b))
                if expanded and expanded.issubset(target):
                    valid_thirds.append(third)
            if valid_thirds:
                add_candidate(
                    "".join(map(str, firsts)) + "-" +
                    "".join(map(str, middle)) + "=" +
                    "".join(map(str, valid_thirds))
                )

    # 大きく覆い、短い表記を優先。行同士は絶対に重複させない。
    ranked = sorted(
        ((set(expanded), line) for expanded, line in candidates.items()),
        key=lambda item: (-len(item[0]), len(item[1]), item[1]),
    )
    uncovered = set(target)
    output = []
    while uncovered:
        best = None
        for expanded, line in ranked:
            if not expanded.issubset(uncovered):
                continue
            score = (len(expanded), -len(line), line.count("="), line)
            if best is None or score > best[0]:
                best = (score, expanded, line)
        if best is None or len(best[1]) < 2:
            break
        _, expanded, line = best
        output.append(line)
        uncovered.difference_update(expanded)

    output.extend("-".join(map(str, combo)) for combo in sorted(uncovered))

    # 最終展開検証。欠落・余分・重複・二重折り返しがあれば安全な個別表記へ戻す。
    covered = set()
    for line in output:
        if str(line).count("=") > 1:
            return ["-".join(map(str, combo)) for combo in sorted(target)]
        expanded = engine._v165_expand_formation_line(line, "3連単")
        if not expanded or not expanded.issubset(target) or expanded & covered:
            return ["-".join(map(str, combo)) for combo in sorted(target)]
        covered.update(expanded)
    if covered != target:
        return ["-".join(map(str, combo)) for combo in sorted(target)]
    return output

def v182_hit_first_odds_adjustment(bets: dict, trials: int, base_info: dict, odds_map: dict) -> dict:
    """的中重視の上位順を維持したまま、オッズで1〜10点を微調整する。"""
    counter = bets.get("三連単", {}) if isinstance(bets, dict) else {}
    if not counter or not odds_map or int(trials or 0) <= 0:
        return {"available": False, "reason": "三連単オッズを読み込むと点数調整を表示します。"}
    ordered = sorted(counter.items(), key=lambda item: item[1], reverse=True)[:10]
    rows = []
    for rank, (combo, count) in enumerate(ordered, 1):
        key = "-".join(map(str, tuple(combo) if isinstance(combo, (tuple, list)) else (combo,)))
        probability = float(count) / max(int(trials), 1) * 100.0
        odds = float(odds_map.get(key, 0) or 0)
        rows.append({"rank": rank, "combo": key, "probability": probability, "odds": odds})
    if not rows or not any(r["odds"] > 0 for r in rows):
        return {"available": False, "reason": "上位10点に対応する三連単オッズが見つかりません。"}

    base_points = max(1, min(10, int(base_info.get("points") or 10)))
    candidates = []
    for n in range(max(1, base_points - 3), min(10, base_points + 3) + 1):
        chosen = rows[:n]
        cost = n * 100.0
        cover = sum(r["probability"] for r in chosen)
        known = [r for r in chosen if r["odds"] > 0]
        black = [r for r in known if r["odds"] * 100.0 >= cost]
        black_cover = sum(r["probability"] for r in black)
        low_cover = sum(r["probability"] for r in known if r["odds"] * 100.0 < cost)
        # 的中カバーを主役にし、黒字で当たり得る範囲を加点、低配当範囲と点数を軽く減点。
        score = cover + 0.35 * black_cover - 0.20 * low_cover - 0.22 * n
        candidates.append({
            "n": n, "cost": cost, "cover": cover, "black_cover": black_cover,
            "low_cover": low_cover, "known": len(known), "score": score,
        })
    best = max(candidates, key=lambda x: (x["score"], -abs(x["n"] - base_points), -x["n"]))
    final_points = int(best["n"])
    selected = rows[:final_points]
    known_selected = [r for r in selected if r["odds"] > 0]
    low_count = sum(1 for r in known_selected if r["odds"] * 100.0 < best["cost"])
    profitable_count = sum(1 for r in known_selected if r["odds"] * 100.0 >= best["cost"])

    if best["known"] < max(3, final_points // 2):
        grade, icon = "参考", "⚪"
        reason = "上位候補のオッズ入力が少ないため、点数調整は参考扱いです。"
    elif best["black_cover"] < best["cover"] * 0.35 or low_count >= max(2, final_points // 2):
        grade, icon = "非推奨", "⛔"
        reason = "的中候補はありますが、購入点数に対して低配当となる候補の割合が高めです。"
    elif best["black_cover"] >= best["cover"] * 0.70:
        grade, icon = "推奨", "✅"
        reason = "的中カバーを維持しつつ、候補総額を上回る配当余地のある組み合わせが多めです。"
    else:
        grade, icon = "的中優先なら候補", "△"
        reason = "的中カバーは妥当ですが、回収効率は中程度です。"

    delta = final_points - base_points
    if delta > 0:
        adjust_reason = f"次点候補の確率寄与と配当余地を考慮し、基本{base_points}点から{delta}点追加しました。"
    elif delta < 0:
        adjust_reason = f"下位候補の確率寄与と低配当リスクを考慮し、基本{base_points}点から{-delta}点減らしました。"
    else:
        adjust_reason = f"基本{base_points}点を維持しました。"
    return {
        "available": True, "base_points": base_points, "final_points": final_points,
        "delta": delta, "cover": best["cover"], "black_cover": best["black_cover"],
        "low_cover": best["low_cover"], "cost": best["cost"], "grade": grade, "icon": icon,
        "reason": reason, "adjust_reason": adjust_reason, "profitable_count": profitable_count,
        "low_count": low_count, "combos": [r["combo"] for r in selected],
    }


def show_v182_odds_adjusted_tight_recommendation(bets: dict, trials: int, meta: dict, odds_map: dict) -> None:
    base = v180_trifecta_tight_recommendation(bets, trials, meta)
    adjusted = v182_hit_first_odds_adjustment(bets, trials, base, odds_map)
    st.markdown("#### 🎛️ 的中重視＋オッズ点数調整")
    if not adjusted.get("available"):
        st.caption(adjusted.get("reason", "オッズを読み込むと表示します。"))
        return
    st.subheader(f"{adjusted['icon']} 最終参考：{adjusted['final_points']}点・{adjusted['grade']}")
    optional_simple = v244_optional_single_wide_candidates(bets, trials, odds_maps)
    if optional_simple:
        st.markdown("##### 🪙 余裕がある場合の単勝・ワイド候補")
        st.caption("回収率重視の本線には自動追加しません。ワイドは表示レンジの下限オッズで安全側に評価しています。")
        copy_lines = []
        for row in optional_simple:
            label = f"{row['type']} {row['combo']}"
            copy_lines.append(label)
            st.markdown(f"**{label}（{float(row['odds']):.1f}倍）**")
            st.caption(f"モデル確率 {float(row['probability']):.2f}%・単体期待値 {float(row['ev']):.1f}%｜{row['reason']}")
        v73_copy_box(
            "余裕がある場合の単勝・ワイド候補",
            "追加候補\n" + "\n".join(copy_lines),
            f"v244_optional_single_wide_{race_key}_{saved_hash}",
            height=max(120, 80 + 27 * len(copy_lines)),
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("基本点数", f"{adjusted['base_points']}点")
    c2.metric("オッズ調整", f"{adjusted['delta']:+d}点")
    c3.metric("候補累積", f"{adjusted['cover']:.2f}%")
    c4.metric("黒字余地側の累積", f"{adjusted['black_cover']:.2f}%")
    if adjusted["grade"] == "非推奨":
        st.warning(adjusted["reason"])
    elif adjusted["grade"] == "推奨":
        st.success(adjusted["reason"])
    else:
        st.info(adjusted["reason"])
    st.caption(adjusted["adjust_reason"] + f" 100円ずつなら候補総額は{int(adjusted['cost']):,}円。")
    st.caption(f"総額以上の配当余地あり {adjusted['profitable_count']}点 / 低配当側 {adjusted['low_count']}点")
    st.code("\n".join(adjusted["combos"]), language=None)
    st.caption("これは過去分布と入力オッズを使った参考判定です。的中や収益を保証するものではありません。")




# Ver187: 8車合成プランをDB保存し、結果登録後に自動照合して次回判定へ反映する。
def _v187_ensure_mixed_learning_tables(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS v187_mixed_plan_runs (
            race_key TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            points INTEGER NOT NULL,
            cost_yen INTEGER NOT NULL,
            grade TEXT,
            cover REAL,
            black REAL,
            low REAL,
            hit_average_multiple REAL,
            model_expected_multiple REAL,
            model_return_rate REAL,
            role_count INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (race_key, plan_hash)
        );
        CREATE TABLE IF NOT EXISTS v187_mixed_plan_tickets (
            race_key TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            combination TEXT NOT NULL,
            probability REAL,
            odds REAL,
            role TEXT,
            PRIMARY KEY (race_key, plan_hash, bet_type, combination)
        );
        CREATE TABLE IF NOT EXISTS v187_mixed_plan_feedback (
            race_key TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            hit INTEGER NOT NULL,
            black_hit INTEGER NOT NULL,
            gami_hit INTEGER NOT NULL,
            payout_yen INTEGER NOT NULL,
            cost_yen INTEGER NOT NULL,
            realized_multiple REAL NOT NULL,
            return_rate REAL NOT NULL,
            winning_types TEXT,
            evaluated_at TEXT NOT NULL,
            PRIMARY KEY (race_key, plan_hash)
        );
        CREATE TABLE IF NOT EXISTS v187_mixed_ticket_feedback (
            race_key TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            combination TEXT NOT NULL,
            hit INTEGER NOT NULL,
            payout_yen INTEGER NOT NULL,
            PRIMARY KEY (race_key, plan_hash, bet_type, combination)
        );
        """)
        # Ver215: 予測時点のプランを後から完全に切り分けられるよう、メタ情報を保持する。
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(v187_mixed_plan_runs)").fetchall()}
        for col_name, col_type in [
            ("app_version", "TEXT"),
            ("logic_version", "TEXT"),
            ("race_date", "TEXT"),
            ("venue", "TEXT"),
            ("race_no", "TEXT"),
            ("starter_count", "INTEGER"),
        ]:
            if col_name not in existing_cols:
                con.execute(f"ALTER TABLE v187_mixed_plan_runs ADD COLUMN {col_name} {col_type}")
        con.commit()


def _v212_norm_bet_type(bet_type: str) -> str:
    """券種名の漢数字・数字表記を同一キーへ統一する。"""
    text = str(bet_type or "").strip().replace("　", "")
    aliases = {
        "三連単": "3連単", "3連単": "3連単",
        "三連複": "3連複", "3連複": "3連複",
        "二連単": "2連単", "2連単": "2連単",
        "二連複": "2連複", "2連複": "2連複",
    }
    return aliases.get(text, text)


def _v187_norm_combo(bet_type: str, combo: str) -> str:
    bet_type = _v212_norm_bet_type(bet_type)
    nums = re.findall(r"\d+", str(combo))
    if bet_type in ("3連複", "2連複"):
        nums = sorted(nums, key=int)
    return "-".join(nums)


def _v187_sync_mixed_feedback(db_path: str) -> int:
    """結果登録済みプランを照合。戻り値は今回新しく評価した件数。"""
    _v187_ensure_mixed_learning_tables(db_path)
    done = 0
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        plans = con.execute("""
            SELECT r.* FROM v187_mixed_plan_runs r
            LEFT JOIN v187_mixed_plan_feedback f
              ON f.race_key=r.race_key AND f.plan_hash=r.plan_hash
            WHERE f.race_key IS NULL
              AND EXISTS (SELECT 1 FROM result_races rr WHERE rr.race_key=r.race_key
                          AND COALESCE(rr.learning_eligible,1)=1)
        """).fetchall()
        for plan in plans:
            payouts = con.execute(
                "SELECT bet_type, combination, payout_yen FROM result_payouts WHERE race_key=?",
                (plan["race_key"],),
            ).fetchall()
            payout_map = {}
            for p in payouts:
                payout_map[(_v212_norm_bet_type(p["bet_type"]), _v187_norm_combo(p["bet_type"], p["combination"]))] = int(p["payout_yen"] or 0)
            if not payout_map:
                continue
            tickets = con.execute(
                "SELECT * FROM v187_mixed_plan_tickets WHERE race_key=? AND plan_hash=?",
                (plan["race_key"], plan["plan_hash"]),
            ).fetchall()
            total_payout = 0
            winning_types = []
            for t in tickets:
                key = (_v212_norm_bet_type(t["bet_type"]), _v187_norm_combo(t["bet_type"], t["combination"]))
                pay = int(payout_map.get(key, 0))
                hit = int(pay > 0)
                total_payout += pay
                if hit:
                    winning_types.append(t["bet_type"])
                con.execute("""
                    INSERT OR REPLACE INTO v187_mixed_ticket_feedback
                    (race_key,plan_hash,bet_type,combination,hit,payout_yen) VALUES (?,?,?,?,?,?)
                """, (plan["race_key"],plan["plan_hash"],t["bet_type"],t["combination"],hit,pay))
            cost = int(plan["cost_yen"] or len(tickets)*100)
            hit = int(total_payout > 0)
            black = int(total_payout >= cost and hit)
            gami = int(0 < total_payout < cost)
            multiple = total_payout / cost if cost else 0.0
            con.execute("""
                INSERT OR REPLACE INTO v187_mixed_plan_feedback
                (race_key,plan_hash,hit,black_hit,gami_hit,payout_yen,cost_yen,realized_multiple,return_rate,winning_types,evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (plan["race_key"],plan["plan_hash"],hit,black,gami,total_payout,cost,multiple,multiple*100.0,
                    json.dumps(sorted(set(winning_types)), ensure_ascii=False), _v228_now_jst_iso()))
            done += 1
        con.commit()
    return done


def _v212_recalculate_plan_feedback(db_path: str, race_key: str, plan_hash: str) -> None:
    """表記差を吸収して指定プランの実績を再計算し、過去の誤判定も修復する。"""
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        plan = con.execute(
            "SELECT * FROM v187_mixed_plan_runs WHERE race_key=? AND plan_hash=?",
            (race_key, plan_hash),
        ).fetchone()
        if plan is None:
            return
        payouts = con.execute(
            "SELECT bet_type, combination, payout_yen FROM result_payouts WHERE race_key=?",
            (race_key,),
        ).fetchall()
        if not payouts:
            return
        payout_map = {
            (_v212_norm_bet_type(p["bet_type"]), _v187_norm_combo(p["bet_type"], p["combination"])): int(p["payout_yen"] or 0)
            for p in payouts
        }
        tickets = con.execute(
            "SELECT * FROM v187_mixed_plan_tickets WHERE race_key=? AND plan_hash=?",
            (race_key, plan_hash),
        ).fetchall()
        total_payout = 0
        winning_types = []
        for t in tickets:
            key = (_v212_norm_bet_type(t["bet_type"]), _v187_norm_combo(t["bet_type"], t["combination"]))
            pay = int(payout_map.get(key, 0))
            hit = int(pay > 0)
            total_payout += pay
            if hit:
                winning_types.append(_v212_norm_bet_type(t["bet_type"]))
            con.execute("""
                INSERT OR REPLACE INTO v187_mixed_ticket_feedback
                (race_key,plan_hash,bet_type,combination,hit,payout_yen) VALUES (?,?,?,?,?,?)
            """, (race_key, plan_hash, t["bet_type"], t["combination"], hit, pay))
        cost = int(plan["cost_yen"] or len(tickets) * 100)
        hit = int(total_payout > 0)
        black = int(hit and total_payout >= cost)
        gami = int(0 < total_payout < cost)
        multiple = total_payout / cost if cost else 0.0
        con.execute("""
            INSERT OR REPLACE INTO v187_mixed_plan_feedback
            (race_key,plan_hash,hit,black_hit,gami_hit,payout_yen,cost_yen,realized_multiple,return_rate,winning_types,evaluated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (race_key, plan_hash, hit, black, gami, total_payout, cost, multiple, multiple * 100.0,
                json.dumps(sorted(set(winning_types)), ensure_ascii=False), _v228_now_jst_iso()))
        con.commit()


def _v208_latest_mixed_plan_result(race_key: str, db_path: str) -> dict:
    """予測時に保存された最新の回収率重視プランを、登録済み払戻と照合して返す。"""
    if not race_key:
        return {"available": False, "reason": "レースキーがありません。"}
    _v187_ensure_mixed_learning_tables(db_path)
    _v187_sync_mixed_feedback(db_path)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        plan = con.execute("""
            SELECT r.*, f.hit, f.black_hit, f.gami_hit, f.payout_yen,
                   f.return_rate, f.winning_types, f.evaluated_at
            FROM v187_mixed_plan_runs r
            LEFT JOIN v187_mixed_plan_feedback f
              ON f.race_key=r.race_key AND f.plan_hash=r.plan_hash
            WHERE r.race_key=?
            ORDER BY datetime(r.created_at) DESC, r.rowid DESC
            LIMIT 1
        """, (race_key,)).fetchone()
        if plan is None:
            return {"available": False, "reason": "このレースでは回収率重視プランが保存されていません。"}
        latest_plan_hash = plan["plan_hash"]
    _v212_recalculate_plan_feedback(db_path, race_key, latest_plan_hash)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        plan = con.execute("""
            SELECT r.*, f.hit, f.black_hit, f.gami_hit, f.payout_yen,
                   f.return_rate, f.winning_types, f.evaluated_at
            FROM v187_mixed_plan_runs r
            LEFT JOIN v187_mixed_plan_feedback f
              ON f.race_key=r.race_key AND f.plan_hash=r.plan_hash
            WHERE r.race_key=? AND r.plan_hash=?
            LIMIT 1
        """, (race_key, latest_plan_hash)).fetchone()
        tickets = con.execute("""
            SELECT t.bet_type, t.combination, t.odds, t.role,
                   COALESCE(f.hit,0) AS hit, COALESCE(f.payout_yen,0) AS payout_yen
            FROM v187_mixed_plan_tickets t
            LEFT JOIN v187_mixed_ticket_feedback f
              ON f.race_key=t.race_key AND f.plan_hash=t.plan_hash
             AND f.bet_type=t.bet_type AND f.combination=t.combination
            WHERE t.race_key=? AND t.plan_hash=?
            ORDER BY CASE t.bet_type WHEN '3連単' THEN 1 WHEN '3連複' THEN 2 WHEN '2連単' THEN 3 WHEN '2連複' THEN 4 ELSE 9 END, t.combination
        """, (race_key, plan["plan_hash"])).fetchall()
    evaluated = plan["return_rate"] is not None
    hit_tickets = [dict(t) for t in tickets if int(t["hit"] or 0) == 1]
    cost = int(plan["cost_yen"] or len(tickets) * 100)
    payout = int(plan["payout_yen"] or 0) if evaluated else 0
    return {
        "available": True,
        "evaluated": evaluated,
        "race_key": race_key,
        "plan_hash": plan["plan_hash"],
        "created_at": plan["created_at"],
        "points": int(plan["points"] or len(tickets)),
        "cost_yen": cost,
        "payout_yen": payout,
        "profit_yen": payout - cost if evaluated else None,
        "return_rate": float(plan["return_rate"] or 0.0) if evaluated else None,
        "hit": bool(plan["hit"]) if evaluated else False,
        "black_hit": bool(plan["black_hit"]) if evaluated else False,
        "gami_hit": bool(plan["gami_hit"]) if evaluated else False,
        "hit_tickets": hit_tickets,
        "tickets": [dict(t) for t in tickets],
    }


def _v208_render_mixed_plan_result(result: dict) -> None:
    """結果分析の先頭付近に、回収率重視プランを買った想定の実績を表示する。"""
    st.subheader("💰 回収率重視プランの実結果")
    if not isinstance(result, dict) or not result.get("available"):
        st.info((result or {}).get("reason", "保存された回収率重視プランがありません。"))
        return
    if not result.get("evaluated"):
        st.warning("プランは保存されていますが、払戻金との照合がまだ完了していません。")
        return
    hit = bool(result.get("hit"))
    black = bool(result.get("black_hit"))
    gami = bool(result.get("gami_hit"))
    if not hit:
        verdict = "× 外れ"
    elif black:
        verdict = "◎ 的中・黒字"
    elif gami:
        verdict = "△ 的中・ガミ"
    else:
        verdict = "○ 的中"
    a,b,c,d = st.columns(4)
    a.metric("的中判定", verdict)
    b.metric("購入想定", f"{int(result.get('cost_yen',0)):,}円")
    c.metric("払戻合計", f"{int(result.get('payout_yen',0)):,}円")
    d.metric("実回収率", f"{float(result.get('return_rate',0)):.1f}%")
    profit = int(result.get("profit_yen") or 0)
    st.metric("収支", f"{profit:+,}円")
    hit_tickets = result.get("hit_tickets") or []
    if hit_tickets:
        rows=[]
        for t in hit_tickets:
            rows.append({
                "券種": t.get("bet_type", ""),
                "的中買い目": t.get("combination", ""),
                "払戻": int(t.get("payout_yen") or 0),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
            column_config={"払戻": st.column_config.NumberColumn(format="%d円")})
        st.caption("複数券種が同時的中した場合は、100円購入時の払戻を合算しています。")
    else:
        st.caption("保存された推奨買い目に的中券はありませんでした。")
    st.caption(f"予測時に保存された最新プラン（{int(result.get('points',0))}点）だけで判定しています。結果確認後の買い目差し替えは行いません。")


def _v187_learning_profile(db_path: str) -> dict:
    _v187_ensure_mixed_learning_tables(db_path)
    _v187_sync_mixed_feedback(db_path)
    out = {"samples":0, "hit_rate":None, "black_rate":None, "gami_rate":None, "return_rate":None, "type_weights":{}}
    with sqlite3.connect(db_path) as con:
        row = con.execute("""
            SELECT COUNT(*), AVG(hit)*100.0, AVG(black_hit)*100.0, AVG(gami_hit)*100.0, AVG(return_rate)
            FROM v187_mixed_plan_feedback
        """).fetchone()
        if row and int(row[0] or 0)>0:
            out.update(samples=int(row[0]), hit_rate=float(row[1] or 0), black_rate=float(row[2] or 0),
                       gami_rate=float(row[3] or 0), return_rate=float(row[4] or 0))
        rows = con.execute("""
            SELECT bet_type, COUNT(*) n, AVG(hit)*100.0 hit_rate, AVG(payout_yen) avg_payout
            FROM v187_mixed_ticket_feedback GROUP BY bet_type
        """).fetchall()
        for bet_type,n,hit_rate,avg_payout in rows:
            # 少数データは1.0へ縮小。実績が増えるほど0.80～1.20の範囲で効かせる。
            reliability = min(1.0, float(n)/30.0)
            raw = 0.80 + min(0.40, max(0.0, float(hit_rate or 0)/25.0))
            out["type_weights"][bet_type] = 1.0 + (raw-1.0)*reliability
    return out


def _v215_race_meta_from_key(race_key: str, db_path: str) -> dict:
    """レースキーまたは結果DBから、集計用の日付・開催場・Rを取得する。"""
    out = {"race_date": "", "venue": "", "race_no": "", "starter_count": None}
    key = str(race_key or "")
    m = re.match(r"^(\d{8})_([^_]+)_(\d+)R$", key)
    if m:
        ymd, venue, race_no = m.groups()
        out.update(race_date=f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}", venue=venue, race_no=race_no)
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT race_date, venue, race_no FROM result_races WHERE race_key=? LIMIT 1",
                (key,),
            ).fetchone()
            if row:
                out["race_date"] = str(row["race_date"] or out["race_date"])
                out["venue"] = str(row["venue"] or out["venue"])
                out["race_no"] = str(row["race_no"] or out["race_no"])
    except Exception:
        pass
    return out


def _v187_save_mixed_plan(db_path: str, race_key: str, result: dict, app_version: str | None = None) -> str:
    """回収率重視プランを、予測時点の買い目・オッズ・確率・版情報ごと完全保存する。"""
    source_version = str(app_version or _V231_APP_VERSION or "Unknown").strip() or "Unknown"
    _v187_ensure_mixed_learning_tables(db_path)
    # Ver247管理修正: 同じ買い目でもアプリ版が違えば別プランとして保存する。
    # これにより、旧版の回収率記録を新版の保存で上書きしない。
    payload = {
        "app_version": source_version,
        "logic_version": "return_plan_v234",
        "tickets": [
            (t.get("type"), t.get("combo"), round(float(t.get("odds", 0)), 3), round(float(t.get("probability", 0)), 5))
            for t in result.get("tickets", [])
        ],
    }
    plan_hash = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    now = _v228_now_jst_iso()
    meta = _v215_race_meta_from_key(str(race_key), db_path)
    starter_count = result.get("starter_count")
    try:
        starter_count = int(starter_count) if starter_count else None
    except Exception:
        starter_count = None
    with sqlite3.connect(db_path) as con:
        cur = con.execute("""
            INSERT OR IGNORE INTO v187_mixed_plan_runs
            (race_key,plan_hash,points,cost_yen,grade,cover,black,low,hit_average_multiple,
             model_expected_multiple,model_return_rate,role_count,created_at,
             app_version,logic_version,race_date,venue,race_no,starter_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(race_key), plan_hash, int(result.get("points", 0)), int(result.get("cost", 0)), result.get("grade"),
            float(result.get("cover", 0)), float(result.get("black", 0)), float(result.get("low", 0)),
            float(result.get("hit_average_multiple", 0)), float(result.get("model_expected_multiple", 0)),
            float(result.get("model_return_rate", 0)), len(result.get("grouped", {})), now,
            source_version, "return_plan_v234", meta.get("race_date"), meta.get("venue"), meta.get("race_no"), starter_count,
        ))
        # 初回保存時だけ買い目を登録する。既存の同版スナップショットも変更しない。
        if int(cur.rowcount or 0) > 0:
            for t in result.get("tickets", []):
                con.execute("""
                    INSERT OR IGNORE INTO v187_mixed_plan_tickets
                    (race_key,plan_hash,bet_type,combination,probability,odds,role) VALUES (?,?,?,?,?,?,?)
                """, (
                    str(race_key), plan_hash, _v212_norm_bet_type(t.get("type")), t.get("combo"),
                    float(t.get("probability", 0)), float(t.get("odds", 0)), t.get("role"),
                ))
        con.commit()
    _v187_sync_mixed_feedback(db_path)
    return plan_hash


def _v195_return_calibration(db_path: str) -> dict:
    """保存済み合成のモデル回収率と実回収率の乖離を、少数標本では弱く反映する。"""
    _v187_ensure_mixed_learning_tables(db_path)
    _v187_sync_mixed_feedback(db_path)
    out = {"samples": 0, "raw_factor": 1.0, "factor": 1.0, "actual_return": None}
    try:
        with sqlite3.connect(db_path) as con:
            row = con.execute("""
                SELECT COUNT(*), SUM(f.payout_yen), SUM(f.cost_yen),
                       SUM(f.cost_yen * COALESCE(r.model_return_rate,0) / 100.0)
                FROM v187_mixed_plan_feedback f
                JOIN v187_mixed_plan_runs r
                  ON r.race_key=f.race_key AND r.plan_hash=f.plan_hash
            """).fetchone()
        n, payout, cost, model_payout = row if row else (0,0,0,0)
        n=int(n or 0); payout=float(payout or 0); cost=float(cost or 0); model_payout=float(model_payout or 0)
        raw = payout/model_payout if model_payout>0 else 1.0
        raw = min(1.10, max(0.35, raw))
        reliability=min(1.0, n/30.0)
        factor=1.0+(raw-1.0)*reliability
        out.update(samples=n, raw_factor=raw, factor=factor, actual_return=(payout/cost*100.0 if cost>0 else None))
    except Exception:
        pass
    return out

def v184_eight_car_mixed_plan(bets: dict, trials: int, meta: dict, odds_maps: dict) -> dict:
    """6〜8車立て向けの役割分担型・回収率合成。

    車立てに応じて三連単中心度を変え、着順ずれ・3着抜け・1・2着逆転を補う。
    Ver191では単なる合成的中率ではなく、購入総額を超える黒字的中率を最優先する。
    低配当保険は、ほかの券種との同時的中を含めて黒字側を実際に増やす場合だけ採用する。
    """
    starter_count = engine.v102_starter_count_for_meta(meta, engine.DB_PATH)
    if not starter_count or int(starter_count) not in (6, 7, 8):
        return {"available": False, "reason": "回収率重視の合成推奨は6〜8車立てに対応しています。"}
    starter_count = int(starter_count)
    if not isinstance(bets, dict) or int(trials or 0) <= 0:
        return {"available": False, "reason": "シミュレーション確率を取得できません。"}

    tri_counter = bets.get("三連単", {}) or {}
    if not tri_counter:
        return {"available": False, "reason": "三連単シミュレーションがありません。"}

    learning = _v187_learning_profile(engine.DB_PATH)
    type_weights = learning.get("type_weights", {})

    # 車立て別に役割を変更。6車は三連単中心、7車は準中心、8車は複数券種の補完を厚くする。
    if starter_count == 6:
        type_specs = {
            "三連単": {"counter": "三連単", "odds": "3tan", "limit": 24, "cap": 10, "target_cover": None, "role": "主軸・着順まで一致"},
            "三連複": {"counter": "三連複", "odds": "3fuku", "limit": 999, "cap": 3, "target_cover": 86.0, "role": "着順ずれを少点数で補完"},
            "2連単": {"counter": "2車単", "odds": "2tansho", "limit": 999, "cap": 2, "target_cover": 84.0, "role": "3着抜けの限定保険"},
            "2連複": {"counter": "2車複", "odds": "2fuku", "limit": 999, "cap": 1, "target_cover": 82.0, "role": "逆転保険・黒字時のみ"},
        }
    elif starter_count == 7:
        type_specs = {
            "三連単": {"counter": "三連単", "odds": "3tan", "limit": 18, "cap": 9, "target_cover": None, "role": "本線・着順まで一致"},
            "三連複": {"counter": "三連複", "odds": "3fuku", "limit": 999, "cap": 5, "target_cover": 88.0, "role": "上位3車の着順ずれ保険"},
            "2連単": {"counter": "2車単", "odds": "2tansho", "limit": 999, "cap": 4, "target_cover": 88.0, "role": "1・2着一致／3着抜け保険"},
            "2連複": {"counter": "2車複", "odds": "2fuku", "limit": 999, "cap": 2, "target_cover": 86.0, "role": "1・2着逆転保険"},
        }
    else:
        type_specs = {
            "三連単": {"counter": "三連単", "odds": "3tan", "limit": 14, "cap": 9, "target_cover": None, "role": "本線・着順まで一致"},
            "三連複": {"counter": "三連複", "odds": "3fuku", "limit": 999, "cap": 7, "target_cover": 90.0, "role": "上位3車の着順ずれ保険"},
            "2連単": {"counter": "2車単", "odds": "2tansho", "limit": 999, "cap": 6, "target_cover": 90.0, "role": "1・2着一致／3着抜け保険"},
            "2連複": {"counter": "2車複", "odds": "2fuku", "limit": 999, "cap": 4, "target_cover": 88.0, "role": "1・2着逆転保険"},
        }

    def combo_text(value, unordered=False):
        vals = tuple(value) if isinstance(value, (tuple, list)) else (value,)
        nums = [str(int(v)) for v in vals]
        if unordered:
            nums = sorted(nums, key=int)
        return "-".join(nums)

    outcomes = []
    for combo, count in tri_counter.items():
        vals = tuple(int(v) for v in (tuple(combo) if isinstance(combo, (tuple, list)) else (combo,)))
        if len(vals) == 3:
            outcomes.append((vals, float(count) / max(int(trials), 1) * 100.0))
    if not outcomes:
        return {"available": False, "reason": "三連単結果空間を作れませんでした。"}

    def ticket_matches(ticket, outcome):
        a, b, c = outcome
        nums = tuple(int(x) for x in ticket["combo"].split("-"))
        if ticket["type"] == "三連単":
            return nums == (a, b, c)
        if ticket["type"] == "三連複":
            return tuple(sorted(nums)) == tuple(sorted((a, b, c)))
        if ticket["type"] == "2連単":
            return nums == (a, b)
        return tuple(sorted(nums)) == tuple(sorted((a, b)))

    candidates = []
    pool_summary = {}
    for label, spec in type_specs.items():
        counter = bets.get(spec["counter"], {}) or {}
        odds_map = odds_maps.get(spec["odds"], {}) or {}
        unordered = label in ("三連複", "2連複")
        ordered = sorted(counter.items(), key=lambda x: x[1], reverse=True)
        added = 0
        cumulative = 0.0
        target_cover = spec.get("target_cover")
        for combo, count in ordered:
            probability = float(count) / max(int(trials), 1) * 100.0
            key = combo_text(combo, unordered=unordered)
            odds = float(odds_map.get(key, 0) or 0)
            # オッズが無い券は最終候補にできないため、候補母集団の累積にも含めない。
            if odds <= 0:
                continue
            learned_weight = float(type_weights.get(label, 1.0))
            ticket = {
                "type": label, "combo": key, "probability": probability,
                "odds": odds, "cap": int(spec["cap"]), "role": spec["role"],
                "learned_weight": learned_weight,
            }
            matched = {i for i, (outcome, _) in enumerate(outcomes) if ticket_matches(ticket, outcome)}
            if not matched:
                continue
            ticket["matched"] = matched
            ticket["matched_probability"] = sum(outcomes[i][1] for i in matched)
            candidates.append(ticket)
            added += 1
            cumulative += probability
            # 三連単は従来通り上位件数。その他は累積88〜90%到達まで候補化。
            if target_cover is not None and cumulative >= float(target_cover):
                break
            if target_cover is None and added >= int(spec["limit"]):
                break
        pool_summary[label] = {
            "points": added,
            "cover": cumulative,
            "target": target_cover,
        }

    # Ver201: 合成最適化で高確率本線まで削らないよう、券種ごとに保護候補を設定する。
    # 点数を固定せず、順位と確率の両方で判定する。保護候補は回収率判定には含めるが、
    # 最終構成の組み替え処理では削除対象にしない。
    protected_ids = set()
    protected_reasons = {}

    def protect_ticket(ticket, reason):
        key = (str(ticket.get("type")), str(ticket.get("combo")))
        protected_ids.add(key)
        protected_reasons.setdefault(key, []).append(str(reason))
        ticket["protected"] = True

    by_type = {}
    for ticket in candidates:
        by_type.setdefault(ticket["type"], []).append(ticket)
    for rows in by_type.values():
        rows.sort(key=lambda t: (float(t.get("probability", 0.0)), float(t.get("odds", 0.0))), reverse=True)

    # 三連単は全車立てで上位2点を必ず保護。上位と差が小さい候補も最大4点まで保護する。
    tri_rows_all = by_type.get("三連単", [])
    if tri_rows_all:
        tri_top = float(tri_rows_all[0].get("probability", 0.0))
        for idx, ticket in enumerate(tri_rows_all[:4]):
            prob = float(ticket.get("probability", 0.0))
            if idx < 2 or (prob >= 2.5 and prob >= tri_top * 0.55):
                protect_ticket(ticket, f"三連単{idx + 1}位・高確率本線")

    # 三連複は広い結果を1点で拾うため、上位2点と単独確率閾値以上を保護する。
    trio_threshold = {6: 12.0, 7: 10.0, 8: 8.0}[starter_count]
    for idx, ticket in enumerate(by_type.get("三連複", [])):
        prob = float(ticket.get("probability", 0.0))
        if idx < 2 or prob >= trio_threshold:
            protect_ticket(ticket, f"三連複{idx + 1}位・確率{prob:.2f}%")

    # 2連単は最上位を保護し、2位も首位との差が小さい場合は残す。
    exacta_rows = by_type.get("2連単", [])
    if exacta_rows:
        exacta_top = float(exacta_rows[0].get("probability", 0.0))
        for idx, ticket in enumerate(exacta_rows[:2]):
            prob = float(ticket.get("probability", 0.0))
            if idx == 0 or (prob >= 6.0 and prob >= exacta_top * 0.55):
                protect_ticket(ticket, f"2連単{idx + 1}位・高確率本線")

    # 2連複も広い結果を拾うため上位1点を保護。2位は確率が十分高い場合に保護する。
    quinella_threshold = {6: 15.0, 7: 12.0, 8: 10.0}[starter_count]
    for idx, ticket in enumerate(by_type.get("2連複", [])[:2]):
        prob = float(ticket.get("probability", 0.0))
        if idx == 0 or prob >= quinella_threshold:
            protect_ticket(ticket, f"2連複{idx + 1}位・確率{prob:.2f}%")

    protected_tickets = [
        ticket for ticket in candidates
        if (str(ticket.get("type")), str(ticket.get("combo"))) in protected_ids
    ]

    tri_candidates = [c for c in candidates if c["type"] == "三連単"]
    if len(tri_candidates) < 2:
        return {"available": False, "reason": "三連単上位候補のオッズが2点以上必要です。"}
    if len(candidates) < 6:
        return {"available": False, "reason": "合成判定に必要なオッズ候補が不足しています。4券種表を読み込んでください。"}

    def evaluate(plan):
        n = len(plan)
        cost = n * 100.0
        cover = black = low = expected_return = 0.0
        covered = set()
        hit_payout_rows = []
        for i, (outcome, probability) in enumerate(outcomes):
            payout = sum(t["odds"] * 100.0 for t in plan if i in t["matched"])
            if payout > 0:
                covered.add(i)
                cover += probability
                expected_return += probability / 100.0 * payout
                hit_payout_rows.append((float(payout), float(probability)))
                if payout >= cost:
                    black += probability
                else:
                    low += probability
        counts = {}
        for t in plan:
            counts[t["type"]] = counts.get(t["type"], 0) + 1
        diversity = len(counts)
        role_bonus = 0.0
        if counts.get("三連単", 0) >= 2:
            role_bonus += 0.5
        if counts.get("三連複", 0) >= 1:
            role_bonus += 0.8
        if counts.get("2連単", 0) >= 1:
            role_bonus += 0.8
        if counts.get("2連複", 0) >= 1:
            role_bonus += 0.35
        # 合成倍率は候補総額に対する払戻倍率。複数券種が同時的中する結果では払戻を合算する。
        if hit_payout_rows and cost > 0 and cover > 0:
            hit_average_payout = expected_return / (cover / 100.0)
            hit_average_multiple = hit_average_payout / cost
            hit_min_multiple = min(payout / cost for payout, _ in hit_payout_rows)
            hit_max_multiple = max(payout / cost for payout, _ in hit_payout_rows)
        else:
            hit_average_payout = 0.0
            hit_average_multiple = 0.0
            hit_min_multiple = 0.0
            hit_max_multiple = 0.0
        model_expected_multiple = expected_return / cost if cost else 0.0
        # Ver191: 黒字的中率を主役にする。単なる的中範囲とガミ的中は強く評価しない。
        # 点数増加は購入総額そのものを押し上げるため、以前より明確に減点する。
        tri_share = (counts.get("三連単", 0) / max(1, n))
        field_bonus = (0.34 * tri_share if starter_count == 6 else (0.12 * tri_share if starter_count == 7 else 0.0))
        score = 0.48 * cover + 1.22 * black - 0.72 * low + 0.55 * role_bonus - 0.16 * n + field_bonus
        black_share_of_hits = black / cover * 100.0 if cover > 0 else 0.0
        gami_share_of_hits = low / cover * 100.0 if cover > 0 else 0.0
        return {
            "points": n, "cost": cost, "cover": cover, "black": black, "low": low,
            "miss": max(0.0, 100.0 - cover), "expected_return": expected_return,
            "model_return_rate": model_expected_multiple * 100.0,
            "model_expected_multiple": model_expected_multiple,
            "hit_average_payout": hit_average_payout,
            "hit_average_multiple": hit_average_multiple,
            "hit_min_multiple": hit_min_multiple,
            "hit_max_multiple": hit_max_multiple,
            "black_share_of_hits": black_share_of_hits,
            "gami_share_of_hits": gami_share_of_hits,
            "score": score, "diversity": diversity, "counts": counts, "covered": covered,
        }

    def marginal(plan, cand):
        before = evaluate(plan)
        after = evaluate(plan + [cand])
        unique = cand["matched"] - before["covered"]
        unique_prob = sum(outcomes[i][1] for i in unique)
        return {
            "unique_prob": unique_prob,
            "cover_gain": after["cover"] - before["cover"],
            "black_gain": after["black"] - before["black"],
            "low_gain": after["low"] - before["low"],
            "score_gain": after["score"] - before["score"],
            "after": after,
        }

    # Ver189: 三連単も2点固定にせず、上位2〜6点を合成全体の土台として比較する。
    # 的中範囲を優先しつつ、追加による黒字側の改善、ガミ化、点数増を同時に評価する。
    tri_seed_options = []
    max_tri_seed = min(8 if starter_count == 6 else (7 if starter_count == 7 else 6), len(tri_candidates))
    for k in range(2, max_tri_seed + 1):
        seed_plan = tri_candidates[:k]
        seed_metrics = evaluate(seed_plan)
        # 三連単だけの土台でも黒字側を優先し、ガミ化と点数増を強めに抑える。
        seed_utility = (
            seed_metrics["cover"]
            + 0.52 * seed_metrics["black"]
            - 0.30 * seed_metrics["low"]
            - 0.16 * k
        )
        tri_seed_options.append((k, seed_plan, seed_metrics, seed_utility))

    best_seed_utility = max(x[3] for x in tri_seed_options)
    # 最高評価にほぼ並ぶなら少ない点数を優先し、無意味な膨張を避ける。
    seed_near = [x for x in tri_seed_options if x[3] >= best_seed_utility - 0.35]
    tri_seed_points, plan, tri_seed_metrics, _ = min(
        seed_near,
        key=lambda x: (x[0], -x[2]["black"], -x[2]["cover"], x[2]["low"]),
    )
    plan = list(plan)
    remaining = [c for c in candidates if c not in plan]

    # まず異なる外れ方を補う券種を1点ずつ検討する。
    for required_type in ("三連複", "2連単", "2連複"):
        choices = []
        for cand in remaining:
            if cand["type"] != required_type:
                continue
            mg = marginal(plan, cand)
            lw = float(cand.get("learned_weight", 1.0))
            new_cost = (len(plan) + 1) * 100.0
            solo_recovers = float(cand.get("odds", 0.0)) * 100.0 >= new_cost
            key = ((1.35 * mg["black_gain"] + 0.35 * mg["cover_gain"] - 0.75 * max(0.0, mg["low_gain"])) * lw,
                   int(solo_recovers), mg["unique_prob"] * lw, cand["probability"], cand["odds"])
            choices.append((key, cand, mg))
        if choices:
            key, cand, mg = max(choices, key=lambda x: x[0])
            # 役割券でも、ほとんど範囲が増えないものは無理に入れない。
            new_cost = (len(plan) + 1) * 100.0
            solo_recovers = float(cand.get("odds", 0.0)) * 100.0 >= new_cost
            min_black_gain = 0.18 if required_type == "2連複" else 0.28
            # 低配当保険は、単独回収できるか、同時的中込みで黒字確率を明確に増やす場合だけ採用。
            if mg["black_gain"] >= min_black_gain and (solo_recovers or mg["black_gain"] >= 0.65):
                plan.append(cand)
                remaining.remove(cand)

    snapshots = []
    if len(plan) >= 6:
        snapshots.append((list(plan), evaluate(plan)))

    # 基本上限は12点。13〜14点目は黒字側が明確に伸びる場合だけ例外採用する。
    while len(plan) < 14 and remaining:
        counts = evaluate(plan)["counts"]
        best = None
        for cand in remaining:
            if counts.get(cand["type"], 0) >= cand["cap"]:
                continue
            mg = marginal(plan, cand)
            # 既存券と重複するだけの券より、新しい外れ方を拾う券を優先。
            lw = float(cand.get("learned_weight", 1.0))
            new_cost = (len(plan) + 1) * 100.0
            solo_recovers = float(cand.get("odds", 0.0)) * 100.0 >= new_cost
            complement = (1.50 * mg["black_gain"] + 0.28 * mg["cover_gain"]
                          - 0.78 * max(0.0, mg["low_gain"]) + 0.10 * mg["unique_prob"]) * lw
            if cand["type"] != "三連単" and counts.get(cand["type"], 0) == 0 and mg["black_gain"] > 0:
                complement += 0.18
            key = (complement, mg["black_gain"], int(solo_recovers), mg["score_gain"], cand["odds"])
            if best is None or key > best[0]:
                best = (key, cand, mg)
        if best is None:
            break
        _, cand, mg = best
        new_cost = (len(plan) + 1) * 100.0
        solo_recovers = float(cand.get("odds", 0.0)) * 100.0 >= new_cost
        # 黒字確率が増えない追加は不採用。低配当券は、強い黒字改善が無ければ止める。
        if mg["black_gain"] <= 0.0:
            break
        if not solo_recovers and mg["black_gain"] < 0.60:
            break
        if len(plan) >= 8 and mg["black_gain"] < 0.22:
            break
        if len(plan) >= 12 and (mg["black_gain"] < 0.80 or mg["after"]["model_expected_multiple"] < evaluate(plan)["model_expected_multiple"]):
            break
        plan.append(cand)
        remaining.remove(cand)
        if len(plan) >= 6:
            snapshots.append((list(plan), evaluate(plan)))

    if not snapshots:
        return {"available": False, "reason": "役割の異なる券を組み合わせた有効な構成を作れませんでした。"}

    # Ver191: 最高評価に近い構成から、黒字的中率と期待倍率を優先して選ぶ。
    best_score = max(m["score"] for _, m in snapshots)
    near = [(p, m) for p, m in snapshots if m["score"] >= best_score - 0.18]
    selected, metrics = max(
        near,
        key=lambda x: (x[1]["black"], x[1]["model_expected_multiple"],
                       x[1]["black_share_of_hits"], x[1]["cover"], -x[1]["low"], -x[1]["points"]),
    )
    selected = list(selected)

    # Ver201: 高確率本線を最終候補へ戻す。追加後の購入総額を含めて再評価し、
    # 回収率が悪ければ判定自体を下げるが、本線を黙って削ることはしない。
    protected_add_notes = []
    selected_ids = {(str(t.get("type")), str(t.get("combo"))) for t in selected}
    for ticket in protected_tickets:
        key = (str(ticket.get("type")), str(ticket.get("combo")))
        if key not in selected_ids:
            selected.append(ticket)
            selected_ids.add(key)
            protected_add_notes.append(
                f"{ticket['type']} {ticket['combo']}（モデル{float(ticket.get('probability',0.0)):.2f}%）を高確率本線として保護"
            )
    metrics = evaluate(selected)

    # Ver192: 単独ではガミになる2連系を、同じ展開に含まれる三連単へ分解して比較する。
    # 的中範囲を極端に捨てず、黒字的中率・期待倍率が改善する場合だけ差し替える。
    replacement_notes = []
    for _ in range(3):
        base_metrics = evaluate(selected)
        base_cost = float(base_metrics.get("cost", len(selected) * 100.0))
        best_swap = None
        for low_ticket in list(selected):
            if low_ticket.get("protected"):
                continue
            if low_ticket.get("type") not in ("2連単", "2連複"):
                continue
            # 現在の総点数に対し、この券だけの払戻では回収できないものを対象にする。
            if float(low_ticket.get("odds", 0.0)) * 100.0 >= base_cost:
                continue
            available_tri = [
                t for t in tri_candidates
                if t not in selected and t.get("matched")
                and t["matched"].issubset(low_ticket.get("matched", set()))
            ]
            available_tri.sort(
                key=lambda t: (float(t.get("probability", 0.0)), float(t.get("odds", 0.0))),
                reverse=True,
            )
            # 1〜4点への分解を比較。総点数は最大14点を維持する。
            max_add = min(5 if starter_count == 6 else 4, len(available_tri), (12 if starter_count == 6 else 15) - len(selected))
            for k in range(1, max_add + 1):
                tri_rows = available_tri[:k]
                trial_plan = [t for t in selected if t is not low_ticket] + tri_rows
                if len(trial_plan) > 14:
                    continue
                trial_metrics = evaluate(trial_plan)
                cover_loss = base_metrics["cover"] - trial_metrics["cover"]
                black_gain = trial_metrics["black"] - base_metrics["black"]
                expected_gain = (trial_metrics["model_expected_multiple"]
                                 - base_metrics["model_expected_multiple"])
                gami_drop = base_metrics["low"] - trial_metrics["low"]
                # 的中重視なので、カバー低下は原則2.5pt以内。大幅な黒字改善時のみ4ptまで許容。
                allowed_loss = 4.0 if black_gain >= 1.20 else 2.5
                if cover_loss > allowed_loss:
                    continue
                if black_gain <= 0.05:
                    continue
                if expected_gain < -0.03:
                    continue
                utility = (1.55 * black_gain + 0.55 * max(0.0, gami_drop)
                           + 8.0 * expected_gain - 0.28 * max(0.0, cover_loss)
                           - 0.06 * max(0, len(trial_plan) - len(selected)))
                key = (utility, black_gain, gami_drop, expected_gain, -cover_loss, -len(trial_plan))
                if best_swap is None or key > best_swap[0]:
                    best_swap = (key, low_ticket, tri_rows, trial_plan, trial_metrics, cover_loss)
        if best_swap is None or best_swap[0][0] <= 0.0:
            break
        _, low_ticket, tri_rows, selected, metrics, cover_loss = best_swap
        replacement_notes.append(
            f"{low_ticket['type']} {low_ticket['combo']}（{float(low_ticket.get('odds',0)):.1f}倍）を外し、"
            f"三連単 {', '.join(t['combo'] for t in tri_rows)} へ分解。"
            f"黒字的中率 {base_metrics['black']:.2f}%→{metrics['black']:.2f}%、"
            f"カバー率 {base_metrics['cover']:.2f}%→{metrics['cover']:.2f}%"
        )

    # Ver194: 同じ2車について、2連複・片側2連単・表裏2連単・重ね買いを全比較する。
    # 例: 2連複6-8 + 高配当側の2連単6-8 は、同じ2点の表裏2連単より
    # 両方向の払戻が高くなる場合がある。固定パターンではなく合成全体を再評価して採用する。
    pair_mix_notes = []
    for _ in range(4):
        base_metrics = evaluate(selected)
        exacta_by_combo = {
            str(t.get("combo")): t for t in candidates
            if t.get("type") == "2連単" and float(t.get("odds", 0.0)) > 0
        }
        best_pair_swap = None

        for quinella_ticket in list(selected):
            if quinella_ticket.get("type") != "2連複":
                continue
            if quinella_ticket.get("protected"):
                continue
            try:
                qa, qb = [int(x) for x in str(quinella_ticket.get("combo", "")).split("-")[:2]]
            except Exception:
                continue

            forward = exacta_by_combo.get(f"{qa}-{qb}")
            reverse = exacta_by_combo.get(f"{qb}-{qa}")
            pair_set = {qa, qb}

            # 現在選択中の同一ペア券を一度まとめて外し、候補構成を公平に比較する。
            related = []
            fixed_plan = []
            for ticket in selected:
                is_related = ticket is quinella_ticket
                if ticket.get("type") == "2連単":
                    try:
                        ea, eb = [int(x) for x in str(ticket.get("combo", "")).split("-")[:2]]
                        is_related = is_related or ({ea, eb} == pair_set)
                    except Exception:
                        pass
                if is_related:
                    related.append(ticket)
                else:
                    fixed_plan.append(ticket)

            option_rows = [("2連複のみ", [quinella_ticket])]
            if forward is not None:
                option_rows.extend([
                    (f"2連単{forward['combo']}のみ", [forward]),
                    (f"2連複＋2連単{forward['combo']}", [quinella_ticket, forward]),
                ])
            if reverse is not None:
                option_rows.extend([
                    (f"2連単{reverse['combo']}のみ", [reverse]),
                    (f"2連複＋2連単{reverse['combo']}", [quinella_ticket, reverse]),
                ])
            if forward is not None and reverse is not None:
                option_rows.extend([
                    ("2連単表裏", [forward, reverse]),
                    ("2連複＋2連単表裏", [quinella_ticket, forward, reverse]),
                ])

            # 同一ペア内に保護された2連単がある場合、その本線を外す比較は行わない。
            if any(t.get("protected") for t in related):
                continue

            current_ids = {(t.get("type"), t.get("combo")) for t in related}
            current_name = "現在構成"
            pair_base_metrics = evaluate(selected)
            current_score = (
                1.70 * pair_base_metrics["black"]
                + 0.18 * pair_base_metrics["cover"]
                - 0.88 * pair_base_metrics["low"]
                + 10.0 * pair_base_metrics["model_expected_multiple"]
                + 2.2 * pair_base_metrics["hit_average_multiple"]
                - 0.12 * len(selected)
            )

            seen_options = set()
            for option_name, option_tickets in option_rows:
                ids = tuple(sorted((t.get("type"), t.get("combo")) for t in option_tickets))
                if ids in seen_options:
                    continue
                seen_options.add(ids)
                trial_plan = list(fixed_plan)
                for ticket in option_tickets:
                    if ticket not in trial_plan:
                        trial_plan.append(ticket)
                if len(trial_plan) > 14:
                    continue

                trial_metrics = evaluate(trial_plan)
                cover_loss = pair_base_metrics["cover"] - trial_metrics["cover"]
                black_gain = trial_metrics["black"] - pair_base_metrics["black"]
                gami_drop = pair_base_metrics["low"] - trial_metrics["low"]
                expected_gain = (trial_metrics["model_expected_multiple"]
                                 - pair_base_metrics["model_expected_multiple"])
                avg_gain = (trial_metrics["hit_average_multiple"]
                            - pair_base_metrics["hit_average_multiple"])
                point_gain = len(trial_plan) - len(selected)

                # 的中重視なので、両方向を拾う構成同士ではカバーをほぼ維持する。
                # 片方向のみへ絞る場合は、黒字側の改善が大きい場合だけ最大2.0ptまで許容する。
                allowed_cover_loss = 2.0 if black_gain >= 0.80 else 0.40
                if cover_loss > allowed_cover_loss:
                    continue
                if expected_gain < -0.025:
                    continue
                if avg_gain < -0.06:
                    continue

                score = (
                    1.70 * trial_metrics["black"]
                    + 0.18 * trial_metrics["cover"]
                    - 0.88 * trial_metrics["low"]
                    + 10.0 * trial_metrics["model_expected_multiple"]
                    + 2.2 * trial_metrics["hit_average_multiple"]
                    - 0.12 * len(trial_plan)
                )
                gain = score - current_score
                # 同点付近なら、点数が少なくガミ率が低い構成を優先する。
                key = (
                    gain,
                    black_gain,
                    gami_drop,
                    expected_gain,
                    avg_gain,
                    -max(0.0, cover_loss),
                    -len(trial_plan),
                )
                if best_pair_swap is None or key > best_pair_swap[0]:
                    best_pair_swap = (
                        key, quinella_ticket, related, option_name, option_tickets,
                        trial_plan, trial_metrics, pair_base_metrics
                    )

        if best_pair_swap is None or best_pair_swap[0][0] <= 0.015:
            break

        (_, quinella_ticket, old_tickets, option_name, option_tickets,
         selected, metrics, before_metrics) = best_pair_swap

        old_desc = "・".join(
            f"{t['type']} {t['combo']}（{float(t.get('odds',0)):.1f}倍）" for t in old_tickets
        )
        new_desc = "・".join(
            f"{t['type']} {t['combo']}（{float(t.get('odds',0)):.1f}倍）" for t in option_tickets
        )
        pair_mix_notes.append(
            f"同一ペア構成を全比較し、{old_desc} から {new_desc} へ変更（{option_name}）。"
            f"黒字的中率 {before_metrics['black']:.2f}%→{metrics['black']:.2f}%、"
            f"ガミ率 {before_metrics['low']:.2f}%→{metrics['low']:.2f}%、"
            f"合成倍率 {before_metrics['hit_average_multiple']:.2f}倍→{metrics['hit_average_multiple']:.2f}倍"
        )

    replacement_notes.extend(pair_mix_notes)

    # Ver212: 長期回収率重視の最低基準。
    # 本線保護の有無にかかわらず、極端な低配当かつ単体期待値100%未満の券は
    # 「的中数を増やすだけの保険」として最終構成から除外する。
    low_odds_floor_notes = []
    for _ in range(8):
        base = evaluate(selected)
        forced = None
        for ticket in list(selected):
            if len(selected) <= 2:
                break
            odds = float(ticket.get("odds", 0.0) or 0.0)
            prob = float(ticket.get("probability", 0.0) or 0.0)
            standalone_ev = (prob / 100.0) * odds
            payout_ratio = (odds * 100.0 / float(base.get("cost", 1.0))) if base.get("cost") else 0.0

            # 2.0倍以下はEV100%未満なら無条件除外。
            # それ以上でも、総額の25%未満しか戻らずEV95%未満なら除外する。
            too_low = (odds <= 2.0 and standalone_ev < 1.0)
            deep_low_return = (payout_ratio < 0.25 and standalone_ev < 0.95)
            if not (too_low or deep_low_return):
                continue

            trial = [t for t in selected if t is not ticket]
            after = evaluate(trial)
            key = (
                base["model_return_rate"] - after["model_return_rate"],
                odds, standalone_ev
            )
            if forced is None or key < forced[0]:
                forced = (key, ticket, trial, after, standalone_ev, payout_ratio)

        if forced is None:
            break
        _, ticket, selected, after, standalone_ev, payout_ratio = forced
        ticket["protected"] = False
        low_odds_floor_notes.append(
            f"{ticket['type']} {ticket['combo']}（{float(ticket.get('odds',0)):.1f}倍）を長期回収率基準で除外。"
            f"単体期待値{standalone_ev*100:.1f}%・単独払戻は候補総額の{payout_ratio*100:.1f}%"
        )

    # Ver204: 券種をまたいだ重複と深いガミ券を、最終総額ベースで再評価する。
    # 高確率本線でも、追加的中範囲が小さく、単体期待値が低く、除外後に
    # 黒字確率・ガミ率・参考回収率が改善する場合は保護を解除して除外する。
    gami_prune_notes = []
    for _ in range(8):
        before = evaluate(selected)
        best_remove = None
        for ticket in list(selected):
            trial_plan = [t for t in selected if t is not ticket]
            if len(trial_plan) < 4:
                continue
            after = evaluate(trial_plan)

            other_matched = set()
            for other in trial_plan:
                other_matched.update(other.get("matched", set()))
            unique_indexes = set(ticket.get("matched", set())) - other_matched
            unique_prob = sum(outcomes[i][1] for i in unique_indexes)

            odds = float(ticket.get("odds", 0.0) or 0.0)
            prob = float(ticket.get("probability", 0.0) or 0.0)
            standalone_ev = (prob / 100.0) * odds
            payout_ratio = (odds * 100.0 / float(before.get("cost", 1.0))) if before.get("cost") else 0.0
            cover_loss = float(before["cover"] - after["cover"])
            black_delta = float(after["black"] - before["black"])
            gami_drop = float(before["low"] - after["low"])
            return_gain = float(after["model_return_rate"] - before["model_return_rate"])

            # Ver220: 「ほかの券が何か当たる」だけではカバー扱いにしない。
            # 券を外したことで、黒字→ガミ／黒字→外れ／的中→外れになる確率を
            # 各シミュレーション着順の払戻額から直接計算する。
            before_cost = len(selected) * 100.0
            after_cost = len(trial_plan) * 100.0
            black_to_gami = 0.0
            black_to_miss = 0.0
            hit_to_miss = 0.0
            payout_loss_expected = 0.0
            for i, (_, outcome_prob) in enumerate(outcomes):
                before_payout = sum(
                    float(t.get("odds", 0.0) or 0.0) * 100.0
                    for t in selected if i in t.get("matched", set())
                )
                after_payout = sum(
                    float(t.get("odds", 0.0) or 0.0) * 100.0
                    for t in trial_plan if i in t.get("matched", set())
                )
                outcome_prob = float(outcome_prob)
                payout_loss_expected += (outcome_prob / 100.0) * max(0.0, before_payout - after_payout)
                if before_payout > 0 and after_payout <= 0:
                    hit_to_miss += outcome_prob
                if before_payout >= before_cost:
                    if after_payout <= 0:
                        black_to_miss += outcome_prob
                    elif after_payout < after_cost:
                        black_to_gami += outcome_prob

            black_result_loss = black_to_gami + black_to_miss
            is_trifecta = str(ticket.get("type", "")).replace("三", "3") == "3連単"
            # 上位10点などの固定順位では守らない。実際に失う黒字結果・固有的中が
            # 十分大きい券だけを、チーム貢献として保護する。
            contribution_protected = is_trifecta and (
                black_result_loss >= 0.75
                or hit_to_miss >= 1.25
                or (prob >= 2.0 and payout_ratio >= 1.0)
            )

            deep_gami = payout_ratio < 0.70
            low_ev_overlap = unique_prob <= 6.0 and standalone_ev < 0.95
            pure_overlap = unique_prob <= 0.01 and standalone_ev < 0.75
            total_gami_cleanup = (
                payout_ratio < 1.0 and gami_drop >= 2.0 and return_gain >= 1.0
                and cover_loss <= 3.5 and black_delta >= -0.5
            )
            removable = (
                (deep_gami and low_ev_overlap and return_gain >= 1.0 and black_delta >= -2.5)
                or (pure_overlap and return_gain >= 1.0 and black_delta >= -2.5)
                or total_gami_cleanup
            )
            if not removable:
                continue

            # 黒字結果や固有的中を実際に失う3連単は、平均回収率だけを理由に切らない。
            if contribution_protected:
                continue

            # 保護券は通常残す。ただし深いガミかつ低期待値で、追加範囲も小さい場合だけ解除する。
            if ticket.get("protected") and not (deep_gami and low_ev_overlap):
                continue

            score = (
                return_gain + 1.50 * gami_drop + 0.80 * black_delta
                - 0.25 * max(0.0, cover_loss) + 20.0 * max(0.0, 1.0 - payout_ratio)
            )
            key = (score, return_gain, gami_drop, black_delta, -cover_loss, -unique_prob)
            if best_remove is None or key > best_remove[0]:
                best_remove = (
                    key, ticket, trial_plan, after, unique_prob, standalone_ev,
                    payout_ratio, cover_loss, black_delta, gami_drop, return_gain
                )

        if best_remove is None:
            break

        (_, ticket, selected, after, unique_prob, standalone_ev, payout_ratio,
         cover_loss, black_delta, gami_drop, return_gain) = best_remove
        ticket["protected"] = False
        gami_prune_notes.append(
            f"{ticket['type']} {ticket['combo']}（{float(ticket.get('odds',0)):.1f}倍）を除外。"
            f"固有的中{hit_to_miss:.2f}%・黒字→ガミ{black_to_gami:.2f}%・"
            f"黒字→外れ{black_to_miss:.2f}%・単体期待値{standalone_ev*100:.1f}%・"
            f"参考回収率{before['model_return_rate']:.1f}%→{after['model_return_rate']:.1f}%・"
            f"ガミ率{before['low']:.2f}%→{after['low']:.2f}%"
        )

    # Ver212: 堅いレースでは、通常合成の期待値比較だけで見送らない。
    # 最有力の1・2着ペアを固定し、3着候補ごとに表裏を揃えた3連単2〜4点を優先する。
    hard_race_info = {"enabled": False}
    try:
        win_share = {}
        pair_share = {}
        for outcome, probability in outcomes:
            a, b, _ = outcome
            win_share[a] = win_share.get(a, 0.0) + float(probability)
            pair = tuple(sorted((a, b)))
            pair_share[pair] = pair_share.get(pair, 0.0) + float(probability)
        top_wins = sorted(win_share.items(), key=lambda x: x[1], reverse=True)
        top_pair, top_pair_prob = max(pair_share.items(), key=lambda x: x[1]) if pair_share else ((0, 0), 0.0)
        top2_win_share = sum(v for _, v in top_wins[:2])
        top_tri_prob = max((float(t.get("probability", 0.0)) for t in tri_candidates), default=0.0)
        hard_score = 0
        hard_score += int(top2_win_share >= 72.0)
        hard_score += int(float(top_pair_prob) >= 38.0)
        hard_score += int(top_tri_prob >= 5.0)
        top_tri_rows = sorted(tri_candidates, key=lambda t: float(t.get("probability", 0.0)), reverse=True)[:10]
        low_odds_count = sum(1 for t in top_tri_rows[:5] if 0 < float(t.get("odds", 0.0)) <= 25.0)
        hard_score += int(low_odds_count >= 3)

        if hard_score >= 3 and len(top_tri_rows) >= 2:
            pair_set = set(int(x) for x in top_pair)
            pair_rows = []
            third_scores = {}
            for t in tri_candidates:
                try:
                    a, b, c = (int(x) for x in str(t.get("combo", "")).split("-"))
                except Exception:
                    continue
                if {a, b} == pair_set and c not in pair_set:
                    pair_rows.append(t)
                    third_scores[c] = third_scores.get(c, 0.0) + float(t.get("probability", 0.0))

            third_order = [c for c, _ in sorted(third_scores.items(), key=lambda x: x[1], reverse=True)]
            structured = []
            used = set()
            for c in third_order[:2]:
                rows = [t for t in pair_rows if str(t.get("combo", "")).endswith(f"-{c}")]
                rows.sort(key=lambda t: float(t.get("probability", 0.0)), reverse=True)
                # 最有力3着候補は中心ペアの表裏を両方残す。
                for t in rows[:2]:
                    key = (t.get("type"), t.get("combo"))
                    if key not in used:
                        structured.append(t); used.add(key)

            # ペア固定だけで不足する場合は、全体上位から補完する。
            for t in top_tri_rows:
                if len(structured) >= 4:
                    break
                key = (t.get("type"), t.get("combo"))
                if key not in used:
                    structured.append(t); used.add(key)

            compact_options = []
            min_k = 2 if len(structured) >= 2 else 1
            for k in range(min_k, min(4, len(structured)) + 1):
                compact = structured[:k]
                compact_metrics = evaluate(compact)
                # どの1点が当たっても購入総額以上になりやすい構成を優先。
                break_even_ok = all(float(t.get("odds", 0.0)) >= float(k) for t in compact)
                utility = (
                    2.10 * compact_metrics["black"]
                    + 10.0 * compact_metrics["model_expected_multiple"]
                    + 0.18 * compact_metrics["cover"]
                    - 0.75 * compact_metrics["low"]
                    - 0.35 * k
                    + (12.0 if break_even_ok else -12.0)
                )
                compact_options.append((utility, compact, compact_metrics, break_even_ok))

            _, compact_plan, compact_metrics, break_even_ok = max(
                compact_options,
                key=lambda x: (x[3], x[0], x[2]["black"], x[2]["model_expected_multiple"], -x[2]["points"]),
            )
            normal_metrics = evaluate(selected)

            # 強い堅い判定では、ガミ保険を多数残す通常構成より中心ペア固定を優先。
            # ただし全点が単独ガミになる構成、または参考回収率が著しく低い構成は採用しない。
            compact_is_better = bool(
                break_even_ok
                and compact_metrics["points"] <= 4
                and compact_metrics["model_return_rate"] >= 95.0
                and compact_metrics["low"] <= normal_metrics["low"]
            )
            hard_race_info = {
                "enabled": True,
                "applied": compact_is_better,
                "score": hard_score,
                "top_pair": top_pair,
                "top_pair_prob": float(top_pair_prob),
                "top2_win_share": float(top2_win_share),
                "normal_points": int(normal_metrics.get("points", len(selected))),
                "compact_points": int(compact_metrics.get("points", len(compact_plan))),
                "normal_return": float(normal_metrics.get("model_return_rate", 0.0)),
                "compact_return": float(compact_metrics.get("model_return_rate", 0.0)),
                "normal_black": float(normal_metrics.get("black", 0.0)),
                "compact_black": float(compact_metrics.get("black", 0.0)),
                "normal_low": float(normal_metrics.get("low", 0.0)),
                "compact_low": float(compact_metrics.get("low", 0.0)),
                "break_even_ok": bool(break_even_ok),
            }
            if compact_is_better:
                selected = list(compact_plan)
                metrics = compact_metrics
                replacement_notes.append(
                    f"堅いレース判定により通常{normal_metrics['points']}点から、中心ペア"
                    f"{top_pair[0]}・{top_pair[1]}固定の3連単{compact_metrics['points']}点へ絞り込み。"
                    f"上位2車勝率合計{top2_win_share:.1f}%・中心ペア確率{top_pair_prob:.1f}%・"
                    f"ガミ率{normal_metrics['low']:.2f}%→{compact_metrics['low']:.2f}%"
                )
    except Exception:
        hard_race_info = {"enabled": False}


    # Ver245: 同じ1着・同じ3車で2着と3着だけが入れ替わる三連単をセット比較する。
    # 片側だけ残すことで取りこぼしやすい展開を守るが、何でも折り返して点数を膨らませない。
    # 確率差が小さく、追加後も単独ガミにならず、全体指標を大きく悪化させない場合だけ最大2点追加。
    v245_pair_protection_notes = []
    try:
        tri_by_combo = {
            str(t.get("combo", "")): t for t in tri_candidates
            if str(t.get("type", "")).replace("三", "3") == "3連単"
        }
        selected_ids_now = {(str(t.get("type", "")), str(t.get("combo", ""))) for t in selected}
        pair_candidates = []
        base_pair_metrics = evaluate(selected)
        for ticket in list(selected):
            if str(ticket.get("type", "")).replace("三", "3") != "3連単":
                continue
            try:
                a, b, c = (int(x) for x in str(ticket.get("combo", "")).split("-"))
            except Exception:
                continue
            reverse_combo = f"{a}-{c}-{b}"
            reverse_ticket = tri_by_combo.get(reverse_combo)
            if reverse_ticket is None or (str(reverse_ticket.get("type", "")), reverse_combo) in selected_ids_now:
                continue
            p1 = float(ticket.get("probability", 0.0) or 0.0)
            p2 = float(reverse_ticket.get("probability", 0.0) or 0.0)
            odds2 = float(reverse_ticket.get("odds", 0.0) or 0.0)
            if p1 <= 0 or p2 < 0.80 or p2 < p1 * 0.62:
                continue
            new_points = len(selected) + 1
            standalone_ev = (p2 / 100.0) * odds2
            if odds2 < float(new_points) or standalone_ev < 0.72:
                continue
            trial_plan = list(selected) + [reverse_ticket]
            after = evaluate(trial_plan)
            return_drop = float(base_pair_metrics.get("model_return_rate", 0.0) - after.get("model_return_rate", 0.0))
            black_drop = float(base_pair_metrics.get("black", 0.0) - after.get("black", 0.0))
            cover_gain = float(after.get("cover", 0.0) - base_pair_metrics.get("cover", 0.0))
            low_gain = float(after.get("low", 0.0) - base_pair_metrics.get("low", 0.0))
            if return_drop > 5.0 or black_drop > 0.55 or low_gain > 1.25:
                continue
            score = 1.25 * cover_gain + 0.60 * p2 + 8.0 * standalone_ev - 0.55 * return_drop - 0.40 * low_gain
            pair_candidates.append((score, ticket, reverse_ticket, after, return_drop, black_drop, cover_gain, standalone_ev))

        for _, source_ticket, reverse_ticket, after, return_drop, black_drop, cover_gain, standalone_ev in sorted(pair_candidates, key=lambda x: x[0], reverse=True)[:2]:
            key = (str(reverse_ticket.get("type", "")), str(reverse_ticket.get("combo", "")))
            if key in {(str(t.get("type", "")), str(t.get("combo", ""))) for t in selected}:
                continue
            selected.append(reverse_ticket)
            base_pair_metrics = evaluate(selected)
            reverse_ticket["protected"] = True
            v245_pair_protection_notes.append(
                f"展開入替ペアを保護：{source_ticket.get('combo')} に対し {reverse_ticket.get('combo')} を追加。"
                f"確率{float(reverse_ticket.get('probability',0)):.2f}%・オッズ{float(reverse_ticket.get('odds',0)):.1f}倍・"
                f"単体期待値{standalone_ev*100:.1f}%・追加カバー+{cover_gain:.2f}pt"
            )
        if v245_pair_protection_notes:
            metrics = evaluate(selected)
            replacement_notes.extend(v245_pair_protection_notes)
    except Exception:
        v245_pair_protection_notes = []

    # Ver206: 低評価でも3着へ残る選手を、無条件で本線へ追加しない。
    # 既存の1・2着軸、3連複、別の3連単から自然に派生し、オッズ込みの期待値がある3連単だけを
    # 「余裕がある場合の追加候補」または「低効率券との入れ替え候補」として提示する。
    base_for_residual = evaluate(selected)
    selected_ids = {(t.get("type"), t.get("combo")) for t in selected}
    # Ver213: 回収率重視の最終構成では「その券だけが的中した場合に赤字」の券を残さない。
    # 券を1点ずつ外すたびに購入総額が下がるため、最終総額に対して再判定を繰り返す。
    # 長期回収率を優先し、本線保護よりも単独黒字条件を優先する。
    solo_gami_exclusion_notes = []
    for _ in range(20):
        current = evaluate(selected)
        current_cost = float(current.get("cost", 0.0) or 0.0)
        if current_cost <= 0 or len(selected) <= 1:
            break
        solo_gami = []
        for ticket in selected:
            odds = float(ticket.get("odds", 0.0) or 0.0)
            payout = odds * 100.0
            if payout + 1e-9 >= current_cost:
                continue
            prob = float(ticket.get("probability", 0.0) or 0.0)
            ev = (prob / 100.0) * odds
            trial = [t for t in selected if t is not ticket]
            after = evaluate(trial)
            return_gain = float(after.get("model_return_rate", 0.0) - current.get("model_return_rate", 0.0))
            black_delta = float(after.get("black", 0.0) - current.get("black", 0.0))
            cover_loss = float(current.get("cover", 0.0) - after.get("cover", 0.0))
            # 低EV、回収率改善、黒字率を傷めにくい券から除外。
            key = (
                1 if ev < 1.0 else 0,
                return_gain,
                black_delta,
                -cover_loss,
                current_cost - payout,
                -ev,
            )
            solo_gami.append((key, ticket, trial, after, payout, ev))
        if not solo_gami:
            break
        solo_gami.sort(key=lambda x: x[0], reverse=True)
        _, ticket, selected, after, payout, ev = solo_gami[0]
        ticket["protected"] = False
        solo_gami_exclusion_notes.append(
            f"{ticket['type']} {ticket['combo']}（{float(ticket.get('odds',0)):.1f}倍）を除外。"
            f"単独払戻{payout:.0f}円が除外前総額{current_cost:.0f}円を下回り、"
            f"単体期待値は{ev*100:.1f}%"
        )

    # Ver214: 単独ガミ除外後、2連単・2連複・3連複を削除して終わりにせず、
    # 同じ展開から派生する高期待値3連単へ1対1で置換した場合を再評価する。
    # 長期回収率・黒字的中率・ガミ率を同時比較し、改善する交換だけ自動採用する。
    cross_type_tri_swap_notes = []
    for _ in range(8):
        base_swap = evaluate(selected)
        best_cross_swap = None
        selected_now = {(t.get("type"), t.get("combo")) for t in selected}

        for old in list(selected):
            old_type = str(old.get("type", ""))
            if old_type not in {"2連単", "2連複", "3連複", "二連単", "二連複", "三連複"}:
                continue

            try:
                nums = tuple(int(x) for x in str(old.get("combo", "")).replace("=", "-").split("-") if str(x).strip())
            except Exception:
                continue

            related_combos = set()
            if old_type in {"2連単", "二連単"} and len(nums) == 2:
                a, b = nums
                for c in range(1, starter_count + 1):
                    if c not in (a, b):
                        related_combos.add(f"{a}-{b}-{c}")
            elif old_type in {"2連複", "二連複"} and len(nums) == 2:
                a, b = nums
                for c in range(1, starter_count + 1):
                    if c not in (a, b):
                        related_combos.add(f"{a}-{b}-{c}")
                        related_combos.add(f"{b}-{a}-{c}")
            elif old_type in {"3連複", "三連複"} and len(nums) == 3:
                import itertools as _itertools_v214
                for p in _itertools_v214.permutations(nums, 3):
                    related_combos.add("-".join(map(str, p)))

            if not related_combos:
                continue

            for cand in candidates:
                if str(cand.get("type", "")) not in {"3連単", "三連単"}:
                    continue
                if str(cand.get("combo", "")) not in related_combos:
                    continue
                if (cand.get("type"), cand.get("combo")) in selected_now or any(
                    str(t.get("type", "")) in {"3連単", "三連単"}
                    and str(t.get("combo", "")) == str(cand.get("combo", "")) for t in selected
                ):
                    continue

                odds = float(cand.get("odds", 0.0) or 0.0)
                probability = float(cand.get("probability", 0.0) or 0.0)
                standalone_ev = probability / 100.0 * odds
                if odds <= 0 or standalone_ev < 1.00:
                    continue

                swapped = [t for t in selected if t is not old] + [cand]
                sm = evaluate(swapped)
                return_gain = float(sm.get("model_return_rate", 0.0) - base_swap.get("model_return_rate", 0.0))
                black_delta = float(sm.get("black", 0.0) - base_swap.get("black", 0.0))
                gami_drop = float(base_swap.get("low", 0.0) - sm.get("low", 0.0))
                cover_loss = float(base_swap.get("cover", 0.0) - sm.get("cover", 0.0))
                avg_gain = float(sm.get("hit_average_multiple", 0.0) - base_swap.get("hit_average_multiple", 0.0))

                # 高配当化だけで的中範囲を壊さないよう、長期回収率の改善を必須にし、
                # 黒字率・ガミ率の悪化とカバー損失には上限を設ける。
                if return_gain < 0.50:
                    continue
                if black_delta < -0.35:
                    continue
                if gami_drop < -0.25:
                    continue
                if cover_loss > 3.0:
                    continue
                if avg_gain < -0.03:
                    continue

                old_odds = float(old.get("odds", 0.0) or 0.0)
                score = (
                    2.2 * return_gain + 1.4 * black_delta + 1.1 * gami_drop
                    - 0.35 * max(0.0, cover_loss) + 8.0 * max(0.0, avg_gain)
                    + 0.02 * max(0.0, odds - old_odds)
                )
                key = (score, return_gain, black_delta, gami_drop, -cover_loss, standalone_ev, odds)
                if best_cross_swap is None or key > best_cross_swap[0]:
                    best_cross_swap = (key, old, cand, swapped, sm, standalone_ev, cover_loss)

        if best_cross_swap is None or best_cross_swap[0][0] <= 0.0:
            break

        _, old, cand, selected, after_swap, standalone_ev, cover_loss = best_cross_swap
        cross_type_tri_swap_notes.append(
            f"{old['type']} {old['combo']}（{float(old.get('odds',0)):.1f}倍）を "
            f"3連単 {cand['combo']}（{float(cand.get('odds',0)):.1f}倍）へ置換。"
            f"参考回収率{base_swap['model_return_rate']:.1f}%→{after_swap['model_return_rate']:.1f}%・"
            f"黒字的中率{base_swap['black']:.2f}%→{after_swap['black']:.2f}%・"
            f"ガミ率{base_swap['low']:.2f}%→{after_swap['low']:.2f}%・"
            f"3連単単体期待値{standalone_ev*100:.1f}%"
        )

    replacement_notes.extend(cross_type_tri_swap_notes)

    # 置換後の最終構成を基準に追加候補を作り直す。
    base_for_residual = evaluate(selected)
    selected_ids = {(t.get("type"), t.get("combo")) for t in selected}
    residual_candidates = []
    selected_exacta = {tuple(int(x) for x in t["combo"].split("-")) for t in selected if t.get("type") in {"2連単", "二連単"}}
    selected_trio = {tuple(sorted(int(x) for x in t["combo"].split("-"))) for t in selected if t.get("type") in {"3連複", "三連複"}}
    selected_tris = [tuple(int(x) for x in t["combo"].split("-")) for t in selected if t.get("type") in {"3連単", "三連単"}]

    for cand in candidates:
        if cand.get("type") not in {"3連単", "三連単"} or (cand.get("type"), cand.get("combo")) in selected_ids:
            continue
        vals = tuple(int(x) for x in str(cand.get("combo", "")).split("-"))
        if len(vals) != 3:
            continue
        a, b, c = vals
        support = []
        if (a, b) in selected_exacta:
            support.append(f"2連単 {a}-{b}を3着まで延長")
        if tuple(sorted(vals)) in selected_trio:
            support.append(f"3連複 {'-'.join(map(str, sorted(vals)))}の着順候補")
        if any(x[0] == a and x[2] == c and x[1] != b for x in selected_tris):
            support.append(f"1着{a}・3着{c}の相手替わり")
        if any(x[0] == a and x[1] == b and x[2] != c for x in selected_tris):
            support.append(f"1・2着{a}-{b}の3着替わり")
        if len(support) < 2:
            continue

        probability = float(cand.get("probability", 0.0) or 0.0)
        odds = float(cand.get("odds", 0.0) or 0.0)
        standalone_ev = probability / 100.0 * odds
        add_metrics = evaluate(selected + [cand])
        cover_gain = add_metrics["cover"] - base_for_residual["cover"]
        black_gain = add_metrics["black"] - base_for_residual["black"]
        return_delta = add_metrics["model_return_rate"] - base_for_residual["model_return_rate"]
        low_delta = add_metrics["low"] - base_for_residual["low"]

        best_swap = None
        for old in selected:
            if old.get("type") == "三連単" and old.get("protected"):
                continue
            swapped = [t for t in selected if t is not old] + [cand]
            sm = evaluate(swapped)
            if sm["model_return_rate"] + 0.01 < base_for_residual["model_return_rate"]:
                continue
            if sm["black"] + 0.25 < base_for_residual["black"]:
                continue
            if sm["low"] > base_for_residual["low"] + 0.25:
                continue
            if sm["cover"] + 1.0 < base_for_residual["cover"]:
                continue
            key = (
                sm["black"] - base_for_residual["black"],
                sm["model_return_rate"] - base_for_residual["model_return_rate"],
                base_for_residual["low"] - sm["low"],
                sm["cover"] - base_for_residual["cover"],
            )
            if best_swap is None or key > best_swap[0]:
                best_swap = (key, old, sm)

        mode = None
        old_ticket = None
        comparison = add_metrics
        if best_swap is not None:
            mode = "入れ替え候補"
            old_ticket = best_swap[1]
            comparison = best_swap[2]
        elif (standalone_ev >= 1.00 and cover_gain >= 0.20
              and return_delta >= -3.0 and black_gain >= -1.0 and low_delta <= 1.5):
            mode = "余裕がある場合の追加候補"

        if mode:
            residual_candidates.append({
                "mode": mode, "ticket": cand, "replace": old_ticket,
                "support": support, "standalone_ev": standalone_ev * 100.0,
                "before": base_for_residual, "after": comparison,
                "score": (1 if mode == "入れ替え候補" else 0, standalone_ev, probability, odds),
            })

    residual_candidates.sort(key=lambda x: x["score"], reverse=True)
    residual_candidates = residual_candidates[:3]

    # Ver260: 画面・買い方の構成は従来のまま、券種をまたぐ完全重複だけを最終再評価する。
    # 例: 3連単で同じ3車の全着順をすでに覆っている場合の同一3連複、
    #     2連単表裏で同じ2車を覆っている場合の同一2連複。
    # 一律削除ではなく、100円均等買いの総額を含めたモデル回収率・黒字率・ガミ率を比較し、
    # 外しても的中範囲を失わず、成績が同等以上になる場合だけ除外する。
    v259_overlap_prune_notes = []
    for _ in range(10):
        before_overlap = evaluate(selected)
        best_overlap_remove = None
        for ticket in list(selected):
            if len(selected) <= 2:
                break
            trial_plan = [t for t in selected if t is not ticket]
            other_matched = set()
            for other in trial_plan:
                other_matched.update(other.get("matched", set()))
            own_matched = set(ticket.get("matched", set()))
            unique_indexes = own_matched - other_matched
            unique_prob = sum(float(outcomes[i][1]) for i in unique_indexes)
            if unique_prob > 0.01:
                continue

            after_overlap = evaluate(trial_plan)
            return_delta = float(after_overlap.get("model_return_rate", 0.0) - before_overlap.get("model_return_rate", 0.0))
            black_delta = float(after_overlap.get("black", 0.0) - before_overlap.get("black", 0.0))
            gami_delta = float(after_overlap.get("low", 0.0) - before_overlap.get("low", 0.0))
            cover_delta = float(after_overlap.get("cover", 0.0) - before_overlap.get("cover", 0.0))

            # 完全重複なのでカバーは原則不変。払戻の上乗せ価値が高ければ残す。
            # 回収率が明確に改善、またはほぼ同等で黒字率・ガミ率が悪化しない時だけ削る。
            acceptable = (
                return_delta >= 0.10
                or (
                    return_delta >= -0.05
                    and black_delta >= -0.05
                    and gami_delta <= 0.05
                    and cover_delta >= -0.01
                )
            )
            if not acceptable:
                continue

            prob = float(ticket.get("probability", 0.0) or 0.0)
            odds = float(ticket.get("odds", 0.0) or 0.0)
            standalone_ev = (prob / 100.0) * odds
            key = (
                return_delta,
                black_delta,
                -gami_delta,
                -standalone_ev,
                odds,
            )
            if best_overlap_remove is None or key > best_overlap_remove[0]:
                best_overlap_remove = (
                    key, ticket, trial_plan, after_overlap, standalone_ev,
                    return_delta, black_delta, gami_delta
                )

        if best_overlap_remove is None:
            break

        (_, ticket, selected, after_overlap, standalone_ev,
         return_delta, black_delta, gami_delta) = best_overlap_remove
        ticket["protected"] = False
        v259_overlap_prune_notes.append(
            f"{ticket['type']} {ticket['combo']}（{float(ticket.get('odds',0)):.1f}倍）は他券で的中範囲を完全包含。"
            f"100円均等の最終比較で除外（単体期待値{standalone_ev*100:.1f}%、"
            f"参考回収率{before_overlap['model_return_rate']:.1f}%→{after_overlap['model_return_rate']:.1f}%、"
            f"黒字率差{black_delta:+.2f}pt、ガミ率差{gami_delta:+.2f}pt）。"
        )

    # Ver260: 完全重複を削った後は、減った点数を機械的には埋めない。
    # 未採用候補を1点ずつ100円均等で再評価し、現在構成より参考回収率が改善する候補だけを追加する。
    # 追加しても改善しない場合は、点数が減ったまま終了する。
    v260_refill_notes = []
    v260_removed_points = len(v259_overlap_prune_notes)
    for _ in range(v260_removed_points):
        before_refill = evaluate(selected)
        selected_ids_now = {(str(t.get("type")), str(t.get("combo"))) for t in selected}
        current_matched = set()
        for t in selected:
            current_matched.update(t.get("matched", set()))

        best_refill = None
        for cand in candidates:
            cid = (str(cand.get("type")), str(cand.get("combo")))
            if cid in selected_ids_now:
                continue

            # 既存構成に完全包含される券は、削除直後に戻さない。
            own_matched = set(cand.get("matched", set()))
            unique_indexes = own_matched - current_matched
            unique_prob = sum(float(outcomes[i][1]) for i in unique_indexes)
            if unique_prob <= 0.01:
                continue

            counts = before_refill.get("counts", {}) or {}
            if counts.get(cand.get("type"), 0) >= int(cand.get("cap", 99)):
                continue

            after_refill = evaluate(selected + [cand])
            return_delta = float(after_refill.get("model_return_rate", 0.0) - before_refill.get("model_return_rate", 0.0))
            black_delta = float(after_refill.get("black", 0.0) - before_refill.get("black", 0.0))
            gami_delta = float(after_refill.get("low", 0.0) - before_refill.get("low", 0.0))
            cover_delta = float(after_refill.get("cover", 0.0) - before_refill.get("cover", 0.0))
            probability = float(cand.get("probability", 0.0) or 0.0)
            odds = float(cand.get("odds", 0.0) or 0.0)
            standalone_ev = (probability / 100.0) * odds

            acceptable = (
                return_delta >= 0.05
                and black_delta >= -0.05
                and gami_delta <= 0.10
                and cover_delta >= 0.0
                and standalone_ev >= 0.95
            )
            if not acceptable:
                continue

            key = (return_delta, black_delta, cover_delta, -gami_delta, standalone_ev, unique_prob, odds)
            if best_refill is None or key > best_refill[0]:
                best_refill = (key, cand, after_refill, standalone_ev, unique_prob,
                               return_delta, black_delta, gami_delta, cover_delta)

        if best_refill is None:
            break

        (_, cand, after_refill, standalone_ev, unique_prob,
         return_delta, black_delta, gami_delta, cover_delta) = best_refill
        selected.append(cand)
        v260_refill_notes.append(
            f"{cand['type']} {cand['combo']}（{float(cand.get('odds',0)):.1f}倍）を次点から追加。"
            f"参考回収率{before_refill['model_return_rate']:.1f}%→{after_refill['model_return_rate']:.1f}% "
            f"（{return_delta:+.2f}pt）、黒字率{black_delta:+.2f}pt、ガミ率{gami_delta:+.2f}pt、"
            f"追加カバー{cover_delta:+.2f}pt、単体期待値{standalone_ev*100:.1f}%・固有カバー{unique_prob:.2f}%。"
        )

    metrics = evaluate(selected)
    calibration = _v195_return_calibration(engine.DB_PATH)
    adjusted_expected_multiple = float(metrics.get("model_expected_multiple", 0.0)) * float(calibration.get("factor", 1.0))
    adjusted_return_rate = adjusted_expected_multiple * 100.0
    metrics["adjusted_expected_multiple"] = adjusted_expected_multiple
    metrics["adjusted_return_rate"] = adjusted_return_rate
    metrics["return_calibration"] = calibration

    # 的中率だけでなく、的中時に黒字となる割合と実績補正後回収率を主軸に評価する。
    gami_share = float(metrics.get("gami_share_of_hits", 0.0))
    black_share = float(metrics.get("black_share_of_hits", 0.0))
    avg_multiple = float(metrics.get("hit_average_multiple", 0.0))
    expected_multiple = float(metrics.get("model_expected_multiple", 0.0))
    adjusted_multiple = float(metrics.get("adjusted_expected_multiple", expected_multiple))

    if adjusted_multiple >= 1.15 and avg_multiple >= 1.35 and black_share >= 68.0 and gami_share <= 25.0:
        multiple_grade = "実績補正後も回収率基準を超える"
    elif adjusted_multiple >= 1.00 and black_share >= 55.0:
        multiple_grade = "実績補正後100%前後"
    else:
        multiple_grade = "実績補正後100%未満"

    samples = int(calibration.get("samples", 0))
    if adjusted_multiple < 0.95 or metrics["black"] < 15.0 or gami_share >= 42.0 or avg_multiple < 1.05:
        grade, icon = "非推奨", "⛔"
        reason = (
            f"実績補正後の参考回収率は{adjusted_return_rate:.1f}%です。"
            "的中範囲があっても、長期的に購入総額を回収しにくい構成です。"
        )
    elif (adjusted_multiple >= 1.15 and metrics["diversity"] >= 2 and metrics["black"] >= 30.0
          and black_share >= 68.0 and gami_share <= 20.0 and avg_multiple >= 1.35
          and samples >= 15):
        grade, icon = "回収率基準を満たす", "✅"
        reason = (
            f"実績補正後の参考回収率が{adjusted_return_rate:.1f}%で、黒字的中率は{metrics['black']:.1f}%です。"
            "保存実績も15レース以上あるため、回収率100%以上を狙う候補として扱います。"
        )
    elif adjusted_multiple >= 1.00:
        grade, icon = "回収率100%候補・検証中", "△"
        reason = (
            f"実績補正後の参考回収率は{adjusted_return_rate:.1f}%です。"
            f"ただし現ロジックの評価可能実績が{samples}レースのため、まだ検証中です。"
        )
    else:
        grade, icon = "的中優先なら候補", "⚠️"
        reason = (
            f"モデル上は的中範囲がありますが、実績補正後の参考回収率は{adjusted_return_rate:.1f}%です。"
            "回収率100%以上を優先する場合は見送り寄りです。"
        )

    grouped = {}
    for ticket in selected:
        grouped.setdefault(ticket["type"], []).append(ticket)
    role_lines = []
    for ticket_type in ("三連単", "三連複", "2連単", "2連複"):
        rows = grouped.get(ticket_type, [])
        if rows:
            role_lines.append(f"{v205_ticket_display_name(ticket_type)}{len(rows)}点：{rows[0]['role']}")
    return {
        "available": True, "grade": grade, "icon": icon, "reason": reason,
        "multiple_grade": multiple_grade,
        "tickets": selected, "grouped": grouped, "role_lines": role_lines,
        "learning": learning, "pool_summary": pool_summary,
        "tri_seed_points": int(tri_seed_points),
        "replacement_notes": replacement_notes,
        "gami_prune_notes": gami_prune_notes,
        "v259_overlap_prune_notes": v259_overlap_prune_notes,
        "v260_refill_notes": v260_refill_notes,
        "low_odds_floor_notes": low_odds_floor_notes,
        "solo_gami_exclusion_notes": solo_gami_exclusion_notes,
        "protected_add_notes": protected_add_notes,
        "protected_count": len([t for t in selected if t.get("protected")]),
        "residual_trifecta_candidates": residual_candidates,
        "hard_race_info": hard_race_info,
        "tri_seed_cover": float(tri_seed_metrics.get("cover", 0.0)),
        "tri_seed_black": float(tri_seed_metrics.get("black", 0.0)),
        **metrics,
    }



def v205_ticket_display_name(ticket_type: str) -> str:
    """内部キーは変えず、画面とコピー欄だけ券種名を数字表記へ統一する。"""
    return {
        "三連単": "3連単",
        "三連複": "3連複",
        "2連単": "2連単",
        "2連複": "2連複",
    }.get(str(ticket_type), str(ticket_type))



def v207_build_mixed_formation_sections(result: dict):
    """回収率重視の最終買い目を、画面最上段で使えるコピー形式へ整形する。"""
    sections = []
    notes = []
    for ticket_type in ("三連単", "三連複", "2連単", "2連複"):
        rows = result.get("grouped", {}).get(ticket_type, []) or []
        combos = [str(r.get("combo", "")).strip() for r in rows if str(r.get("combo", "")).strip()]
        if not combos:
            continue
        try:
            formations = engine.v67_compress_formations(combos, ticket_type)
            if ticket_type == "三連単":
                formations = v203_standard_trifecta_formations(formations, combos)
        except Exception as exc:
            formations = []
            notes.append(f"{v205_ticket_display_name(ticket_type)}: フォーメーション変換に失敗したため個別表記を使用（{exc}）")
        if not formations:
            formations = combos
            notes.append(f"{v205_ticket_display_name(ticket_type)}: 圧縮できない組み合わせは個別表記のまま出力")
        sections.append(
            f"{v205_ticket_display_name(ticket_type)} {len(combos)}点\n" + "\n".join(str(x) for x in formations)
        )
    return sections, notes


def v244_optional_single_wide_candidates(bets: dict, trials: int, odds_maps: dict) -> list[dict]:
    """単勝・ワイドは本線へ自動混入せず、余裕がある場合の候補として評価する。"""
    tri_counter = (bets or {}).get("三連単", {}) or {}
    if not tri_counter or int(trials or 0) <= 0:
        return []
    outcomes = []
    for combo, count in tri_counter.items():
        vals = tuple(int(v) for v in (tuple(combo) if isinstance(combo, (tuple, list)) else (combo,)))
        if len(vals) == 3:
            outcomes.append((vals, float(count) / max(int(trials), 1) * 100.0))
    if not outcomes:
        return []
    win_prob = {}
    wide_prob = {}
    for (a, b, c), prob in outcomes:
        win_prob[a] = win_prob.get(a, 0.0) + prob
        for x, y in ((a, b), (a, c), (b, c)):
            key = "-".join(map(str, sorted((x, y))))
            wide_prob[key] = wide_prob.get(key, 0.0) + prob
    rows = []
    for car_text, odds in (odds_maps.get("tansho", {}) or {}).items():
        try:
            car = int(car_text); odd = float(odds); prob = float(win_prob.get(car, 0.0))
        except Exception:
            continue
        ev = prob / 100.0 * odd * 100.0
        if odd > 0 and prob >= 8.0 and ev >= 100.0:
            rows.append({"type":"単勝","combo":str(car),"probability":prob,"odds":odd,"ev":ev,
                         "reason":"1着確率と単勝オッズの組み合わせが100%以上"})
    for combo, odds in (odds_maps.get("wide", {}) or {}).items():
        try:
            odd = float(odds); prob = float(wide_prob.get(str(combo), 0.0))
        except Exception:
            continue
        ev = prob / 100.0 * odd * 100.0
        # ワイドはHTMLの下限オッズを使うため、判定はやや厳しめ。
        if odd > 0 and prob >= 18.0 and ev >= 105.0:
            rows.append({"type":"ワイド","combo":str(combo),"probability":prob,"odds":odd,"ev":ev,
                         "reason":"3着内ペア確率とワイド下限オッズで期待値105%以上"})
    rows.sort(key=lambda r:(float(r.get("ev",0)), float(r.get("probability",0))), reverse=True)
    # 同券種が並びすぎないよう最大3点。
    return rows[:3]

def show_v184_eight_car_mixed_plan(
    bets: dict, trials: int, meta: dict, odds_maps: dict, race_key: str = "",
    app_version: str | None = None, save_enabled: bool = True,
) -> None:
    result = v184_eight_car_mixed_plan(bets, trials, meta, odds_maps)
    starter_count = engine.v102_starter_count_for_meta(meta, engine.DB_PATH) or 0
    st.markdown(f"#### 🧩 {int(starter_count)}車向け・黒字的中重視の回収率合成")
    if not result.get("available"):
        st.caption(result.get("reason", "4券種オッズを読み込むと表示します。"))
        return
    saved_hash = ""
    try:
        # 復元表示では回収率プランを新規保存しない。
        # 保存済み予測の版を現行版として誤登録する事故を防ぐ。
        if save_enabled and race_key and str(race_key) != "current":
            saved_hash = _v187_save_mixed_plan(
                engine.DB_PATH, str(race_key), result, app_version=app_version
            )
    except Exception as exc:
        st.warning(f"合成プランをDBへ保存できませんでした: {exc}")

    # Ver207: ユーザーが主に見る最終買い目と一括コピーを、説明・監査・指標より先に表示する。
    hard_info = result.get("hard_race_info") or {}
    if hard_info.get("enabled"):
        pair = hard_info.get("top_pair") or (0, 0)
        if hard_info.get("applied"):
            st.success(
                f"🔒 堅いレース判定：3連単{int(hard_info.get('compact_points',0))}点へ自動絞り込み｜"
                f"中心ペア {pair[0]}・{pair[1]}（1・2着確率{float(hard_info.get('top_pair_prob',0)):.1f}%）"
            )
        else:
            st.info(
                f"🔒 堅いレース候補を検出しましたが、少点数化で黒字確率または参考回収率が悪化するため通常構成を維持しました。"
            )
    formation_sections, formation_notes = v207_build_mixed_formation_sections(result)
    st.subheader(f"{result['icon']} 回収率重視：{result['points']}点・{result['grade']}")
    if formation_sections:
        formation_copy_text = "\n\n".join(formation_sections)
        v73_copy_box(
            "回収率重視の推奨買い目・一括コピー",
            formation_copy_text,
            f"v207_priority_formation_{race_key}_{saved_hash}_{result.get('points', 0)}",
            height=max(190, min(520, 95 + 27 * formation_copy_text.count("\n"))),
        )
        st.caption(
            f"候補総額 {int(result.get('cost', 0)):,}円｜黒字的中率 {float(result.get('black', 0)):.2f}%｜"
            f"ガミ率 {float(result.get('low', 0)):.2f}%｜実績補正後回収率 {float(result.get('adjusted_return_rate', 0)):.1f}%"
        )
        for note in formation_notes:
            st.caption(note)
    if result.get("protected_add_notes"):
        st.info(f"高確率本線を{int(result.get('protected_count', 0))}点保護しています。")
        for note in result.get("protected_add_notes", []):
            st.caption(f"・{note}")
    if result.get("replacement_notes"):
        st.success("ガミ保険を3連単へ置換・同一ペアを再編しました。")
        for note in result.get("replacement_notes", []):
            st.caption(f"・{note}")
    if result.get("low_odds_floor_notes"):
        st.info("長期回収率基準により、極端な低配当・低期待値の保険券を除外しました。")
        for note in result.get("low_odds_floor_notes", []):
            st.caption(f"・{note}")
    if result.get("solo_gami_exclusion_notes"):
        st.info("長期回収率優先のため、単独的中で購入総額を回収できない買い目を最終構成から除外しました。")
        for note in result.get("solo_gami_exclusion_notes", []):
            st.caption(f"・{note}")

    if result.get("v259_overlap_prune_notes"):
        st.caption("券種間の完全重複を100円均等・回収率基準で再評価しました。")
        for note in result.get("v259_overlap_prune_notes", []):
            st.caption(f"・{note}")

    if result.get("v260_refill_notes"):
        st.caption("重複削除後は、100円均等のまま回収率が改善する次点候補だけを再評価して補充しました。")
        for note in result.get("v260_refill_notes", []):
            st.caption(f"・{note}")

    if result.get("gami_prune_notes"):
        st.warning("深いガミ券・重複効率の低い券を最終総額ベースで除外しました。")
        for note in result.get("gami_prune_notes", []):
            st.caption(f"・{note}")
    residual_rows = result.get("residual_trifecta_candidates", []) or []
    if residual_rows:
        st.markdown("##### 🪶 3着残り・期待値追加候補")
        st.caption("本線には自動追加しません。既存の軸・3連複・相手関係から自然に派生し、オッズ込みで条件を満たした3連単だけを表示します。")
        residual_copy = []
        for item in residual_rows:
            ticket = item.get("ticket", {})
            old = item.get("replace")
            before = item.get("before", {})
            after = item.get("after", {})
            mode = item.get("mode", "追加候補")
            combo = str(ticket.get("combo", ""))
            residual_copy.append(combo)
            if old:
                title = f"{mode}：{v205_ticket_display_name(old.get('type'))} {old.get('combo')} → 3連単 {combo}"
            else:
                title = f"{mode}：3連単 {combo}"
            st.markdown(f"**{title}**")
            st.caption(
                f"モデル{float(ticket.get('probability',0)):.3f}%・{float(ticket.get('odds',0)):.1f}倍・単体期待値{float(item.get('standalone_ev',0)):.1f}% ／ "
                f"黒字的中率 {float(before.get('black',0)):.2f}%→{float(after.get('black',0)):.2f}%・"
                f"参考回収率 {float(before.get('model_return_rate',0)):.1f}%→{float(after.get('model_return_rate',0)):.1f}%"
            )
            st.caption("根拠：" + "／".join(item.get("support", [])))
        if residual_copy:
            v73_copy_box(
                "余裕がある場合の3連単追加候補",
                "3連単 追加候補\n" + "\n".join(residual_copy),
                f"v206_residual_trifecta_{race_key}_{saved_hash}",
                height=max(125, 82 + 27 * len(residual_copy)),
            )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("合成的中率", f"{result['cover']:.2f}%")
    c2.metric("黒字的中率", f"{result['black']:.2f}%")
    c3.metric("トリガミ率", f"{result['low']:.2f}%")
    c4.metric("候補総額", f"{int(result['cost']):,}円")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("的中時平均合成倍率", f"{result['hit_average_multiple']:.2f}倍")
    m2.metric("最低合成倍率", f"{result['hit_min_multiple']:.2f}倍")
    m3.metric("最高合成倍率", f"{result['hit_max_multiple']:.2f}倍")
    m4.metric("モデル期待倍率", f"{result['model_expected_multiple']:.2f}倍")
    cal = result.get("return_calibration", {})
    r1, r2, r3 = st.columns(3)
    r1.metric("実績補正後回収率", f"{result.get('adjusted_return_rate',0):.1f}%")
    r2.metric("回収率補正係数", f"×{float(cal.get('factor',1.0)):.3f}")
    r3.metric("補正実績数", f"{int(cal.get('samples',0))}R")

    pool = result.get("pool_summary", {})
    if pool:
        st.markdown("##### 🎯 券種別の候補母集団")
        cols = st.columns(4)
        for idx, ticket_type in enumerate(("三連単", "三連複", "2連単", "2連複")):
            info = pool.get(ticket_type, {})
            target = info.get("target")
            target_text = f"目標{target:.0f}%" if isinstance(target, (int, float)) else "上位候補"
            cols[idx].metric(
                v205_ticket_display_name(ticket_type),
                f"{int(info.get('points', 0))}点",
                f"累積{float(info.get('cover', 0.0)):.1f}%・{target_text}",
            )
        st.caption("3連単以外は車立て別の累積確率候補から、合成効果・ガミ・倍率を比較して採用します。6車は3連単だけが最良なら、ほかの券種を無理に混ぜません。")
    st.caption(
        f"3連単の初期本線は上位2〜6点を比較し、今回は{int(result.get('tri_seed_points', 2))}点を採用。"
        f"本線段階のカバー{float(result.get('tri_seed_cover', 0.0)):.2f}%・黒字側{float(result.get('tri_seed_black', 0.0)):.2f}%を基準に、"
        "その後ほかの券種と3連単追加候補を同じ土俵で比較しています。"
    )
    q1, q2, q3 = st.columns(3)
    q1.metric("的中時の黒字割合", f"{result['black_share_of_hits']:.1f}%")
    q2.metric("的中時のガミ割合", f"{result['gami_share_of_hits']:.1f}%")
    q3.metric("倍率判定", result.get("multiple_grade", "参考"))
    if result["grade"] == "非推奨":
        st.error(result["reason"])
    elif "合成推奨" in result["grade"]:
        st.success(result["reason"])
    elif "ガミ注意" in result["grade"]:
        st.warning(result["reason"])
    else:
        st.info(result["reason"])
    st.caption(" / ".join(result.get("role_lines", [])))
    st.markdown("##### 買い目ごとの詳細")
    order = ("三連単", "三連複", "2連単", "2連複")
    for ticket_type in order:
        rows = result["grouped"].get(ticket_type, [])
        if not rows:
            continue
        st.markdown(f"**{v205_ticket_display_name(ticket_type)}：{len(rows)}点｜{rows[0]['role']}**")
        ticket_lines = []
        combos = []
        for r in rows:
            combo = str(r.get("combo", "")).strip()
            if combo:
                combos.append(combo)
            solo_gami = float(r["odds"]) * 100.0 < float(result["cost"])
            note = " / 単独的中ではガミ注意" if solo_gami else ""
            protect_note = " / 本線保護" if r.get("protected") else ""
            ticket_lines.append(
                f"{combo}  ({r['odds']:.1f}倍 / モデル{r['probability']:.2f}%{protect_note}{note})"
            )
        st.code("\n".join(ticket_lines), language=None)

    st.caption(
        f"モデル上の全外れ率 {result['miss']:.2f}%・参考モデル回収率 {result['model_return_rate']:.1f}%・実績補正後 {result.get('adjusted_return_rate',0):.1f}% 。"
        "車立て別に点数と券種配分を変え、黒字的中率が明確に改善する候補だけ追加します。6車は3連単中心、7車は中間、8車は補完券種を厚めに評価します。"
    )
    st.caption(
        "判定は黒字的中率を最優先し、的中時の黒字割合・ガミ割合・平均合成倍率・モデル期待倍率を使用します。"
        "券種別の高確率本線は先に保護しますが、最終総額に対して深いガミで、追加カバーと単体期待値が低い場合は保護を解除して除外します。"
        "低配当保険は、ほかの券種との同時的中を含めて黒字確率を増やす場合だけ採用します。"
        "単独でガミになる2連系は3連単1〜4点への分解を比較し、2連複は片側2連単・表裏2連単・2連複との重ね買いを全比較し、合成全体が改善する構成だけ採用します。"
    )
    st.caption(
        "各買い目の『単独的中ではガミ注意』は、その券だけが当たった場合の払戻が候補総額を下回る意味です。"
        "別券種も同時的中すれば、合算で黒字になる場合があります。"
    )
    st.caption(
        "合成倍率は、各候補を100円ずつ購入した候補総額に対する払戻倍率です。"
        "結果によって当たる券種と同時的中数が変わるため、固定値ではなく最低・平均・最高で表示しています。"
    )
    learning = result.get("learning", {})
    samples = int(learning.get("samples", 0) or 0)
    if samples > 0:
        st.caption(
            f"結果学習：{samples}レース｜実績的中率 {learning.get('hit_rate',0):.1f}%｜"
            f"実績黒字率 {learning.get('black_rate',0):.1f}%｜実績回収率 {learning.get('return_rate',0):.1f}%"
        )
    else:
        st.caption("結果学習：保存開始直後のため実績なし。結果登録後、自動照合して次回の券種配分へ少しずつ反映します。")
    if saved_hash:
        st.caption(f"💾 この合成プランはDB保存済み（ID: {saved_hash}）。結果登録後に自動評価されます。")
    st.caption("同じ結果で複数券種が同時的中する場合は払戻を合算しています。確率とオッズによる参考構成です。")

def install_uploaded_db(uploaded) -> tuple[bool, str]:
    data = uploaded.getvalue()
    if not data.startswith(b"SQLite format 3\x00"):
        return False, "SQLite形式ではありません。ZIPではなく autorace_players.sqlite3 本体を選んでください。"

    digest = hashlib.sha256(data).hexdigest()
    if st.session_state.get("loaded_db_hash") == digest:
        return True, "このDBは読み込み済みです。"

    target = Path(engine.DB_PATH)
    temp = target.with_suffix(target.suffix + ".uploading")
    backup = target.with_suffix(target.suffix + ".backup")
    try:
        temp.write_bytes(data)
        with sqlite3.connect(temp) as con:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            if str(check).lower() != "ok":
                raise ValueError(f"DB整合性チェック: {check}")
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            if not tables:
                raise ValueError("テーブルがありません")

        if target.exists():
            backup.write_bytes(target.read_bytes())
        target.write_bytes(temp.read_bytes())
        engine.mount_and_init_db()
        st.session_state["loaded_db_hash"] = digest
        return True, f"DBを読み込みました（{len(data) / 1024 / 1024:.1f} MB）"
    except Exception as exc:
        if backup.exists():
            target.write_bytes(backup.read_bytes())
        return False, f"DBを読み込めませんでした: {type(exc).__name__}: {exc}"
    finally:
        temp.unlink(missing_ok=True)


def secret_value(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
    except Exception:
        return default
    return str(value).strip() if value is not None else default



# Ver247管理修正2: 同一レースを新版で再予測しても、旧版を回収率比較から消さない。
def _v215_return_dashboard_rows(db_path: str) -> pd.DataFrame:
    """各レース・各バージョンの最新プランを採用し、結果済み実績を集計用DataFrameで返す。"""
    _v187_ensure_mixed_learning_tables(db_path)
    _v187_sync_mixed_feedback(db_path)
    query = """
        WITH latest AS (
            SELECT r.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.race_key, COALESCE(NULLIF(r.app_version,''), 'Unknown')
                       ORDER BY datetime(r.created_at) DESC, r.rowid DESC
                   ) AS rn
            FROM v187_mixed_plan_runs r
        )
        SELECT l.race_key,
               COALESCE(NULLIF(l.race_date,''), rr.race_date) AS race_date,
               COALESCE(NULLIF(l.venue,''), rr.venue) AS venue,
               COALESCE(NULLIF(l.race_no,''), rr.race_no) AS race_no,
               l.app_version, l.logic_version, l.points, l.cost_yen,
               l.grade, l.model_return_rate, l.created_at,
               f.hit, f.black_hit, f.gami_hit, f.payout_yen,
               f.return_rate, f.evaluated_at
        FROM latest l
        LEFT JOIN result_races rr ON rr.race_key=l.race_key
        LEFT JOIN v187_mixed_plan_feedback f
          ON f.race_key=l.race_key AND f.plan_hash=l.plan_hash
        WHERE l.rn=1 AND f.return_rate IS NOT NULL
        ORDER BY COALESCE(NULLIF(l.race_date,''), rr.race_date),
                 COALESCE(NULLIF(l.venue,''), rr.venue),
                 CAST(COALESCE(NULLIF(l.race_no,''), rr.race_no) AS INTEGER)
    """
    try:
        with sqlite3.connect(db_path) as con:
            df = pd.read_sql_query(query, con)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df["month"] = df["race_date"].dt.strftime("%Y-%m")
    df["収支"] = pd.to_numeric(df["payout_yen"], errors="coerce").fillna(0) - pd.to_numeric(df["cost_yen"], errors="coerce").fillna(0)
    df["推奨区分"] = df["grade"].fillna("").astype(str).apply(
        lambda x: "非推奨" if ("非推奨" in x or "⛔" in x) else "推奨"
    )
    return df


def _v216_summary_values(df: pd.DataFrame) -> dict:
    """全体・推奨のみ・非推奨のみの実績を同じ基準で返す。"""
    def one(part: pd.DataFrame) -> dict:
        if part.empty:
            return {"races": 0, "cost": 0, "payout": 0, "profit": 0, "return": None,
                    "hit_rate": None, "black_rate": None, "gami_rate": None}
        cost = float(pd.to_numeric(part["cost_yen"], errors="coerce").fillna(0).sum())
        payout = float(pd.to_numeric(part["payout_yen"], errors="coerce").fillna(0).sum())
        return {
            "races": int(len(part)), "cost": int(cost), "payout": int(payout),
            "profit": int(payout - cost), "return": (payout / cost * 100.0) if cost > 0 else None,
            "hit_rate": float(pd.to_numeric(part["hit"], errors="coerce").fillna(0).mean() * 100.0),
            "black_rate": float(pd.to_numeric(part["black_hit"], errors="coerce").fillna(0).mean() * 100.0),
            "gami_rate": float(pd.to_numeric(part["gami_hit"], errors="coerce").fillna(0).mean() * 100.0),
        }
    return {
        "全レース": one(df),
        "推奨のみ": one(df[df["推奨区分"] == "推奨"]),
        "非推奨のみ": one(df[df["推奨区分"] == "非推奨"]),
    }


def _v215_aggregate_return(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    work = df.copy()
    grouped = work.groupby(group_cols, dropna=False).agg(
        レース数=("race_key", "count"),
        的中数=("hit", "sum"),
        黒字数=("black_hit", "sum"),
        ガミ数=("gami_hit", "sum"),
        購入額=("cost_yen", "sum"),
        払戻額=("payout_yen", "sum"),
        収支=("収支", "sum"),
    ).reset_index()
    grouped["的中率"] = grouped["的中数"] / grouped["レース数"].clip(lower=1) * 100.0
    grouped["黒字率"] = grouped["黒字数"] / grouped["レース数"].clip(lower=1) * 100.0
    grouped["回収率"] = grouped["払戻額"] / grouped["購入額"].replace(0, pd.NA) * 100.0

    # 同じ集計単位について、非推奨を除いた成績を横並びにする。
    recommended = work[work["推奨区分"] == "推奨"]
    if not recommended.empty:
        rec = recommended.groupby(group_cols, dropna=False).agg(
            推奨レース数=("race_key", "count"),
            推奨的中数=("hit", "sum"),
            推奨黒字数=("black_hit", "sum"),
            推奨ガミ数=("gami_hit", "sum"),
            推奨購入額=("cost_yen", "sum"),
            推奨払戻額=("payout_yen", "sum"),
            推奨収支=("収支", "sum"),
        ).reset_index()
        rec["非推奨除外回収率"] = rec["推奨払戻額"] / rec["推奨購入額"].replace(0, pd.NA) * 100.0
        rec["非推奨除外的中率"] = rec["推奨的中数"] / rec["推奨レース数"].clip(lower=1) * 100.0
        rec["非推奨除外黒字率"] = rec["推奨黒字数"] / rec["推奨レース数"].clip(lower=1) * 100.0
        keep = group_cols + ["推奨レース数", "非推奨除外回収率", "非推奨除外的中率", "非推奨除外黒字率", "推奨収支"]
        grouped = grouped.merge(rec[keep], on=group_cols, how="left")
    else:
        grouped["推奨レース数"] = 0
        grouped["非推奨除外回収率"] = pd.NA
        grouped["非推奨除外的中率"] = pd.NA
        grouped["非推奨除外黒字率"] = pd.NA
        grouped["推奨収支"] = 0
    return grouped


def _v215_render_return_dashboard(db_path: str) -> None:
    st.markdown("## 📊 回収率重視プラン実績")
    st.caption("各レース・各バージョンで最後に保存されたプランを、予測時点の買い目のまま別々に集計します。新版を再シミュレーションしても旧版の実績は残ります。")
    df = _v215_return_dashboard_rows(db_path)
    if df.empty:
        st.info("結果まで照合済みの回収率重視プランがまだありません。今後の予測では買い目・オッズ・確率・バージョンを自動保存します。")
        return

    venues = sorted([str(v) for v in df["venue"].dropna().unique() if str(v)])
    c1, c2 = st.columns(2)
    selected_venue = c1.selectbox("開催場で絞る", ["全開催場"] + venues, key="v215_return_venue")
    versions = sorted([str(v) for v in df["app_version"].dropna().unique() if str(v)])
    selected_version = c2.selectbox("バージョンで絞る", ["全バージョン"] + versions, key="v215_return_version")
    filtered = df.copy()
    if selected_venue != "全開催場":
        filtered = filtered[filtered["venue"].astype(str) == selected_venue]
    if selected_version != "全バージョン":
        filtered = filtered[filtered["app_version"].astype(str) == selected_version]
    if filtered.empty:
        st.warning("選択条件に該当する実績がありません。")
        return

    summary = _v216_summary_values(filtered)
    all_s = summary["全レース"]
    rec_s = summary["推奨のみ"]
    no_s = summary["非推奨のみ"]

    st.markdown("### 全体と非推奨除外の比較")
    a,b,c,d,e = st.columns(5)
    a.metric("全レース", f"{all_s['races']}R")
    b.metric("全体回収率", f"{all_s['return']:.1f}%" if all_s['return'] is not None else "－")
    c.metric("非推奨除外回収率", f"{rec_s['return']:.1f}%" if rec_s['return'] is not None else "－",
             delta=(f"{rec_s['return']-all_s['return']:+.1f}pt" if rec_s['return'] is not None and all_s['return'] is not None else None))
    d.metric("推奨のみ収支", f"{rec_s['profit']:+,}円")
    e.metric("推奨率", f"{(rec_s['races']/all_s['races']*100.0):.1f}%" if all_s['races'] else "－")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**全レース**")
        st.caption(f"的中率 {all_s['hit_rate']:.1f}% / 黒字率 {all_s['black_rate']:.1f}% / ガミ率 {all_s['gami_rate']:.1f}% / 収支 {all_s['profit']:+,}円")
    with c2:
        st.markdown("**推奨のみ（非推奨除外）**")
        if rec_s['races']:
            st.caption(f"{rec_s['races']}R・的中率 {rec_s['hit_rate']:.1f}% / 黒字率 {rec_s['black_rate']:.1f}% / ガミ率 {rec_s['gami_rate']:.1f}% / 収支 {rec_s['profit']:+,}円")
        else:
            st.caption("該当なし")
    with c3:
        st.markdown("**非推奨のみ**")
        if no_s['races']:
            st.caption(f"{no_s['races']}R・回収率 {no_s['return']:.1f}% / 的中率 {no_s['hit_rate']:.1f}% / 収支 {no_s['profit']:+,}円")
        else:
            st.caption("該当なし")

    st.markdown("### 日別")
    daily = _v215_aggregate_return(filtered, ["race_date"])
    if not daily.empty:
        daily["race_date"] = pd.to_datetime(daily["race_date"]).dt.strftime("%Y-%m-%d")
        st.dataframe(daily.sort_values("race_date", ascending=False), use_container_width=True, hide_index=True)

    st.markdown("### 開催場別")
    st.dataframe(_v215_aggregate_return(filtered, ["venue"]).sort_values("回収率", ascending=False), use_container_width=True, hide_index=True)

    st.markdown("### 日付 × 開催場")
    day_venue = _v215_aggregate_return(filtered, ["race_date", "venue"])
    if not day_venue.empty:
        day_venue["race_date"] = pd.to_datetime(day_venue["race_date"]).dt.strftime("%Y-%m-%d")
        st.dataframe(day_venue.sort_values(["race_date", "venue"], ascending=[False, True]), use_container_width=True, hide_index=True)

    st.markdown("### 月別")
    st.dataframe(_v215_aggregate_return(filtered, ["month"]).sort_values("month", ascending=False), use_container_width=True, hide_index=True)

    _v226_render_prediction_condition_analysis(db_path)

    with st.expander("レース別の明細", expanded=False):
        detail = filtered.copy()
        detail["日付"] = detail["race_date"].dt.strftime("%Y-%m-%d")
        detail["判定"] = detail.apply(lambda r: "◎黒字" if r.get("black_hit") else ("△ガミ" if r.get("gami_hit") else "×外れ"), axis=1)
        cols = ["日付","venue","race_no","推奨区分","grade","判定","points","cost_yen","payout_yen","return_rate","収支","app_version"]
        detail = detail[cols].rename(columns={
            "venue":"開催場","race_no":"R","points":"点数","cost_yen":"購入額",
            "payout_yen":"払戻額","return_rate":"回収率","app_version":"バージョン","grade":"元判定",
        })
        st.dataframe(detail.sort_values(["日付","開催場","R"], ascending=[False,True,True]), use_container_width=True, hide_index=True)


def _v226_prediction_condition_rows(db_path: str) -> pd.DataFrame:
    """開催場・ハンデ構成別に予測順位の実績を集計するためのレース単位データ。"""
    query = """
        WITH finish_info AS (
            SELECT race_key,
                   MAX(CASE WHEN finish=1 THEN car_no END) AS winner,
                   COUNT(CASE WHEN finish IS NOT NULL THEN 1 END) AS starter_count,
                   COUNT(DISTINCT CASE WHEN finish IS NOT NULL THEN CAST(handicap AS TEXT) END) AS handicap_kinds,
                   MIN(CASE WHEN finish IS NOT NULL THEN CAST(handicap AS REAL) END) AS min_handicap,
                   MAX(CASE WHEN finish IS NOT NULL THEN CAST(handicap AS REAL) END) AS max_handicap
            FROM result_entries
            GROUP BY race_key
        ),
        pred AS (
            SELECT race_key,
                   MAX(CASE WHEN predicted_rank=1 THEN car_no END) AS predicted_winner
            FROM prediction_snapshots
            GROUP BY race_key
        ),
        feedback AS (
            SELECT race_key,
                   MAX(CASE WHEN bet_type='3連単' THEN predicted_rank END) AS trifecta_rank,
                   MAX(CASE WHEN bet_type='3連複' THEN predicted_rank END) AS trio_rank,
                   MAX(CASE WHEN bet_type='2連単' THEN predicted_rank END) AS exacta_rank,
                   MAX(CASE WHEN bet_type='2連複' THEN predicted_rank END) AS quinella_rank
            FROM v67_ticket_feedback
            GROUP BY race_key
        )
        SELECT rr.race_key, rr.race_date, rr.venue, rr.race_no, rr.surface, rr.track_temp,
               f.winner, f.starter_count, f.handicap_kinds, f.min_handicap, f.max_handicap,
               p.predicted_winner, fb.trifecta_rank, fb.trio_rank, fb.exacta_rank, fb.quinella_rank
        FROM result_races rr
        JOIN finish_info f ON f.race_key=rr.race_key
        LEFT JOIN pred p ON p.race_key=rr.race_key
        LEFT JOIN feedback fb ON fb.race_key=rr.race_key
        WHERE COALESCE(rr.model_eligible,1)=1
    """
    try:
        with sqlite3.connect(db_path) as con:
            df = pd.read_sql_query(query, con)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df["勝者1位的中"] = (pd.to_numeric(df["winner"], errors="coerce") == pd.to_numeric(df["predicted_winner"], errors="coerce")).astype(int)
    df["ハンデ構成"] = df.apply(
        lambda r: "同ハンデ" if int(r.get("handicap_kinds") or 0) == 1 else "ハンデ差あり", axis=1
    )
    df["ハンデ幅"] = pd.to_numeric(df["max_handicap"], errors="coerce") - pd.to_numeric(df["min_handicap"], errors="coerce")
    df["日付×開催場"] = df["race_date"].dt.strftime("%Y-%m-%d") + " " + df["venue"].fillna("").astype(str)
    return df


def _v226_prediction_condition_summary(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    def pct_le(series, n):
        s = pd.to_numeric(series, errors="coerce").dropna()
        return float((s <= n).mean() * 100.0) if len(s) else pd.NA
    out = df.groupby(group_cols, dropna=False).agg(
        races=("race_key", "count"),
        winner_top1=("勝者1位的中", lambda s: float(pd.to_numeric(s, errors="coerce").fillna(0).mean() * 100.0)),
        trifecta_avg=("trifecta_rank", lambda s: float(pd.to_numeric(s, errors="coerce").dropna().mean()) if len(pd.to_numeric(s, errors="coerce").dropna()) else pd.NA),
        trifecta_top10=("trifecta_rank", lambda s: pct_le(s, 10)),
        trifecta_top20=("trifecta_rank", lambda s: pct_le(s, 20)),
        trio_top10=("trio_rank", lambda s: pct_le(s, 10)),
        exacta_top10=("exacta_rank", lambda s: pct_le(s, 10)),
    ).reset_index()
    return out.rename(columns={
        "races":"レース数", "winner_top1":"勝者1位的中率", "trifecta_avg":"3連単平均順位",
        "trifecta_top10":"3連単10位内率", "trifecta_top20":"3連単20位内率",
        "trio_top10":"3連複10位内率", "exacta_top10":"2連単10位内率",
    })


def _v226_render_prediction_condition_analysis(db_path: str) -> None:
    st.markdown("### 🧭 開催場・ハンデ構成別の予測成績")
    st.caption("回収率だけでなく、勝者の1位予測率と実結果買い目の予測順位を比較します。件数が少ない区分は参考値です。")
    df = _v226_prediction_condition_rows(db_path)
    if df.empty:
        st.info("開催場・ハンデ構成別に集計できる予測結果がありません。")
        return

    max_date = df["race_date"].max()
    recent_days = st.selectbox("分析期間", ["全期間", "直近7日", "直近30日"], key="v226_condition_period")
    view = df.copy()
    if pd.notna(max_date) and recent_days != "全期間":
        days = 7 if recent_days == "直近7日" else 30
        view = view[view["race_date"] >= max_date - pd.Timedelta(days=days-1)]
    if view.empty:
        st.warning("選択期間に該当する結果がありません。")
        return

    st.markdown("#### 開催場別")
    venue = _v226_prediction_condition_summary(view, ["venue"])
    if not venue.empty:
        venue = venue.rename(columns={"venue":"開催場"}).sort_values(["3連単20位内率","勝者1位的中率"], ascending=False)
        st.dataframe(venue, use_container_width=True, hide_index=True, column_config={
            "勝者1位的中率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連単平均順位": st.column_config.NumberColumn(format="%.1f位"),
            "3連単10位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連単20位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連複10位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "2連単10位内率": st.column_config.NumberColumn(format="%.1f%%"),
        })

    st.markdown("#### ハンデ構成別")
    handicap = _v226_prediction_condition_summary(view, ["ハンデ構成"])
    if not handicap.empty:
        st.dataframe(handicap, use_container_width=True, hide_index=True, column_config={
            "勝者1位的中率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連単平均順位": st.column_config.NumberColumn(format="%.1f位"),
            "3連単10位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連単20位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連複10位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "2連単10位内率": st.column_config.NumberColumn(format="%.1f%%"),
        })

    st.markdown("#### 日付 × 開催場")
    day_venue = _v226_prediction_condition_summary(view, ["日付×開催場"])
    if not day_venue.empty:
        st.dataframe(day_venue.sort_values("日付×開催場", ascending=False), use_container_width=True, hide_index=True, column_config={
            "勝者1位的中率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連単平均順位": st.column_config.NumberColumn(format="%.1f位"),
            "3連単10位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連単20位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "3連複10位内率": st.column_config.NumberColumn(format="%.1f%%"),
            "2連単10位内率": st.column_config.NumberColumn(format="%.1f%%"),
        })

    with st.expander("レース別の予測順位を確認", expanded=False):
        detail = view[["race_date","venue","race_no","ハンデ構成","winner","predicted_winner","trifecta_rank","trio_rank","exacta_rank","quinella_rank"]].copy()
        detail["日付"] = detail["race_date"].dt.strftime("%Y-%m-%d")
        detail = detail.rename(columns={"venue":"開催場","race_no":"R","winner":"実勝者","predicted_winner":"予測1位","trifecta_rank":"3連単順位","trio_rank":"3連複順位","exacta_rank":"2連単順位","quinella_rank":"2連複順位"})
        st.dataframe(detail[["日付","開催場","R","ハンデ構成","実勝者","予測1位","3連単順位","3連複順位","2連単順位","2連複順位"]].sort_values(["日付","開催場","R"], ascending=[False,True,True]), use_container_width=True, hide_index=True)

def github_config() -> dict:
    return {
        "token": secret_value("GITHUB_TOKEN"),
        "repo": secret_value("GITHUB_REPO"),
        "branch": secret_value("GITHUB_BRANCH", "main") or "main",
        "path": secret_value("GITHUB_DB_PATH", "autorace_players.sqlite3") or "autorace_players.sqlite3",
    }


def github_request(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    cfg = github_config()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AutoRaceAI-Streamlit",
    }
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return int(response.status), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"message": body}
        return int(exc.code), parsed


def github_api_url() -> str:
    cfg = github_config()
    encoded_path = urllib.parse.quote(cfg["path"], safe="/")
    return f"https://api.github.com/repos/{cfg['repo']}/contents/{encoded_path}"


def github_ready() -> tuple[bool, str]:
    cfg = github_config()
    missing = [k for k in ("token", "repo") if not cfg[k]]
    if missing:
        return False, "Streamlit Secretsに GITHUB_TOKEN と GITHUB_REPO を設定してください。"
    return True, "GitHub保存設定済み"


def pull_db_from_github() -> tuple[bool, str]:
    ready, message = github_ready()
    if not ready:
        return False, message
    cfg = github_config()
    url = github_api_url() + "?ref=" + urllib.parse.quote(cfg["branch"])
    status, body = github_request(url)
    if status != 200:
        return False, f"GitHubからDBを取得できませんでした: {body.get('message', status)}"
    try:
        data = base64.b64decode(body["content"].replace("\n", ""))
        if not data.startswith(b"SQLite format 3\x00"):
            return False, "GitHub上のファイルがSQLiteではありません。"
        Path(engine.DB_PATH).write_bytes(data)
        engine.mount_and_init_db()
        return True, f"GitHubからDBを取得しました（{len(data) / 1024 / 1024:.2f} MB）"
    except Exception as exc:
        return False, f"GitHub DB取得エラー: {type(exc).__name__}: {exc}"


def push_db_to_github(commit_message: str) -> tuple[bool, str]:
    ready, message = github_ready()
    if not ready:
        return False, message
    db_path = Path(engine.DB_PATH)
    if not db_path.exists():
        return False, "保存するDBがありません。"

    cfg = github_config()
    url = github_api_url()
    query_url = url + "?ref=" + urllib.parse.quote(cfg["branch"])
    status, existing = github_request(query_url)
    sha = existing.get("sha") if status == 200 else None
    if status not in (200, 404):
        return False, f"GitHub上のDB確認に失敗しました: {existing.get('message', status)}"

    payload = {
        "message": commit_message,
        "content": base64.b64encode(db_path.read_bytes()).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha
    put_status, result = github_request(url, method="PUT", payload=payload)
    if put_status not in (200, 201):
        return False, f"GitHub保存に失敗しました: {result.get('message', put_status)}"
    return True, "DBをGitHubへ保存しました。再起動・再デプロイ後も復元できます。"


# 起動後の最初の1回だけ、GitHub上の最新DBを取得
if "github_pull_done" not in st.session_state:
    st.session_state["github_pull_done"] = True
    ready, _ = github_ready()
    if ready:
        ok, msg = pull_db_from_github()
        st.session_state["github_pull_message"] = (ok, msg)

with st.sidebar:
    st.header("予測設定")
    trials = st.selectbox("試行回数", [3000, 10000, 20000], index=2)
    seed = st.number_input("乱数シード", min_value=0, value=20260719, step=1)
    st.divider()
    st.subheader("履歴DB")
    db_file = st.file_uploader(
        "autorace_players.sqlite3を選択",
        type=None,
        help="iPhoneの『ファイル』からSQLite本体を選択してください。ZIPのままでは読み込めません。",
    )
    if db_file is not None:
        ok, message = install_uploaded_db(db_file)
        (st.success if ok else st.error)(message)

    ready, ready_msg = github_ready()
    (st.success if ready else st.warning)(ready_msg)
    if st.button("GitHubからDBを再読込", use_container_width=True, disabled=not ready):
        ok, msg = pull_db_from_github()
        (st.success if ok else st.error)(msg)
        if ok:
            st.rerun()
    if st.button("現在のDBをGitHubへ保存", use_container_width=True, disabled=not ready):
        ok, msg = push_db_to_github("AutoRaceAI: DBを手動保存")
        (st.success if ok else st.error)(msg)

    try:
        summary = db_summary(engine.DB_PATH)
        c1, c2 = st.columns(2)
        display_players = summary.get("players", 0) or summary.get("import_players", 0)
        display_history = summary.get("history", 0) or summary.get("import_history", 0)
        c1.metric("登録選手", display_players)
        c2.metric("登録履歴", display_history)
        st.caption(f"DB容量: {summary.get('size', 0) / 1024 / 1024:.2f} MB")
        db_path = Path(engine.DB_PATH)
        if db_path.exists():
            st.download_button(
                "💾 DBを端末へ保存",
                db_path.read_bytes(),
                file_name="autorace_players.sqlite3",
                mime="application/octet-stream",
                use_container_width=True,
            )
    except Exception as exc:
        st.warning(f"DB情報を確認できません: {exc}")

def _show_sticky_notice(key: str) -> None:
    notice = st.session_state.get(key)
    if not isinstance(notice, dict):
        return
    level = str(notice.get("level", "info"))
    message = str(notice.get("message", ""))
    if message:
        getattr(st, level, st.info)(message)


def _set_sticky_notice(key: str, level: str, message: str) -> None:
    st.session_state[key] = {"level": level, "message": message}


def render_last_result_analysis(view: dict) -> None:
    """登録後の解析結果を、再描画後もセッションから復元して表示する。"""
    if not isinstance(view, dict) or not view:
        return
    comparison = view.get("comparison")
    analysis = view.get("analysis") or {}
    adjustment = view.get("adjustment") or {}
    st.markdown("### 📌 直前に登録した結果解析")
    st.caption(f"登録キー: {view.get('key', '不明')}｜別タブ操作や再描画後も保持されます。")
    mixed_result = view.get("mixed_plan_result")
    if not isinstance(mixed_result, dict):
        mixed_result = _v208_latest_mixed_plan_result(str(view.get("key", "")), engine.DB_PATH)
    _v208_render_mixed_plan_result(mixed_result)
    if "message" in analysis:
        st.warning(analysis["message"])
    else:
        a, b, c = st.columns(3)
        a.metric("平均順位誤差", analysis.get("平均順位誤差", "-"))
        b.metric("1着的中", "○" if analysis.get("1着的中") else "×")
        c.metric("予測TOP3一致", f"{analysis.get('3着内一致数', 0)}/3")
        if isinstance(comparison, pd.DataFrame) and not comparison.empty:
            show_cols = [x for x in ["着順", "車番", "選手名_x", "predicted_rank", "順位誤差", "win_prob", "top3_prob"] if x in comparison.columns]
            st.dataframe(comparison[show_cols], use_container_width=True, hide_index=True)
    pred = view.get("predicted_trifecta")
    actual = view.get("actual_trifecta")
    if pred and actual:
        st.subheader("三連単の完全一致判定")
        t1, t2, t3 = st.columns(3)
        t1.metric("予測", pred)
        t2.metric("実結果", actual)
        t3.metric("三連単的中", "○" if pred == actual else "×")
    if "before" in adjustment:
        st.subheader("結果による重みの微調整")
        rows = []
        for name in adjustment["before"]:
            rows.append({
                "項目": name,
                "調整前": adjustment["before"][name],
                "調整後": adjustment["after"][name],
                "変化": adjustment["after"][name] - adjustment["before"][name],
                "今回結果との相関": adjustment["evidence"][name],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
            column_config={
                "調整前": st.column_config.NumberColumn(format="%.4f"),
                "調整後": st.column_config.NumberColumn(format="%.4f"),
                "変化": st.column_config.NumberColumn(format="%+.4f"),
                "今回結果との相関": st.column_config.NumberColumn(format="%+.3f"),
            })
        st.info(adjustment.get("note", "学習重みを更新しました。"))


# 一般予定通知はメインタブから完全分離する。
# launcher は fragment、通知本体は dialog 内で動くため、開閉・入力・予約で他画面を再実行しない。
@st.dialog("🔔 一般予定通知", width="large")
def _v132_general_reminder_dialog():
    v123_render_general_reminder_tab()


@st.fragment
def _v132_general_reminder_launcher():
    if st.button("🔔 一般予定通知を開く", use_container_width=True, key="v132_open_general_reminder"):
        _v132_general_reminder_dialog()


_v132_general_reminder_launcher()

# 「↑ 上へ」の着地点。タイトルではなく、操作を再開しやすいメインタブまで戻す。
st.markdown('<div id="main-tabs" style="scroll-margin-top:72px;"></div>', unsafe_allow_html=True)
_main_pages = ["🏁 予測", "📊 回収率実績", "✅ 結果登録・解析", "👤 選手情報登録", "🗃️ 登録情報確認"]
if st.session_state.get("v155_main_page") not in _main_pages:
    st.session_state["v155_main_page"] = _main_pages[0]

st.markdown("""
<style>
.v161-nav-card{padding:12px 14px;margin:4px 0 8px;border:1px solid #cbd8ea;border-radius:14px;background:linear-gradient(135deg,#eef5ff,#f8fafc)}
.v161-nav-title{font-size:1.03rem;font-weight:800;color:#243247}
.v161-nav-note{font-size:.82rem;color:#607086;margin-top:3px}
</style>
<div class="v161-nav-card">
 <div class="v161-nav-title">🧭 表示する画面</div>
 <div class="v161-nav-note">下の選択欄で画面を切り替えます。通常の実行ボタンとは別のメニューです。</div>
</div>
""", unsafe_allow_html=True)
_current_page = st.session_state.get("v155_main_page", _main_pages[0])
_selected_from_nav = st.selectbox(
    "画面を選択",
    options=_main_pages,
    index=_main_pages.index(_current_page),
    key="v161_main_page_select",
    label_visibility="collapsed",
)
st.session_state["v155_main_page"] = _selected_from_nav

selected_main_page = st.session_state.get("v155_main_page", _main_pages[0])

if selected_main_page == "📊 回収率実績":
    _v215_render_return_dashboard(engine.DB_PATH)

elif selected_main_page == "🏁 予測":
    st.info("Ver20予測方式：予測競走タイム＋高速6周イベントモデル。欠車・出走取消は存在しない選手として完全除外します。")
    with st.expander("🔧 今回どこを調整したか"):
        if st.button("調整履歴を読み込む", key="v138_load_adjustment_log", use_container_width=True):
            st.session_state["v138_show_adjustment_log"] = True
        if st.session_state.get("v138_show_adjustment_log"):
            st.dataframe(
                _v138_cached_adjustment_log(engine.DB_PATH, _v138_db_token(engine.DB_PATH)),
                use_container_width=True, hide_index=True
            )
        else:
            st.caption("必要な時だけ読み込みます。")
        st.caption("Ver20では10要素（試走・ST・ハンデ・近況・走路適性・前残り・追い込み・周回安定・コース適性・相手耐性）を評価します。三連単は順番まで完全一致した場合だけ的中です。1レースの変更幅は各項目±0.003以内です。")
    # Ver231: 保存済み予測をバージョン履歴から復元。旧Ver222データも救済表示する。
    saved_histories = _v231_list_prediction_histories(engine.DB_PATH)
    # Ver235: 新履歴が1件でも存在すると旧v222保存が全て隠れる不具合を修正。
    # 両方を常に読み込み、DBから削除・移行せず一覧上で統合する。
    saved_predictions = _v222_list_prediction_restores(engine.DB_PATH)
    if saved_histories or saved_predictions:
        st.markdown("### ♻️ 保存済みレース・予測版を復元")
        restore_labels = []
        restore_by_label = {}
        for item in saved_histories:
            when = str(item.get("prediction_time") or "").replace("T", " ")[:19]
            base_label = f"{item.get('race_label','保存済み予測')}｜{item.get('app_version','Unknown')}｜{when}"
            label = base_label
            suffix = 2
            while label in restore_by_label:
                label = f"{base_label} ({suffix})"
                suffix += 1
            restore_labels.append(label)
            restore_by_label[label] = {"kind":"history", **item}
        for item in saved_predictions:
            when = str(item.get("updated_at") or "").replace("T", " ")[:19]
            base_label = f"{item.get('race_label','保存済み予測')}｜旧形式・最新保存｜{when}"
            label = base_label
            suffix = 2
            while label in restore_by_label:
                label = f"{base_label} ({suffix})"
                suffix += 1
            restore_labels.append(label)
            restore_by_label[label] = {"kind":"legacy", **item}
        st.caption("最近の保存済み予測をボタンで復元します。文字入力欄ではないため、iPhoneのキーボードは開きません。")
        visible_labels = restore_labels[:24]
        with st.expander(f"保存済み予測一覧（最新{len(visible_labels)}件）", expanded=False):
            for idx, restore_label in enumerate(visible_labels):
                target = restore_by_label.get(restore_label) or {}
                c_info, c_button = st.columns([4,1])
                with c_info:
                    st.markdown(f"**{restore_label}**")
                with c_button:
                    clicked = st.button("復元", key=f"v242_restore_btn_{idx}_{hashlib.md5(restore_label.encode()).hexdigest()[:8]}", use_container_width=True)
                if clicked:
                    if target.get("kind") == "history":
                        restored_view, restored_text, restored_venue, history_meta = _v231_load_prediction_history(engine.DB_PATH, int(target.get("history_id") or 0))
                    else:
                        restored_view, restored_text, restored_venue = _v222_load_prediction_restore(engine.DB_PATH, target.get("race_key", ""))
                        history_meta = {"app_version":"Unknown", "simulation_mode":"旧保存形式"}
                    if restored_view:
                        # Ver261: 復元は完全に「表示＋入力復元」だけ。
                        # 復元しただけでは、周回スナップショット・回収率・現行版履歴を新規保存しない。
                        # 下の「復元内容を現在Verで再シミュレーション」を押した時だけ、
                        # 現在コードのVerとして新規予測・周回スナップショットを保存する。
                        restored_view = dict(restored_view)
                        restored_view["_v231_restored_only"] = True
                        restored_view["_v231_source_app_version"] = str(
                            history_meta.get("app_version") or restored_view.get("app_version") or "Unknown"
                        )
                        restored_view["_v261_restore_source_history_id"] = int(target.get("history_id") or 0)
                        restore_lap_status={}
                        st.session_state["last_prediction_view"] = restored_view
                        st.session_state["v163_saved_prediction_text"] = restored_text
                        st.session_state["v163_saved_prediction_venue"] = restored_venue
                        st.session_state["prediction_input_version"] = int(st.session_state.get("prediction_input_version", 0)) + 1
                        restored_race_key = str(target.get("race_key") or history_meta.get("race_key") or restored_view.get("race_key") or "").strip()
                        restored_result_view = _v232_load_result_view_for_race(engine.DB_PATH, restored_race_key)
                        if restored_result_view:
                            st.session_state["v41_last_result_view"] = restored_result_view
                            st.session_state["v232_restored_result_view"] = restored_result_view
                        else:
                            st.session_state.pop("v232_restored_result_view", None)
                        st.session_state["v231_restore_notice"] = {
                            "label": target.get("race_label") or "保存済みレース",
                            "version": history_meta.get("app_version") or "Unknown",
                            "mode": history_meta.get("simulation_mode") or "不明",
                            "result_restored": bool(restored_result_view),
                            "lap_snapshot": restore_lap_status,
                        }
                        st.rerun()
                    else:
                        st.warning("保存済み予測を復元できませんでした。")
        notice = st.session_state.pop("v231_restore_notice", None)
        if isinstance(notice, dict):
            result_note = "登録済み結果・的中判定も復元しました。" if notice.get("result_restored") else "この予測版に対応する登録済み結果はまだありません。"
            st.success(
                f"{notice.get('label')}を復元しました。元の予測版: {notice.get('version')} / {notice.get('mode')}。"
                f"保存済みオッズも下で自動復元されます。{result_note}"
            )
            st.info(
                f"復元しただけでは新しい実績は登録しません。下の『▶ 復元内容を{_V231_APP_VERSION}で再シミュレーション』を押すと、"
                f"同じレースを現在コードで再計算し、{_V231_APP_VERSION}の別履歴として保存します。"
            )
        restored_result = st.session_state.get("v232_restored_result_view")
        if isinstance(restored_result, dict) and restored_result:
            with st.expander("✅ このレースの登録済み結果・実回収率", expanded=False):
                render_last_result_analysis(restored_result)
        st.caption("同じレースを再予測しても上書きせず、予測時刻・バージョン別に履歴を残します。旧形式の保存も消さずに一覧へ統合し、Version Unknown／旧形式として扱います。")

    st.session_state.setdefault("prediction_input_version", 0)
    if st.button("🗑️ 予測入力をリセット", use_container_width=True, key="reset_prediction_input"):
        st.session_state["prediction_input_version"] += 1
        st.session_state.pop("last_prediction_view", None)
        st.session_state.pop("v232_restored_result_view", None)
        _v163_clear_saved_inputs("v163_saved_prediction_text", "v163_saved_prediction_venue")
        st.rerun()
    prediction_version = st.session_state["prediction_input_version"]
    prediction_text_key = f"race_card_text_{prediction_version}"
    _v163_restore_input(prediction_text_key, "v163_saved_prediction_text", "")
    text = st.text_area(
        "公式出走表を全文貼り付け",
        height=430,
        placeholder="autorace.jpの出走表をコピーして貼り付け",
        key=prediction_text_key,
        on_change=_v163_save_input,
        args=(prediction_text_key, "v163_saved_prediction_text"),
    )

    # 本文から開催場を取得できない場合だけ、予測用の補助入力を表示する。
    prediction_venue_override = ""
    detected_prediction_venue = ""
    if text.strip():
        try:
            detected_meta = engine.v15_parse_race_meta(text) or {}
            detected_prediction_venue = str(detected_meta.get("開催場") or "").strip()
        except Exception:
            detected_prediction_venue = ""

        if detected_prediction_venue:
            st.caption(f"開催場を自動取得: {detected_prediction_venue}")
        else:
            prediction_venue_key = f"prediction_venue_override_{prediction_version}"
            _v163_restore_input(prediction_venue_key, "v163_saved_prediction_venue", "")
            prediction_venue_override = st.selectbox(
                "開催場（出走表から取得できないため選択してください）",
                options=["", "川口", "伊勢崎", "浜松", "山陽", "飯塚"],
                format_func=lambda value: "選択してください" if value == "" else value,
                key=prediction_venue_key,
                on_change=_v163_save_input,
                args=(prediction_venue_key, "v163_saved_prediction_venue"),
            )
            st.caption("選手の所属場は開催場として使いません。実際の開催場を選択してください。")

    # 出走表の事前解析は入力中に毎回走らせず、確認ボタンを押した時だけ実行する。
    preview_key = hashlib.sha1(text.encode("utf-8")).hexdigest() if text.strip() else ""
    if text.strip() and st.button("📋 出走表の読み取りを確認", use_container_width=True, key=f"v138_preview_{prediction_version}"):
        try:
            preview_entries = engine.v15_parse_entries(text)
            preview_meta = engine.v15_parse_race_meta(text) or {}
            st.session_state["v138_prediction_preview"] = {
                "key": preview_key, "entries": preview_entries, "meta": preview_meta
            }
        except Exception as exc:
            st.session_state["v138_prediction_preview"] = {"key": preview_key, "error": str(exc)}

    preview_state = st.session_state.get("v138_prediction_preview", {})
    if preview_state.get("key") == preview_key:
        if preview_state.get("error"):
            st.warning(f"出走表の事前確認に失敗しました: {preview_state['error']}")
        else:
            preview_entries = preview_state.get("entries")
            preview_meta = preview_state.get("meta") or {}
            if isinstance(preview_entries, pd.DataFrame) and not preview_entries.empty:
                preview_cols = [c for c in [
                    "車番", "選手名", "所属", "ハンデ", "試走T", "ST", "試走偏差",
                    "現ランク", "平均競走T", "最高競走T", "近10走着順", "近10走2連",
                    "近10走3連", "車名"
                ] if c in preview_entries.columns]
                expected_entries = int(preview_meta.get("出走数")) if preview_meta.get("出走数") else None
                actual_entries = int(preview_entries["車番"].nunique())
                # Ver209: シミュレーション前に入力内容とDB量を確認できるよう、2表を最上部へ常時表示する。
                st.markdown("## 🔎 シミュレーション前の確認")
                st.subheader(f"解析した出走表（{actual_entries}名）")
                st.dataframe(preview_entries[preview_cols], use_container_width=True, hide_index=True)
                if expected_entries and actual_entries < expected_entries:
                    present = set(preview_entries["車番"].dropna().astype(int).tolist())
                    missing = [car for car in range(1, expected_entries + 1) if car not in present]
                    st.warning(f"⚠ {actual_entries}/{expected_entries}車のみ読み取りました。未読込候補: " + "、".join(f"{car}番" for car in missing))
                elif expected_entries:
                    st.success(f"✅ {actual_entries}/{expected_entries}車を正常に読み取りました。")
                show_player_data_coverage(preview_entries)
                st.caption("この2表を確認してから、下の予測ボタンを押してください。")
            else:
                st.warning("出走表から選手を読み取れませんでした。")

    manual_excluded = []
    if text.strip():
        auto_excluded = {int(car): "手動指定" for car in manual_excluded}
        # 車番候補はヘッダの出走数から軽量生成。詳細パーサーを入力のたびに実行しない。
        count_match = re.search(r"([1-8])車", text)
        expected_count = int(count_match.group(1)) if count_match else 8
        available_cars = list(range(1, expected_count + 1))
        if preview_state.get("key") == preview_key and isinstance(preview_state.get("entries"), pd.DataFrame):
            parsed_cars = sorted(preview_state["entries"]["車番"].dropna().astype(int).unique().tolist())
            if parsed_cars:
                available_cars = sorted(set(available_cars) | set(parsed_cars))
        defaults = [car for car in available_cars if car in auto_excluded]
        manual_excluded = st.multiselect(
            "欠車・出走取消として除外する車番（手動で変更できます）",
            options=available_cars,
            default=defaults,
            format_func=lambda x: f"{x}番",
            key=f"manual_excluded_cars_{prediction_version}",
        )
        if auto_excluded:
            detected = "、".join(f"{car}番" for car in sorted(auto_excluded))
            st.caption(f"自動検出: {detected}。誤っている場合は上の選択を外してください。")
        else:
            st.caption("自動検出された欠車はありません。必要な車番だけ選択してください。")

    # Ver261: 通常予測と、復元した入力を現在Verで再シミュレーションする操作を明確に分離。
    _restored_view_for_rerun = st.session_state.get("last_prediction_view") or {}
    _is_restored_for_rerun = bool(isinstance(_restored_view_for_rerun, dict) and _restored_view_for_rerun.get("_v231_restored_only"))
    if _is_restored_for_rerun:
        _src_ver = str(_restored_view_for_rerun.get("_v231_source_app_version") or _restored_view_for_rerun.get("app_version") or "Unknown")
        st.caption(f"復元元: {_src_ver} → 再シミュレーション保存先: {_V231_APP_VERSION}")
        _b1, _b2 = st.columns(2)
        with _b1:
            prediction_clicked = st.button("通常予測として実行", use_container_width=True, key="v261_normal_prediction")
        with _b2:
            rerun_clicked = st.button(
                f"▶ 復元内容を{_V231_APP_VERSION}で再シミュレーション",
                type="primary", use_container_width=True, key="v261_rerun_current_version"
            )
        prediction_clicked = bool(prediction_clicked or rerun_clicked)
        st.session_state["v261_rerun_requested"] = bool(rerun_clicked)
    else:
        prediction_clicked = st.button("解析して元版設定で予測", type="primary", use_container_width=True)
        st.session_state["v261_rerun_requested"] = False
    if prediction_clicked:
        if not text.strip():
            st.warning("出走表を貼り付けてください。")
            st.stop()
        if not detected_prediction_venue and not prediction_venue_override:
            st.warning("開催場を選択してください。")
            st.stop()
        try:
            prediction_text = text
            if prediction_venue_override:
                prediction_text = f"開催場: {prediction_venue_override}\n" + text
            prediction_timing = {}
            with st.spinner("高速6周イベントシミュレーションを実行中…"):
                _t0 = time_module.perf_counter()
                df, bets, output, entries, meta = engine.ver16_run_prediction(prediction_text, int(trials), int(seed), manual_excluded=manual_excluded)
                # Ver230 beta: 壁補正を後掛けせず、スタートから6周すべての展開へ内蔵。
                meta = dict(meta or {})
                df, bets, wall_audit = _v230_six_lap_simulation(df, bets, entries, meta, int(trials), int(seed))
                meta["壁補正監査"] = wall_audit
                meta["6周展開シミュレーション"] = wall_audit
                _t1 = time_module.perf_counter()
                finish_prob = engine.v30_finish_probabilities(df, bets, int(trials))
                df = engine.v196_apply_probability_aligned_ranks(df, finish_prob)
                _t2 = time_module.perf_counter()
            # オッズ欄の表示に必要なレースキーだけ同期保存。
            _t_save0 = time_module.perf_counter()
            race_key = engine.v34_save_prediction_snapshot(meta, df, finish_prob, engine.DB_PATH)
            _t_save1 = time_module.perf_counter()
            # 全買い目確率と特徴量は待たずにバックグラウンド保存する。
            threading.Thread(
                target=_v217_deferred_prediction_db_save,
                args=(meta, bets, int(trials), df),
                daemon=True,
                name="autorace-deferred-db-save",
            ).start()
            prediction_timing = {
                "simulation": _t1 - _t0,
                "aggregation": _t2 - _t1,
                "db_save": _t_save1 - _t_save0,
                "deferred_db_save": True,
                "total": _t_save1 - _t0,
            }
            # 重いDB全体診断は予測完了の必須経路から外し、詳細表示時に必要になった場合だけ取得する。
            # オッズ入力などによる再描画後も、直前の予測結果を保持する。
            prediction_view = {
                "df": df,
                "bets": bets,
                "output": output,
                "entries": entries,
                "meta": meta,
                "finish_prob": finish_prob,
                "race_key": race_key,
                "trials": int(trials),
                "excluded": [int(x) for x in manual_excluded],
                "learning_boundary": {},
                "future_audit": {},
                "day_trend": {},
                "prediction_timing": prediction_timing,
                "app_version": _V231_APP_VERSION,
                "simulation_mode": _V231_SIMULATION_MODE,
                "settings_hash": _v231_settings_hash(int(trials), int(seed), [int(x) for x in manual_excluded]),
                "prediction_time": _v228_now_jst_iso(),
                "seed": int(seed),
                "rerun_from_restored": bool(st.session_state.get("v261_rerun_requested", False)),
                "rerun_source_version": (
                    str(_restored_view_for_rerun.get("_v231_source_app_version") or _restored_view_for_rerun.get("app_version") or "")
                    if bool(st.session_state.get("v261_rerun_requested", False)) else ""
                ),
                "rerun_source_history_id": (
                    int(_restored_view_for_rerun.get("_v261_restore_source_history_id") or 0)
                    if bool(st.session_state.get("v261_rerun_requested", False)) else 0
                ),
            }
            st.session_state["last_prediction_view"] = prediction_view
            # 新しい予測が生成された時点で復元専用フラグは解除される。
            # rerun_requested は保存メッセージ用にこの実行中だけ保持する。
            # 復元に必要な出走表と計算済み結果を同期保存。次回は再シミュレーション不要。
            try:
                history_id = _v231_save_prediction_history(
                    engine.DB_PATH, race_key, text, prediction_venue_override, prediction_view, int(trials), int(seed)
                )
                # 旧復元テーブルにも最新だけ保存し、既存機能との互換性を維持する。
                _v222_save_prediction_restore(
                    engine.DB_PATH, race_key, text, prediction_venue_override, prediction_view
                )
                if bool(st.session_state.get("v261_rerun_requested", False)):
                    _from_ver = str(prediction_view.get("rerun_source_version") or "旧版")
                    st.success(
                        f"再シミュレーションが完了しました。{_from_ver}の復元入力を現在の{_V231_APP_VERSION}で再計算し、"
                        f"別履歴として保存しました（履歴ID: {history_id}）。元の{_from_ver}履歴は変更していません。"
                    )
                else:
                    st.success(f"予測が完了しました。{_V231_APP_VERSION}として履歴保存しました（履歴ID: {history_id}）。")
                try:
                    _lap_save=((meta.get("6周展開シミュレーション") or {}).get("lap_snapshot_save") or {})
                    if _lap_save and not _lap_save.get("ok"):
                        st.warning("精度比較用の周回保存に失敗しました: "+str(_lap_save.get("reason") or "不明"))
                    elif _lap_save.get("ok"):
                        st.caption(f"精度比較用周回保存: {_lap_save.get('saved_laps',0)}周 / {_lap_save.get('app_version',_V231_APP_VERSION)}")
                except Exception:
                    pass
            except Exception as save_exc:
                st.success("予測が完了しました。")
                st.warning(f"復元用保存だけ失敗しました: {type(save_exc).__name__}: {save_exc}")
            st.session_state["v261_rerun_requested"] = False
            if prediction_timing:
                st.caption(
                    f"処理時間：イベント計算 {prediction_timing['simulation']:.2f}秒 / "
                    f"確率集計 {prediction_timing['aggregation']:.2f}秒 / "
                    f"最小DB保存 {prediction_timing['db_save']:.2f}秒 / 表示まで {prediction_timing['total']:.2f}秒"
                )
        except Exception as exc:
            st.error(f"予測エラー: {type(exc).__name__}: {exc}")
            st.exception(exc)

    view = st.session_state.get("last_prediction_view")
    if view:
        # 予測が完了した後だけ、解析ボタン直下にショートカットを表示する。
        # 監査・補正テーブルより先に置き、スマホでもすぐ結果各部へ移動できるようにする。
        v73_section_nav()
        st.caption(
            f"予測版: {view.get('app_version','Unknown')}｜方式: {view.get('simulation_mode','不明')}｜"
            f"設定ID: {view.get('settings_hash','-')}｜予測時刻: {str(view.get('prediction_time','-')).replace('T',' ')[:19]}"
        )
        timing = view.get("prediction_timing") or {}
        if timing:
            st.caption(
                f"前回処理時間：イベント計算 {float(timing.get('simulation',0)):.2f}秒 / "
                f"確率集計 {float(timing.get('aggregation',0)):.2f}秒 / "
                f"最小DB保存 {float(timing.get('db_save',0)):.2f}秒（詳細保存はバックグラウンド）"
            )
        try:
            df = view["df"]
            bets = view["bets"]
            output = view["output"]
            entries = view["entries"]
            meta = view["meta"]
            finish_prob = view["finish_prob"]
            race_key = view.get("race_key", "current")
            view_trials = int(view.get("trials", trials))
            excluded = {int(car): "手動指定" for car in view.get("excluded", [])}
            boundary = view.get("learning_boundary") or {}
            audit = view.get("future_audit") or {}
            day_trend = view.get("day_trend") or {}
            odds_namespace = re.sub(r"[^0-9A-Za-z_-]+", "_", str(race_key))[-80:] or "current"
            # Ver207: DB全体診断や詳細表より先に、オッズ入力と回収率重視の買い目を最優先表示する。
            v202_quick_bulk_odds_input(odds_namespace, race_key=race_key)
            fast_odds_maps = {
                "3tan": st.session_state.get(f"saved_odds_{odds_namespace}_3tan", {}),
                "3fuku": st.session_state.get(f"saved_odds_{odds_namespace}_3fuku", {}),
                "2tansho": st.session_state.get(f"saved_odds_{odds_namespace}_2tansho", {}),
                "2fuku": st.session_state.get(f"saved_odds_{odds_namespace}_2fuku", {}),
                "tansho": st.session_state.get(f"saved_odds_{odds_namespace}_tansho", {}),
                "wide": st.session_state.get(f"saved_odds_{odds_namespace}_wide", {}),
            }
            st.markdown('<div id="return-priority-plan"></div>', unsafe_allow_html=True)
            st.markdown("## ⭐ 最優先・回収率重視の推奨買い目")
            st.caption("オッズ読込後、監査・確率表・展開表などの詳細表示より先に計算して表示します。")
            show_v184_eight_car_mixed_plan(
                bets, view_trials, meta, fast_odds_maps, race_key=race_key,
                app_version=str(view.get("app_version") or view.get("_v231_source_app_version") or "Unknown"),
                save_enabled=not bool(view.get("_v231_restored_only", False)),
            )
            if bool(view.get("_v231_restored_only", False)):
                st.caption("復元表示中のため、回収率実績へ新しいプランは保存していません。再解析した場合だけ現行版として保存します。")
            st.divider()
            st.markdown("### 詳細予測・診断")
            if day_trend:
                val = day_trend.get("validation") or {}
                if float(day_trend.get("blend", 0.0) or 0.0) > 0:
                    st.info(f"📍 当日展開傾向：{day_trend.get('label','中立')}｜直前{int(day_trend.get('prior_races',0))}R｜シナリオ補正 {float(day_trend.get('effective_shift',0.0)):+.3f}")
                else:
                    st.caption(f"当日展開傾向は直前{int(day_trend.get('prior_races',0))}Rを確認しましたが、時系列検証で改善が確認できないため本番反映を停止しています。")
                with st.expander("当日展開傾向の検証", expanded=False):
                    st.write(day_trend.get("reason", ""))
                    if int(val.get("race_count",0)):
                        st.write(f"過去{int(val['race_count'])}Rの未来参照なし検証：会場基準MAE {float(val['baseline_mae']):.4f} / 当日補正MAE {float(val['day_mae']):.4f}")
            if audit:
                status = audit.get("status", "OK")
                if status == "OK":
                    st.success(
                        f"🔒 未来データ監査：OK｜確認 {int(audit.get('total_checked',0))}件｜"
                        f"使用 {int(audit.get('used',0))}件｜除外 {int(audit.get('excluded',0))}件｜混入 0件"
                    )
                else:
                    st.error(f"未来データ監査：混入 {int(audit.get('violations',0))}件を検出し、使用を停止しました。")
                with st.expander("未来データ監査の内訳", expanded=False):
                    events = audit.get("events", [])
                    if events:
                        st.dataframe(pd.DataFrame(events).drop(columns=["details"], errors="ignore"), use_container_width=True, hide_index=True)
                        for ev in events:
                            if ev.get("details"):
                                st.caption(f"{ev.get('source')}: " + " / ".join(ev.get("details", [])))
                    else:
                        st.caption("監査対象の履歴はありませんでした。")
            if boundary:
                st.info(
                    f"🕒 学習境界：{boundary.get('label', '')}｜"
                    f"使用 {int(boundary.get('used_rows', 0))}件｜"
                    f"対象レース以降を除外 {int(boundary.get('excluded_rows', 0))}件"
                )
                if int(boundary.get('unknown_r_same_day_excluded', 0)):
                    st.caption(
                        f"同日でR不明の履歴 {int(boundary.get('unknown_r_same_day_excluded', 0))}件は、"
                        "先読み防止のため安全側で除外しました。"
                    )
            if excluded:
                detail = "、".join(f"{car}番（{status}）" for car, status in sorted(excluded.items()))
                st.warning(f"解析対象外: {detail}。確率・順位・買い目の組み合わせから完全に除外しました。")
            st.caption(f"実出走数: {len(entries)}車 / 三連単組み合わせ数: {len(entries)*(len(entries)-1)*(len(entries)-2)}通り")
            with st.expander("解析入力と登録データ量を再確認", expanded=False):
                st.dataframe(entries.drop(columns=["_raw"], errors="ignore"), use_container_width=True, hide_index=True)
                show_player_data_coverage(entries)

            cols = [c for c in [
                "改善後順位", "1着候補順位", "連対候補順位", "3着候補順位", "総合点順位_従来",
                "車", "選手名", "ハンデ", "試走換算", "予測競走T", "レース信頼度",
                "本番1着率", "本番連対率", "本番3着率", "本番3着内率", "順位整合メモ",
                "基礎スピード点", "実戦能力点", "勝負強さ点", "展開適性点",
                "スタート伸び指数", "ゴール前伸び指数", "安定上位指数",
                "6周壁遭遇率", "6周追抜成功回数", "連続追抜発生回数", "1周目先頭率", "壁リスク", "壁突破力", "前残り指数", "壁ロス推定",
                "混戦突破適性", "逃げ判定", "初周先頭推定", "逃げ残り推定", "逃切り推定", "逃げ履歴件数", "逃げ履歴補正",
                "展開タイプ_実測", "展開履歴件数", "展開学習補正", "熱走路帯", "高温履歴件数", "熱走路適性", "熱走路学習補正", "Ver60総合補正",
                "同ハンデ内枠補正", "車中期成績補正", "試走偏差補正", "高温位置補正", "Ver24展開穴補正", "選手別条件適性補正", "条件適性根拠", "条件一致最大件数", "条件適性信頼度",
                "今回レース種別", "レース種別履歴件数", "レース種別適性信頼度", "レース種別適性差", "レース種別適性補正", "レース種別適性傾向", "改善後総合点",
            ] if c in df.columns]
            result = df[cols].sort_values(["改善後順位", "車"]).reset_index(drop=True)
            wall_audit = meta.get("壁補正監査") or {}
            if wall_audit.get("enabled"):
                risks = wall_audit.get("high_risk") or []
                risk_text = " / ".join(
                    f"{int(x['car'])}番 壁ロス{float(x.get('effective_loss',0))*100:.1f}%" for x in risks
                )
                st.info(f"🧱 6周内蔵型の壁展開を反映｜{risk_text}\n\n{wall_audit.get('message','')}")
                lap_cmp = wall_audit.get("actual_lap_comparison") or []
                if lap_cmp:
                    st.markdown("**実測グランドノートとの1周別比較**")
                    st.dataframe(pd.DataFrame(lap_cmp).rename(columns={"lap":"周回","label":"実測ラベル","actual":"実際隊列","predicted":"予測代表隊列","position_accuracy":"位置一致率%","pairwise_accuracy":"前後関係一致率%"}), use_container_width=True, hide_index=True)
                else:
                    pred_laps = wall_audit.get("predicted_lap_orders") or []
                    if pred_laps:
                        st.caption("予測対象レースの実結果が登録されると、ここに1周ごとの実測比較が表示されます。")
                scen_dist=wall_audit.get("scenario_distribution_v263") or {}
                if scen_dist:
                    st.markdown("**Ver263 複数展開シナリオ**")
                    st.dataframe(pd.DataFrame([{"展開タイプ":k,"予測確率%":round(float(v),2)} for k,v in scen_dist.items()]),use_container_width=True,hide_index=True)
                    act=wall_audit.get("actual_scenario_v263") or '不明'
                    if act!='不明':
                        simv=float(wall_audit.get("closest_route_similarity_v263",0.0) or 0.0)
                        st.success(f"実際の展開判定: {act}｜予測ルート内の最高近似度 {simv:.1f}%")
                        routes=wall_audit.get("top_routes_v263") or []
                        if routes:
                            st.dataframe(pd.DataFrame(routes).rename(columns={"support":"発生率%","scenario":"展開タイプ","similarity":"実測近似度%","route":"1〜6周ルート"}),use_container_width=True,hide_index=True)
                try:
                    sim_n = int(wall_audit.get("sim_trials", 0) or 0)
                    planned_n = int(wall_audit.get("planned_trials", 0) or 0)
                    prep_s = float(wall_audit.get("prepare_seconds", 0.0) or 0.0)
                    sim_s = float(wall_audit.get("simulation_seconds", 0.0) or 0.0)
                    top_scenarios = wall_audit.get("top_scenarios") or []
                    top_text = " / ".join(
                        f"{x.get('combo')} {float(x.get('prob',0)):.2f}%" for x in top_scenarios[:3]
                    ) or "候補なし"
                    st.caption(
                        f"展開診断｜実行 {sim_n:,}回 / 計画 {planned_n:,}回｜準備 {prep_s:.2f}秒 / 6周計算 {sim_s:.2f}秒｜"
                        f"上位展開 {top_text}"
                    )
                except Exception:
                    pass
            st.subheader("予測順位")
            st.dataframe(result, use_container_width=True, hide_index=True)
            st.caption("Ver196では最終順位を本シミュレーションの1着率と一致させます。従来の総合点順位は診断列として残し、連対・3着候補は別順位で確認できます。")
            try:
                v196_val = v202_cached_probability_rank_validation(str(engine.DB_PATH), Path(engine.DB_PATH).stat().st_mtime)
                if int(v196_val.get("race_count", 0)):
                    st.info(
                        f"DB検証 {int(v196_val['race_count'])}R｜上位3車捕捉 平均 "
                        f"{v196_val['baseline_top3']:.3f}→{v196_val['aligned_top3']:.3f}台｜"
                        f"勝者の平均順位 {v196_val['baseline_winner_rank']:.3f}→{v196_val['aligned_winner_rank']:.3f}位"
                    )
            except Exception as exc:
                st.caption(f"確率整合順位の履歴検証を表示できませんでした: {exc}")
            try:
                pb = v202_cached_position_bias_profile(str(engine.DB_PATH), Path(engine.DB_PATH).stat().st_mtime)
                state = "反映" if pb.get("enabled") else "停止"
                st.info(
                    f"原因別学習ゲート（前後位置）: {state}｜比較{int(pb.get('race_count',0))}R "
                    f"（学習{int(pb.get('train_count',0))}R／直近{int(pb.get('valid_count',0))}R）｜"
                    f"展開補正 {float(pb.get('front_shift',0.0))*100:+.1f}pt\n\n{pb.get('reason','')}"
                )
            except Exception as exc:
                st.caption(f"原因別学習ゲートを表示できませんでした: {exc}")

            with st.expander("🏟️ 今回の開催場重み・適用補正", expanded=True):
                venue_name = str(meta.get("開催場") or "").strip()
                try:
                    profile = engine.v92_venue_weight_profile(venue_name, engine.DB_PATH)
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("開催場", profile.get("開催場") or "未取得")
                    race_count = int(profile.get("レース数", 0) or 0)
                    row_count = int(profile.get("開催場全履歴行数", 0) or 0)
                    row_conf = float(profile.get("レース番号不要反映率", 0.0) or 0.0)
                    restored_count = int(profile.get("履歴復元レース数", 0) or 0)
                    m2.metric("全履歴学習", f"{row_count:,}行")
                    m3.metric("全履歴反映率", f"{row_conf * 100:.1f}%")
                    m4.metric("展開学習", f"{race_count}R")
                    if row_count < 50:
                        st.warning("開催場の全履歴が50行未満のため、第1層補正は安全側で弱く反映されます。")
                    st.caption(f"第1層はレース番号不要で{row_count:,}行を使用。第2層は同一レースへ復元できた{race_count}R（履歴復元{restored_count}R）で前残り・追込みを学習します。")
                    st.caption("選手×開催場相性も本人の全場成績との差から縮小推定し、走数が少ない選手は弱く反映します。")
                    weight_df = pd.DataFrame(profile.get("重み明細", []))
                    if not weight_df.empty:
                        # 表示専用に0〜1の信頼度を百分率へ変換する。学習値そのものは変更しない。
                        weight_view = weight_df.copy()
                        if "信頼度" in weight_view.columns:
                            weight_view["信頼度"] = pd.to_numeric(weight_view["信頼度"], errors="coerce") * 100.0
                        st.dataframe(
                            weight_view,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "基礎係数": st.column_config.NumberColumn(format="%.2f"),
                                "開催場差": st.column_config.NumberColumn(format="%+.3f"),
                                "信頼度": st.column_config.NumberColumn(format="%.1f%%"),
                                "実効係数": st.column_config.NumberColumn(format="%.3f"),
                                "最大寄与目安": st.column_config.NumberColumn(format="%.3f"),
                                "実際寄与目安": st.column_config.NumberColumn(format="%.3f"),
                            },
                        )
                    applied = engine.v92_applied_venue_corrections(df)
                    if not applied.empty:
                        st.markdown("#### 各選手へ実際に掛かった補正")
                        # 表示専用に0〜1の信頼度を百分率へ変換する。予測計算値は維持する。
                        applied_view = applied.copy()
                        for pct_col in [
                            "選手開催場相性信頼度", "開催場全履歴反映率",
                            "開催場学習反映率", "開催場特徴信頼度",
                        ]:
                            if pct_col in applied_view.columns:
                                applied_view[pct_col] = pd.to_numeric(applied_view[pct_col], errors="coerce") * 100.0
                        st.dataframe(
                            applied_view,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "開催場特徴補正": st.column_config.NumberColumn(format="%+.3f"),
                                "開催場補正前総合点": st.column_config.NumberColumn(format="%.3f"),
                                "開催場補正後総合点": st.column_config.NumberColumn(format="%.3f"),
                                "開催場補正前予測T": st.column_config.NumberColumn(format="%.4f"),
                                "開催場共通タイム補正秒": st.column_config.NumberColumn(format="%+.4f"),
                                "選手開催場相性秒": st.column_config.NumberColumn(format="%+.4f"),
                                "開催場補正秒": st.column_config.NumberColumn(format="%+.4f"),
                                "開催場補正後予測T": st.column_config.NumberColumn(format="%.4f"),
                                "選手開催場相性信頼度": st.column_config.NumberColumn(format="%.1f%%"),
                                "開催場全履歴反映率": st.column_config.NumberColumn(format="%.1f%%"),
                                "開催場学習反映率": st.column_config.NumberColumn(format="%.1f%%"),
                                "開催場特徴信頼度": st.column_config.NumberColumn(format="%.1f%%"),
                                "開催場前残り差": st.column_config.NumberColumn(format="%+.3f"),
                                "開催場追込み差": st.column_config.NumberColumn(format="%+.3f"),
                                "開催場試走信頼差": st.column_config.NumberColumn(format="%+.3f"),
                                "開催場ST影響差": st.column_config.NumberColumn(format="%+.3f"),
                            },
                        )
                    st.caption("Ver96は展開層・全履歴層・選手相性を分けて表示します。補正は縮小・上限制御され、少数データだけで順位が暴れない設計です。")
                except Exception as exc:
                    st.warning(f"開催場重みを表示できませんでした: {exc}")

            with st.expander("🎯 レース種別適性・一般戦傾向", expanded=True):
                type_cols = [c for c in [
                    "車", "選手名", "今回レース種別", "レース種別履歴件数",
                    "レース種別適性信頼度", "レース種別適性差", "レース種別適性補正",
                    "試走実走変換平均", "レース種別適性傾向"
                ] if c in df.columns]
                if type_cols:
                    type_view = df[type_cols].sort_values(["レース種別適性補正", "車"], ascending=[False, True]).reset_index(drop=True)
                    if "レース種別適性信頼度" in type_view.columns:
                        type_view["レース種別適性信頼度"] = pd.to_numeric(type_view["レース種別適性信頼度"], errors="coerce") * 100.0
                    st.dataframe(type_view, use_container_width=True, hide_index=True, column_config={
                        "レース種別適性信頼度": st.column_config.NumberColumn(format="%.1f%%"),
                        "レース種別適性差": st.column_config.NumberColumn(format="%+.3f"),
                        "レース種別適性補正": st.column_config.NumberColumn(format="%+.3f"),
                        "試走実走変換平均": st.column_config.NumberColumn(format="%.3f"),
                    })
                else:
                    st.info("レース種別適性データを取得できませんでした。")
                st.caption("『やる気』や意図は断定せず、一般戦・予選・準決勝系・優勝戦・選抜戦ごとの実走差を縮小推定します。履歴が少ない選手は本人の通常成績へ強く寄せます。")
                st.caption("次走予定や調整目的は、未来の出走データがDBに十分揃うまでは補正に使いません。")

            with st.expander("🔄 グランドノート追い抜き相性", expanded=True):
                matchup_cols = [c for c in [
                    "車", "選手名", "対戦周回比較数", "信頼対戦相手数",
                    "追い抜き相性指数", "追い抜き相性補正", "追い抜き相性メモ"
                ] if c in df.columns]
                if matchup_cols:
                    matchup_view = df[matchup_cols].sort_values(["追い抜き相性補正", "車"], ascending=[False, True]).reset_index(drop=True)
                    st.dataframe(matchup_view, use_container_width=True, hide_index=True, column_config={
                        "追い抜き相性指数": st.column_config.NumberColumn(format="%+.3f"),
                        "追い抜き相性補正": st.column_config.NumberColumn(format="%+.3f"),
                    })
                else:
                    st.info("今回の出走選手間で比較できるグランドノート履歴がありません。")
                st.caption("周回ごとの前後関係が反転した場合だけ追い抜きとして集計します。最終着順だけの先着は含めません。対戦数が少ない組み合わせは縮小し、補正を小さく制限します。")

            with st.expander("🏍️ 逃げ役・逃げ残り診断", expanded=True):
                escape_cols = [c for c in [
                    "車", "選手名", "ハンデ", "逃げ判定", "初周先頭推定", "逃げ残り推定",
                    "逃切り推定", "逃げ履歴件数", "初周先頭実測件数", "逃げ履歴補正", "逃げ根拠"
                ] if c in df.columns]
                escape_view = df[escape_cols].sort_values(["ハンデ", "車"]).reset_index(drop=True)
                for pct_col in ["初周先頭推定", "逃げ残り推定", "逃切り推定"]:
                    if pct_col in escape_view.columns:
                        escape_view[pct_col] = pd.to_numeric(escape_view[pct_col], errors="coerce") * 100.0
                st.dataframe(
                    escape_view, use_container_width=True, hide_index=True,
                    column_config={
                        "初周先頭推定": st.column_config.NumberColumn(format="%.1f%%"),
                        "逃げ残り推定": st.column_config.NumberColumn(format="%.1f%%"),
                        "逃切り推定": st.column_config.NumberColumn(format="%.1f%%"),
                        "逃げ履歴補正": st.column_config.NumberColumn(format="%+.3f"),
                    },
                )
                st.caption("初周先頭は主導権、逃げ残りは2～3着を含む粘り、逃切りは1着まで残す見込みです。周回実測が少ない間は0m実績・ST・今回の位置関係を中心に推定します。")

            with st.expander("🧭 グランドノート展開学習・熱走路診断", expanded=True):
                v60_cols = [c for c in [
                    "車", "選手名", "展開タイプ_実測", "展開履歴件数", "初周主導指数", "位置維持指数",
                    "捌き指数", "追込み指数", "終盤指数_実測", "失速リスク", "展開学習補正",
                    "熱走路帯", "高温履歴件数", "50℃以上3着内率", "熱走路適性", "熱走路学習補正",
                    "Ver60総合補正", "Ver60根拠"
                ] if c in df.columns]
                v60_view = df[v60_cols].sort_values(["Ver60総合補正", "車"], ascending=[False, True]).reset_index(drop=True)
                for pc in ["初周主導指数", "位置維持指数", "捌き指数", "追込み指数", "終盤指数_実測", "失速リスク", "50℃以上3着内率", "熱走路適性"]:
                    if pc in v60_view.columns:
                        v60_view[pc] = pd.to_numeric(v60_view[pc], errors="coerce") * 100.0
                st.dataframe(v60_view, use_container_width=True, hide_index=True, column_config={
                    "初周主導指数": st.column_config.NumberColumn(format="%.1f%%"),
                    "位置維持指数": st.column_config.NumberColumn(format="%.1f%%"),
                    "捌き指数": st.column_config.NumberColumn(format="%.1f%%"),
                    "追込み指数": st.column_config.NumberColumn(format="%.1f%%"),
                    "終盤指数_実測": st.column_config.NumberColumn(format="%.1f%%"),
                    "失速リスク": st.column_config.NumberColumn(format="%.1f%%"),
                    "50℃以上3着内率": st.column_config.NumberColumn(format="%.1f%%"),
                    "熱走路適性": st.column_config.NumberColumn(format="%.1f%%"),
                    "展開学習補正": st.column_config.NumberColumn(format="%+.3f"),
                    "熱走路学習補正": st.column_config.NumberColumn(format="%+.3f"),
                    "Ver60総合補正": st.column_config.NumberColumn(format="%+.3f"),
                })
                temp_now = meta.get("走路温度") or "未取得"
                st.caption(f"今回の走路温度：{temp_now}℃。47℃以上を細分化し、50℃以降は前残り・位置維持・選手別高温実績の影響を非線形に強めています。")
                st.caption("周回履歴が少ない選手は補正を自動で縮小します。予測対象日以降の結果は学習に使いません。")

            with st.expander("🌤️ 天候・複合条件適性", expanded=True):
                weather_cols = [c for c in [
                    "車", "選手名", "今回天候", "天候条件キー", "天候一致最大件数",
                    "天候適性信頼度", "天候適性補正", "天候適性根拠"
                ] if c in df.columns]
                if weather_cols:
                    weather_view = df[weather_cols].sort_values(["天候適性補正", "車"], ascending=[False, True]).reset_index(drop=True)
                    if "天候適性信頼度" in weather_view.columns:
                        weather_view["天候適性信頼度"] = pd.to_numeric(weather_view["天候適性信頼度"], errors="coerce") * 100.0
                    st.dataframe(weather_view, use_container_width=True, hide_index=True, column_config={
                        "天候適性信頼度": st.column_config.NumberColumn(format="%.1f%%"),
                        "天候適性補正": st.column_config.NumberColumn(format="%+.3f"),
                    })
                else:
                    st.info("天候データを取得できなかったため、今回は天候補正を行っていません。")
                st.caption("天候単独に加え、天候×走路状態×走路温度帯×時間帯を選手別に学習します。複合条件は十分な履歴件数がある場合だけ反映します。")

            st.markdown('<div id="prediction-summary"></div>', unsafe_allow_html=True)
            v73_section_nav()
            st.subheader("6周の代表展開")
            lap_df = engine.v30_representative_lap_projection(df)
            st.dataframe(lap_df, use_container_width=True, hide_index=True)
            st.caption("確率計算は全試行で、スタート・中盤・最終周のイベントを生成しています。この表は指標から作った代表的な1展開です。")
            st.caption(f"予測保存キー: {race_key}（結果登録時の比較・重み調整に使用）")

            with st.expander("🧪 学習重みによる順位・確率の変化", expanded=False):
                try:
                    v190_profile = v202_cached_weight_validation_profile(str(engine.DB_PATH), Path(engine.DB_PATH).stat().st_mtime)
                    st.caption(
                        f"重み検証ゲート：現在重みの{float(v190_profile.get('blend',1.0))*100:.0f}%を適用 "
                        f"（比較{int(v190_profile.get('race_count',0))}R／直近検証{int(v190_profile.get('validation_count',0))}R）"
                    )
                    st.caption(str(v190_profile.get("reason", "")))
                except Exception as exc:
                    st.caption(f"重み検証ゲートの表示を取得できませんでした: {exc}")
                impact_df = engine.v50_weight_impact_summary(df)
                st.dataframe(
                    impact_df, use_container_width=True, hide_index=True,
                    column_config={
                        "推定1着率_調整前": st.column_config.NumberColumn(format="%.2f%%"),
                        "推定1着率_調整後": st.column_config.NumberColumn(format="%.2f%%"),
                        "1着率変化": st.column_config.NumberColumn(format="%+.2f%%"),
                        "推定3着内率_調整前": st.column_config.NumberColumn(format="%.2f%%"),
                        "推定3着内率_調整後": st.column_config.NumberColumn(format="%.2f%%"),
                        "3着内率変化": st.column_config.NumberColumn(format="%+.2f%%"),
                    },
                )
                st.caption("コメントは現在の重み・各選手の特徴・順位変化から毎回作り直します。確率差は重みの影響だけを見る診断用近似で、下の本シミュレーション確率とは別です。")

                st.markdown("#### 三連単で確率が上がった組み合わせ")
                trifecta_impact = engine.v50_trifecta_weight_impact(df, limit=20)
                st.dataframe(
                    trifecta_impact, use_container_width=True, hide_index=True,
                    column_config={
                        "調整前確率": st.column_config.NumberColumn(format="%.3f%%"),
                        "調整後確率": st.column_config.NumberColumn(format="%.3f%%"),
                        "確率変化": st.column_config.NumberColumn(format="%+.3f%%"),
                    },
                )
                st.caption("上昇幅順位は、保存済み学習重みを適用したことで三連単確率がどれだけ増えたかの順位です。")
            show_prediction_confidence(finish_prob, bets, view_trials)
            show_v180_trifecta_tight_recommendation(bets, view_trials, meta)

            st.markdown('<div id="finish-probability"></div>', unsafe_allow_html=True)
            st.subheader("着順確率")
            st.dataframe(
                finish_prob,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "1着率": st.column_config.NumberColumn("1着率", format="%.2f%%"),
                    "2着率": st.column_config.NumberColumn("2着率", format="%.2f%%"),
                    "3着率": st.column_config.NumberColumn("3着率", format="%.2f%%"),
                    "3着内率": st.column_config.NumberColumn("3着内率", format="%.2f%%"),
                },
            )

            scenario_probs = engine.v27_scenario_probabilities(meta.get("走路温度", 30.0))
            scenario_df = pd.DataFrame([{"展開": name, "確率": prob * 100} for name, prob in sorted(scenario_probs.items(), key=lambda x: x[1], reverse=True)])
            st.subheader("展開予想")
            st.dataframe(scenario_df, use_container_width=True, hide_index=True, column_config={"確率": st.column_config.NumberColumn("確率", format="%.1f%%")})
            top_scenario = scenario_df.iloc[0]["展開"] if not scenario_df.empty else "不明"
            st.caption(f"中心展開：{top_scenario}。Ver15.2のシミュレーションで使う4展開の事前確率です。")

            st.subheader("今回条件の反映状況")
            condition_rows = [
                {"条件": "天候", "入力値": meta.get("天候") or "未取得", "反映": "直接反映（選手別の天候・複合条件適性）"},
                {"条件": "走路状態", "入力値": meta.get("走路状態") or "未取得", "反映": "直接反映（履歴の走路適合重み）"},
                {"条件": "走路温度", "入力値": f"{meta.get('走路温度')}℃" if pd.notna(meta.get("走路温度")) else "未取得", "反映": "直接反映（6周展開・変動幅・天候との複合適性）"},
                {"条件": "気温", "入力値": f"{meta.get('気温')}℃" if pd.notna(meta.get("気温")) else "未取得", "反映": "取得・保存・類似レース検索（直接補正は未実装）"},
                {"条件": "湿度", "入力値": f"{meta.get('湿度')}%" if pd.notna(meta.get("湿度")) else "未取得", "反映": "取得・保存・類似レース検索（直接補正は未実装）"},
            ]
            st.dataframe(pd.DataFrame(condition_rows), use_container_width=True, hide_index=True)

            odds_namespace = re.sub(r"[^0-9A-Za-z_-]+", "_", str(race_key))[-80:] or "current"
            st.markdown('<div id="ticket-probability"></div>', unsafe_allow_html=True)
            st.subheader("券種別確率・オッズ比較")
            ticket_options = {
                "2連単": ("2車単", "2tansho", False),
                "2連複": ("2車複", "2fuku", True),
                "3連複": ("三連複", "3fuku", True),
                "3連単": ("三連単", "3tan", False),
            }
            selected_ticket = st.radio(
                "表示する券種", list(ticket_options), horizontal=True,
                key=f"v155_ticket_view_{odds_namespace}",
                help="選択した券種だけを集計・表示します。ほかの券種は切り替えた時に計算します。",
            )
            ticket_key, odds_key, unordered = ticket_options[selected_ticket]
            show_ticket_table(selected_ticket, bets, ticket_key, view_trials, 20)
            show_odds_comparison(
                selected_ticket, bets, ticket_key, view_trials, odds_key,
                unordered=unordered, namespace=odds_namespace, show_bulk=False,
            )
            trifecta_odds = st.session_state.get(f"saved_odds_{odds_namespace}_3tan", {})
            show_v182_odds_adjusted_tight_recommendation(
                bets, view_trials, meta, trifecta_odds
            )
            show_v67_self_evaluation(meta)

            v73_section_nav()

            if Path(output).exists():
                st.download_button(
                    "予測結果Excelを保存",
                    Path(output).read_bytes(),
                    file_name=Path(output).name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
        except Exception as exc:
            st.error(f"保存済み予測の表示エラー: {type(exc).__name__}: {exc}")
            st.exception(exc)

if selected_main_page == "✅ 結果登録・解析":
    st.subheader("公式結果を登録して予測と比較")
    _show_sticky_notice("result_register_notice")
    st.info("結果ページを先頭のレース番号から払戻金まで全文コピーして貼り付けます。縦型の着順表、6周のグランドノート、払戻金にも対応します。")
    st.session_state.setdefault("result_input_version", 0)

    saved_results = _v233_list_saved_results(engine.DB_PATH, 250)
    if saved_results:
        st.markdown("#### ♻️ 保存済み結果を呼び出す")
        result_options = {
            0: "選択してください",
            **{
                i + 1: f"{row.get('race_date','')}｜{row.get('venue','')} {row.get('race_no','')}R｜保存 {str(row.get('updated_at') or '')[:19]}"
                for i, row in enumerate(saved_results)
            },
        }
        selected_saved_result = st.selectbox(
            "登録済み結果",
            list(result_options.keys()),
            format_func=lambda idx: result_options[idx],
            key="v233_saved_result_selector",
        )
        if st.button(
            "📥 選択した結果を入力欄へ復元",
            use_container_width=True,
            disabled=not bool(selected_saved_result),
            key="v233_restore_saved_result",
        ):
            row = saved_results[int(selected_saved_result) - 1]
            race_key_restore = str(row.get("race_key") or "")
            restored_text = _v238_load_exact_raw_result(engine.DB_PATH, race_key_restore)
            if restored_text:
                st.session_state["v163_saved_result_text"] = restored_text
                st.session_state["v163_saved_result_venue"] = str(row.get("venue") or "")
                st.session_state["v163_saved_result_race_no"] = str(row.get("race_no") or "")
                st.session_state["v238_result_restore_source"] = "exact_archive"
                st.session_state["result_input_version"] = int(st.session_state.get("result_input_version", 0)) + 1
                for key in ["v35_result_meta","v35_result_rows","v35_result_laps","v35_result_payouts"]:
                    st.session_state.pop(key, None)
                st.session_state["result_reset_notice"] = f"元の結果本文を復元しました：{race_key_restore}。内容を確認して置き換えできます。"
                st.rerun()
            else:
                st.session_state["v238_structured_preview_race_key"] = race_key_restore
                st.warning(
                    "この旧結果には、貼り付け時の元本文が保存されていません。"
                    " 不完全な再構成本文による誤上書きを防ぐため、編集欄には復元しません。"
                    " 公式結果の原文を貼り付けてください。"
                )
        st.caption("Ver238以降は元の結果本文を専用保管します。元本文がない旧結果は安全のため編集欄へ再構成復元しません。")

    def _reset_result_input_only():
        """結果入力関連だけを初期化し、DB・予測・学習キャッシュは維持する。"""
        st.session_state["result_input_version"] = int(st.session_state.get("result_input_version", 0)) + 1
        result_only_keys = [
            "v35_result_meta",
            "v35_result_rows",
            "v35_result_laps",
            "v35_result_payouts",
            "result_register_notice",
            "result_register_progress",
            "result_register_stage",
            "result_entry_count_check",
            "result_replace_confirmed",
        ]
        for key in result_only_keys:
            st.session_state.pop(key, None)
        _v163_clear_saved_inputs(
            "v163_saved_result_text", "v163_saved_result_venue", "v163_saved_result_race_no"
        )
        st.session_state.pop("v238_result_restore_source", None)
        st.session_state.pop("v238_structured_preview_race_key", None)
        st.session_state["result_reset_notice"] = "結果入力だけをリセットしました。予測結果・DBキャッシュ・重み設定は維持しています。"

    st.button(
        "🗑️ 結果入力をリセット",
        use_container_width=True,
        key="reset_result_input",
        on_click=_reset_result_input_only,
    )
    if st.session_state.get("result_reset_notice"):
        st.success(st.session_state.pop("result_reset_notice"))
    result_version = st.session_state["result_input_version"]
    result_text_key = f"official_result_text_{result_version}"
    _v163_restore_input(result_text_key, "v163_saved_result_text", "")
    result_text = st.text_area(
        "公式結果ページを全文貼り付け",
        height=620,
        key=result_text_key,
        placeholder="6R\n確定\n2026年7月21日(火)\n…\n着順 車番 選手名\n…\nグランドノート\n…\n払戻金\n…",
        on_change=_v163_save_input,
        args=(result_text_key, "v163_saved_result_text"),
    )

    if result_text and "v238_result_restore_source" not in st.session_state:
        st.session_state["v238_result_restore_source"] = "manual"
    detected_result_venue = _detect_result_venue_from_title(result_text)
    c1, c2 = st.columns(2)
    if detected_result_venue:
        c1.success(f"開催場をタイトルから自動判定：{detected_result_venue}")
        venue_override = detected_result_venue
    else:
        result_venue_key = f"result_venue_select_{result_version}"
        _v163_restore_input(result_venue_key, "v163_saved_result_venue", "")
        venue_override = c1.selectbox(
            "開催場（タイトルから判定できないため選択してください）",
            [""] + RESULT_VENUES,
            key=result_venue_key,
            format_func=lambda value: "選択してください" if value == "" else value,
            on_change=_v163_save_input,
            args=(result_venue_key, "v163_saved_result_venue"),
        )
        c1.caption("選手の所属LGは開催場判定に使用しません。")
    result_race_no_key = f"result_race_no_{result_version}"
    _v163_restore_input(result_race_no_key, "v163_saved_result_race_no", "")
    race_no_override = c2.text_input(
        "レース番号（本文から取れない場合のみ）",
        key=result_race_no_key,
        on_change=_v163_save_input,
        args=(result_race_no_key, "v163_saved_result_race_no"),
    )

    if st.button("結果を解析", use_container_width=True):
        if not venue_override:
            st.warning("開催場を選択してください。")
        else:
            try:
                meta_r, rows_r, laps_r, payouts_r = engine.v35_parse_result_text(
                    result_text, venue_override, race_no_override
                )
                meta_r, rows_r, nonstarter_numbers = _v224_restore_nonstarter_rows(
                    result_text, meta_r, rows_r
                )
                meta_r, poststart_incidents = _v227_detect_poststart_incidents(result_text, meta_r)
                st.session_state["v224_nonstarter_numbers"] = nonstarter_numbers
                st.session_state["v227_poststart_incidents"] = poststart_incidents
                st.session_state["v35_result_meta"] = meta_r
                st.session_state["v35_result_rows"] = rows_r
                st.session_state["v35_result_laps"] = laps_r
                st.session_state["v35_result_payouts"] = payouts_r
            except Exception as exc:
                st.error(f"結果解析エラー: {exc}")

    meta_r = st.session_state.get("v35_result_meta")
    rows_r = st.session_state.get("v35_result_rows")
    laps_r = st.session_state.get("v35_result_laps")
    payouts_r = st.session_state.get("v35_result_payouts")

    no_contest_r = bool(isinstance(meta_r, dict) and meta_r.get("レース状態") == "不成立")

    if no_contest_r:
        st.write("解析したレース情報", meta_r)
        st.error("🚫 レース不成立・全返還")
        st.info("着順・競走タイム・STは登録せず、選手履歴・予測評価・重み学習の対象外として保存します。")
        if isinstance(payouts_r, pd.DataFrame) and not payouts_r.empty:
            st.subheader("全返還")
            st.dataframe(payouts_r, use_container_width=True, hide_index=True)

        result_exists = False
        existing_result_key = ""
        existing_registered_at = None
        try:
            result_exists, existing_result_key, existing_registered_at = engine.v41_race_exists(meta_r, engine.DB_PATH)
        except Exception:
            pass
        replace_registered = False
        if result_exists:
            st.warning(f"このレースは登録済みです：{existing_result_key}（{existing_registered_at or '登録日時不明'}）")
            replace_registered = st.checkbox(
                "登録済み内容を、不成立・全返還へ置き換える",
                key=f"replace_no_contest_{existing_result_key}",
            )
        label = "登録済み結果を不成立へ置き換える" if replace_registered else "不成立・全返還としてDBへ登録"
        if st.button(label, type="primary", use_container_width=True, disabled=bool(result_exists and not replace_registered), key="register_no_contest"):
            try:
                key, registration = engine.v162_register_no_contest(
                    meta_r, payouts_r, engine.DB_PATH, replace=replace_registered
                )
                if registration.get("duplicate"):
                    st.warning(registration.get("message"))
                else:
                    msg = f"不成立・全返還として登録しました: {key}"
                    _set_sticky_notice("result_register_notice", "success", msg)
                    st.success(msg)
                    st.warning("AI学習対象外です。着順・選手履歴・追い抜き相性・レース種別適性・重みは更新していません。")
                    try:
                        ok, push_msg = push_db_to_github(f"AutoRaceAI: {key} 不成立・全返還登録")
                        (st.success if ok else st.warning)(push_msg)
                    except Exception as push_exc:
                        st.warning(f"DB保存後のGitHub反映に失敗しました: {push_exc}")
            except Exception as exc:
                st.error(f"不成立登録エラー: {type(exc).__name__}: {exc}")

    elif isinstance(rows_r, pd.DataFrame) and not rows_r.empty:
        st.write("解析したレース情報", meta_r)
        nonstarter_numbers = st.session_state.get("v224_nonstarter_numbers") or []
        poststart_incidents = st.session_state.get("v227_poststart_incidents") or []
        if nonstarter_numbers:
            cars_text = "・".join(f"{int(x)}番" for x in nonstarter_numbers)
            st.info(
                f"{cars_text}の欠車・発走前除外を検出しました。"
                f" 事故レースにはせず、実着順・学習・的中判定ではその車だけ比較対象外にします。"
            )
        if poststart_incidents:
            detail = " / ".join(f"{int(x.get('車番'))}番 {x.get('理由')}" for x in poststart_incidents)
            st.warning(
                f"発走後の事故・反則を検出しました：{detail}。"
                "このレースは予測精度評価・選手履歴学習・展開学習・重み更新の対象外です。"
                "結果、払戻金、実際の回収率判定は保存します。"
            )
        st.subheader("着順・タイム")
        st.dataframe(rows_r, use_container_width=True, hide_index=True)

        if isinstance(laps_r, pd.DataFrame) and not laps_r.empty:
            st.subheader("6周グランドノート")
            lap_table = laps_r.pivot(index="周回", columns="順位", values="車番")
            order = [f"{i}周目" for i in range(1, 7)] + ["ゴール線"]
            lap_table = lap_table.reindex([x for x in order if x in lap_table.index])
            st.dataframe(lap_table, use_container_width=True)
        else:
            st.caption("グランドノートは見つかりませんでした。着順結果だけでも登録できます。")

        if isinstance(payouts_r, pd.DataFrame) and not payouts_r.empty:
            st.subheader("払戻金")
            st.dataframe(payouts_r, use_container_width=True, hide_index=True)
        else:
            st.caption("払戻金は見つかりませんでした。")

        result_exists = False
        existing_result_key = ""
        existing_registered_at = None
        try:
            result_exists, existing_result_key, existing_registered_at = engine.v41_race_exists(meta_r, engine.DB_PATH)
        except Exception:
            pass

        replace_registered = False
        if result_exists:
            st.warning(f"このレースは登録済みです：{existing_result_key}（{existing_registered_at or '登録日時不明'}）")
            replace_registered = st.checkbox(
                "登録済みの結果を、今回の内容で置き換える",
                key=f"replace_result_{existing_result_key}",
                help="着順・タイム・払戻金・グランドノート・自動追加された選手履歴を置き換えます。予測スナップショットは残します。",
            )
            if replace_registered:
                st.info("再登録では、古い結果データを削除してから今回の内容を登録し直します。")

        safety_errors, safety_warnings = _v238_result_safety_check(
            engine.DB_PATH, existing_result_key if result_exists else "", rows_r
        )
        for msg in safety_warnings:
            st.warning(f"上書き確認：{msg}")
        if safety_errors:
            st.error("安全チェックで登録を停止しました。")
            for msg in safety_errors:
                st.caption(f"・{msg}")
        button_label = "登録済み結果を置き換えて再解析" if replace_registered else "DBへ登録して予測差・展開を解析"
        button_disabled = bool((result_exists and not replace_registered) or safety_errors)
        if st.button(button_label, type="primary", use_container_width=True, disabled=button_disabled):
            try:
                # 解析後のsession_state復元や再登録でも、発走後事故の学習遮断フラグを再適用する。
                meta_r, poststart_incidents = _v227_detect_poststart_incidents(result_text, dict(meta_r or {}))
                st.session_state["v35_result_meta"] = meta_r
                st.session_state["v227_poststart_incidents"] = poststart_incidents
                with st.spinner("① SQLiteへ保存 → ② 予測差・展開を解析しています…"):
                    if replace_registered:
                        key, comparison, analysis, adjustment, registration = engine.v70_replace_registered_result(
                            meta_r, rows_r, laps_r, payouts_r, engine.DB_PATH
                        )
                    else:
                        key, comparison, analysis, adjustment, registration = engine.v41_register_result(
                            meta_r, rows_r, laps_r, payouts_r, engine.DB_PATH
                        )
                    ticket_analysis = engine.v67_analyze_ticket_result(meta_r, rows_r, engine.DB_PATH)
                    # Ver255管理修正: 予測時に保存済みのプランだけを、結果登録後に払戻と照合する。
                    # 復元表示だけでは新規保存せず、結果登録時点で元バージョンのまま回収率へ反映する。
                    _v187_sync_mixed_feedback(engine.DB_PATH)
                    mixed_plan_result = _v208_latest_mixed_plan_result(key, engine.DB_PATH)
                if registration.get("duplicate"):
                    duplicate_message = analysis.get("message", "このレースは登録済みです。")
                    _set_sticky_notice("result_register_notice", "warning", duplicate_message)
                    st.warning(duplicate_message)
                else:
                    if registration.get("replaced"):
                        result_message = f"登録済み結果を置き換えました: {key}"
                    else:
                        result_message = f"結果を登録しました: {key}"
                    _set_sticky_notice("result_register_notice", "success", result_message)
                    st.success(result_message)
                    if isinstance(mixed_plan_result, dict) and mixed_plan_result.get("available"):
                        if mixed_plan_result.get("evaluated"):
                            st.success(
                                "予測時に保存した買い目を、保存時のバージョンのまま払戻金と照合し、"
                                f"回収率へ反映しました（実回収率 {float(mixed_plan_result.get('return_rate') or 0.0):.1f}%）。"
                            )
                        else:
                            st.info(
                                "予測履歴は保存済みですが、払戻金が未登録または照合できないため、"
                                "回収率はまだ未確定です。払戻金を含めて結果登録すると反映されます。"
                            )
                    else:
                        st.caption(
                            "このレースには予測時に保存された回収率プランがないため、"
                            "結果だけを登録しました。復元表示だけでは新しいプランは作成しません。"
                        )
                    if registration.get("learning_excluded") or meta_r.get("学習対象外"):
                        exclusion_reason = registration.get('learning_exclusion_reason') or meta_r.get('learning_exclusion_reason') or '事故・異常終了'
                        st.warning(
                            "⚠️ 発走後事故のためAI学習対象外です。"
                            f" 理由: {exclusion_reason}。"
                            "結果・払戻金・グランドノートだけ保存し、選手履歴・展開・追い抜き相性・開催場・壁補正・重み更新には使いません。"
                        )
                        st.info(
                            "学習監査｜選手履歴: 停止 / 展開・追い抜き: 停止 / "
                            "開催場・壁補正: 停止 / 重み更新: 停止"
                        )
                    show_v67_result_analysis(ticket_analysis)
                    predicted_trifecta_saved = ""
                    actual_trifecta_saved = ""
                    if "message" not in analysis:
                        predicted_trifecta_saved = "→".join(map(str, comparison.sort_values("predicted_rank")["車番"].head(3).astype(int)))
                        actual_trifecta_saved = "→".join(map(str, rows_r.sort_values("着順")["車番"].head(3).astype(int)))
                    try:
                        source_kind = str(st.session_state.get("v238_result_restore_source") or "manual")
                        _v238_save_exact_raw_result(engine.DB_PATH, key, str(result_text or ""), source_kind)
                    except Exception as raw_exc:
                        st.warning(f"元の結果本文の保管に失敗しました：{type(raw_exc).__name__}")
                    result_view_payload = {
                        "key": key,
                        "comparison": comparison,
                        "analysis": analysis,
                        "adjustment": adjustment,
                        "predicted_trifecta": predicted_trifecta_saved,
                        "actual_trifecta": actual_trifecta_saved,
                        "mixed_plan_result": mixed_plan_result,
                        "raw_result_text": str(result_text or ""),
                        "raw_result_source": str(st.session_state.get("v238_result_restore_source") or "manual"),
                        "result_meta": dict(meta_r or {}),
                        "result_rows": rows_r.copy() if isinstance(rows_r, pd.DataFrame) else rows_r,
                        "result_laps": laps_r.copy() if isinstance(laps_r, pd.DataFrame) else laps_r,
                        "result_payouts": payouts_r.copy() if isinstance(payouts_r, pd.DataFrame) else payouts_r,
                        "learning_excluded": bool(registration.get("learning_excluded") or meta_r.get("学習対象外")),
                        "learning_exclusion_reason": registration.get("learning_exclusion_reason") or meta_r.get("learning_exclusion_reason"),
                        "learning_audit": {
                            "選手履歴": "停止" if meta_r.get("学習対象外") else "実行",
                            "展開・追い抜き": "停止" if meta_r.get("学習対象外") else "実行",
                            "開催場・壁補正": "停止" if meta_r.get("学習対象外") else "実行",
                            "重み更新": "停止" if meta_r.get("学習対象外") else "実行",
                            "保存時刻": _v228_now_jst_iso(),
                        },
                    }
                    st.session_state["v41_last_result_view"] = result_view_payload
                    try:
                        _v223_save_result_view(engine.DB_PATH, key, result_view_payload)
                    except Exception:
                        pass
                    _v208_render_mixed_plan_result(mixed_plan_result)
                    if registration.get("learning_excluded") or meta_r.get("学習対象外"):
                        st.info(
                            "このレースの予測順位誤差・TOP3一致・三連単完全一致は成績集計へ加えません。"
                            "発走後の出来事で着順が変わった可能性があるためです。"
                        )
                    elif "message" in analysis:
                        st.warning(analysis["message"])
                    else:
                        a, b, c = st.columns(3)
                        a.metric("平均順位誤差", analysis["平均順位誤差"])
                        b.metric("1着的中", "○" if analysis["1着的中"] else "×")
                        c.metric("予測TOP3一致", f"{analysis['3着内一致数']}/3")
                        show_cols = [x for x in ["着順", "車番", "選手名_x", "predicted_rank", "順位誤差", "win_prob", "top3_prob"] if x in comparison.columns]
                        st.dataframe(comparison[show_cols], use_container_width=True, hide_index=True)

                    lap_items = []
                    for label in ["1周目先頭", "ゴール先頭", "先頭交代回数", "最大順位上昇車", "最大順位上昇"]:
                        if label in analysis:
                            lap_items.append(f"{label}: {analysis[label]}")
                    if lap_items:
                        st.info("展開解析｜" + " / ".join(lap_items))

                    # 三連単は上位3車の順番が完全一致した場合だけ的中。
                    if not (registration.get("learning_excluded") or meta_r.get("学習対象外")) and "message" not in analysis:
                        pred_trifecta = "→".join(map(str, comparison.sort_values("predicted_rank")["車番"].head(3).astype(int)))
                        actual_trifecta = "→".join(map(str, rows_r.sort_values("着順")["車番"].head(3).astype(int)))
                        exact_hit = pred_trifecta == actual_trifecta
                        st.subheader("三連単の完全一致判定")
                        t1, t2, t3 = st.columns(3)
                        t1.metric("予測", pred_trifecta)
                        t2.metric("実結果", actual_trifecta)
                        t3.metric("三連単的中", "○" if exact_hit else "×")
                        st.caption("1着だけ、TOP3の車が同じだけでは三連単的中にしません。順番まで完全一致のみ○です。")

                    st.subheader("結果による重みの微調整")
                    # v4.1登録処理内で、全履歴・直近重視の重み更新まで完了済み。
                    if "before" in adjustment:
                        weight_rows=[]
                        for name in adjustment["before"]:
                            weight_rows.append({"項目":name,"調整前":adjustment["before"][name],"調整後":adjustment["after"][name],
                                                "変化":adjustment["after"][name]-adjustment["before"][name],
                                                "今回結果との相関":adjustment["evidence"][name]})
                        st.dataframe(pd.DataFrame(weight_rows), use_container_width=True, hide_index=True,
                            column_config={"調整前":st.column_config.NumberColumn(format="%.4f"),"調整後":st.column_config.NumberColumn(format="%.4f"),
                                           "変化":st.column_config.NumberColumn(format="%+.4f"),"今回結果との相関":st.column_config.NumberColumn(format="%+.3f")})
                        q1,q2,q3=st.columns(3)
                        q1.metric("調整前の上位3車",adjustment["before_top3"])
                        q2.metric("調整後の診断",adjustment["after_top3"])
                        q3.metric("実結果",adjustment["actual_top3"])
                        stats = adjustment.get("learning_stats", {})
                        st.info(adjustment["note"])
                        if stats:
                            st.caption(f"学習対象: 全{stats.get('race_count',0)}レース / 直近{stats.get('recent_count',0)}レースを中心 / 最新レース寄与 約{stats.get('latest_contribution',0)*100:.1f}%")
                    else:
                        st.info(adjustment.get("message","重みは変更していません。"))

                    st.subheader("選手履歴の更新結果")
                    if registration.get("learning_excluded") or meta_r.get("学習対象外"):
                        st.caption("発走後事故レースのため、選手履歴・展開・重みは更新していません。")
                    h1, h2, h3 = st.columns(3)
                    h1.metric("新規履歴", analysis.get("履歴追加", 0))
                    h2.metric("重複スキップ", analysis.get("履歴重複スキップ", 0))
                    h3.metric("周回順位", analysis.get("周回履歴保存", 0))
                    st.caption("結果登録した競走T・試走T・ST・着順・ハンデ・走路条件は、次回以降の予測用選手履歴へ反映されます。")
                    if registration.get("race_context_updated"):
                        ctx_stats = registration.get("race_context_refresh", {})
                        ov_stats = registration.get("overtake_matchups_refresh", {})
                        st.success(
                            f"追加分析も更新しました｜レース種別適性 {ctx_stats.get('profiles', 0)}件 / "
                            f"追い抜き相性 {ov_stats.get('pairs', 0)}組"
                        )
                    elif registration.get("post_analysis_error"):
                        st.warning("結果は登録しましたが、追加適性の更新でエラー: " + str(registration.get("post_analysis_error")))
                    st.caption("同一判定は開催日・開催場・レース番号で行います。レース名称は判定に使いません。同じレースは通常登録では重複を防止します。再登録を選んだ場合だけ、古い結果を今回の内容へ置き換えます。")
                    st.caption(f"順位分析対象: {analysis.get('分析対象', 0)}名 / 除外: {analysis.get('分析除外', 0)}名。着順なし・欠車・中止・失格などは順位分析から除外します。")
                    with st.spinner("③ GitHubへDBを保存しています…"):
                        ok, msg = push_db_to_github(f"AutoRaceAI: {key} 結果・周回・払戻登録")
                    full_message = result_message + (f"｜{msg}" if msg else "")
                    _set_sticky_notice("result_register_notice", "success" if ok else "warning", full_message)
                    (st.success if ok else st.warning)(msg)
            except Exception as exc:
                error_message = f"結果登録エラー: {type(exc).__name__}: {exc}"
                _set_sticky_notice("result_register_notice", "error", error_message)
                st.error(error_message)
                st.exception(exc)

    last_result_view = st.session_state.get("v41_last_result_view")
    if not last_result_view:
        last_result_view = _v223_load_latest_result_view(engine.DB_PATH)
        if last_result_view:
            st.session_state["v41_last_result_view"] = last_result_view
    if last_result_view:
        st.divider()
        st.caption("直前の結果解析を保持しています。結果入力をリセットしても消えません。")
        render_last_result_analysis(last_result_view)


if selected_main_page == "🗃️ 登録情報確認":
    st.subheader("全結果バックテスト・重み最適化")
    st.caption("単発レースの結果だけでなく、予測時に保存した特徴と登録済み結果をまとめて比較します。古い約70%で候補を探し、新しい約30%でも悪化しない候補だけを提案します。")
    candidate_count = st.slider("試す重み候補数", 200, 3000, 800, 100, key="v74_candidate_count")
    if st.button("🧠 全結果から重みを最適化", use_container_width=True, key="v74_optimize"):
        with st.spinner("登録済みレースをバックテスト中です…"):
            st.session_state["v74_optimization"] = engine.v74_optimize_weights(engine.DB_PATH, candidate_count)
    opt = st.session_state.get("v74_optimization")
    if opt:
        if not opt.get("ok"):
            st.warning(opt.get("message", "最適化できませんでした。"))
        else:
            c1,c2,c3=st.columns(3)
            c1.metric("全レース", opt["race_count"])
            c2.metric("探索用", opt["train_count"])
            c3.metric("検証用", opt["validation_count"])
            st.markdown("#### 重みの提案")
            st.dataframe(opt["weights"], use_container_width=True, hide_index=True, column_config={
                "現在":st.column_config.NumberColumn(format="%.4f"),
                "提案":st.column_config.NumberColumn(format="%.4f"),
                "変化":st.column_config.NumberColumn(format="%+.4f"),
            })
            st.markdown("#### バックテスト比較")
            st.dataframe(opt["comparison"], use_container_width=True, hide_index=True, column_config={
                "現在":st.column_config.NumberColumn(format="%.3f"),
                "提案":st.column_config.NumberColumn(format="%.3f"),
                "改善方向の差":st.column_config.NumberColumn(format="%+.3f"),
            })
            valid_before=opt["before_validation"].get("objective",0) or 0
            valid_after=opt["proposed_validation"].get("objective",0) or 0
            if valid_after > valid_before + 1e-6:
                st.success(f"新しい約30%の検証レースでも総合評価が {valid_before*100:.2f} → {valid_after*100:.2f} に改善しました。")
            else:
                st.info("検証レースで明確な改善候補が見つからなかったため、現在値に近い提案です。無理に重みを動かしません。")
            confirm_apply=st.checkbox("提案重みを適用する", key=f"v74_apply_confirm_{opt.get('optimization_id')}")
            if st.button("✅ 提案重みを適用", use_container_width=True, disabled=not confirm_apply, key=f"v74_apply_{opt.get('optimization_id')}"):
                ok,msg=engine.v74_apply_optimized_weights(opt["optimization_id"],engine.DB_PATH)
                (st.success if ok else st.warning)(msg)
                if ok:
                    st.session_state.pop("v74_optimization",None)
                    st.rerun()
    # Ver271安定化: 学習テーブルの読込失敗でアプリ全体を落とさない。
    # DB本体の予測・結果テーブルと、重み学習の補助テーブルは切り離して扱う。
    try:
        hist74=engine.v74_optimization_history(engine.DB_PATH,20)
        if not hist74.empty:
            with st.expander("過去の最適化履歴", expanded=False):
                st.dataframe(hist74,use_container_width=True,hide_index=True)
    except sqlite3.DatabaseError as exc:
        st.warning(
            "最適化履歴テーブルを読み込めませんでした。"
            "予測DB全体が壊れているとは限りません。"
            f"（{type(exc).__name__}: {exc}）"
        )
    except Exception as exc:
        st.warning(f"最適化履歴の読込をスキップしました: {type(exc).__name__}: {exc}")

    st.divider()
    st.subheader("学習重み・変更履歴")
    try:
        _v270_weights_df=engine.v40_current_weights(engine.DB_PATH)
        st.dataframe(_v270_weights_df, use_container_width=True, hide_index=True,
            column_config={"現在の重み":st.column_config.NumberColumn(format="%.4f"),"初期値":st.column_config.NumberColumn(format="%.4f"),"初期値からの差":st.column_config.NumberColumn(format="%+.4f")})
    except sqlite3.DatabaseError as exc:
        st.warning(
            "学習重みテーブルを読み込めないため、この表示だけスキップしました。"
            f"（{type(exc).__name__}: {exc}）"
        )
    except Exception as exc:
        st.warning(f"学習重み表示をスキップしました: {type(exc).__name__}: {exc}")

    try:
        history_df=engine.v39_weight_history(engine.DB_PATH,100)
        if history_df.empty:
            st.caption("重み変更履歴はまだありません。")
        else:
            st.dataframe(history_df,use_container_width=True,hide_index=True)
    except sqlite3.DatabaseError as exc:
        st.warning(
            "重み変更履歴テーブルを読み込めないため、この表示だけスキップしました。"
            f"（{type(exc).__name__}: {exc}）"
        )
    except Exception as exc:
        st.warning(f"重み変更履歴の表示をスキップしました: {type(exc).__name__}: {exc}")
    st.subheader("結果登録履歴・取り消し")
    reg_history = engine.v41_registration_history(engine.DB_PATH, 50)
    if reg_history.empty:
        st.caption("v4.1で登録した結果はまだありません。")
    else:
        st.dataframe(reg_history, use_container_width=True, hide_index=True)
        active_rows = reg_history[reg_history["状態"] == "登録中"] if "状態" in reg_history.columns else reg_history
        if not active_rows.empty:
            labels = active_rows["レースID"].astype(str).tolist()
            selected_key = st.selectbox("登録結果をもう一度見る", labels, key="registration_detail_key")
            if st.button("📖 選択した登録結果を開く", use_container_width=True):
                st.session_state["opened_registration_key"] = selected_key
        opened_key = st.session_state.get("opened_registration_key")
        if opened_key:
            detail = engine.v41_registration_detail(opened_key, engine.DB_PATH)
            st.markdown(f"### 登録結果詳細：{opened_key}")
            if not detail["race"].empty:
                st.dataframe(detail["race"], use_container_width=True, hide_index=True)
            st.subheader("着順・タイム")
            st.dataframe(detail["entries"], use_container_width=True, hide_index=True)
            if not detail["laps"].empty:
                st.subheader("周回順位")
                st.dataframe(detail["laps"], use_container_width=True, hide_index=True)
            if not detail["payouts"].empty:
                st.subheader("払戻金")
                st.dataframe(detail["payouts"], use_container_width=True, hide_index=True)
            if not detail["feedback"].empty:
                st.subheader("予測比較・解析保存内容")
                st.dataframe(detail["feedback"], use_container_width=True, hide_index=True)
    confirm_undo = st.checkbox("最後の結果登録を取り消すことを確認しました", key="confirm_v41_undo")
    if st.button("↩ 最後の結果登録を取り消す", use_container_width=True, disabled=not confirm_undo):
        ok,msg=engine.v41_undo_last_registration(engine.DB_PATH)
        if ok:
            push_ok, push_msg = push_db_to_github("AutoRaceAI: 最後の結果登録を取り消し")
            st.success(msg)
            (st.success if push_ok else st.warning)(push_msg)
        else:
            st.warning(msg)

def _v146_reset_player_input():
    st.session_state["player_input_version"] = int(st.session_state.get("player_input_version", 0)) + 1
    for key in ["parsed_player_history", "player_register_notice", "player_registration_lookup"]:
        st.session_state.pop(key, None)
    _v163_clear_saved_inputs("v163_saved_player_name", "v163_saved_player_history")
    st.session_state["player_register_notice"] = {"level":"success", "message":"選手入力だけをリセットしました。"}

if selected_main_page == "👤 選手情報登録":
    st.subheader("選手情報を登録")
    _show_sticky_notice("player_register_notice")
    st.session_state.setdefault("player_input_version", 0)
    st.button(
        "🗑️ 選手入力をリセット", use_container_width=True, key="reset_player_input",
        on_click=_v146_reset_player_input,
    )
    player_version = st.session_state["player_input_version"]
    player_name_key = f"player_name_input_{player_version}"
    _v163_restore_input(player_name_key, "v163_saved_player_name", "")
    player_name = st.text_input(
        "選手名", placeholder="例：横田翔", key=player_name_key,
        on_change=_v163_save_input,
        args=(player_name_key, "v163_saved_player_name"),
    )
    show_player_registration_status(player_name)
    player_history_key = f"player_history_text_{player_version}"
    _v163_restore_input(player_history_key, "v163_saved_player_history", "")
    history_text = st.text_area(
        "公式プロフィールの直近履歴を貼り付け",
        height=520,
        placeholder="前走\n4\n2026年7月21日\n伊勢崎\n予選\n…",
        key=player_history_key,
        on_change=_v163_save_input,
        args=(player_history_key, "v163_saved_player_history"),
    )

    if st.button("貼り付け内容を解析", use_container_width=True):
        if not player_name.strip() or not history_text.strip():
            st.warning("選手名と履歴を入力してください。")
        else:
            parsed = engine.v15_parse_player_history(history_text, player_name=player_name.strip())
            st.session_state["parsed_player_history"] = parsed

    parsed = st.session_state.get("parsed_player_history")
    if isinstance(parsed, pd.DataFrame) and not parsed.empty:
        st.success(f"{len(parsed)}件を解析しました。登録前に内容を確認してください。")
        preview_cols = [c for c in [
            "選手名", "開催日", "開催場", "レース", "レース名", "レース種別", "着順", "天候", "走路",
            "走路温度", "気温", "湿度", "車番", "ハンデ", "距離", "周回数",
            "人気", "競走T", "試走T", "ST"
        ] if c in parsed.columns]
        st.dataframe(parsed[preview_cols], use_container_width=True, hide_index=True, height=420)

        if st.button("DBへ登録してGitHubに保存", type="primary", use_container_width=True):
            try:
                with st.spinner("① SQLiteへ選手履歴を保存しています…"):
                    report = engine.v47_save_player_history(parsed, db_path=engine.DB_PATH)
                st.session_state["pending_player_history"] = report["pending"]
                changed, skipped, pending_count = report["changed"], report["skipped"], report["pending_count"]
                text = (
                    f"読込 {report['read']}件｜追加・更新 {changed}件｜"
                    f"重複処理 {skipped}件（数値完全一致 {report.get('exact_duplicate_skipped', 0)}件）｜"
                    f"保留 {pending_count}件"
                )
                if changed:
                    with st.spinner("② GitHubへDBを保存しています…"):
                        ok, msg = push_db_to_github(f"AutoRaceAI: {player_name.strip()} の履歴を{changed}件追加・更新")
                    full_text = text + (f"｜{msg}" if msg else "")
                    level = "success" if ok else "warning"
                elif pending_count:
                    full_text = text + "｜必須項目を補完すると不足行だけ登録できます。"
                    level = "warning"
                else:
                    full_text = text + "｜新規登録対象はありませんでした。"
                    level = "info"
                if report.get("race_context_updated"):
                    rs = report.get("race_context_refresh", {})
                    full_text += f"｜レース種別適性 {rs.get('profiles', 0)}件を再分析"
                elif report.get("race_context_refresh_error"):
                    full_text += "｜適性再分析エラー: " + str(report.get("race_context_refresh_error"))
                    level = "warning"
                _set_sticky_notice("player_register_notice", level, full_text)
                getattr(st, level, st.info)(full_text)
            except Exception as exc:
                error_message = f"登録エラー: {type(exc).__name__}: {exc}"
                _set_sticky_notice("player_register_notice", "error", error_message)
                st.error(error_message)
                st.exception(exc)

    pending_notice = st.session_state.pop("pending_save_notice", None)
    if isinstance(pending_notice, dict):
        level = pending_notice.get("level", "info")
        message = pending_notice.get("message", "")
        getattr(st, level, st.info)(message)

    pending = st.session_state.get("pending_player_history")
    if isinstance(pending, pd.DataFrame) and not pending.empty:
        st.markdown("### ⚠️ R・必須項目の入力待ち")
        st.caption("R候補は参考表示です。数値が完全一致していてRだけ違う場合は、「既存Rへ統合」または「入力したRで新規登録」を選択してください。Rを空欄のままにすると保留されます。")
        edit_cols = [c for c in ["選手名","開催日","開催場","レース","R候補","重複処理","レース名","着順","車番","走路","ハンデ","試走T","競走T","ST","保留理由"] if c in pending.columns]
        # 不足行の編集はフォーム内に固定する。
        # 文字入力や選択変更だけではアプリ全体を再実行せず、登録ボタンでまとめて送信する。
        with st.form(f"pending_history_form_{player_version}", clear_on_submit=False):
            edited = st.data_editor(
                pending[edit_cols], use_container_width=True, hide_index=True,
                key=f"pending_history_editor_{player_version}",
                disabled=[c for c in ["R候補", "保留理由"] if c in edit_cols],
                column_config={
                    "開催場": st.column_config.SelectboxColumn("開催場", options=["川口","伊勢崎","浜松","飯塚","山陽"]),
                    "レース": st.column_config.NumberColumn("R", min_value=1, max_value=12, step=1),
                    "重複処理": st.column_config.SelectboxColumn(
                        "重複処理",
                        options=["選択してください", "既存Rへ統合", "入力したRで新規登録"],
                    ),
                },
            )
            save_pending_submitted = st.form_submit_button(
                "不足行だけ登録", type="primary", use_container_width=True
            )

        if save_pending_submitted:
            import time as _time
            _started_at = _time.perf_counter()
            _stage = "入力内容の確認"
            try:
                with st.status("不足行の登録処理を開始しました", expanded=True) as pending_status:
                    pending_status.write(f"① 入力内容を確認しています：{len(edited)}件")
                    repaired = pending.copy()
                    for c in edited.columns:
                        if c != "保留理由":
                            repaired[c] = edited[c].values
                    repaired = repaired.drop(columns=["保留理由"], errors="ignore")

                    _stage = "重複候補とRの確認"
                    pending_status.write("② 重複候補とレース番号の選択を確認しています")
                    unresolved = pd.Series(False, index=repaired.index)
                    if "重複処理" in repaired.columns:
                        conflict_mask = pending.get(
                            "保留理由", pd.Series("", index=pending.index)
                        ).astype(str).str.contains("Rが異なります", na=False)
                        unresolved = conflict_mask & repaired["重複処理"].fillna(
                            "選択してください"
                        ).eq("選択してください")
                        repaired["_v58_duplicate_confirmed"] = ~unresolved

                    if unresolved.any():
                        message = f"Rが異なる重複候補 {int(unresolved.sum())}件の登録方法を選択してください。"
                        pending_status.update(label="入力待ちで停止しました", state="error", expanded=True)
                        pending_status.write("登録処理はまだSQLiteへ進んでいません。")
                        st.session_state["pending_save_notice"] = {
                            "level": "warning", "message": message
                        }
                        st.warning(message)
                    else:
                        _stage = "SQLiteへの保存"
                        pending_status.write("③ SQLiteへ不足行を保存しています")
                        report2 = engine.v131_save_pending_player_history(
                            repaired, db_path=engine.DB_PATH
                        )

                        _stage = "保存結果の再照合"
                        pending_status.write("④ SQLiteを再検索し、実際に保存された件数を確認しています")
                        st.session_state["pending_player_history"] = report2["pending"]
                        verified = int(report2.get("verified", 0))
                        pending_count = int(report2.get("pending_count", 0))
                        changed = int(report2.get("changed", report2.get("saved", verified)))
                        skipped = int(report2.get("skipped", 0))
                        pending_status.write(
                            f"確認結果：保存確認 {verified}件 / 変更候補 {changed}件 / "
                            f"重複・スキップ {skipped}件 / 残り保留 {pending_count}件"
                        )

                        if verified:
                            _stage = "GitHubへの保存"
                            pending_status.write("⑤ GitHubへ更新済みDBを保存しています")
                            ok, msg = push_db_to_github(
                                f"AutoRaceAI: {player_name.strip()} の保留履歴を{verified}件登録"
                            )
                            level = "success" if ok else "warning"
                            message = (
                                f"不足行をDBへ{verified}件登録し、保存後の確認も完了しました。"
                                f"残り保留 {pending_count}件。"
                                + (f" {msg}" if msg else "")
                            )
                            pending_status.write("⑥ 登録処理が完了しました")
                            pending_status.update(
                                label=f"不足行登録完了：{verified}件",
                                state="complete", expanded=True
                            )
                        elif pending_count:
                            level = "warning"
                            reasons = ""
                            if isinstance(report2.get("pending"), pd.DataFrame) and "保留理由" in report2["pending"].columns:
                                vals = report2["pending"]["保留理由"].dropna().astype(str).unique().tolist()[:3]
                                reasons = " 理由: " + " / ".join(vals) if vals else ""
                            message = f"DBへ登録できませんでした。残り保留 {pending_count}件。{reasons}"
                            pending_status.update(
                                label="保存できない行が残っています",
                                state="error", expanded=True
                            )
                            pending_status.write(message)
                        else:
                            level = "info"
                            message = "登録対象の変更はありませんでした。既に同じ履歴が登録されている可能性があります。"
                            pending_status.update(
                                label="新しい登録対象はありませんでした",
                                state="complete", expanded=True
                            )
                            pending_status.write(message)

                        elapsed = _time.perf_counter() - _started_at
                        pending_status.write(f"処理時間：{elapsed:.1f}秒")
                        st.session_state["pending_save_notice"] = {
                            "level": level,
                            "message": message + f"（処理時間 {elapsed:.1f}秒）"
                        }
                        st.rerun()
            except Exception as exc:
                elapsed = _time.perf_counter() - _started_at
                error_message = (
                    f"不足行登録エラー（{_stage}）: {type(exc).__name__}: {exc} "
                    f"（開始から {elapsed:.1f}秒）"
                )
                st.session_state["pending_save_notice"] = {
                    "level": "error", "message": error_message
                }
                st.error(error_message)
                st.exception(exc)

if selected_main_page == "🗃️ 登録情報確認":
    st.subheader("🏟️ 開催場別の学習重み")
    st.caption("第1層はレース番号なしでも全履歴を使用し、第2層だけ開催日・開催場・R単位で展開を学習します。")

    st.markdown("#### 過去分の一括再分析")
    st.caption("現在DBに登録されている全履歴を5開催場まとめて再計算し、分析日時と特徴値をDBへ保存します。結果登録や履歴追加後に実行してください。")
    if st.button("開催場特徴を過去分まとめて再分析", type="primary", use_container_width=True, key="v103_rebuild_venue_analysis"):
        with st.spinner("川口・伊勢崎・浜松・飯塚・山陽を再分析しています…"):
            rebuild = engine.v103_rebuild_all_venue_analysis(engine.DB_PATH)
        st.session_state["v103_last_venue_rebuild"] = rebuild
        if rebuild.get("失敗", 0):
            st.warning(f"一括分析完了：成功 {rebuild.get('成功', 0)}場｜失敗 {rebuild.get('失敗', 0)}場")
        else:
            st.success(f"一括分析完了：5開催場を {rebuild.get('分析日時', '')} に更新しました。")
        st.rerun()

    if st.button("保存済み開催場分析を表示", use_container_width=True, key="v138_load_saved_venue_analysis"):
        st.session_state["v138_show_saved_venue_analysis"] = True
    if st.session_state.get("v138_show_saved_venue_analysis"):
        try:
            cached_venue = _v138_cached_venue_analysis(engine.DB_PATH, _v138_db_token(engine.DB_PATH))
            if not cached_venue.empty:
                latest_time = _format_jst(cached_venue["分析日時"].max()) if "分析日時" in cached_venue.columns else ""
                st.info(f"保存済み一括分析：{latest_time}｜{len(cached_venue)}開催場")
                with st.expander("保存済みの開催場分析結果", expanded=False):
                    st.dataframe(_jst_datetime_columns(cached_venue), use_container_width=True, hide_index=True)
            else:
                st.info("保存済みの一括分析はまだありません。")
        except Exception as exc:
            st.warning(f"保存済み開催場分析を読み込めませんでした: {exc}")

    st.markdown("#### 開催場別重みの詳細")
    st.caption("この処理は重いため自動実行しません。必要なときだけ読み込んでください。")
    if st.button("開催場別重み一覧を読み込む", use_container_width=True, key="v132_load_venue_profiles"):
        with st.spinner("開催場別重みを読み込んでいます…"):
            try:
                st.session_state["v132_venue_profiles"] = engine.v92_all_venue_weight_profiles(engine.DB_PATH)
                st.session_state.pop("v132_venue_profile_error", None)
            except Exception as exc:
                st.session_state["v132_venue_profile_error"] = str(exc)

    if st.session_state.get("v132_venue_profile_error"):
        st.warning(f"開催場別重みを取得できませんでした: {st.session_state['v132_venue_profile_error']}")

    venue_profiles = st.session_state.get("v132_venue_profiles")
    if isinstance(venue_profiles, pd.DataFrame) and not venue_profiles.empty:
        st.dataframe(
            venue_profiles,
            use_container_width=True,
            hide_index=True,
            column_config={
                "全履歴反映率": st.column_config.ProgressColumn(format="%.1f%%", min_value=0.0, max_value=1.0),
                "展開反映率": st.column_config.ProgressColumn(format="%.1f%%", min_value=0.0, max_value=1.0),
                "試走信頼差": st.column_config.NumberColumn(format="%+.3f"),
                "ST影響差": st.column_config.NumberColumn(format="%+.3f"),
                "ハンデ影響差": st.column_config.NumberColumn(format="%+.3f"),
                "タイム基準差秒": st.column_config.NumberColumn(format="%+.4f"),
                "試走本走差秒": st.column_config.NumberColumn(format="%+.4f"),
                "前残り差": st.column_config.NumberColumn(format="%+.3f"),
                "追込み1着差": st.column_config.NumberColumn(format="%+.3f"),
                "追込み3着内差": st.column_config.NumberColumn(format="%+.3f"),
                "高温前残り差": st.column_config.NumberColumn(format="%+.3f"),
            },
        )
        selected_venue = st.selectbox("詳しく見る開催場", engine.V92_VENUES, key="v132_selected_venue")
        if st.button("選択した開催場の詳細を読み込む", use_container_width=True, key="v132_load_selected_venue"):
            with st.spinner(f"{selected_venue}の詳細を読み込んでいます…"):
                try:
                    st.session_state["v132_selected_venue_profile"] = engine.v92_venue_weight_profile(selected_venue, engine.DB_PATH)
                    st.session_state["v132_selected_venue_name"] = selected_venue
                except Exception as exc:
                    st.warning(f"開催場詳細を取得できませんでした: {exc}")
        selected_profile = st.session_state.get("v132_selected_venue_profile")
        if selected_profile and st.session_state.get("v132_selected_venue_name") == selected_venue:
            detail = pd.DataFrame(selected_profile.get("重み明細", []))
            if not detail.empty:
                st.dataframe(
                    detail, use_container_width=True, hide_index=True,
                    column_config={
                        "基礎係数": st.column_config.NumberColumn(format="%.2f"),
                        "開催場差": st.column_config.NumberColumn(format="%+.3f"),
                        "信頼度": st.column_config.NumberColumn(format="%.1f%%"),
                        "実効係数": st.column_config.NumberColumn(format="%.3f"),
                        "最大寄与目安": st.column_config.NumberColumn(format="%.3f"),
                    },
                )
            st.caption("全履歴層はレース番号なしでも利用します。展開層だけレース復元数に依存します。")

    st.divider()
    st.subheader("登録情報・DBメンテナンス")
    st.caption("選手一覧、履歴、登録漏れ監査は重いため、開くまでDB集計を実行しません。")
    if st.button("登録情報確認を開く", use_container_width=True, key="v132_open_registration_info"):
        st.session_state["v132_registration_info_open"] = True
    if st.session_state.get("v132_registration_info_open", False):
        if st.button("登録情報確認を閉じる", use_container_width=True, key="v132_close_registration_info"):
            st.session_state.pop("v132_registration_info_open", None)
            st.rerun()
        st.divider()
        st.subheader("登録されている情報")
        try:
            info = _v138_cached_db_summary(engine.DB_PATH, _v138_db_token(engine.DB_PATH))
            if not info["exists"]:
                st.warning("DBファイルがありません。")
            elif not info["tables"]:
                st.warning("DBにテーブルがありません。")
            else:
                with sqlite3.connect(engine.DB_PATH) as con:
                    canonical_count = int(con.execute("SELECT COUNT(*) FROM race_history").fetchone()[0]) if "race_history" in info["tables"] else 0
                    import_count = int(con.execute("SELECT COUNT(*) FROM v15_player_history_imports").fetchone()[0]) if "v15_player_history_imports" in info["tables"] else 0

                    if canonical_count > 0 and "players" in info["tables"]:
                        players = pd.read_sql_query(
                            """
                            SELECT p.player_name AS 選手名, COUNT(h.history_id) AS 登録件数,
                                   MAX(h.race_date) AS 最新日
                            FROM players p
                            LEFT JOIN race_history h ON h.player_id=p.player_id
                            GROUP BY p.player_id, p.player_name
                            HAVING COUNT(h.history_id) > 0
                            ORDER BY p.player_name
                            """, con)
                        source_mode = "players / race_history"
                    elif import_count > 0:
                        players = pd.read_sql_query(
                            """
                            SELECT player_name AS 選手名, COUNT(*) AS 登録件数,
                                   MAX(race_date) AS 最新日
                            FROM v15_player_history_imports
                            WHERE player_name IS NOT NULL AND player_name <> ''
                            GROUP BY player_name ORDER BY player_name
                            """, con)
                        source_mode = "v15_player_history_imports"
                    else:
                        players = pd.DataFrame(columns=["選手名", "登録件数", "最新日"])
                        source_mode = "データなし"

                    st.caption(f"表示元: {source_mode}")
                    query = st.text_input("選手名検索", placeholder="例：横田翔", key="db_player_search")
                    shown = players
                    if query.strip():
                        compact = re.sub(r"[\s　]+", "", query.strip())
                        shown = players[players["選手名"].astype(str).str.replace(r"[\s　]+", "", regex=True).str.contains(compact, case=False, na=False)]
                    st.write(f"選手一覧: {len(shown)}件")
                    st.dataframe(shown, use_container_width=True, hide_index=True, height=300)

                    names = shown["選手名"].astype(str).tolist()
                    if names:
                        selected = st.selectbox("履歴を確認する選手", names)
                        if canonical_count > 0:
                            history = pd.read_sql_query(
                                """
                                SELECT h.history_id AS 履歴ID, h.race_date AS 日付, h.venue AS 開催場, h.race_no AS レース,
                                       h.finish AS 着順, h.surface AS 走路, h.handicap AS ハンデ,
                                       h.trial_time AS 試走T, h.race_time AS 競走T,
                                       h.start_time AS ST, h.source AS 登録元, h.created_at AS 登録日時
                                FROM race_history h JOIN players p ON p.player_id=h.player_id
                                WHERE p.player_name=? ORDER BY h.race_date DESC, h.history_id DESC
                                """, con, params=(selected,))
                        else:
                            history = pd.read_sql_query(
                                """
                                SELECT history_key AS 履歴キー, race_date AS 日付, venue AS 開催場, race_type AS レース種別,
                                       rank AS 着順, weather AS 天候, surface AS 走路,
                                       track_temp AS 走路温度, air_temp AS 気温, humidity AS 湿度,
                                       car_no AS 車番, handicap AS ハンデ, distance AS 距離,
                                       laps AS 周回数, popularity AS 人気,
                                       trial_time AS 試走T, race_time AS 競走T, st AS ST,
                                       created_at AS 登録日時
                                FROM v15_player_history_imports
                                WHERE player_name=? ORDER BY race_date DESC, created_at DESC
                                """, con, params=(selected,))
                        st.write(f"{selected}：履歴 {len(history)}件")
                        display_history = history.drop(columns=[c for c in ["履歴ID", "履歴キー"] if c in history.columns], errors="ignore")
                        st.dataframe(display_history, use_container_width=True, hide_index=True, height=430)

                        if not history.empty:
                            st.markdown("#### 🗑️ 誤登録した履歴を削除")
                            st.caption("削除対象を選び、内容を確認してから実行してください。正規履歴を削除した場合、同じ走行の条件詳細データも同時に削除します。")
                            delete_options = []
                            for idx, r in history.reset_index(drop=True).iterrows():
                                race_label = r.get("レース", r.get("レース種別", ""))
                                finish_label = r.get("着順", "-")
                                trial_label = r.get("試走T", "-")
                                race_time_label = r.get("競走T", "-")
                                delete_options.append(
                                    f"{idx + 1}. {r.get('日付', '')} {r.get('開催場', '')} {race_label or ''} "
                                    f"着{finish_label} 試{trial_label} 競{race_time_label}"
                                )
                            selected_delete_label = st.selectbox(
                                "削除する履歴", delete_options, key=f"delete_history_select_{selected}"
                            )
                            selected_delete_idx = delete_options.index(selected_delete_label)
                            delete_row = history.reset_index(drop=True).iloc[selected_delete_idx]
                            preview_delete = delete_row.drop(labels=[c for c in ["履歴ID", "履歴キー"] if c in delete_row.index])
                            st.dataframe(pd.DataFrame([preview_delete]), use_container_width=True, hide_index=True)
                            confirm_delete = st.checkbox(
                                "この履歴を削除することを確認しました",
                                key=f"confirm_delete_history_{selected}_{selected_delete_idx}",
                            )
                            if st.button(
                                "選択した履歴を削除",
                                type="primary",
                                use_container_width=True,
                                disabled=not confirm_delete,
                                key=f"delete_history_button_{selected}",
                            ):
                                if "履歴ID" in history.columns:
                                    result = engine.v37_delete_race_history(int(delete_row["履歴ID"]), engine.DB_PATH)
                                else:
                                    result = engine.v37_delete_import_history(str(delete_row["履歴キー"]), engine.DB_PATH)
                                if result.get("deleted"):
                                    ok, msg = push_db_to_github(f"AutoRaceAI: {selected} の誤登録履歴を削除")
                                    if ok:
                                        st.success(result["message"] + " " + msg)
                                    else:
                                        st.warning(result["message"] + " GitHub保存は未完了です。" + msg)
                                    st.rerun()
                                else:
                                    st.warning(result.get("message", "削除できませんでした。"))

                        st.markdown("#### 🧹 選手情報を一括削除")
                        st.caption("選択中の選手について、正規履歴・条件詳細・周回特徴・選手別予測スナップショットをまとめて削除します。他選手とレース本体は残ります。")
                        delete_all_result_rows = st.checkbox(
                            "結果登録内のこの選手の行も削除する",
                            value=False,
                            key=f"delete_all_result_rows_{selected}",
                        )
                        confirm_player_name = st.text_input(
                            "確認のため選手名を入力",
                            placeholder=selected,
                            key=f"confirm_delete_player_name_{selected}",
                        )
                        normalized_confirm = re.sub(r"[\s　]+", "", confirm_player_name or "")
                        normalized_selected = re.sub(r"[\s　]+", "", selected or "")
                        can_delete_all = normalized_confirm == normalized_selected and bool(normalized_selected)
                        if st.button(
                            f"{selected} の選手情報を一括削除",
                            type="primary",
                            use_container_width=True,
                            disabled=not can_delete_all,
                            key=f"delete_all_player_button_{selected}",
                        ):
                            result = engine.v46_delete_player_all(
                                selected, engine.DB_PATH, delete_result_rows=delete_all_result_rows
                            )
                            if result.get("deleted"):
                                ok, msg = push_db_to_github(f"AutoRaceAI: {selected} の選手情報を一括削除")
                                detail = " / ".join(f"{k}:{v}" for k, v in result.get("counts", {}).items() if v)
                                if ok:
                                    st.success(result.get("message", "削除しました。") + (f" ({detail})" if detail else "") + " " + msg)
                                else:
                                    st.warning(result.get("message", "削除しました。") + (f" ({detail})" if detail else "") + " GitHub保存は未完了です。" + msg)
                                st.rerun()
                            else:
                                st.warning(result.get("message", "削除対象がありませんでした。"))

                    st.divider()
                    st.subheader("DBメンテナンス")
                    st.caption("姓名の空白違いと数値完全一致の同一走行を整理します。ただしRが異なる組み合わせは勝手に統合せず、確認対象として残します。")
                    if st.button("完全一致を含む重複データを一括統合", use_container_width=True):
                        result = engine.v32_merge_duplicate_players(engine.DB_PATH)
                        exact_result = engine.v58_cleanup_exact_numeric_duplicates(engine.DB_PATH)
                        race_result = engine.v33_cleanup_duplicate_histories(engine.DB_PATH)
                        identity_result = engine.v46_cleanup_player_identity_duplicates(engine.DB_PATH)
                        ok, msg = push_db_to_github("AutoRaceAI: 数値完全一致を含む重複履歴を一括統合")
                        summary = (
                            f"選手 {result['merged_players']}件を統合、履歴 {result['moved_histories']}件を移動、"
                            f"数値完全一致の正規履歴 {exact_result['merged_histories']}件・詳細履歴 {exact_result['merged_imports']}件、R相違の確認対象 {exact_result.get('r_conflicts', 0)}組、"
                            f"その他の同一走行履歴 {race_result['deleted_histories']}件・詳細履歴 {race_result['deleted_imports']}件、"
                            f"レース識別違いの正規履歴 {identity_result['merged_histories']}件・詳細履歴 {identity_result['merged_imports']}件を統合しました。"
                        )
                        if ok:
                            st.success(summary + " " + msg)
                        else:
                            st.warning(summary + " GitHub保存は未完了です。" + msg)
                        st.rerun()

                    st.divider()
                    st.subheader("予測・結果の登録漏れチェック")
                    st.caption("選手別履歴を直接照合します。Rがあるデータは日付・開催場・Rで、Rがないデータも同時登録された選手セットから完全レースを復元します。")
                    try:
                        if st.button("登録漏れチェックを実行・更新", use_container_width=True, key="v138_refresh_health"):
                            with st.spinner("登録履歴を照合しています…"):
                                _v138_cached_database_health.clear()
                                st.session_state["v138_health_loaded"] = True
                        if not st.session_state.get("v138_health_loaded"):
                            st.info("登録漏れチェックは重いため、自動実行しません。上のボタンを押した時だけ実行します。")
                            v97_health = None
                        else:
                            v97_health = _v138_cached_database_health(engine.DB_PATH, _v138_db_token(engine.DB_PATH))
                        if not v97_health:
                            raise RuntimeError("登録漏れチェックは未実行です")
                        v97_summary = v97_health.get("summary", {})
                        c1, c2, c3, c4, c5 = st.columns(5)
                        c1.metric("完全データ", f"{v97_summary.get('完全データ', 0)}R")
                        c2.metric("予測可能・未予測", f"{v97_summary.get('未予測', 0)}R")
                        c3.metric("結果未登録", f"{v97_summary.get('結果未登録', 0)}R")
                        c4.metric("予測済・結果未登録", f"{v97_summary.get('予測済結果未登録', 0)}R")
                        c5.metric("Rなし復元", f"{v97_summary.get('Rなし完全データ', 0)}R")

                        complete_all = v97_health.get("complete_all", [])
                        unidentified_complete = v97_health.get("unidentified_complete", [])
                        unpredicted = v97_health.get("predictable_unpredicted", [])
                        missing_result = v97_health.get("complete_missing_result", [])
                        predicted_missing = v97_health.get("predicted_missing_result", [])
                        incomplete = v97_health.get("incomplete", [])


                        with st.expander(f"選手別データ照合・全員分が揃ったレース（{len(complete_all)}R）", expanded=True):
                            if complete_all:
                                df_complete = pd.DataFrame(complete_all)
                                show_cols = [c for c in ["開催日", "開催場", "R", "登録状況", "予測", "結果", "抽出元", "照合状態", "選手"] if c in df_complete.columns]
                                st.dataframe(df_complete[show_cols], use_container_width=True, hide_index=True)
                                st.download_button(
                                    "完全データレース一覧をCSV保存",
                                    df_complete.to_csv(index=False).encode("utf-8-sig"),
                                    file_name="complete_player_data_races.csv",
                                    mime="text/csv",
                                    use_container_width=True,
                                )
                            else:
                                st.caption("全員分が揃ったレースは見つかりませんでした。")

                        if unidentified_complete:
                            with st.expander(f"Rは不明だが全員分が揃った登録セット（{len(unidentified_complete)}R）", expanded=False):
                                df_unknown = pd.DataFrame(unidentified_complete)
                                st.dataframe(df_unknown[[c for c in ["開催日", "開催場", "R", "登録状況", "抽出元", "選手"] if c in df_unknown.columns]], use_container_width=True, hide_index=True)
                                st.caption("選手全員分は揃っていますが、Rを特定できないため予測・結果の保存状況は断定していません。")

                        with st.expander(f"全選手データあり・予測未保存（{len(unpredicted)}R）", expanded=bool(unpredicted)):
                            if unpredicted:
                                df_unpred = pd.DataFrame(unpredicted)
                                show_cols = [c for c in ["開催日", "開催場", "R", "登録状況", "結果", "選手"] if c in df_unpred.columns]
                                st.dataframe(df_unpred[show_cols], use_container_width=True, hide_index=True)
                                st.download_button(
                                    "未予測一覧をCSV保存",
                                    df_unpred.to_csv(index=False).encode("utf-8-sig"),
                                    file_name="predictable_but_unpredicted.csv",
                                    mime="text/csv",
                                    use_container_width=True,
                                )
                                st.markdown("#### 一括予測")
                                st.caption("全選手分が揃い、Rを特定できる未予測レースだけが対象です。各レースの予測時点より後の履歴は学習から遮断します。")
                                selectable = [r for r in unpredicted if not str(r.get("R", "")).startswith("R不明")]
                                label_map = {
                                    f"{r.get('開催日')}｜{r.get('開催場')}｜{r.get('R')}｜{r.get('登録状況')}": r
                                    for r in selectable
                                }
                                selected_labels = st.multiselect(
                                    "一括予測するレース",
                                    options=list(label_map.keys()),
                                    default=list(label_map.keys()),
                                    key="v99_batch_prediction_selection",
                                )
                                batch_trials = st.select_slider(
                                    "一括予測の試行回数",
                                    options=[2000, 5000, 10000, 20000],
                                    value=5000,
                                    help="大量レースでは5000回が軽めです。保存後に必要なレースだけ通常画面で20000回へ再予測できます。",
                                    key="v99_batch_trials",
                                )
                                if st.button("選択した未予測レースを一括予測", type="primary", use_container_width=True, disabled=not selected_labels):
                                    selected_records = [label_map[x] for x in selected_labels]
                                    with st.spinner(f"{len(selected_records)}レースを時系列順に予測しています…"):
                                        batch_report = engine.v99_run_batch_predictions(
                                            selected_records, int(batch_trials), 20260719, engine.DB_PATH
                                        )
                                    st.session_state["v99_last_batch_report"] = batch_report
                                    if batch_report.get("成功", 0):
                                        st.success(
                                            f"一括予測完了：成功 {batch_report.get('成功', 0)}R｜"
                                            f"スキップ {batch_report.get('スキップ', 0)}R｜エラー {batch_report.get('エラー', 0)}R"
                                        )
                                    else:
                                        st.warning(
                                            f"保存できたレースはありませんでした。スキップ {batch_report.get('スキップ', 0)}R｜"
                                            f"エラー {batch_report.get('エラー', 0)}R"
                                        )
                                    st.rerun()
                                last_batch = st.session_state.get("v99_last_batch_report")
                                if last_batch:
                                    with st.expander("直前の一括予測レポート", expanded=bool(last_batch.get("エラー") or last_batch.get("スキップ"))):
                                        st.write(
                                            f"対象 {last_batch.get('対象', 0)}R｜成功 {last_batch.get('成功', 0)}R｜"
                                            f"スキップ {last_batch.get('スキップ', 0)}R｜エラー {last_batch.get('エラー', 0)}R"
                                        )
                                        details = pd.DataFrame(last_batch.get("details", []))
                                        if not details.empty:
                                            st.dataframe(details, use_container_width=True, hide_index=True)
                                            st.download_button(
                                                "一括予測レポートをCSV保存",
                                                details.to_csv(index=False).encode("utf-8-sig"),
                                                file_name="batch_prediction_report.csv",
                                                mime="text/csv",
                                                use_container_width=True,
                                            )
                            else:
                                st.success("全選手データがそろったレースは、すべて予測保存済みです。")

                        with st.expander(f"全選手データあり・結果未登録（{len(missing_result)}R）", expanded=False):
                            if missing_result:
                                df_missing = pd.DataFrame(missing_result)
                                show_cols = [c for c in ["開催日", "開催場", "R", "登録状況", "予測", "選手"] if c in df_missing.columns]
                                st.dataframe(df_missing[show_cols], use_container_width=True, hide_index=True)
                                st.download_button(
                                    "結果未登録一覧をCSV保存",
                                    df_missing.to_csv(index=False).encode("utf-8-sig"),
                                    file_name="complete_data_missing_results.csv",
                                    mime="text/csv",
                                    use_container_width=True,
                                )
                            else:
                                st.success("完全データのレースに結果登録漏れはありません。")

                        with st.expander(f"予測済み・結果未登録だけ（{len(predicted_missing)}R）", expanded=False):
                            if predicted_missing:
                                df_pm = pd.DataFrame(predicted_missing)
                                st.dataframe(df_pm[[c for c in ["開催日", "開催場", "R", "登録状況", "選手"] if c in df_pm.columns]], use_container_width=True, hide_index=True)
                            else:
                                st.caption("該当レースはありません。")

                        with st.expander(f"選手データ不足・人数不一致（{len(incomplete)}R）", expanded=False):
                            if incomplete:
                                df_inc = pd.DataFrame(incomplete)
                                st.dataframe(df_inc[[c for c in ["開催日", "開催場", "R", "登録状況", "不足人数", "選手"] if c in df_inc.columns]], use_container_width=True, hide_index=True)
                            else:
                                st.caption("該当レースはありません。")

                        if v97_summary.get("識別不能", 0):
                            st.caption(f"日付を特定できず集計対象外となった履歴: {v97_summary.get('識別不能', 0)}行")
                        st.info("『予測未保存』は、予測スナップショットがDBに残っていない状態です。過去に画面表示だけ行い、保存前の版で予測したレースも含まれる場合があります。")
                    except RuntimeError as exc:
                        if "未実行" not in str(exc):
                            st.warning(f"登録漏れチェックを実行できませんでした: {exc}")
                    except Exception as exc:
                        st.warning(f"登録漏れチェックを実行できませんでした: {exc}")

                    st.divider()
                    st.subheader("事故レースの学習除外")
                    st.caption("落車・反則・周回誤認・失格・競走中止などが1台でもあるレースは、結果を残したままレース全体をAI学習から除外します。")
                    try:
                        accident_status = engine.v76_accident_learning_status(engine.DB_PATH)
                        st.caption(
                            f"登録結果 {accident_status.get('登録結果', 0)}レース / "
                            f"学習対象外 {accident_status.get('事故レース除外', 0)}レース"
                        )
                        if accident_status.get("対象レース"):
                            with st.expander("学習対象外の事故レースを確認", expanded=False):
                                st.dataframe(pd.DataFrame(accident_status["対象レース"]), use_container_width=True, hide_index=True)
                    except Exception as exc:
                        st.caption(f"事故レース状況を取得できませんでした: {exc}")
                    if st.button("登録済み結果を再点検して事故レースを学習対象外にする", use_container_width=True):
                        with st.spinner("登録済み結果を再点検しています..."):
                            accident_result = engine.v76_reclassify_existing_accident_races(engine.DB_PATH)
                        ok, msg = push_db_to_github("AutoRaceAI: 事故レースを学習対象外へ再分類")
                        summary = (
                            f"{accident_result.get('結果レース確認', 0)}レースを確認し、"
                            f"事故 {accident_result.get('事故レース', 0)}レースを学習対象外にしました。"
                            f" 選手履歴 {accident_result.get('選手履歴除外', 0)}行を除外、"
                            f"周回学習 {accident_result.get('周回学習削除', 0)}行・"
                            f"事故レースの重み履歴 {accident_result.get('重み履歴削除', 0)}件を削除しました。"
                        )
                        if ok:
                            st.success(summary + " " + msg)
                        else:
                            st.warning(summary + " GitHub保存は未完了です。" + msg)
                        st.rerun()

                    st.divider()
                    st.subheader("グランドノート再学習")
                    st.caption("旧バージョンを含む登録済み結果のグランドノートを、選手別の展開学習へ全件再同期します。再同期後は、初周主導・位置維持・捌き・追込み・終盤・失速・熱走路補正のすべてに反映されます。")
                    try:
                        gn_status = engine.v62_grand_note_learning_status(engine.DB_PATH)
                        st.caption(
                            f"結果周回 {gn_status.get('結果周回行', 0)}行 / "
                            f"学習済み {gn_status.get('選手別周回行', 0)}行 / "
                            f"学習レース {gn_status.get('学習レース数', 0)}件 / "
                            f"未同期推定 {gn_status.get('未同期行推定', 0)}行"
                        )
                    except Exception as exc:
                        st.caption(f"再学習状況を取得できませんでした: {exc}")

                    if st.button("登録済みグランドノートを全件再学習", use_container_width=True):
                        with st.spinner("登録済みグランドノートを再同期しています..."):
                            gn_result = engine.v62_rebuild_grand_note_learning(engine.DB_PATH)
                        ok, msg = push_db_to_github("AutoRaceAI: 登録済みグランドノートを全件再学習")
                        summary = (
                            f"対象 {gn_result.get('対象レース', 0)}レース・{gn_result.get('対象選手', 0)}選手、"
                            f"周回 {gn_result.get('同期成功', 0)}行を反映 "
                            f"（新規 {gn_result.get('新規', 0)} / 更新 {gn_result.get('更新', 0)}）。"
                        )
                        if gn_result.get('結果選手不明', 0) or gn_result.get('選手不明', 0):
                            summary += (
                                f" 選手名不足 {gn_result.get('結果選手不明', 0)}行、"
                                f"選手未解決 {gn_result.get('選手不明', 0)}行。"
                            )
                        if ok:
                            st.success(summary + " " + msg)
                        else:
                            st.warning(summary + " GitHub保存は未完了です。" + msg)
                        st.rerun()

                    st.divider()
                    st.subheader("グランドノート未リンク修復")
                    with st.expander("Ver257 過去予測の周回再構成", expanded=False):
                        st.caption("保存済み予測の出走表を現在の6周モデルで再実行し、実測グランドノートとの差分学習を起動します。再構成値は通常予測の35%重みで使い、過去結果そのものを順位生成には使いません。")
                        try:
                            s253=_v253_reconstruction_status(engine.DB_PATH)
                            c1,c2,c3=st.columns(3)
                            c1.metric("再構成済み",f"{s253.get('races',0)}レース")
                            c2.metric("保存周回",f"{s253.get('lap_rows',0)}行")
                            c3.metric("実測照合",f"{s253.get('paired_laps',0)}周")
                            if s253.get('position_match') is not None:
                                st.caption(
                                    f"全体検証｜位置一致 {s253['position_match']*100:.1f}%｜"
                                    f"前後関係一致 {s253['pair_match']*100:.1f}%｜"
                                    f"平均順位誤差 {s253['position_mae']:.2f}台"
                                )
                                if s253.get('by_lap'):
                                    lap_df=pd.DataFrame(s253['by_lap']).rename(columns={
                                        'lap':'周回','paired':'照合数','position_match':'位置一致率',
                                        'pair_match':'前後関係一致率','position_mae':'平均順位誤差'})
                                    lap_df['位置一致率']=(lap_df['位置一致率']*100).round(1).astype(str)+'%'
                                    lap_df['前後関係一致率']=(lap_df['前後関係一致率']*100).round(1).astype(str)+'%'
                                    lap_df['平均順位誤差']=lap_df['平均順位誤差'].round(2)
                                    st.dataframe(lap_df,use_container_width=True,hide_index=True)
                            if s253.get('learning_enabled'):
                                st.success(f"差分学習は有効です（{s253.get('learning_races',0)}レース・{s253.get('learning_samples',0)}比較）。")
                            elif s253.get('reason'):
                                st.info(f"差分学習: {s253.get('reason')}")
                            if s253.get('race_labels'):
                                st.caption("再構成済み: "+" / ".join(s253['race_labels'][:20]))
                        except Exception as exc:
                            st.warning(f"再構成状況の表示に失敗しました: {exc}")
                        v253_limit=st.number_input("再構成する保存履歴数",min_value=1,max_value=200,value=80,step=10,key="v253_backfill_limit")
                        if st.button("過去予測を周回再構成",key="v253_backfill_button",use_container_width=True):
                            with st.spinner("保存済み予測を1レースずつ再構成しています…"):
                                r253=_v253_backfill_saved_lap_predictions(engine.DB_PATH,int(v253_limit))
                            if r253.get("saved",0)>0: st.success(r253.get("message","完了しました。"))
                            else: st.info(r253.get("message","再構成対象がありませんでした。"))
                            if r253.get('saved_races'):
                                st.caption("今回再構成: "+" / ".join(r253['saved_races']))
                            if r253.get("errors"):
                                st.warning(" / ".join(r253["errors"]))
                            st.rerun()

                    with st.expander(f"{_V231_APP_VERSION} 精度比較・一括再シミュレーションセンター", expanded=False):
                        st.caption("DBに実際に保存された周回予測だけを、同じ実測グランドノートで比較します。旧版を現在コードで再現したふりはせず、補正値の自動書換えも行いません。")
                        run_v255=st.button("保存済みバージョンを再評価",key="v255_backtest_run",use_container_width=True)
                        try:
                            snap_status=_v260_snapshot_storage_status(engine.DB_PATH,_V231_APP_VERSION)
                            ss1,ss2,ss3=st.columns(3)
                            ss1.metric(f"{_V231_APP_VERSION} 予測履歴",f"{int(snap_status.get('history_races',0))}レース")
                            ss2.metric("周回スナップショット保存",f"{int(snap_status.get('snapshot_races',0))}レース / {int(snap_status.get('snapshot_laps',0))}周")
                            ss3.metric("照合待ち・保存漏れ候補",f"{int(snap_status.get('waiting_or_missing',0))}レース")
                            if snap_status.get('waiting_or_missing',0):
                                st.warning("予測履歴数より周回保存数が少ないため、未保存レースがあります。保存済み予測から再計算せずに周回データを補完できます。")
                            st.markdown("#### 🔧 周回スナップショット一括補完")
                            st.caption("保存済みの全バージョンを対象に、周回データが無い履歴だけ補完します。元のバージョンは変更しません。")
                            _v262_repair_limit=st.number_input("一括補完する履歴数",min_value=1,max_value=500,value=120,step=20,key="v262_repair_limit")
                            if st.button("🔧 全バージョンの周回スナップショットを一括補完",key="v262_repair_all_snapshots",use_container_width=True):
                                with st.spinner("保存済み予測の周回データを一括補完しています…"):
                                    _repair=_v260_repair_saved_snapshot_history(engine.DB_PATH,'',int(_v262_repair_limit))
                                if _repair.get('repaired_races',0)>0:
                                    st.success(_repair.get('message','修復しました。'))
                                else:
                                    st.info(_repair.get('message','修復対象がありませんでした。'))
                                if _repair.get('labels'):
                                    st.caption("修復: "+" / ".join(_repair.get('labels',[])[:30]))
                                if _repair.get('errors'):
                                    st.warning(" / ".join(_repair.get('errors',[])[:12]))
                                st.rerun()

                            st.markdown(f"#### ⏱️ 保存済み予測を時系列順に{_V231_APP_VERSION}で再シミュレーション＋回収率採点")
                            st.caption("保存レースを開催日→開催場→Rの順に並べ、当時その時点より前に判明していた結果だけで再計算します。同日も1R→2R→3R…の順です。再計算後、保存済み最古オッズと登録済み払戻があるレースだけ、1点100円の仮想回収率を自動採点します。")
                            _v262_batch_limit=st.number_input("一括再シミュレーションする保存レース数",min_value=1,max_value=300,value=80,step=10,key="v262_batch_rerun_limit")
                            if st.button(f"⏱️ 時系列順に{_V231_APP_VERSION}で一括再シミュレーション",key="v262_batch_rerun",type="primary",use_container_width=True):
                                _prog=st.progress(0.0,text="一括再シミュレーションを開始します…")
                                def _v262_progress(done,total,label):
                                    frac=(float(done)/float(total)) if total else 1.0
                                    _prog.progress(min(1.0,max(0.0,frac)),text=f"{done}/{total}｜{label}")
                                _batch=_v262_batch_rerun_saved_histories(engine.DB_PATH,int(_v262_batch_limit),_v262_progress)
                                _prog.progress(1.0,text="一括再シミュレーション完了")
                                if _batch.get('rerun',0)>0:
                                    st.success(_batch.get('message','完了しました。'))
                                else:
                                    st.info(_batch.get('message','追加対象がありませんでした。'))
                                if _batch.get('labels'):
                                    st.caption("新規保存: "+" / ".join(_batch.get('labels',[])[:30]))
                                if _batch.get('errors'):
                                    st.warning(" / ".join(_batch.get('errors',[])[:12]))

                                # Ver272: 自動再シミュレーションの仮想回収率をその場で表示。
                                _roi_n=int(_batch.get("roi_evaluated",0) or 0)
                                if _roi_n>0:
                                    st.markdown("#### 📊 仮想100円均等・回収率バックテスト")
                                    _r1,_r2,_r3,_r4=st.columns(4)
                                    _r1.metric("採点対象",f"{_roi_n}R")
                                    _r2.metric("仮想投資",f"{int(_batch.get('roi_cost_yen',0) or 0):,}円")
                                    _r3.metric("仮想払戻",f"{int(_batch.get('roi_payout_yen',0) or 0):,}円")
                                    _rr=_batch.get("roi_return_rate")
                                    _r4.metric("参考回収率","—" if _rr is None else f"{float(_rr):.1f}%")
                                    st.caption(
                                        f"的中 {_batch.get('roi_hits',0)}R / "
                                        f"保存オッズなし {_batch.get('roi_no_odds',0)}R / "
                                        f"払戻未登録 {_batch.get('roi_no_payout',0)}R。"
                                        "各買い目は1点100円の仮想評価です。"
                                    )
                                    _roi_df=pd.DataFrame(_batch.get("roi_rows") or [])
                                    if not _roi_df.empty:
                                        _show_cols=[c for c in [
                                            "race","points","cost_yen","payout_yen","return_rate","hit",
                                            "odds_created_at","reason"
                                        ] if c in _roi_df.columns]
                                        st.dataframe(
                                            _roi_df[_show_cols],
                                            use_container_width=True,
                                            hide_index=True,
                                            column_config={
                                                "cost_yen":st.column_config.NumberColumn("仮想投資",format="%d円"),
                                                "payout_yen":st.column_config.NumberColumn("仮想払戻",format="%d円"),
                                                "return_rate":st.column_config.NumberColumn("回収率",format="%.1f%%"),
                                            },
                                        )
                                    st.caption(
                                        "比較の公平性を優先し、各レースに保存されている最古のオッズスナップショットを使用します。"
                                        "これは過去データの検証用で、実購入処理は行いません。"
                                    )
                                elif int(_batch.get("rerun",0) or 0)>0:
                                    st.info(
                                        "回収率採点できるレースがありませんでした。"
                                        "保存済みオッズと払戻の両方がある過去レースだけが対象です。"
                                    )
                                st.rerun()
                            result255=_v255_backtest_center(engine.DB_PATH)
                            cmp_df=result255.get('comparison',pd.DataFrame())
                            if cmp_df.empty:
                                st.info("比較できる保存済み周回予測がまだありません。")
                            else:
                                show=cmp_df.copy()
                                show['位置一致率']=show['位置一致率'].map(lambda x:f"{float(x):.1f}%")
                                show['前後関係一致率']=show['前後関係一致率'].map(lambda x:f"{float(x):.1f}%")
                                st.dataframe(show,use_container_width=True,hide_index=True)
                                decision=str(result255.get('decision') or '保留')
                                reason=str(result255.get('decision_reason') or '')
                                if decision=='採用候補':
                                    st.success(f"採用候補: {result255.get('best_version')}｜{reason}")
                                else:
                                    st.info(f"判定: {decision}｜{reason}")
                            learn=result255.get('learning') or {}
                            player_learn=result255.get('player_learning') or {}
                            l1,l2=st.columns(2)
                            l1.metric("場・周回差分学習",f"{int(learn.get('races',0) or 0)}レース / {int(learn.get('samples',0) or 0)}比較")
                            l2.metric("保存予測×選手残差",f"{int(player_learn.get('races',0) or 0)}レース / {int(player_learn.get('samples',0) or 0)}比較")
                            full_player=_v258_player_actual_lap_calibration(engine.DB_PATH,'','')
                            f1,f2,f3=st.columns(3)
                            f1.metric("全実測・選手学習",f"{int(full_player.get('races',0) or 0)}レース")
                            f2.metric("追抜比較",f"{int(full_player.get('pairs',0) or 0):,}件")
                            f3.metric("学習選手",f"{int(full_player.get('players',0) or 0)}人")
                            total_laps=int(full_player.get('total_laps',0) or 0); linked_laps=int(full_player.get('linked_laps',0) or 0)
                            if total_laps:
                                st.caption(f"グランドノート選手リンク: {linked_laps:,}/{total_laps:,}行（{linked_laps/total_laps*100:.1f}%）｜{full_player.get('reason','')}")
                            if not learn.get('enabled'):
                                st.caption("場・周回差分学習: "+str(learn.get('reason') or '未発動'))
                            if not player_learn.get('enabled'):
                                st.caption("選手別周回学習: "+str(player_learn.get('reason') or '未発動'))
                            if run_v255:
                                rid=_v255_save_backtest_report(engine.DB_PATH,result255)
                                if rid:
                                    st.success(f"比較結果をDBへ保存しました（レポートID: {rid}）。")
                                else:
                                    st.warning("比較は完了しましたが、レポートのDB保存に失敗しました。")
                        except Exception as exc:
                            st.warning(f"精度比較の表示に失敗しました: {type(exc).__name__}: {exc}")

                    st.caption("結果の周回順位はあるのに選手名へ結び付いていないデータを、レース・車番単位で診断して修復します。候補が一意のものだけ自動修復し、曖昧なものは手動で選びます。")
                    try:
                        health = engine.v63_db_health_report(engine.DB_PATH)
                        score = health.get("health_score", 0)
                        icon = "🟢" if score >= 99 else ("🟡" if score >= 95 else "🔴")
                        st.metric("グランドノートDB健康度", f"{icon} {score:.1f}%")
                        st.caption(
                            f"未リンク {health.get('unlinked_groups', 0)}組・{health.get('unlinked_rows', 0)}行 / "
                            f"結果周回 {health.get('result_lap_rows', 0)}行 / 学習済み {health.get('learned_lap_rows', 0)}行"
                        )
                        unresolved = engine.v63_grand_note_unlinked_groups(engine.DB_PATH)
                    except Exception as exc:
                        unresolved = []
                        st.error(f"未リンク診断に失敗しました: {exc}")

                    if unresolved:
                        if st.button("一意に決まる未リンクだけ自動修復", use_container_width=True):
                            repair_result = engine.v63_auto_repair_grand_note_links(engine.DB_PATH)
                            ok, msg = push_db_to_github("AutoRaceAI: グランドノート未リンクを自動修復")
                            summary = (
                                f"{repair_result.get('repaired_groups', 0)}組・{repair_result.get('synced_rows', 0)}行を修復。"
                                f"手動確認 {repair_result.get('manual_groups', 0)}組。"
                            )
                            st.success(summary + (" " + msg if ok else " GitHub保存は未完了です。" + msg))
                            st.rerun()

                        player_choices = engine.v63_player_name_choices(engine.DB_PATH)
                        for idx, item in enumerate(unresolved):
                            title = (
                                f"{item.get('race_date', '')} {item.get('venue', '')} "
                                f"{item.get('race_no', '')}R・{item.get('car_no')}番 "
                                f"（未リンク {item.get('lap_rows', 0)}行）"
                            )
                            with st.expander(title, expanded=True):
                                st.caption(
                                    f"1周目順位: {item.get('first_lap_position') or '-'} / "
                                    f"ゴール順位: {item.get('goal_position') or '-'} / "
                                    f"race_key: {item.get('race_key')}"
                                )
                                auto_candidates = [c.get('player_name') for c in item.get('candidates', [])]
                                if auto_candidates:
                                    st.info("自動候補: " + " / ".join(auto_candidates))
                                else:
                                    st.warning("自動候補を特定できません。結果ページを確認して選手名を選択してください。")

                                options = ["選択してください"] + auto_candidates + [n for n in player_choices if n not in auto_candidates]
                                selected_name = st.selectbox(
                                    "この車番の選手",
                                    options,
                                    key=f"v63_player_{item.get('race_key')}_{item.get('car_no')}_{idx}",
                                )
                                if st.button(
                                    "この未リンクを修復",
                                    key=f"v63_repair_{item.get('race_key')}_{item.get('car_no')}_{idx}",
                                    use_container_width=True,
                                    disabled=selected_name == "選択してください",
                                ):
                                    result = engine.v63_repair_grand_note_link(
                                        item.get('race_key'), item.get('car_no'), selected_name, engine.DB_PATH
                                    )
                                    if result.get('ok'):
                                        ok, msg = push_db_to_github("AutoRaceAI: グランドノート未リンクを手動修復")
                                        st.success(result.get('message', '修復しました。') + f" 周回{result.get('synced_rows', 0)}行を再学習しました。" + (" " + msg if ok else " GitHub保存は未完了です。" + msg))
                                        st.rerun()
                                    else:
                                        st.warning(result.get('message', '修復できませんでした。'))
                    else:
                        st.success("未リンクのグランドノートはありません。すべて選手別学習へ反映されています。")

                    st.divider()
                    table = st.selectbox("DBテーブルを直接確認", info["tables"])
                    columns = [r[1] for r in con.execute(f"PRAGMA table_info({qident(table)})").fetchall()]
                    count = con.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0]
                    st.caption(f"{table}: {count}件 / 列: {', '.join(columns)}")
                    preview = pd.read_sql_query(f"SELECT * FROM {qident(table)} LIMIT 500", con)
                    st.dataframe(preview, use_container_width=True, hide_index=True, height=400)
                    if count > 500:
                        st.caption("表示は先頭500件です。")
        except Exception as exc:
            st.error(f"登録情報の確認エラー: {type(exc).__name__}: {exc}")
            st.exception(exc)

    st.caption("GitHub保存にはStreamlit Secretsの設定が必要です。トークンはコードやGitHubへ直接書かないでください。")




# Ver213: 予測必須経路の全DB診断を遅延化、工程別時間計測、回収率重視プランの単独ガミ完全除外。

# Ver214: 2連単・2連複・3連複から関連高期待値3連単への1対1置換を長期回収率基準で自動比較。

# Ver215: 回収率重視プランを版情報付きで完全保存し、日別・開催場別・日付×開催場・月別・全体の実回収率ダッシュボードを追加。


# Ver216: 回収率実績に非推奨除外・推奨のみ・非推奨のみ比較を追加

# Ver217: シミュレーション後は最小スナップショットだけ同期保存し、全買い目・特徴量保存をバックグラウンド化。

# Ver221: 4券種全オッズの時刻別DB保存・最新自動復元・履歴選択復元

# Ver226: 開催場別・ハンデ構成別・日付開催場別の予測成績分析を回収率実績画面へ追加。


# Ver245: 6周展開診断表示と、同一1着・同一3車の2着3着入替ペアを条件付きで保護。




# Ver268: ハンデ残差補正・安全判定
try:
    with st.expander("🎯 Ver268 ハンデ残差補正・安全判定", expanded=False):
        st.caption(
            "保存済み予測Tと実結果の残差からハンデ補正を学習します。"
            "同一実走のVer重複を除外し、5-fold CVでMAEが3%以上改善した時だけ自動採用します。"
        )
        _m268 = _v268_handicap_bias_model(engine.DB_PATH, "", "")
        a,b,c,d = st.columns(4)
        a.metric("学習走数", f"{int(_m268.get('samples',0) or 0)}")
        b.metric("CV補正前MAE", "—" if _m268.get("mae_before") is None else f"{_m268['mae_before']:.4f}秒")
        c.metric("CV補正後MAE", "—" if _m268.get("mae_after_cv") is None else f"{_m268['mae_after_cv']:.4f}秒")
        d.metric("改善率", f"{float(_m268.get('improvement_pct',0.0)):+.1f}%")
        if _m268.get("enabled"):
            st.success("ハンデ残差補正: 自動採用")
        else:
            st.info("ハンデ残差補正: 自動保留")
        st.caption(
            f"傾き {float(_m268.get('slope',0.0)):+.6f}秒/m / "
            f"切片 {float(_m268.get('intercept',0.0)):+.4f}秒 / "
            f"{_m268.get('reason','')}"
        )
except Exception as _v268_ui_exc:
    st.warning("Ver268ハンデ安全判定エラー: " + _runtime_exception_text(_v268_ui_exc))


# Ver266: 予測競走Tスナップショット管理
try:
    with st.expander("💾 Ver268 予測競走Tスナップショット", expanded=False):
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
    with st.expander("🧩 Ver268 選手名None補修", expanded=False):
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
    with st.expander("🔬 Ver268 基礎予測・誤差解析", expanded=False):
        _v266_render_error_analysis(engine.DB_PATH)
except Exception as _v266_exc:
    st.warning("Ver266誤差解析の表示に失敗しました: " + _runtime_exception_text(_v266_exc))
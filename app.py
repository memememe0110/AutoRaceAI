from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

import engine

st.set_page_config(page_title="AutoRaceAI スマホ本予測", page_icon="🏁", layout="wide")
st.title("🏁 AutoRaceAI スマホ本予測")
st.caption("Ver15.2の設定値・評価関数・モンテカルロを使用するスマホ版です。")


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
        for table, key in [("players", "players"), ("race_history", "history")]:
            if table in info["tables"]:
                info[key] = int(con.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0])
    return info


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


with st.sidebar:
    st.header("予測設定")
    trials = st.selectbox("試行回数", [3000, 10000, 20000], index=1)
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

    try:
        summary = db_summary(engine.DB_PATH)
        c1, c2 = st.columns(2)
        c1.metric("登録選手", summary.get("players", 0))
        c2.metric("登録履歴", summary.get("history", 0))
        st.caption(f"DB容量: {summary.get('size', 0) / 1024 / 1024:.2f} MB")
    except Exception as exc:
        st.warning(f"DB情報を確認できません: {exc}")

prediction_tab, db_tab = st.tabs(["🏁 予測", "🗃️ 登録情報確認"])

with prediction_tab:
    st.info("予測方式：Ver15.2互換エンジン。完全一致は同じDB・設定・乱数条件での照合が必要です。")
    text = st.text_area(
        "公式出走表を全文貼り付け",
        height=430,
        placeholder="autorace.jpの出走表をコピーして貼り付け",
    )

    if st.button("解析して元版設定で予測", type="primary", use_container_width=True):
        if not text.strip():
            st.warning("出走表を貼り付けてください。")
            st.stop()
        try:
            with st.spinner("Ver15.2本体で予測中…"):
                df, bets, output, entries, meta = engine.ver16_run_prediction(
                    text, int(trials), int(seed)
                )
            st.success("予測が完了しました")
            st.subheader("解析した出走表")
            st.dataframe(entries.drop(columns=["_raw"], errors="ignore"), use_container_width=True, hide_index=True)

            cols = [c for c in [
                "改善後順位", "車", "選手名", "ハンデ", "試走換算",
                "基礎スピード点", "実戦能力点", "勝負強さ点", "展開適性点",
                "スタート伸び指数", "ゴール前伸び指数", "安定上位指数",
                "混戦突破適性", "改善後総合点",
            ] if c in df.columns]
            result = df[cols].sort_values(["改善後順位", "車"]).reset_index(drop=True)
            st.subheader("予測順位")
            st.dataframe(result, use_container_width=True, hide_index=True)

            total = int(trials)
            top = sorted(bets["三連単"].items(), key=lambda x: x[1], reverse=True)[:20]
            tri = pd.DataFrame([
                {"順位": i, "三連単": "-".join(map(str, combo)), "確率": f"{(count / total) * 100:.2f}%"}
                for i, (combo, count) in enumerate(top, 1)
            ])
            st.subheader("三連単確率 上位20")
            st.dataframe(
                tri,
                use_container_width=True,
                hide_index=True,
                column_config={"確率": st.column_config.NumberColumn(format="%.3f")},
            )

            if Path(output).exists():
                st.download_button(
                    "予測結果Excelを保存",
                    Path(output).read_bytes(),
                    file_name=Path(output).name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
        except Exception as exc:
            st.error(f"予測エラー: {type(exc).__name__}: {exc}")
            st.exception(exc)

with db_tab:
    st.subheader("登録されている情報")
    try:
        info = db_summary(engine.DB_PATH)
        if not info["exists"]:
            st.warning("DBファイルがありません。")
        elif not info["tables"]:
            st.warning("DBにテーブルがありません。")
        else:
            st.caption("DB内のテーブル: " + "、".join(info["tables"]))
            with sqlite3.connect(engine.DB_PATH) as con:
                if "players" in info["tables"]:
                    players = pd.read_sql_query(
                        "SELECT player_id, player_name, created_at, updated_at FROM players ORDER BY player_name",
                        con,
                    )
                    query = st.text_input("選手名検索", placeholder="例：横田翔")
                    shown = players
                    if query.strip():
                        shown = players[players["player_name"].astype(str).str.contains(query.strip(), case=False, na=False)]
                    st.write(f"選手一覧: {len(shown)}件")
                    st.dataframe(shown, use_container_width=True, hide_index=True, height=300)

                    names = shown["player_name"].astype(str).tolist()
                    if names:
                        selected = st.selectbox("履歴を確認する選手", names)
                        history = pd.read_sql_query(
                            """
                            SELECT h.race_date AS 日付, h.venue AS 開催場, h.race_no AS R,
                                   h.finish AS 着順, h.starters AS 車立て, h.surface AS 走路,
                                   h.handicap AS ハンデ, h.trial_time AS 試走T,
                                   h.race_time AS 競走T, h.start_time AS ST,
                                   h.result_status AS 状態, h.use_for_model AS 予測使用,
                                   h.source AS 登録元
                            FROM race_history h
                            JOIN players p ON p.player_id = h.player_id
                            WHERE p.player_name = ?
                            ORDER BY h.race_date DESC, h.history_id DESC
                            """,
                            con,
                            params=(selected,),
                        )
                        st.write(f"{selected}：履歴 {len(history)}件")
                        st.dataframe(history, use_container_width=True, hide_index=True, height=430)
                else:
                    st.info("このDBには players テーブルがありません。下のテーブル確認から内容を確認できます。")

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

st.info("今回の版はSciPyを使わずに相関計算するため、Streamlit Cloudの『No module named scipy』エラーを回避します。")

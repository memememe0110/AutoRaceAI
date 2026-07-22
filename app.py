from pathlib import Path
import shutil
import sqlite3
import tempfile

import pandas as pd
import streamlit as st
import engine

st.set_page_config(page_title="AutoRaceAI スマホ本予測", page_icon="🏁", layout="wide")
st.title("🏁 AutoRaceAI スマホ本予測")
st.caption("Ver15.2と同じ設定値・評価関数・モンテカルロを使用します。")


def db_summary(db_path):
    """SQLiteを壊さずに検査して、読み込み結果を返す。"""
    path = Path(db_path)
    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "message": "DBファイルが空です。", "tables": [], "players": 0, "history": 0}

    try:
        with path.open("rb") as f:
            header = f.read(16)
        if header != b"SQLite format 3\x00":
            return {"ok": False, "message": "SQLite形式ではありません。ZIPのままではなく、autorace_players.sqlite3本体を選んでください。", "tables": [], "players": 0, "history": 0}

        with sqlite3.connect(str(path)) as con:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            if str(check).lower() != "ok":
                return {"ok": False, "message": f"SQLite整合性エラー: {check}", "tables": [], "players": 0, "history": 0}
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            players = con.execute("SELECT COUNT(*) FROM players").fetchone()[0] if "players" in tables else 0
            history = con.execute("SELECT COUNT(*) FROM race_history").fetchone()[0] if "race_history" in tables else 0
        return {"ok": True, "message": "OK", "tables": tables, "players": players, "history": history}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "tables": [], "players": 0, "history": 0}


def install_uploaded_db(uploaded):
    data = uploaded.getvalue()
    if not data:
        raise ValueError("選択したファイルが空です。")

    # まず一時ファイルで検査する。失敗時に現在のDBを壊さない。
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        tmp.write(data)
        temp_path = Path(tmp.name)

    try:
        before = db_summary(temp_path)
        if not before["ok"]:
            raise ValueError(before["message"])

        target = Path(engine.DB_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_suffix(".backup.sqlite3")
        if target.exists() and target.stat().st_size > 0:
            shutil.copy2(target, backup)

        shutil.copy2(temp_path, target)
        # 旧DBに不足テーブルがあっても、元データを残したまま追加する。
        engine.mount_and_init_db()
        engine.v15_init_tables(str(target))

        after = db_summary(target)
        if not after["ok"]:
            if backup.exists():
                shutil.copy2(backup, target)
            raise ValueError(after["message"])
        return after
    finally:
        temp_path.unlink(missing_ok=True)


with st.sidebar:
    st.header("予測設定")
    trials = st.selectbox("試行回数", [3000, 10000, 20000], index=1)
    seed = st.number_input("乱数シード", min_value=0, value=20260719, step=1)
    st.divider()
    st.subheader("履歴DB")
    st.caption("iPhoneでも選択できるよう、拡張子の制限は外してあります。")
    db_file = st.file_uploader(
        "autorace_players.sqlite3を選択",
        type=None,
        accept_multiple_files=False,
        help="ZIPではなくSQLite本体を選択してください。",
    )

    if db_file is not None:
        st.write(f"選択中: **{db_file.name}** ({len(db_file.getvalue()):,} bytes)")
        if st.button("この履歴DBを読み込む", use_container_width=True):
            try:
                info = install_uploaded_db(db_file)
                st.session_state["db_loaded"] = True
                st.success("履歴DBを読み込みました。")
                st.write(f"登録選手: {info['players']:,} / 登録履歴: {info['history']:,}")
            except Exception as exc:
                st.error(f"DB読込エラー: {exc}")

    current = db_summary(engine.DB_PATH)
    if current["ok"]:
        c1, c2 = st.columns(2)
        c1.metric("登録選手", current["players"])
        c2.metric("登録履歴", current["history"])
        with st.expander("DB内のテーブルを確認"):
            st.code("\n".join(current["tables"]) if current["tables"] else "テーブルなし")
    else:
        st.warning(current["message"])

text = st.text_area("公式出走表を全文貼り付け", height=430, placeholder="autorace.jpの出走表をコピーして貼り付け")

if st.button("解析して元版設定で予測", type="primary", use_container_width=True):
    if not text.strip():
        st.warning("出走表を貼り付けてください。")
        st.stop()
    try:
        with st.spinner("Ver15.2本体で予測中…"):
            df, bets, output, entries, meta = engine.ver16_run_prediction(text, int(trials), int(seed))
        st.success("予測が完了しました")
        st.subheader("解析した出走表")
        st.dataframe(entries.drop(columns=["_raw"], errors="ignore"), use_container_width=True, hide_index=True)

        cols = [c for c in [
            "改善後順位", "車", "選手名", "ハンデ", "試走換算",
            "基礎スピード点", "実戦能力点", "勝負強さ点", "展開適性点",
            "スタート伸び指数", "ゴール前伸び指数", "安定上位指数",
            "混戦突破適性", "改善後総合点"
        ] if c in df.columns]
        result = df[cols].sort_values(["改善後順位", "車"]).reset_index(drop=True)
        st.subheader("予測順位")
        st.dataframe(result, use_container_width=True, hide_index=True)

        total = int(trials)
        top = sorted(bets["三連単"].items(), key=lambda x: x[1], reverse=True)[:20]
        tri = pd.DataFrame([
            {"順位": i, "三連単": "-".join(map(str, combo)), "確率": count / total}
            for i, (combo, count) in enumerate(top, 1)
        ])
        st.subheader("三連単確率 上位20")
        st.dataframe(tri, use_container_width=True, hide_index=True)

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

st.info("DBはアプリ再起動や再デプロイで初期化される場合があります。その場合は同じSQLiteファイルを再度読み込んでください。")

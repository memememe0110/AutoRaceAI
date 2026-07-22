import shutil
from pathlib import Path
import pandas as pd
import streamlit as st
import engine

st.set_page_config(page_title="AutoRaceAI スマホ本予測", page_icon="🏁", layout="wide")
st.title("🏁 AutoRaceAI スマホ本予測")
st.caption("Ver15.2と同じ設定値・評価関数・モンテカルロを使用します。")

with st.sidebar:
    st.header("予測設定")
    trials = st.selectbox("試行回数", [3000, 10000, 20000], index=1)
    seed = st.number_input("乱数シード", min_value=0, value=20260719, step=1)
    st.divider()
    st.subheader("履歴DB")
    db_file = st.file_uploader("以前の autorace_players.sqlite3", type=["sqlite3", "db"])
    if db_file is not None:
        Path(engine.DB_PATH).write_bytes(db_file.getvalue())
        engine.mount_and_init_db()
        st.success("履歴DBを読み込みました")
    try:
        import sqlite3
        with sqlite3.connect(engine.DB_PATH) as con:
            p = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
            h = con.execute("SELECT COUNT(*) FROM race_history").fetchone()[0]
        st.metric("登録選手", p)
        st.metric("登録履歴", h)
    except Exception:
        pass

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
            "改善後順位","車","選手名","ハンデ","試走換算",
            "基礎スピード点","実戦能力点","勝負強さ点","展開適性点",
            "スタート伸び指数","ゴール前伸び指数","安定上位指数",
            "混戦突破適性","改善後総合点"
        ] if c in df.columns]
        result = df[cols].sort_values(["改善後順位","車"]).reset_index(drop=True)
        st.subheader("予測順位")
        st.dataframe(result, use_container_width=True, hide_index=True)

        total = int(trials)
        top = sorted(bets["三連単"].items(), key=lambda x: x[1], reverse=True)[:20]
        tri = pd.DataFrame([
            {"順位":i, "三連単":"-".join(map(str, combo)), "確率":count/total}
            for i,(combo,count) in enumerate(top,1)
        ])
        st.subheader("三連単確率 上位20")
        st.dataframe(tri, use_container_width=True, hide_index=True, column_config={"確率":st.column_config.ProgressColumn(format="%.3f%%", min_value=0, max_value=0.2)})

        if Path(output).exists():
            st.download_button("予測結果Excelを保存", Path(output).read_bytes(), file_name=Path(output).name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    except Exception as exc:
        st.error(f"予測エラー: {type(exc).__name__}: {exc}")
        st.exception(exc)

st.info("元版と同じ結果に近づけるには、元版で使っていたSQLite履歴DBをサイドバーから読み込んでください。履歴が空の場合も計算はできますが、履歴由来の評価は再現されません。")

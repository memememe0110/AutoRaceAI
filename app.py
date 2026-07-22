from pathlib import Path
import pandas as pd
import streamlit as st
from parser import EntryParser, ParseError
from database import Database
from predictor import predict_race

st.set_page_config(page_title="AutoRaceAI Ver18.0 α", page_icon="🏁")
st.title("🏁 AutoRaceAI Ver18.0 α")
st.caption("Notebookを使わないスマホ用基盤版")

db = Database("autorace.db")
db.initialize()

tab1, tab2 = st.tabs(["予測", "DB管理"])

with tab1:
    text = st.text_area("公式出走表を貼り付け", height=360)
    if st.button("解析", type="primary", use_container_width=True):
        try:
            st.session_state["race"] = EntryParser().parse(text)
        except ParseError as e:
            st.error(str(e))

    race = st.session_state.get("race")
    if race:
        st.subheader("開催情報")
        st.json({k: v for k, v in race.items() if k not in ("players", "raw_text")})
        st.subheader(f"出走選手候補: {len(race['players'])}人")
        st.dataframe(pd.DataFrame(race["players"]), hide_index=True, use_container_width=True)

        if st.button("SQLiteへ保存", use_container_width=True):
            race_id = db.save_race(race)
            st.success(f"保存しました。レースID: {race_id}")

        if st.button("簡易予測", use_container_width=True):
            st.session_state["prediction"] = predict_race(race)

    if st.session_state.get("prediction"):
        st.subheader("簡易予測")
        st.caption("現在は試走T・ハンデ・車番だけを使う仮予測です。")
        st.dataframe(pd.DataFrame(st.session_state["prediction"]), hide_index=True, use_container_width=True)

with tab2:
    stats = db.stats()
    st.metric("保存レース数", stats["races"])
    st.metric("保存出走数", stats["entries"])
    path = Path("autorace.db")
    if path.exists():
        st.download_button("DBをダウンロード", path.read_bytes(), "autorace.db", use_container_width=True)

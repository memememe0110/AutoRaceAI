import os
from pathlib import Path
os.environ.setdefault('AUTORACEAI_DATA_DIR', str(Path(__file__).resolve().parent/'data'))

import pandas as pd
import streamlit as st
from mobile_helpers import *

st.set_page_config(page_title='AutoRaceAI Personal', page_icon='🏁', layout='centered')
st.markdown('''<style>
.block-container{padding-top:1rem;padding-bottom:3rem;max-width:760px}
.stButton button{min-height:48px;font-size:1rem;font-weight:700}
textarea{font-size:16px!important}
</style>''', unsafe_allow_html=True)

DB_DIR.mkdir(parents=True,exist_ok=True)
mount_and_init_db()
init_full_result_tables()

st.title('🏁 AutoRaceAI Personal')
st.caption('スマホは操作だけ。計算と保存はクラウド側で行います。')

tab_pred,tab_hist,tab_result,tab_db=st.tabs(['予測','履歴登録','結果登録','DB管理'])

with tab_pred:
    raw=st.text_area('公式出走表を全文貼り付け',height=280,key='pred_text')
    trials=st.select_slider('シミュレーション回数',options=[2000,5000,10000,20000],value=10000)
    if st.button('予測を実行',type='primary',use_container_width=True):
        try:
            with st.spinner('予測・シミュレーション中…'):
                r=predict_from_text(raw,trials=trials)
            st.success('予測完了')
            meta=r['meta']
            st.dataframe(pd.DataFrame([{'項目':k,'取得値':meta.get(k)} for k in ['開催日','開催場','レース','発走時刻','天候','走路状況','気温','湿度','走路温度','風向','風速']]),use_container_width=True,hide_index=True)
            cols=[c for c in ['改善後順位','車','選手名','ハンデ','学習前総合点','全結果学習補正','改善後総合点'] if c in r['df'].columns]
            st.dataframe(r['df'][cols].sort_values('改善後順位'),use_container_width=True,hide_index=True)
            top=[]
            for combo,count in sorted(r['bets']['三連単'].items(),key=lambda x:x[1],reverse=True)[:10]:
                top.append({'組合せ':'-'.join(map(str,combo)),'確率':count/trials})
            st.subheader('三連単確率 上位10')
            st.dataframe(pd.DataFrame(top),use_container_width=True,hide_index=True,column_config={'確率':st.column_config.ProgressColumn(format='%.2f%%',min_value=0,max_value=1)})
            p=Path(r['result_file'])
            if p.exists():
                st.download_button('予測Excelをダウンロード',p.read_bytes(),file_name=p.name,mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',use_container_width=True)
        except Exception as e:
            st.exception(e)

with tab_hist:
    name=st.text_input('選手名（自動判定できない場合のみ）')
    raw=st.text_area('公式選手履歴または表を貼り付け',height=300,key='hist_text')
    if st.button('履歴を登録',use_container_width=True):
        try:
            df,report=register_history_text(raw,name or None)
            st.success('登録完了')
            st.dataframe(report,use_container_width=True,hide_index=True)
            st.dataframe(df.head(30),use_container_width=True,hide_index=True)
        except Exception as e: st.exception(e)

with tab_result:
    venue=st.text_input('開催場（自動取得できない場合のみ）')
    race_no=st.text_input('レース番号（例: 12R、自動取得できない場合のみ）')
    raw=st.text_area('公式結果ページを全文貼り付け',height=320,key='result_text')
    if st.button('結果を解析',use_container_width=True):
        try:
            meta,results,laps=preview_result_text(raw,venue,race_no)
            st.session_state['parsed_result']=(raw,venue,race_no)
            st.dataframe(pd.DataFrame([{'項目':k,'値':v} for k,v in meta.items()]),use_container_width=True,hide_index=True)
            st.dataframe(pd.DataFrame(results),use_container_width=True,hide_index=True)
            st.info(f'{len(results)}選手、周回順位 {len(laps)}行を解析しました。')
        except Exception as e: st.exception(e)
    if st.button('結果登録＋全結果再学習',type='primary',use_container_width=True):
        try:
            with st.spinner('結果登録・再学習中…'):
                r=register_result_text(raw,venue,race_no,learn=True)
            st.success(f"登録完了: 新規{r['added']}件、更新{r['updated']}件")
            st.dataframe(pd.DataFrame(r['results']),use_container_width=True,hide_index=True)
            if r['learning'] and 'settings' in r['learning']:
                st.write(f"再学習対象 {r['learning']['races_used']}レース、誤差 {r['learning']['old_loss']:.4f} → {r['learning']['new_loss']:.4f}")
                st.dataframe(r['learning']['settings'],use_container_width=True,hide_index=True)
            elif r['learning']:
                st.info(r['learning'].get('message','結果は登録済みです。'))
        except Exception as e: st.exception(e)

with tab_db:
    with sqlite3.connect(str(DB_PATH)) as con:
        players=con.execute('SELECT COUNT(*) FROM players').fetchone()[0]
        history=con.execute('SELECT COUNT(*) FROM race_history').fetchone()[0]
        tables=pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",con)
    c1,c2=st.columns(2); c1.metric('選手',players); c2.metric('履歴',history)
    st.dataframe(tables,use_container_width=True,hide_index=True)
    if Path(DB_PATH).exists():
        st.download_button('SQLite DBをバックアップ',Path(DB_PATH).read_bytes(),file_name='autorace_players.sqlite3',mime='application/octet-stream',use_container_width=True)
    uploaded=st.file_uploader('バックアップDBから復元',type=['sqlite3','db'])
    if uploaded and st.button('このDBへ復元',use_container_width=True):
        Path(DB_PATH).write_bytes(uploaded.getvalue()); st.success('復元しました。ページを再読み込みしてください。')

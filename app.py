import streamlit as st
from parser import parse_page
from predictor import predict

st.set_page_config(page_title='AutoRaceAI スマホ予測',page_icon='🏁',layout='centered')
st.title('🏁 AutoRaceAI スマホ予測')
st.caption('公式出走表を全文コピーして貼り付けるだけ')
text=st.text_area('公式出走表',height=320,placeholder='autorace.jp の出走表をここへ貼り付け')
if st.button('予測する',type='primary',use_container_width=True):
    meta,entries=parse_page(text)
    if len(entries)<2:
        st.error('選手を解析できませんでした。車番から選手データまで含めて全文を貼り付けてください。')
    else:
        result=predict(entries,meta)
        title=f"{meta.get('開催場','')} {meta.get('レース','')}R {meta.get('走路状態','')}"
        st.subheader(title.strip())
        top=result.iloc[0]
        st.success(f"◎ 本命  {int(top['車番'])}番 {top['選手名']}　1着目安 {top['1着目安']:.1f}%")
        cols=st.columns(min(3,len(result)))
        for i,c in enumerate(cols):
            r=result.iloc[i]
            c.metric(f"{r['印']} {int(r['車番'])}番",r['選手名'],f"総合 {r['総合点']:.1f}")
        show=result[['予測順位','印','車番','選手名','ハンデ','試走T','ST','予測競走T','総合点','1着目安']].copy()
        show['総合点']=show['総合点'].round(1); show['1着目安']=show['1着目安'].round(1)
        st.dataframe(show,use_container_width=True,hide_index=True)
        with st.expander('解析した出走データ'):
            st.dataframe(entries,use_container_width=True,hide_index=True)
        st.info('この初版は、Ver15.2の評価思想を公式出走表だけで動かす軽量予測です。選手履歴DBを使う完全版とは数値が一致しません。')

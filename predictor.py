import re
import numpy as np
import pandas as pd

def _lower_score(s):
    x=pd.to_numeric(s,errors='coerce'); x=x.fillna(x.median())
    if x.nunique()<=1:return pd.Series(50.0,index=x.index)
    return (1-(x-x.min())/(x.max()-x.min()))*100

def _higher_score(s):
    x=pd.to_numeric(s,errors='coerce'); x=x.fillna(x.median())
    if x.nunique()<=1:return pd.Series(50.0,index=x.index)
    return ((x-x.min())/(x.max()-x.min()))*100

def _rank_score(v):
    if not isinstance(v,str): return 45.0
    m=re.match(r'([SAB])-?(\d+)',v)
    if not m:return 45.0
    base={'S':100,'A':78,'B':56}[m.group(1)]
    return max(25,base-min(int(m.group(2)),100)*0.18)

def predict(entries: pd.DataFrame, meta: dict) -> pd.DataFrame:
    if entries.empty: return entries
    d=entries.copy()
    d['試走点']=_lower_score(d['試走T'])
    d['ST点']=_lower_score(d['ST'])
    d['平均競走点']=_lower_score(d['平均競走T'])
    d['最高競走点']=_lower_score(d['最高競走T'])
    d['審査点']=_higher_score(d['審査P'])
    d['ランク点']=d['現ランク'].map(_rank_score)
    wet='湿' in str(meta.get('走路状態',''))
    r2='湿2連対率' if wet else '良2連対率'; r3='湿3連対率' if wet else '良3連対率'
    d['走路成績点']=(_higher_score(d[r2])*0.45+_higher_score(d[r3])*0.55)
    h=pd.to_numeric(d['ハンデ'],errors='coerce').fillna(0)
    d['ハンデ点']=_lower_score(h)
    # Ver15.2の主要思想を、出走表だけで使える軽量版へ圧縮
    d['総合点']=(d['試走点']*0.28+d['平均競走点']*0.18+d['最高競走点']*0.08+
                 d['ST点']*0.13+d['審査点']*0.10+d['ランク点']*0.08+
                 d['走路成績点']*0.10+d['ハンデ点']*0.05)
    trial=pd.to_numeric(d['試走T'],errors='coerce')
    avg=pd.to_numeric(d['平均競走T'],errors='coerce')
    base=avg.where(avg.notna(),trial+0.45)
    d['予測競走T']=(base + h*0.0018 - (d['総合点']-50)*0.00065).round(3)
    z=(d['総合点']-d['総合点'].max())/8.0
    p=np.exp(z); d['1着目安']=p/p.sum()*100
    d=d.sort_values(['総合点','試走T'],ascending=[False,True]).reset_index(drop=True)
    d.insert(0,'予測順位',np.arange(1,len(d)+1))
    marks=['◎','○','▲','△','注','×','']
    d.insert(1,'印',[marks[i] if i<len(marks) else '' for i in range(len(d))])
    return d

import re
from typing import Any
import numpy as np
import pandas as pd

MARKS = '◎○▲△×注☆★'

def clean_text(text: str) -> str:
    return str(text or '').replace('\u3000', ' ').replace('\r\n','\n').replace('\r','\n')

def normalize_name(s: str) -> str:
    s = re.sub(rf'^[{MARKS}\s]+', '', str(s or '').strip())
    return re.sub(r'\s+', '', s)

def first(patterns, text, flags=0):
    for p in patterns:
        m = re.search(p, text, flags)
        if m: return m.group(1)
    return None

def fnum(x):
    try: return float(x)
    except: return np.nan

def parse_race_meta(text: str) -> dict[str, Any]:
    compact=' '.join(x.strip() for x in clean_text(text).splitlines() if x.strip())
    out={}
    m=re.search(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日',compact)
    if m: out['開催日']=f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    m=re.search(r'(?:^|\s)(\d{1,2})R(?:\s|$)',compact,re.I)
    if m: out['レース']=int(m.group(1))
    m=re.search(r'(\d{1,2}:\d{2})\s*発走',compact)
    if m: out['発走時刻']=m.group(1)
    m=re.search(r'(\d{4})m\s+(\d+)車\s+(\d+)周',compact)
    if m: out.update({'距離':int(m.group(1)),'出走数':int(m.group(2)),'周回数':int(m.group(3))})
    m=re.search(r'(良走路|湿走路|斑走路|良|湿|斑)\s*/\s*(-?\d+(?:\.\d+)?)℃',compact)
    if m: out.update({'走路状態':m.group(1),'走路温度':float(m.group(2))})
    m=re.search(r'気温[:：]?\s*(-?\d+(?:\.\d+)?)℃?',compact)
    if m: out['気温']=float(m.group(1))
    m=re.search(r'湿度[:：]?\s*(\d+(?:\.\d+)?)%',compact)
    if m: out['湿度']=float(m.group(1))
    for venue in ['川口','伊勢崎','浜松','山陽','飯塚']:
        if venue in compact: out['開催場']=venue; break
    return out

def _is_player_start(lines, i):
    m=re.fullmatch(r'([1-8])\s+(.+)',lines[i])
    if not m or i+1>=len(lines): return False
    return bool(re.search(r'(?:[+-]?\d+|-)m\s*/\s*ST',lines[i+1],re.I))

def split_blocks(text: str):
    lines=[x.strip() for x in clean_text(text).splitlines() if x.strip()]
    starts=[i for i in range(len(lines)) if _is_player_start(lines,i)]
    blocks=[]
    for j,s in enumerate(starts):
        e=starts[j+1] if j+1<len(starts) else len(lines)
        m=re.fullmatch(r'([1-8])\s+(.+)',lines[s])
        blocks.append((int(m.group(1)),m.group(2),lines[s+1:e]))
    return blocks

def parse_entries(text: str) -> pd.DataFrame:
    rows=[]
    for car,name,lines in split_blocks(text):
        compact=' '.join(lines)
        hm=re.search(r'([+-]?\d+|-)m\s*/\s*ST\s*([+-]?\d?\.\d{2,3})',compact,re.I)
        handicap=0 if hm and hm.group(1)=='-' else (int(hm.group(1)) if hm else np.nan)
        st=fnum(hm.group(2)) if hm else np.nan
        trial=fnum(first([r'試\s*([3-9]\.\d{2,3})'],compact))
        rank=first([r'\b([SAB]-?\d+)\b'],compact)
        review=fnum(first([r'\(前\s*[SAB]-?\d+\)\s*([0-9]{2,3}\.\d{3})',r'\b[SAB]-?\d+\b\s*([0-9]{2,3}\.\d{3})'],compact))
        avg_trial=fnum(first([r'平均試走T\s*([3-9]\.\d{2,3})'],compact))
        avg_race=fnum(first([r'平均競走T\s*([3-9]\.\d{3})'],compact))
        best_race=fnum(first([r'最高競走T\s*([3-9]\.\d{3})'],compact))
        pct=[float(x) for x in re.findall(r'([0-9]+(?:\.[0-9]+)?)%',compact)]
        tail=pct[2:] if len(pct)>=2 else []
        rows.append({'車番':car,'選手名':normalize_name(name),'ハンデ':handicap,'ST':st,'試走T':trial,
                     '現ランク':rank,'審査P':review,'平均試走T':avg_trial,'平均競走T':avg_race,'最高競走T':best_race,
                     '2連対率':tail[0] if len(tail)>0 else np.nan,'3連対率':tail[1] if len(tail)>1 else np.nan,
                     '良2連対率':tail[2] if len(tail)>2 else np.nan,'良3連対率':tail[3] if len(tail)>3 else np.nan,
                     '湿2連対率':tail[4] if len(tail)>4 else np.nan,'湿3連対率':tail[5] if len(tail)>5 else np.nan})
    return pd.DataFrame(rows).sort_values('車番').reset_index(drop=True) if rows else pd.DataFrame()

def parse_page(text: str):
    return parse_race_meta(text), parse_entries(text)

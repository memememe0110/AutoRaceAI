from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import engine

st.set_page_config(page_title="AutoRaceAI スマホ本予測", page_icon="🏁", layout="wide")
st.title("🏁 AutoRaceAI スマホ本予測")
st.caption("Ver80｜順位列重複エラー修正・上へボタンをメインタブへ移動")

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
    """入力した選手名がDBに登録済みか、重複加算せずに確認する。"""
    key = normalize_player_key(name)
    result = {
        "found": False,
        "matched_name": "",
        "canonical_count": 0,
        "model_count": 0,
        "detail_count": 0,
        "latest": None,
        "candidates": [],
    }
    if not key or not Path(db_path).exists():
        return result

    with sqlite3.connect(db_path) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        names = {}

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
                item = names.setdefault(pkey, {
                    "names": [], "canonical": 0, "model": 0, "detail": 0, "dates": []
                })
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
                item = names.setdefault(pkey, {
                    "names": [], "canonical": 0, "model": 0, "detail": 0, "dates": []
                })
                item["names"].append(str(player_name))
                item["detail"] += int(count_all or 0)
                if latest:
                    item["dates"].append(str(latest))

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

        # 完全一致しない場合は、入力文字を含む近い候補だけ表示する。
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
    c1, c2, c3, c4 = st.columns(4)
    car_text = f"{int(info['top_car'])}番" if pd.notna(info.get("top_car")) else "不明"
    c1.metric("1着中心", car_text, f"1着率 {info['top1']:.2f}%")
    c2.metric("1位と2位の差", f"{info['gap']:.2f}pt")
    c3.metric("中心車の3着内率", f"{info['top3']:.2f}%")
    c4.metric("三連単1位の確率", f"{info['top_trifecta']:.2f}%")
    st.caption("自信度は、1着率の高さ・次点との差・3着内率・三連単確率の集中度をまとめた診断です。的中を保証する数値ではありません。")


def show_ticket_table(title: str, bets: dict, key: str, trials: int, top_n: int = 20) -> None:
    st.subheader(f"{title} 上位{top_n}")
    table = ticket_probability_table(bets, key, trials, top_n)
    if table.empty:
        st.info(f"{title}の集計結果がありません。")
        return
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "確率": st.column_config.NumberColumn("確率", format="%.2f%%"),
            "的中回数": st.column_config.NumberColumn("的中回数", format="%d回"),
        },
    )



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

    stats = engine.v72_ticket_feedback_stats(
        engine.DB_PATH,
        trifecta_outlier_cutoff=float(outlier_cutoff),
    )
    if stats.empty:
        st.info("結果照合データがまだありません。今後、予測後に結果を登録すると券種別の平均と強調ラインが育ちます。")
        return

    summary = stats[[
        "券種", "レース数", "大外し除外",
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

    tabs = st.tabs(["2連単", "2連複", "3連複", "3連単"])
    all_formation_text: dict[str, str] = {}
    for tab, bet_type in zip(tabs, ["2連単", "2連複", "3連複", "3連単"]):
        with tab:
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
                if within20_count > 0:
                    w20_value = float(r[f"20点以内{coverage}%カバー"])
                    st.caption(
                        f"過去20点以内的中：{within20_count}レース ／ "
                        f"{coverage}%地点の累積確率：{w20_value:.2f}%"
                    )
                    with st.expander(f"過去20点以内で的中した{within20_count}レースを確認"):
                        w20_details = engine.v81_trifecta_within20_details(engine.DB_PATH)
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
                    f"過去20点以内的中{coverage}%ライン：上位累積 {cutoff:.2f}%まで "
                    f"（三連単が上位20点以内で的中した過去{used}レースから計算）"
                )
                st.caption("このラインは『20点以内で当たるレースの累積位置』を見る指標で、全レースの的中率を表すものではありません。")
            elif line_mode == "実用ライン":
                cutoff = float(r[f"実用{coverage}%カバー"])
                used = int(r["実用レース数"])
                st.success(
                    f"実用{coverage}%カバーライン：上位累積 {cutoff:.2f}%まで "
                    f"（全{sample}レース中 {used}レース使用・三連単{int(outlier_cutoff)}%以上のレース{excluded}件を除外）"
                )
            else:
                cutoff = float(r[f"{coverage}%カバー"])
                st.info(f"全結果{coverage}%カバーライン：上位累積 {cutoff:.2f}%まで（{sample}レースすべて使用）")

            if excluded > 0:
                with st.expander(f"三連単{int(outlier_cutoff)}%以上で除外した{excluded}レースを確認"):
                    details = engine.v72_ticket_outlier_details(
                        bet_type,
                        engine.DB_PATH,
                        trifecta_outlier_cutoff=float(outlier_cutoff),
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

            m1, m2, m3 = st.columns(3)
            m1.metric("強調点数", f"{actual_points}点")
            m2.metric("強調範囲の累積", f"{actual_cover:.2f}%")
            m3.metric("ライン要求", f"{cutoff:.2f}%")
            if cap_enabled and original_points > actual_points:
                st.warning(
                    f"{coverage}%カバーラインには{original_points}点必要ですが、最大{int(cap_points)}点に制限しました。"
                    f" 現在の強調範囲は累積{actual_cover:.2f}%です。"
                )
            else:
                st.success(f"選択したラインを{actual_points}点・累積{actual_cover:.2f}%でカバーしています。")

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
                v73_copy_box(
                    f"{bet_type} フォーメーション",
                    formation_text,
                    f"{bet_type}_{coverage}_{line_mode}_{trifecta_line_mode}_{cap_enabled}_{cap_points}",
                    height=max(105, min(260, 44 + 28 * len(formations))),
                )
                st.caption("共通部分だけをまとめた簡易表記です。正確な対象は上の一覧表でも確認できます。")

    if all_formation_text:
        st.markdown('<div id="copy-all-formations"></div>', unsafe_allow_html=True)
        st.markdown("### 📋 全券種まとめてコピー")
        all_text = "\n\n".join(
            f"【{bet_type}】\n{all_formation_text[bet_type]}"
            for bet_type in ["2連単", "2連複", "3連複", "3連単"]
            if bet_type in all_formation_text
        )
        v73_copy_box(
            "強調対象フォーメーション一式",
            all_text,
            f"all_{coverage}_{line_mode}_{cap_enabled}_{cap_points}",
            height=max(180, min(420, 75 + 26 * all_text.count("\n"))),
        )
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


def show_odds_comparison(title: str, bets: dict, key: str, trials: int, widget_key: str, unordered: bool = False, namespace: str = "current") -> None:
    """各確率表の直下でオッズを入力し、再描画後も入力値を保持する。"""
    st.markdown(f"#### {title} オッズ入力")
    st.caption("確率上位の各行へオッズを直接入力できます。公平倍率との差のみを表示し、購入推奨は行いません。")

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

def render_last_result_analysis(view: dict) -> None:
    """登録後の解析結果を、再描画後もセッションから復元して表示する。"""
    if not isinstance(view, dict) or not view:
        return
    comparison = view.get("comparison")
    analysis = view.get("analysis") or {}
    adjustment = view.get("adjustment") or {}
    st.markdown("### 📌 直前に登録した結果解析")
    st.caption(f"登録キー: {view.get('key', '不明')}｜別タブ操作や再描画後も保持されます。")
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


# 「↑ 上へ」の着地点。タイトルではなく、操作を再開しやすいメインタブまで戻す。
st.markdown('<div id="main-tabs" style="scroll-margin-top:72px;"></div>', unsafe_allow_html=True)
prediction_tab, result_tab, register_tab, db_tab = st.tabs(["🏁 予測", "✅ 結果登録・解析", "👤 選手情報登録", "🗃️ 登録情報確認"])

with prediction_tab:
    st.info("Ver20予測方式：予測競走タイム＋高速6周イベントモデル。欠車・出走取消は存在しない選手として完全除外します。")
    with st.expander("🔧 今回どこを調整したか"):
        st.dataframe(engine.v36_get_adjustment_log(engine.DB_PATH), use_container_width=True, hide_index=True)
        st.caption("Ver20では10要素（試走・ST・ハンデ・近況・走路適性・前残り・追い込み・周回安定・コース適性・相手耐性）を評価します。三連単は順番まで完全一致した場合だけ的中です。1レースの変更幅は各項目±0.003以内です。")
    st.session_state.setdefault("prediction_input_version", 0)
    if st.button("🗑️ 予測入力をリセット", use_container_width=True, key="reset_prediction_input"):
        st.session_state["prediction_input_version"] += 1
        st.session_state.pop("last_prediction_view", None)
        st.rerun()
    prediction_version = st.session_state["prediction_input_version"]
    text = st.text_area(
        "公式出走表を全文貼り付け",
        height=430,
        placeholder="autorace.jpの出走表をコピーして貼り付け",
        key=f"race_card_text_{prediction_version}",
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
            prediction_venue_override = st.selectbox(
                "開催場（出走表から取得できないため選択してください）",
                options=["", "川口", "伊勢崎", "浜松", "山陽", "飯塚"],
                format_func=lambda value: "選択してください" if value == "" else value,
                key=f"prediction_venue_override_{prediction_version}",
            )
            st.caption("選手の所属場は開催場として使いません。実際の開催場を選択してください。")

    # Ver64: 予測実行前に出走表の読み取り結果を確認できるようにする。
    if text.strip():
        try:
            preview_entries = engine.v15_parse_entries(text)
            if isinstance(preview_entries, pd.DataFrame) and not preview_entries.empty:
                preview_cols = [c for c in [
                    "車番", "選手名", "所属", "ハンデ", "試走T", "ST",
                    "試走偏差", "現ランク", "平均競走T", "最高競走T",
                    "近10走着順", "近10走2連", "近10走3連", "車名"
                ] if c in preview_entries.columns]
                with st.expander(f"📋 出走表の読み取り確認（{len(preview_entries)}名）", expanded=False):
                    st.dataframe(preview_entries[preview_cols], use_container_width=True, hide_index=True)
                    if preview_entries["車番"].nunique() < 8:
                        st.warning("8車すべてを取得できていません。貼り付け範囲を確認してください。")
            else:
                st.warning("出走表から選手を読み取れませんでした。ページ全体をコピーして貼り付けてください。")
        except Exception as exc:
            st.warning(f"出走表の事前確認に失敗しました: {exc}")

    manual_excluded = []
    if text.strip():
        auto_excluded = {int(car): "手動指定" for car in manual_excluded}
        # 欠車表示を一度除去して全車番を取得し、ユーザーが状態を上書きできるようにする。
        status_removed = re.sub(r"(?m)^\s*(欠車|出走取消|出走取り消し|不出走|除外|参加解除)\s*$", "", text)
        normalized = re.sub(r"(?m)^\s*ハンデ\s*", "", status_removed)
        all_entries = engine.v152_parse_vertical_entries(engine.v15_clean_text(normalized))
        available_cars = sorted(all_entries["車番"].dropna().astype(int).unique().tolist()) if not all_entries.empty else list(range(1, 9))
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

    prediction_clicked = st.button("解析して元版設定で予測", type="primary", use_container_width=True)
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
            with st.spinner("高速6周イベントシミュレーションを実行中…"):
                df, bets, output, entries, meta = engine.ver16_run_prediction(prediction_text, int(trials), int(seed), manual_excluded=manual_excluded)
                finish_prob = engine.v30_finish_probabilities(df, bets, int(trials))
                race_key = engine.v34_save_prediction_snapshot(meta, df, finish_prob, engine.DB_PATH)
                engine.v67_save_ticket_snapshot(meta, bets, int(trials), engine.DB_PATH)
                engine.v40_save_prediction_features(meta, df, engine.DB_PATH)
            # オッズ入力などによる再描画後も、直前の予測結果を保持する。
            st.session_state["last_prediction_view"] = {
                "df": df,
                "bets": bets,
                "output": output,
                "entries": entries,
                "meta": meta,
                "finish_prob": finish_prob,
                "race_key": race_key,
                "trials": int(trials),
                "excluded": [int(x) for x in manual_excluded],
                "learning_boundary": engine.v61_learning_boundary_summary(),
                "future_audit": engine.v68_get_latest_future_audit(),
            }
            st.success("予測が完了しました")
        except Exception as exc:
            st.error(f"予測エラー: {type(exc).__name__}: {exc}")
            st.exception(exc)

    view = st.session_state.get("last_prediction_view")
    if view:
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
            st.subheader("解析した出走表")
            st.dataframe(entries.drop(columns=["_raw"], errors="ignore"), use_container_width=True, hide_index=True)
            show_player_data_coverage(entries)

            cols = [c for c in [
                "改善後順位", "車", "選手名", "ハンデ", "試走換算", "予測競走T", "レース信頼度",
                "基礎スピード点", "実戦能力点", "勝負強さ点", "展開適性点",
                "スタート伸び指数", "ゴール前伸び指数", "安定上位指数",
                "混戦突破適性", "逃げ判定", "初周先頭推定", "逃げ残り推定", "逃切り推定", "逃げ履歴件数", "逃げ履歴補正",
                "展開タイプ_実測", "展開履歴件数", "展開学習補正", "熱走路帯", "高温履歴件数", "熱走路適性", "熱走路学習補正", "Ver60総合補正",
                "同ハンデ内枠補正", "車中期成績補正", "試走偏差補正", "高温位置補正", "Ver24展開穴補正", "選手別条件適性補正", "条件適性根拠", "条件一致最大件数", "条件適性信頼度", "改善後総合点",
            ] if c in df.columns]
            result = df[cols].sort_values(["改善後順位", "車"]).reset_index(drop=True)
            st.subheader("予測順位")
            st.dataframe(result, use_container_width=True, hide_index=True)

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
            ticket_tabs = st.tabs(["2連単", "2連複", "3連複", "3連単"])
            with ticket_tabs[0]:
                show_ticket_table("2連単（2車単）確率", bets, "2車単", view_trials, 20)
                show_odds_comparison("2連単", bets, "2車単", view_trials, "2tansho", unordered=False, namespace=odds_namespace)
            with ticket_tabs[1]:
                show_ticket_table("2連複（2車複）確率", bets, "2車複", view_trials, 20)
                show_odds_comparison("2連複", bets, "2車複", view_trials, "2fuku", unordered=True, namespace=odds_namespace)
            with ticket_tabs[2]:
                show_ticket_table("3連複確率", bets, "三連複", view_trials, 20)
                show_odds_comparison("3連複", bets, "三連複", view_trials, "3fuku", unordered=True, namespace=odds_namespace)
            with ticket_tabs[3]:
                show_ticket_table("3連単確率", bets, "三連単", view_trials, 20)
                show_odds_comparison("3連単", bets, "三連単", view_trials, "3tan", unordered=False, namespace=odds_namespace)

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

with result_tab:
    st.subheader("公式結果を登録して予測と比較")
    st.info("結果ページを先頭のレース番号から払戻金まで全文コピーして貼り付けます。縦型の着順表、6周のグランドノート、払戻金にも対応します。")
    st.session_state.setdefault("result_input_version", 0)
    if st.button("🗑️ 結果入力をリセット", use_container_width=True, key="reset_result_input"):
        st.session_state["result_input_version"] += 1
        for key in ["v35_result_meta", "v35_result_rows", "v35_result_laps", "v35_result_payouts", "v41_last_result_view"]:
            st.session_state.pop(key, None)
        st.rerun()
    result_version = st.session_state["result_input_version"]
    c1, c2 = st.columns(2)
    venue_override = c1.text_input("開催場（本文から取れない場合のみ）", key=f"result_venue_{result_version}")
    race_no_override = c2.text_input("レース番号（本文から取れない場合のみ）", key=f"result_race_no_{result_version}")
    result_text = st.text_area(
        "公式結果ページを全文貼り付け",
        height=620,
        key=f"official_result_text_{result_version}",
        placeholder="6R\n確定\n2026年7月21日(火)\n…\n着順 車番 選手名\n…\nグランドノート\n…\n払戻金\n…",
    )
    if st.button("結果を解析", use_container_width=True):
        try:
            meta_r, rows_r, laps_r, payouts_r = engine.v35_parse_result_text(
                result_text, venue_override, race_no_override
            )
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

    if isinstance(rows_r, pd.DataFrame) and not rows_r.empty:
        st.write("解析したレース情報", meta_r)
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

        button_label = "登録済み結果を置き換えて再解析" if replace_registered else "DBへ登録して予測差・展開を解析"
        button_disabled = bool(result_exists and not replace_registered)
        if st.button(button_label, type="primary", use_container_width=True, disabled=button_disabled):
            try:
                if replace_registered:
                    key, comparison, analysis, adjustment, registration = engine.v70_replace_registered_result(
                        meta_r, rows_r, laps_r, payouts_r, engine.DB_PATH
                    )
                else:
                    key, comparison, analysis, adjustment, registration = engine.v41_register_result(
                        meta_r, rows_r, laps_r, payouts_r, engine.DB_PATH
                    )
                ticket_analysis = engine.v67_analyze_ticket_result(meta_r, rows_r, engine.DB_PATH)
                if registration.get("duplicate"):
                    st.warning(analysis.get("message", "このレースは登録済みです。"))
                    st.stop()
                if registration.get("replaced"):
                    st.success(f"登録済み結果を置き換えました: {key}")
                else:
                    st.success(f"結果を登録しました: {key}")
                if registration.get("learning_excluded"):
                    st.warning(
                        "⚠️ 事故レースのためAI学習対象外です。"
                        f" 理由: {registration.get('learning_exclusion_reason') or '事故・異常終了'}。"
                        "結果・払戻金・グランドノートは保存しましたが、選手履歴学習・展開学習・重み更新には使いません。"
                    )
                show_v67_result_analysis(ticket_analysis)
                predicted_trifecta_saved = ""
                actual_trifecta_saved = ""
                if "message" not in analysis:
                    predicted_trifecta_saved = "→".join(map(str, comparison.sort_values("predicted_rank")["車番"].head(3).astype(int)))
                    actual_trifecta_saved = "→".join(map(str, rows_r.sort_values("着順")["車番"].head(3).astype(int)))
                st.session_state["v41_last_result_view"] = {
                    "key": key,
                    "comparison": comparison,
                    "analysis": analysis,
                    "adjustment": adjustment,
                    "predicted_trifecta": predicted_trifecta_saved,
                    "actual_trifecta": actual_trifecta_saved,
                }
                if "message" in analysis:
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
                if "message" not in analysis:
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
                h1, h2, h3 = st.columns(3)
                h1.metric("新規履歴", analysis.get("履歴追加", 0))
                h2.metric("重複スキップ", analysis.get("履歴重複スキップ", 0))
                h3.metric("周回順位", analysis.get("周回履歴保存", 0))
                st.caption("結果登録した競走T・試走T・ST・着順・ハンデ・走路条件は、次回以降の予測用選手履歴へ反映されます。")
                st.caption("同一判定は開催日・開催場・レース番号で行います。レース名称は判定に使いません。同じレースは通常登録では重複を防止します。再登録を選んだ場合だけ、古い結果を今回の内容へ置き換えます。")
                st.caption(f"順位分析対象: {analysis.get('分析対象', 0)}名 / 除外: {analysis.get('分析除外', 0)}名。着順なし・欠車・中止・失格などは順位分析から除外します。")
                ok, msg = push_db_to_github(f"AutoRaceAI: {key} 結果・周回・払戻登録")
                (st.success if ok else st.warning)(msg)
            except Exception as exc:
                st.error(f"結果登録エラー: {type(exc).__name__}: {exc}")
                st.exception(exc)

    last_result_view = st.session_state.get("v41_last_result_view")
    if last_result_view:
        st.divider()
        render_last_result_analysis(last_result_view)


with db_tab:
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
    hist74=engine.v74_optimization_history(engine.DB_PATH,20)
    if not hist74.empty:
        with st.expander("過去の最適化履歴", expanded=False):
            st.dataframe(hist74,use_container_width=True,hide_index=True)

    st.divider()
    st.subheader("学習重み・変更履歴")
    st.dataframe(engine.v40_current_weights(engine.DB_PATH), use_container_width=True, hide_index=True,
        column_config={"現在の重み":st.column_config.NumberColumn(format="%.4f"),"初期値":st.column_config.NumberColumn(format="%.4f"),"初期値からの差":st.column_config.NumberColumn(format="%+.4f")})
    history_df=engine.v39_weight_history(engine.DB_PATH,100)
    if history_df.empty:
        st.caption("重み変更履歴はまだありません。")
    else:
        st.dataframe(history_df,use_container_width=True,hide_index=True)
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

with register_tab:
    st.subheader("選手情報を登録")
    st.session_state.setdefault("player_input_version", 0)
    if st.button("🗑️ 選手入力をリセット", use_container_width=True, key="reset_player_input"):
        st.session_state["player_input_version"] += 1
        st.session_state.pop("parsed_player_history", None)
        st.rerun()
    player_version = st.session_state["player_input_version"]
    player_name = st.text_input("選手名", placeholder="例：横田翔", key=f"player_name_input_{player_version}")
    show_player_registration_status(player_name)
    history_text = st.text_area(
        "公式プロフィールの直近履歴を貼り付け",
        height=520,
        placeholder="前走\n4\n2026年7月21日\n伊勢崎\n予選\n…",
        key=f"player_history_text_{player_version}",
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
                report = engine.v47_save_player_history(parsed, db_path=engine.DB_PATH)
                st.session_state["pending_player_history"] = report["pending"]
                changed, skipped, pending_count = report["changed"], report["skipped"], report["pending_count"]
                if changed:
                    ok, msg = push_db_to_github(f"AutoRaceAI: {player_name.strip()} の履歴を{changed}件追加・更新")
                    text = f"読込 {report['read']}件｜追加・更新 {changed}件｜重複処理 {skipped}件（数値完全一致 {report.get('exact_duplicate_skipped', 0)}件）｜保留 {pending_count}件"
                    (st.success if ok else st.warning)(text + (f"｜{msg}" if msg else ""))
                else:
                    st.info(f"読込 {report['read']}件｜追加・更新 0件｜保留 {pending_count}件")
            except Exception as exc:
                st.error(f"登録エラー: {type(exc).__name__}: {exc}")
                st.exception(exc)

    pending = st.session_state.get("pending_player_history")
    if isinstance(pending, pd.DataFrame) and not pending.empty:
        st.markdown("### ⚠️ R・必須項目の入力待ち")
        st.caption("R候補は参考表示です。数値が完全一致していてRだけ違う場合は、「既存Rへ統合」または「入力したRで新規登録」を選択してください。Rを空欄のままにすると保留されます。")
        edit_cols = [c for c in ["選手名","開催日","開催場","レース","R候補","重複処理","レース名","着順","車番","走路","ハンデ","試走T","競走T","ST","保留理由"] if c in pending.columns]
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
        if st.button("不足行だけ登録", type="primary", use_container_width=True, key=f"save_pending_{player_version}"):
            try:
                # 編集対象以外の元カラムも保持して戻す
                repaired = pending.copy()
                for c in edited.columns:
                    if c != "保留理由": repaired[c] = edited[c].values
                repaired = repaired.drop(columns=["保留理由"], errors="ignore")

                # 数値完全一致なのにRが異なる行は、利用者の選択後だけ保存する。
                unresolved = pd.Series(False, index=repaired.index)
                if "重複処理" in repaired.columns:
                    conflict_mask = pending.get("保留理由", pd.Series("", index=pending.index)).astype(str).str.contains("Rが異なります", na=False)
                    unresolved = conflict_mask & repaired["重複処理"].fillna("選択してください").eq("選択してください")
                    repaired["_v58_duplicate_confirmed"] = ~unresolved
                if unresolved.any():
                    st.warning(f"Rが異なる重複候補 {int(unresolved.sum())}件の登録方法を選択してください。")
                    st.stop()

                report2 = engine.v47_save_player_history(repaired, db_path=engine.DB_PATH)
                st.session_state["pending_player_history"] = report2["pending"]
                if report2["changed"]:
                    ok, msg = push_db_to_github(f"AutoRaceAI: {player_name.strip()} の保留履歴を{report2['changed']}件登録")
                    (st.success if ok else st.warning)(f"不足行を{report2['changed']}件登録しました。残り保留 {report2['pending_count']}件。{msg}")
                elif report2["pending_count"]:
                    st.warning(f"まだ必須項目が不足しています。残り保留 {report2['pending_count']}件。")
                else:
                    st.info("登録対象の変更はありませんでした。")
                st.rerun()
            except Exception as exc:
                st.error(f"不足行登録エラー: {type(exc).__name__}: {exc}")
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

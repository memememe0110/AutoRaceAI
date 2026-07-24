from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st

import engine

st.set_page_config(page_title="AutoRaceAI スマホ本予測", page_icon="🏁", layout="wide")
st.title("🏁 AutoRaceAI スマホ本予測")
st.caption("Ver19｜ハンデ補正適正化・追い込み型の外枠評価改善")


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

prediction_tab, result_tab, register_tab, db_tab = st.tabs(["🏁 予測", "✅ 結果登録・解析", "👤 選手情報登録", "🗃️ 登録情報確認"])

with prediction_tab:
    st.info("Ver19予測方式：予測競走タイム＋高速6周イベントモデル。欠車・出走取消は存在しない選手として完全除外します。")
    with st.expander("🔧 今回どこを調整したか"):
        st.dataframe(engine.v36_get_adjustment_log(engine.DB_PATH), use_container_width=True, hide_index=True)
        st.caption("Ver19では10要素（試走・ST・ハンデ・近況・走路適性・前残り・追い込み・周回安定・コース適性・相手耐性）を評価します。三連単は順番まで完全一致した場合だけ的中です。1レースの変更幅は各項目±0.003以内です。")
    if st.button("🗑️ 予測入力をリセット", use_container_width=True, key="reset_prediction_input"):
        for key in ["race_card_text"]:
            st.session_state.pop(key, None)
        st.rerun()
    text = st.text_area(
        "公式出走表を全文貼り付け",
        height=430,
        placeholder="autorace.jpの出走表をコピーして貼り付け",
        key="race_card_text",
    )

    if st.button("解析して元版設定で予測", type="primary", use_container_width=True):
        if not text.strip():
            st.warning("出走表を貼り付けてください。")
            st.stop()
        try:
            with st.spinner("高速6周イベントシミュレーションを実行中…"):
                df, bets, output, entries, meta = engine.ver16_run_prediction(text, int(trials), int(seed))
            st.success("予測が完了しました")
            excluded = engine.v17_detect_nonstarters(text)
            if excluded:
                detail = "、".join(f"{car}番（{status}）" for car, status in sorted(excluded.items()))
                st.warning(f"解析対象外: {detail}。確率・順位・買い目の組み合わせから完全に除外しました。")
            st.caption(f"実出走数: {len(entries)}車 / 三連単組み合わせ数: {len(entries)*(len(entries)-1)*(len(entries)-2)}通り")
            st.subheader("解析した出走表")
            st.dataframe(entries.drop(columns=["_raw"], errors="ignore"), use_container_width=True, hide_index=True)

            cols = [c for c in [
                "改善後順位", "車", "選手名", "ハンデ", "試走換算", "予測競走T", "レース信頼度",
                "基礎スピード点", "実戦能力点", "勝負強さ点", "展開適性点",
                "スタート伸び指数", "ゴール前伸び指数", "安定上位指数",
                "混戦突破適性", "改善後総合点",
            ] if c in df.columns]
            result = df[cols].sort_values(["改善後順位", "車"]).reset_index(drop=True)
            st.subheader("予測順位")
            st.dataframe(result, use_container_width=True, hide_index=True)

            st.subheader("6周の代表展開")
            lap_df = engine.v30_representative_lap_projection(df)
            st.dataframe(lap_df, use_container_width=True, hide_index=True)
            st.caption("確率計算は全試行で、スタート・中盤・最終周のイベントを生成しています。この表は指標から作った代表的な1展開です。")

            finish_prob = engine.v30_finish_probabilities(df, bets, int(trials))
            race_key = engine.v34_save_prediction_snapshot(meta, df, finish_prob, engine.DB_PATH)
            engine.v40_save_prediction_features(meta, df, engine.DB_PATH)
            st.caption(f"予測保存キー: {race_key}（結果登録時の比較・重み調整に使用）")

            with st.expander("🧪 学習重みによる順位変化"):
                compare_cols = [c for c in ["車","選手名","調整前順位","改善後順位","調整前総合点","学習重み補正","改善後総合点","主な評価理由"] if c in df.columns]
                st.dataframe(df[compare_cols].sort_values("改善後順位"), use_container_width=True, hide_index=True)
                st.caption("調整前は元モデル、改善後は保存済み学習重みを小さく加えた順位です。")
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
            scenario_df = pd.DataFrame([
                {"展開": name, "確率": prob * 100}
                for name, prob in sorted(scenario_probs.items(), key=lambda x: x[1], reverse=True)
            ])
            st.subheader("展開予想")
            st.dataframe(
                scenario_df,
                use_container_width=True,
                hide_index=True,
                column_config={"確率": st.column_config.NumberColumn("確率", format="%.1f%%")},
            )
            top_scenario = scenario_df.iloc[0]["展開"] if not scenario_df.empty else "不明"
            st.caption(f"中心展開：{top_scenario}。Ver15.2のシミュレーションで使う4展開の事前確率です。")

            st.subheader("今回条件の反映状況")
            condition_rows = [
                {"条件": "走路状態", "入力値": meta.get("走路状態") or "未取得", "反映": "直接反映（履歴の走路適合重み）"},
                {"条件": "走路温度", "入力値": f"{meta.get('走路温度')}℃" if pd.notna(meta.get("走路温度")) else "未取得", "反映": "直接反映（6周展開・変動幅）"},
                {"条件": "気温", "入力値": f"{meta.get('気温')}℃" if pd.notna(meta.get("気温")) else "未取得", "反映": "取得・保存・類似レース検索（直接補正は未実装）"},
                {"条件": "湿度", "入力値": f"{meta.get('湿度')}%" if pd.notna(meta.get("湿度")) else "未取得", "反映": "取得・保存・類似レース検索（直接補正は未実装）"},
            ]
            st.dataframe(pd.DataFrame(condition_rows), use_container_width=True, hide_index=True)

            ticket_tabs = st.tabs(["2連単", "2連複", "3連複", "3連単"])
            with ticket_tabs[0]:
                show_ticket_table("2連単（2車単）確率", bets, "2車単", int(trials), 20)
            with ticket_tabs[1]:
                show_ticket_table("2連複（2車複）確率", bets, "2車複", int(trials), 20)
            with ticket_tabs[2]:
                show_ticket_table("3連複確率", bets, "三連複", int(trials), 20)
            with ticket_tabs[3]:
                show_ticket_table("3連単確率", bets, "三連単", int(trials), 20)

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

with result_tab:
    st.subheader("公式結果を登録して予測と比較")
    st.info("結果ページを先頭のレース番号から払戻金まで全文コピーして貼り付けます。縦型の着順表、6周のグランドノート、払戻金にも対応します。")
    if st.button("🗑️ 結果入力をリセット", use_container_width=True, key="reset_result_input"):
        for key in ["result_venue", "result_race_no", "official_result_text", "v35_result_meta", "v35_result_rows", "v35_result_laps", "v35_result_payouts", "v41_last_result_view"]:
            st.session_state.pop(key, None)
        st.rerun()
    c1, c2 = st.columns(2)
    venue_override = c1.text_input("開催場（本文から取れない場合のみ）", key="result_venue")
    race_no_override = c2.text_input("レース番号（本文から取れない場合のみ）", key="result_race_no")
    result_text = st.text_area(
        "公式結果ページを全文貼り付け",
        height=620,
        key="official_result_text",
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

        if st.button("DBへ登録して予測差・展開を解析", type="primary", use_container_width=True):
            try:
                key, comparison, analysis, adjustment, registration = engine.v41_register_result(
                    meta_r, rows_r, laps_r, payouts_r, engine.DB_PATH
                )
                if registration.get("duplicate"):
                    st.warning(analysis.get("message", "このレースは登録済みです。"))
                    st.stop()
                st.success(f"結果を登録しました: {key}")
                st.session_state["v41_last_result_view"] = {
                    "key": key,
                    "comparison": comparison,
                    "analysis": analysis,
                    "adjustment": adjustment,
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
                st.caption("同一判定は開催日・開催場・レース番号で行います。レース名称は判定に使いません。同じレースは履歴追加も重み更新も行いません。")
                st.caption(f"順位分析対象: {analysis.get('分析対象', 0)}名 / 除外: {analysis.get('分析除外', 0)}名。着順なし・欠車・中止・失格などは順位分析から除外します。")
                ok, msg = push_db_to_github(f"AutoRaceAI: {key} 結果・周回・払戻登録")
                (st.success if ok else st.warning)(msg)
            except Exception as exc:
                st.error(f"結果登録エラー: {type(exc).__name__}: {exc}")
                st.exception(exc)

with db_tab:
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
    if st.button("🗑️ 選手入力をリセット", use_container_width=True, key="reset_player_input"):
        for key in ["player_name_input", "player_history_text", "parsed_player_history"]:
            st.session_state.pop(key, None)
        st.rerun()
    player_name = st.text_input("選手名", placeholder="例：横田翔", key="player_name_input")
    history_text = st.text_area(
        "公式プロフィールの直近履歴を貼り付け",
        height=520,
        placeholder="前走\n4\n2026年7月21日\n伊勢崎\n予選\n…",
        key="player_history_text",
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
            "選手名", "開催日", "開催場", "レース種別", "着順", "天候", "走路",
            "走路温度", "気温", "湿度", "車番", "ハンデ", "距離", "周回数",
            "人気", "競走T", "試走T", "ST"
        ] if c in parsed.columns]
        st.dataframe(parsed[preview_cols], use_container_width=True, hide_index=True, height=420)

        if st.button("DBへ登録してGitHubに保存", type="primary", use_container_width=True):
            try:
                inserted, skipped = engine.v15_save_player_history(parsed, db_path=engine.DB_PATH)
                if inserted:
                    ok, msg = push_db_to_github(f"AutoRaceAI: {player_name.strip()} の履歴を{inserted}件登録")
                    if ok:
                        st.success(f"{inserted}件登録、{skipped}件は重複のためスキップしました。{msg}")
                    else:
                        st.warning(
                            f"DBには{inserted}件登録しましたが、GitHub保存は未完了です。{msg}\n"
                            "消失防止のため、サイドバーの『DBを端末へ保存』も使ってください。"
                        )
                else:
                    st.info(f"新規登録は0件です。{skipped}件すべて登録済みでした。")
            except Exception as exc:
                st.error(f"登録エラー: {type(exc).__name__}: {exc}")
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
                            SELECT h.race_date AS 日付, h.venue AS 開催場, h.race_no AS レース,
                                   h.finish AS 着順, h.surface AS 走路, h.handicap AS ハンデ,
                                   h.trial_time AS 試走T, h.race_time AS 競走T,
                                   h.start_time AS ST, h.source AS 登録元, h.created_at AS 登録日時
                            FROM race_history h JOIN players p ON p.player_id=h.player_id
                            WHERE p.player_name=? ORDER BY h.race_date DESC, h.history_id DESC
                            """, con, params=(selected,))
                    else:
                        history = pd.read_sql_query(
                            """
                            SELECT race_date AS 日付, venue AS 開催場, race_type AS レース種別,
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
                    st.dataframe(history, use_container_width=True, hide_index=True, height=430)

                st.divider()
                st.subheader("DBメンテナンス")
                st.caption("姓名の空白違いを統合し、レース名が『一般戦』『7R』など違っていても、同じ走行結果なら重複を整理します。")
                if st.button("氏名・同一レースの重複をまとめて整理", use_container_width=True):
                    result = engine.v32_merge_duplicate_players(engine.DB_PATH)
                    race_result = engine.v33_cleanup_duplicate_histories(engine.DB_PATH)
                    ok, msg = push_db_to_github("AutoRaceAI: 氏名と同一走行結果の重複を整理")
                    summary = (
                        f"選手 {result['merged_players']}件を統合、履歴 {result['moved_histories']}件を移動、"
                        f"氏名統合時の重複 {result['deleted_histories']}件、同一走行履歴 {race_result['deleted_histories']}件、"
                        f"詳細履歴 {race_result['deleted_imports']}件を削除しました。"
                    )
                    if ok:
                        st.success(summary + " " + msg)
                    else:
                        st.warning(summary + " GitHub保存は未完了です。" + msg)
                    st.rerun()

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

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
st.caption("Ver15.2詳細6周シミュレーション・選手履歴登録・GitHub DB保存対応版")


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

prediction_tab, register_tab, db_tab = st.tabs(["🏁 予測", "👤 選手情報登録", "🗃️ 登録情報確認"])

with prediction_tab:
    st.info("予測方式：Ver15.2の詳細6周モデル（simulate_detailed）。各試行で隊列変化を計算し、確率を集計します。")
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
            with st.spinner("6周詳細シミュレーションを実行中…"):
                df, bets, output, entries, meta = engine.ver16_run_prediction(text, int(trials), int(seed))
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

            st.subheader("6周の代表展開")
            lap_df = engine.v30_representative_lap_projection(df)
            st.dataframe(lap_df, use_container_width=True, hide_index=True)
            st.caption("確率計算は全試行で6周詳細モデルを実行しています。この表は、その指標から作った見やすい代表的な1展開です。")

            finish_prob = engine.v30_finish_probabilities(df, bets, int(trials))
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

            total = int(trials)
            top = sorted(bets["三連単"].items(), key=lambda x: x[1], reverse=True)[:20]
            tri = pd.DataFrame([
                {"順位": i, "三連単": "-".join(map(str, combo)), "確率": (count / total) * 100}
                for i, (combo, count) in enumerate(top, 1)
            ])
            st.subheader("三連単確率 上位20")
            st.dataframe(
                tri,
                use_container_width=True,
                hide_index=True,
                column_config={"確率": st.column_config.NumberColumn("確率", format="%.2f%%")},
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

with register_tab:
    st.subheader("選手情報を登録")
    player_name = st.text_input("選手名", placeholder="例：横田翔")
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

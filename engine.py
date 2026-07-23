from __future__ import annotations
import io, math, re, sqlite3, hashlib, time, traceback
from collections import Counter
from datetime import datetime, date
from pathlib import Path
import numpy as np
import pandas as pd
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import to_excel

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "autorace_players.sqlite3"
DB_DIR = APP_DIR
HISTORY_COLS_DB = ['開催日','開催場','レース','着順','出走','走路','ハンデ','試走T','競走T','ST']

def mount_and_init_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            player_id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS race_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            race_date TEXT, venue TEXT, race_no TEXT, finish REAL, starters REAL,
            surface TEXT, handicap TEXT, trial_time REAL, race_time REAL, start_time REAL,
            result_status TEXT NOT NULL DEFAULT '通常',
            use_for_model INTEGER NOT NULL DEFAULT 1,
            source TEXT, record_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(player_id) REFERENCES players(player_id)
        );
        CREATE INDEX IF NOT EXISTS idx_history_player_date ON race_history(player_id, race_date DESC);
        """)
    return DB_PATH

mount_and_init_db()

HISTORY_COLUMNS = [
    "開催日", "開催場", "レース", "着順", "出走",
    "走路", "ハンデ", "試走T", "競走T", "ST"
]



def same_or_stronger_repass_penalty(chaser_strength, leader_strength):
    """
    一度前へ出た相手が同レベル以上なら、抜き返しに必要な差を大きくする。
    leader_strength >= chaser_strength のとき強い追加障壁を与える。
    """
    chaser = float(chaser_strength)
    leader = float(leader_strength)
    gap = leader - chaser

    if gap >= 5.0:
        return 2.4
    if gap >= 2.0:
        return 1.8
    if gap >= 0.0:
        return 1.35
    if gap >= -2.0:
        return 0.75
    return 0.25


def uploaded_file(value):
    if not value:
        return None, None
    if isinstance(value, dict):
        name = next(iter(value))
        return name, bytes(value[name]["content"])
    item = value[0]
    return item["name"], bytes(item["content"])


def text(v):
    return "" if v is None else str(v).strip()


def number(v, default=np.nan):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default


def handicap_number(v, default=np.nan):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else default


def excel_serial(v):
    if v is None or v == "":
        return np.nan
    if isinstance(v, datetime):
        base = datetime(1899, 12, 30)
        return float((v - base).days)
    if isinstance(v, date):
        base = date(1899, 12, 30)
        return float((v - base).days)
    return number(v)


def mean_or_blank(values):
    vals = [float(v) for v in values if not pd.isna(v)]
    return float(np.mean(vals)) if vals else np.nan


def sample_std(values):
    vals = [float(v) for v in values if not pd.isna(v)]
    return float(np.std(vals, ddof=1)) if len(vals) >= 2 else np.nan


def find_header(ws):
    for r in range(1, min(ws.max_row, 20) + 1):
        vals = [text(ws.cell(r, c).value) for c in range(1, 20)]
        if all(col in vals for col in HISTORY_COLUMNS):
            return r, {col: vals.index(col) + 1 for col in HISTORY_COLUMNS}
    raise ValueError(f"{ws.title}: 履歴10列の見出しが見つかりません")


def read_history(ws):
    header_row, col_map = find_header(ws)
    rows = []

    for r in range(header_row + 1, ws.max_row + 1):
        raw = {col: ws.cell(r, col_map[col]).value for col in HISTORY_COLUMNS}
        if all(v in (None, "") for v in raw.values()):
            continue

        trial = number(raw["試走T"])
        race_t = number(raw["競走T"])

        rows.append({
            "開催日": excel_serial(raw["開催日"]),
            "開催場": text(raw["開催場"]),
            "レース": text(raw["レース"]),
            "着順": number(raw["着順"]),
            "出走": number(raw["出走"]),
            "走路": text(raw["走路"]),
            "ハンデ": text(raw["ハンデ"]),
            "ハンデ数値": handicap_number(raw["ハンデ"]),
            "試走T": trial,
            "競走T": race_t,
            "ST": number(raw["ST"]),
            "タイム差": (
                race_t - trial
                if not pd.isna(trial)
                and not pd.isna(race_t)
                and (race_t - trial) > 0
                else np.nan
            ),
            "有効": (
                not pd.isna(trial)
                and not pd.isna(race_t)
                and (race_t - trial) > 0
            ),
        })

    return pd.DataFrame(rows)


def read_race(ws):
    labels = {}
    for r in range(1, min(ws.max_row, 30) + 1):
        key = text(ws.cell(r, 1).value)
        if key:
            labels[key] = ws.cell(r, 2).value

    return {
        "開催日": excel_serial(labels.get("レース開催日")),
        "開催場": text(labels.get("今回の開催場")),
        "走路": text(labels.get("今回の走路")),
    }


def read_settings(ws):
    # 元Excelでは設定値の位置が固定
    def b(row, default):
        return number(ws.cell(row, 2).value, default)

    return {
        "最近重視日数": b(4, 30),
        "同一開催場倍率": b(5, 1.35),
        "別開催場倍率": b(6, 0.85),
        "同一走路倍率": b(7, 1.5),
        "別走路倍率": b(8, 0.7),
        "同一ハンデ倍率": b(9, 1.4),
        "近接ハンデ倍率": b(10, 1.1),
        "遠隔ハンデ倍率": b(11, 0.75),
        "ST補正係数": b(12, 0.04),
        "10m換算秒": b(22, 0.018),

        "試走点": b(29, 24),
        "最近5走点": 10.0,
        "ST点": b(31, 8),
        "ハンデ適性点": b(32, 8),
        "開催場適性点": b(33, 5),
        "走路適性点": b(34, 5),
        "位置取り点": b(35, 6),
        "着順指数点": b(36, 8),
        "勝率点": b(37, 6),
        "連対率点": b(38, 4),
        "上昇度点": b(39, 2),
        "再現性点": b(40, 1),
        "今回ハンデ勝率点": 4.0,

        "ハンデ改善点": b(43, 6),
        "満点改善幅": b(44, 20),
        "前走比率": b(45, 0.6),
        "3着以内率点": b(49, 6),
        "平均着順点": b(50, 6),
        "着順安定度点": b(51, 4),
        "審査Pランク点": b(52, 6),
    }


def current_player(ws):
    return {
        "選手名": text(ws["B2"].value) or ws.title,
        "今回試走T": number(ws["E2"].value),
        "今回ハンデ": handicap_number(ws["G2"].value),
        "今回走路": text(ws["E3"].value),
        "審査P": number(ws["I2"].value),
        "現ランク": text(ws["I3"].value).upper(),
    }


def normalize_surface(value):
    """走路表記を 良・斑・湿 にそろえる。"""
    s = text(value)
    if "良" in s:
        return "良"
    if "斑" in s:
        return "斑"
    if "湿" in s:
        return "湿"
    return s


def surface_compatibility_weight(history_surface, current_surface):
    """
    今回走路に対する過去走路の採用率。
    同一条件を中心にし、異なる条件はほぼ度外視する。
    """
    past = normalize_surface(history_surface)
    now = normalize_surface(current_surface)

    if not past or not now:
        return 0.05
    if past == now:
        return 1.0

    weights = {
        # 今回が良走路
        ("斑", "良"): 0.30,
        ("湿", "良"): 0.02,

        # 今回が湿走路
        ("斑", "湿"): 0.20,
        ("良", "湿"): 0.02,

        # 今回が斑走路（良と湿の中間として扱う）
        ("良", "斑"): 0.15,
        ("湿", "斑"): 0.20,
    }
    return weights.get((past, now), 0.05)


def weighted_mean_series(values, weights):
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce")
    mask = v.notna() & w.notna() & (w > 0)
    if not mask.any() or float(w[mask].sum()) == 0:
        return np.nan
    return float((v[mask] * w[mask]).sum() / w[mask].sum())


def weighted_rate(condition, weights):
    c = pd.Series(condition, dtype=float)
    w = pd.to_numeric(weights, errors="coerce")
    mask = c.notna() & w.notna() & (w > 0)
    if not mask.any() or float(w[mask].sum()) == 0:
        return np.nan
    return float((c[mask] * w[mask]).sum() / w[mask].sum())


def weighted_std_series(values, weights):
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce")
    mask = v.notna() & w.notna() & (w > 0)
    if mask.sum() < 2 or float(w[mask].sum()) == 0:
        return np.nan

    vv = v[mask].astype(float)
    ww = w[mask].astype(float)
    avg = float((vv * ww).sum() / ww.sum())
    variance = float((ww * (vv - avg) ** 2).sum() / ww.sum())
    return float(np.sqrt(max(variance, 0.0)))


HISTORY_MODEL_COLUMNS = [
    "開催日", "開催場", "レース", "着順", "出走", "走路",
    "ハンデ", "ハンデ数値", "試走T", "競走T", "ST",
    "タイム差", "有効",
]


def normalize_history_schema(df):
    """DB版・Excel版どちらの履歴でも元版の予測列へ揃える。"""
    x = pd.DataFrame() if df is None else df.copy()

    # DB由来の履歴には、元Excelでread_history()が作っていた計算列がない。
    for col in HISTORY_MODEL_COLUMNS:
        if col not in x.columns:
            x[col] = pd.Series(index=x.index, dtype="object")

    # SQLiteの日付文字列とExcelシリアル値の両方に対応
    x["開催日"] = x["開催日"].apply(excel_serial)

    # 数値列を安全に統一
    numeric_cols = ["着順", "出走", "ハンデ数値", "試走T", "競走T", "ST", "タイム差"]
    for col in numeric_cols:
        x[col] = pd.to_numeric(x[col], errors="coerce")

    # DBのハンデ列から数値ハンデを復元
    missing_h = x["ハンデ数値"].isna()
    if missing_h.any():
        x.loc[missing_h, "ハンデ数値"] = x.loc[missing_h, "ハンデ"].apply(handicap_number)

    # 元版と同じタイム差・有効判定を復元
    computed_gap = x["競走T"] - x["試走T"]
    missing_gap = x["タイム差"].isna()
    x.loc[missing_gap, "タイム差"] = computed_gap[missing_gap]

    valid_calc = (
        x["試走T"].notna()
        & x["競走T"].notna()
        & x["タイム差"].notna()
        & (x["タイム差"] > 0)
    )
    supplied = x["有効"]
    if supplied.isna().all():
        x["有効"] = valid_calc
    else:
        supplied_bool = supplied.map(
            lambda v: v if isinstance(v, (bool, np.bool_))
            else str(v).strip().lower() not in ("", "0", "false", "none", "nan")
        )
        x["有効"] = supplied_bool & valid_calc

    x["開催場"] = x["開催場"].fillna("").astype(str)
    x["走路"] = x["走路"].fillna("").astype(str)
    return x[HISTORY_MODEL_COLUMNS].copy()


def prepare_history(df, current, race, settings):
    x = normalize_history_schema(df)

    if x.empty:
        for col in ["経過日数", "最近重み", "場重み", "走路適合重み", "走路重み", "ハンデ重み", "総合重み"]:
            x[col] = pd.Series(index=x.index, dtype="float64")
        return x

    # Ver10.6: 公式プロフィール取得時に、予測対象レースの確定結果が
    # 履歴先頭へ混入している場合がある。同日・同開催場の行は予測から除外する。
    same_day_result = (
        pd.to_numeric(x["開催日"], errors="coerce") == float(race["開催日"])
    ) & (x["開催場"].astype(str) == str(race["開催場"]))
    x = x.loc[~same_day_result].copy()
    if x.empty:
        for col in ["経過日数", "最近重み", "場重み", "走路適合重み", "走路重み", "ハンデ重み", "総合重み"]:
            x[col] = pd.Series(index=x.index, dtype="float64")
        return x

    x["経過日数"] = np.maximum(0, race["開催日"] - x["開催日"])
    x["最近重み"] = np.exp(-x["経過日数"] / settings["最近重視日数"])

    x["場重み"] = np.where(
        x["開催場"] == race["開催場"],
        settings["同一開催場倍率"],
        settings["別開催場倍率"],
    )

    # 走路が異なる履歴は一律倍率ではなく、今回走路との近さで採用率を変える
    x["走路適合重み"] = x["走路"].apply(
        lambda surface: surface_compatibility_weight(surface, race["走路"])
    )

    # 従来の設定倍率は使用せず、1.0 / 0.20 / 0.15 / 0.10 / 0.02 を直接使用
    x["走路重み"] = x["走路適合重み"]

    diff = np.abs(x["ハンデ数値"] - current["今回ハンデ"])
    x["ハンデ重み"] = np.where(
        diff == 0,
        settings["同一ハンデ倍率"],
        np.where(
            diff <= 10,
            settings["近接ハンデ倍率"],
            settings["遠隔ハンデ倍率"],
        ),
    )

    x["総合重み"] = (
        x["最近重み"]
        * x["場重み"]
        * x["走路重み"]
        * x["ハンデ重み"]
    )

    x.loc[~x["有効"], "総合重み"] = np.nan
    return x


def smooth_points(values, maximum, lower_is_better=True, zero_is_zero=False):
    """
    順位だけではなく、平均との差とばらつきを使って点数化する。
    平均値付近は最大点の50%、おおむね±2標準偏差で0～最大点に収まる。
    """
    arr = np.array(values, dtype=float)
    valid = arr[~np.isnan(arr)]

    if len(valid) == 0:
        return [np.nan] * len(arr)

    mean = float(np.mean(valid))
    std = float(np.std(valid, ddof=1)) if len(valid) >= 2 else 0.0

    # 全員ほぼ同じ場合は中間点
    if std < 1e-9:
        result = []
        for v in arr:
            if np.isnan(v):
                result.append(np.nan)
            elif zero_is_zero and v == 0:
                result.append(0.0)
            else:
                result.append(maximum * 0.5)
        return result

    result = []
    for v in arr:
        if np.isnan(v):
            result.append(np.nan)
            continue

        if zero_is_zero and v == 0:
            result.append(0.0)
            continue

        z = (v - mean) / std
        if lower_is_better:
            z = -z

        # z=-2で0点、z=0で50%、z=+2で満点
        normalized = np.clip(0.5 + z / 4.0, 0.0, 1.0)
        result.append(float(normalized * maximum))

    return result


def player_metrics(car, current, hist, race, settings):
    x = prepare_history(hist, current, race, settings)

    # 旧DB・空履歴でも落ちないよう「有効」列を補完する。
    if "有効" not in x.columns:
        x = x.copy()
        x["有効"] = True
    else:
        x = x.copy()
        x["有効"] = x["有効"].fillna(True).astype(bool)

    valid = x.loc[x["有効"]].copy()
    first5 = valid.head(5)
    first10 = valid.head(10)
    first30 = valid.head(30)

    converted_trial = (
        current["今回試走T"]
        + current["今回ハンデ"] / 10 * settings["10m換算秒"]
    )

    # タイム差も今回走路との適合度で加重する
    recent5_diff = weighted_mean_series(
        first5["タイム差"],
        first5["走路適合重み"],
    )

    # 最近の試走水準と今回試走の悪化幅。
    # 過去ハンデも今回と同じ10m換算秒で補正して比較する。
    recent_trial_converted = (
        pd.to_numeric(first5["試走T"], errors="coerce")
        + pd.to_numeric(first5["ハンデ数値"], errors="coerce").fillna(0.0)
        / 10 * settings["10m換算秒"]
    )
    recent_trial_baseline = weighted_mean_series(
        recent_trial_converted,
        first5["走路適合重み"],
    )
    current_trial_deterioration = (
        max(0.0, converted_trial - recent_trial_baseline)
        if not pd.isna(recent_trial_baseline) else 0.0
    )
    # Ver12.2: 選手自身の平常試走からの良化・悪化を測る。
    # 絶対タイムだけではなく、普段より何秒速いかを当日の仕上がりとして使う。
    recent_trial_sd = weighted_std_series(
        recent_trial_converted, first5["走路適合重み"]
    )
    current_trial_change = (
        float(recent_trial_baseline - converted_trial)
        if not pd.isna(recent_trial_baseline) else 0.0
    )

    weighted_rows = x[
        x["ST"].notna() & x["総合重み"].notna()
    ]
    weighted_st = (
        float((weighted_rows["ST"] * weighted_rows["総合重み"]).sum()
              / weighted_rows["総合重み"].sum())
        if not weighted_rows.empty and weighted_rows["総合重み"].sum() != 0
        else np.nan
    )

    # STの安定性: 平均が速くてもばらつきが大きい選手は少し評価を下げる
    st_std = weighted_std_series(
        weighted_rows["ST"], weighted_rows["総合重み"]
    ) if not weighted_rows.empty else np.nan
    st_stability = (
        1 / (1 + st_std * 25)
        if not pd.isna(st_std) else np.nan
    )

    # 試走再現率: 本走タイムと試走タイムの差が小さく、安定する選手を評価
    trial_rows = first10[
        first10["試走T"].notna() & first10["競走T"].notna()
    ].copy()
    if not trial_rows.empty:
        trial_rows["試走本走差"] = (
            pd.to_numeric(trial_rows["競走T"], errors="coerce")
            - pd.to_numeric(trial_rows["試走T"], errors="coerce")
        ).abs()
        trial_gap_mean = weighted_mean_series(
            trial_rows["試走本走差"], trial_rows["走路適合重み"]
        )
        trial_gap_std = weighted_std_series(
            trial_rows["試走本走差"], trial_rows["走路適合重み"]
        )
        trial_reproduction = (
            1 / (1 + trial_gap_mean * 12 + trial_gap_std * 10)
            if not pd.isna(trial_gap_mean) else np.nan
        )
    else:
        trial_gap_mean = np.nan
        trial_gap_std = np.nan
        trial_reproduction = np.nan

    # Ver6.9: 試走信頼度とレース巧者指数
    craft_rows = first30[
        first30["試走T"].notna() & first30["着順"].notna()
    ].copy()
    if len(craft_rows) >= 5:
        craft_rows["試走換算履歴"] = (
            pd.to_numeric(craft_rows["試走T"], errors="coerce")
            + pd.to_numeric(craft_rows["ハンデ数値"], errors="coerce").fillna(0.0)
            / 10 * settings["10m換算秒"]
        )
        craft_rows["試走百分位"] = craft_rows["試走換算履歴"].rank(
            pct=True, method="average", ascending=True
        )
        craft_rows["着順百分位"] = pd.to_numeric(
            craft_rows["着順"], errors="coerce"
        ).rank(pct=True, method="average", ascending=True)
        # SciPy不要のSpearman相関。既に百分位化済みの2列なので、
        # そのPearson相関はSpearman相関と同等になる。
        trial_finish_corr = craft_rows["試走百分位"].corr(
            craft_rows["着順百分位"], method="pearson"
        )
        if pd.isna(trial_finish_corr):
            trial_finish_corr = 0.0
        trial_trust = float(np.clip(
            0.50 + 0.50 * trial_finish_corr, 0.20, 1.00
        ))
        craft_rows["巧者差"] = (
            craft_rows["試走百分位"] - craft_rows["着順百分位"]
        )
        race_craft_index = weighted_mean_series(
            craft_rows["巧者差"], craft_rows["走路適合重み"]
        )
    else:
        trial_finish_corr = np.nan
        trial_trust = 0.50
        race_craft_index = 0.0

    same_h = valid[
        valid["ハンデ"] == f"{int(current['今回ハンデ'])}m"
    ]
    same_h_diff = (
        mean_or_blank(same_h["タイム差"].tolist())
        if not same_h.empty else recent5_diff
    )

    same_venue = valid[valid["開催場"] == race["開催場"]]
    venue_diff = mean_or_blank(same_venue["タイム差"].tolist())

    same_surface = valid[valid["走路"] == race["走路"]]
    surface_diff = mean_or_blank(same_surface["タイム差"].tolist())

    # 着順系も今回走路との適合度で加重する
    finish_scores = {1: 10, 2: 8, 3: 6, 4: 5, 5: 4, 6: 3, 7: 2, 8: 1}

    scored_finish10 = first10["着順"].map(
        lambda v: finish_scores.get(int(v), np.nan) if not pd.isna(v) else np.nan
    )
    finish_index = weighted_mean_series(
        scored_finish10,
        first10["走路適合重み"],
    )

    finish30 = pd.to_numeric(first30["着順"], errors="coerce")
    road_weight30 = first30["走路適合重み"]

    win_rate = weighted_rate(
        (finish30 == 1).where(finish30.notna(), np.nan),
        road_weight30,
    )
    quinella_rate = weighted_rate(
        finish30.isin([1, 2]).where(finish30.notna(), np.nan),
        road_weight30,
    )
    show_rate = weighted_rate(
        finish30.isin([1, 2, 3]).where(finish30.notna(), np.nan),
        road_weight30,
    )

    average_finish = weighted_mean_series(
        first10["着順"],
        first10["走路適合重み"],
    )
    finish_std10 = weighted_std_series(
        first10["着順"],
        first10["走路適合重み"],
    )
    finish_stability = (
        1 / (1 + finish_std10)
        if not pd.isna(finish_std10) else np.nan
    )

    newest5_df = valid.head(5)
    previous5_df = valid.iloc[5:10]
    newest5_avg = weighted_mean_series(
        newest5_df["着順"], newest5_df["走路適合重み"]
    )
    previous5_avg = weighted_mean_series(
        previous5_df["着順"], previous5_df["走路適合重み"]
    )
    improvement = (
        float(previous5_avg - newest5_avg)
        if not pd.isna(newest5_avg) and not pd.isna(previous5_avg)
        else np.nan
    )

    diff_std = weighted_std_series(
        first10["タイム差"],
        first10["走路適合重み"],
    )
    reproducibility = (
        1 / (1 + diff_std * 100)
        if not pd.isna(diff_std) else np.nan
    )

    same_h_30 = first30[
        first30["ハンデ"] == f"{int(current['今回ハンデ'])}m"
    ]
    current_h_win_rate = (
        float((same_h_30["着順"] == 1).sum() / len(same_h_30))
        if len(same_h_30) else 0.0
    )

    prev_h = (
        float(valid.iloc[0]["ハンデ数値"])
        if len(valid) and not pd.isna(valid.iloc[0]["ハンデ数値"])
        else np.nan
    )
    avg5_h = mean_or_blank(valid.head(5)["ハンデ数値"].tolist())

    handicap_improvement = (
        max(0, prev_h - current["今回ハンデ"]) * settings["前走比率"]
        + max(0, avg5_h - current["今回ハンデ"]) * (1 - settings["前走比率"])
        if not pd.isna(prev_h) and not pd.isna(avg5_h)
        else np.nan
    )

    handicap_change_score = (
        min(handicap_improvement / settings["満点改善幅"], 1)
        * settings["ハンデ改善点"]
        if not pd.isna(handicap_improvement) and settings["満点改善幅"] != 0
        else 0.0
    )

    # 直近3走の好調補正
    # 今回走路との適合度を使うため、湿走路の好走だけで大きく上がりにくい。
    recent3 = valid.head(3).copy()
    if len(recent3):
        recent3_top3_rate = weighted_rate(
            (pd.to_numeric(recent3["着順"], errors="coerce") <= 3)
            .where(pd.to_numeric(recent3["着順"], errors="coerce").notna(), np.nan),
            recent3["走路適合重み"],
        )
    else:
        recent3_top3_rate = 0.0

    if len(recent3) >= 3 and recent3_top3_rate >= (2 / 3):
        recent3_form_bonus = 3.0
    elif len(recent3) >= 3 and recent3_top3_rate >= (1 / 3):
        recent3_form_bonus = 1.2
    else:
        recent3_form_bonus = 0.0

    # 直近5走補正: 一時的な1走だけでなく、5走全体の好調・不調を反映
    recent5_finish = pd.to_numeric(first5["着順"], errors="coerce")
    recent5_top3_rate = weighted_rate(
        (recent5_finish <= 3).where(recent5_finish.notna(), np.nan),
        first5["走路適合重み"],
    ) if len(first5) else np.nan
    recent5_avg_finish = weighted_mean_series(
        recent5_finish, first5["走路適合重み"]
    ) if len(first5) else np.nan

    if len(first5) >= 5 and not pd.isna(recent5_avg_finish):
        if recent5_avg_finish <= 3.0 and recent5_top3_rate >= 0.60:
            recent5_form_bonus = 3.0
        elif recent5_avg_finish <= 4.0 and recent5_top3_rate >= 0.40:
            recent5_form_bonus = 1.5
        elif recent5_avg_finish >= 6.0:
            recent5_form_bonus = -2.0
        elif recent5_avg_finish >= 5.0:
            recent5_form_bonus = -0.8
        else:
            recent5_form_bonus = 0.0
    else:
        recent5_form_bonus = 0.0

    # Ver6.8: 今回走路だけに絞り過ぎず、直近の着順そのものから
    # 「今回の試走不振が単発の外れだった可能性」を推定する。
    # 湿走路の能力評価を上げる処理ではなく、試走1回の信頼度だけを緩める。
    raw_recent3_finish = pd.to_numeric(valid.head(3)["着順"], errors="coerce")
    raw_recent5_finish = pd.to_numeric(valid.head(5)["着順"], errors="coerce")
    raw_recent3_top3 = (
        float((raw_recent3_finish <= 3).mean())
        if raw_recent3_finish.notna().any() else 0.0
    )
    raw_recent3_top2 = (
        float((raw_recent3_finish <= 2).mean())
        if raw_recent3_finish.notna().any() else 0.0
    )
    raw_recent5_top3 = (
        float((raw_recent5_finish <= 3).mean())
        if raw_recent5_finish.notna().any() else 0.0
    )
    raw_recent5_poor = (
        float((raw_recent5_finish >= 6).mean())
        if raw_recent5_finish.notna().any() else 0.0
    )
    raw_recent4_top3 = (
        float((raw_recent5_finish.head(4) <= 3).mean())
        if raw_recent5_finish.head(4).notna().any() else 0.0
    )
    raw_recent2_poor = (
        float((raw_recent5_finish.head(2) >= 6).mean())
        if raw_recent5_finish.head(2).notna().any() else 0.0
    )

    # Ver10.6: 上昇カーブ指数。validは新しい履歴から並ぶ前提で、
    # 古い3走平均から新しい3走平均への改善幅を、回帰傾向と合わせて連続値化する。
    curve_finish = pd.to_numeric(valid.head(8)["着順"], errors="coerce").dropna().to_numpy(float)
    if len(curve_finish) >= 4:
        recent_n = min(3, len(curve_finish) // 2)
        old_part = curve_finish[-recent_n:]
        new_part = curve_finish[:recent_n]
        level_gain = float(np.mean(old_part) - np.mean(new_part))
        # 時系列を古い→新しいへ反転し、負の傾きほど着順改善
        chrono = curve_finish[::-1]
        x_curve = np.arange(len(chrono), dtype=float)
        slope = float(np.polyfit(x_curve, chrono, 1)[0]) if len(chrono) >= 3 else 0.0
        upward_curve_index = float(np.clip(level_gain * 0.18 - slope * 0.55, -1.0, 1.0))
    else:
        upward_curve_index = 0.0

    # Ver10.6: 終盤指数。対戦相手ごとの試走順位は入力に無いため、
    # 選手自身の履歴内で「試走水準より着順が良かったか」を標準化して推定する。
    late_rows = valid.head(20).copy()
    late_rows = late_rows[late_rows["試走T"].notna() & late_rows["着順"].notna()]
    if len(late_rows) >= 5:
        late_trial = (
            pd.to_numeric(late_rows["試走T"], errors="coerce")
            + pd.to_numeric(late_rows["ハンデ数値"], errors="coerce").fillna(0.0)
            / 10 * settings["10m換算秒"]
        )
        late_finish = pd.to_numeric(late_rows["着順"], errors="coerce")
        trial_sd = float(late_trial.std(ddof=0))
        finish_sd_local = float(late_finish.std(ddof=0))
        trial_z = (late_trial - late_trial.mean()) / (trial_sd if trial_sd > 1e-9 else 1.0)
        finish_z = (late_finish - late_finish.mean()) / (finish_sd_local if finish_sd_local > 1e-9 else 1.0)
        # 試走が悪い側（正）でも着順が良い側（負）なら正の終盤力
        conversion_advantage = trial_z - finish_z
        recency_w = np.exp(-np.arange(len(late_rows), dtype=float) / 7.0)
        final_kick_index = float(np.clip(np.average(conversion_advantage, weights=recency_w) / 1.8, -1.0, 1.0))
    else:
        final_kick_index = 0.0

    # 直近3走の複数好走を重視。1走だけの好走では救済を弱くする。
    trial_outlier_confidence = np.clip(
        (raw_recent3_top3 - 1 / 3) * 1.05
        + raw_recent3_top2 * 0.45
        + max(0.0, raw_recent5_top3 - 0.40) * 0.55,
        0.0,
        1.0,
    )

    # Ver12.2: 過去レースから「好位置を取れた時の粘り」と「出遅れ時の追上げ」を推定する。
    # 1周目順位は履歴にないため、ST・枠位置・ハンデ帯から序盤位置を確率的に推定する。
    pos_rows = first30[
        first30["ST"].notna() & first30["着順"].notna()
    ].copy()
    position_sample = int(len(pos_rows))
    if position_sample >= 6:
        st_num = pd.to_numeric(pos_rows["ST"], errors="coerce")
        finish_num = pd.to_numeric(pos_rows["着順"], errors="coerce")
        lane_num = pd.to_numeric(pos_rows["出走"], errors="coerce").fillna(4.5)
        hand_num = pd.to_numeric(pos_rows["ハンデ数値"], errors="coerce").fillna(0.0)
        st_center = float(st_num.median())
        st_scale = float(st_num.std(ddof=1)) if len(st_num) >= 2 else 0.04
        st_scale = max(0.025, min(0.10, st_scale if np.isfinite(st_scale) else 0.04))
        # 小さいほど序盤で前にいる可能性が高い。内枠効果は控えめにする。
        early_score = (st_num - st_center) / st_scale + (lane_num - 4.5) * 0.075 + hand_num * 0.010
        good_cut = float(early_score.quantile(0.38))
        poor_cut = float(early_score.quantile(0.68))
        good_mask = early_score <= good_cut
        poor_mask = early_score >= poor_cut
        good_n = int(good_mask.sum()); poor_n = int(poor_mask.sum())
        good_top3 = float((finish_num[good_mask] <= 3).mean()) if good_n >= 2 else np.nan
        good_top2 = float((finish_num[good_mask] <= 2).mean()) if good_n >= 2 else np.nan
        good_avg = float(finish_num[good_mask].mean()) if good_n >= 2 else np.nan
        poor_top3 = float((finish_num[poor_mask] <= 3).mean()) if poor_n >= 2 else np.nan
        poor_avg = float(finish_num[poor_mask].mean()) if poor_n >= 2 else np.nan
        # 少数標本は全体平均へ縮約し、極端な判定を避ける。
        overall_top3 = float((finish_num <= 3).mean())
        shrink_good = min(1.0, good_n / 8.0)
        shrink_poor = min(1.0, poor_n / 8.0)
        good_top3_s = overall_top3 + (good_top3 - overall_top3) * shrink_good if np.isfinite(good_top3) else overall_top3
        good_top2_s = float((finish_num <= 2).mean()) + (good_top2 - float((finish_num <= 2).mean())) * shrink_good if np.isfinite(good_top2) else float((finish_num <= 2).mean())
        poor_top3_s = overall_top3 + (poor_top3 - overall_top3) * shrink_poor if np.isfinite(poor_top3) else overall_top3
        position_hold_index = float(np.clip(good_top3_s * 0.72 + good_top2_s * 0.28, 0.0, 1.0))
        rear_chase_index = float(np.clip(poor_top3_s, 0.0, 1.0))
        position_dependency = float(np.clip((good_top3_s - poor_top3_s + 0.45) / 0.90, 0.0, 1.0))
        bipolar_index = float(np.clip(abs(good_top3_s - poor_top3_s) * 0.80 + min(1.0, float(finish_num.std(ddof=1)) / 2.6) * 0.20, 0.0, 1.0))
    else:
        good_n = poor_n = 0
        good_avg = poor_avg = np.nan
        position_hold_index = 0.50
        rear_chase_index = 0.35
        position_dependency = 0.50
        bipolar_index = 0.30

    return {
        "車": car,
        "選手名": current["選手名"],
        "ハンデ": current["今回ハンデ"],
        "試走換算": converted_trial,
        "直近5差": recent5_diff,
        "平均ST": weighted_st,
        "ST標準偏差": st_std,
        "ST安定性": st_stability,
        "試走本走差": trial_gap_mean,
        "試走本走差標準偏差": trial_gap_std,
        "試走再現率": trial_reproduction,
        "試走着順相関": trial_finish_corr,
        "試走信頼度": trial_trust,
        "レース巧者指数": race_craft_index,
        "同ハンデ差": same_h_diff,
        "開催場差": venue_diff,
        "走路差": surface_diff,
        "着順指数": finish_index,
        "勝率": win_rate,
        "連対率": quinella_rate,
        "上昇度": improvement,
        "再現性": reproducibility,
        "今回ハンデ勝率": current_h_win_rate,
        "3着以内率": show_rate,
        "平均着順": average_finish,
        "着順安定度": finish_stability,
        "審査P": current["審査P"],
        "現ランク": current["現ランク"],
        "前走ハンデ": prev_h,
        "直近5走平均ハンデ": avg5_h,
        "ハンデ改善量": handicap_improvement,
        "ハンデ変化点": handicap_change_score,
        "着順標準偏差": sample_std(first30["着順"].tolist()),
        "直近3走好調補正": recent3_form_bonus,
        "直近5走補正": recent5_form_bonus,
        "直近5走平均着順": recent5_avg_finish,
        "直近5走3着内率": recent5_top3_rate,
        "直近3走生3着内率": raw_recent3_top3,
        "直近3走生2着内率": raw_recent3_top2,
        "直近5走生3着内率": raw_recent5_top3,
        "直近5走生凡走率": raw_recent5_poor,
        "直近4走生3着内率": raw_recent4_top3,
        "直近2走生凡走率": raw_recent2_poor,
        "上昇カーブ指数": upward_curve_index,
        "終盤指数": final_kick_index,
        "試走単発外れ信頼度": float(trial_outlier_confidence),
        "直近試走基準": recent_trial_baseline,
        "直近試走標準偏差": recent_trial_sd,
        "自己試走変化秒": current_trial_change,
        "今回試走悪化幅": float(current_trial_deterioration),
        "位置分析走数": position_sample,
        "好位置推定走数": good_n,
        "後方推定走数": poor_n,
        "好位置時平均着順": good_avg,
        "後方時平均着順": poor_avg,
        "好位置維持指数": position_hold_index,
        "後方追上げ指数": rear_chase_index,
        "位置依存指数": position_dependency,
        "二極化指数": bipolar_index,
    }


def outer_lane_penalty(df, max_penalty=4.5):
    """
    10m以上の同ハンデ帯で横並びが3人以上の場合、外側ほど減点する。

    - 0m線は対象外
    - 同ハンデ2人以下は対象外
    - 人数が多いほど補正を強くする
    - 最外枠でも最大4.5点まで
    """
    penalty = pd.Series(0.0, index=df.index)
    same_line_count = pd.Series(1, index=df.index, dtype=int)
    outer_order = pd.Series(1, index=df.index, dtype=int)

    for handicap, group in df.groupby("ハンデ", dropna=False):
        if pd.isna(handicap):
            continue

        ordered = group.sort_values("車")
        count = len(ordered)

        for order, idx in enumerate(ordered.index, 1):
            same_line_count.loc[idx] = count
            outer_order.loc[idx] = order

        if float(handicap) < 10 or count < 3:
            continue

        # 3人で1/3、4人で2/3、5人以上で最大強度
        crowd_factor = min(1.0, max(0.0, (count - 2) / 3.0))

        for order, idx in enumerate(ordered.index, 1):
            # 内から外への位置を0～1で表す
            outer_ratio = (order - 1) / (count - 1)

            # 内側半分は減点せず、外側半分から滑らかに減点
            outer_exposure = max(0.0, (outer_ratio - 0.5) / 0.5)
            penalty.loc[idx] = -max_penalty * crowd_factor * outer_exposure

    return penalty, same_line_count, outer_order


def add_race_type_features(df):
    """逃げ成功率・内枠残存率・レースタイプを当該レースの相対比較から作る。"""
    df = df.copy()
    handicap = pd.to_numeric(df["ハンデ"], errors="coerce").fillna(0.0)
    front_h = float(handicap.min())
    front_mask = handicap == front_h

    escape_prob = np.zeros(len(df), dtype=float)
    inner_hold_prob = np.zeros(len(df), dtype=float)
    inner_ratio = np.zeros(len(df), dtype=float)
    st_speed_score = np.zeros(len(df), dtype=float)

    front = df.loc[front_mask].sort_values("車")
    count = len(front)
    if count:
        st_vals = pd.to_numeric(front["平均ST"], errors="coerce").fillna(0.20)
        st_rank = st_vals.rank(method="average", ascending=True)
        if count > 1:
            speed = 1.0 - (st_rank - 1.0) / (count - 1.0)
        else:
            speed = pd.Series(0.60, index=front.index)

        st_std = pd.to_numeric(front["ST標準偏差"], errors="coerce").fillna(0.055)
        stability = 1.0 - np.clip((st_std - 0.020) / 0.075, 0.0, 1.0)

        if count > 1:
            lane = pd.Series(
                [1.0 - i / (count - 1.0) for i in range(count)],
                index=front.index,
                dtype=float,
            )
        else:
            lane = pd.Series(0.70, index=front.index, dtype=float)

        quinella = pd.to_numeric(front["連対率"], errors="coerce").fillna(0.15).clip(0, 1)
        show = pd.to_numeric(front["3着以内率"], errors="coerce").fillna(0.25).clip(0, 1)
        recent_avg = pd.to_numeric(front["直近5走平均着順"], errors="coerce").fillna(5.0)
        recent_hold = np.clip((6.5 - recent_avg) / 5.5, 0.0, 1.0)
        sustain = np.clip(quinella * 0.45 + show * 0.25 + recent_hold * 0.30, 0.0, 1.0)

        escape_raw = (
            0.06
            + speed.to_numpy(float) * 0.46
            + stability.to_numpy(float) * 0.22
            + lane.to_numpy(float) * 0.12
            + sustain.to_numpy(float) * 0.20
        )
        escape_raw = np.clip(escape_raw, 0.08, 0.88)

        # 逃げ切れなくても、最内寄り・ST安定・近況の粘りがあれば
        # 2～4番手でコースを守る確率を別に持たせる。
        hold_raw = (
            0.10
            + lane.to_numpy(float) * 0.38
            + stability.to_numpy(float) * 0.18
            + show.to_numpy(float) * 0.17
            + quinella.to_numpy(float) * 0.08
            + recent_hold.to_numpy(float) * 0.11
            + escape_raw * 0.05
            - (1.0 - speed.to_numpy(float)) * 0.04
        )
        hold_raw = np.clip(hold_raw, 0.10, 0.86)

        for idx, escape_value, hold_value in zip(front.index, escape_raw, hold_raw):
            pos = df.index.get_loc(idx)
            escape_prob[pos] = float(escape_value)
            inner_hold_prob[pos] = float(hold_value)
            inner_ratio[pos] = float(lane.loc[idx])
            st_speed_score[pos] = float(speed.loc[idx])

    df["前線ハンデ"] = front_h
    df["前線内側度"] = inner_ratio
    df["同ハンデST優位度"] = st_speed_score
    df["逃げ成功率"] = escape_prob
    df["内枠残存率"] = inner_hold_prob

    race_types = []
    for _, row in df.iterrows():
        is_front = float(row["ハンデ"]) == front_h
        escape = float(row["逃げ成功率"])
        hold = float(row["内枠残存率"])
        craft = float(row.get("レース巧者指数", 0.0) or 0.0)
        trust = float(row.get("試走信頼度", 0.5) or 0.5)
        finish_sd = float(row.get("着順標準偏差", 1.5) or 1.5)
        st_sd = float(row.get("ST標準偏差", 0.05) or 0.05)

        if is_front and escape >= 0.58:
            race_types.append("逃げ型")
        elif is_front and hold >= 0.58:
            race_types.append("内枠粘り型")
        elif craft >= 0.08:
            race_types.append("追込型")
        elif trust >= 0.62 and craft < -0.03:
            race_types.append("試走型")
        elif finish_sd >= 2.35 or st_sd >= 0.070:
            race_types.append("ムラ型")
        else:
            race_types.append("バランス型")
    df["レースタイプ"] = race_types
    return df

def calculate_excel_model(metrics, settings):
    df = pd.DataFrame(metrics)
    df = add_race_type_features(df)

    df["試走点"] = smooth_points(
        df["試走換算"].tolist(),
        settings["試走点"],
        lower_is_better=True,
    )
    # 信頼度が低い選手は、良い試走も悪い試走も中間点へ圧縮する。
    trial_midpoint = settings["試走点"] * 0.50
    trial_trust = pd.to_numeric(
        df["試走信頼度"], errors="coerce"
    ).fillna(0.50).clip(0.20, 1.00)
    df["試走点"] = (
        trial_midpoint
        + (df["試走点"] - trial_midpoint) * trial_trust
    ) * 0.85

    # Ver6.8: 直近に複数回の好走がある選手は、今回試走が悪くても
    # その1回だけが外れ値だった可能性を残す。全員一律ではなく、
    # 試走点が集団中央値を下回った分だけ最大55%戻す。
    trial_median = float(pd.to_numeric(df["試走点"], errors="coerce").median())
    relief_conf = pd.to_numeric(
        df["試走単発外れ信頼度"], errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0)
    trial_shortfall = np.maximum(0.0, trial_median - df["試走点"])
    trial_deterioration = pd.to_numeric(
        df["今回試走悪化幅"], errors="coerce"
    ).fillna(0.0).clip(lower=0.0)

    # 集団平均との差だけでなく、その選手自身の最近の試走水準から
    # 0.05秒以上悪化していれば最大効果として扱う。
    personal_outlier_scale = np.clip(trial_deterioration / 0.05, 0.0, 1.0)
    df["試走単発救済点"] = (
        trial_shortfall * relief_conf * 0.35
        + personal_outlier_scale * relief_conf * 4.0
    )

    # Ver8.1: 直近好調で今回試走だけ悪い選手を「近況主役候補」として点数側でも評価。
    # 車番ではなく、直近3走・5走と試走単発外れ信頼度の組合せで決める。
    recent3_lead = np.clip(
        (pd.to_numeric(df["直近3走好調補正"], errors="coerce").fillna(0.0) + 1.0) / 4.5,
        0.0, 1.0,
    )
    recent5_lead = np.clip(
        (pd.to_numeric(df["直近5走補正"], errors="coerce").fillna(0.0) + 1.0) / 4.5,
        0.0, 1.0,
    )
    df["近況主役点"] = (
        relief_conf
        * (0.58 * recent3_lead + 0.42 * recent5_lead)
        * (2.8 + personal_outlier_scale * 2.7)
    )

    df["最近点"] = smooth_points(
        df["直近5差"].tolist(),
        settings["最近5走点"],
        lower_is_better=True,
    )
    df["ST点"] = smooth_points(
        df["平均ST"].tolist(),
        settings["ST点"],
        lower_is_better=True,
    )
    df["ST安定点"] = smooth_points(
        df["ST安定性"].tolist(),
        2.5,
        lower_is_better=False,
    )
    df["試走再現点"] = smooth_points(
        df["試走再現率"].tolist(),
        3.5,
        lower_is_better=False,
    )
    df["レース巧者点"] = smooth_points(
        df["レース巧者指数"].tolist(),
        4.0,
        lower_is_better=False,
    )
    df["ハンデ点"] = smooth_points(
        df["同ハンデ差"].tolist(),
        settings["ハンデ適性点"],
        lower_is_better=True,
    )
    df["開催場点"] = smooth_points(
        df["開催場差"].tolist(),
        settings["開催場適性点"],
        lower_is_better=True,
    )
    df["走路点"] = smooth_points(
        df["走路差"].tolist(),
        settings["走路適性点"],
        lower_is_better=True,
    )

    # Ver7.4: 位置の利を「逃げ切り」だけでなく「インで残る力」でも評価。
    # 逃げ成功率が低めでも、最内でコースを守れる選手の位置点を失わせない。
    front_h = float(pd.to_numeric(df["ハンデ"], errors="coerce").min())
    position_use = (
        pd.to_numeric(df["逃げ成功率"], errors="coerce").fillna(0.0) * 0.48
        + pd.to_numeric(df["内枠残存率"], errors="coerce").fillna(0.0) * 0.52
    ).clip(0.0, 1.0)
    df["位置活用率"] = position_use
    df["位置点"] = [
        settings["位置取り点"] * (0.30 + 0.70 * float(use))
        if float(h) == front_h
        else settings["位置取り点"] * 0.2
        if float(h) == front_h + 10
        else 0.0
        for h, use in zip(df["ハンデ"], position_use)
    ]

    # Ver7.5: 最前線の最内は、逃げ切れなくても距離ロスが少なく、
    # コースを守って後続の壁になりやすい。その戦略価値を点数側にも反映する。
    front_mask = pd.to_numeric(df["ハンデ"], errors="coerce").fillna(0.0) == front_h
    lane_value = pd.to_numeric(df["前線内側度"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    hold_value = pd.to_numeric(df["内枠残存率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent_value = np.clip(
        (6.5 - pd.to_numeric(df["直近5走平均着順"], errors="coerce").fillna(5.0)) / 5.5,
        0.0, 1.0
    )
    # Ver8.3: 内枠は総合能力そのものではなく補助的な戦略価値として扱う。
    # Ver8.2より加算幅を約55%へ縮小し、元から強い最内車の過剰評価を防ぐ。
    df["内枠展開点"] = np.where(
        front_mask,
        settings["位置取り点"] * 0.55 * (
            lane_value * 0.46 + hold_value * 0.34 + recent_value * 0.20
        ),
        0.0,
    )

    # Ver8.3: 勝ち切り力は表示用の参考指標。総合点には直接加算しない。
    # 最内だけでなく、逃げ、連対力、近況の裏付けがある場合のみ高くなる。
    escape_value = pd.to_numeric(df["逃げ成功率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    quinella_value = pd.to_numeric(df["連対率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    st_value = pd.to_numeric(df["同ハンデST優位度"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    craft_value = np.clip(
        0.50 + pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.0) * 2.0,
        0.0, 1.0,
    )
    df["最内勝ち切り点"] = np.where(
        front_mask,
        settings["位置取り点"] * 0.45 * (
            lane_value * 0.18
            + escape_value * 0.25
            + quinella_value * 0.20
            + recent_value * 0.15
            + st_value * 0.12
            + craft_value * 0.10
        ),
        0.0,
    )

    df["着順点"] = smooth_points(
        df["着順指数"].tolist(),
        settings["着順指数点"],
        lower_is_better=False,
    )
    df["勝率点"] = smooth_points(
        df["勝率"].tolist(),
        settings["勝率点"],
        lower_is_better=False,
    )
    df["連対点"] = smooth_points(
        df["連対率"].tolist(),
        settings["連対率点"],
        lower_is_better=False,
    )
    df["上昇点"] = smooth_points(
        df["上昇度"].tolist(),
        settings["上昇度点"],
        lower_is_better=False,
    )
    df["再現性点"] = smooth_points(
        df["再現性"].tolist(),
        settings["再現性点"],
        lower_is_better=False,
    )
    df["ハンデ勝率点"] = smooth_points(
        df["今回ハンデ勝率"].tolist(),
        settings["今回ハンデ勝率点"],
        lower_is_better=False,
        zero_is_zero=True,
    )

    df["3着以内率点"] = smooth_points(
        df["3着以内率"].tolist(),
        settings["3着以内率点"],
        lower_is_better=False,
    )
    df["平均着順点"] = smooth_points(
        df["平均着順"].tolist(),
        settings["平均着順点"],
        lower_is_better=True,
    )
    df["着順安定度点"] = smooth_points(
        df["着順安定度"].tolist(),
        settings["着順安定度点"],
        lower_is_better=False,
    )

    def parse_rank_strength(rank):
        """
        S1 / S-1 / A103 / B-204 などを数値化する。
        S級 > A級 > B級を維持し、同じ級では数字が小さいほど高評価。
        """
        value = str(rank).strip().upper().replace("－", "-")
        match = re.fullmatch(r"([SAB])\s*-?\s*(\d{1,3})", value)
        if not match:
            return np.nan

        grade = match.group(1)
        rank_no = int(match.group(2))
        if rank_no < 1:
            return np.nan

        # 1位を1.0、200位をほぼ0.0として級内評価。
        # 201以上も解析し、0未満にはしない。
        within_grade = max(0.0, 1.0 - (rank_no - 1) / 200.0)
        grade_base = {"S": 2.0, "A": 1.0, "B": 0.0}[grade]
        return grade_base + within_grade

    rank_values = [
        parse_rank_strength(rank)
        for rank in df["現ランク"]
    ]

    judge_points = smooth_points(
        df["審査P"].tolist(),
        settings["審査Pランク点"] * 0.50,
        lower_is_better=False,
    )
    rank_points = smooth_points(
        rank_values,
        settings["審査Pランク点"] * 0.50,
        lower_is_better=False,
    )

    df["審査Pランク点"] = [
        (
            jp if not pd.isna(jp)
            else settings["審査Pランク点"] * 0.50 * 0.5
        )
        + (
            rp if not pd.isna(rp)
            else settings["審査Pランク点"] * 0.50 * 0.5
        )
        for jp, rp in zip(judge_points, rank_points)
    ]


    # Ver9.0: 「走れる能力」と「1着まで取り切る力」を分離する。
    # 3着以内が多くても1着が少ない選手は、総合能力を大きく落とさず、
    # シミュレーション上の1着昇格だけを抑える。
    win_rate_v = pd.to_numeric(df["勝率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    quinella_v = pd.to_numeric(df["連対率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    top3_v = pd.to_numeric(df["3着以内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent_avg_v = pd.to_numeric(df["直近5走平均着順"], errors="coerce").fillna(5.0)
    craft_v = np.clip(
        0.50 + pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.0) * 2.0,
        0.0, 1.0,
    )
    win_given_top3 = (win_rate_v / (top3_v + 0.08)).clip(0.0, 1.0)
    win_given_top2 = (win_rate_v / (quinella_v + 0.06)).clip(0.0, 1.0)
    recent_win_shape = np.clip((4.8 - recent_avg_v) / 3.8, 0.0, 1.0)
    df["勝ち切り指数"] = np.clip(
        win_given_top3 * 0.42
        + win_given_top2 * 0.26
        + win_rate_v * 0.18
        + recent_win_shape * 0.08
        + craft_v * 0.06,
        0.0, 1.0,
    )
    # 連下力は、能力は高いが勝ち切れないタイプを2～3着候補として残す指標。
    df["連下安定指数"] = np.clip(
        top3_v * 0.52 + quinella_v * 0.28 + craft_v * 0.20,
        0.0, 1.0,
    )

    # Ver12.2: 勝ち切り率とは別に、上位へ安定して残る再現性を評価する。
    # 車番や今回の結果は使わず、長期・直近成績、凡走の少なさ、着順安定度で構成する。
    finish_stability_v = pd.to_numeric(
        df.get("着順安定度", pd.Series(0.45, index=df.index)), errors="coerce"
    ).fillna(0.45).clip(0.0, 1.0)
    recent_top3_stable = pd.to_numeric(
        df.get("直近5走生3着内率", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0)
    recent_poor_stable = pd.to_numeric(
        df.get("直近5走生凡走率", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0)
    df["安定上位指数"] = np.clip(
        top3_v * 0.30
        + quinella_v * 0.16
        + recent_top3_stable * 0.23
        + (1.0 - recent_poor_stable) * 0.13
        + finish_stability_v * 0.12
        + craft_v * 0.06,
        0.0, 1.0,
    )

    # Ver10.0: 能力評価を4層へ再設計する。
    # 1) 基礎スピード: 今回試走・ST・格・適性
    # 2) 実戦能力: 近況の着順構成・安定性・速度を結果へ変える力
    # 3) 勝負強さ: 上位進出時に1着まで取り切る力
    # 4) 展開適性: 逃げ・差し・混戦・位置維持への対応力
    # 車番自体は能力点へ使わず、最内の逃げ残りはシミュレーション側で扱う。

    # ---------- 直近内容 ----------
    recent_top3 = pd.to_numeric(df["直近5走生3着内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent_poor = pd.to_numeric(df["直近5走生凡走率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent4_top3 = pd.to_numeric(df["直近4走生3着内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent2_poor = pd.to_numeric(df["直近2走生凡走率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent3_top3 = pd.to_numeric(df["直近3走生3着内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)

    craft_reliability = np.clip(
        0.50 + pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.0) * 2.0,
        0.0, 1.0,
    )
    df["近況信頼度"] = np.clip(
        pd.to_numeric(df["試走信頼度"], errors="coerce").fillna(0.50) * 0.58
        + craft_reliability * 0.42,
        0.0, 1.0,
    )

    raw_recent_content = (
        (recent_top3 - 0.35) * 13.0
        - recent_poor * 10.0
        + (recent4_top3 - 0.50) * 6.5
        - recent2_poor * 8.0
        + np.maximum(0.0, recent3_top3 - 1.0 / 3.0) * 3.5
    )
    positive_recent = np.maximum(raw_recent_content, 0.0)
    negative_recent = np.minimum(raw_recent_content, 0.0)
    reliability_gate = np.clip(0.72 + 0.38 * df["近況信頼度"], 0.72, 1.0)
    low_reliability_penalty = np.maximum(0.0, 0.48 - df["近況信頼度"]) * 7.0
    sustainable_top3 = 0.40 + 0.55 * df["近況信頼度"]
    df["短期上振れ抑制"] = np.clip(
        np.maximum(0.0, recent_top3 - sustainable_top3) * 4.0,
        0.0, 2.0,
    )
    df["近況信頼度補正"] = (
        positive_recent * (reliability_gate - 1.0)
        - low_reliability_penalty
        - df["短期上振れ抑制"]
    )
    df["直近内容補正"] = np.clip(
        negative_recent
        + positive_recent * reliability_gate
        - low_reliability_penalty
        - df["短期上振れ抑制"],
        -10.0, 8.0,
    )

    # ---------- 1. 基礎スピード層 ----------
    # 試走だけで順位を作らず、ST・格・ハンデ/場/走路適性と組み合わせる。
    df["基礎スピード点"] = (
        pd.to_numeric(df["試走点"], errors="coerce").fillna(0.0) * 0.88
        + pd.to_numeric(df["試走単発救済点"], errors="coerce").fillna(0.0) * 0.55
        + pd.to_numeric(df["試走再現点"], errors="coerce").fillna(0.0) * 0.65
        + pd.to_numeric(df["ST点"], errors="coerce").fillna(0.0) * 0.78
        + pd.to_numeric(df["ST安定点"], errors="coerce").fillna(0.0) * 0.62
        + pd.to_numeric(df["審査Pランク点"], errors="coerce").fillna(0.0) * 0.72
        + pd.to_numeric(df["ハンデ点"], errors="coerce").fillna(0.0) * 0.65
        + pd.to_numeric(df["開催場点"], errors="coerce").fillna(0.0) * 0.55
        + pd.to_numeric(df["走路点"], errors="coerce").fillna(0.0) * 0.55
    )

    # ---------- 2. 実戦能力層 ----------
    # 相関の強い着順系を丸ごと足さず、長期実績・直近内容・安定性へ整理する。
    long_result = (
        pd.to_numeric(df["着順点"], errors="coerce").fillna(0.0) * 0.72
        + pd.to_numeric(df["3着以内率点"], errors="coerce").fillna(0.0) * 0.92
        + pd.to_numeric(df["平均着順点"], errors="coerce").fillna(0.0) * 0.82
        + pd.to_numeric(df["着順安定度点"], errors="coerce").fillna(0.0) * 0.95
        + pd.to_numeric(df["レース巧者点"], errors="coerce").fillna(0.0) * 0.85
        + pd.to_numeric(df["再現性点"], errors="coerce").fillna(0.0) * 0.65
    )
    speed_core = (
        pd.to_numeric(df["試走点"], errors="coerce").fillna(0.0)
        + pd.to_numeric(df["ST点"], errors="coerce").fillna(0.0)
    )
    conversion = np.clip(
        recent_top3 * 0.58
        + (1.0 - recent_poor) * 0.27
        + craft_reliability * 0.15,
        0.0, 1.0,
    )
    df["速度実戦変換補正"] = np.clip(
        speed_core * (conversion - 0.52) * 0.68,
        -4.5, 4.0,
    )
    df["実戦能力点"] = (
        long_result
        + pd.to_numeric(df["最近点"], errors="coerce").fillna(0.0) * 0.52
        + pd.to_numeric(df["近況主役点"], errors="coerce").fillna(0.0) * 0.42
        + df["直近内容補正"]
        + df["速度実戦変換補正"]
    )

    # ---------- 3. 勝負強さ層 ----------
    # ここは能力順位への寄与を限定し、主にシミュレーションの1着昇格へ使う。
    df["勝負強さ点"] = (
        pd.to_numeric(df["勝率点"], errors="coerce").fillna(0.0) * 0.82
        + pd.to_numeric(df["連対点"], errors="coerce").fillna(0.0) * 0.48
        + pd.to_numeric(df["上昇点"], errors="coerce").fillna(0.0) * 0.60
        + pd.to_numeric(df["勝ち切り指数"], errors="coerce").fillna(0.35) * 5.0
    )

    # ---------- 4. 展開適性層 ----------
    # 車番位置の直接点は含めない。位置維持・混戦対応・ハンデ実績のみを評価。
    df["展開適性点"] = (
        craft_reliability * 4.0
        + pd.to_numeric(df["ST安定性"], errors="coerce").fillna(0.50).clip(0.0, 1.0) * 2.0
        + pd.to_numeric(df["ハンデ勝率点"], errors="coerce").fillna(0.0) * 0.35
        + pd.to_numeric(df["連下安定指数"], errors="coerce").fillna(0.35) * 2.5
    )

    # 選手タイプを履歴から自動分類。表示とシミュレーション補助に使用する。
    front_trait = np.clip(
        recent_top3 * 0.35
        + pd.to_numeric(df["ST安定性"], errors="coerce").fillna(0.5) * 0.30
        + pd.to_numeric(df["連下安定指数"], errors="coerce").fillna(0.35) * 0.35,
        0.0, 1.0,
    )
    chase_trait = np.clip(
        pd.to_numeric(df["勝ち切り指数"], errors="coerce").fillna(0.35) * 0.42
        + craft_reliability * 0.33
        + conversion * 0.25,
        0.0, 1.0,
    )
    volatile_trait = np.clip(recent_poor * 0.60 + recent2_poor * 0.40, 0.0, 1.0)
    df["選手タイプ"] = np.select(
        [
            volatile_trait >= 0.55,
            (front_trait >= 0.67) & (pd.to_numeric(df["勝ち切り指数"], errors="coerce").fillna(0.35) < 0.48),
            chase_trait >= 0.62,
            front_trait >= 0.58,
        ],
        ["ムラ型", "安定連下型", "勝負型", "安定型"],
        default="標準型",
    )

    # 4層の最終能力点。実戦能力を最大、基礎スピードを次点とする。
    # 勝負強さは能力順位を独占しないよう15%、展開適性は10%に制限する。
    df["改良総合点"] = (
        df["基礎スピード点"] * 0.35
        + df["実戦能力点"] * 0.45
        + df["勝負強さ点"] * 0.10
        + df["展開適性点"] * 0.10
        + pd.to_numeric(df["ハンデ変化点"], errors="coerce").fillna(0.0) * 0.30
    )

    # 10m以上の同ハンデ帯で、人数が多い外枠を補助的に減点
    (
        df["同ハンデ人数"],
        df["同ハンデ外順"],
    ) = (np.nan, np.nan)
    outer_penalty, line_count, outer_order = outer_lane_penalty(
        df, max_penalty=4.5
    )
    df["同ハンデ人数"] = line_count
    df["同ハンデ外順"] = outer_order
    df["外枠不利補正"] = outer_penalty

    df["改善後総合点"] = (
        df["改良総合点"]
        + df["ハンデ変化点"] * 0.70
        + df["外枠不利補正"]
    )

    # Ver10.2: 実戦評価の裏付けを確認する。
    # 過去の実戦成績が、今回の試走・ST・格などの基礎速度を大きく上回る場合は、
    # 一時的な上振れとして能力点だけを穏やかに割り引く。展開確率は維持する。
    practical_overhang = np.maximum(
        0.0,
        pd.to_numeric(df["実戦能力点"], errors="coerce").fillna(0.0)
        - pd.to_numeric(df["基礎スピード点"], errors="coerce").fillna(0.0) * 1.05,
    )
    df["実戦裏付け不足補正"] = -np.clip(practical_overhang, 0.0, 5.0)

    # 0m内枠は車番そのものではなく、位置活用率・試走信頼度・長期連対率が
    # そろったときだけ「今回も実戦力を再現しやすい」と評価する。
    handicap_v = pd.to_numeric(df["ハンデ"], errors="coerce").fillna(0.0)
    position_v = pd.to_numeric(df["位置活用率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    trust_v = pd.to_numeric(df["試走信頼度"], errors="coerce").fillna(0.45).clip(0.0, 1.0)
    quinella_support = pd.to_numeric(df["連対率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    df["内枠実戦再現補正"] = np.where(
        handicap_v <= 0.0,
        position_v * (0.45 + trust_v) * (0.70 + quinella_support) * 6.0,
        0.0,
    )

    # 内枠でも試走信頼度が低い場合は、前残り期待を能力順位へ過剰転写しない。
    df["内枠信頼不足補正"] = np.where(
        handicap_v <= 0.0,
        -np.maximum(0.0, 0.50 - trust_v) * 17.0,
        0.0,
    )

    # 直近5走全体より直近4走が改善し、直近3走も上位を保つ選手を小幅加点。
    # 単なる高水準ではなく「新しい方へ向けて良化」した場合のみ働く。
    recent5_v = pd.to_numeric(df["直近5走生3着内率"], errors="coerce").fillna(0.0)
    recent4_v = pd.to_numeric(df["直近4走生3着内率"], errors="coerce").fillna(0.0)
    recent3_v = pd.to_numeric(df["直近3走生3着内率"], errors="coerce").fillna(0.0)
    df["継続上昇補正"] = np.clip(
        np.maximum(0.0, recent4_v - recent5_v) * 7.0
        + np.maximum(0.0, recent3_v - 0.50) * 1.2,
        0.0, 1.5,
    ) * np.clip(0.70 + trust_v * 0.45, 0.70, 1.15)

    # 最内車は、試走信頼度が十分高い場合に限り、前で自分のペースを作る再現性を加点。
    # 信頼度が低い最内車には働かないため、単純な1番車優遇にはしない。
    car_v = pd.to_numeric(df["車"], errors="coerce").fillna(99)
    df["最内高信頼再現補正"] = np.where(
        (car_v == 1) & (handicap_v <= 0.0),
        np.clip(np.maximum(0.0, trust_v - 0.50) * 50.0, 0.0, 2.6),
        0.0,
    )

    df["評価整合補正"] = (
        df["実戦裏付け不足補正"]
        + df["内枠実戦再現補正"]
        + df["内枠信頼不足補正"]
        + df["継続上昇補正"]
        + df["最内高信頼再現補正"]
    )
    df["改善後総合点"] = df["改善後総合点"] + df["評価整合補正"]

    # Ver10.2: 選手タイプを3方向で補足する。車番固定ではなく、
    # レース内の相対値から「巻き返し余地」「再現力」「近況単独の過大評価」を判定する。
    def _race_z(series):
        v = pd.to_numeric(series, errors="coerce")
        v = v.fillna(v.mean())
        sd = float(v.std(ddof=0))
        if not np.isfinite(sd) or sd < 1e-9:
            return pd.Series(0.0, index=v.index)
        return ((v - float(v.mean())) / sd).clip(-2.0, 2.0)

    base_z = _race_z(df["基礎スピード点"])
    avg_finish_z = _race_z(df["平均着順"])
    trust_z = _race_z(df["試走信頼度"])
    craft_z = _race_z(df["レース巧者指数"])
    long_top3_z = _race_z(df["3着以内率"])
    recent_top3_z = _race_z(df["直近5走生3着内率"])
    practical_z = _race_z(df["実戦能力点"])
    win_z = _race_z(df["勝ち切り指数"])

    df["巻き返し余地補正"] = np.clip(
        np.maximum(0.0, base_z) * np.maximum(0.0, avg_finish_z) * 2.0
        + np.maximum(0.0, base_z) * 0.55,
        0.0, 3.5,
    )
    df["実戦再現力補正"] = np.clip(
        np.maximum(0.0, trust_z) * 0.75
        + np.maximum(0.0, craft_z) * 1.05
        + np.maximum(0.0, long_top3_z) * 0.45,
        0.0, 2.4,
    )
    recent_top3_raw = pd.to_numeric(df["直近5走生3着内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    df["勝負余力補正"] = np.clip(
        np.maximum(0.0, win_z) * np.maximum(0.0, 0.75 - recent_top3_raw) * 2.8,
        0.0, 1.8,
    )
    df["内側高信頼補正"] = np.where(
        handicap_v <= 0.0,
        np.clip(np.maximum(0.0, trust_v - 0.50) * position_v * 10.0, 0.0, 1.2),
        0.0,
    )
    unsupported = (
        np.maximum(0.0, practical_z) * np.maximum(0.0, -base_z) * 1.50
        + np.maximum(0.0, recent_top3_z) * np.maximum(0.0, -craft_z) * 1.20
        + np.maximum(0.0, recent_top3_z) * np.maximum(0.0, -trust_z) * 0.70
    )
    perfect_surge = np.maximum(0.0, recent_top3_z - 1.0) * (1.10 + np.maximum(0.0, -craft_z))
    df["近況単独過大補正"] = -np.clip(unsupported + perfect_surge, 0.0, 4.5)
    df["選手タイプ総合補正"] = (
        df["巻き返し余地補正"]
        + df["実戦再現力補正"]
        + df["勝負余力補正"]
        + df["内側高信頼補正"]
        + df["近況単独過大補正"]
    )
    df["改善後総合点"] = df["改善後総合点"] + df["選手タイプ総合補正"]

    # Ver10.5: 3着以内と凡走の間にある4～5着の内容を評価する。
    # 3着以内率だけの段差で、競走内容をまとめている選手が沈みすぎるのを防ぐ。
    # 車番は使用せず、直近5走の「6着以下ではないが3着以内でもない」割合から算出。
    recent_mid_hold = np.clip(
        1.0
        - pd.to_numeric(df["直近5走生3着内率"], errors="coerce").fillna(0.0)
        - pd.to_numeric(df["直近5走生凡走率"], errors="coerce").fillna(0.0),
        0.0, 1.0,
    )
    trust_for_hold = pd.to_numeric(
        df["試走信頼度"], errors="coerce"
    ).fillna(0.45).clip(0.0, 1.0)
    craft_for_hold = np.clip(
        0.50 + pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.0) * 2.0,
        0.0, 1.0,
    )
    df["中位粘り率"] = recent_mid_hold
    df["中位粘り補正"] = np.clip(
        recent_mid_hold
        * (0.75 + trust_for_hold * 0.45 + craft_for_hold * 0.35)
        * 2.6,
        0.0, 2.8,
    )

    # 重いハンデから軽くなっただけで無条件加点せず、
    # 試走信頼度と中位粘りが伴う場合に限り条件改善を追加評価する。
    handicap_change_v = pd.to_numeric(
        df["ハンデ変化点"], errors="coerce"
    ).fillna(0.0).clip(lower=0.0)
    df["条件改善再現補正"] = np.clip(
        handicap_change_v
        * (0.30 + trust_for_hold * 0.30 + recent_mid_hold * 0.35),
        0.0, 1.6,
    )

    df["中位内容総合補正"] = (
        df["中位粘り補正"] + df["条件改善再現補正"]
    )
    df["改善後総合点"] = df["改善後総合点"] + df["中位内容総合補正"]

    # Ver10.6: 新しい3指標を能力評価へ追加。
    # 全て車番非依存で、レース内相対値と履歴の流れから算出する。
    curve_v = pd.to_numeric(df.get("上昇カーブ指数", 0.0), errors="coerce").fillna(0.0).clip(-1.0, 1.0)
    kick_v = pd.to_numeric(df.get("終盤指数", 0.0), errors="coerce").fillna(0.0).clip(-1.0, 1.0)

    df["上昇カーブ補正"] = np.clip(curve_v * 3.2, -2.0, 3.2)
    df["終盤力補正"] = np.clip(kick_v * 3.0, -1.8, 3.0)

    # 履歴に対戦相手ランクが無いため、現在の格に対して長期実戦成績が上回る選手を
    # 「格以上に走れる＝相手耐性あり」と推定する。単純な低ランク救済にはしない。
    rank_base_z = _race_z(df["審査Pランク点"])
    durable_z = _race_z(
        pd.to_numeric(df["3着以内率"], errors="coerce").fillna(0.0) * 0.55
        + pd.to_numeric(df["着順安定度"], errors="coerce").fillna(0.0) * 0.25
        + pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.0) * 0.20
    )
    recent_poor_gate = 1.0 - pd.to_numeric(
        df["直近5走生凡走率"], errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0)
    under_ranked = np.maximum(0.0, -rank_base_z)
    df["相手レベル耐性補正"] = np.clip(
        np.maximum(0.0, durable_z)
        * (0.35 + under_ranked * 0.85)
        * recent_poor_gate
        * 1.45
        + np.maximum(0.0, kick_v) * 0.45,
        0.0, 2.2,
    )

    df["Ver10_6総合補正"] = (
        df["上昇カーブ補正"]
        + df["終盤力補正"]
        + df["相手レベル耐性補正"]
    )
    df["改善後総合点"] = df["改善後総合点"] + df["Ver10_6総合補正"]


    # Ver10.7: 能力順位とは別に「1着まで届く上振れ余地」を評価する。
    # ① 前線最内で逃げの形を作れるタイプ
    # ② 格・ST・勝ち切り力があり、近況不振でも一発の天井が残るタイプ
    # の2経路を車番非依存で算出する。
    handicap_now = pd.to_numeric(df["ハンデ"], errors="coerce").fillna(0.0)
    front_h = float(handicap_now.min())
    front_gate = (handicap_now == front_h).astype(float)
    inner_degree = pd.to_numeric(df.get("前線内側度", 0.0), errors="coerce").fillna(0.0).clip(0.0, 1.0)
    escape_v = pd.to_numeric(df.get("逃げ成功率", 0.0), errors="coerce").fillna(0.0).clip(0.0, 1.0)
    hold_v = pd.to_numeric(df.get("内枠残存率", 0.0), errors="coerce").fillna(0.0).clip(0.0, 1.0)
    close_v = pd.to_numeric(df.get("勝ち切り指数", 0.0), errors="coerce").fillna(0.0).clip(0.0, 1.0)
    st_point_z = _race_z(pd.to_numeric(df["ST点"], errors="coerce").fillna(0.0))
    rank_point_z = _race_z(pd.to_numeric(df["審査Pランク点"], errors="coerce").fillna(0.0))
    trial_point_z = _race_z(pd.to_numeric(df["試走点"], errors="coerce").fillna(0.0))
    recent_poor_v = pd.to_numeric(df["直近5走生凡走率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)

    df["逃げ切り上振れ指数"] = np.clip(
        front_gate
        * inner_degree
        * (0.34 * escape_v + 0.28 * hold_v + 0.18 * close_v
           + 0.10 * np.maximum(0.0, trial_point_z)
           + 0.10 * np.maximum(0.0, -st_point_z)),
        0.0, 1.0,
    )

    # 近況不振は通常評価では減点するが、格・ST・勝率が揃う場合の
    # 「一発の天井」までゼロにはしない。連下安定とは分離する。
    raw_chase_ceiling = (
        0.30 * np.maximum(0.0, rank_point_z)
        + 0.24 * np.maximum(0.0, st_point_z)
        + 0.30 * close_v
        + 0.16 * np.maximum(0.0, trial_point_z)
    )
    # 格やSTだけでなく、最低限の勝ち切り実績を必要条件にする。
    # これにより「速いが勝ち筋が薄い」選手の過大評価を防ぐ。
    df["追上げ勝負余地指数"] = np.clip(
        raw_chase_ceiling
        * (0.48 + close_v * 1.55)
        * (0.82 + recent_poor_v * 0.18),
        0.0, 1.0,
    )

    df["勝ち上振れ余地指数"] = np.maximum(
        df["逃げ切り上振れ指数"],
        df["追上げ勝負余地指数"],
    )
    # 能力点への反映は小さく留め、主効果はシミュレーションの1着分岐に置く。
    df["勝ち上振れ能力補正"] = np.clip(
        df["勝ち上振れ余地指数"] * 1.55,
        0.0, 1.55,
    )
    df["改善後総合点"] = df["改善後総合点"] + df["勝ち上振れ能力補正"]

    # 格・ST・勝ち切りの3要素が偏らず揃う選手を「一発勝負型」として小幅評価。
    # どれか1項目だけ突出した選手は上がりすぎないよう、3要素の最小値寄りで判定する。
    rank_pct = pd.to_numeric(df["審査Pランク点"], errors="coerce").rank(pct=True)
    st_pct = pd.to_numeric(df["ST点"], errors="coerce").rank(pct=True)
    close_pct = close_v.rank(pct=True)
    balanced_ceiling = (
        np.minimum(np.minimum(rank_pct, st_pct), close_pct) * 0.65
        + (rank_pct * st_pct * close_pct) ** (1.0 / 3.0) * 0.35
    )
    df["一発勝負バランス補正"] = np.clip(
        np.maximum(0.0, balanced_ceiling - 0.48) * 2.2,
        0.0, 1.15,
    )
    df["改善後総合点"] = df["改善後総合点"] + df["一発勝負バランス補正"]


    # Ver11.3: 当日状態指数。今回試走の相対順位を中心に、
    # 直近状態・上昇カーブ・ST・試走信頼度が伴う場合だけ強く評価する。
    trial_raw = pd.to_numeric(df["試走換算"], errors="coerce")
    trial_pct = trial_raw.rank(pct=True, ascending=False, method="average").fillna(0.50)
    st_pct_today = pd.to_numeric(df["平均ST"], errors="coerce").rank(
        pct=True, ascending=False, method="average"
    ).fillna(0.50)
    trust_today = pd.to_numeric(df["試走信頼度"], errors="coerce").fillna(0.50).clip(0.20, 1.00)
    recent3_today = pd.to_numeric(df["直近3走生3着内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    recent5_today = pd.to_numeric(df["直近5走生3着内率"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    curve_today = pd.to_numeric(df.get("上昇カーブ指数", 0.0), errors="coerce").fillna(0.0).clip(-1.0, 1.0)
    craft_today = pd.to_numeric(df.get("レース巧者指数", 0.0), errors="coerce").fillna(0.0)
    craft_today = np.clip(0.50 + craft_today * 1.5, 0.0, 1.0)
    recent_today = np.clip(recent3_today * 0.58 + recent5_today * 0.42, 0.0, 1.0)
    trend_today = np.clip(0.50 + curve_today * 0.50, 0.0, 1.0)
    support_gate = np.clip(
        0.30 + trust_today * 0.28 + recent_today * 0.20
        + trend_today * 0.12 + craft_today * 0.10, 0.35, 1.00
    )
    own_trial_change = pd.to_numeric(
        df.get("自己試走変化秒", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0)
    own_trial_sd = pd.to_numeric(
        df.get("直近試走標準偏差", pd.Series(0.035, index=df.index)), errors="coerce"
    ).fillna(0.035).clip(0.018, 0.080)
    # 良化0.04秒前後で強評価、悪化0.04秒前後で弱評価。
    # 標準偏差が大きい選手は、単発の変化を少し割り引く。
    own_scale = np.maximum(0.040, own_trial_sd * 1.35)
    df["自己比試走指数"] = np.clip(
        0.50 + own_trial_change / (own_scale * 2.0), 0.0, 1.0
    )
    df["当日状態指数"] = np.clip(
        (trial_pct * 0.39 + df["自己比試走指数"] * 0.22
         + st_pct_today * 0.12 + recent_today * 0.15
         + trend_today * 0.08 + craft_today * 0.04) * support_gate, 0.0, 1.0
    )
    state_center = float(df["当日状態指数"].median())
    df["当日状態補正"] = np.clip(
        np.maximum(0.0, df["当日状態指数"] - state_center) * 8.5, 0.0, 3.4
    )
    df["改善後総合点"] = df["改善後総合点"] + df["当日状態補正"]

    # Ver12.2: 爆発指数。
    # 単なる試走1位ではなく、2位との差が大きい「突出試走」を今日だけの勝ち切り上振れとして扱う。
    trial_values = trial_raw.to_numpy(float)
    valid_trial = trial_values[np.isfinite(trial_values)]
    if len(valid_trial) >= 2:
        sorted_trial = np.sort(valid_trial)
        best_trial = float(sorted_trial[0])
        second_trial = float(sorted_trial[1])
    elif len(valid_trial) == 1:
        best_trial = second_trial = float(valid_trial[0])
    else:
        best_trial = second_trial = np.nan

    # 0.05秒差でほぼ最大。トップ以外は突出度を持たない。
    best_mask = np.isfinite(trial_values) & np.isclose(trial_values, best_trial, atol=1e-9)
    top_margin = max(0.0, second_trial - best_trial) if np.isfinite(best_trial) else 0.0
    margin_power = float(np.clip(top_margin / 0.05, 0.0, 1.0))
    trial_dominance = np.zeros(len(df), dtype=float)
    trial_dominance[best_mask] = margin_power
    df["試走突出度"] = trial_dominance

    # 今日の気配を主役にしつつ、信頼度・上昇傾向を補助条件にする。
    # 履歴が弱くても突出試走そのものは残すが、無条件で能力トップにはしない。
    explosion_support = np.clip(
        0.58 + trust_today.to_numpy(float) * 0.20
        + trend_today.to_numpy(float) * 0.12
        + recent_today.to_numpy(float) * 0.10,
        0.58, 1.00,
    )
    df["爆発指数"] = np.clip(
        (trial_pct.to_numpy(float) * 0.24
         + trial_dominance * 0.34
         + df["自己比試走指数"].to_numpy(float) * 0.25
         + df["当日状態指数"].to_numpy(float) * 0.17)
        * explosion_support,
        0.0, 1.0,
    )
    explosion_center = float(df["爆発指数"].median())
    # 能力順位への加点は控えめ。主効果はシミュレーションの上振れ分岐。
    df["爆発能力補正"] = np.clip(
        np.maximum(0.0, df["爆発指数"] - explosion_center) * 2.2,
        0.0, 1.10,
    )
    df["改善後総合点"] = df["改善後総合点"] + df["爆発能力補正"]

    # Ver12.2: 当日レース評価。
    # 過去能力の絶対点差が大きすぎると、突出試走を何点足しても届かないため、
    # レース内百分位で「基礎力・当日試走・格・ST・ハンデ位置・上昇度」を再合成する。
    # これにより過去能力を捨てず、その日の番組内での相対的な主役候補を評価する。
    base_today_pct = pd.to_numeric(df["改善後総合点"], errors="coerce").rank(
        pct=True, ascending=True, method="average"
    ).fillna(0.50)
    rank_today_pct = pd.to_numeric(df["審査Pランク点"], errors="coerce").rank(
        pct=True, ascending=True, method="average"
    ).fillna(0.50)
    handicap_today_pct = pd.to_numeric(df["ハンデ"], errors="coerce").rank(
        pct=True, ascending=True, method="average"
    ).fillna(0.50)
    trend_rank_pct = pd.to_numeric(df.get("上昇カーブ指数", 0.0), errors="coerce").rank(
        pct=True, ascending=True, method="average"
    ).fillna(0.50)

    # Ver12.2: 前団主導指数。
    # 0m逃げを無条件に上げず、最前線から10m後ろの選手が、今回試走・当日状態・ST・
    # 試走信頼度を伴っている場合に「前団を先に攻略できる主役候補」として評価する。
    handicap_num = pd.to_numeric(df["ハンデ"], errors="coerce").fillna(0.0)
    min_handicap_value = float(handicap_num.min())
    handicap_gap = handicap_num - min_handicap_value
    front_band_gate = ((handicap_gap > 0.0) & (handicap_gap <= 10.0)).astype(float)
    df["前団主導指数"] = np.clip(
        (trial_pct * 0.36
         + df["自己比試走指数"] * 0.18
         + df["当日状態指数"] * 0.22
         + st_pct_today * 0.12
         + trust_today * 0.08
         + trend_rank_pct * 0.04) * front_band_gate,
        0.0, 1.0,
    )

    # 後方追走不成立リスク。
    # 20m以上後方で、前団の最速試走より0.02秒を超えて劣る場合だけ発生。
    # ランクそのものは下げず、今回の追走が成立しにくい条件として扱う。
    front_trial_mask = handicap_gap <= 10.0
    front_trials = trial_raw[front_trial_mask & trial_raw.notna()]
    best_front_trial = float(front_trials.min()) if len(front_trials) else float(trial_raw.min())
    trial_deficit = np.clip((trial_raw - best_front_trial - 0.02) / 0.04, 0.0, 1.0).fillna(0.0)
    backline_gate = (handicap_gap >= 20.0).astype(float)
    df["後方追走不成立リスク"] = np.clip(
        trial_deficit
        * backline_gate
        * (0.78 - df["当日状態指数"] * 0.28)
        * (1.00 - df["爆発指数"] * 0.45),
        0.0, 0.85,
    )

    df["当日レース指数"] = np.clip(
        trial_pct * 0.25
        + df["自己比試走指数"] * 0.11
        + rank_today_pct * 0.17
        + base_today_pct * 0.11
        + st_pct_today * 0.05
        + handicap_today_pct * 0.15
        + trend_rank_pct * 0.06
        + df["前団主導指数"] * 0.14
        - df["後方追走不成立リスク"] * 0.08,
        0.0, 1.0,
    )
    # 爆発指数が高い選手は僅差の順位比較で優先する。過大な固定加点にはしない。
    df["当日レース指数"] = np.clip(
        df["当日レース指数"] + df["試走突出度"] * 0.08 + df["爆発指数"] * 0.04,
        0.0, 1.0,
    )
    df["当日勝ち切り指数"] = np.clip(
        df["自己比試走指数"] * 0.36
        + df["前団主導指数"] * 0.30
        + df["爆発指数"] * 0.18
        + pd.to_numeric(df.get("勝ち切り指数", 0.0), errors="coerce").fillna(0.0).rank(pct=True) * 0.16,
        0.0, 1.0,
    )

    # Ver12.2: 当日試走が横並びのレースでは、格・突破力だけの順位独占を抑え、
    # 安定して2～4着へ残れる選手を評価へ戻す。
    stable_top_pct = pd.to_numeric(df["安定上位指数"], errors="coerce").fillna(0.5).rank(pct=True)
    recent_poor_gate_v = 1.0 - pd.to_numeric(
        df.get("直近5走生凡走率", 0.0), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0)
    trial_spread = float(pd.to_numeric(df["試走換算"], errors="coerce").max() - pd.to_numeric(df["試走換算"], errors="coerce").min())
    trial_tie_gate = float(np.clip((0.08 - trial_spread) / 0.06, 0.0, 1.0))
    df["安定上位評価補正"] = np.clip(
        (stable_top_pct - 0.50) * (0.10 + 0.10 * trial_tie_gate)
        + (recent_poor_gate_v - 0.55) * 0.035,
        -0.055, 0.125,
    )
    df["当日レース指数"] = np.clip(
        df["当日レース指数"] + df["安定上位評価補正"], 0.0, 1.0
    )

    df["基礎履歴総合点"] = df["改善後総合点"]
    df["改善後総合点"] = df["当日レース指数"] * 30.0

    # 選手タイプVer2。既存タイプを残しつつ、強い特徴がある場合だけ上書きする。
    df["選手タイプVer2"] = df["選手タイプ"].astype(str)
    df.loc[(curve_v >= 0.35) & (kick_v >= 0.05), "選手タイプVer2"] = "上昇型"
    df.loc[(kick_v >= 0.35) & (curve_v < 0.35), "選手タイプVer2"] = "終盤型"
    df.loc[(kick_v >= 0.22) & (pd.to_numeric(df["連下安定指数"], errors="coerce").fillna(0.0) >= 0.52), "選手タイプVer2"] = "追走終盤型"
    df.loc[(pd.to_numeric(df["内枠残存率"], errors="coerce").fillna(0.0) >= 0.58) & (kick_v < 0.20), "選手タイプVer2"] = "前残り型"

    # Excel COUNTIF(">") + 1 と同じ競技順位
    df["改良順位"] = [
        1 + sum(other > value for other in df["改良総合点"])
        for value in df["改良総合点"]
    ]
    df["改善後順位"] = [
        1 + sum(other > value for other in df["改善後総合点"])
        for value in df["改善後総合点"]
    ]

    return df




def prepare_simulation_arrays(df):
    """Ver7.2: 近況好調を実戦展開へ強く反映する配列。"""
    cars = df["車"].astype(int).to_numpy()
    handicap = pd.to_numeric(df["ハンデ"], errors="coerce").fillna(0.0).to_numpy(float)
    st = pd.to_numeric(df["平均ST"], errors="coerce").fillna(0.20).clip(0.06, 0.40).to_numpy(float)
    st_std = pd.to_numeric(df.get("ST標準偏差", pd.Series(0.045, index=df.index)), errors="coerce").fillna(0.045).clip(0.015, 0.12).to_numpy(float)
    outer_penalty = pd.to_numeric(df["外枠不利補正"], errors="coerce").fillna(0.0).abs().to_numpy(float)
    line_count = pd.to_numeric(df["同ハンデ人数"], errors="coerce").fillna(1.0).to_numpy(float)
    scores = df["改善後総合点"].to_numpy(float)
    finish_sd = pd.to_numeric(df["着順標準偏差"], errors="coerce").fillna(1.5).clip(0.8, 3.0).to_numpy(float)

    # Ver11.1: 走路状況が変わっても崩れにくい度合い。
    # 着順の散らばり、ST安定性、試走再現性を組み合わせる。
    finish_consistency = np.clip(1.0 - (finish_sd - 0.8) / 2.2, 0.0, 1.0)

    trial_outlier_confidence = pd.to_numeric(df.get("試走単発外れ信頼度", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0).clip(0.0, 1.0).to_numpy(float)
    trial_relief_points = pd.to_numeric(df.get("試走単発救済点", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0).to_numpy(float)
    # Ver7.2: 試走が単発で悪かった好調選手は、本走で戻す分岐を強める。
    form_upside_prob = np.clip(
        trial_outlier_confidence
        * np.clip(trial_relief_points / 2.6, 0.0, 1.0)
        * 1.02,
        0.0, 0.58,
    )
    form_upside_power = 0.34 + trial_outlier_confidence * 0.76

    # 直近の好調度を、能力点とは別の「展開発現力」として作る。
    recent3_bonus = pd.to_numeric(df.get("直近3走好調補正", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0).clip(-3.0, 4.0).to_numpy(float)
    recent5_bonus = pd.to_numeric(df.get("直近5走補正", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0).clip(-3.0, 4.0).to_numpy(float)
    top3_rate_flow = pd.to_numeric(df.get("3着以内率", pd.Series(0.35, index=df.index)), errors="coerce").fillna(0.35).clip(0.0, 1.0).to_numpy(float)
    quinella_rate_flow = pd.to_numeric(df.get("連対率", pd.Series(0.20, index=df.index)), errors="coerce").fillna(0.20).clip(0.0, 1.0).to_numpy(float)
    craft_flow = pd.to_numeric(df.get("レース巧者指数", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0).to_numpy(float)
    craft_scale = np.tanh(craft_flow / 0.30)

    # Ver7.4: 試走だけ速く、実戦で着順へ変換しにくい選手を抑える。
    # 車番固定ではなく、試走信頼度・レース巧者・ST安定性・外位置で判定する。
    trial_trust_flow = pd.to_numeric(
        df.get("試走信頼度", pd.Series(0.50, index=df.index)),
        errors="coerce",
    ).fillna(0.50).clip(0.20, 1.00).to_numpy(float)
    st_stability_flow = pd.to_numeric(
        df.get("ST安定性", pd.Series(0.50, index=df.index)),
        errors="coerce",
    ).fillna(0.50).clip(0.0, 1.0).to_numpy(float)
    execution_quality = np.clip(
        0.34 * trial_trust_flow
        + 0.27 * ((craft_scale + 1.0) / 2.0)
        + 0.22 * st_stability_flow
        + 0.17 * top3_rate_flow,
        0.0, 1.0,
    )

    road_stability = np.clip(
        0.42 * finish_consistency
        + 0.31 * st_stability_flow
        + 0.17 * trial_trust_flow
        + 0.10 * execution_quality,
        0.0, 1.0,
    )
    volatility_profile = np.clip(1.0 - road_stability, 0.0, 1.0)

    recent_form_strength = np.clip(
        0.34 * top3_rate_flow
        + 0.24 * quinella_rate_flow
        + 0.18 * np.clip((recent3_bonus + 1.5) / 5.5, 0.0, 1.0)
        + 0.14 * np.clip((recent5_bonus + 1.5) / 5.5, 0.0, 1.0)
        + 0.10 * ((craft_scale + 1.0) / 2.0),
        0.0, 1.0,
    )

    current_day_strength = pd.to_numeric(
        df.get("当日状態指数", pd.Series(0.50, index=df.index)), errors="coerce"
    ).fillna(0.50).clip(0.0, 1.0).to_numpy(float)
    explosion_strength = pd.to_numeric(
        df.get("爆発指数", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0).to_numpy(float)
    trial_dominance_flow = pd.to_numeric(
        df.get("試走突出度", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0).to_numpy(float)
    front_command_flow = pd.to_numeric(
        df.get("前団主導指数", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0).to_numpy(float)
    pursuit_failure_risk = pd.to_numeric(
        df.get("後方追走不成立リスク", pd.Series(0.0, index=df.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 0.85).to_numpy(float)
    own_trial_form = pd.to_numeric(
        df.get("自己比試走指数", pd.Series(0.5, index=df.index)), errors="coerce"
    ).fillna(0.5).clip(0.0, 1.0).to_numpy(float)
    today_win_conversion = pd.to_numeric(
        df.get("当日勝ち切り指数", pd.Series(0.5, index=df.index)), errors="coerce"
    ).fillna(0.5).clip(0.0, 1.0).to_numpy(float)

    score_std = np.std(scores, ddof=1)
    ability_z = (scores - np.mean(scores)) / (score_std if score_std > 0 else 1.0)
    form_std = np.std(recent_form_strength, ddof=1)
    form_z = (recent_form_strength - np.mean(recent_form_strength)) / (form_std if form_std > 0 else 1.0)
    # Ver7.4: 能力75%・近況展開25%。
    # 通常スタート時は基礎能力をしっかり出し、展開失敗時だけ大きく落ちる二山型。
    state_std = np.std(current_day_strength, ddof=1)
    state_z = (current_day_strength - np.mean(current_day_strength)) / (state_std if state_std > 0 else 1.0)
    z_scores = ability_z * 0.68 + form_z * 0.20 + state_z * 0.12

    same_line_outer_ratio = np.zeros(len(df), dtype=float)
    same_line_inner_ratio = np.zeros(len(df), dtype=float)
    for _, group in df.groupby("ハンデ", dropna=False):
        ordered = group.sort_values("車")
        count = len(ordered)
        if count <= 1:
            continue
        for order, idx in enumerate(ordered.index, 1):
            pos = df.index.get_loc(idx)
            ratio = (order - 1) / (count - 1)
            same_line_outer_ratio[pos] = ratio
            same_line_inner_ratio[pos] = 1.0 - ratio

    # 外側で実戦変換力が低いほど、混戦で位置を失いやすい。
    traffic_conversion = np.clip(
        execution_quality
        - same_line_outer_ratio * 0.20
        + np.clip(craft_scale, -1.0, 1.0) * 0.10,
        0.05, 0.95,
    )


    # Ver9.0: 能力とは別の勝ち切り・連下指標。
    closing_base = pd.to_numeric(
        df.get("勝ち切り指数", pd.Series(0.35, index=df.index)),
        errors="coerce",
    ).fillna(0.35).clip(0.0, 1.0)
    final_kick_flow = pd.to_numeric(
        df.get("終盤指数", pd.Series(0.0, index=df.index)),
        errors="coerce",
    ).fillna(0.0).clip(-1.0, 1.0)
    win_upside_flow = pd.to_numeric(
        df.get("勝ち上振れ余地指数", pd.Series(0.0, index=df.index)),
        errors="coerce",
    ).fillna(0.0).clip(0.0, 1.0).to_numpy(float)
    closing_strength = np.clip(
        closing_base.to_numpy(float)
        + np.maximum(0.0, final_kick_flow.to_numpy(float)) * 0.13
        + win_upside_flow * 0.20,
        0.0, 1.0,
    )
    position_hold_flow = pd.to_numeric(
        df.get("好位置維持指数", pd.Series(0.50, index=df.index)), errors="coerce"
    ).fillna(0.50).clip(0.0, 1.0).to_numpy(float)
    rear_chase_flow = pd.to_numeric(
        df.get("後方追上げ指数", pd.Series(0.35, index=df.index)), errors="coerce"
    ).fillna(0.35).clip(0.0, 1.0).to_numpy(float)
    position_dependency_flow = pd.to_numeric(
        df.get("位置依存指数", pd.Series(0.50, index=df.index)), errors="coerce"
    ).fillna(0.50).clip(0.0, 1.0).to_numpy(float)
    bipolar_flow = pd.to_numeric(
        df.get("二極化指数", pd.Series(0.30, index=df.index)), errors="coerce"
    ).fillna(0.30).clip(0.0, 1.0).to_numpy(float)

    place_strength = pd.to_numeric(
        df.get("連下安定指数", pd.Series(0.45, index=df.index)),
        errors="coerce",
    ).fillna(0.45).clip(0.0, 1.0).to_numpy(float)

    zero_line_outer = (handicap == 0).astype(float) * same_line_outer_ratio * np.clip((line_count - 1.0) / 2.0, 0.0, 1.0)
    outer_start_delay = outer_penalty * 0.060 + zero_line_outer * 0.065

    st_speed_risk = np.clip((st - 0.14) / 0.16, 0.0, 1.0)
    st_variation_risk = np.clip((st_std - 0.025) / 0.065, 0.0, 1.0)
    outer_risk = np.clip(outer_penalty / 4.5 + zero_line_outer * 0.75, 0.0, 1.0)
    crowd_risk = np.clip((line_count - 2.0) / 4.0, 0.0, 1.0)
    conversion_risk = np.clip(1.0 - traffic_conversion, 0.0, 1.0)
    normal_late_prob = np.clip(
        0.040 + st_speed_risk * 0.066 + st_variation_risk * 0.098
        + outer_risk * 0.076 + crowd_risk * 0.032
        + conversion_risk * same_line_outer_ratio * 0.090,
        0.030, 0.29,
    )
    severe_late_prob = np.clip(
        0.009 + st_speed_risk * 0.021 + st_variation_risk * 0.042
        + outer_risk * 0.032
        + conversion_risk * same_line_outer_ratio * 0.045,
        0.006, 0.12,
    )

    zero_inner_escape = (handicap == 0).astype(float) * same_line_inner_ratio * np.clip((line_count - 1.0) / 2.0, 0.0, 1.0)
    escape_success_prob = pd.to_numeric(df.get("逃げ成功率", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0).clip(0.0, 0.90).to_numpy(float)

    top3_rate = pd.to_numeric(df.get("3着以内率", pd.Series(0.35, index=df.index)), errors="coerce").fillna(0.35).clip(0.0, 1.0).to_numpy(float)
    quinella_rate = pd.to_numeric(df.get("連対率", pd.Series(0.20, index=df.index)), errors="coerce").fillna(0.20).clip(0.0, 1.0).to_numpy(float)
    st_stability = pd.to_numeric(df.get("ST安定性", pd.Series(0.50, index=df.index)), errors="coerce").fillna(0.50).clip(0.0, 1.0).to_numpy(float)

    # Ver7.4: 点数モデルで算出した内枠残存率をシミュレーションにも使用。
    # 当日の混雑度とST安定性を少しだけ再加味する。
    stored_inner_hold = pd.to_numeric(
        df.get("内枠残存率", pd.Series(0.0, index=df.index)),
        errors="coerce",
    ).fillna(0.0).clip(0.0, 0.90).to_numpy(float)
    inner_hold_prob = np.clip(
        stored_inner_hold * 0.82
        + zero_inner_escape * 0.08
        + st_stability * 0.06
        + top3_rate * 0.04,
        0.08, 0.88,
    )
    front_handicap = float(np.min(handicap))
    front_line = handicap == front_handicap
    inner_hold_prob *= front_line.astype(float)

    # Ver8.3: 勝ち切り確率は「最内だから」ではなく能力の裏付けを必須にする。
    # z_scores・実戦変換力が低い車は、内を守れても1着への強制昇格を抑える。
    ability_gate = 1.0 / (1.0 + np.exp(-np.clip(z_scores, -3.0, 3.0)))
    front_win_prob = np.clip(
        escape_success_prob * 0.30
        + inner_hold_prob * 0.10
        + quinella_rate * 0.17
        + recent_form_strength * 0.13
        + st_stability * 0.11
        + execution_quality * 0.10
        + ability_gate * 0.07
        + win_upside_flow * 0.14,
        0.03, 0.74,
    ) * front_line.astype(float)
    base_start_key = handicap * 0.10 + cars.astype(float) * 0.002 + st * 0.88 + outer_start_delay - zero_inner_escape * 0.010 - escape_success_prob * 0.010

    return {
        "cars": cars, "handicap": handicap, "st": st, "st_std": st_std,
        "z_scores": z_scores, "finish_sd": finish_sd,
        "outer_start_delay": outer_start_delay,
        "same_line_outer_ratio": same_line_outer_ratio,
        "same_line_inner_ratio": same_line_inner_ratio,
        "zero_inner_escape": zero_inner_escape,
        "normal_late_prob": normal_late_prob,
        "severe_late_prob": severe_late_prob,
        "base_start_key": base_start_key,
        "trial_outlier_confidence": trial_outlier_confidence,
        "form_upside_prob": form_upside_prob,
        "form_upside_power": form_upside_power,
        "recent_form_strength": recent_form_strength,
        "execution_quality": execution_quality,
        "trial_trust_flow": trial_trust_flow,
        "st_stability_flow": st_stability_flow,
        "traffic_conversion": traffic_conversion,
        "road_stability": road_stability,
        "current_day_strength": current_day_strength,
        "explosion_strength": explosion_strength,
        "trial_dominance_flow": trial_dominance_flow,
        "front_command_flow": front_command_flow,
        "pursuit_failure_risk": pursuit_failure_risk,
        "own_trial_form": own_trial_form,
        "today_win_conversion": today_win_conversion,
        "volatility_profile": volatility_profile,
        "form_leader_strength": np.clip(
            form_upside_prob * 0.38
            + recent_form_strength * 0.24
            + execution_quality * 0.14
            + closing_strength * 0.20
            + front_command_flow * 0.12,
            0.0, 1.0,
        ),
        "closing_strength": closing_strength,
        "win_upside_flow": win_upside_flow,
        "final_kick_flow": final_kick_flow.to_numpy(float),
        "place_strength": place_strength,
        "position_hold_flow": position_hold_flow,
        "rear_chase_flow": rear_chase_flow,
        "position_dependency_flow": position_dependency_flow,
        "bipolar_flow": bipolar_flow,
        "escape_success_prob": escape_success_prob,
        "inner_hold_prob": inner_hold_prob,
        "front_win_prob": front_win_prob,
        "front_handicap": front_handicap,
        "front_line": front_line,
    }


def simulate_detailed(df, trials, seed, track_temp=30.0):
    """Ver12.2: 大人数同ハンデ線を含む6周モデルに、安定上位と突破機会を追加する。"""
    rng = np.random.default_rng(seed)
    arr = prepare_simulation_arrays(df)
    cars = arr["cars"]; n = len(cars)
    handicap = arr["handicap"]; st = arr["st"]
    z_scores = arr["z_scores"]; finish_sd = arr["finish_sd"]
    explosion_strength = arr["explosion_strength"]
    trial_dominance_flow = arr["trial_dominance_flow"]
    front_command_flow = arr.get("front_command_flow", np.zeros(n, dtype=float))
    pursuit_failure_risk = arr.get("pursuit_failure_risk", np.zeros(n, dtype=float))
    own_trial_form = arr.get("own_trial_form", np.full(n, 0.5, dtype=float))
    today_win_conversion = arr.get("today_win_conversion", np.full(n, 0.5, dtype=float))
    outer_start_delay = arr["outer_start_delay"]
    same_line_outer_ratio = arr["same_line_outer_ratio"]
    same_line_inner_ratio = arr["same_line_inner_ratio"]
    normal_late_prob = arr["normal_late_prob"]; severe_late_prob = arr["severe_late_prob"]
    base_start_key = arr["base_start_key"]
    form_upside_prob = arr["form_upside_prob"]; form_upside_power = arr["form_upside_power"]
    recent_form_strength = arr["recent_form_strength"]
    execution_quality = arr["execution_quality"]
    trial_trust_flow = arr["trial_trust_flow"]
    st_stability_flow = arr["st_stability_flow"]
    traffic_conversion = arr["traffic_conversion"]
    road_stability = arr["road_stability"]
    current_day_strength = arr.get("current_day_strength", np.full(n, 0.50, dtype=float))
    volatility_profile = arr["volatility_profile"]
    form_leader_strength = arr["form_leader_strength"]
    closing_strength = arr["closing_strength"]
    win_upside_flow = arr.get("win_upside_flow", np.zeros(n, dtype=float))
    final_kick_flow = arr.get("final_kick_flow", np.zeros(n, dtype=float))
    place_strength = arr["place_strength"]
    position_hold_flow = arr.get("position_hold_flow", np.full(n, 0.50))
    rear_chase_flow = arr.get("rear_chase_flow", np.full(n, 0.35))
    position_dependency_flow = arr.get("position_dependency_flow", np.full(n, 0.50))
    bipolar_flow = arr.get("bipolar_flow", np.full(n, 0.30))
    escape_success_prob = arr["escape_success_prob"]
    inner_hold_prob = arr["inner_hold_prob"]
    front_win_prob = arr["front_win_prob"]
    front_line = arr["front_line"]
    min_handicap = arr["front_handicap"]

    # Ver11.1: 全車同ハンデは横一列スタートとして明示的に評価する。
    # 車番固定ではなく、平均ST・ST安定性・今回試走・実戦変換力から
    # 「スタート一気指数」を作り、外枠でも本当に切れる選手は先頭争いへ出せる。
    all_same_handicap = bool(n >= 3 and np.nanmax(handicap) - np.nanmin(handicap) < 1e-9)
    # Ver12.2: 全車横一列だけでなく、0m単騎＋10mに多数など「大人数の同ハンデ線」も混戦モデル対象。
    unique_h, h_counts = np.unique(handicap, return_counts=True)
    dense_idx = int(np.argmax(h_counts)) if len(h_counts) else 0
    dense_handicap = float(unique_h[dense_idx]) if len(unique_h) else float(np.nanmin(handicap))
    dense_line_size = int(h_counts[dense_idx]) if len(h_counts) else n
    dense_line_race = bool(dense_line_size >= 4)
    dense_line_mask = np.isclose(handicap, dense_handicap)
    trial_values = pd.to_numeric(
        df.get("試走換算", pd.Series(np.nan, index=df.index)),
        errors="coerce",
    ).to_numpy(float)
    valid_trial = trial_values[np.isfinite(trial_values)]
    trial_fill = float(np.nanmedian(valid_trial)) if len(valid_trial) else 3.50
    trial_values = np.where(np.isfinite(trial_values), trial_values, trial_fill)

    def _lower_rank_strength(values):
        values = np.asarray(values, dtype=float)
        order_idx = np.argsort(values, kind="stable")
        strength = np.zeros(len(values), dtype=float)
        if len(values) <= 1:
            strength[:] = 0.5
        else:
            strength[order_idx] = 1.0 - np.arange(len(values), dtype=float) / (len(values) - 1.0)
        return strength

    st_launch_strength = _lower_rank_strength(st)
    trial_launch_strength = _lower_rank_strength(trial_values)
    same_line_launch_strength = np.clip(
        st_launch_strength * 0.46
        + st_stability_flow * 0.19
        + trial_launch_strength * 0.22
        + execution_quality * 0.13,
        0.0, 1.0,
    )

    # Ver11.3: 好スタートを「実際の先行位置」に変換し、その位置を短時間維持する力。
    # STだけではなく、本走への変換力・試走信頼度・走路安定性を組み合わせる。
    # 能力点を直接持ち上げず、スタート成功時だけ展開へ作用させる。
    trial_trust_flow = pd.to_numeric(
        df.get("試走信頼度", pd.Series(0.50, index=df.index)),
        errors="coerce",
    ).fillna(0.50).clip(0.0, 1.0).to_numpy(float)
    lead_conversion_strength = np.clip(
        same_line_launch_strength * 0.43
        + st_stability_flow * 0.13
        + execution_quality * 0.18
        + traffic_conversion * 0.14
        + road_stability * 0.08
        + trial_trust_flow * 0.04,
        0.0, 1.0,
    )

    finish_matrix = np.zeros((n, n), dtype=np.int64)
    trifecta = Counter()
    trio = Counter()
    exacta = Counter()
    quinella = Counter()
    scenario_counts = Counter()
    phases = 3

    # Ver12.2: 熱による前残りと、スタート後の隊列形成による残りを分離する。
    # 41℃前後でも横一列スタートで内枠が先に隊列を作れば残れる一方、
    # 温度だけで全ての前残りを強化しない。
    track_temp = float(track_temp if track_temp is not None else 30.0)
    if track_temp < 44.0:
        heat_index = 0.0
    elif track_temp < 48.0:
        heat_index = 0.05 + (track_temp - 44.0) / 4.0 * 0.20
    elif track_temp < 50.0:
        heat_index = 0.25 + (track_temp - 48.0) / 2.0 * 0.25
    else:
        heat_index = 0.50 + np.clip((track_temp - 50.0) / 10.0, 0.0, 1.0) * 0.50
    heat_index = float(np.clip(heat_index, 0.0, 1.0))
    chaos_index = float(np.clip((track_temp - 48.0) / 16.0, 0.0, 1.0))
    front_temp_gate = 0.20 + heat_index * 0.80
    escape_success_prob = escape_success_prob * (0.34 + heat_index * 0.66)
    inner_hold_prob = inner_hold_prob * front_temp_gate
    front_win_prob = front_win_prob * (0.24 + heat_index * 0.76)
    normal_pass_margin = 0.19 + heat_index * 0.15
    incumbent_bonus = 0.060 + heat_index * 0.070

    # 50℃未満でも温度由来の前残りは弱いが、横一列の隊列残存は別処理で発生する。
    scenario_names = np.array(["先行縦長", "前残り", "混戦", "追い込み"], dtype=object)
    scenario_prob = np.array([0.36, 0.12, 0.28, 0.24], dtype=float)
    scenario_prob += heat_index * np.array([-0.055, 0.185, -0.040, -0.090])
    scenario_prob = np.clip(scenario_prob, 0.03, None)
    scenario_prob /= scenario_prob.sum()
    scenario_ids = rng.choice(4, size=int(trials), p=scenario_prob)
    scenario_noise_map = np.array([0.88, 0.82, 1.28, 1.06])
    leader_factor_map = np.array([0.90, 1.24, 1.12, 0.70])
    escape_factor_map = np.array([1.08, 1.22, 0.82, 0.72]) * (1.0 + heat_index * 0.07)
    hold_factor_map = np.array([1.08, 1.20, 0.90, 0.78]) * (1.0 + heat_index * 0.12)
    win_factor_map = np.array([1.04, 1.16, 0.82, 0.64]) * (1.0 + heat_index * 0.06)

    for scenario_id in scenario_ids:
        scenario = scenario_names[int(scenario_id)]
        scenario_counts[str(scenario)] += 1
        draw = rng.random(n)
        severe = draw < severe_late_prob
        normal = (~severe) & (draw < severe_late_prob + normal_late_prob)
        late_delay = np.zeros(n, dtype=float)
        if severe.any():
            late_delay[severe] = rng.uniform(0.30, 0.62, int(severe.sum()))
            mask = severe & (handicap == 0)
            late_delay[mask] += same_line_outer_ratio[mask] * 0.34
        if normal.any():
            late_delay[normal] = rng.uniform(0.12, 0.28, int(normal.sum()))
            mask = normal & (handicap == 0)
            late_delay[mask] += same_line_outer_ratio[mask] * 0.20

        start_noise_sd = 0.034 * (1.0 + chaos_index * (0.35 + volatility_profile * 0.65))
        start_key = base_start_key + rng.normal(0, start_noise_sd, n) + late_delay

        if all_same_handicap:
            # 横一列では内外の距離差より、切った直後の加速とST再現性を優先する。
            # 55℃付近ではグリップ低下により全体のブレは増える一方、
            # 好スタートを決めた選手が先にコースを選べる価値も少し高まる。
            launch_value = 0.040 + heat_index * 0.022
            start_key -= same_line_launch_strength * launch_value
            # 横一列では最内側ほど最初の進路を確保しやすい。
            # ただしST・再現性が低い選手を車番だけで救済しない。
            inner_lane_ratio = 1.0 - same_line_outer_ratio
            lane_launch_gate = np.clip(
                same_line_launch_strength * 0.58 + st_stability_flow * 0.22
                + execution_quality * 0.20, 0.0, 1.0
            )
            start_key -= inner_lane_ratio * lane_launch_gate * 0.024

            # 上位2名程度にだけ「スタート一気」の分岐を持たせる。
            # 毎回固定せず、指数・安定性・熱走路の組合せで発生させる。
            launch_rank = np.argsort(-same_line_launch_strength)
            for launch_order, idx in enumerate(launch_rank[:min(3, n)]):
                burst_prob = np.clip(
                    0.07
                    + same_line_launch_strength[idx] * 0.24
                    + st_stability_flow[idx] * 0.08
                    + heat_index * 0.05
                    - launch_order * 0.035,
                    0.04, 0.38,
                )
                if not severe[idx] and rng.random() < burst_prob:
                    start_key[idx] -= rng.uniform(0.025, 0.065) * (
                        0.75 + same_line_launch_strength[idx] * 0.50
                    )

            # 外枠一律減点を少し緩和。ただしSTが悪い外枠は救済しない。
            start_key -= same_line_outer_ratio * same_line_launch_strength * (
                0.010 + heat_index * 0.008
            )

        order = np.argsort(start_key)

        # Ver11.3: 横一列でスタート上位に入った選手だけ、先行転換を成立させる。
        # 「指数が高いだけ」では発動せず、実際のスタート順が前方であることを条件にする。
        launch_retention = np.zeros(n, dtype=float)
        launch_pair = None
        if all_same_handicap:
            front_window = [int(i) for i in order[:min(3, n)]]
            for start_pos, idx in enumerate(front_window):
                inner_lane_ratio = 1.0 - same_line_outer_ratio[idx]
                formation_value = inner_lane_ratio * (
                    0.55 * lead_conversion_strength[idx]
                    + 0.45 * same_line_launch_strength[idx]
                )
                conversion_prob = np.clip(
                    0.05
                    + lead_conversion_strength[idx] * 0.42
                    + same_line_launch_strength[idx] * 0.15
                    + formation_value * 0.16
                    + heat_index * 0.06
                    - start_pos * 0.055
                    - (0.10 if normal[idx] else 0.0),
                    0.03, 0.70,
                )
                if not severe[idx] and rng.random() < conversion_prob:
                    launch_retention[idx] = np.clip(
                        0.16
                        + lead_conversion_strength[idx] * 0.34
                        + formation_value * 0.16
                        + position_hold_flow[idx] * 0.27
                        + position_dependency_flow[idx] * 0.09
                        + heat_index * 0.06
                        - start_pos * 0.045,
                        0.16, 0.93,
                    )

            # 先頭と番手がともに高い先行転換力を持つ場合、短い隊列を形成。
            # これにより「先頭が行き、もう1車が追走して残る」を自然に再現する。
            if n >= 2:
                lead_idx, second_idx = int(order[0]), int(order[1])
                pair_quality = min(lead_conversion_strength[lead_idx], lead_conversion_strength[second_idx])
                leader_inner = 1.0 - same_line_outer_ratio[lead_idx]
                pair_prob = np.clip(
                    0.04 + pair_quality * 0.27 + leader_inner * 0.08
                    + heat_index * 0.05,
                    0.03, 0.43,
                )
                if (launch_retention[lead_idx] > 0
                        and launch_retention[second_idx] > 0
                        and rng.random() < pair_prob):
                    launch_pair = (lead_idx, second_idx)

        # 能力の直接支配をさらに弱め、近況・スタート・位置取りを相対的に強化。
        scenario_noise = scenario_noise_map[int(scenario_id)]
        # 熱走路では滑りやライン乱れによる上下振れを増やす。
        # 安定型は増幅を小さく、ムラ型は大きくする。
        race_noise_sd = (0.16 + finish_sd * 0.075) * scenario_noise
        race_noise_sd *= 1.0 + chaos_index * (0.22 + volatility_profile * 0.72)
        race_noise = rng.normal(0, race_noise_sd)
        base_pace = (
            z_scores * 0.78
            + race_noise
            - (st - 0.20) * 0.58
            + (execution_quality - 0.50) * 0.20
            + chaos_index * (road_stability - 0.50) * 0.12
            + (current_day_strength - 0.50) * (0.10 + heat_index * 0.12)
            + front_command_flow * (0.13 + heat_index * 0.10)
            + (own_trial_form - 0.50) * 0.10
            - pursuit_failure_risk * (0.12 + heat_index * 0.10)
        )

        # 後方追走不成立は毎回固定減点せず、条件が悪い試行で大敗側へ分岐する。
        pursuit_fail = rng.random(n) < np.clip(
            pursuit_failure_risk * (0.24 + chaos_index * 0.18), 0.0, 0.42
        )
        if pursuit_fail.any():
            base_pace[pursuit_fail] -= rng.uniform(0.20, 0.48, int(pursuit_fail.sum()))

        # Ver12.2: 突出試走は毎回固定加点せず、当日の動きが本走で発現する試行だけ強く出す。
        # 高温時は機力差が展開へ出やすくなる一方、不発試行も残す。
        explosion_hit_prob = np.clip(
            0.03 + explosion_strength * (0.30 + heat_index * 0.12)
            + trial_dominance_flow * (0.12 + heat_index * 0.08),
            0.03, 0.62,
        )
        explosion_hit = rng.random(n) < explosion_hit_prob
        if explosion_hit.any():
            explosion_power = (
                0.10
                + explosion_strength[explosion_hit] * (0.26 + heat_index * 0.10)
                + trial_dominance_flow[explosion_hit] * (0.12 + heat_index * 0.06)
            )
            base_pace[explosion_hit] += explosion_power

        # 突出気配が発現した車は終盤の勝ち切り分岐にも乗りやすくする。
        explosion_finish_boost = np.zeros(n, dtype=float)
        explosion_finish_boost[explosion_hit] = np.clip(
            explosion_strength[explosion_hit] * 0.16
            + trial_dominance_flow[explosion_hit] * 0.10,
            0.0, 0.25,
        )
        # 先行転換成立時は序盤の巡航へ小幅加点。後半まで能力差を無効化しない。
        retained_mask = launch_retention > 0
        if retained_mask.any():
            base_pace[retained_mask] += (
                launch_retention[retained_mask] * (0.10 + heat_index * 0.045)
            )
        # Ver12.2: 実際に前方スタートを取れた時だけ、過去の位置維持型を反映する。
        start_rank = np.empty(n, dtype=int)
        start_rank[order] = np.arange(n)
        good_position_now = start_rank <= min(2, n - 1)
        if good_position_now.any():
            base_pace[good_position_now] += (
                position_hold_flow[good_position_now] * 0.10
                + position_dependency_flow[good_position_now] * 0.045
            )
        # 二極化型がスタート後方になった場合は、後方追上げ力が低いほど大敗側へ振れる。
        poor_position_now = start_rank >= max(4, n - 3)
        poor_branch_prob = np.clip(
            0.05 + bipolar_flow * 0.25 + position_dependency_flow * 0.18
            - rear_chase_flow * 0.18, 0.02, 0.48
        )
        poor_collapse = poor_position_now & (rng.random(n) < poor_branch_prob)
        if poor_collapse.any():
            base_pace[poor_collapse] -= (
                0.10 + bipolar_flow[poor_collapse] * 0.18
                + position_dependency_flow[poor_collapse] * 0.10
            )

        if launch_pair is not None:
            pair_leader, pair_follower = launch_pair
            base_pace[pair_leader] += 0.035 + heat_index * 0.018
            base_pace[pair_follower] += 0.055 + heat_index * 0.025

        if scenario == "先行縦長":
            base_pace[front_line] += 0.08 + execution_quality[front_line] * 0.05
            chase_mask = handicap > min_handicap
            base_pace[chase_mask] += 0.05 + traffic_conversion[chase_mask] * 0.07
        elif scenario == "前残り":
            base_pace[front_line] += 0.14 + recent_form_strength[front_line] * 0.08
            base_pace[~front_line] -= 0.05
        elif scenario == "混戦":
            base_pace += rng.normal(0, 0.10, n)
        elif scenario == "追い込み":
            chase_mask = handicap > min_handicap
            base_pace[chase_mask] += 0.15 + traffic_conversion[chase_mask] * 0.12
            base_pace[front_line] -= 0.07

        # 好調選手は毎回固定加点せず、その日の展開で動きが出る確率を上げる。
        flow_hit_prob = np.clip(0.07 + recent_form_strength * 0.38 + form_upside_prob * 0.12, 0.07, 0.50)
        flow_hit = rng.random(n) < flow_hit_prob
        if flow_hit.any():
            base_pace[flow_hit] += rng.uniform(0.04, 0.13, int(flow_hit.sum())) * (0.65 + recent_form_strength[flow_hit])

        form_upside = rng.random(n) < form_upside_prob
        if form_upside.any():
            base_pace[form_upside] += rng.uniform(form_upside_power[form_upside] * 0.82, form_upside_power[form_upside] * 1.30)

        # Ver8.1: 近況主役候補。前残り・混戦では、試走外れから本走で戻して
        # 1着まで押し上がる独立分岐を持たせる。車番固定ではない。
        leader_candidate = int(np.argmax(form_leader_strength))
        scenario_leader_factor = leader_factor_map[int(scenario_id)]
        leader_breakout = (
            not severe[leader_candidate]
            and rng.random() < np.clip(
                0.04
                + form_leader_strength[leader_candidate] * 0.38 * scenario_leader_factor
                + closing_strength[leader_candidate] * 0.14,
                0.04, 0.54,
            )
        )
        if leader_breakout:
            base_pace[leader_candidate] += (
                rng.uniform(0.24, 0.48)
                + form_upside_prob[leader_candidate] * 0.48
                + recent_form_strength[leader_candidate] * 0.10
                + closing_strength[leader_candidate] * 0.12
            )

        recovery = np.ones(n, dtype=float)
        if severe.any():
            recovery[severe] = rng.uniform(0.40, 0.64, int(severe.sum()))
            base_pace[severe] -= rng.uniform(0.28, 0.56, int(severe.sum()))
        if normal.any():
            recovery[normal] = rng.uniform(0.64, 0.84, int(normal.sum()))
            base_pace[normal] -= rng.uniform(0.10, 0.27, int(normal.sum()))

        passed = np.zeros((n, n), dtype=bool)
        # Ver8.4: 一度抜かれた後の隊列崩壊を記録する。
        # collapse_level が高いほど、その後の巡航力と抵抗力が落ちる。
        overtaken_count = np.zeros(n, dtype=np.int8)
        collapse_level = np.zeros(n, dtype=float)
        first_overtaken_phase = np.full(n, phases, dtype=np.int8)
        delay_total = late_delay + outer_start_delay
        initial_leader = int(order[0])
        clean_escape = front_line[initial_leader] and not severe[initial_leader] and not normal[initial_leader] and rng.random() < np.clip(escape_success_prob[initial_leader] * escape_factor_map[int(scenario_id)], 0.02, 0.96)

        # 最内前線車は、逃げ失敗でも「内枠残り」へ分岐できる。
        front_candidates = [int(i) for i in order if front_line[int(i)]]
        inner_front = min(front_candidates, key=lambda i: cars[i]) if front_candidates else initial_leader
        inner_hold = (not clean_escape and not severe[inner_front] and rng.random() < np.clip(inner_hold_prob[inner_front] * hold_factor_map[int(scenario_id)], 0.02, 0.97))

        # Ver8.3: 展開成立時だけ勝ち切り抽選を行う。
        # 内枠残存だけの場合は、クリーンな逃げより勝ち切り条件を厳しくする。
        win_setup_factor = 1.0 if clean_escape else 0.66
        front_win = (
            not severe[inner_front]
            and (clean_escape or inner_hold)
            and rng.random() < np.clip(
                front_win_prob[inner_front] * win_factor_map[int(scenario_id)] * win_setup_factor,
                0.02, 0.76,
            )
        )
        if front_win:
            base_pace[inner_front] += rng.uniform(0.12, 0.25) + escape_success_prob[inner_front] * 0.11

        boxed = (not clean_escape and not inner_hold and (normal[inner_front] or rng.random() < 0.18 + same_line_outer_ratio[inner_front] * 0.10))

        if clean_escape:
            base_pace[initial_leader] += 0.13 + escape_success_prob[initial_leader] * 0.32 + recent_form_strength[initial_leader] * 0.08
        elif inner_hold:
            base_pace[inner_front] += 0.15 + inner_hold_prob[inner_front] * 0.24 + recent_form_strength[inner_front] * 0.07
            inner_pos = int(np.where(order == inner_front)[0][0])
            if inner_pos <= 2 and rng.random() < 0.20 + inner_hold_prob[inner_front] * 0.22:
                order = np.delete(order, inner_pos)
                order = np.insert(order, 0, inner_front)
        elif boxed:
            base_pace[inner_front] -= rng.uniform(0.10, 0.24)

        # Ver7.4: 前線の最内車が残る展開では、その後ろの「実戦変換力が高い車」が
        # コースを拾って続く。一方、外側で変換力が低い車は包まれやすい。
        front_indices = np.where(front_line)[0]
        follower = None
        if (clean_escape or inner_hold) and len(front_indices) >= 3:
            follower_score = (
                traffic_conversion[front_indices] * 0.25
                + form_upside_prob[front_indices] * 0.65
                + recent_form_strength[front_indices] * 0.15
                - same_line_outer_ratio[front_indices] * 0.04
            )
            follower_score[front_indices == inner_front] = -999.0
            follower = int(front_indices[int(np.argmax(follower_score))])
            if rng.random() < np.clip(0.24 + traffic_conversion[follower] * 0.34 + form_upside_prob[follower] * 0.50, 0.20, 0.72):
                base_pace[follower] += rng.uniform(0.12, 0.25) + form_upside_prob[follower] * 0.22

        weak_outer = front_indices[
            (same_line_outer_ratio[front_indices] >= 0.70)
            & (traffic_conversion[front_indices] < 0.43)
        ]
        for idx in weak_outer:
            if rng.random() < 0.24 + (0.43 - traffic_conversion[idx]) * 0.75:
                base_pace[idx] -= rng.uniform(0.10, 0.23)

        for phase in range(phases):
            phase_recovery = (phase + 1) / phases
            # 抜かれた後はライン・リズムを失い、残り周回ほど後退が連鎖しやすい。
            # ただし実戦変換力が高い選手は崩れ幅を抑える。
            collapse_drag = collapse_level * (0.72 + 0.12 * phase)
            pace = (
                base_pace
                + rng.normal(0, (0.075 + finish_sd * 0.027) * (1.0 + chaos_index * (0.15 + volatility_profile * 0.48)))
                + z_scores * (1.0 - recovery) * phase_recovery * 0.34
                - collapse_drag
            )
            pos = n - 1
            while pos > 0:
                trailing = int(order[pos]); ahead = int(order[pos - 1])
                margin = normal_pass_margin + incumbent_bonus
                # 熱走路では路面を使って前へ出る余地が小さくなるため、
                # 後方からの仕掛けほど追加の速度差を必要とする。
                position_heat = heat_index * (0.018 + 0.012 * min(pos, 5))
                margin += position_heat
                # 実戦変換力が低い追走車は、速い試走があっても混戦で抜きづらい。
                margin += max(0.0, 0.50 - traffic_conversion[trailing]) * 0.34
                # 実戦変換力が高い車は前を捌く際の必要差を少し小さくする。
                margin -= max(0.0, traffic_conversion[trailing] - 0.58) * 0.16
                # 前団主導指数が高い10m勢は、同ハンデ集団を捌いて前線へ出る力として扱う。
                margin -= max(0.0, front_command_flow[trailing] - 0.42) * 0.24
                # すでに抜かれて崩れた車は、後続への抵抗力も低下する。
                margin -= min(0.24, collapse_level[ahead] * 0.30)

                if clean_escape and ahead == initial_leader and pos - 1 == 0:
                    margin += max(0.11, 0.15 - phase * 0.022 + escape_success_prob[ahead] * 0.24)
                    margin += heat_index * (0.055 - phase * 0.007)
                if inner_hold and ahead == inner_front and pos - 1 <= 2:
                    # 先頭でなくてもインを守る車を抜くには余分な差が必要。
                    margin += max(0.09, 0.16 - phase * 0.017 + inner_hold_prob[ahead] * 0.19 + recent_form_strength[ahead] * 0.035)
                if front_win and ahead == inner_front and pos - 1 == 0:
                    margin += max(0.12, 0.20 - phase * 0.020 + front_win_prob[ahead] * 0.22)
                if follower is not None and trailing == follower and pos <= 4:
                    margin -= 0.07 + form_upside_prob[follower] * 0.12

                # Ver11.3: 先行転換に成功した車は1〜2周目だけ抜かれにくくする。
                # 熱走路ほど追抜側にも負荷がかかるが、後半には効果を減衰させる。
                if launch_retention[ahead] > 0 and pos - 1 <= 2:
                    early_retention = max(0.0, 1.0 - phase / max(1, phases - 1))
                    margin += launch_retention[ahead] * early_retention * (
                        0.095 + heat_index * 0.060
                    )
                # 前に出た時に抜かれにくい選手は、序盤限定ではなく中終盤まで抵抗する。
                if start_rank[ahead] <= 2 and pos - 1 <= 3:
                    phase_keep = 0.70 + 0.30 * max(0.0, 1.0 - phase / max(1, phases - 1))
                    margin += position_hold_flow[ahead] * position_dependency_flow[ahead] * phase_keep * 0.105
                if launch_pair is not None:
                    pair_leader, pair_follower = launch_pair
                    if ahead == pair_leader and trailing == pair_follower and pos - 1 == 0:
                        # 番手車は先頭を無理に早仕掛けせず、隊列を保ちやすい。
                        margin += 0.055 + heat_index * 0.025
                    elif ahead == pair_follower and pos - 1 <= 1:
                        # 先頭・番手が形成された時、後続が番手を抜く障壁を少し増やす。
                        margin += 0.045 + heat_index * 0.040

                close_count = sum(abs(pace[int(order[k])] - pace[trailing]) < 0.30 for k in range(pos + 1, min(n, pos + 4)))
                delayed_nearby = sum(delay_total[int(order[k])] >= 0.16 for k in range(max(0, pos - 1), min(n, pos + 3)))
                margin += min(0.23, close_count * 0.055 + delayed_nearby * 0.042)

                if passed[ahead, trailing]:
                    margin += same_or_stronger_repass_penalty(pace[trailing], pace[ahead])
                if severe[trailing]: margin += 0.16 * (1.0 - phase_recovery)
                elif normal[trailing]: margin += 0.08 * (1.0 - phase_recovery)
                if handicap[trailing] == 0 and same_line_outer_ratio[trailing] > 0 and (severe[trailing] or normal[trailing]):
                    margin += same_line_outer_ratio[trailing] * (0.78 - phase * 0.11)

                if pace[trailing] > pace[ahead] + margin:
                    # 抜かれた側の崩れ判定。能力差が大きい、早い周回、
                    # 実戦変換力が低い、すでに一度抜かれているほど連鎖しやすい。
                    ability_gap = max(0.0, pace[trailing] - pace[ahead] - margin)
                    early_factor = (phases - phase) / phases
                    stability_guard = (
                        0.52 * execution_quality[ahead]
                        + 0.30 * traffic_conversion[ahead]
                        + 0.18 * recent_form_strength[ahead]
                    )
                    collapse_prob = np.clip(
                        0.10
                        + ability_gap * 0.34
                        + early_factor * 0.13
                        + overtaken_count[ahead] * 0.16
                        + max(0.0, 0.55 - stability_guard) * 0.48,
                        0.06, 0.82,
                    )
                    if rng.random() < collapse_prob:
                        collapse_add = (
                            0.10
                            + min(0.32, ability_gap * 0.24)
                            + early_factor * 0.07
                            + max(0.0, 0.55 - stability_guard) * 0.16
                        )
                        collapse_level[ahead] = min(0.78, collapse_level[ahead] + collapse_add)
                    overtaken_count[ahead] += 1
                    if first_overtaken_phase[ahead] == phases:
                        first_overtaken_phase[ahead] = phase

                    order[pos - 1], order[pos] = trailing, ahead
                    passed[trailing, ahead] = True
                pos -= 1

        # Ver7.4: 前線最内が残ったレースでは、隊列が崩れにくい展開も作る。
        # 番手候補は車番固定ではなく、試走外れ復調・実戦変換力・近況から選ぶ。
        if (clean_escape or inner_hold) and int(order[0]) == inner_front and follower is not None:
            line_lock_prob = np.clip(
                0.18 + inner_hold_prob[inner_front] * 0.16
                + form_upside_prob[follower] * 0.48
                + traffic_conversion[follower] * 0.10
                + heat_index * 0.14,
                0.18, 0.66,
            )
            if rng.random() < line_lock_prob:
                cur_f = int(np.where(order == follower)[0][0])
                if cur_f != 1:
                    order = np.delete(order, cur_f)
                    order = np.insert(order, 1, follower)

                remaining_front = [
                    int(i) for i in np.where(front_line)[0]
                    if int(i) not in (inner_front, follower)
                ]
                if remaining_front:
                    third_score = np.array([
                        recent_form_strength[i] * 0.44
                        + traffic_conversion[i] * 0.36
                        + execution_quality[i] * 0.20
                        for i in remaining_front
                    ])
                    third_candidate = remaining_front[int(np.argmax(third_score))]
                    cur_t = int(np.where(order == third_candidate)[0][0])
                    if cur_t != 2 and rng.random() < 0.62:
                        order = np.delete(order, cur_t)
                        order = np.insert(order, 2, third_candidate)

        # Ver8.1: シナリオごとの最終隊列調整。
        if scenario == "先行縦長":
            front_now = [int(i) for i in order if front_line[int(i)]]
            if len(front_now) >= 2:
                selected_front = sorted(front_now, key=lambda i: (-base_pace[i], int(cars[i])))[:2]
                for target_pos, idx in enumerate(selected_front):
                    cur = int(np.where(order == idx)[0][0])
                    order = np.delete(order, cur); order = np.insert(order, target_pos, idx)
            chase_candidates = [int(i) for i in order if handicap[int(i)] > min_handicap and not severe[int(i)]]
            if chase_candidates:
                chase_score = np.array([base_pace[i] + traffic_conversion[i] * 0.20 for i in chase_candidates])
                chaser = chase_candidates[int(np.argmax(chase_score))]
                cur = int(np.where(order == chaser)[0][0])
                if cur > 2 and rng.random() < (0.48 + traffic_conversion[chaser] * 0.24) * (1.0 - heat_index * 0.38):
                    order = np.delete(order, cur); order = np.insert(order, 2, chaser)
        elif scenario == "追い込み":
            chase_candidates = [int(i) for i in order if handicap[int(i)] > min_handicap and not severe[int(i)]]
            if chase_candidates:
                chase_score = np.array([base_pace[i] + traffic_conversion[i] * 0.26 for i in chase_candidates])
                chaser = chase_candidates[int(np.argmax(chase_score))]
                cur = int(np.where(order == chaser)[0][0])
                target = 1 if rng.random() < 0.35 * (1.0 - heat_index * 0.42) else 2
                if cur > target:
                    order = np.delete(order, cur); order = np.insert(order, target, chaser)
        elif scenario == "混戦":
            front_top = sum(front_line[int(i)] for i in order[:4])
            if front_top >= 3 and rng.random() < 0.24 * (1.0 - heat_index * 0.30):
                chase_candidates = [int(i) for i in order if handicap[int(i)] > min_handicap and traffic_conversion[int(i)] >= 0.45]
                if chase_candidates:
                    chaser = max(chase_candidates, key=lambda i: base_pace[i] + traffic_conversion[i] * 0.18)
                    cur = int(np.where(order == chaser)[0][0])
                    target = int(rng.integers(1, 4))
                    if cur > target:
                        order = np.delete(order, cur); order = np.insert(order, target, chaser)

        # Ver8.4: 隊列崩壊の最終連鎖。
        # 早い段階で能力上位に抜かれ、後方にも強い車が複数いる場合は、
        # 2～3着で踏みとどまらず着外まで飲み込まれる展開を作る。
        for idx in range(n):
            if overtaken_count[idx] <= 0 or collapse_level[idx] < 0.16:
                continue
            cur = int(np.where(order == idx)[0][0])
            if cur >= n - 1:
                continue
            behind = [int(j) for j in order[cur + 1:]]
            stronger_behind = [
                j for j in behind
                if (z_scores[j] - z_scores[idx] > 0.28)
                and traffic_conversion[j] >= 0.42
                and not severe[j]
            ]
            if not stronger_behind:
                continue
            early = 1.0 - first_overtaken_phase[idx] / max(1, phases)
            cascade_prob = np.clip(
                0.08
                + collapse_level[idx] * 0.62
                + min(3, len(stronger_behind)) * 0.10
                + early * 0.14
                - execution_quality[idx] * 0.16
                - heat_index * 0.10,
                0.04, 0.78,
            )
            if rng.random() < cascade_prob:
                max_drop = min(len(stronger_behind), 3)
                drop = 1 + int(rng.integers(0, max_drop))
                target = min(n - 1, cur + drop)
                order = np.delete(order, cur)
                order = np.insert(order, target, idx)

        # Ver8.3: 勝ち切り抽選成立でも無条件に先頭へ固定しない。
        # クリーン逃げ、能力、現在位置が揃った場合に限って先頭へ戻す。
        if front_win:
            cur_w = int(np.where(order == inner_front)[0][0])
            finish_win_prob = np.clip(
                (0.46 if clean_escape else 0.24)
                + front_win_prob[inner_front] * 0.34
                + execution_quality[inner_front] * 0.12
                - max(0, cur_w - 2) * 0.10
                - collapse_level[inner_front] * 0.44
                - overtaken_count[inner_front] * 0.08,
                0.08, 0.88,
            )
            if cur_w <= 3 and rng.random() < finish_win_prob:
                order = np.delete(order, cur_w)
                order = np.insert(order, 0, inner_front)

        # Ver8.1: 近況主役候補が展開をつかんだ場合は、最終的に1～2着へ進出。
        # 前残りでは先頭まで、先行縦長・混戦では1～2番手を抽選する。
        if leader_breakout and not severe[leader_candidate]:
            cur_l = int(np.where(order == leader_candidate)[0][0])
            # 能力が高くても勝ち切り指数が低い選手は、1着固定ではなく2～3着へ。
            # ただし最前線の逃げ車は front_win 側で別評価するため、ここでは後方主役候補を調整。
            close = float(closing_strength[leader_candidate])
            if scenario == "前残り":
                first_prob = 0.30 + close * 0.48
                target_l = 0 if rng.random() < first_prob else 1
            elif scenario == "混戦":
                first_prob = 0.18 + close * 0.42
                target_l = 0 if rng.random() < first_prob else 1
            elif scenario == "先行縦長":
                first_prob = 0.12 + close * 0.34
                target_l = 0 if rng.random() < first_prob else 1
            else:
                target_l = 1 if rng.random() < (0.48 + place_strength[leader_candidate] * 0.30) else 2
            if front_win and leader_candidate != inner_front:
                target_l = max(1, target_l)
            if cur_l > target_l:
                order = np.delete(order, cur_l)
                order = np.insert(order, target_l, leader_candidate)

        # Ver9.0: 最終勝ち切り判定。
        # 後方から能力で先頭へ来たものの勝ち切り実績が弱い選手は、
        # 勝負強い2～3番手候補に差される場合を作る。
        # 1番など最前線車の clean_escape / front_win は保護して、逃げ残りを消さない。
        current_leader = int(order[0])
        protected_escape = (
            current_leader == inner_front
            and (clean_escape or front_win)
            and collapse_level[current_leader] < 0.22
        )
        if not protected_escape and closing_strength[current_leader] < 0.48:
            challengers = [
                int(i) for i in order[1:4]
                if not severe[int(i)]
                and closing_strength[int(i)] > closing_strength[current_leader] + 0.10
                and base_pace[int(i)] > base_pace[current_leader] - 0.18
            ]
            if challengers:
                finisher = max(
                    challengers,
                    key=lambda i: closing_strength[i] * 0.58 + base_pace[i] * 0.24 + place_strength[i] * 0.18,
                )
                conversion_prob = np.clip(
                    0.06
                    + (closing_strength[finisher] - closing_strength[current_leader]) * 0.42
                    + max(0.0, base_pace[finisher] - base_pace[current_leader]) * 0.18
                    - heat_index * (0.09 - np.maximum(0.0, final_kick_flow[finisher]) * 0.035),
                    0.02, 0.34,
                )
                if rng.random() < conversion_prob:
                    cur_f = int(np.where(order == finisher)[0][0])
                    order = np.delete(order, cur_f)
                    order = np.insert(order, 0, finisher)

        # Ver10.7: 能力順位が中位でも、格・ST・勝ち切り力が揃う選手には
        # 展開がほどけた際の一発逆転を残す。現在2～5番手にいることを条件とし、
        # 後方から無条件に先頭へ飛ばす処理にはしない。
        upset_pool = [
            int(i) for i in order[1:5]
            if not severe[int(i)]
            and win_upside_flow[int(i)] >= 0.34
            and closing_strength[int(i)] >= 0.24
            and base_pace[int(i)] > base_pace[int(order[0])] - 0.38
        ]
        if upset_pool and not protected_escape:
            upsetter = max(
                upset_pool,
                key=lambda i: (
                    win_upside_flow[i] * 0.30
                    + today_win_conversion[i] * 0.32
                    + closing_strength[i] * 0.22
                    + execution_quality[i] * 0.10
                    + st_stability_flow[i] * 0.06
                ),
            )
            upset_prob = np.clip(
                0.015
                + win_upside_flow[upsetter] * 0.070
                + today_win_conversion[upsetter] * 0.085
                + closing_strength[upsetter] * 0.055
                + max(0.0, base_pace[upsetter] - base_pace[current_leader]) * 0.06
                - heat_index * 0.035,
                0.015, 0.145,
            )
            if rng.random() < upset_prob:
                cur_u = int(np.where(order == upsetter)[0][0])
                order = np.delete(order, cur_u)
                order = np.insert(order, 0, upsetter)

        # 包まれた内枠車は1着候補から落ちやすいが、完全着外固定にはしない。
        if boxed:
            cur = int(np.where(order == inner_front)[0][0])
            target = min(n - 1, cur + int(rng.integers(1, 4)))
            if target > cur:
                order = np.delete(order, cur); order = np.insert(order, target, inner_front)

        for idx in range(n):
            if handicap[idx] == 0 and same_line_outer_ratio[idx] > 0 and (severe[idx] or normal[idx]):
                cur = int(np.where(order == idx)[0][0])
                drop = int(rng.integers(4, 7)) if severe[idx] else int(rng.integers(3, 6))
                drop = max(1, int(round(drop * (0.70 + same_line_outer_ratio[idx]))))
                target = min(n - 1, cur + drop)
                if target > cur:
                    order = np.delete(order, cur); order = np.insert(order, target, idx)

        # Ver10.3: 能力順位とは別に、スタート後の「隊列連鎖」を再現する。
        # 車番固定ではなく、ハンデ構成・内外位置・逃げ力・追走力・レール残存力から選ぶ。
        unique_lines = np.unique(handicap)
        front_members = [int(i) for i in np.where(front_line)[0]]
        back_members = [int(i) for i in np.where(handicap > min_handicap)[0]]

        # A) 複数ハンデ線: 前線の外寄りで逃げ力が高い車が主導権を取ると、
        # 後方線の追走上位が2番手、前線の総合力上位が3番手に収まる隊列。
        if len(unique_lines) >= 2 and len(front_members) >= 3 and back_members:
            launch_pool = [
                i for i in front_members
                if same_line_outer_ratio[i] >= 0.45 and not severe[i]
            ]
            if launch_pool:
                launch_scores = {
                    i: (
                        escape_success_prob[i] * 0.34
                        + execution_quality[i] * 0.18
                        + recent_form_strength[i] * 0.14
                        + closing_strength[i] * 0.12
                        + place_strength[i] * 0.10
                        + (1.0 / (1.0 + np.exp(-z_scores[i]))) * 0.12
                    )
                    for i in launch_pool
                }
                flow_leader = max(launch_pool, key=lambda i: launch_scores[i])
                flow_pos = int(np.where(order == flow_leader)[0][0])
                chase_pool = [i for i in back_members if not severe[i]]
                if (
                    chase_pool
                    and flow_pos <= 3
                    and escape_success_prob[flow_leader] >= 0.76
                    and max(place_strength[i] for i in chase_pool) >= 0.50
                ):
                    chase_scores = {
                        i: (
                            place_strength[i] * 0.34
                            + traffic_conversion[i] * 0.28
                            + execution_quality[i] * 0.18
                            + closing_strength[i] * 0.12
                            + recent_form_strength[i] * 0.08
                            - same_line_outer_ratio[i] * 0.04
                        )
                        for i in chase_pool
                    }
                    chaser = max(chase_pool, key=lambda i: chase_scores[i])
                    hold_pool = [i for i in front_members if i != flow_leader and not severe[i]]
                    if hold_pool:
                        hold_scores = {
                            i: (
                                (1.0 / (1.0 + np.exp(-z_scores[i]))) * 0.34
                                + place_strength[i] * 0.22
                                + execution_quality[i] * 0.18
                                + recent_form_strength[i] * 0.14
                                + inner_hold_prob[i] * 0.08
                                + same_line_inner_ratio[i] * 0.04
                            )
                            for i in hold_pool
                        }
                        holder = max(hold_pool, key=lambda i: hold_scores[i])
                        chain_prob = np.clip(
                            0.18 + launch_scores[flow_leader] * 0.18
                            + chase_scores[chaser] * 0.12
                            + (0.05 if scenario in ("先行縦長", "前残り") else 0.0)
                            - flow_pos * 0.025,
                            0.16, 0.42,
                        )
                        if rng.random() < chain_prob:
                            selected = [flow_leader, chaser, holder]
                            rest = [int(i) for i in order if int(i) not in selected]
                            order = np.array(selected + rest, dtype=int)

        # B) 全車同一ハンデ線: 外寄りの先行車が切り込み、
        # その内側の勝負強い車が追走し、最内寄りの安定車が3着へ残る隊列。
        if len(unique_lines) == 1 and n >= 6:
            outer_candidates = [
                i for i in range(n)
                if 0.55 <= same_line_outer_ratio[i] < 0.99 and not severe[i]
            ]
            if outer_candidates:
                launch_scores = {
                    i: (
                        escape_success_prob[i] * 0.28
                        + closing_strength[i] * 0.20
                        + place_strength[i] * 0.16
                        + execution_quality[i] * 0.16
                        + recent_form_strength[i] * 0.12
                        + traffic_conversion[i] * 0.08
                    )
                    for i in outer_candidates
                }
                launch_leader = max(outer_candidates, key=lambda i: launch_scores[i])
                follower_pool = [
                    i for i in range(n)
                    if i != launch_leader
                    and cars[i] < cars[launch_leader]
                    and same_line_outer_ratio[i] >= 0.30
                    and not severe[i]
                ]
                rail_pool = [
                    i for i in range(n)
                    if i != launch_leader
                    and same_line_inner_ratio[i] >= 0.70
                    and not severe[i]
                ]
                if follower_pool and rail_pool:
                    follower_scores = {
                        i: (
                            closing_strength[i] * 0.38
                            + place_strength[i] * 0.22
                            + execution_quality[i] * 0.17
                            + traffic_conversion[i] * 0.13
                            + recent_form_strength[i] * 0.10
                            - abs(float(cars[launch_leader] - cars[i]) - 2.0) * 0.025
                        )
                        for i in follower_pool
                    }
                    follower2 = max(follower_pool, key=lambda i: follower_scores[i])
                    rail_pool = [i for i in rail_pool if i != follower2]
                    if rail_pool:
                        rail_scores = {
                            i: (
                                same_line_inner_ratio[i] * 0.42
                                + trial_trust_flow[i] * 0.20
                                + recent_form_strength[i] * 0.16
                                + place_strength[i] * 0.12
                                + st_stability_flow[i] * 0.10
                            )
                            for i in rail_pool
                        }
                        rail3 = max(rail_pool, key=lambda i: rail_scores[i])
                        formation_prob = np.clip(
                            0.11 + launch_scores[launch_leader] * 0.16
                            + follower_scores[follower2] * 0.11
                            + (0.04 if scenario in ("先行縦長", "混戦") else 0.0),
                            0.11, 0.30,
                        )
                        # 現在の先頭が選定先行車、または先頭争い圏内のときだけ成立。
                        launch_pos = int(np.where(order == launch_leader)[0][0])
                        if launch_pos <= 2 and rng.random() < formation_prob:
                            selected = [launch_leader, follower2, rail3]
                            rest = [int(i) for i in order if int(i) not in selected]
                            order = np.array(selected + rest, dtype=int)

        # Ver12.2: 全車同ハンデ限定の6周簡易モデル。
        # 現在の順位を1周目隊列として、隣接する相手だけを抜く現実寄りの処理を重ねる。
        # 前団が密集すると後方ほど突破障壁が増えるが、集団突破力の高い選手は越えられる。
        if dense_line_race and n >= 5:
            group_breakthrough = pd.to_numeric(
                df.get("集団突破力", pd.Series(0.50, index=df.index)), errors="coerce"
            ).fillna(0.50).clip(0, 1).to_numpy(float)

            # 大人数同ハンデ線では、能力順位より先にスタートで線内隊列を作る。
            # 内側は進路を取りやすいが、スタート一気・先行転換力が高い外側は前へ出られる。
            if not all_same_handicap:
                dense_members = [int(i) for i in order if dense_line_mask[int(i)]]
                other_members = [int(i) for i in order if not dense_line_mask[int(i)]]
                if len(dense_members) >= 4:
                    launch_metric = {}
                    for ii in dense_members:
                        launch_metric[ii] = (
                            same_line_launch_strength[ii] * 0.34
                            + lead_conversion_strength[ii] * 0.26
                            + st_stability_flow[ii] * 0.14
                            + trial_launch_strength[ii] * 0.10
                            + same_line_inner_ratio[ii] * 0.16
                            + rng.normal(0.0, 0.075)
                        )
                    dense_members = sorted(dense_members, key=lambda ii: launch_metric[ii], reverse=True)
                    # 最前ハンデ車は序盤の前、密集線はその直後へ置く。
                    front_others = sorted(other_members, key=lambda ii: (handicap[ii], list(order).index(ii)))
                    order = np.array(front_others + dense_members, dtype=int)

            # 1周目の隊列を強く残す。好スタート内枠だけ小さな進路確保を持つ。
            first_lap_pos = np.empty(n, dtype=int)
            for p0, i0 in enumerate(order):
                first_lap_pos[int(i0)] = p0
            front_pack_size = int(min(n - 1, max(3, min(5, dense_line_size // 2 + 1))))

            for lap in range(2, 7):
                # 2〜4周目は前団内の攻防中心、5〜6周目は突破力と終盤力を強める。
                late_phase = max(0.0, (lap - 4) / 2.0)
                new_order = order.copy()
                # 前からではなく後ろから判定し、1周に何人も瞬間移動するのを防ぐ。
                moved = set()
                for pos in range(n - 1, 0, -1):
                    chaser = int(new_order[pos])
                    leader = int(new_order[pos - 1])
                    if chaser in moved or leader in moved:
                        continue

                    # 先頭3台付近が固まるほど壁が厚くなる。後方から前団入口へ来た選手に強く作用。
                    front_density = min(1.0, front_pack_size / 4.0)
                    entering_front = pos <= front_pack_size + 1
                    congestion = (0.13 + 0.24 * front_density) if entering_front else (0.04 + 0.08 * front_density)
                    if dense_line_mask[chaser] and lap <= 4:
                        congestion += same_line_outer_ratio[chaser] * 0.10

                    # 内側の恩恵は「好スタートで前団に入れた」場合のみ。固定の能力加点ではない。
                    leader_early_front = first_lap_pos[leader] <= 2
                    inner_route_hold = (same_line_inner_ratio[leader] ** 1.4) * (0.10 if leader_early_front else 0.025)

                    chase_power = (
                        group_breakthrough[chaser] * (0.32 + 0.20 * late_phase)
                        + closing_strength[chaser] * (0.15 + 0.17 * late_phase)
                        + traffic_conversion[chaser] * 0.18
                        + execution_quality[chaser] * 0.12
                        + rear_chase_flow[chaser] * 0.10
                        + current_day_strength[chaser] * 0.08
                    )
                    stable_top_flow = pd.to_numeric(
                        df.get("安定上位指数", pd.Series(0.50, index=df.index)), errors="coerce"
                    ).fillna(0.50).clip(0, 1).to_numpy(float)
                    hold_power = (
                        position_hold_flow[leader] * 0.21
                        + execution_quality[leader] * 0.16
                        + place_strength[leader] * 0.15
                        + traffic_conversion[leader] * 0.10
                        + st_stability_flow[leader] * 0.08
                        + stable_top_flow[leader] * 0.18
                        + inner_route_hold
                    )

                    # Ver12.2: 能力だけでは抜けず、前団が崩れる「突破機会」が必要。
                    # 前走者の位置維持が弱い、前団内に能力差がある、終盤になるほど隙間が生まれる。
                    leader_instability = np.clip(
                        0.55 - position_hold_flow[leader] * 0.26
                        - execution_quality[leader] * 0.14
                        + volatility_profile[leader] * 0.18,
                        0.08, 0.72
                    )
                    pack_spread = np.clip(
                        abs(execution_quality[chaser] - execution_quality[leader]) * 0.35
                        + abs(current_day_strength[chaser] - current_day_strength[leader]) * 0.25,
                        0.0, 0.35
                    )
                    breakthrough_opportunity = np.clip(
                        0.12 + leader_instability * 0.34 + pack_spread
                        + late_phase * 0.22 - congestion * 0.30,
                        0.035, 0.72
                    )
                    congestion_after_skill = congestion * (1.0 - 0.42 * group_breakthrough[chaser])
                    margin = chase_power - hold_power - congestion_after_skill
                    pass_prob = 1.0 / (1.0 + np.exp(-(margin - 0.085) * 6.6))
                    # 適性と機会を掛け合わせる。強い選手でも混戦が開かなければ待たされる。
                    pass_prob *= breakthrough_opportunity * (0.72 + 0.38 * late_phase)
                    if severe[chaser]:
                        pass_prob *= 0.20
                    elif normal[chaser]:
                        pass_prob *= 0.62
                    if bipolar_flow[chaser] > 0.65 and first_lap_pos[chaser] >= n - 2:
                        pass_prob *= 0.78

                    if rng.random() < np.clip(pass_prob, 0.01, 0.72):
                        new_order[pos - 1], new_order[pos] = chaser, leader
                        moved.add(chaser); moved.add(leader)

                order = new_order

                # 前団内では、好位置維持だけでなく実戦能力のある選手が少しずつ上がれる。
                # これにより3番が2番、続いて1番を抜くような段階的変動を許す。
                if lap <= 4 and front_pack_size >= 2:
                    for pos in range(min(front_pack_size, n - 1), 0, -1):
                        chaser = int(order[pos]); leader = int(order[pos - 1])
                        internal_edge = (
                            group_breakthrough[chaser] * 0.28 + execution_quality[chaser] * 0.24
                            + current_day_strength[chaser] * 0.18 + closing_strength[chaser] * 0.12
                            - position_hold_flow[leader] * 0.22 - execution_quality[leader] * 0.10
                        )
                        internal_prob = np.clip(0.07 + internal_edge * 0.30, 0.015, 0.30)
                        if rng.random() < internal_prob:
                            order[pos - 1], order[pos] = chaser, leader
                            break

        for rank, idx in enumerate(order):
            finish_matrix[int(idx), rank] += 1
        top3 = tuple(int(cars[int(order[i])]) for i in range(3))
        top2 = tuple(int(cars[int(order[i])]) for i in range(2))
        trifecta[top3] += 1
        trio[tuple(sorted(top3))] += 1
        exacta[top2] += 1
        quinella[tuple(sorted(top2))] += 1

    finish_counts = {int(car): Counter({rank + 1: int(finish_matrix[i, rank]) for rank in range(n) if finish_matrix[i, rank] > 0}) for i, car in enumerate(cars)}
    bet_counts = {
        "三連単": trifecta,
        "三連複": trio,
        "2車単": exacta,
        "2車複": quinella,
    }
    return finish_counts, bet_counts



def simulate(df, trials, seed, track_temp=30.0):
    """Ver12.3: 高速ベクトル型の6周近似モデル。

    1周目のST反応とスタート後の伸びを分離し、最終周には僅差の差し判定を追加する。
    車番固定の結果合わせは行わず、全選手共通の指標から確率的に発生させる。
    """
    rng = np.random.default_rng(int(seed))
    arr = prepare_simulation_arrays(df)
    cars = np.asarray(arr["cars"], dtype=int)
    n = len(cars); trials = int(trials)
    handicap = np.asarray(arr["handicap"], dtype=float)
    z = np.asarray(arr["z_scores"], dtype=float)
    finish_sd = np.asarray(arr["finish_sd"], dtype=float)
    st = np.asarray(arr["st"], dtype=float)
    st_stability = np.asarray(arr["st_stability_flow"], dtype=float)
    execution = np.asarray(arr["execution_quality"], dtype=float)
    traffic = np.asarray(arr["traffic_conversion"], dtype=float)
    closing = np.asarray(arr["closing_strength"], dtype=float)
    current = np.asarray(arr.get("current_day_strength", np.full(n, .5)), dtype=float)
    position_hold = np.asarray(arr.get("position_hold_flow", np.full(n, .5)), dtype=float)
    rear_chase = np.asarray(arr.get("rear_chase_flow", np.full(n, .35)), dtype=float)
    dependency = np.asarray(arr.get("position_dependency_flow", np.full(n, .5)), dtype=float)
    bipolar = np.asarray(arr.get("bipolar_flow", np.full(n, .3)), dtype=float)
    volatility = np.asarray(arr.get("volatility_profile", np.full(n, .5)), dtype=float)
    final_kick = np.asarray(arr.get("final_kick_flow", closing), dtype=float)
    recent_form = np.asarray(arr.get("recent_form_strength", current), dtype=float)

    stable = pd.to_numeric(df.get("安定上位指数", pd.Series(.5,index=df.index)), errors="coerce").fillna(.5).clip(0,1).to_numpy(float)
    breakthrough = pd.to_numeric(df.get("集団突破力", pd.Series(.5,index=df.index)), errors="coerce").fillna(.5).clip(0,1).to_numpy(float)
    trial = pd.to_numeric(df.get("試走換算", pd.Series(np.nan,index=df.index)), errors="coerce").to_numpy(float)
    fill=float(np.nanmedian(trial[np.isfinite(trial)])) if np.isfinite(trial).any() else 3.50
    trial=np.where(np.isfinite(trial),trial,fill)
    trial_strength=(trial.max()-trial)/(max(1e-6,trial.max()-trial.min())) if trial.max()>trial.min() else np.full(n,.5)
    st_fill=float(np.nanmedian(st[np.isfinite(st)])) if np.isfinite(st).any() else .15
    st2=np.where(np.isfinite(st),st,st_fill)
    st_strength=(st2.max()-st2)/(max(1e-6,st2.max()-st2.min())) if st2.max()>st2.min() else np.full(n,.5)

    temp=float(track_temp if track_temp is not None else 30.0)
    heat=np.clip((temp-44.0)/12.0,0,1)
    unique_h, counts=np.unique(handicap,return_counts=True)
    dense_h=float(unique_h[np.argmax(counts)])
    dense_mask=np.isclose(handicap,dense_h)
    dense_size=int(counts.max()); same_line=dense_size>=4
    line_indices=np.where(dense_mask)[0]
    lane_ratio=np.zeros(n)
    if len(line_indices)>1:
        order_line=line_indices[np.argsort(cars[line_indices])]
        lane_ratio[order_line]=np.linspace(1.0,0.0,len(order_line))

    # 評価の中心。安定上位は残すが、格だけで外枠が自動突破しないよう抑える。
    base=(z*.76 + stable*.30 + execution*.15 + current*.13 + trial_strength*.11)
    noise_sd=np.clip(.68 + finish_sd*.50 + volatility*.30, .52, 1.45)
    score=base[None,:] + rng.normal(0,noise_sd,size=(trials,n))

    # ST反応と「その後の伸び」を分離。
    reaction = st_strength*.54 + st_stability*.25 + execution*.13 + trial_strength*.08
    start_draw = reaction[None,:] + rng.normal(0,.25,size=(trials,n))
    stretch_index=np.clip(trial_strength*.30 + execution*.25 + st_stability*.16 + current*.14 + recent_form*.10 + traffic*.05,0,1)
    # 上振れは全員に起こり得るが、指数が高いほど頻度と幅が増える。
    stretch_event = rng.random((trials,n)) < np.clip(.06 + stretch_index[None,:]*.24, .05, .30)
    stretch_power = stretch_event * rng.uniform(.12,.58,size=(trials,n)) * (.55 + stretch_index[None,:])
    first_lap = start_draw + stretch_power

    if same_line:
        gate=1/(1+np.exp(-(first_lap-.48)*8.0))
        score += gate * lane_ratio[None,:] * .32
        front_idx=np.argpartition(-first_lap, min(2,n-1), axis=1)[:,:min(3,n)]
        front_mask=np.zeros((trials,n),dtype=bool)
        front_mask[np.arange(trials)[:,None],front_idx]=True
        score += front_mask*(.20 + position_hold[None,:]*.35 + stable[None,:]*.15)
        # スタート伸び上振れ車は、外枠でも前団へ入った価値を上乗せ。
        score += front_mask * stretch_power * .42
        gap=rng.beta(2.0,3.2,size=(trials,1))
        opportunity=np.clip(gap-(dense_size-3)*.035,0,1)
        chase=(~front_mask)*opportunity*(breakthrough[None,:]*.48 + rear_chase[None,:]*.27 + closing[None,:]*.20)
        score += chase
        bad=first_lap < .37
        score -= bad*(dependency[None,:]*.32 + bipolar[None,:]*rng.uniform(.04,.31,size=(trials,n)))
    else:
        min_h=np.nanmin(handicap); front=np.isclose(handicap,min_h)
        score[:,front] += (.12+heat*.22)*position_hold[None,front]
        score[:,~front] += rear_chase[None,~front]*.18 + closing[None,~front]*.12
        score += stretch_power*.20

    # 中盤の突破機会。
    finish_gap=rng.beta(2.2,2.8,size=(trials,1))
    score += finish_gap*(closing[None,:]*.18 + breakthrough[None,:]*.16)

    # 最終周のゴール前伸び。大逆転ではなく、近い相手だけを僅差で交わせるようにする。
    goal_kick_index=np.clip(final_kick*.36 + closing*.25 + execution*.15 + current*.10 + trial_strength*.08 + rear_chase*.06,0,1)
    goal_event=rng.random((trials,n)) < np.clip(.08 + goal_kick_index[None,:]*.22, .06, .29)
    goal_power=goal_event*rng.uniform(.05,.26,size=(trials,n))*(.65+goal_kick_index[None,:])
    score += goal_power
    score += rng.normal(0,.055,size=(trials,n))

    # いったん順位化し、僅差の隣接2台だけ最終周差しを追加。
    order=np.argsort(-score,axis=1)
    sorted_score=np.take_along_axis(score,order,axis=1)
    for rank in range(n-1):
        lead_idx=order[:,rank]; chase_idx=order[:,rank+1]
        margin=sorted_score[:,rank]-sorted_score[:,rank+1]
        chase_kick=goal_power[np.arange(trials),chase_idx]
        lead_hold=position_hold[lead_idx]*.11 + stable[lead_idx]*.06
        pass_mask=(margin < .22) & ((chase_kick-lead_hold) > margin*.55) & (rng.random(trials)<.52)
        if np.any(pass_mask):
            a=order[pass_mask,rank].copy(); b=order[pass_mask,rank+1].copy()
            order[pass_mask,rank]=b; order[pass_mask,rank+1]=a

    finish_matrix=np.zeros((n,n),dtype=np.int64)
    for rank in range(n): finish_matrix[:,rank]=np.bincount(order[:,rank],minlength=n)
    top3=cars[order[:,:3]]; top2=cars[order[:,:2]]
    def make_counter(a):
        vals,cnt=np.unique(a,axis=0,return_counts=True)
        return Counter({tuple(map(int,v)):int(c) for v,c in zip(vals,cnt)})
    finish_counts={int(car):Counter({rank+1:int(finish_matrix[i,rank]) for rank in range(n) if finish_matrix[i,rank]>0}) for i,car in enumerate(cars)}
    return finish_counts,{"三連単":make_counter(top3),"三連複":make_counter(np.sort(top3,axis=1)),"2車単":make_counter(top2),"2車複":make_counter(np.sort(top2,axis=1))}

def style_header(ws, row, last_col):
    fill = PatternFill("solid", fgColor="4472C4")
    font = Font(color="FFFFFF", bold=True)
    for c in range(1, last_col + 1):
        cell = ws.cell(row, c)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")


def auto_width(ws):
    for col_idx in range(1, ws.max_column + 1):
        max_len = 0
        for row_idx in range(1, ws.max_row + 1):
            cell = ws.cell(row_idx, col_idx)
            if isinstance(cell, MergedCell):
                continue
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(
            max(max_len + 2, 10), 24
        )


def create_result_excel(content, filename, df, finish_counts, bet_counts, trials, track_temp=30.0):
    wb = load_workbook(io.BytesIO(content), data_only=False)

    generated_sheets = [
        "Colab計算結果", "着順分布",
        "三連単確率", "三連単期待値", "三連複確率", "三連複期待値",
        "2車単確率", "2車単期待値", "2車複確率", "2車複期待値",
    ]
    for sheet_name in generated_sheets:
        if sheet_name in wb.sheetnames:
            del wb[sheet_name]

    result = wb.create_sheet("Colab計算結果")
    dist = wb.create_sheet("着順分布")

    result.merge_cells("A1:AV1")
    result["A1"] = f"AutoRaceAI Ver12.2 走路環境結果（走路温度 {track_temp:.0f}℃）"
    result["A1"].fill = PatternFill("solid", fgColor="17365D")
    result["A1"].font = Font(color="FFFFFF", bold=True, size=14)
    result["A1"].alignment = Alignment(horizontal="center")

    output_cols = [
        "車", "選手名", "ハンデ", "試走換算", "直近5差", "平均ST",
        "ST標準偏差", "ST安定性", "同ハンデ差", "開催場差", "走路差", "着順指数", "勝率",
        "連対率", "3着以内率", "平均着順", "着順安定度",
        "審査P", "現ランク", "上昇度", "再現性", "今回ハンデ勝率",
        "試走点", "試走単発救済点", "近況主役点", "最近点", "ST点", "ハンデ点", "開催場点",
        "走路点", "位置点", "内枠展開点", "最内勝ち切り点", "着順点", "勝率点", "連対点",
        "上昇点", "再現性点", "ハンデ勝率点", "3着以内率点", "平均着順点", "着順安定度点",
        "審査Pランク点", "試走信頼度", "レース巧者指数", "レース巧者点",
        "逃げ成功率", "内枠残存率", "位置活用率", "同ハンデST優位度", "レースタイプ", "改良総合点",
        "改良順位", "前走ハンデ", "直近5走平均ハンデ", "ハンデ改善量", "ハンデ変化点", "同ハンデ人数",
        "同ハンデ外順", "外枠不利補正", "直近3走好調補正", "巻き返し余地補正", "実戦再現力補正",
        "勝負余力補正", "内側高信頼補正", "近況単独過大補正", "選手タイプ総合補正",
        "中位粘り率", "中位粘り補正", "条件改善再現補正", "中位内容総合補正",
        "上昇カーブ指数", "上昇カーブ補正", "終盤指数", "終盤力補正", "相手レベル耐性補正",
        "Ver10_6総合補正", "選手タイプVer2", "改善後総合点", "改善後順位",
    ]
    output_cols = [c for c in output_cols if c in df.columns]
    # シミュレーションと同じ定義で安定指数を表示する。
    finish_sd = pd.to_numeric(df.get("着順標準偏差", pd.Series(1.5, index=df.index)), errors="coerce").fillna(1.5).clip(0.8, 3.0)
    finish_consistency = (1.0 - (finish_sd - 0.8) / 2.2).clip(0.0, 1.0)
    df = df.copy()
    df["走路安定指数"] = (
        0.42 * finish_consistency
        + 0.31 * pd.to_numeric(df.get("ST安定性", 0.5), errors="coerce").fillna(0.5).clip(0, 1)
        + 0.17 * pd.to_numeric(df.get("試走信頼度", 0.5), errors="coerce").fillna(0.5).clip(0.2, 1)
        + 0.10 * pd.to_numeric(df.get("近況信頼度", 0.5), errors="coerce").fillna(0.5).clip(0, 1)
    ).clip(0, 1)
    df["走路安定タイプ"] = pd.cut(df["走路安定指数"], [-0.01, 0.48, 0.66, 1.01], labels=["ムラ型", "標準型", "安定型"]).astype(str)
    output_cols += ["走路安定指数", "走路安定タイプ"]

    for c, name in enumerate(output_cols, 1): result.cell(3, c, name)
    style_header(result, 3, len(output_cols))
    for r, (_, row) in enumerate(df.iterrows(), 4):
        for c, name in enumerate(output_cols, 1):
            value = row.get(name)
            result.cell(r, c, None if pd.isna(value) else value)
    percent_names = {"勝率", "連対率", "3着以内率", "今回ハンデ勝率", "走路安定指数"}
    for c, name in enumerate(output_cols, 1):
        if name in percent_names:
            for r in range(4, 4 + len(df)): result.cell(r, c).number_format = "0.0%"

    dist.merge_cells("A1:J1")
    dist["A1"] = "着順分布"
    dist["A1"].fill = PatternFill("solid", fgColor="17365D")
    dist["A1"].font = Font(color="FFFFFF", bold=True, size=14)
    dist["A1"].alignment = Alignment(horizontal="center")
    dist_headers = ["車", "選手名"] + [f"{rank}着率" for rank in range(1, 9)]
    for c, name in enumerate(dist_headers, 1): dist.cell(3, c, name)
    style_header(dist, 3, len(dist_headers))
    for r, (_, row) in enumerate(df.iterrows(), 4):
        car = int(row["車"])
        values = [car, row["選手名"]] + [finish_counts[car][rank] / trials for rank in range(1, 9)]
        for c, value in enumerate(values, 1):
            dist.cell(r, c, value)
            if c >= 3: dist.cell(r, c).number_format = "0.0%"

    def make_probability_and_ev(ticket_name, counter):
        prob = wb.create_sheet(f"{ticket_name}確率")
        ev = wb.create_sheet(f"{ticket_name}期待値")
        ordered = ticket_name in ("三連単", "2車単")
        pick_count = 3 if ticket_name.startswith("三連") else 2
        labels = (["1着", "2着", "3着"] if pick_count == 3 and ordered else
                  ["車1", "車2", "車3"] if pick_count == 3 else
                  ["1着", "2着"] if ordered else ["車1", "車2"])
        prob.merge_cells(start_row=1, start_column=1, end_row=1, end_column=pick_count + 3)
        prob["A1"] = f"{ticket_name}確率（Ver11.1走路環境補正）"
        prob["A1"].fill = PatternFill("solid", fgColor="17365D")
        prob["A1"].font = Font(color="FFFFFF", bold=True, size=14)
        headers = ["順位"] + labels + ["組合せ", "確率"]
        for c, name in enumerate(headers, 1): prob.cell(3, c, name)
        style_header(prob, 3, len(headers))
        sorted_items = sorted(counter.items(), key=lambda x: x[1], reverse=True)
        for rank, (combo, count) in enumerate(sorted_items, 1):
            row = rank + 3
            values = [rank] + list(combo) + ["-".join(map(str, combo)), count / trials]
            for c, value in enumerate(values, 1): prob.cell(row, c, value)
            prob.cell(row, len(headers)).number_format = "0.000%"

        ev.merge_cells(start_row=1, start_column=1, end_row=1, end_column=pick_count + 7)
        ev["A1"] = f"{ticket_name}期待値（確率 × 実オッズ）"
        ev["A1"].fill = PatternFill("solid", fgColor="17365D")
        ev["A1"].font = Font(color="FFFFFF", bold=True, size=14)
        ev_headers = ["順位"] + labels + ["組合せ", "補正確率", "適正オッズ", "実オッズ入力", "期待値倍率", "期待値判定"]
        for c, name in enumerate(ev_headers, 1): ev.cell(3, c, name)
        style_header(ev, 3, len(ev_headers))
        prob_col = 2 + pick_count + 1
        fair_col = prob_col + 1; odds_col = fair_col + 1; value_col = odds_col + 1; judge_col = value_col + 1
        from openpyxl.utils import get_column_letter
        for rank, (combo, count) in enumerate(sorted_items, 1):
            row = rank + 3; p = count / trials
            values = [rank] + list(combo) + ["-".join(map(str, combo)), p, (1/p if p > 0 else None)]
            for c, value in enumerate(values, 1): ev.cell(row, c, value)
            ev.cell(row, prob_col).number_format = "0.000%"
            ev.cell(row, fair_col).number_format = "0.00"
            ev.cell(row, odds_col).number_format = "0.00"
            ev.cell(row, value_col, f'=IF({get_column_letter(odds_col)}{row}="","",{get_column_letter(prob_col)}{row}*{get_column_letter(odds_col)}{row})')
            ev.cell(row, value_col).number_format = "0.000"
            ev.cell(row, judge_col, f'=IF({get_column_letter(value_col)}{row}="","",IF({get_column_letter(value_col)}{row}>=1,"期待値あり","期待値不足"))')
        return prob, ev

    sheets = [result, dist]
    for ticket in ["三連単", "三連複", "2車単", "2車複"]:
        sheets.extend(make_probability_and_ev(ticket, bet_counts[ticket]))
    for ws in sheets:
        ws.freeze_panes = "A4"
        auto_width(ws)

    path = str(APP_DIR / f"{Path(filename).stem}_prediction.xlsx")
    wb.save(path)
    return path


def run_model(content, filename, trials, seed, track_temp=30.0):
    wb = load_workbook(io.BytesIO(content), data_only=True)

    required = ["レース予測", "設定"] + [f"選手{i}" for i in range(1, 9)]
    missing = [name for name in required if name not in wb.sheetnames]
    if missing:
        raise ValueError("不足シート: " + ", ".join(missing))

    race = read_race(wb["レース予測"])
    settings = read_settings(wb["設定"])

    metrics = []
    for car in range(1, 9):
        ws = wb[f"選手{car}"]
        current = current_player(ws)
        history = read_history(ws)
        metrics.append(
            player_metrics(car, current, history, race, settings)
        )

    df = calculate_excel_model(metrics, settings)

    # Ver12.2 診断列。シミュレーションと同じ考え方で、スタート一気と先行転換を表示する。
    st_v = pd.to_numeric(df["平均ST"], errors="coerce").fillna(0.20).to_numpy(float)
    trial_v = pd.to_numeric(df["試走換算"], errors="coerce").fillna(pd.to_numeric(df["試走換算"], errors="coerce").median()).to_numpy(float)
    def _rank_lower(v):
        v = np.asarray(v, dtype=float); idx = np.argsort(v, kind="stable")
        out = np.zeros(len(v), dtype=float)
        out[idx] = 0.5 if len(v) <= 1 else 1.0 - np.arange(len(v), dtype=float) / (len(v) - 1.0)
        return out
    st_stable_v = pd.to_numeric(df["ST安定性"], errors="coerce").fillna(0.50).clip(0, 1).to_numpy(float)
    execution_v = pd.to_numeric(df["実戦変換力"], errors="coerce").fillna(0.50).clip(0, 1).to_numpy(float) if "実戦変換力" in df.columns else pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.50).clip(0, 1).to_numpy(float)
    traffic_v = pd.to_numeric(df["レース巧者指数"], errors="coerce").fillna(0.50).clip(0, 1).to_numpy(float)
    road_v = pd.to_numeric(df.get("走路安定指数", pd.Series(0.50, index=df.index)), errors="coerce").fillna(0.50).clip(0, 1).to_numpy(float)
    trust_v = pd.to_numeric(df["試走信頼度"], errors="coerce").fillna(0.50).clip(0, 1).to_numpy(float)
    df["スタート一気指数"] = np.clip(_rank_lower(st_v) * 0.46 + st_stable_v * 0.19 + _rank_lower(trial_v) * 0.22 + execution_v * 0.13, 0, 1)
    df["先行転換力"] = np.clip(df["スタート一気指数"].to_numpy(float) * 0.43 + st_stable_v * 0.13 + execution_v * 0.18 + traffic_v * 0.14 + road_v * 0.08 + trust_v * 0.04, 0, 1)
    same_line_diag = bool(len(df) >= 3 and pd.to_numeric(df["ハンデ"], errors="coerce").max() - pd.to_numeric(df["ハンデ"], errors="coerce").min() < 1e-9)
    if same_line_diag:
        inner_ratio_diag = 1.0 - (pd.to_numeric(df["車"], errors="coerce").fillna(1.0).to_numpy(float) - 1.0) / max(1.0, len(df) - 1.0)
        df["隊列残存指数"] = np.clip(inner_ratio_diag * (df["スタート一気指数"].to_numpy(float) * 0.48 + df["先行転換力"].to_numpy(float) * 0.52), 0, 1)
    else:
        df["隊列残存指数"] = 0.0

    # Ver12.2: 能力が高い選手は混戦の壁を越えられるよう、全選手共通の集団突破力を作る。
    # 内枠・車番はここには入れず、実戦能力・終盤・交通処理・勝負強さ・後方追上げで構成する。
    def _norm01_col(col, default=50.0):
        v = pd.to_numeric(df.get(col, pd.Series(default, index=df.index)), errors="coerce").fillna(default).to_numpy(float)
        if np.nanmax(v) <= 1.5:
            return np.clip(v, 0, 1)
        lo, hi = np.nanpercentile(v, 10), np.nanpercentile(v, 90)
        if hi - lo < 1e-9:
            return np.full(len(v), 0.5)
        return np.clip((v - lo) / (hi - lo), 0, 1)

    practical_v = _norm01_col("実戦能力点")
    closing_v = _norm01_col("終盤指数")
    traffic_v2 = _norm01_col("レース巧者指数")
    clutch_v = _norm01_col("勝負強さ点")
    rear_v = np.clip(pd.to_numeric(df.get("後方追上げ指数", pd.Series(0.35, index=df.index)), errors="coerce").fillna(0.35).to_numpy(float), 0, 1)
    trial_v2 = _norm01_col("当日レース指数")
    stable_v2 = np.clip(pd.to_numeric(df.get("安定上位指数", pd.Series(0.50, index=df.index)), errors="coerce").fillna(0.50).to_numpy(float), 0, 1)
    recent_poor_v2 = np.clip(pd.to_numeric(df.get("直近5走生凡走率", pd.Series(0.25, index=df.index)), errors="coerce").fillna(0.25).to_numpy(float), 0, 1)

    raw_breakthrough = (
        practical_v * 0.23 + closing_v * 0.20 + traffic_v2 * 0.18
        + clutch_v * 0.10 + rear_v * 0.13 + trial_v2 * 0.08
        + stable_v2 * 0.08
    )
    # 能力差を0～1へ広げすぎず、強い選手でも「機会がなければ抜けない」範囲へ圧縮。
    df["混戦突破適性"] = np.clip(
        0.26 + raw_breakthrough * 0.50 - recent_poor_v2 * 0.08,
        0.18, 0.78
    )
    df["集団突破力"] = df["混戦突破適性"]

    # Ver12.3 診断列：ST反応とは別のスタート伸びと、最終周の伸びを表示。
    _trial_s = _norm01_col("当日レース指数")
    _exec_s = _norm01_col("実戦能力点")
    _ststab_s = np.clip(pd.to_numeric(df.get("ST安定性", pd.Series(.5,index=df.index)), errors="coerce").fillna(.5).to_numpy(float),0,1)
    _current_s = np.clip(pd.to_numeric(df.get("当日状態指数", pd.Series(.5,index=df.index)), errors="coerce").fillna(.5).to_numpy(float),0,1)
    _recent_s = np.clip(pd.to_numeric(df.get("近況信頼度", pd.Series(.5,index=df.index)), errors="coerce").fillna(.5).to_numpy(float),0,1)
    df["スタート伸び指数"] = np.clip(_trial_s*.30 + _exec_s*.25 + _ststab_s*.16 + _current_s*.14 + _recent_s*.10 + traffic_v2*.05,0,1)
    _final_s = _norm01_col("終盤指数")
    df["ゴール前伸び指数"] = np.clip(_final_s*.36 + closing_v*.25 + _exec_s*.15 + _current_s*.10 + _trial_s*.08 + rear_v*.06,0,1)

    finish_counts, bet_counts = simulate_detailed(df, trials, seed, track_temp=track_temp)
    output = create_result_excel(
        content, filename, df, finish_counts, bet_counts, trials, track_temp=track_temp
    )
    return df, bet_counts, output




# ============================================================
# AutoRaceAI Ver15.0
# スマホ全文コピペ予測・選手履歴追加・結果登録
# ============================================================

import re
import json
import math
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd



# Ver14.1までのDB_PATHを優先して利用します。
if "DB_PATH" not in globals():
    DB_DIR = Path("/content/drive/MyDrive/AutoRaceAI")
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DB_DIR / "autorace_players.sqlite3")

V15_STATE = {
    "race_meta": {},
    "entries": pd.DataFrame(),
    "player_history": pd.DataFrame(),
    "raw_prediction_text": "",
    "raw_player_text": "",
    "raw_result_text": "",
}

V15_ENTRY_COLUMNS = [
    "車番", "選手名", "ハンデ", "試走T", "ST", "年齢", "級別", "期別"
]

V15_HISTORY_COLUMNS = [
    "開催日", "開催場", "レース", "着順", "出走", "走路",
    "ハンデ", "試走T", "競走T", "ST"
]


# ------------------------------
# 共通ユーティリティ
# ------------------------------
def v15_clean_text(text):
    text = str(text or "")
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("℃", "℃ ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r\n?", "\n", text)
    return text.strip()


def v15_float(value):
    if value is None:
        return np.nan
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else np.nan


def v15_int(value):
    x = v15_float(value)
    return int(x) if pd.notna(x) else None


def v15_normalize_name(name):
    name = str(name or "").strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"^[0-9]+\s*", "", name)
    # 公式サイトの予想印・お気に入り印を氏名から除去
    name = re.sub(r"^[◎○◯▲△×注☆★◇◆□■・]+\s*", "", name)
    return name.strip()


def v15_hash(*parts):
    joined = "|".join("" if p is None else str(p).strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def v15_table_exists(con, table):
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return row is not None


def v15_columns(con, table):
    if not v15_table_exists(con, table):
        return []
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def v15_first_match(patterns, text, flags=0):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return m.group(1).strip()
    return None


# ------------------------------
# レース情報解析
# ------------------------------
def v15_parse_race_meta(text):
    text = v15_clean_text(text)
    compact = re.sub(r"\s+", " ", text)

    date_raw = v15_first_match([
        r"((?:20)?\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ], compact)

    # 上のfirst_matchでは複数groupを扱えないため、日付のみ個別処理
    date_iso = None
    dm = re.search(r"((?:20)?\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", compact)
    if dm:
        y = int(dm.group(1))
        if y < 100:
            y += 2000
        date_iso = f"{y:04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    else:
        dm = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", compact)
        if dm:
            date_iso = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

    distance = v15_int(v15_first_match([
        r"(\d{4})\s*m",
        r"距離[:：]?\s*(\d{4})",
    ], compact, re.I))

    laps = v15_int(v15_first_match([
        r"\((\d+)\s*周\)",
        r"(\d+)\s*周",
    ], compact))

    start_time = v15_first_match([
        r"発走(?:予定)?時間\s*[:：]?\s*(\d{1,2}:\d{2})",
        r"発走\s*[:：]?\s*(\d{1,2}:\d{2})",
    ], compact)

    weather = v15_first_match([
        r"天候\s*[:：]\s*([^\s　]+)",
    ], compact)

    surface = v15_first_match([
        r"走路状況\s*[:：]\s*([^\s　]+)",
        r"走路状態\s*[:：]\s*([^\s　]+)",
    ], compact)

    track_temp = v15_float(v15_first_match([
        r"走路温度\s*[:：]\s*(-?\d+(?:\.\d+)?)",
    ], compact))

    air_temp = v15_float(v15_first_match([
        r"(?<!走路)気温\s*[:：]\s*(-?\d+(?:\.\d+)?)",
    ], compact))

    humidity = v15_float(v15_first_match([
        r"湿度\s*[:：]\s*(\d+(?:\.\d+)?)",
    ], compact))

    race_no = v15_int(v15_first_match([
        r"(?:第\s*)?(\d{1,2})\s*R",
        r"レース番号\s*[:：]?\s*(\d{1,2})",
    ], compact, re.I))

    venue = v15_first_match([
        r"(川口|伊勢崎|浜松|山陽|飯塚)\s*(?:オート|走路|開催)?",
        r"開催場\s*[:：]?\s*([^\s　]+)",
    ], compact)

    race_type = None
    race_type_candidates = [
        "SG優勝戦", "G1優勝戦", "G2優勝戦", "優勝戦",
        "準決勝戦", "準決勝", "選抜戦", "特別選抜戦",
        "一般戦", "予選", "最終予選", "二次予選", "一次予選"
    ]
    for label in race_type_candidates:
        if label in compact:
            race_type = label
            break

    race_title = None
    title_match = re.search(
        r"((?:一般戦|準決勝戦?|優勝戦|選抜戦|特別選抜戦|予選)[^0-9\n]{0,20})\s*(\d{4})\s*m",
        compact
    )
    if title_match:
        race_title = title_match.group(1).strip()

    prize = v15_int(v15_first_match([
        r"1着賞金\s*([\d,]+)\s*円",
    ], compact))

    time_band = None
    if start_time:
        hour = int(start_time.split(":")[0])
        if hour < 16:
            time_band = "昼"
        elif hour < 18:
            time_band = "夕方"
        else:
            time_band = "夜"

    meta = {
        "開催日": date_iso,
        "開催場": venue,
        "レース": race_no,
        "レース名": race_title or race_type,
        "レース種別": race_type,
        "距離": distance,
        "周回数": laps,
        "発走時刻": start_time,
        "時間帯": time_band,
        "天候": weather,
        "走路状態": surface,
        "走路温度": track_temp,
        "気温": air_temp,
        "湿度": humidity,
        "1着賞金": prize,
    }
    return meta


# ------------------------------
# 出走表解析
# ------------------------------
def v15_parse_entry_line(line):
    line = v15_clean_text(line)
    if not line:
        return None

    # タブ区切りを優先
    parts = [p.strip() for p in re.split(r"\t+| {2,}", line) if p.strip()]
    first = re.match(r"^\s*([1-8])(?:\s+|[　\t])", line)
    if not first:
        # コピー時に車番と選手名が連結する場合
        first = re.match(r"^\s*([1-8])\s*([^\d\s].+)", line)
    if not first:
        return None

    car_no = int(first.group(1))

    # よくある数値を拾う
    handicap_match = re.search(r"(?<!\d)(0|10|20|30|40|50|60|70|80)\s*m?(?!\d)", line)
    trial_match = re.search(r"(?<!\d)(3\.\d{2})(?!\d)", line)
    st_match = re.search(r"(?:ST|スタート)\s*[:：]?\s*([+-]?\d?\.\d{2,3})", line, re.I)

    # 名前候補。車番の後から、ハンデ・タイム等の前まで
    body = re.sub(r"^\s*[1-8]\s*", "", line).strip()
    stop_positions = []
    for pat in [
        r"\s(?:0|10|20|30|40|50|60|70|80)\s*m?(?:\s|$)",
        r"\s3\.\d{2}(?:\s|$)",
        r"\s[AB]\d(?:\s|$)",
        r"\s\d{1,2}期(?:\s|$)",
    ]:
        m = re.search(pat, body)
        if m:
            stop_positions.append(m.start())
    name_area = body[:min(stop_positions)] if stop_positions else body

    # 級別・年齢・期別のような末尾情報を除外
    name_area = re.sub(r"\s+[AB]\d.*$", "", name_area).strip()
    name_area = re.sub(r"\s+\d{1,2}期.*$", "", name_area).strip()
    name_area = re.sub(r"\s+\d{2}歳.*$", "", name_area).strip()

    # 名前が取れないときは分割部品から探索
    if len(name_area) < 2 and len(parts) >= 2:
        name_area = parts[1]

    name = v15_normalize_name(name_area)
    if not name or name in {"車番", "選手名", "枠番"}:
        return None

    age = v15_int(v15_first_match([r"(\d{2})\s*歳"], line))
    grade = v15_first_match([r"\b([AB][12])\b"], line)
    term = v15_int(v15_first_match([r"(\d{1,2})\s*期"], line))

    return {
        "車番": car_no,
        "選手名": name,
        "ハンデ": v15_int(handicap_match.group(1)) if handicap_match else None,
        "試走T": v15_float(trial_match.group(1)) if trial_match else np.nan,
        "ST": v15_float(st_match.group(1)) if st_match else np.nan,
        "年齢": age,
        "級別": grade,
        "期別": term,
    }


def v15_parse_entries(text):
    rows = []
    seen = set()

    for raw in v15_clean_text(text).splitlines():
        row = v15_parse_entry_line(raw)
        if not row:
            continue
        key = row["車番"]
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    df = pd.DataFrame(rows, columns=V15_ENTRY_COLUMNS)
    if not df.empty:
        df = df.sort_values("車番").reset_index(drop=True)
    return df


def v15_parse_prediction_page(text):
    meta = v15_parse_race_meta(text)
    entries = v15_parse_entries(text)
    return meta, entries


# ------------------------------
# 選手履歴解析
# ------------------------------
def v15_guess_history_row(line):
    original = line
    line = v15_clean_text(line)

    dm = re.search(r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})", line)
    if not dm:
        return None
    date_iso = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

    venue = None
    for v in ["川口", "伊勢崎", "浜松", "山陽", "飯塚"]:
        if v in line:
            venue = v
            break

    race_no = v15_int(v15_first_match([r"(\d{1,2})\s*R"], line, re.I))

    rank = None
    rank_match = re.search(r"(?:^|\s)([1-8])\s*着(?:\s|$)", line)
    if rank_match:
        rank = int(rank_match.group(1))
    else:
        # 表形式では着順が単独数字のことがある
        nums = re.findall(r"(?:^|\s)([1-8])(?:\s|$)", line)
        if nums:
            rank = int(nums[-1])

    surface = None
    for s in ["良走路", "湿走路", "斑走路", "風走路", "良", "湿", "斑"]:
        if s in line:
            surface = s
            break

    handicap = None
    hm = re.search(r"(?<!\d)(0|10|20|30|40|50|60|70|80)\s*m?(?!\d)", line)
    if hm:
        handicap = int(hm.group(1))

    times = [float(x) for x in re.findall(r"(?<!\d)(3\.\d{2}|4\.\d{2}|[12]\.\d{2})(?!\d)", line)]
    trial = times[0] if times else np.nan
    race_time = times[1] if len(times) >= 2 else np.nan

    st = np.nan
    stm = re.search(r"(?:ST|スタート)\s*[:：]?\s*([+-]?\d?\.\d{2,3})", line, re.I)
    if stm:
        st = float(stm.group(1))
    elif len(times) >= 3:
        st = times[-1]

    starters = v15_int(v15_first_match([
        r"(\d)\s*車",
        r"出走\s*[:：]?\s*(\d)",
    ], line))

    return {
        "開催日": date_iso,
        "開催場": venue,
        "レース": race_no,
        "着順": rank,
        "出走": starters,
        "走路": surface,
        "ハンデ": handicap,
        "試走T": trial,
        "競走T": race_time,
        "ST": st,
        "_raw": original,
    }


def v15_parse_player_history(text, player_name=None):
    rows = []
    for line in v15_clean_text(text).splitlines():
        row = v15_guess_history_row(line)
        if row:
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["選手名"] + V15_HISTORY_COLUMNS)

    if player_name:
        df.insert(0, "選手名", v15_normalize_name(player_name))
    else:
        # ページ先頭の氏名らしき文字列
        guessed = v15_first_match([
            r"選手名\s*[:：]\s*([^\n]+)",
            r"プロフィール\s+([^\n]+)",
        ], v15_clean_text(text))
        df.insert(0, "選手名", v15_normalize_name(guessed) if guessed else "")

    return df[["選手名"] + V15_HISTORY_COLUMNS + ["_raw"]]


# ------------------------------
# Ver15用DB
# ------------------------------
def v15_init_tables(db_path=DB_PATH):
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS v15_race_inputs (
                race_key TEXT PRIMARY KEY,
                race_date TEXT,
                venue TEXT,
                race_no INTEGER,
                race_name TEXT,
                race_type TEXT,
                distance INTEGER,
                laps INTEGER,
                start_time TEXT,
                time_band TEXT,
                weather TEXT,
                surface TEXT,
                track_temp REAL,
                air_temp REAL,
                humidity REAL,
                first_prize INTEGER,
                raw_text TEXT,
                created_at TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS v15_race_entry_inputs (
                race_key TEXT,
                car_no INTEGER,
                player_name TEXT,
                handicap INTEGER,
                trial_time REAL,
                st REAL,
                age INTEGER,
                grade TEXT,
                term INTEGER,
                created_at TEXT,
                PRIMARY KEY (race_key, car_no)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS v15_similarity_weights (
                feature_name TEXT PRIMARY KEY,
                initial_weight REAL,
                current_weight REAL,
                updated_at TEXT,
                reason TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS v15_player_history_imports (
                history_key TEXT PRIMARY KEY,
                player_name TEXT,
                race_date TEXT,
                venue TEXT,
                race_no INTEGER,
                rank INTEGER,
                starters INTEGER,
                surface TEXT,
                handicap INTEGER,
                trial_time REAL,
                race_time REAL,
                st REAL,
                raw_line TEXT,
                created_at TEXT
            )
        """)

        defaults = {
            "走路状態": 0.22,
            "走路温度": 0.18,
            "ハンデ構成": 0.18,
            "開催場": 0.12,
            "距離": 0.10,
            "湿度": 0.08,
            "気温": 0.06,
            "レース種別": 0.04,
            "発走時間": 0.02,
        }
        now = datetime.now().isoformat(timespec="seconds")
        for name, weight in defaults.items():
            con.execute("""
                INSERT OR IGNORE INTO v15_similarity_weights
                (feature_name, initial_weight, current_weight, updated_at, reason)
                VALUES (?, ?, ?, ?, ?)
            """, (name, weight, weight, now, "Ver15.0初期値"))
        con.commit()


def v15_race_key(meta):
    return v15_hash(
        meta.get("開催日"), meta.get("開催場"), meta.get("レース"),
        meta.get("距離"), meta.get("発走時刻")
    )


def v15_save_race_input(meta, entries, raw_text, db_path=DB_PATH):
    v15_init_tables(db_path)
    key = v15_race_key(meta)
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as con:
        con.execute("""
            INSERT OR REPLACE INTO v15_race_inputs (
                race_key, race_date, venue, race_no, race_name, race_type,
                distance, laps, start_time, time_band, weather, surface,
                track_temp, air_temp, humidity, first_prize, raw_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            key, meta.get("開催日"), meta.get("開催場"), meta.get("レース"),
            meta.get("レース名"), meta.get("レース種別"), meta.get("距離"),
            meta.get("周回数"), meta.get("発走時刻"), meta.get("時間帯"),
            meta.get("天候"), meta.get("走路状態"), meta.get("走路温度"),
            meta.get("気温"), meta.get("湿度"), meta.get("1着賞金"),
            raw_text, now
        ))

        for _, row in entries.iterrows():
            con.execute("""
                INSERT OR REPLACE INTO v15_race_entry_inputs (
                    race_key, car_no, player_name, handicap, trial_time,
                    st, age, grade, term, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                key, int(row["車番"]), row["選手名"],
                None if pd.isna(row["ハンデ"]) else int(row["ハンデ"]),
                None if pd.isna(row["試走T"]) else float(row["試走T"]),
                None if pd.isna(row["ST"]) else float(row["ST"]),
                None if pd.isna(row["年齢"]) else int(row["年齢"]),
                None if pd.isna(row["級別"]) else str(row["級別"]),
                None if pd.isna(row["期別"]) else int(row["期別"]),
                now
            ))
        con.commit()
    return key


def v15_save_player_history(df, db_path=DB_PATH):
    if df is None or df.empty:
        return 0, 0

    v15_init_tables(db_path)
    inserted = 0
    skipped = 0
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as con:
        for _, row in df.iterrows():
            key = v15_hash(
                row.get("選手名"), row.get("開催日"), row.get("開催場"),
                row.get("レース"), row.get("着順"), row.get("試走T"),
                row.get("競走T")
            )
            exists = con.execute(
                "SELECT 1 FROM v15_player_history_imports WHERE history_key=?",
                (key,)
            ).fetchone()
            if exists:
                skipped += 1
                continue

            con.execute("""
                INSERT INTO v15_player_history_imports (
                    history_key, player_name, race_date, venue, race_no,
                    rank, starters, surface, handicap, trial_time,
                    race_time, st, raw_line, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                key, row.get("選手名"), row.get("開催日"),
                row.get("開催場"),
                None if pd.isna(row.get("レース")) else int(row.get("レース")),
                None if pd.isna(row.get("着順")) else int(row.get("着順")),
                None if pd.isna(row.get("出走")) else int(row.get("出走")),
                row.get("走路"),
                None if pd.isna(row.get("ハンデ")) else int(row.get("ハンデ")),
                None if pd.isna(row.get("試走T")) else float(row.get("試走T")),
                None if pd.isna(row.get("競走T")) else float(row.get("競走T")),
                None if pd.isna(row.get("ST")) else float(row.get("ST")),
                row.get("_raw"), now
            ))
            inserted += 1
        con.commit()

    return inserted, skipped


# ------------------------------
# 類似レース候補
# ------------------------------
def v15_similarity_weights(db_path=DB_PATH):
    v15_init_tables(db_path)
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(
            "SELECT feature_name, current_weight FROM v15_similarity_weights",
            con
        )
    return dict(zip(df["feature_name"], df["current_weight"]))


def v15_handicap_signature(entries):
    if entries is None or entries.empty or "ハンデ" not in entries:
        return {}
    vals = pd.to_numeric(entries["ハンデ"], errors="coerce").dropna().astype(int)
    return vals.value_counts().sort_index().to_dict()


def v15_numeric_similarity(a, b, scale):
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return None
    return max(0.0, 1.0 - abs(float(a) - float(b)) / float(scale))


def v15_categorical_similarity(a, b):
    if not a or not b:
        return None
    return 1.0 if str(a) == str(b) else 0.0


def v15_handicap_similarity(sig_a, sig_b):
    if not sig_a or not sig_b:
        return None
    keys = sorted(set(sig_a) | set(sig_b))
    va = np.array([sig_a.get(k, 0) for k in keys], dtype=float)
    vb = np.array([sig_b.get(k, 0) for k in keys], dtype=float)
    denom = max(1.0, va.sum(), vb.sum())
    return max(0.0, 1.0 - np.abs(va - vb).sum() / denom)


def v15_find_similar_races(meta, entries, top_n=10, db_path=DB_PATH):
    v15_init_tables(db_path)
    weights = v15_similarity_weights(db_path)
    current_key = v15_race_key(meta)
    current_sig = v15_handicap_signature(entries)

    with sqlite3.connect(db_path) as con:
        races = pd.read_sql_query("SELECT * FROM v15_race_inputs", con)
        all_entries = pd.read_sql_query(
            "SELECT race_key, handicap FROM v15_race_entry_inputs", con
        )

    if races.empty:
        return pd.DataFrame()

    sig_map = {}
    if not all_entries.empty:
        for key, g in all_entries.groupby("race_key"):
            vals = pd.to_numeric(g["handicap"], errors="coerce").dropna().astype(int)
            sig_map[key] = vals.value_counts().sort_index().to_dict()

    rows = []
    for _, r in races.iterrows():
        if r["race_key"] == current_key:
            continue

        feature_scores = {
            "走路状態": v15_categorical_similarity(meta.get("走路状態"), r["surface"]),
            "走路温度": v15_numeric_similarity(meta.get("走路温度"), r["track_temp"], 25),
            "ハンデ構成": v15_handicap_similarity(current_sig, sig_map.get(r["race_key"], {})),
            "開催場": v15_categorical_similarity(meta.get("開催場"), r["venue"]),
            "距離": v15_numeric_similarity(meta.get("距離"), r["distance"], 1000),
            "湿度": v15_numeric_similarity(meta.get("湿度"), r["humidity"], 50),
            "気温": v15_numeric_similarity(meta.get("気温"), r["air_temp"], 20),
            "レース種別": v15_categorical_similarity(meta.get("レース種別"), r["race_type"]),
            "発走時間": None,
        }

        if meta.get("発走時刻") and r["start_time"]:
            try:
                h1, m1 = map(int, meta["発走時刻"].split(":"))
                h2, m2 = map(int, str(r["start_time"]).split(":"))
                feature_scores["発走時間"] = max(
                    0.0, 1.0 - abs((h1 * 60 + m1) - (h2 * 60 + m2)) / 360
                )
            except Exception:
                pass

        usable = {
            k: v for k, v in feature_scores.items()
            if v is not None and k in weights
        }
        total_w = sum(weights[k] for k in usable)
        if total_w <= 0:
            continue

        score = sum(weights[k] * usable[k] for k in usable) / total_w
        strongest = sorted(
            usable.items(),
            key=lambda kv: weights[kv[0]] * kv[1],
            reverse=True
        )[:3]

        rows.append({
            "開催日": r["race_date"],
            "開催場": r["venue"],
            "レース": r["race_no"],
            "レース種別": r["race_type"],
            "距離": r["distance"],
            "類似度": round(score * 100, 1),
            "主な一致条件": "・".join(k for k, _ in strongest),
        })

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values("類似度", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )


# ------------------------------
# 予測セルへの引き渡し
# ------------------------------
def v15_apply_to_prediction(meta, entries):
    global V15_RACE_META, V15_ENTRY_DF
    V15_RACE_META = dict(meta)
    V15_ENTRY_DF = entries.copy()

    # 既存の予測ロジックが参照しやすい別名も用意
    globals()["CURRENT_RACE_META"] = V15_RACE_META
    globals()["CURRENT_ENTRY_DF"] = V15_ENTRY_DF

    # 既存LATEST_TRACK_TEMPへ反映
    if pd.notna(meta.get("走路温度")):
        globals()["LATEST_TRACK_TEMP"] = float(meta["走路温度"])

    return V15_RACE_META, V15_ENTRY_DF


# ------------------------------
# 結果全文をVer13.2へ渡す
# ------------------------------
def v15_parse_result_preview(text):
    if "parse_full_official_result" in globals():
        return parse_full_official_result(text)
    return {
        "error": "Ver13.2のparse_full_official_result関数が見つかりません。",
        "raw_text": text
    }


def v15_save_result_using_existing(text):
    if "parse_full_official_result" not in globals():
        raise RuntimeError("Ver13.2の結果解析関数が読み込まれていません。上から順にセルを実行してください。")
    parsed = parse_full_official_result(text)

    if "save_full_official_result" not in globals():
        raise RuntimeError("Ver13.2の結果保存関数が読み込まれていません。")

    # 既存関数の引数差に耐える
    try:
        return save_full_official_result(parsed)
    except TypeError:
        return save_full_official_result(parsed, DB_PATH)


# ------------------------------
# UI
# ------------------------------
v15_init_tables()

style = {"description_width": "110px"}


# ============================================================
# Ver15.1 縦型選手履歴コピペ対応
# 「前走」「前々走」「3走前」...ごとに改行された形式を解析
# ============================================================

def v151_parse_date(line):
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", line)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def v151_is_history_header(line):
    line = line.strip()
    return bool(re.fullmatch(r"(?:前走|前々走|\d+走前)", line))


def v151_split_history_blocks(text):
    lines = [x.strip() for x in v15_clean_text(text).splitlines() if x.strip()]
    blocks = []
    current = None

    for line in lines:
        if v151_is_history_header(line):
            if current:
                blocks.append(current)
            current = {"見出し": line, "lines": []}
        elif current is not None:
            current["lines"].append(line)

    if current:
        blocks.append(current)

    return blocks


def v151_parse_history_block(block, player_name=""):
    lines = block.get("lines", [])
    if not lines:
        return None

    row = {
        "選手名": v15_normalize_name(player_name),
        "開催日": None,
        "開催場": None,
        "レース": None,
        "着順": None,
        "出走": None,
        "走路": None,
        "ハンデ": None,
        "試走T": np.nan,
        "競走T": np.nan,
        "ST": np.nan,
        "天候": None,
        "走路温度": np.nan,
        "気温": np.nan,
        "湿度": np.nan,
        "レース種別": None,
        "距離": None,
        "周回数": None,
        "人気": None,
        "車番": None,
        "_raw": "\n".join([block.get("見出し", "")] + lines),
    }

    # 先頭の単独数字は着順として扱う
    for i, line in enumerate(lines):
        if re.fullmatch(r"[1-8]", line):
            row["着順"] = int(line)
            break

    for line in lines:
        # 開催日
        if row["開催日"] is None:
            d = v151_parse_date(line)
            if d:
                row["開催日"] = d
                continue

        # 開催場
        if line in ["川口", "伊勢崎", "浜松", "山陽", "飯塚"]:
            row["開催場"] = line
            continue

        # レース種別
        if any(x in line for x in [
            "一般戦", "予選", "準決勝", "準決勝戦", "優勝戦",
            "選抜戦", "特別選抜戦", "最終予選", "二次予選", "一次予選"
        ]):
            row["レース種別"] = line
            continue

        # 天候
        if line in ["晴", "曇", "雨", "雪", "小雨"]:
            row["天候"] = line
            continue

        # 走路
        if line in ["良", "湿", "斑", "良走路", "湿走路", "斑走路"]:
            row["走路"] = line
            continue

        # 走路温度
        m = re.fullmatch(r"走\s*(-?\d+(?:\.\d+)?)", line)
        if m:
            row["走路温度"] = float(m.group(1))
            continue

        # 気温
        m = re.fullmatch(r"気\s*(-?\d+(?:\.\d+)?)", line)
        if m:
            row["気温"] = float(m.group(1))
            continue

        # 湿度
        m = re.fullmatch(r"湿\s*(\d+(?:\.\d+)?)", line)
        if m:
            row["湿度"] = float(m.group(1))
            continue

        # 車番・ハンデ
        # 例: 1番-m / 3番10m / 8番20m
        m = re.fullmatch(r"([1-8])番\s*([+-]?\d+|-)?m?", line)
        if m:
            row["車番"] = int(m.group(1))
            if m.group(2) in (None, "-"):
                row["ハンデ"] = 0
            else:
                row["ハンデ"] = int(m.group(2))
            continue

        # 距離・周回
        m = re.fullmatch(r"(\d{4})m\((\d+)周\)", line)
        if m:
            row["距離"] = int(m.group(1))
            row["周回数"] = int(m.group(2))
            continue

        # 人気
        m = re.fullmatch(r"(\d+)人気", line)
        if m:
            row["人気"] = int(m.group(1))
            continue

        # 競走タイム
        m = re.fullmatch(r"([3-9]\.\d{3})", line)
        if m:
            row["競走T"] = float(m.group(1))
            continue

        # 試走
        m = re.fullmatch(r"試\s*([3-9]\.\d{2,3})", line)
        if m:
            row["試走T"] = float(m.group(1))
            continue

        # ST
        m = re.fullmatch(r"ST\s*([+-]?\d?\.\d{2,3})", line, re.I)
        if m:
            row["ST"] = float(m.group(1))
            continue

    # 出走数は貼り付け情報にないため、通常8車として補完しない
    # 必要なら結果ページ登録時に上書き
    return row


def v151_parse_vertical_player_history(text, player_name=None):
    blocks = v151_split_history_blocks(text)
    if not blocks:
        return pd.DataFrame()

    rows = []
    for block in blocks:
        row = v151_parse_history_block(block, player_name=player_name or "")
        if row:
            rows.append(row)

    return pd.DataFrame(rows)


# 既存関数を上書きし、縦型と表形式の両方に対応
def v15_parse_player_history(text, player_name=None):
    vertical = v151_parse_vertical_player_history(text, player_name=player_name)

    # 縦型が2件以上取れたらこちらを採用
    if not vertical.empty and vertical["開催日"].notna().sum() >= 1:
        expected = [
            "選手名", "開催日", "開催場", "レース", "着順", "出走", "走路",
            "ハンデ", "試走T", "競走T", "ST",
            "天候", "走路温度", "気温", "湿度",
            "レース種別", "距離", "周回数", "人気", "車番", "_raw"
        ]
        for col in expected:
            if col not in vertical.columns:
                vertical[col] = np.nan
        return vertical[expected]

    # 従来の1行1レース形式へフォールバック
    rows = []
    for line in v15_clean_text(text).splitlines():
        row = v15_guess_history_row(line)
        if row:
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["選手名"] + V15_HISTORY_COLUMNS)

    if player_name:
        df.insert(0, "選手名", v15_normalize_name(player_name))
    else:
        guessed = v15_first_match([
            r"選手名\s*[:：]\s*([^\n]+)",
            r"プロフィール\s+([^\n]+)",
        ], v15_clean_text(text))
        df.insert(0, "選手名", v15_normalize_name(guessed) if guessed else "")

    return df[["選手名"] + V15_HISTORY_COLUMNS + ["_raw"]]


# DBに追加列を安全に追加
def v151_ensure_player_import_columns(db_path=DB_PATH):
    v15_init_tables(db_path)
    extra_columns = {
        "weather": "TEXT",
        "track_temp": "REAL",
        "air_temp": "REAL",
        "humidity": "REAL",
        "race_type": "TEXT",
        "distance": "INTEGER",
        "laps": "INTEGER",
        "popularity": "INTEGER",
        "car_no": "INTEGER",
    }

    with sqlite3.connect(db_path) as con:
        cols = set(v15_columns(con, "v15_player_history_imports"))
        for name, sql_type in extra_columns.items():
            if name not in cols:
                con.execute(
                    f"ALTER TABLE v15_player_history_imports ADD COLUMN {name} {sql_type}"
                )
        con.commit()


def v15_save_player_history(df, db_path=DB_PATH):
    if df is None or df.empty:
        return 0, 0

    v151_ensure_player_import_columns(db_path)
    inserted = 0
    skipped = 0
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as con:
        for _, row in df.iterrows():
            key = v15_hash(
                row.get("選手名"), row.get("開催日"), row.get("開催場"),
                row.get("レース"), row.get("着順"), row.get("試走T"),
                row.get("競走T"), row.get("車番")
            )

            exists = con.execute(
                "SELECT 1 FROM v15_player_history_imports WHERE history_key=?",
                (key,)
            ).fetchone()
            if exists:
                skipped += 1
                continue

            con.execute("""
                INSERT INTO v15_player_history_imports (
                    history_key, player_name, race_date, venue, race_no,
                    rank, starters, surface, handicap, trial_time,
                    race_time, st, raw_line, created_at,
                    weather, track_temp, air_temp, humidity,
                    race_type, distance, laps, popularity, car_no
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                key,
                row.get("選手名"),
                row.get("開催日"),
                row.get("開催場"),
                None if pd.isna(row.get("レース")) else int(row.get("レース")),
                None if pd.isna(row.get("着順")) else int(row.get("着順")),
                None if pd.isna(row.get("出走")) else int(row.get("出走")),
                row.get("走路"),
                None if pd.isna(row.get("ハンデ")) else int(row.get("ハンデ")),
                None if pd.isna(row.get("試走T")) else float(row.get("試走T")),
                None if pd.isna(row.get("競走T")) else float(row.get("競走T")),
                None if pd.isna(row.get("ST")) else float(row.get("ST")),
                row.get("_raw"),
                now,
                row.get("天候"),
                None if pd.isna(row.get("走路温度")) else float(row.get("走路温度")),
                None if pd.isna(row.get("気温")) else float(row.get("気温")),
                None if pd.isna(row.get("湿度")) else float(row.get("湿度")),
                row.get("レース種別"),
                None if pd.isna(row.get("距離")) else int(row.get("距離")),
                None if pd.isna(row.get("周回数")) else int(row.get("周回数")),
                None if pd.isna(row.get("人気")) else int(row.get("人気")),
                None if pd.isna(row.get("車番")) else int(row.get("車番")),
            ))
            inserted += 1

        con.commit()

    return inserted, skipped




# ============================================================
# Ver15.2 縦型出走表コピペ対応
# 公式サイトの「車番→選手名→複数行データ」形式を解析
# ============================================================

def v152_split_entry_blocks(text):
    """公式出走表の縦型ブロックを安全に分割する。

    車級の「1」「2」や成績中の数字を車番と誤認しないよう、
    新しい選手ブロックは次のどちらかだけを開始点にする。
    1) 「1 選手名」のような車番＋氏名の行
    2) 車番単独行の直後が氏名、その次が「0m/ST0.15」形式
    """
    lines = [x.strip() for x in v15_clean_text(text).splitlines() if x.strip()]
    blocks = []
    current = None

    def is_handicap_st_line(value):
        return bool(re.search(r"(?:[+-]?\d+|-)\s*m\s*/\s*ST\s*[+-]?\d?\.\d{2,3}", value, re.I))

    def looks_like_player_name(value):
        value = value.strip()
        if not value or len(value) > 40:
            return False
        if re.match(r"^(着順|平均|最高|試|ST|V\d|\d+(?:\.\d+)?%?$)", value):
            return False
        return bool(re.search(r"[^0-9.%()\s]", value))

    i = 0
    while i < len(lines):
        line = lines[i]

        # 公式ページの標準形: 「1 ▲廿樂歩」
        m_inline = re.fullmatch(r"([1-8])[\t 　]+(.+)", line)
        inline_start = False
        if m_inline:
            candidate_name = m_inline.group(2).strip()
            # 次行がハンデ/STなら、確実に選手見出し
            inline_start = (
                looks_like_player_name(candidate_name)
                and i + 1 < len(lines)
                and is_handicap_st_line(lines[i + 1])
            )

        # 一部コピー形式: 車番、氏名、ハンデ/ST がそれぞれ別行
        m_single = re.fullmatch(r"([1-8])", line)
        single_start = bool(
            m_single
            and i + 2 < len(lines)
            and looks_like_player_name(lines[i + 1])
            and is_handicap_st_line(lines[i + 2])
        )

        if inline_start:
            if current:
                blocks.append(current)
            current = {
                "車番": int(m_inline.group(1)),
                "lines": [m_inline.group(2).strip()],
            }
        elif single_start:
            if current:
                blocks.append(current)
            current = {
                "車番": int(m_single.group(1)),
                "lines": [lines[i + 1]],
            }
            i += 1  # 氏名行は取り込み済み
        elif current is not None:
            current["lines"].append(line)

        i += 1

    if current:
        blocks.append(current)

    # 同じ車番が紛れた場合は、内容の長いブロックを優先
    unique = {}
    for block in blocks:
        no = block["車番"]
        if not block["lines"]:
            continue
        if no not in unique or len(block["lines"]) > len(unique[no]["lines"]):
            unique[no] = block
    return [unique[k] for k in sorted(unique)]


def v152_parse_entry_block(block):
    car_no = int(block["車番"])
    lines = [x.strip() for x in block.get("lines", []) if x.strip()]
    if not lines:
        return None

    # 先頭行を選手名として採用
    player_name = v15_normalize_name(lines[0])

    joined = "\n".join(lines)
    compact = " ".join(lines)

    handicap = None
    st = np.nan
    hm = re.search(r"([+-]?\d+|-)\s*m\s*/\s*ST\s*([+-]?\d?\.\d{2,3})", compact, re.I)
    if hm:
        handicap = 0 if hm.group(1) == "-" else int(hm.group(1))
        st = float(hm.group(2))
    else:
        hm2 = re.search(r"([+-]?\d+|-)\s*m", compact)
        if hm2:
            handicap = 0 if hm2.group(1) == "-" else int(hm2.group(1))
        stm = re.search(r"ST\s*([+-]?\d?\.\d{2,3})", compact, re.I)
        if stm:
            st = float(stm.group(1))

    # 当日試走
    trial = np.nan
    tm = re.search(r"試\s*([3-9]\.\d{2,3}|-)", compact)
    if tm and tm.group(1) != "-":
        trial = float(tm.group(1))

    # 試走偏差
    trial_dev = np.nan
    dm = re.search(r"(?:試\s*[3-9]\.\d{2,3}|試-)\s+(-|[0-9]\.\d{3})", compact)
    if dm and dm.group(1) != "-":
        trial_dev = float(dm.group(1))

    # ランク
    rank = v15_first_match([r"\b([SAB]-?\d+)\b"], compact)
    prev_rank = v15_first_match([r"\(前\s*([SAB]-?\d+)\)"], compact)

    # 審査ポイント
    review_point = np.nan
    rpm = re.search(r"\(前[SAB]-?\d+\)\s*([0-9]{1,3}\.\d{3})", compact)
    if rpm:
        review_point = float(rpm.group(1))
    else:
        # ランク直後の3桁小数
        rpm = re.search(r"\b[SAB]-?\d+\b\s*([0-9]{1,3}\.\d{3})", compact)
        if rpm:
            review_point = float(rpm.group(1))

    current_year_v = v15_int(v15_first_match([r"\bV(\d+)\b"], compact))
    final_count = v15_int(v15_first_match([r"(\d+)回"], compact))

    all_v = None
    all_vm = re.findall(r"\bV(\d+)\b", compact)
    if all_vm:
        try:
            all_v = int(all_vm[-1])
        except:
            pass

    avg_trial = v15_float(v15_first_match([r"平均試走T\s*([3-9]\.\d{2,3})"], compact))
    avg_race = v15_float(v15_first_match([r"平均競走T\s*([3-9]\.\d{3})"], compact))
    best_race = v15_float(v15_first_match([r"最高競走T\s*([3-9]\.\d{3})"], compact))

    recent_finish = v15_first_match([r"着順\s*(\d+-\d+-\d+-\d+)"], compact)
    two_rate = v15_float(v15_first_match([r"2連\s*([0-9.]+)%"], compact))
    three_rate = v15_float(v15_first_match([r"3連\s*([0-9.]+)%"], compact))

    # 車名は「3連xx%」の後、単独行として現れることが多い
    car_name = None
    for idx, line in enumerate(lines):
        if re.fullmatch(r"3連\s*[0-9.]+%", line) and idx + 1 < len(lines):
            candidate = lines[idx + 1]
            if not re.fullmatch(r"\d+", candidate):
                car_name = candidate
                break

    # 近90/180日の末尾パーセント群
    percent_values = [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)%", compact)]
    # 最初の2つは近10走の2連・3連なので、その後を着別成績用に使う
    tail = percent_values[2:] if len(percent_values) >= 2 else []
    metrics = {
        "2連対率": tail[0] if len(tail) > 0 else np.nan,
        "3連対率": tail[1] if len(tail) > 1 else np.nan,
        "良2連対率": tail[2] if len(tail) > 2 else np.nan,
        "良3連対率": tail[3] if len(tail) > 3 else np.nan,
        "湿2連対率": tail[4] if len(tail) > 4 else np.nan,
        "湿3連対率": tail[5] if len(tail) > 5 else np.nan,
    }

    row = {
        "車番": car_no,
        "選手名": player_name,
        "ハンデ": handicap,
        "試走T": trial,
        "ST": st,
        "年齢": None,
        "級別": rank.split("-")[0] if rank else None,
        "期別": None,
        "現ランク": rank,
        "前ランク": prev_rank,
        "審査P": review_point,
        "試走偏差": trial_dev,
        "今年V": current_year_v,
        "優出回数": final_count,
        "通算V": all_v,
        "平均試走T": avg_trial,
        "平均競走T": avg_race,
        "最高競走T": best_race,
        "近10走着順": recent_finish,
        "近10走2連": two_rate,
        "近10走3連": three_rate,
        "車名": car_name,
        **metrics,
        "_raw": joined,
    }
    return row


def v152_parse_vertical_entries(text):
    blocks = v152_split_entry_blocks(text)
    rows = []
    for block in blocks:
        row = v152_parse_entry_block(block)
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


# 既存関数を上書きして縦型優先・従来形式フォールバック
def v15_parse_entries(text):
    vertical = v152_parse_vertical_entries(text)

    if not vertical.empty and vertical["車番"].nunique() >= 2:
        return vertical.sort_values("車番").reset_index(drop=True)

    rows = []
    seen = set()
    for raw in v15_clean_text(text).splitlines():
        row = v15_parse_entry_line(raw)
        if not row:
            continue
        key = row["車番"]
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("車番").reset_index(drop=True)
    return df


# レース情報パーサーも今回の形式へ強化
_old_v15_parse_race_meta = v15_parse_race_meta

def v15_parse_race_meta(text):
    meta = _old_v15_parse_race_meta(text)
    compact = " ".join([x.strip() for x in v15_clean_text(text).splitlines() if x.strip()])

    # 天候が単独行で貼られる形式（晴・曇・雨など）
    wm = re.search(r"(?:^|\s)(晴|曇|雨|小雨|雪)(?:\s|$)", compact)
    if wm:
        meta["天候"] = wm.group(1)

    # 6R
    m = re.search(r"(?:^|\s)(\d{1,2})R(?:\s|$)", compact, re.I)
    if m:
        meta["レース"] = int(m.group(1))

    # 13:13発走
    m = re.search(r"(\d{1,2}:\d{2})\s*発走", compact)
    if m:
        meta["発走時刻"] = m.group(1)

    # 良走路 /60℃
    m = re.search(r"(良走路|湿走路|斑走路|良|湿|斑)\s*/\s*(-?\d+(?:\.\d+)?)℃", compact)
    if m:
        meta["走路状態"] = m.group(1)
        meta["走路温度"] = float(m.group(2))

    # 3100m 8車 6周
    m = re.search(r"(\d{4})m\s+(\d+)車\s+(\d+)周", compact)
    if m:
        meta["距離"] = int(m.group(1))
        meta["出走数"] = int(m.group(2))
        meta["周回数"] = int(m.group(3))

    # 初日・最終日等
    m = re.search(r"(初日|\d+日目|最終日)", compact)
    if m:
        meta["開催日程"] = m.group(1)

    # 締切
    m = re.search(r"(\d{1,2}:\d{2})\s*締切", compact)
    if m:
        meta["締切時刻"] = m.group(1)

    return meta





# ============================================================
# AutoRaceAI Ver16.0
# 出走表コピペ + SQLite履歴 → Ver15.2本体へ直接接続
# ============================================================

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils.datetime import to_excel
from datetime import datetime
import traceback

VER16_LATEST_DF = None
VER16_LATEST_BETS = None
VER16_LATEST_OUTPUT = None

def ver16_safe_float(v, default=None):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)
    except Exception:
        return default

def ver16_surface(v):
    s = str(v or "")
    if "湿" in s:
        return "湿"
    if "斑" in s:
        return "斑"
    if "良" in s:
        return "良"
    return s or "良"

def ver16_get_history(name):
    # Ver13 DB管理関数を優先
    try:
        df = get_player_history(v15_normalize_name(name), model_only=True)
        if not df.empty:
            return df
    except Exception:
        pass

    # 名前空白差のフォールバック
    try:
        target = re.sub(r"[\s　]+", "", str(name))
        with sqlite3.connect(str(DB_PATH)) as con:
            return pd.read_sql_query("""
                SELECT
                    h.race_date AS 開催日,
                    h.venue AS 開催場,
                    h.race_no AS レース,
                    h.finish AS 着順,
                    h.starters AS 出走,
                    h.surface AS 走路,
                    h.handicap AS ハンデ,
                    h.trial_time AS 試走T,
                    h.race_time AS 競走T,
                    h.start_time AS ST,
                    h.result_status AS 結果区分
                FROM race_history h
                JOIN players p ON p.player_id=h.player_id
                WHERE REPLACE(REPLACE(p.player_name,' ',''),'　','')=?
                  AND COALESCE(h.use_for_model,1)=1
                ORDER BY h.race_date DESC, h.history_id DESC
            """, con, params=(target,))
    except Exception:
        return pd.DataFrame()

def ver16_make_settings_sheet(ws):
    ws["A1"] = "設定項目"
    ws["B1"] = "値"
    defaults = {
        4:30, 5:1.35, 6:0.85, 7:1.5, 8:0.7, 9:1.4, 10:1.1, 11:0.75,
        12:0.04, 22:0.018, 29:24, 31:8, 32:8, 33:5, 34:5, 35:6,
        36:8, 37:6, 38:4, 39:2, 40:1, 43:6, 44:20, 45:0.6,
        49:6, 50:6, 51:4, 52:6
    }
    labels = {
        4:"最近重視日数",5:"同一開催場倍率",6:"別開催場倍率",
        7:"同一走路倍率",8:"別走路倍率",9:"同一ハンデ倍率",
        10:"近接ハンデ倍率",11:"遠隔ハンデ倍率",12:"ST補正係数",
        22:"10m換算秒",29:"試走点",31:"ST点",32:"ハンデ適性点",
        33:"開催場適性点",34:"走路適性点",35:"位置取り点",
        36:"着順指数点",37:"勝率点",38:"連対率点",39:"上昇度点",
        40:"再現性点",43:"ハンデ改善点",44:"満点改善幅",
        45:"前走比率",49:"3着以内率点",50:"平均着順点",
        51:"着順安定度点",52:"審査Pランク点"
    }
    for r, value in defaults.items():
        ws.cell(r, 1, labels.get(r, f"設定{r}"))
        ws.cell(r, 2, value)

def ver16_build_virtual_excel(text):
    meta = v15_parse_race_meta(text)
    entries = v15_parse_entries(text)

    if entries.empty:
        raise ValueError("出走表を解析できませんでした。公式ページを全文コピーして貼り付けてください。")
    if entries["車番"].nunique() < 2:
        raise ValueError("解析できた選手が少なすぎます。車番から選手情報まで含めて貼り付けてください。")

    wb = Workbook()
    wb.remove(wb.active)

    # レース予測
    race_ws = wb.create_sheet("レース予測")
    date_text = meta.get("開催日") or datetime.now().strftime("%Y-%m-%d")
    venue = meta.get("開催場") or ""
    surface = ver16_surface(meta.get("走路状態") or meta.get("走路状況") or "良")
    race_ws.append(["レース開催日", date_text])
    race_ws.append(["今回の開催場", venue])
    race_ws.append(["今回の走路", surface])
    race_ws.append(["レース", meta.get("レース") or meta.get("レース番号") or ""])
    race_ws.append(["距離", meta.get("距離") or 3100])
    race_ws.append(["周回数", meta.get("周回数") or 6])

    # 設定
    set_ws = wb.create_sheet("設定")
    ver16_make_settings_sheet(set_ws)

    history_headers = ["開催日","開催場","レース","着順","出走","走路","ハンデ","試走T","競走T","ST"]

    entry_map = {int(r["車番"]): r for _, r in entries.iterrows()}

    # 本体は選手1～8を要求するため、8枚必ず生成
    for car in range(1, 9):
        ws = wb.create_sheet(f"選手{car}")
        row = entry_map.get(car)

        if row is None:
            ws["B2"] = f"未登録{car}"
            ws["E2"] = 3.99
            ws["G2"] = 0
            ws["E3"] = surface
            ws["I2"] = 0
            ws["I3"] = "B-999"
            hist = pd.DataFrame()
        else:
            name = str(row.get("選手名", "")).strip()
            ws["B2"] = name
            ws["E2"] = ver16_safe_float(row.get("試走T"), ver16_safe_float(row.get("平均試走T"), 3.50))
            ws["G2"] = ver16_safe_float(row.get("ハンデ"), 0)
            ws["E3"] = surface
            ws["I2"] = ver16_safe_float(row.get("審査P"), 0)
            ws["I3"] = str(row.get("現ランク") or row.get("級別") or "B-999")
            hist = ver16_get_history(name)

        header_row = 6
        for c, h in enumerate(history_headers, 1):
            ws.cell(header_row, c, h)

        if not hist.empty:
            hist = hist.copy()
            for i, (_, hrow) in enumerate(hist.head(100).iterrows(), header_row + 1):
                for c, h in enumerate(history_headers, 1):
                    value = hrow.get(h)
                    if h == "開催日" and value not in (None, ""):
                        dt = pd.to_datetime(value, errors="coerce")
                        if not pd.isna(dt):
                            value = dt.to_pydatetime()
                    if pd.isna(value) if not isinstance(value, str) else False:
                        value = None
                    ws.cell(i, c, value)

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue(), meta, entries

def ver16_run_prediction(text, trials=10000, seed=20260719):
    content, meta, entries = ver16_build_virtual_excel(text)
    track_temp = ver16_safe_float(meta.get("走路温度"), 30.0)
    filename = f"AutoRaceAI_Ver16_{meta.get('開催場') or 'race'}_{meta.get('レース') or ''}R.xlsx"
    df, bets, output = run_model(
        content,
        filename,
        min(int(trials), 20000),
        int(seed),
        float(track_temp)
    )
    return df, bets, output, entries, meta



# ============================================================
# v2.7: Ver13系DB互換登録・展開確率表示
# ============================================================
def v27_scenario_probabilities(track_temp=30.0):
    """simulate_detailed と同じ4展開の事前確率を返す。"""
    track_temp = float(track_temp if track_temp is not None else 30.0)
    if track_temp < 44.0:
        heat_index = 0.0
    elif track_temp < 48.0:
        heat_index = 0.05 + (track_temp - 44.0) / 4.0 * 0.20
    elif track_temp < 50.0:
        heat_index = 0.25 + (track_temp - 48.0) / 2.0 * 0.25
    else:
        heat_index = 0.50 + np.clip((track_temp - 50.0) / 10.0, 0.0, 1.0) * 0.50
    names = np.array(["先行縦長", "前残り", "混戦", "追い込み"], dtype=object)
    prob = np.array([0.36, 0.12, 0.28, 0.24], dtype=float)
    prob += heat_index * np.array([-0.055, 0.185, -0.040, -0.090])
    prob = np.clip(prob, 0.03, None)
    prob /= prob.sum()
    return {str(n): float(p) for n, p in zip(names, prob)}


def _v27_norm_player_name(name):
    """表示用氏名。全角/半角をそろえ、氏名内の空白は1個に整える。"""
    import unicodedata
    value = unicodedata.normalize("NFKC", str(name or "")).strip()
    return re.sub(r"\s+", " ", value)


def v32_player_name_key(name):
    """同一人物判定用キー。全角/半角・空白有無を無視する。"""
    import unicodedata
    value = unicodedata.normalize("NFKC", str(name or "")).strip()
    return re.sub(r"\s+", "", value).casefold()


def _v32_find_player(con, name):
    key = v32_player_name_key(name)
    if not key:
        return None
    for player_id, player_name in con.execute("SELECT player_id, player_name FROM players ORDER BY player_id"):
        if v32_player_name_key(player_name) == key:
            return int(player_id), str(player_name)
    return None


def _v33_norm_text(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", "", str(value)).strip().lower()


def _v33_norm_number(value, digits=4):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().lower().replace("m", "")
    if text in {"", "none", "nan", "-"}:
        return None
    try:
        return round(float(text), digits)
    except (TypeError, ValueError):
        return _v33_norm_text(value)


def _v33_history_signature(row):
    """同一走行結果の判定キー。

    レース名称と走路表記は登録元により「一般戦/7R」「湿/斑」のように揺れるため
    判定から除外する。日付・場・着順・ハンデ・各タイムが一致した走行を同一とする。
    """
    return (
        _v33_norm_text(row.get("race_date")),
        _v33_norm_text(row.get("venue")),
        _v33_norm_number(row.get("finish"), 0),
        _v33_norm_number(row.get("handicap"), 0),
        _v33_norm_number(row.get("trial_time"), 3),
        _v33_norm_number(row.get("race_time"), 3),
        _v33_norm_number(row.get("start_time"), 3),
    )


def _v32_history_signature(row):
    """後方互換名。v3.3からレース名称に依存しない。"""
    return _v33_history_signature(row)


def v32_merge_duplicate_players(db_path=DB_PATH):
    """空白表記だけ異なる選手を統合し、重複履歴も整理する。"""
    mount_and_init_db()
    merged_players = 0
    moved_histories = 0
    deleted_histories = 0
    normalized_imports = 0

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        players = con.execute(
            "SELECT player_id, player_name, created_at FROM players ORDER BY player_id"
        ).fetchall()
        groups = {}
        for p in players:
            key = v32_player_name_key(p["player_name"])
            if key:
                groups.setdefault(key, []).append(p)

        for key, members in groups.items():
            if len(members) < 2:
                continue
            canonical = members[0]
            canonical_id = int(canonical["player_id"])
            canonical_name = _v27_norm_player_name(canonical["player_name"])

            existing_rows = con.execute(
                "SELECT * FROM race_history WHERE player_id=? ORDER BY history_id",
                (canonical_id,),
            ).fetchall()
            existing_signatures = {_v32_history_signature(dict(r)) for r in existing_rows}

            for duplicate in members[1:]:
                duplicate_id = int(duplicate["player_id"])
                rows = con.execute(
                    "SELECT * FROM race_history WHERE player_id=? ORDER BY history_id",
                    (duplicate_id,),
                ).fetchall()
                for row in rows:
                    row_dict = dict(row)
                    signature = _v32_history_signature(row_dict)
                    if signature in existing_signatures:
                        con.execute("DELETE FROM race_history WHERE history_id=?", (row["history_id"],))
                        deleted_histories += 1
                        continue
                    new_key = _v27_record_key(key, *signature)
                    suffix = 0
                    candidate = new_key
                    while con.execute("SELECT 1 FROM race_history WHERE record_key=?", (candidate,)).fetchone():
                        suffix += 1
                        candidate = _v27_record_key(new_key, suffix)
                    con.execute(
                        "UPDATE race_history SET player_id=?, record_key=? WHERE history_id=?",
                        (canonical_id, candidate, row["history_id"]),
                    )
                    existing_signatures.add(signature)
                    moved_histories += 1
                con.execute("DELETE FROM players WHERE player_id=?", (duplicate_id,))
                merged_players += 1

        if v15_table_exists(con, "v15_player_history_imports"):
            rows = con.execute("SELECT * FROM v15_player_history_imports ORDER BY created_at, history_key").fetchall()
            seen = set()
            for row in rows:
                d = dict(row)
                key = v32_player_name_key(d.get("player_name"))
                if not key:
                    continue
                found = _v32_find_player(con, d.get("player_name"))
                canonical_name = found[1] if found else _v27_norm_player_name(d.get("player_name"))
                logical = (
                    key, d.get("race_date"), d.get("venue"), d.get("race_no"), d.get("rank"),
                    d.get("surface"), d.get("handicap"), d.get("trial_time"), d.get("race_time"),
                    d.get("st"), d.get("car_no")
                )
                if logical in seen:
                    con.execute("DELETE FROM v15_player_history_imports WHERE history_key=?", (d["history_key"],))
                    continue
                seen.add(logical)
                if d.get("player_name") != canonical_name:
                    con.execute(
                        "UPDATE v15_player_history_imports SET player_name=? WHERE history_key=?",
                        (canonical_name, d["history_key"]),
                    )
                    normalized_imports += 1
        con.commit()

    return {
        "merged_players": merged_players,
        "moved_histories": moved_histories,
        "deleted_histories": deleted_histories,
        "normalized_imports": normalized_imports,
    }


def v33_cleanup_duplicate_histories(db_path=DB_PATH):
    """同一選手内の重複走行を、レース名称に依存せず一括整理する。"""
    mount_and_init_db()
    deleted_histories = 0
    deleted_imports = 0

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")

        player_ids = [r[0] for r in con.execute("SELECT player_id FROM players ORDER BY player_id")]
        for player_id in player_ids:
            rows = con.execute(
                "SELECT * FROM race_history WHERE player_id=? ORDER BY created_at, history_id",
                (player_id,),
            ).fetchall()
            seen = {}
            for row in rows:
                sig = _v33_history_signature(dict(row))
                if sig not in seen:
                    seen[sig] = row
                    continue
                keep = seen[sig]
                # より具体的なレース番号（例: 7R）を表示名として優先する。
                keep_name = str(keep["race_no"] or "")
                current_name = str(row["race_no"] or "")
                if re.fullmatch(r"\d+R", current_name, flags=re.I) and not re.fullmatch(r"\d+R", keep_name, flags=re.I):
                    con.execute("UPDATE race_history SET race_no=? WHERE history_id=?", (current_name, keep["history_id"]))
                con.execute("DELETE FROM race_history WHERE history_id=?", (row["history_id"],))
                deleted_histories += 1

        if v15_table_exists(con, "v15_player_history_imports"):
            rows = con.execute(
                "SELECT * FROM v15_player_history_imports ORDER BY created_at, history_key"
            ).fetchall()
            seen = set()
            for row in rows:
                d = dict(row)
                sig = (
                    v32_player_name_key(d.get("player_name")),
                    _v33_norm_text(d.get("race_date")),
                    _v33_norm_text(d.get("venue")),
                    _v33_norm_number(d.get("rank"), 0),
                    _v33_norm_number(d.get("handicap"), 0),
                    _v33_norm_number(d.get("trial_time"), 3),
                    _v33_norm_number(d.get("race_time"), 3),
                    _v33_norm_number(d.get("st"), 3),
                )
                if sig in seen:
                    con.execute(
                        "DELETE FROM v15_player_history_imports WHERE history_key=?",
                        (d["history_key"],),
                    )
                    deleted_imports += 1
                else:
                    seen.add(sig)
        con.commit()

    return {"deleted_histories": deleted_histories, "deleted_imports": deleted_imports}


def _v27_record_key(*values):
    raw = "|".join("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v) for v in values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def v15_save_player_history(df, db_path=DB_PATH):
    """貼付履歴をVer13系 players/race_history に保存し、v15表にもミラーする。"""
    if df is None or df.empty:
        return 0, 0
    mount_and_init_db()
    v151_ensure_player_import_columns(db_path)
    inserted = 0
    skipped = 0
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        for _, row in df.iterrows():
            entered_name = _v27_norm_player_name(row.get("選手名"))
            if not entered_name:
                skipped += 1
                continue
            existing_player = _v32_find_player(con, entered_name)
            if existing_player:
                player_id, name = existing_player
            else:
                con.execute(
                    "INSERT INTO players(player_name, created_at, updated_at) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (entered_name,),
                )
                player_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
                name = entered_name

            race_date = row.get("開催日")
            venue = row.get("開催場")
            race_type = row.get("レース種別")
            finish = None if pd.isna(row.get("着順")) else float(row.get("着順"))
            starters = None if pd.isna(row.get("出走")) else float(row.get("出走"))
            surface = row.get("走路")
            handicap_num = None if pd.isna(row.get("ハンデ")) else int(row.get("ハンデ"))
            handicap_text = None if handicap_num is None else f"{handicap_num}m"
            trial = None if pd.isna(row.get("試走T")) else float(row.get("試走T"))
            race_time = None if pd.isna(row.get("競走T")) else float(row.get("競走T"))
            st = None if pd.isna(row.get("ST")) else float(row.get("ST"))
            car_no = None if pd.isna(row.get("車番")) else int(row.get("車番"))
            candidate_row = {
                "race_date": race_date, "venue": venue, "finish": finish,
                "handicap": handicap_text, "trial_time": trial,
                "race_time": race_time, "start_time": st,
            }
            signature = _v33_history_signature(candidate_row)
            record_key = _v27_record_key(v32_player_name_key(name), *signature)

            # record_keyだけでなく実データでも確認する。旧版で別名保存された履歴も止める。
            existing_rows = con.execute(
                "SELECT * FROM race_history WHERE player_id=? AND race_date=? AND venue=?",
                (player_id, race_date, venue),
            ).fetchall()
            exists = False
            col_names = [d[0] for d in con.execute("SELECT * FROM race_history LIMIT 0").description]
            for existing_row in existing_rows:
                if _v33_history_signature(dict(zip(col_names, existing_row))) == signature:
                    exists = True
                    break
            if exists:
                skipped += 1
            else:
                con.execute(
                    """INSERT INTO race_history(
                        player_id, race_date, venue, race_no, finish, starters, surface,
                        handicap, trial_time, race_time, start_time, result_status,
                        use_for_model, source, record_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '通常', 1, 'スマホ貼付登録', ?, CURRENT_TIMESTAMP)""",
                    (player_id, race_date, venue, race_type, finish, starters, surface,
                     handicap_text, trial, race_time, st, record_key),
                )
                inserted += 1

            # 詳細条件を保持するミラー表。予測本体は上のrace_historyを参照。
            history_key = v15_hash(v32_player_name_key(name), race_date, venue, finish, handicap_num, trial, race_time, st)
            con.execute("""
                INSERT OR IGNORE INTO v15_player_history_imports (
                    history_key, player_name, race_date, venue, race_no,
                    rank, starters, surface, handicap, trial_time,
                    race_time, st, raw_line, created_at,
                    weather, track_temp, air_temp, humidity,
                    race_type, distance, laps, popularity, car_no
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                history_key, name, race_date, venue, None,
                None if finish is None else int(finish), None if starters is None else int(starters), surface,
                handicap_num, trial, race_time, st, row.get("_raw"), now,
                row.get("天候"), None if pd.isna(row.get("走路温度")) else float(row.get("走路温度")),
                None if pd.isna(row.get("気温")) else float(row.get("気温")),
                None if pd.isna(row.get("湿度")) else float(row.get("湿度")),
                race_type, None if pd.isna(row.get("距離")) else int(row.get("距離")),
                None if pd.isna(row.get("周回数")) else int(row.get("周回数")),
                None if pd.isna(row.get("人気")) else int(row.get("人気")), car_no,
            ))
        con.commit()
    return inserted, skipped


# ============================================================
# v3.0: 6周詳細シミュレーション表示補助
# ============================================================
def v30_representative_lap_projection(df):
    """詳細シミュレーション用指標から、代表的な6周の隊列推移を作る。

    確率計算そのものは simulate_detailed() が担当する。この関数は画面表示用で、
    スタート隊列から最終予測順位へ、隣接追い抜きだけで移る代表経路を返す。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    work = df.copy().reset_index(drop=True)
    cars = pd.to_numeric(work.get("車"), errors="coerce").fillna(999).astype(int).to_numpy()
    names = work.get("選手名", pd.Series([""] * len(work))).astype(str).to_numpy()
    handicap = pd.to_numeric(work.get("ハンデ"), errors="coerce").fillna(0).to_numpy(float)
    st = pd.to_numeric(work.get("ST予測", work.get("平均ST", pd.Series(0.15, index=work.index))), errors="coerce").fillna(0.15).to_numpy(float)
    start_stretch = pd.to_numeric(work.get("スタート伸び指数", pd.Series(0.5, index=work.index)), errors="coerce").fillna(0.5).to_numpy(float)
    final_rank = pd.to_numeric(work.get("改善後順位", pd.Series(range(1, len(work)+1))), errors="coerce").fillna(len(work)).to_numpy(float)
    closing = pd.to_numeric(work.get("ゴール前伸び指数", work.get("終盤指数", pd.Series(0.5, index=work.index))), errors="coerce").fillna(0.5).to_numpy(float)
    breakthrough = pd.to_numeric(work.get("混戦突破適性", pd.Series(0.5, index=work.index)), errors="coerce").fillna(0.5).to_numpy(float)

    # ハンデ前方を基本に、STとスタート伸びで1周目隊列を作る。
    start_key = handicap * 0.045 + st * 1.8 - start_stretch * 0.18 + cars * 0.002
    order = list(np.argsort(start_key, kind="stable"))
    target = list(np.argsort(final_rank, kind="stable"))
    snapshots = [order.copy()]

    # 2～6周目。1周に一度、隣接車だけを入れ替えて徐々に最終隊列へ近づける。
    for lap in range(2, 7):
        desired_pos = {idx: pos for pos, idx in enumerate(target)}
        candidates = []
        for pos in range(1, len(order)):
            chaser, leader = order[pos], order[pos-1]
            if desired_pos[chaser] < desired_pos[leader]:
                late = max(0.0, (lap - 3) / 3.0)
                strength = (breakthrough[chaser] * (1-late) + closing[chaser] * late)
                resistance = 0.35 * breakthrough[leader] + 0.25 * closing[leader]
                urgency = desired_pos[leader] - desired_pos[chaser]
                candidates.append((strength - resistance + urgency * 0.08, pos))
        # 周回後半ほど最大2組、序盤は最大1組。互いに重ならない隣接交換のみ。
        candidates.sort(reverse=True)
        max_swaps = 1 if lap <= 3 else 2
        used = set(); swaps = 0
        for _, pos in candidates:
            if swaps >= max_swaps or pos in used or (pos-1) in used:
                continue
            order[pos-1], order[pos] = order[pos], order[pos-1]
            used.update({pos-1, pos}); swaps += 1
        snapshots.append(order.copy())

    rows=[]
    for lap, snap in enumerate(snapshots, 1):
        row={"周回": f"{lap}周目"}
        for pos, idx in enumerate(snap, 1):
            row[f"{pos}位"] = f"{cars[idx]} {names[idx]}".strip()
        rows.append(row)
    return pd.DataFrame(rows)


def v30_finish_probabilities(df, bet_counts, trials):
    """三連単カウントから各車の1～3着確率を％値で集計する。"""
    total=max(1, int(trials))
    counts={int(c): [0,0,0] for c in pd.to_numeric(df.get("車"), errors="coerce").dropna().astype(int)}
    for combo, n in bet_counts.get("三連単", {}).items():
        for pos, car in enumerate(combo[:3]):
            counts.setdefault(int(car), [0,0,0])[pos] += int(n)
    name_map={int(r["車"]): str(r.get("選手名", "")) for _,r in df.iterrows() if pd.notna(r.get("車"))}
    rows=[]
    for car, vals in counts.items():
        rows.append({"車":car,"選手名":name_map.get(car,""),"1着率":vals[0]/total*100,"2着率":vals[1]/total*100,"3着率":vals[2]/total*100,"3着内率":sum(vals)/total*100})
    return pd.DataFrame(rows).sort_values(["1着率","3着内率"],ascending=False).reset_index(drop=True)

# ============================================================
# v3.4 結果登録・予測比較解析
# ============================================================
def v34_race_key(meta):
    date = re.sub(r"[^0-9]", "", str(meta.get("開催日") or meta.get("race_date") or ""))[:8]
    venue = str(meta.get("開催場") or meta.get("venue") or "").strip()
    race = re.sub(r"[^0-9]", "", str(meta.get("レース") or meta.get("race_no") or ""))
    return f"{date}_{venue}_{race or '0'}R"


def v34_init_feedback_tables(db_path=DB_PATH):
    with sqlite3.connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS prediction_snapshots (
            race_key TEXT NOT NULL, car_no INTEGER NOT NULL, player_name TEXT,
            predicted_rank INTEGER, win_prob REAL, top3_prob REAL,
            created_at TEXT, PRIMARY KEY(race_key, car_no)
        );
        CREATE TABLE IF NOT EXISTS result_races (
            race_key TEXT PRIMARY KEY, race_date TEXT, venue TEXT, race_no TEXT,
            surface TEXT, track_temp REAL, air_temp REAL, humidity REAL,
            registered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS result_entries (
            race_key TEXT NOT NULL, car_no INTEGER NOT NULL, player_name TEXT,
            finish INTEGER, trial_time REAL, race_time REAL, start_time REAL,
            handicap TEXT, result_status TEXT,
            PRIMARY KEY(race_key, car_no)
        );
        CREATE TABLE IF NOT EXISTS prediction_feedback (
            race_key TEXT PRIMARY KEY, sample_size INTEGER, mean_rank_error REAL,
            winner_hit INTEGER, top3_hit_count INTEGER, analysis_json TEXT,
            analyzed_at TEXT
        );
        """)


def v34_save_prediction_snapshot(meta, df, finish_prob=None, db_path=DB_PATH):
    v34_init_feedback_tables(db_path)
    key = v34_race_key(meta)
    probs = {}
    if isinstance(finish_prob, pd.DataFrame) and not finish_prob.empty:
        for _, r in finish_prob.iterrows():
            car = int(r.get("車", r.get("車番", 0)) or 0)
            probs[car] = (float(r.get("1着率", 0) or 0), float(r.get("3着内率", 0) or 0))
    now = datetime.now().isoformat(timespec="seconds")
    rank_col = "改善後順位" if "改善後順位" in df.columns else ("順位" if "順位" in df.columns else None)
    car_col = "車" if "車" in df.columns else "車番"
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM prediction_snapshots WHERE race_key=?", (key,))
        for _, r in df.iterrows():
            car = int(r[car_col]); win, top3 = probs.get(car, (None, None))
            con.execute("""INSERT INTO prediction_snapshots
                (race_key,car_no,player_name,predicted_rank,win_prob,top3_prob,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (key, car, str(r.get("選手名", "")), int(r[rank_col]) if rank_col else None, win, top3, now))
        con.commit()
    return key


def v34_parse_result_text(text, venue_override="", race_no_override=""):
    clean = v15_clean_text(text)
    if not clean:
        raise ValueError("結果ページを貼り付けてください。")
    meta = v15_parse_race_meta(clean)
    if venue_override.strip(): meta["開催場"] = venue_override.strip()
    if str(race_no_override).strip(): meta["レース"] = v15_int(race_no_override)
    rows = []
    for raw in clean.splitlines():
        line = raw.replace("　", " ").strip()
        if not line: continue
        parts = [p.strip() for p in re.split(r"\t+| {2,}", line) if p.strip()]
        if len(parts) < 3: continue
        if not re.fullmatch(r"[1-8]", parts[0]) or not re.fullmatch(r"[1-8]", parts[1]): continue
        finish, car = int(parts[0]), int(parts[1])
        name = v15_normalize_name(parts[2])
        nums = []
        for p in parts[3:]:
            m = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", p.replace("m", ""))
            if m: nums.append(float(m.group()))
        trial = next((x for x in nums if 3.2 <= x <= 3.9), np.nan)
        race_t = next((x for x in nums if 3.3 <= x <= 4.2 and (pd.isna(trial) or x != trial)), np.nan)
        st = next((x for x in nums if 0 <= x < 1), np.nan)
        handicap = next((p for p in parts if re.fullmatch(r"-?\d+m?", p) and int(p.replace('m','')) % 10 == 0), "")
        rows.append({"着順":finish,"車番":car,"選手名":name,"ハンデ":handicap,
                     "試走T":trial,"競走T":race_t,"ST":st,"結果区分":"通常"})
    if len(rows) < 3:
        raise ValueError("着順表を解析できませんでした。公式結果ページの着・車・選手名・試走タイム・競走タイムを含めて貼り付けてください。")
    rows = sorted({r["車番"]:r for r in rows}.values(), key=lambda x:x["着順"])
    return meta, pd.DataFrame(rows)


def v34_save_result_and_analyze(meta, results, db_path=DB_PATH):
    v34_init_feedback_tables(db_path)
    key = v34_race_key(meta)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute("""INSERT INTO result_races VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(race_key) DO UPDATE SET surface=excluded.surface,track_temp=excluded.track_temp,
          air_temp=excluded.air_temp,humidity=excluded.humidity,registered_at=excluded.registered_at""",
          (key,meta.get("開催日"),meta.get("開催場"),str(meta.get("レース") or ""),meta.get("走路状態"),
           meta.get("走路温度"),meta.get("気温"),meta.get("湿度"),now))
        con.execute("DELETE FROM result_entries WHERE race_key=?", (key,))
        for _,r in results.iterrows():
            con.execute("""INSERT INTO result_entries VALUES(?,?,?,?,?,?,?,?,?)""",
              (key,int(r["車番"]),str(r["選手名"]),int(r["着順"]),r.get("試走T"),r.get("競走T"),r.get("ST"),str(r.get("ハンデ", "")),str(r.get("結果区分","通常"))))
        pred = pd.read_sql_query("SELECT * FROM prediction_snapshots WHERE race_key=?", con, params=(key,))
        con.commit()
    merged = results.merge(pred, left_on="車番", right_on="car_no", how="left") if not pred.empty else results.copy()
    if pred.empty:
        return key, merged, {"message":"同じ日付・開催場・レース番号の保存済み予測がないため、結果登録のみ完了しました。"}
    merged["順位誤差"] = merged["着順"] - merged["predicted_rank"]
    mae = float(merged["順位誤差"].abs().mean())
    pred_winner = int(pred.sort_values("predicted_rank").iloc[0]["car_no"])
    actual_winner = int(results.sort_values("着順").iloc[0]["車番"])
    predicted_top3 = set(pred.nsmallest(3,"predicted_rank")["car_no"].astype(int))
    actual_top3 = set(results.nsmallest(3,"着順")["車番"].astype(int))
    analysis = {"平均順位誤差":round(mae,2),"1着的中":pred_winner==actual_winner,
                "3着内一致数":len(predicted_top3 & actual_top3),"予測1着":pred_winner,"実際1着":actual_winner}
    with sqlite3.connect(db_path) as con:
        con.execute("""INSERT INTO prediction_feedback VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(race_key) DO UPDATE SET sample_size=excluded.sample_size,mean_rank_error=excluded.mean_rank_error,
          winner_hit=excluded.winner_hit,top3_hit_count=excluded.top3_hit_count,analysis_json=excluded.analysis_json,analyzed_at=excluded.analyzed_at""",
          (key,len(merged),mae,int(pred_winner==actual_winner),len(predicted_top3&actual_top3),json.dumps(analysis,ensure_ascii=False),now))
        con.commit()
    return key, merged, analysis

# ============================================================
# v3.5 公式結果全文（縦型）・グランドノート・払戻金対応
# ============================================================
def v35_init_result_tables(db_path=DB_PATH):
    v34_init_feedback_tables(db_path)
    with sqlite3.connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS result_laps (
            race_key TEXT NOT NULL,
            lap_label TEXT NOT NULL,
            lap_no INTEGER,
            position INTEGER NOT NULL,
            car_no INTEGER NOT NULL,
            PRIMARY KEY (race_key, lap_label, position)
        );
        CREATE TABLE IF NOT EXISTS result_payouts (
            race_key TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            combination TEXT NOT NULL,
            payout_yen INTEGER,
            popularity INTEGER,
            PRIMARY KEY (race_key, bet_type, combination)
        );
        """)
        con.commit()


def _v35_japanese_date(text):
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _v35_float(value):
    try:
        return float(str(value).strip())
    except Exception:
        return np.nan


def _v35_parse_meta(text, venue_override="", race_no_override=""):
    meta = v15_parse_race_meta(v15_clean_text(text))
    date = _v35_japanese_date(text)
    if date:
        meta["開催日"] = date

    race_match = re.search(r"(?m)^\s*(\d{1,2})R\s*$", text)
    if race_match:
        meta["レース"] = int(race_match.group(1))
    if str(race_no_override).strip():
        meta["レース"] = v15_int(race_no_override)

    venues = ["川口", "伊勢崎", "浜松", "山陽", "飯塚"]
    found_venue = next((v for v in venues if v in text), "")
    if venue_override.strip():
        found_venue = venue_override.strip()
    if found_venue:
        meta["開催場"] = found_venue

    surface_match = re.search(r"(良走路|湿走路|斑走路|荒走路)\s*/\s*(-?\d+(?:\.\d+)?)℃", text)
    if surface_match:
        meta["走路状態"] = surface_match.group(1)
        meta["走路温度"] = float(surface_match.group(2))
    else:
        sm = re.search(r"(良走路|湿走路|斑走路|荒走路)", text)
        if sm:
            meta["走路状態"] = sm.group(1)

    am = re.search(r"気温[：:]\s*(-?\d+(?:\.\d+)?)℃", text)
    hm = re.search(r"湿度[：:]\s*(\d+(?:\.\d+)?)%", text)
    if am:
        meta["気温"] = float(am.group(1))
    if hm:
        meta["湿度"] = float(hm.group(1))

    weather_match = re.search(r"(?m)^\s*(晴|曇|雨|小雨|雪)\s*$", text)
    if weather_match:
        meta["天候"] = weather_match.group(1)

    race_type_match = re.search(r"(?m)^\s*(予選|一般戦|準決勝戦|準決勝|優勝戦|選抜戦|特別一般戦)\s+(\d+)m\((\d+)周\)", text)
    if race_type_match:
        meta["レース種別"] = race_type_match.group(1)
        meta["距離"] = int(race_type_match.group(2))
        meta["周回数"] = int(race_type_match.group(3))
    return meta


def _v35_parse_entries(text):
    # 着順表だけを対象にする。払戻金や周回順位を誤認しないよう区間を限定。
    start = text.find("着順")
    if start < 0:
        start = 0
    ends = [p for p in [text.find("グランドノート", start), text.find("払戻金", start)] if p >= 0]
    end = min(ends) if ends else len(text)
    block = text[start:end]
    lines = [re.sub(r"[\t\u3000]+", " ", x).strip() for x in block.splitlines()]
    lines = [x for x in lines if x]

    rows = []
    i = 0
    while i < len(lines):
        m = re.match(r"^([1-8])\s+([1-8])(?:\s+(.+))?$", lines[i])
        if not m:
            i += 1
            continue
        finish, car = int(m.group(1)), int(m.group(2))
        inline_name = (m.group(3) or "").strip()
        i += 1
        name = inline_name
        if not name and i < len(lines):
            name = lines[i]
            i += 1
        # LG/ハンデ/試走T
        if i >= len(lines):
            break
        profile = lines[i]
        i += 1
        pm = re.search(r"([^/]+)/\s*(-?\d+)m\s*/\s*(\d\.\d{2})", profile)
        if not pm:
            continue
        lg = pm.group(1).strip()
        handicap = int(pm.group(2))
        trial = float(pm.group(3))

        # 競走T（人気）
        race_t = np.nan
        popularity = np.nan
        if i < len(lines):
            rm = re.search(r"(\d\.\d{3})\s*\((\d+)\)", lines[i])
            if rm:
                race_t = float(rm.group(1))
                popularity = int(rm.group(2))
                i += 1

        # ST/事故
        st_time = np.nan
        accident = ""
        if i < len(lines):
            sm = re.match(r"^(0\.\d{2})(?:\s+(.+))?$", lines[i])
            if sm:
                st_time = float(sm.group(1))
                accident = (sm.group(2) or "").strip()
                i += 1

        rows.append({
            "着順": finish,
            "車番": car,
            "選手名": v15_normalize_name(name),
            "所属": lg,
            "ハンデ": handicap,
            "試走T": trial,
            "競走T": race_t,
            "ST": st_time,
            "人気": popularity,
            "事故": accident,
            "結果区分": "通常" if not accident else accident,
        })
    if len(rows) < 3:
        raise ValueError("着順表を解析できませんでした。『着順 車番 選手名』からグランドノート直前までを含めて貼り付けてください。")
    return pd.DataFrame(sorted(rows, key=lambda r: r["着順"]))


def _v35_parse_laps(text):
    if "グランドノート" not in text:
        return pd.DataFrame(columns=["周回", "周回番号", "順位", "車番"])
    block = text.split("グランドノート", 1)[1]
    if "払戻金" in block:
        block = block.split("払戻金", 1)[0]
    records = []
    for raw in block.splitlines():
        line = re.sub(r"[\t\u3000]+", " ", raw).strip()
        m = re.match(r"^(ゴール線|([1-9])周目)\s+((?:[1-8]\s*){3,8})$", line)
        if not m:
            continue
        label = m.group(1)
        lap_no = 99 if label == "ゴール線" else int(m.group(2))
        cars = [int(x) for x in re.findall(r"[1-8]", m.group(3))]
        for pos, car in enumerate(cars, start=1):
            records.append({"周回": label, "周回番号": lap_no, "順位": pos, "車番": car})
    if not records:
        return pd.DataFrame(columns=["周回", "周回番号", "順位", "車番"])
    return pd.DataFrame(records).sort_values(["周回番号", "順位"]).reset_index(drop=True)


def _v35_parse_payouts(text):
    if "払戻金" not in text:
        return pd.DataFrame(columns=["券種", "組合せ", "払戻金", "人気"])
    block = text.split("払戻金", 1)[1]
    lines = [re.sub(r"[\t\u3000]+", " ", x).strip() for x in block.splitlines()]
    lines = [x for x in lines if x]
    known = {"単勝", "複勝", "2連複", "2連単", "ワイド", "3連複", "3連単"}
    current = ""
    rows = []
    pattern = re.compile(r"^(?:(単勝|複勝|2連複|2連単|ワイド|3連複|3連単)\s+)?(.+?)\s+([\d,]+)円\s+(\d+)人気$")
    for line in lines:
        m = pattern.match(line)
        if not m:
            continue
        if m.group(1):
            current = m.group(1)
        if current not in known:
            continue
        combo = re.sub(r"\s+", "", m.group(2))
        rows.append({"券種": current, "組合せ": combo, "払戻金": int(m.group(3).replace(",", "")), "人気": int(m.group(4))})
    return pd.DataFrame(rows)


def v35_parse_result_text(text, venue_override="", race_no_override=""):
    if not str(text).strip():
        raise ValueError("結果ページを貼り付けてください。")
    meta = _v35_parse_meta(text, venue_override, race_no_override)
    rows = _v35_parse_entries(text)
    laps = _v35_parse_laps(text)
    payouts = _v35_parse_payouts(text)
    if not meta.get("開催日") or not meta.get("開催場") or not meta.get("レース"):
        raise ValueError("開催日・開催場・レース番号のいずれかを取得できませんでした。必要な場合は画面の補助入力を使ってください。")
    return meta, rows, laps, payouts


def _v35_lap_analysis(results, laps):
    if laps is None or laps.empty:
        return {}
    pivot = laps.pivot(index="周回", columns="順位", values="車番")
    lap1 = laps[laps["周回"] == "1周目"].sort_values("順位")
    goal = laps[laps["周回"] == "ゴール線"].sort_values("順位")
    if goal.empty:
        goal = laps[laps["周回番号"] == laps["周回番号"].max()].sort_values("順位")
    leader_sequence = []
    ordered_labels = [x for x in laps.sort_values("周回番号")["周回"].drop_duplicates().tolist()]
    for label in ordered_labels:
        part = laps[(laps["周回"] == label) & (laps["順位"] == 1)]
        if not part.empty:
            leader_sequence.append(int(part.iloc[0]["車番"]))
    lead_changes = sum(a != b for a, b in zip(leader_sequence, leader_sequence[1:]))
    analysis = {"先頭交代回数": int(lead_changes)}
    if not lap1.empty:
        analysis["1周目先頭"] = int(lap1.iloc[0]["車番"])
    if not goal.empty:
        analysis["ゴール先頭"] = int(goal.iloc[0]["車番"])
    if not lap1.empty and not goal.empty:
        start_pos = {int(r["車番"]): int(r["順位"]) for _, r in lap1.iterrows()}
        goal_pos = {int(r["車番"]): int(r["順位"]) for _, r in goal.iterrows()}
        gains = {car: start_pos[car] - goal_pos.get(car, start_pos[car]) for car in start_pos}
        if gains:
            best_car = max(gains, key=gains.get)
            analysis["最大順位上昇車"] = int(best_car)
            analysis["最大順位上昇"] = int(gains[best_car])
    return analysis


def v35_save_result_and_analyze(meta, results, laps=None, payouts=None, db_path=DB_PATH):
    v35_init_result_tables(db_path)
    key, comparison, analysis = v34_save_result_and_analyze(meta, results, db_path)
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM result_laps WHERE race_key=?", (key,))
        if isinstance(laps, pd.DataFrame) and not laps.empty:
            for _, r in laps.iterrows():
                con.execute("INSERT INTO result_laps VALUES(?,?,?,?,?)",
                            (key, str(r["周回"]), int(r["周回番号"]), int(r["順位"]), int(r["車番"])))
        con.execute("DELETE FROM result_payouts WHERE race_key=?", (key,))
        if isinstance(payouts, pd.DataFrame) and not payouts.empty:
            for _, r in payouts.iterrows():
                con.execute("INSERT INTO result_payouts VALUES(?,?,?,?,?)",
                            (key, str(r["券種"]), str(r["組合せ"]), int(r["払戻金"]), int(r["人気"])))
        lap_analysis = _v35_lap_analysis(results, laps)
        if lap_analysis:
            analysis = dict(analysis)
            analysis.update(lap_analysis)
            if "message" not in analysis:
                con.execute("UPDATE prediction_feedback SET analysis_json=? WHERE race_key=?",
                            (json.dumps(analysis, ensure_ascii=False), key))
        con.commit()
    return key, comparison, analysis

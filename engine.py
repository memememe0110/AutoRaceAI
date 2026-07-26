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
        "10m換算秒": b(22, 0.012),

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
    """走路表記を 良・斑・湿・風 にそろえる。"""
    s = text(value)
    if "良" in s:
        return "良"
    if "斑" in s:
        return "斑"
    if "湿" in s:
        return "湿"
    if "風" in s:
        return "風"
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

        # 風走路は乾いた良走路に近いが、別条件として弱めに共有する
        ("風", "良"): 0.65,
        ("良", "風"): 0.65,
        ("斑", "風"): 0.18,
        ("風", "斑"): 0.22,
        ("湿", "風"): 0.03,
        ("風", "湿"): 0.03,
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


def outer_lane_penalty(df, max_penalty=3.0):
    """
    10m以上の同ハンデ帯で横並びが3人以上の場合、外側ほど減点する。

    Ver19では一律減点をやめ、格・審査P・追上げ実績・レース巧者度で緩和する。
    強い追い込み選手は最外でも軽い減点に留め、裏付けが弱い選手には
    外枠リスクを残す。

    - 0m線は対象外
    - 同ハンデ2人以下は対象外
    - 人数が多いほど補正を強くする
    - 緩和前の最外枠でも最大3.0点まで
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

            # 追い込み・捌きの裏付けがある選手は外枠不利を緩和する。
            # 各指標が欠損していても安全に計算できるよう中立値を使う。
            row = df.loc[idx]
            rank_text = str(row.get("現ランク", "")).strip().upper()
            grade_strength = 1.0 if rank_text.startswith("S") else (0.55 if rank_text.startswith("A") else 0.15)

            judge_p = pd.to_numeric(pd.Series([row.get("審査P", np.nan)]), errors="coerce").iloc[0]
            judge_strength = float(np.clip((judge_p - 55.0) / 45.0, 0.0, 1.0)) if not pd.isna(judge_p) else 0.45

            rear_chase = pd.to_numeric(pd.Series([row.get("後方追上げ指数", np.nan)]), errors="coerce").iloc[0]
            rear_strength = float(np.clip(rear_chase, 0.0, 1.0)) if not pd.isna(rear_chase) else 0.45

            craft_raw = pd.to_numeric(pd.Series([row.get("レース巧者指数", np.nan)]), errors="coerce").iloc[0]
            # レース巧者指数は概ね-0.25～+0.25なので0～1へ写像する。
            craft_strength = float(np.clip(0.5 + craft_raw * 2.0, 0.0, 1.0)) if not pd.isna(craft_raw) else 0.45

            win_raw = pd.to_numeric(pd.Series([row.get("勝ち切り指数", np.nan)]), errors="coerce").iloc[0]
            win_strength = float(np.clip(win_raw, 0.0, 1.0)) if not pd.isna(win_raw) else 0.45

            pursuit_strength = (
                grade_strength * 0.30
                + judge_strength * 0.25
                + rear_strength * 0.20
                + craft_strength * 0.15
                + win_strength * 0.10
            )

            # 最上位クラスでも外枠リスクをゼロにはしない。
            # 緩和率は最大72%、最低8%。
            relief = float(np.clip(0.08 + pursuit_strength * 0.64, 0.08, 0.72))
            penalty.loc[idx] = -max_penalty * crowd_factor * outer_exposure * (1.0 - relief)

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
        df, max_penalty=3.0
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
    outer_risk = np.clip(outer_penalty / 3.0 + zero_line_outer * 0.75, 0.0, 1.0)
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
    primary_front = handicap == front_handicap

    # Ver27: 「最前ハンデだけが逃げ役」という固定をやめる。
    # 最前線の裏付けが弱く、10m後ろに明確に実戦力の高い選手がいる場合は、
    # その選手を序盤で先頭へ立てる早期先頭候補として前線へ含める。
    ability_gate_pre = 1.0 / (1.0 + np.exp(-np.clip(z_scores, -3.0, 3.0)))
    early_lead_strength = np.clip(
        execution_quality * 0.27
        + traffic_conversion * 0.22
        + recent_form_strength * 0.17
        + st_stability * 0.12
        + quinella_rate * 0.12
        + top3_rate * 0.10,
        0.0, 1.0,
    )
    primary_best = float(np.max(early_lead_strength[primary_front])) if primary_front.any() else 0.50
    secondary_line = np.isclose(handicap, front_handicap + 10.0)
    secondary_gate = (
        secondary_line
        & (early_lead_strength >= max(0.48, primary_best + 0.055))
        & (traffic_conversion >= 0.43)
    )
    # 最前線がかなり弱いレースでは、差が小さくても10m線の上位1車だけを候補化。
    if primary_best < 0.40 and secondary_line.any() and not secondary_gate.any():
        sec_idx = np.where(secondary_line)[0]
        best_sec = int(sec_idx[np.argmax(early_lead_strength[sec_idx])])
        if early_lead_strength[best_sec] >= 0.43:
            secondary_gate[best_sec] = True
    front_line = primary_front | secondary_gate
    # 10m早期先頭候補は、最前線と同じ強さで固定せず適性に応じて残存率を縮小。
    front_weight = primary_front.astype(float) + secondary_gate.astype(float) * np.clip(
        0.48 + early_lead_strength * 0.34, 0.48, 0.78
    )
    inner_hold_prob *= front_weight

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
    ) * front_weight

    # 熱走路でも、格だけでなく実戦変換・捌き・安定性の裏付けがある追込車は
    # 一律に仕掛けを封じない。後段の追抜必要差を緩和する係数として使用する。
    heat_pursuit_relief = np.clip(
        ability_gate * 0.28
        + execution_quality * 0.24
        + traffic_conversion * 0.24
        + st_stability * 0.10
        + top3_rate * 0.14,
        0.0, 1.0,
    )
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
        "primary_front": primary_front,
        "secondary_front": secondary_gate,
        "early_lead_strength": early_lead_strength,
        "heat_pursuit_relief": heat_pursuit_relief,
    }


def simulate_detailed(df, trials, seed, track_temp=30.0):
    """Ver30: 10m早期先頭交代と逃げ切りを三連単の周回展開へ反映する。"""
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
    primary_front = arr.get("primary_front", front_line)
    secondary_front = arr.get("secondary_front", np.zeros_like(front_line, dtype=bool))
    early_lead_strength = arr.get("early_lead_strength", np.zeros(n, dtype=float))
    heat_pursuit_relief = arr.get("heat_pursuit_relief", np.zeros(n, dtype=float))
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
            if secondary_front.any():
                # Ver28: 表示順位は変えず、10m早期先頭候補を周回展開で強める。
                # 最前車を早めに交わして逃げ役へ移る混合展開を三連単へ反映する。
                base_pace[secondary_front] += (
                    0.050
                    + early_lead_strength[secondary_front] * 0.070
                    + heat_index * 0.025
                )
            chase_mask = handicap > min_handicap
            base_pace[chase_mask] += 0.05 + traffic_conversion[chase_mask] * 0.07
        elif scenario == "前残り":
            base_pace[front_line] += 0.14 + recent_form_strength[front_line] * 0.08
            if secondary_front.any():
                # 熱走路の前残りを0m固定にせず、条件を満たした10m車にも配分する。
                base_pace[secondary_front] += (
                    0.060
                    + early_lead_strength[secondary_front] * 0.085
                    + heat_index * 0.035
                )
            base_pace[~front_line] -= 0.05
        elif scenario == "混戦":
            base_pace += rng.normal(0, 0.10, n)
            if secondary_front.any():
                # 混戦でも早期に前へ付けた候補は3着内へ残る余地を持たせる。
                base_pace[secondary_front] += 0.025 + early_lead_strength[secondary_front] * 0.040
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

        # Ver30: 弱い最前車の直後にいる10m早期先頭候補が、序盤で先頭役を引き継ぐ分岐。
        # これまでは候補へ加点しても勝ち切り抽選の中心が0m車のまま残ることがあった。
        takeover_front = None
        sec_candidates = [int(i) for i in order if secondary_front[int(i)] and not severe[int(i)]]
        if sec_candidates and scenario in ("先行縦長", "前残り", "混戦"):
            takeover_front = max(sec_candidates, key=lambda i: early_lead_strength[i])
            primary_strength = float(np.max(early_lead_strength[primary_front])) if primary_front.any() else 0.45
            takeover_prob = np.clip(
                0.08
                + heat_index * 0.16
                + early_lead_strength[takeover_front] * 0.34
                + max(0.0, 0.48 - primary_strength) * 0.75
                + (0.09 if scenario == "前残り" else 0.04 if scenario == "先行縦長" else 0.0),
                0.08, 0.68,
            )
            if rng.random() < takeover_prob:
                cur_t = int(np.where(order == takeover_front)[0][0])
                order = np.delete(order, cur_t)
                order = np.insert(order, 0, takeover_front)
                initial_leader = takeover_front
                base_pace[takeover_front] += 0.10 + early_lead_strength[takeover_front] * 0.12 + heat_index * 0.04

        clean_escape = front_line[initial_leader] and not severe[initial_leader] and not normal[initial_leader] and rng.random() < np.clip(escape_success_prob[initial_leader] * escape_factor_map[int(scenario_id)], 0.02, 0.96)

        # 早期先頭交代が成立した場合は、その10m車を勝ち切り抽選の中心にする。
        front_candidates = [int(i) for i in order if front_line[int(i)]]
        if takeover_front is not None and initial_leader == takeover_front:
            inner_front = takeover_front
        else:
            inner_front = min(front_candidates, key=lambda i: cars[i]) if front_candidates else initial_leader
        inner_hold = (not clean_escape and not severe[inner_front] and rng.random() < np.clip(inner_hold_prob[inner_front] * hold_factor_map[int(scenario_id)], 0.02, 0.97))

        # Ver8.3: 展開成立時だけ勝ち切り抽選を行う。
        # 内枠残存だけの場合は、クリーンな逃げより勝ち切り条件を厳しくする。
        win_setup_factor = 1.0 if clean_escape else 0.66
        if takeover_front is not None and inner_front == takeover_front:
            win_setup_factor = max(win_setup_factor, 0.86 + heat_index * 0.08)
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
                # 捌き・実戦変換の裏付けがある車は、熱走路でも必要差の増加を最大55%緩和。
                position_heat *= (1.0 - 0.55 * heat_pursuit_relief[trailing])
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
            and (clean_escape or front_win or (takeover_front is not None and inner_front == takeover_front))
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
    """v3.8: 高速ベクトル型の6周イベントモデル。

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

    # Ver17: 予測競走タイムを確率差の中心へ追加する。
    predicted_time = pd.to_numeric(
        df.get("予測競走T", pd.Series(np.nan, index=df.index)), errors="coerce"
    ).to_numpy(float)
    if np.isfinite(predicted_time).any():
        pt_fill = float(np.nanmedian(predicted_time[np.isfinite(predicted_time)]))
        predicted_time = np.where(np.isfinite(predicted_time), predicted_time, pt_fill)
        pt_span = max(1e-6, float(predicted_time.max()-predicted_time.min()))
        time_strength = (predicted_time.max()-predicted_time)/pt_span
    else:
        pt_span = 0.0
        time_strength = trial_strength.copy()

    # 評価の中心。点数だけでなく予測タイム差を明示的に反映する。
    base=(z*.64 + stable*.24 + execution*.14 + current*.12 + trial_strength*.12 + time_strength*.68)
    # 明確なタイム差がある時だけランダム幅を縮める。混戦時は無理に確率を尖らせない。
    sharpness = np.clip((pt_span-.015)/.055, 0.0, 1.0)
    noise_scale = 1.0 - sharpness*.30
    noise_sd=np.clip((.62 + finish_sd*.44 + volatility*.25)*noise_scale, .40, 1.25)
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
        # 7車立てなどで作成した補助シート「未登録8」は計算へ入れない。
        # ダミーを入れてから結果だけ消すのではなく、点数の正規化前に除外する。
        current_name = str(current.get("選手名", current.get("name", "")) or "").strip()
        if re.match(r"^未登録\d+$", re.sub(r"\s+", "", current_name)):
            continue
        history = read_history(ws)
        metrics.append(
            player_metrics(car, current, history, race, settings)
        )

    if len(metrics) < 3:
        raise ValueError("実在選手を3人以上取得できませんでした。出走表の貼り付け内容を確認してください。")
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

    # Ver18: 結果登録で更新している10項目学習を本番予測へ適用する。
    # 旧版はここだけ5項目版(v39)のままで、保存した10項目重みが予測へ
    # 反映されない不整合があった。元モデルを保持しつつ最大±3点だけ補正する。
    df = v40_apply_adaptive_weights(df, DB_PATH)
    # Ver17: 当日試走を中心に予測競走タイムを作り、確率差の根拠として利用する。
    df = v17_add_predicted_time(df, track_temp=track_temp)

    # Ver24: 当日出走表に含まれる中期車成績・試走偏差・同ハンデ内枠・
    # 高温走路の位置価値を、最大±2.5点の補助補正としてシミュレーション前に反映する。
    # 結果に合わせて特定車を上げるのではなく、全車へ同じ規則を適用する。
    if "v24_apply_race_context_bonus" in globals():
        df = v24_apply_race_context_bonus(
            df, globals().get("LATEST_ENTRY_STATS"), track_temp=track_temp
        )
    # Ver25: 選手本人の履歴から、今回条件で繰り返し現れる得意・不得意を反映。
    if "v25_player_condition_affinity" in globals():
        df = v25_player_condition_affinity(
            df, globals().get("LATEST_ENTRY_STATS"), globals().get("LATEST_RACE_META"), DB_PATH
        )
    # Ver59: 初周先頭と逃げ残りを分離し、登録済み履歴から選手別に補正する。
    if "v59_apply_escape_history" in globals():
        df = v59_apply_escape_history(
            df, globals().get("LATEST_ENTRY_STATS"), globals().get("LATEST_RACE_META"), DB_PATH
        )
    # Ver60: グランドノートの周回変化と50℃以上の非線形な熱走路適性を反映。
    if "v60_apply_lap_and_heat_learning" in globals():
        df = v60_apply_lap_and_heat_learning(
            df, globals().get("LATEST_ENTRY_STATS"), globals().get("LATEST_RACE_META"), DB_PATH
        )
    # Ver65: 天候を走路・温度帯・時間帯と組み合わせた選手別適性として反映。
    if "v65_apply_weather_condition_learning" in globals():
        df = v65_apply_weather_condition_learning(
            df, globals().get("LATEST_ENTRY_STATS"), globals().get("LATEST_RACE_META"), DB_PATH
        )

    # v3.8: 逐次的な追抜き入替ループを使わず、高速ベクトル型の6周イベントモデルを使用。
    finish_counts, bet_counts = simulate(df, trials, seed, track_temp=track_temp)
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
    # 新しい出走表では「田中 竜二(浜松)」のように所属LGが氏名末尾へ付く。
    # 所属場だけを除き、DB照合・履歴検索に使う選手名を純粋な氏名へ統一する。
    tracks = "川口|伊勢崎|浜松|飯塚|山陽"
    name = re.sub(rf"\s*[（(](?:{tracks})[）)]\s*$", "", name)
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
    for s in ["良走路", "湿走路", "斑走路", "風走路", "良", "湿", "斑", "風"]:
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
        "レース名": None,
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

        # レース名・レース種別
        # 縦型履歴では「予選」「一般戦」「準決勝戦Ａ」「マイスター選抜」などが
        # レース番号の代わりになる正式なレース名として1行で記載される。
        # 大会名（例: Ｇ２川口記念）とは分離し、race_nameにも必ず反映する。
        race_name_keywords = [
            "一般", "予選", "準々決勝", "準決", "決勝", "優勝", "選抜", "特選",
            "マイスター", "ランチアタック", "グレードレース", "順位決定", "特別一般"
        ]
        if any(x in line for x in race_name_keywords):
            row["レース名"] = line
            row["レース種別"] = line
            continue

        # 天候
        if line in ["晴", "曇", "雨", "雪", "小雨"]:
            row["天候"] = line
            continue

        # 走路
        if line in ["良", "湿", "斑", "風", "良走路", "湿走路", "斑走路", "風走路"]:
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


# Ver35: ヘッダー付きタブ区切り履歴の貼り付け対応
def v35_parse_tabular_player_history(text, player_name=None):
    # タブは列区切りなので、通常のclean_textで空白へ潰さず保持する。
    cleaned = str(text or "").replace("\u3000", " ").replace("\xa0", " ")
    cleaned = re.sub(r"\r\n?", "\n", cleaned).strip()
    lines = [line.rstrip("\r") for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return pd.DataFrame()

    # Excel/サイト表コピーのタブ区切りを優先。連続空白は氏名等を壊しやすいため使わない。
    header = [x.strip() for x in lines[0].split("\t")]
    aliases = {
        "日付": "開催日", "開催日": "開催日",
        "場": "開催場", "開催場": "開催場",
        "R": "レース", "レース": "レース",
        "レース名": "レース名", "競走名": "レース名", "競走名称": "レース名",
        "着": "着順", "着順": "着順",
        "車番": "車番",
        "走路": "走路",
        "ハンデ": "ハンデ",
        "試走T": "試走T", "試走": "試走T",
        "競走T": "競走T", "競走": "競走T",
        "ST": "ST",
        "出走": "出走",
        "天候": "天候",
        "走路温度": "走路温度", "走温": "走路温度",
        "気温": "気温",
        "湿度": "湿度",
        "種別": "レース種別", "レース種別": "レース種別",
        "距離": "距離", "周回数": "周回数", "人気": "人気",
        "異": "異常", "異常": "異常", "事故": "異常", "事故内容": "異常",
    }
    mapped = [aliases.get(h) for h in header]
    required = {"開催日", "開催場", "着順", "試走T", "競走T", "ST"}
    if "\t" not in lines[0] or len(required.intersection({x for x in mapped if x})) < 4:
        return pd.DataFrame()

    def missing(v):
        return str(v).strip() in {"", "-", "—", "–", "―", "ー", "−", "null", "None", "nan"}

    def num(v, integer=False):
        if missing(v):
            return np.nan
        t = str(v).strip().replace(",", "")
        t = re.sub(r"(?:m|R|人気|周)$", "", t, flags=re.I).strip()
        try:
            x = float(t)
            return int(x) if integer else x
        except Exception:
            return np.nan

    rows = []
    for raw in lines[1:]:
        parts = [x.strip() for x in raw.split("\t")]
        if not any(parts):
            continue
        if len(parts) < len(header):
            parts += [""] * (len(header) - len(parts))
        elif len(parts) > len(header):
            parts = parts[:len(header)-1] + [" ".join(parts[len(header)-1:])]
        src = {mapped[i]: parts[i] for i in range(len(header)) if mapped[i]}

        date_value = src.get("開催日", "")
        date_iso = None
        if not missing(date_value):
            dm = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", date_value)
            if dm:
                date_iso = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

        surface = None if missing(src.get("走路", "")) else src.get("走路", "").replace("走路", "")
        rank = num(src.get("着順", ""), integer=True)
        race_no = num(src.get("レース", ""), integer=True)
        handicap = num(src.get("ハンデ", ""), integer=True)
        car_no = num(src.get("車番", ""), integer=True)

        row = {
            "選手名": v15_normalize_name(player_name or ""),
            "開催日": date_iso,
            "開催場": None if missing(src.get("開催場", "")) else src.get("開催場", ""),
            "レース": race_no,
            "レース名": None if missing(src.get("レース名", "")) else src.get("レース名", ""),
            "着順": rank,
            "出走": num(src.get("出走", ""), integer=True),
            "走路": surface,
            "ハンデ": handicap,
            "試走T": num(src.get("試走T", "")),
            "競走T": num(src.get("競走T", "")),
            "ST": num(src.get("ST", "")),
            "天候": None if missing(src.get("天候", "")) else src.get("天候", ""),
            "走路温度": num(src.get("走路温度", "")),
            "気温": num(src.get("気温", "")),
            "湿度": num(src.get("湿度", "")),
            "レース種別": None if missing(src.get("レース種別", "")) else src.get("レース種別", ""),
            "距離": num(src.get("距離", ""), integer=True),
            "周回数": num(src.get("周回数", ""), integer=True),
            "人気": num(src.get("人気", ""), integer=True),
            "異常": None if missing(src.get("異常", "")) else src.get("異常", ""),
            "車番": car_no,
            "_raw": raw,
        }
        # 日付・場・Rのどれかが取れた行だけ履歴候補として残す。欠損行も確認用に保持。
        if row["開催日"] or row["開催場"] or not pd.isna(row["レース"]):
            rows.append(row)

    return pd.DataFrame(rows)


# 既存関数を上書きし、縦型・タブ表・1行形式のすべてに対応
def v15_parse_player_history(text, player_name=None):
    tabular = v35_parse_tabular_player_history(text, player_name=player_name)
    if not tabular.empty:
        expected = [
            "選手名", "開催日", "開催場", "レース", "レース名", "着順", "出走", "走路",
            "ハンデ", "試走T", "競走T", "ST",
            "天候", "走路温度", "気温", "湿度",
            "レース種別", "距離", "周回数", "人気", "異常", "車番", "_raw"
        ]
        for col in expected:
            if col not in tabular.columns:
                tabular[col] = np.nan
        return tabular[expected]

    vertical = v151_parse_vertical_player_history(text, player_name=player_name)

    # 縦型が2件以上取れたらこちらを採用
    if not vertical.empty and vertical["開催日"].notna().sum() >= 1:
        expected = [
            "選手名", "開催日", "開催場", "レース", "レース名", "着順", "出走", "走路",
            "ハンデ", "試走T", "競走T", "ST",
            "天候", "走路温度", "気温", "湿度",
            "レース種別", "距離", "周回数", "人気", "異常", "車番", "_raw"
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
        "race_name": "TEXT",
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
                and any(is_handicap_st_line(lines[j]) for j in range(i + 1, min(len(lines), i + 5)))
            )

        # 一部コピー形式: 車番、氏名、ハンデ/ST がそれぞれ別行
        m_single = re.fullmatch(r"([1-8])", line)
        single_start = bool(
            m_single
            and i + 2 < len(lines)
            and looks_like_player_name(lines[i + 1])
            and any(is_handicap_st_line(lines[j]) for j in range(i + 2, min(len(lines), i + 6)))
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
    else:
        # スマホ縦型では「ハンデ10m/ST0.17 3.48」のように試走Tの見出しが省略される。
        tm2 = re.search(r"ST\s*[+-]?\d?\.\d{2,3}\s+([3-9]\.\d{2,3}|-)", compact, re.I)
        if tm2 and tm2.group(1) != "-":
            trial = float(tm2.group(1))

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
def v17_detect_nonstarters(text):
    """出走表本文から欠車・出走取消などの車番を安全に検出する。

    車番ごとの選手ブロック内だけを検査するため、次の選手にある「欠車」を
    直前の選手へ誤って割り当てない。
    """
    clean = v15_clean_text(text)
    status_pattern = re.compile(r"欠車|出走取消|出走取り消し|不出走|除外|参加解除")
    excluded = {}

    # 公式縦型表示を車番見出し単位で分割する。
    starts = list(re.finditer(r"(?m)^\s*([1-8])(?:[\t 　]+)(?=\S)", clean))
    for idx, match in enumerate(starts):
        car = int(match.group(1))
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(clean)
        block = clean[match.start():end]
        found = status_pattern.search(block)
        if found:
            excluded[car] = found.group(0)
    return excluded


def v15_parse_entries(text, manual_excluded=None):
    excluded = dict(v17_detect_nonstarters(text))
    if manual_excluded is not None:
        # 画面で指定した状態を最優先。空集合なら全車出走として扱う。
        excluded = {int(car): "手動欠車" for car in manual_excluded}
    # 公式表示の「ハンデ0m/ST...」も従来パーサーの「0m/ST...」形式へ正規化。
    parse_text = re.sub(r"(?m)^\s*ハンデ\s*", "", v15_clean_text(text))
    # 欠車表示が氏名とハンデの間に入っても、ブロック認識できるよう除去して解析する。
    parse_text = re.sub(r"(?m)^\s*(欠車|出走取消|出走取り消し|不出走|除外|参加解除)\s*$", "", parse_text)
    vertical = v152_parse_vertical_entries(parse_text)

    if not vertical.empty and vertical["車番"].nunique() >= 2:
        vertical = vertical.copy()
        vertical["出走状態"] = vertical["車番"].map(lambda x: excluded.get(int(x), "出走"))
        vertical["解析対象"] = ~vertical["車番"].astype(int).isin(excluded)
        # シミュレーションへは実際に出走する選手だけ渡す。
        vertical = vertical[vertical["解析対象"]].copy()
        return vertical.sort_values("車番").reset_index(drop=True)

    rows = []
    seen = set()
    for raw in v15_clean_text(text).splitlines():
        row = v15_parse_entry_line(raw)
        if not row:
            continue
        key = row["車番"]
        if key in seen or int(key) in excluded:
            continue
        seen.add(key)
        row["出走状態"] = "出走"
        row["解析対象"] = True
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
    m = re.search(r"(良走路|湿走路|斑走路|風走路|良|湿|斑|風)\s*/\s*(-?\d+(?:\.\d+)?)℃", compact)
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


# ============================================================
# Ver61: 予測対象レース以降のデータを完全除外
# ============================================================
VER61_LEARNING_BOUNDARY = {
    "date": "", "race_no": None, "used_rows": 0, "excluded_rows": 0,
    "players": 0, "same_day_before_rows": 0, "unknown_r_same_day_excluded": 0,
}

def v61_race_no(value):
    """5R / 5 / 5.0 を整数レース番号へ統一。取得不能はNone。"""
    if value is None:
        return None
    m = re.search(r"(\d{1,2})", str(value))
    if not m:
        return None
    try:
        n = int(m.group(1))
        return n if 1 <= n <= 12 else None
    except Exception:
        return None


def v61_normalize_date(value):
    try:
        if "v47_normalize_required_date" in globals():
            return v47_normalize_required_date(value)
    except Exception:
        pass
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return s


def v61_set_learning_boundary(meta=None):
    meta = meta or {}
    date = v61_normalize_date(meta.get("開催日") or meta.get("日付") or "")
    race_no = v61_race_no(meta.get("レース") or meta.get("R") or meta.get("race_no"))
    VER61_LEARNING_BOUNDARY.update({
        "date": date, "race_no": race_no, "used_rows": 0, "excluded_rows": 0,
        "players": 0, "same_day_before_rows": 0, "unknown_r_same_day_excluded": 0,
    })
    return dict(VER61_LEARNING_BOUNDARY)


def v61_filter_history_df(df, date_col="開催日", race_col="レース", count_stats=True):
    """
    対象日より前、または対象日かつ対象Rより前だけを返す。
    対象Rが不明なら同日データをすべて除外する。
    同日で履歴側Rが不明な行も安全側で除外する。
    """
    if df is None or df.empty:
        return df
    target_date = VER61_LEARNING_BOUNDARY.get("date") or ""
    target_r = VER61_LEARNING_BOUNDARY.get("race_no")
    if not target_date or date_col not in df.columns:
        if count_stats:
            VER61_LEARNING_BOUNDARY["used_rows"] += len(df)
            VER61_LEARNING_BOUNDARY["players"] += 1
        return df.copy()
    x = df.copy()
    dates = x[date_col].map(v61_normalize_date)
    before = dates < target_date
    same = dates == target_date
    after = dates > target_date
    same_before = pd.Series(False, index=x.index)
    unknown_same = pd.Series(False, index=x.index)
    if target_r is not None and race_col in x.columns:
        rr = x[race_col].map(v61_race_no)
        same_before = same & rr.notna() & rr.lt(target_r)
        unknown_same = same & rr.isna()
    else:
        unknown_same = same
    keep = before | same_before
    out = x.loc[keep].copy()
    if count_stats:
        VER61_LEARNING_BOUNDARY["used_rows"] += int(keep.sum())
        VER61_LEARNING_BOUNDARY["excluded_rows"] += int((~keep).sum())
        VER61_LEARNING_BOUNDARY["same_day_before_rows"] += int(same_before.sum())
        VER61_LEARNING_BOUNDARY["unknown_r_same_day_excluded"] += int(unknown_same.sum())
        VER61_LEARNING_BOUNDARY["players"] += 1
    return out


def v61_filter_lap_df(df, race_key_col="race_key"):
    """race_key(YYYYMMDD_開催場_6R)を対象日・対象R境界で除外。"""
    if df is None or df.empty or race_key_col not in df.columns:
        return df
    target_date = (VER61_LEARNING_BOUNDARY.get("date") or "").replace("-", "")
    target_r = VER61_LEARNING_BOUNDARY.get("race_no")
    if not target_date:
        return df.copy()
    x = df.copy()
    keys = x[race_key_col].astype(str)
    dates = keys.str.extract(r"^(\d{8})", expand=False)
    rr = keys.str.extract(r"_(\d{1,2})R(?:_|$)", expand=False)
    rr = pd.to_numeric(rr, errors="coerce")
    before = dates < target_date
    same = dates == target_date
    if target_r is None:
        keep = before
    else:
        keep = before | (same & rr.notna() & rr.lt(target_r))
    return x.loc[keep].copy()


def v61_learning_boundary_summary():
    b = dict(VER61_LEARNING_BOUNDARY)
    r = b.get("race_no")
    if b.get("date"):
        b["label"] = f"{b['date']} {str(r)+'R' if r is not None else '同日全除外'} より前"
    else:
        b["label"] = "境界日を取得できず（全履歴）"
    return b

def ver16_get_history(name):
    """選手履歴を取得し、Ver61の時点境界を必ず適用する。"""
    df = pd.DataFrame()
    try:
        df = get_player_history(v15_normalize_name(name), model_only=True)
    except Exception:
        df = pd.DataFrame()

    if df is None or df.empty:
        try:
            target = re.sub(r"[\s　]+", "", str(name))
            with sqlite3.connect(str(DB_PATH)) as con:
                df = pd.read_sql_query("""
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
                      AND h.finish IS NOT NULL AND h.finish >= 1
                      AND h.trial_time IS NOT NULL AND h.trial_time > 0
                      AND h.race_time IS NOT NULL AND h.race_time > h.trial_time
                      AND h.start_time IS NOT NULL AND h.start_time > 0
                      AND COALESCE(h.result_status,'') NOT LIKE '%欠責%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%周誤%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%欠車%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%出走取消%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%競走中止%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%落車%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%反則%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%不成立%'
                      AND COALESCE(h.result_status,'') NOT LIKE '%失格%'
                    ORDER BY h.race_date DESC, h.history_id DESC
                """, con, params=(target,))
        except Exception:
            df = pd.DataFrame()
    return v61_filter_history_df(df, "開催日", "レース", count_stats=True)

def ver16_make_settings_sheet(ws):
    ws["A1"] = "設定項目"
    ws["B1"] = "値"
    defaults = {
        4:30, 5:1.35, 6:0.85, 7:1.5, 8:0.7, 9:1.4, 10:1.1, 11:0.75,
        12:0.04, 22:0.012, 29:24, 31:8, 32:8, 33:5, 34:5, 35:6,
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

def ver16_build_virtual_excel(text, manual_excluded=None):
    meta = v15_parse_race_meta(text)
    entries = v15_parse_entries(text, manual_excluded=manual_excluded)

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

def v17_add_predicted_time(df, track_temp=30.0):
    """点数を補助に使いながら、当日試走を中心に相対的な予測競走タイムを作る。"""
    if df is None or df.empty:
        return df
    out = df.copy()
    n = len(out)
    trial = pd.to_numeric(out.get("試走換算", pd.Series(np.nan, index=out.index)), errors="coerce")
    if trial.notna().any():
        trial = trial.fillna(float(trial.median()))
    else:
        trial = pd.Series(3.50, index=out.index, dtype=float)
    st = pd.to_numeric(out.get("平均ST", pd.Series(.15, index=out.index)), errors="coerce").fillna(.15)
    handicap = pd.to_numeric(out.get("ハンデ", pd.Series(0, index=out.index)), errors="coerce").fillna(0)
    practical = pd.to_numeric(out.get("実戦能力点", pd.Series(50, index=out.index)), errors="coerce").fillna(50)
    closing = pd.to_numeric(out.get("終盤指数", pd.Series(50, index=out.index)), errors="coerce").fillna(50)
    current = pd.to_numeric(out.get("当日状態指数", pd.Series(.5, index=out.index)), errors="coerce").fillna(.5)
    stable = pd.to_numeric(out.get("安定上位指数", pd.Series(.5, index=out.index)), errors="coerce").fillna(.5)

    def norm(v):
        a = np.asarray(v, dtype=float)
        lo, hi = np.nanpercentile(a, 10), np.nanpercentile(a, 90)
        if not np.isfinite(lo) or not np.isfinite(hi) or hi-lo < 1e-9:
            return np.full(len(a), .5)
        return np.clip((a-lo)/(hi-lo), 0, 1)

    practical_n = norm(practical)
    closing_n = norm(closing)
    st_penalty = (st - float(st.min())) * 0.10
    # ハンデはスタート地点の差として扱う。後方車を単純に弱者扱いせず、負担のみ小さく秒換算。
    handicap_penalty = (handicap - float(handicap.min())) / 10.0 * 0.006
    ability_bonus = practical_n * 0.010 + closing_n * 0.006 + np.asarray(current) * 0.005 + np.asarray(stable) * 0.004
    heat_penalty = max(0.0, (float(track_temp or 30.0)-45.0)/15.0) * 0.004
    predicted = np.asarray(trial, dtype=float) + 0.095 + st_penalty + handicap_penalty + heat_penalty - ability_bonus
    out["予測競走T"] = np.round(predicted, 4)

    spread = float(np.nanmax(predicted)-np.nanmin(predicted)) if len(predicted) else 0.0
    if spread >= .060:
        confidence = "本命明確"
    elif spread >= .030:
        confidence = "やや本命"
    else:
        confidence = "混戦"
    out["レース信頼度"] = confidence
    return out



def v24_apply_race_context_bonus(df, entries=None, track_temp=30.0):
    """Ver24: 穴候補を拾うための当日文脈補正。

    補正材料:
    1) 同ハンデ内枠の序盤位置価値
    2) 近90日相当の車2連対率・3連対率
    3) 試走偏差の再現性
    4) 高温走路での前・中団の残りやすさ

    最大±2.5点に制限し、元モデルの順位を丸ごと置き換えない。
    """
    if df is None or df.empty or entries is None or len(entries) == 0:
        return df
    out = df.copy()
    ent = entries.copy()
    car_col = "車" if "車" in out.columns else "車番"
    if car_col not in out.columns or "車番" not in ent.columns:
        return out

    def nseries(frame, col, default=np.nan):
        return pd.to_numeric(frame.get(col, pd.Series(default, index=frame.index)), errors="coerce")

    # 車番で当日データを結合
    keep = [c for c in ["車番", "ハンデ", "試走偏差", "2連対率", "3連対率", "良2連対率", "良3連対率"] if c in ent.columns]
    current = ent[keep].drop_duplicates("車番").copy()
    current["車番"] = pd.to_numeric(current["車番"], errors="coerce")
    out[car_col] = pd.to_numeric(out[car_col], errors="coerce")
    merged = out[[car_col]].merge(current, left_on=car_col, right_on="車番", how="left")

    handicap = nseries(merged, "ハンデ", 0.0).fillna(nseries(out, "ハンデ", 0.0)).fillna(0.0)
    car = nseries(out, car_col, 99).fillna(99)

    # 1) 同ハンデ内枠。2車以上の線で、最内を最大+1.2点。
    lane_bonus = np.zeros(len(out), dtype=float)
    for h in sorted(handicap.dropna().unique()):
        idx = np.flatnonzero(np.isclose(handicap.to_numpy(float), float(h)))
        if len(idx) < 2:
            continue
        ordered = idx[np.argsort(car.iloc[idx].to_numpy(float), kind="stable")]
        if len(ordered) == 2:
            vals = np.array([1.00, -0.15])
        else:
            vals = np.linspace(1.20, -0.45, len(ordered))
        lane_bonus[ordered] = vals

    # 2) 車の中期成績。良走路では良成績を少し優先し、欠損時は全体成績を利用。
    two = nseries(merged, "2連対率")
    three = nseries(merged, "3連対率")
    good_two = nseries(merged, "良2連対率")
    good_three = nseries(merged, "良3連対率")
    two_use = good_two.where(good_two.notna(), two).fillna(two.median() if two.notna().any() else 25.0)
    three_use = good_three.where(good_three.notna(), three).fillna(three.median() if three.notna().any() else 40.0)
    medium_raw = two_use * 0.42 + three_use * 0.58
    center = float(medium_raw.median()) if medium_raw.notna().any() else 35.0
    spread = float((medium_raw.quantile(.85) - medium_raw.quantile(.15))) if medium_raw.notna().sum() >= 3 else 20.0
    spread = max(spread, 12.0)
    medium_bonus = np.clip((medium_raw.to_numpy(float) - center) / spread * 1.8, -1.1, 1.4)

    # 3) 試走偏差。小さいほど本走へ再現しやすい。絶対値とメンバー相対を併用。
    dev = nseries(merged, "試走偏差")
    dev_fill = dev.fillna(dev.median() if dev.notna().any() else 0.095)
    dev_center = float(dev_fill.median())
    deviation_bonus = np.clip((dev_center - dev_fill.to_numpy(float)) / 0.025 * 0.75, -0.65, 0.95)

    # 4) 高温走路。45℃超から前・中団を緩やかに支援し、最後方線を少し抑える。
    temp = float(track_temp or 30.0)
    heat = np.clip((temp - 45.0) / 10.0, 0.0, 1.0)
    hmin, hmax = float(handicap.min()), float(handicap.max())
    hrange = max(hmax - hmin, 10.0)
    frontness = 1.0 - (handicap.to_numpy(float) - hmin) / hrange
    heat_bonus = heat * np.clip((frontness - 0.30) * 1.15, -0.40, 0.70)

    total = np.clip(lane_bonus + medium_bonus + deviation_bonus + heat_bonus, -2.5, 2.5)
    out["同ハンデ内枠補正"] = np.round(lane_bonus, 3)
    out["車中期成績補正"] = np.round(medium_bonus, 3)
    out["試走偏差補正"] = np.round(deviation_bonus, 3)
    out["高温位置補正"] = np.round(heat_bonus, 3)
    out["Ver24展開穴補正"] = np.round(total, 3)

    if "改善後総合点" in out.columns:
        out["改善後総合点"] = pd.to_numeric(out["改善後総合点"], errors="coerce").fillna(0.0) + total
        out["改善後順位"] = out["改善後総合点"].rank(method="min", ascending=False).astype(int)
    # シミュレーションが参照する当日指数にも小さく反映。二重加点を避けて0.35倍。
    if "当日レース指数" in out.columns:
        out["当日レース指数"] = pd.to_numeric(out["当日レース指数"], errors="coerce").fillna(50.0) + total * 0.35
    if "予測競走T" in out.columns:
        out["予測競走T"] = np.round(pd.to_numeric(out["予測競走T"], errors="coerce") - total * 0.0012, 4)
    return out



def v25_player_condition_affinity(df, entries=None, meta=None, db_path=DB_PATH):
    """選手ごとに、特定条件で繰り返し現れる得意・不得意を学習して補正する。

    対象条件は走路、走路温度帯、湿度帯、開催場、ハンデ帯、レース種別。
    本人の全履歴に対する条件内成績の差を使い、件数と再現性で縮小する。
    少数データの偶然を避けるため、条件一致4件未満は原則採用せず、
    最終補正も最大±2.2点に制限する。
    """
    if df is None or df.empty or entries is None or len(entries) == 0:
        return df
    out = df.copy()
    meta = meta or globals().get("LATEST_RACE_META", {}) or {}
    car_col = "車" if "車" in out.columns else "車番"
    if car_col not in out.columns:
        return out

    def bucket_temp(v):
        try: v=float(v)
        except Exception: return None
        if v >= 50: return "50℃以上"
        if v >= 45: return "45-49℃"
        if v >= 35: return "35-44℃"
        if v >= 25: return "25-34℃"
        return "24℃以下"

    def bucket_humidity(v):
        try: v=float(v)
        except Exception: return None
        if v >= 80: return "80%以上"
        if v >= 60: return "60-79%"
        if v >= 40: return "40-59%"
        return "39%以下"

    def bucket_handicap(v):
        try: v=float(v)
        except Exception: return None
        if v <= 0: return "0m"
        if v <= 10: return "10m"
        if v <= 20: return "20m"
        if v <= 30: return "30m"
        return "40m以上"

    current_conditions = {
        "surface": str(meta.get("走路") or "").strip(),
        "track_temp": bucket_temp(meta.get("走路温度")),
        "humidity": bucket_humidity(meta.get("湿度")),
        "venue": str(meta.get("開催場") or "").strip(),
        "race_type": str(meta.get("レース種別") or meta.get("種別") or "").strip(),
    }

    ent = entries.copy()
    ent["車番"] = pd.to_numeric(ent.get("車番"), errors="coerce")
    names = ent.set_index("車番").get("選手名", pd.Series(dtype=object)).to_dict()
    handicaps = pd.to_numeric(ent.set_index("車番").get("ハンデ", pd.Series(dtype=float)), errors="coerce").to_dict()

    bonuses=[]; labels=[]; match_counts=[]; confidences=[]
    try:
        con = sqlite3.connect(db_path)
    except Exception:
        con = None
    for _, row in out.iterrows():
        car = pd.to_numeric(pd.Series([row.get(car_col)]), errors="coerce").iloc[0]
        name = str(names.get(car, row.get("選手名", ""))).strip()
        h_now = bucket_handicap(handicaps.get(car, row.get("ハンデ")))
        if not name or con is None:
            bonuses.append(0.0); labels.append("データなし"); match_counts.append(0); confidences.append(0.0); continue
        key = v32_player_name_key(name) if "v32_player_name_key" in globals() else re.sub(r"[\s　]", "", name)
        hist = pd.read_sql_query("""
            SELECT player_name, race_date, venue, rank, starters, surface, handicap,
                   track_temp, humidity, race_type
            FROM v15_player_history_imports
            WHERE replace(replace(player_name,' ',''),'　','')=?
              AND rank IS NOT NULL AND rank > 0
              AND (starters IS NULL OR rank <= starters)
              AND trial_time IS NOT NULL AND trial_time > 0
              AND race_time IS NOT NULL AND race_time > trial_time
              AND st IS NOT NULL AND st > 0
              AND COALESCE(raw_line,'') NOT LIKE '%欠責%'
              AND COALESCE(raw_line,'') NOT LIKE '%周誤%'
              AND COALESCE(raw_line,'') NOT LIKE '%欠車%'
              AND COALESCE(raw_line,'') NOT LIKE '%出走取消%'
              AND COALESCE(raw_line,'') NOT LIKE '%競走中止%'
              AND COALESCE(raw_line,'') NOT LIKE '%落車%'
              AND COALESCE(raw_line,'') NOT LIKE '%反則%'
              AND COALESCE(raw_line,'') NOT LIKE '%不成立%'
              AND COALESCE(raw_line,'') NOT LIKE '%失格%'
            ORDER BY race_date DESC
            LIMIT 160
        """, con, params=(key,))
        if hist.empty:
            bonuses.append(0.0); labels.append("履歴不足"); match_counts.append(0); confidences.append(0.0); continue
        rank = pd.to_numeric(hist["rank"], errors="coerce")
        starters = pd.to_numeric(hist["starters"], errors="coerce").fillna(8).clip(lower=2)
        # 1着=1、最下位=0の相対成績。人数差を吸収する。
        perf = ((starters-rank)/(starters-1)).clip(0,1)
        valid = rank.notna() & perf.notna()
        hist=hist.loc[valid].copy(); perf=perf.loc[valid]
        if len(hist) < 8:
            bonuses.append(0.0); labels.append("履歴不足"); match_counts.append(len(hist)); confidences.append(0.0); continue
        base=float(perf.mean())
        hist["temp_bucket"] = hist["track_temp"].map(bucket_temp)
        hist["humidity_bucket"] = hist["humidity"].map(bucket_humidity)
        hist["handicap_bucket"] = hist["handicap"].map(bucket_handicap)
        specs=[
            ("走路", "surface", current_conditions["surface"], 1.00),
            ("熱走路", "temp_bucket", current_conditions["track_temp"], 0.95),
            ("湿度", "humidity_bucket", current_conditions["humidity"], .70),
            ("開催場", "venue", current_conditions["venue"], .65),
            ("ハンデ帯", "handicap_bucket", h_now, .90),
            ("レース種別", "race_type", current_conditions["race_type"], .55),
        ]
        parts=[]
        for label,col,val,strength in specs:
            if val in (None, "") or col not in hist.columns: continue
            mask=hist[col].astype(str).eq(str(val))
            n=int(mask.sum())
            if n < 4: continue
            cond=float(perf.loc[hist.index[mask]].mean())
            delta=cond-base
            # Ver27: 少数条件の偶然を強く学習しない。
            # 4-7件±0.3、8-14件±0.7、15-24件±1.2、25件以上±1.8が上限。
            conf=min(1.0, max(0.0, (n-3)/18.0))
            if n <= 7:
                sample_cap = 0.30
            elif n <= 14:
                sample_cap = 0.70
            elif n <= 24:
                sample_cap = 1.20
            else:
                sample_cap = 1.80
            # 直近偏重を避けながら、顕著な差だけ採用。
            effect=float(np.clip(delta*4.4*strength*conf, -sample_cap, sample_cap))
            if abs(effect) >= .08:
                parts.append((effect,label,n,delta,conf))
        if not parts:
            bonuses.append(0.0); labels.append("顕著な適性なし"); match_counts.append(0); confidences.append(0.0); continue
        # 同じレース結果を複数条件で数えすぎないよう、強い上位3要素だけ採用。
        parts=sorted(parts, key=lambda x: abs(x[0]), reverse=True)[:3]
        raw=sum(x[0] for x in parts)
        # 上位条件を合算しても、最大一致件数に対応した信頼上限を超えない。
        max_n=max(x[2] for x in parts)
        total_cap = 0.30 if max_n <= 7 else 0.70 if max_n <= 14 else 1.20 if max_n <= 24 else 1.80
        total=float(np.clip(raw, -total_cap, total_cap))
        bonuses.append(total)
        labels.append(" / ".join(f"{x[1]}{'得意' if x[0]>0 else '苦手'}({x[2]}件)" for x in parts))
        match_counts.append(max(x[2] for x in parts))
        confidences.append(round(max(x[4] for x in parts),3))
    if con is not None: con.close()

    bonus=np.asarray(bonuses,dtype=float)
    out["選手別条件適性補正"] = np.round(bonus,3)
    out["条件適性根拠"] = labels
    out["条件一致最大件数"] = match_counts
    out["条件適性信頼度"] = confidences
    if "改善後総合点" in out.columns:
        out["改善後総合点"] = pd.to_numeric(out["改善後総合点"], errors="coerce").fillna(0.0)+bonus
        out["改善後順位"] = out["改善後総合点"].rank(method="min", ascending=False).astype(int)
    if "当日レース指数" in out.columns:
        out["当日レース指数"] = pd.to_numeric(out["当日レース指数"], errors="coerce").fillna(50.0)+bonus*.42
    if "予測競走T" in out.columns:
        out["予測競走T"] = np.round(pd.to_numeric(out["予測競走T"], errors="coerce")-bonus*.0013,4)
    return out



# ============================================================
# Ver59: 選手別の逃げ役・逃げ残り学習
# ============================================================
def v59_apply_escape_history(df, entries=None, meta=None, db_path=None):
    """
    登録済み履歴から、最前線選手の「初周先頭」と「残り」を別々に推定する。

    周回順位が少ない間は、0m実績・着順・ST・今回の位置関係を中心に使い、
    周回データが増えるほど実測の初周先頭率を優先する。
    現在レースの日付以降は学習対象から除外し、結果の先読みを防ぐ。
    """
    out = df.copy()
    n = len(out)
    defaults = {
        "初周先頭推定": np.zeros(n),
        "逃げ残り推定": np.zeros(n),
        "逃切り推定": np.zeros(n),
        "逃げ履歴件数": np.zeros(n, dtype=int),
        "初周先頭実測件数": np.zeros(n, dtype=int),
        "逃げ履歴補正": np.zeros(n),
        "逃げ判定": ["対象外"] * n,
        "逃げ根拠": ["最前線ではありません"] * n,
    }
    for c, v in defaults.items():
        out[c] = v

    if out.empty:
        return out

    handicaps = pd.to_numeric(out.get("ハンデ"), errors="coerce").fillna(0.0)
    front_h = float(handicaps.min())
    front_idx = list(out.index[handicaps.eq(front_h)])
    if not front_idx:
        return out

    db_path = db_path or globals().get("DB_PATH")
    current_date = str((meta or {}).get("開催日") or (meta or {}).get("日付") or "").strip()
    current_date = v47_normalize_required_date(current_date) if "v47_normalize_required_date" in globals() else current_date

    con = None
    try:
        if db_path and Path(str(db_path)).exists():
            con = sqlite3.connect(str(db_path))
    except Exception:
        con = None

    front_sorted = out.loc[front_idx].sort_values("車")
    front_count = len(front_sorted)
    st_now = pd.to_numeric(front_sorted.get("平均ST"), errors="coerce").fillna(0.20)
    st_rank = st_now.rank(method="average", ascending=True)

    for order, idx in enumerate(front_sorted.index):
        row = out.loc[idx]
        name = str(row.get("選手名") or "").strip()
        car = int(pd.to_numeric(pd.Series([row.get("車")]), errors="coerce").fillna(99).iloc[0])
        inner = 1.0 if front_count <= 1 else 1.0 - order / max(1, front_count - 1)
        st_adv = 0.60 if front_count <= 1 else 1.0 - (float(st_rank.loc[idx]) - 1.0) / max(1.0, front_count - 1.0)
        lone_front = 1.0 if front_count == 1 else 0.0

        hist = pd.DataFrame()
        lap = pd.DataFrame()
        if con is not None and name:
            key = v32_player_name_key(name) if "v32_player_name_key" in globals() else re.sub(r"\s+", "", name)
            try:
                hist = pd.read_sql_query(
                    """SELECT race_date, venue, rank, handicap, st, car_no, surface, track_temp, player_name
                       FROM v15_player_history_imports""", con
                )
                hist = hist[hist["player_name"].map(v32_player_name_key).eq(key)] if "player_name" in hist.columns else hist.iloc[0:0]
            except Exception:
                try:
                    hist = pd.read_sql_query(
                        """SELECT race_date, venue, rank, handicap, st, car_no, surface, track_temp, player_name
                           FROM v15_player_history_imports""", con
                    )
                    hist = hist[hist["player_name"].map(v32_player_name_key).eq(key)]
                except Exception:
                    hist = pd.DataFrame()
            try:
                lap = pd.read_sql_query(
                    "SELECT race_key, lap_label, position, player_name FROM player_lap_history", con
                )
                lap = lap[lap["player_name"].map(v32_player_name_key).eq(key)]
            except Exception:
                lap = pd.DataFrame()

        if not hist.empty:
            hist["race_date"] = hist["race_date"].astype(str)
            if current_date:
                hist = hist[hist["race_date"] < current_date]
            hist["handicap"] = pd.to_numeric(hist["handicap"], errors="coerce")
            hist["rank"] = pd.to_numeric(hist["rank"], errors="coerce")
            hist["st"] = pd.to_numeric(hist["st"], errors="coerce")
            same_front = hist[hist["handicap"].eq(front_h) & hist["rank"].notna()].copy()
        else:
            same_front = pd.DataFrame()

        hcount = len(same_front)
        # ベイズ平滑化。少数履歴で0%/100%に振り切れないようにする。
        wins = int((same_front.get("rank", pd.Series(dtype=float)) == 1).sum()) if hcount else 0
        top2 = int((same_front.get("rank", pd.Series(dtype=float)) <= 2).sum()) if hcount else 0
        top3 = int((same_front.get("rank", pd.Series(dtype=float)) <= 3).sum()) if hcount else 0
        win_rate = (wins + 1.0) / (hcount + 8.0)
        top2_rate = (top2 + 2.0) / (hcount + 8.0)
        top3_rate = (top3 + 3.0) / (hcount + 8.0)

        recent = same_front.sort_values("race_date", ascending=False).head(8) if hcount else same_front
        recent_top3 = ((recent["rank"] <= 3).sum() + 2.0) / (len(recent) + 5.0) if len(recent) else 0.40
        hist_st = same_front["st"].dropna() if hcount else pd.Series(dtype=float)
        st_quality = float(np.clip((0.23 - hist_st.mean()) / 0.16, 0.0, 1.0)) if len(hist_st) else 0.50

        lap_count = 0
        lap_lead_rate = 0.50
        if not lap.empty:
            if current_date:
                lap = v61_filter_lap_df(lap, "race_key")
            first_laps = lap[lap["lap_label"].astype(str).eq("1周目")]
            lap_count = len(first_laps)
            if lap_count:
                lap_lead_rate = (int((pd.to_numeric(first_laps["position"], errors="coerce") == 1).sum()) + 1.0) / (lap_count + 2.0)

        position_lead = 0.23 + inner * 0.24 + st_adv * 0.20 + lone_front * 0.20 + st_quality * 0.13
        lap_weight = min(0.55, lap_count / 10.0)
        first_lead = float(np.clip(position_lead * (1.0 - lap_weight) + lap_lead_rate * lap_weight, 0.08, 0.94))
        hold = float(np.clip(first_lead * 0.34 + top3_rate * 0.34 + top2_rate * 0.18 + recent_top3 * 0.14, 0.08, 0.88))
        win = float(np.clip(first_lead * 0.30 + win_rate * 0.52 + top2_rate * 0.18, 0.03, 0.72))

        old_escape = float(pd.to_numeric(pd.Series([row.get("逃げ成功率")]), errors="coerce").fillna(0.35).iloc[0])
        old_hold = float(pd.to_numeric(pd.Series([row.get("内枠残存率")]), errors="coerce").fillna(0.40).iloc[0])
        out.at[idx, "逃げ成功率"] = float(np.clip(old_escape * 0.52 + win * 0.48, 0.03, 0.86))
        out.at[idx, "内枠残存率"] = float(np.clip(old_hold * 0.42 + hold * 0.58, 0.08, 0.90))
        out.at[idx, "初周先頭推定"] = round(first_lead, 3)
        out.at[idx, "逃げ残り推定"] = round(hold, 3)
        out.at[idx, "逃切り推定"] = round(win, 3)
        out.at[idx, "逃げ履歴件数"] = int(hcount)
        out.at[idx, "初周先頭実測件数"] = int(lap_count)

        # 1着固定ではなく、主に2～3着残りへ効かせる小さな補正。
        bonus = float(np.clip((hold - 0.42) * 2.2 + (first_lead - 0.55) * 0.7, -0.65, 1.05))
        out.at[idx, "逃げ履歴補正"] = round(bonus, 3)
        if first_lead >= 0.70 and hold >= 0.55:
            label = "逃げ役濃厚・残り期待"
        elif first_lead >= 0.68:
            label = "逃げ役濃厚"
        elif first_lead >= 0.55:
            label = "逃げ候補"
        else:
            label = "逃げ不確実"
        out.at[idx, "逃げ判定"] = label
        out.at[idx, "逃げ根拠"] = (
            f"最前{int(front_h)}m・内側度{inner:.2f}・ST優位{st_adv:.2f} / "
            f"同ハンデ{hcount}走: 1着{wins}、2着内{top2}、3着内{top3} / "
            f"初周順位実測{lap_count}走"
        )

    if con is not None:
        con.close()

    bonus_s = pd.to_numeric(out["逃げ履歴補正"], errors="coerce").fillna(0.0)
    if "改善後総合点" in out.columns:
        out["改善後総合点"] = pd.to_numeric(out["改善後総合点"], errors="coerce").fillna(0.0) + bonus_s
        out["改善後順位"] = out["改善後総合点"].rank(method="min", ascending=False).astype(int)
    if "当日レース指数" in out.columns:
        out["当日レース指数"] = pd.to_numeric(out["当日レース指数"], errors="coerce").fillna(50.0) + bonus_s * 0.34
    if "予測競走T" in out.columns:
        out["予測競走T"] = np.round(pd.to_numeric(out["予測競走T"], errors="coerce") - bonus_s * 0.0008, 4)
    return out



# ============================================================
# Ver60: グランドノート展開学習・50℃以上の熱走路細分化
# ============================================================
def v60_heat_band(track_temp):
    """50℃付近の非線形な変化を細かく分ける。"""
    try:
        t = float(track_temp)
    except Exception:
        t = 30.0
    if t < 42: return "～41℃", 0.00
    if t < 47: return "42～46℃", 0.12
    if t < 50: return "47～49℃", 0.30
    if t < 52: return "50～51℃", 0.58
    if t < 54: return "52～53℃", 0.76
    if t < 56: return "54～55℃", 0.90
    return "56℃以上", 1.00


def _v60_name_key(value):
    try:
        return v32_player_name_key(value)
    except Exception:
        return re.sub(r"\s+", "", str(value or ""))


def _v60_safe_rate(num, den, a=1.0, b=2.0):
    return float((float(num) + a) / (float(den) + b)) if den >= 0 else 0.5


def v60_apply_lap_and_heat_learning(df, entries=None, meta=None, db_path=None):
    """
    グランドノートから序盤主導・位置維持・捌き・追込み・終盤失速を集計し、
    走路温度50℃以上は細分化した選手別高温適性と組み合わせて予測へ反映する。
    """
    out = df.copy()
    n = len(out)
    defaults = {
        "展開履歴件数": np.zeros(n, dtype=int),
        "初周主導指数": np.full(n, 0.5),
        "位置維持指数": np.full(n, 0.5),
        "捌き指数": np.full(n, 0.5),
        "追込み指数": np.full(n, 0.5),
        "終盤指数_実測": np.full(n, 0.5),
        "失速リスク": np.full(n, 0.5),
        "展開学習補正": np.zeros(n),
        "展開タイプ_実測": ["データ不足"] * n,
        "熱走路帯": [v60_heat_band((meta or {}).get("走路温度"))[0]] * n,
        "高温履歴件数": np.zeros(n, dtype=int),
        "50℃以上3着内率": np.full(n, np.nan),
        "熱走路適性": np.full(n, 0.5),
        "熱走路学習補正": np.zeros(n),
        "Ver60総合補正": np.zeros(n),
        "Ver60根拠": ["周回・高温履歴を確認中"] * n,
    }
    for c, v in defaults.items():
        out[c] = v
    if out.empty:
        return out

    db_path = db_path or globals().get("DB_PATH")
    current_date = str((meta or {}).get("開催日") or (meta or {}).get("日付") or "").strip()
    try:
        current_date = v47_normalize_required_date(current_date)
    except Exception:
        pass
    try:
        track_temp = float((meta or {}).get("走路温度") or 30.0)
    except Exception:
        track_temp = 30.0
    band, heat_level = v60_heat_band(track_temp)

    lap_all = pd.DataFrame()
    hist_all = pd.DataFrame()
    con = None
    try:
        if db_path and Path(str(db_path)).exists():
            con = sqlite3.connect(str(db_path))
            lap_all = pd.read_sql_query(
                "SELECT race_key, player_name, car_no, lap_label, lap_no, position FROM player_lap_history", con
            )
            hist_all = pd.read_sql_query(
                "SELECT player_name, race_date, race_no, rank, handicap, track_temp, car_no, st FROM v15_player_history_imports", con
            )
    except Exception:
        lap_all = pd.DataFrame(); hist_all = pd.DataFrame()
    finally:
        if con is not None:
            con.close()

    if not lap_all.empty:
        lap_all["name_key"] = lap_all["player_name"].map(_v60_name_key)
        lap_all["position"] = pd.to_numeric(lap_all["position"], errors="coerce")
        lap_all["lap_no"] = pd.to_numeric(lap_all.get("lap_no"), errors="coerce")
        if current_date:
            lap_all = v61_filter_lap_df(lap_all, "race_key")
    if not hist_all.empty:
        hist_all["name_key"] = hist_all["player_name"].map(_v60_name_key)
        hist_all["track_temp"] = pd.to_numeric(hist_all["track_temp"], errors="coerce")
        hist_all["rank"] = pd.to_numeric(hist_all["rank"], errors="coerce")
        if current_date:
            hist_all = v61_filter_history_df(hist_all, "race_date", "race_no", count_stats=False)

    for idx, row in out.iterrows():
        key = _v60_name_key(row.get("選手名"))
        p_laps = lap_all[lap_all["name_key"].eq(key)].copy() if not lap_all.empty else pd.DataFrame()
        race_count = 0
        lead_vals=[]; hold_vals=[]; pass_vals=[]; close_vals=[]; fade_vals=[]
        if not p_laps.empty:
            for race_key, g in p_laps.groupby("race_key"):
                g = g.dropna(subset=["position"]).copy()
                if g.empty: continue
                def pos_for(label):
                    z=g[g["lap_label"].astype(str).eq(label)]
                    return float(z.iloc[0]["position"]) if not z.empty else None
                first=pos_for("1周目")
                goal=pos_for("ゴール線")
                if goal is None:
                    goal=pos_for("ゴール")
                if first is None or goal is None: continue
                race_count += 1
                lead_vals.append(1.0 if first == 1 else max(0.0, 1.0-(first-1)/7.0))
                hold_vals.append(float(np.clip(1.0-abs(goal-first)/7.0,0,1)))
                pass_vals.append(float(np.clip((first-goal)/5.0+0.5,0,1)))
                close_vals.append(float(np.clip((first-goal)/7.0+0.5,0,1)))
                fade_vals.append(float(np.clip((goal-first)/5.0+0.5,0,1)))
        if race_count:
            lead=float(np.mean(lead_vals)); hold=float(np.mean(hold_vals)); passing=float(np.mean(pass_vals)); closing=float(np.mean(close_vals)); fade=float(np.mean(fade_vals))
        else:
            lead=hold=passing=closing=fade=0.5

        p_hist = hist_all[hist_all["name_key"].eq(key)].copy() if not hist_all.empty else pd.DataFrame()
        hot = p_hist[p_hist["track_temp"].ge(50) & p_hist["rank"].notna()] if not p_hist.empty else pd.DataFrame()
        normal = p_hist[p_hist["track_temp"].lt(50) & p_hist["rank"].notna()] if not p_hist.empty else pd.DataFrame()
        hot_n=len(hot)
        hot_top3=_v60_safe_rate((hot["rank"]<=3).sum(), hot_n, 2, 5) if hot_n else np.nan
        norm_top3=_v60_safe_rate((normal["rank"]<=3).sum(), len(normal), 2, 5) if len(normal) else 0.40
        hot_skill=0.5 if hot_n==0 else float(np.clip(0.5+(hot_top3-norm_top3)*1.25,0.05,0.95))

        # 展開補正は履歴数に応じて縮小。50℃以上では前残り・位置維持を強める。
        lap_conf=min(1.0, race_count/8.0)
        base_flow=((lead-0.5)*0.55 + (hold-0.5)*0.50 + (passing-0.5)*0.45 + (closing-0.5)*0.35 - (fade-0.5)*0.35) * lap_conf
        front_role=float(pd.to_numeric(pd.Series([row.get("初周先頭推定")]), errors="coerce").fillna(0.35).iloc[0])
        hot_flow=heat_level*((hot_skill-0.5)*1.10 + (hold-0.5)*0.45 + (front_role-0.5)*0.28)
        flow_bonus=float(np.clip(base_flow*1.10,-0.85,0.85))
        heat_bonus=float(np.clip(hot_flow,-0.75,0.90))
        total=float(np.clip(flow_bonus+heat_bonus,-1.20,1.35))

        if lead>=0.67 and hold>=0.62: typ="逃げ・粘り型"
        elif passing>=0.66 and closing>=0.62: typ="捌き・追込み型"
        elif fade>=0.64: typ="終盤失速注意"
        elif hold>=0.65: typ="位置維持型"
        elif race_count: typ="展開混合型"
        else: typ="データ不足"

        out.at[idx,"展開履歴件数"]=race_count
        out.at[idx,"初周主導指数"]=round(lead,3)
        out.at[idx,"位置維持指数"]=round(hold,3)
        out.at[idx,"捌き指数"]=round(passing,3)
        out.at[idx,"追込み指数"]=round(closing,3)
        out.at[idx,"終盤指数_実測"]=round(1.0-fade,3)
        out.at[idx,"失速リスク"]=round(fade,3)
        out.at[idx,"展開学習補正"]=round(flow_bonus,3)
        out.at[idx,"展開タイプ_実測"]=typ
        out.at[idx,"熱走路帯"]=band
        out.at[idx,"高温履歴件数"]=hot_n
        out.at[idx,"50℃以上3着内率"]=round(hot_top3,3) if hot_n else np.nan
        out.at[idx,"熱走路適性"]=round(hot_skill,3)
        out.at[idx,"熱走路学習補正"]=round(heat_bonus,3)
        out.at[idx,"Ver60総合補正"]=round(total,3)
        out.at[idx,"Ver60根拠"]=(f"周回{race_count}走: 主導{lead:.2f} 維持{hold:.2f} 捌き{passing:.2f} 失速{fade:.2f} / " f"{band}・50℃以上{hot_n}走・熱適性{hot_skill:.2f}")

    bonus=pd.to_numeric(out["Ver60総合補正"],errors="coerce").fillna(0.0)
    if "改善後総合点" in out.columns:
        out["改善後総合点"] = pd.to_numeric(out["改善後総合点"],errors="coerce").fillna(0.0)+bonus
        out["改善後順位"] = out["改善後総合点"].rank(method="min",ascending=False).astype(int)
    if "当日レース指数" in out.columns:
        out["当日レース指数"] = pd.to_numeric(out["当日レース指数"],errors="coerce").fillna(50.0)+bonus*0.42
    if "予測競走T" in out.columns:
        out["予測競走T"] = np.round(pd.to_numeric(out["予測競走T"],errors="coerce")-bonus*0.0009,4)
    # 既存シミュレーションが参照する列にも穏やかに注入。
    if "混戦突破適性" in out.columns:
        out["混戦突破適性"] = np.clip(pd.to_numeric(out["混戦突破適性"],errors="coerce").fillna(.5)+(out["捌き指数"]-.5)*.16,0.05,.95)
    if "ゴール前伸び指数" in out.columns:
        out["ゴール前伸び指数"] = np.clip(pd.to_numeric(out["ゴール前伸び指数"],errors="coerce").fillna(.5)+(out["終盤指数_実測"]-.5)*.18,0.05,.95)
    if "内枠残存率" in out.columns:
        out["内枠残存率"] = np.clip(pd.to_numeric(out["内枠残存率"],errors="coerce").fillna(.4)+(out["位置維持指数"]-.5)*.16*heat_level,0.05,.95)
    return out

def ver16_run_prediction(text, trials=10000, seed=20260719, manual_excluded=None):
    # 先にメタ情報を読み、履歴取得より前に学習境界を固定する。
    _meta_for_cutoff = v15_parse_race_meta(text)
    v61_set_learning_boundary(_meta_for_cutoff)
    content, meta, entries = ver16_build_virtual_excel(text, manual_excluded=manual_excluded)
    # 解析結果側の正規化済みメタで境界を再確定。
    v61_set_learning_boundary(meta)
    # run_model内でも当日出走表の補助指標を参照できるよう、一回の予測中だけ保持する。
    globals()["LATEST_ENTRY_STATS"] = entries.copy()
    globals()["LATEST_RACE_META"] = dict(meta)
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




# Ver37: 誤登録履歴の個別削除
def _v37_same_num(a, b, digits=3):
    try:
        if a is None or b is None:
            return a is None and b is None
        return round(float(a), digits) == round(float(b), digits)
    except Exception:
        return str(a or "").strip() == str(b or "").strip()


def _v37_import_matches_canonical(import_row, canonical_row, player_name):
    """正規履歴と詳細取込の同一走行を、空白表記差を吸収して照合する。"""
    d = dict(import_row)
    c = dict(canonical_row)
    if v32_player_name_key(d.get("player_name")) != v32_player_name_key(player_name):
        return False
    if _v33_norm_text(d.get("race_date")) != _v33_norm_text(c.get("race_date")):
        return False
    if _v33_norm_text(d.get("venue")) != _v33_norm_text(c.get("venue")):
        return False
    # 日付・場だけで削除すると同日別レースを巻き込むため、走行値を複数照合する。
    checks = [
        _v37_same_num(d.get("rank"), c.get("finish"), 0),
        _v37_same_num(d.get("trial_time"), c.get("trial_time"), 3),
        _v37_same_num(d.get("race_time"), c.get("race_time"), 3),
        _v37_same_num(d.get("st"), c.get("start_time"), 3),
    ]
    # 有効な値が3項目以上一致したものだけ連動削除する。
    available = 0
    matched = 0
    pairs = [
        (d.get("rank"), c.get("finish"), 0),
        (d.get("trial_time"), c.get("trial_time"), 3),
        (d.get("race_time"), c.get("race_time"), 3),
        (d.get("st"), c.get("start_time"), 3),
        (d.get("handicap"), str(c.get("handicap") or "").replace("m", ""), 0),
    ]
    for a, b, digits in pairs:
        if a is None or b is None or str(a).strip() in {"", "-"} or str(b).strip() in {"", "-"}:
            continue
        available += 1
        if _v37_same_num(a, b, digits):
            matched += 1
    return available >= 3 and matched == available


def v37_delete_race_history(history_id, db_path=DB_PATH):
    """正規履歴を1件削除し、同じ走行の詳細取込も連動削除する。"""
    mount_and_init_db()
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT h.*, p.player_name
            FROM race_history h JOIN players p ON p.player_id=h.player_id
            WHERE h.history_id=?
            """,
            (int(history_id),),
        ).fetchone()
        if row is None:
            return {"deleted": 0, "deleted_imports": 0, "message": "対象履歴が見つかりません。"}

        deleted_imports = 0
        if v15_table_exists(con, "v15_player_history_imports"):
            candidates = con.execute(
                "SELECT * FROM v15_player_history_imports WHERE race_date=? AND venue=?",
                (row["race_date"], row["venue"]),
            ).fetchall()
            for imp in candidates:
                if _v37_import_matches_canonical(imp, row, row["player_name"]):
                    con.execute(
                        "DELETE FROM v15_player_history_imports WHERE history_key=?",
                        (imp["history_key"],),
                    )
                    deleted_imports += 1

        con.execute("DELETE FROM race_history WHERE history_id=?", (int(history_id),))
        con.commit()
        label = f"{row['player_name']} {row['race_date']} {row['venue']} {row['race_no'] or ''}".strip()
        return {
            "deleted": 1,
            "deleted_imports": deleted_imports,
            "message": f"{label} を削除しました。条件詳細の対応データ {deleted_imports}件も削除しました。",
        }


def v37_delete_import_history(history_key, db_path=DB_PATH):
    """詳細取込だけに存在する履歴を1件削除する。"""
    mount_and_init_db()
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM v15_player_history_imports WHERE history_key=?",
            (str(history_key),),
        ).fetchone()
        if row is None:
            return {"deleted": 0, "message": "対象履歴が見つかりません。"}
        con.execute(
            "DELETE FROM v15_player_history_imports WHERE history_key=?",
            (str(history_key),),
        )
        con.commit()
        label = f"{row['player_name']} {row['race_date']} {row['venue']} {row['race_no'] or ''}R"
        return {"deleted": 1, "message": f"{label} の条件詳細データを削除しました。"}


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




def _v45_norm_race_no(value):
    """レース番号を比較用整数へ正規化。5R / 予選5R / 5 を同じ5として扱う。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    m = re.search(r"(\d{1,2})", str(value))
    return int(m.group(1)) if m else None


def _v45_identity_record_key(player_name, race_date, venue, race_no, fallback=None):
    """Ver45: 同一レースキー = 選手名 + 開催日 + 開催場 + R。

    Rが取得できない古いデータだけは、誤統合を避けるためfallbackを追加する。
    """
    rn = _v45_norm_race_no(race_no)
    base = ["v45", v32_player_name_key(player_name), str(race_date or "").strip(), str(venue or "").strip(), rn]
    if rn is None:
        base.append(fallback or "race_no_missing")
    return _v27_record_key(*base)




def _v48_num(value):
    """重複判定用の数値化。空欄・0以下は比較対象外。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, str):
        s = value.strip().replace("m", "")
        if s in {"", "-", "—", "–", "―", "None", "nan"}:
            return None
        value = s
    try:
        n = float(value)
        return n if n > 0 else None
    except Exception:
        return None


def _v48_numeric_identity_match(existing, incoming):
    """Rが無い履歴を、同日・同場の数値一致で同一走行か判定する。

    2項目以上が一致し、比較できた項目に明確な不一致がない場合のみ同一扱い。
    タイム類は小数丸め差を吸収する。
    """
    specs = (
        ("finish", "finish", 0.0),
        ("starters", "starters", 0.0),
        ("handicap", "handicap", 0.0),
        ("trial_time", "trial_time", 0.0015),
        ("race_time", "race_time", 0.0015),
        ("start_time", "start_time", 0.0015),
    )
    matched = 0
    compared = 0
    conflicts = 0
    for old_key, new_key, tol in specs:
        old = _v48_num(existing[old_key] if old_key in existing.keys() else None)
        new = _v48_num(incoming.get(new_key))
        if old is None or new is None:
            continue
        compared += 1
        if abs(old - new) <= tol:
            matched += 1
        else:
            conflicts += 1
    return compared >= 2 and matched >= 2 and conflicts == 0


def _v48_find_same_race_without_r(candidates, incoming):
    """R欠損時に数値一致する既存履歴を1件だけ返す。曖昧なら統合しない。"""
    matches = [r for r in candidates if _v48_numeric_identity_match(r, incoming)]
    return matches[0] if len(matches) == 1 else None

def _v45_merge_history_rows(con, rows, preferred_id=None):
    """同一レースの複数行を、情報があるカラム単位で1行へ統合する。"""
    rows = list(rows or [])
    if not rows:
        return None
    # 情報量が多い行、または指定行を残す。
    def score(r):
        fields = ("race_no", "race_name", "finish", "starters", "surface", "handicap", "trial_time", "race_time", "start_time")
        return sum(_v34_has_value(r[f], positive=f in {"race_no","finish","starters","trial_time","race_time","start_time"}) for f in fields)
    keep = next((r for r in rows if preferred_id is not None and r["history_id"] == preferred_id), None)
    if keep is None:
        keep = max(rows, key=score)
    merged = dict(keep)
    for r in rows:
        if r["history_id"] == keep["history_id"]:
            continue
        for f in ("race_no", "race_name", "finish", "starters", "surface", "handicap", "trial_time", "race_time", "start_time"):
            positive = f in {"race_no", "finish", "starters", "trial_time", "race_time", "start_time"}
            merged[f] = _v34_merge(merged.get(f), r[f], positive=positive)
        # 異常情報は通常より優先して保持する。
        if r["result_status"] and r["result_status"] != "通常":
            merged["result_status"] = r["result_status"]
            merged["use_for_model"] = 0
        elif merged.get("use_for_model") is None:
            merged["use_for_model"] = r["use_for_model"]
    for r in rows:
        if r["history_id"] != keep["history_id"]:
            con.execute("DELETE FROM race_history WHERE history_id=?", (r["history_id"],))
    return keep, merged


def _v34_has_value(value, *, positive=False):
    """重複更新用。空欄・NaNは既存値を消さない。時刻系は0以下も空欄扱い。"""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, str):
        if not value.strip() or value.strip().lower() in {"nan", "none", "null", "-"}:
            return False
    if positive:
        try:
            return float(value) > 0
        except Exception:
            return False
    return True


def _v34_merge(old, new, *, positive=False):
    return new if _v34_has_value(new, positive=positive) else old


def v15_save_player_history(df, db_path=DB_PATH):
    """貼付履歴を保存。同一レース／同一record_keyは、値があるカラムだけ更新する。"""
    if df is None or df.empty:
        return 0, 0
    mount_and_init_db()
    v151_ensure_player_import_columns(db_path)
    # Ver38: 正規履歴にもレース名を独立保存する。既存DBは自動移行。
    with sqlite3.connect(str(db_path)) as _con:
        _cols = set(v15_columns(_con, "race_history"))
        if "race_name" not in _cols:
            _con.execute("ALTER TABLE race_history ADD COLUMN race_name TEXT")
        _con.commit()
    # Ver46: 旧版でレース名違いにより分かれた同一レースを先に統合。
    v46_cleanup_player_identity_duplicates(db_path)
    changed = 0
    skipped = 0
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
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
            race_no_value = None if pd.isna(row.get("レース")) else int(row.get("レース"))
            race_name = row.get("レース名")
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
            raw_history_text = str(row.get("_raw") or "")
            explicit_status = str(row.get("異常") or "")
            status_source = f"{raw_history_text} {explicit_status}"
            invalid_markers = (
                "欠責", "周誤", "欠車", "出走取消", "出走取り消し", "不出走",
                "競走中止", "落車", "反則", "不成立", "失格", "除外", "参加解除"
            )
            marker = next((m for m in invalid_markers if m in status_source), "")

            # サイトの簡易表では、取消・欠車が「— / — / — / —」だけで表されることがある。
            # 着順・走路・試走・競走・STがすべて無い行は、通常の欠損ではなく
            # 「出走取消等」として保持し、予測・条件適性・平均計算から除外する。
            surface_missing = surface is None or str(surface).strip() in {"", "-", "—", "–", "―"}
            cancel_like_missing = (
                finish is None and surface_missing
                and (trial is None or trial <= 0)
                and (race_time is None or race_time <= 0)
                and (st is None or st <= 0)
            )
            numeric_invalid = (
                finish is None or finish < 1
                or trial is None or trial <= 0
                or race_time is None or race_time <= 0
                or race_time <= trial
                or st is None or st <= 0
            )
            use_for_model = 0 if marker or cancel_like_missing or numeric_invalid else 1
            if marker:
                result_status = marker
            elif cancel_like_missing:
                result_status = "出走取消等"
            elif numeric_invalid:
                result_status = "無効タイム"
            else:
                result_status = "通常"

            # Ver45: 重複判定は「選手名＋開催日＋開催場＋R」だけに統一。
            # グレード・種別・車番・ハンデ・タイム差は、別レース判定には使わない。
            candidates = con.execute(
                "SELECT * FROM race_history WHERE player_id=? AND race_date=? AND venue=? ORDER BY history_id",
                (player_id, race_date, venue),
            ).fetchall()
            incoming_identity = {
                "finish": finish, "starters": starters, "handicap": handicap_num,
                "trial_time": trial, "race_time": race_time, "start_time": st,
            }
            if race_no_value is not None:
                # Rがある場合は従来どおり、選手＋日付＋場＋Rで確定。
                same_race = [r for r in candidates if _v45_norm_race_no(r["race_no"]) == _v45_norm_race_no(race_no_value)]
                # Ver56: Rが入力された場合は、そのRだけで既存履歴を特定する。
                # 候補にないRなら新しいレースとして登録し、数値一致では上書きしない。
            else:
                # Ver56: Rなし行は、保存前の候補判定で必要なら保留へ回す。
                # 数値一致による自動統合は行わない。
                same_race = []
            target = same_race[0] if same_race else None

            candidate_row = {
                "race_date": race_date, "venue": venue, "finish": finish,
                "handicap": handicap_text, "trial_time": trial,
                "race_time": race_time, "start_time": st,
            }
            signature = _v33_history_signature(candidate_row)
            incoming_record_key = _v45_identity_record_key(name, race_date, venue, race_no_value, fallback=signature)

            # 旧record_keyで同一レースが複数行ある場合は、登録前に1行へ統合。
            if len(same_race) > 1:
                keep, merged_existing = _v45_merge_history_rows(con, same_race, preferred_id=target["history_id"])
                con.execute(
                    """UPDATE race_history SET race_no=?, race_name=?, finish=?, starters=?, surface=?, handicap=?,
                       trial_time=?, race_time=?, start_time=?, result_status=?, use_for_model=? WHERE history_id=?""",
                    (merged_existing.get("race_no"), merged_existing.get("race_name"), merged_existing.get("finish"),
                     merged_existing.get("starters"), merged_existing.get("surface"), merged_existing.get("handicap"),
                     merged_existing.get("trial_time"), merged_existing.get("race_time"), merged_existing.get("start_time"),
                     merged_existing.get("result_status") or "通常", int(merged_existing.get("use_for_model") or 0), keep["history_id"]),
                )
                target = con.execute("SELECT * FROM race_history WHERE history_id=?", (keep["history_id"],)).fetchone()

            if target is None:
                target = con.execute(
                    "SELECT * FROM race_history WHERE record_key=? LIMIT 1",
                    (incoming_record_key,),
                ).fetchone()

            if target is None:
                record_key = incoming_record_key
                con.execute(
                    """INSERT INTO race_history(
                        player_id, race_date, venue, race_no, race_name, finish, starters, surface,
                        handicap, trial_time, race_time, start_time, result_status,
                        use_for_model, source, record_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'スマホ貼付登録', ?, CURRENT_TIMESTAMP)""",
                    (player_id, race_date, venue, race_no_value, race_name, finish, starters, surface,
                     handicap_text, trial, race_time, st, result_status, use_for_model, record_key),
                )
                changed += 1
            else:
                merged = {
                    "race_no": _v34_merge(target["race_no"], race_no_value, positive=True),
                    "race_name": _v34_merge(target["race_name"], race_name),
                    "finish": _v34_merge(target["finish"], finish, positive=True),
                    "starters": _v34_merge(target["starters"], starters, positive=True),
                    "surface": _v34_merge(target["surface"], surface),
                    "handicap": _v34_merge(target["handicap"], handicap_text),
                    "trial_time": _v34_merge(target["trial_time"], trial, positive=True),
                    "race_time": _v34_merge(target["race_time"], race_time, positive=True),
                    "start_time": _v34_merge(target["start_time"], st, positive=True),
                }
                # 事故区分が明示されたときは更新。通常入力の欠損だけでは既存の事故区分を消さない。
                if marker:
                    merged_status = marker
                    merged_use = 0
                elif result_status == "通常" and all(_v34_has_value(merged[k], positive=True) for k in ("finish", "trial_time", "race_time", "start_time")):
                    merged_status = "通常"
                    merged_use = 1 if float(merged["race_time"]) > float(merged["trial_time"]) else 0
                else:
                    merged_status = target["result_status"]
                    merged_use = target["use_for_model"]
                candidate_row = {
                    "race_date": race_date, "venue": venue, "finish": merged["finish"],
                    "handicap": merged["handicap"], "trial_time": merged["trial_time"],
                    "race_time": merged["race_time"], "start_time": merged["start_time"],
                }
                signature = _v33_history_signature(candidate_row)
                record_key = _v45_identity_record_key(name, race_date, venue, merged["race_no"], fallback=signature)

                # カラム更新によって別行と同じrecord_keyになる場合も、完全置換せず
                # 既存の同一走行行へ統合する。古い重複行だけ削除する。
                conflict = con.execute(
                    "SELECT * FROM race_history WHERE record_key=? AND history_id<>? LIMIT 1",
                    (record_key, target["history_id"]),
                ).fetchone()
                delete_after_update = None
                if conflict is not None:
                    merged = {
                        "race_no": _v34_merge(conflict["race_no"], merged["race_no"], positive=True),
                        "race_name": _v34_merge(conflict["race_name"], merged["race_name"]),
                        "finish": _v34_merge(conflict["finish"], merged["finish"], positive=True),
                        "starters": _v34_merge(conflict["starters"], merged["starters"], positive=True),
                        "surface": _v34_merge(conflict["surface"], merged["surface"]),
                        "handicap": _v34_merge(conflict["handicap"], merged["handicap"]),
                        "trial_time": _v34_merge(conflict["trial_time"], merged["trial_time"], positive=True),
                        "race_time": _v34_merge(conflict["race_time"], merged["race_time"], positive=True),
                        "start_time": _v34_merge(conflict["start_time"], merged["start_time"], positive=True),
                    }
                    if not marker:
                        merged_status = conflict["result_status"] if conflict["result_status"] else merged_status
                        merged_use = conflict["use_for_model"] if conflict["use_for_model"] is not None else merged_use
                    delete_after_update = target["history_id"]
                    target = conflict

                con.execute(
                    """UPDATE race_history SET race_no=?, race_name=?, finish=?, starters=?, surface=?, handicap=?,
                       trial_time=?, race_time=?, start_time=?, result_status=?, use_for_model=?,
                       source='スマホ貼付登録', record_key=? WHERE history_id=?""",
                    (merged["race_no"], merged["race_name"], merged["finish"], merged["starters"], merged["surface"],
                     merged["handicap"], merged["trial_time"], merged["race_time"], merged["start_time"],
                     merged_status, merged_use, record_key, target["history_id"]),
                )
                if delete_after_update is not None:
                    con.execute("DELETE FROM race_history WHERE history_id=?", (delete_after_update,))
                changed += 1

            detail_values = {
                "player_name": name, "race_date": race_date, "venue": venue,
                "race_no": race_no_value, "race_name": race_name, "rank": None if finish is None else int(finish),
                "starters": None if starters is None else int(starters), "surface": surface,
                "handicap": handicap_num, "trial_time": trial, "race_time": race_time, "st": st,
                "raw_line": row.get("_raw"), "weather": row.get("天候"),
                "track_temp": None if pd.isna(row.get("走路温度")) else float(row.get("走路温度")),
                "air_temp": None if pd.isna(row.get("気温")) else float(row.get("気温")),
                "humidity": None if pd.isna(row.get("湿度")) else float(row.get("湿度")),
                "race_type": race_type,
                "distance": None if pd.isna(row.get("距離")) else int(row.get("距離")),
                "laps": None if pd.isna(row.get("周回数")) else int(row.get("周回数")),
                "popularity": None if pd.isna(row.get("人気")) else int(row.get("人気")),
                "car_no": car_no,
            }
            detail_candidates = con.execute(
                "SELECT * FROM v15_player_history_imports WHERE race_date=? AND venue=?",
                (race_date, venue),
            ).fetchall()
            detail_target = None
            for d in detail_candidates:
                if v32_player_name_key(d["player_name"]) != v32_player_name_key(name):
                    continue
                if race_no_value is not None:
                    # Ver56: 詳細履歴もRが一致する行だけ更新する。
                    if d["race_no"] is None or int(d["race_no"]) != race_no_value:
                        continue
                else:
                    # Rなしは、同じ正規化レース名の既存行が1件だけの場合に限る。
                    if _v55_race_name_key(d["race_name"]) != _v55_race_name_key(race_name):
                        continue
                if car_no is not None and d["car_no"] is not None and int(d["car_no"]) != car_no:
                    continue
                detail_target = d
                break

            if detail_target is None:
                history_key = v15_hash(v32_player_name_key(name), race_date, venue, race_no_value, finish, handicap_num, trial, race_time, st)
                # 数値同定で候補を見つけられなくても、同じhistory_keyが既に存在する場合がある。
                # UNIQUE違反にせず、その既存行へカラム単位で追記・更新する。
                detail_target = con.execute(
                    "SELECT * FROM v15_player_history_imports WHERE history_key=? LIMIT 1",
                    (history_key,),
                ).fetchone()
                if detail_target is None:
                    con.execute("""
                        INSERT INTO v15_player_history_imports (
                            history_key, player_name, race_date, venue, race_no, race_name, rank, starters, surface,
                            handicap, trial_time, race_time, st, raw_line, created_at, weather, track_temp,
                            air_temp, humidity, race_type, distance, laps, popularity, car_no
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (history_key, name, race_date, venue, race_no_value, race_name, detail_values["rank"], detail_values["starters"],
                           surface, handicap_num, trial, race_time, st, detail_values["raw_line"], now,
                           detail_values["weather"], detail_values["track_temp"], detail_values["air_temp"],
                           detail_values["humidity"], race_type, detail_values["distance"], detail_values["laps"],
                           detail_values["popularity"], car_no))

            if detail_target is not None:
                fields = ["player_name","race_no","race_name","rank","starters","surface","handicap","trial_time",
                          "race_time","st","raw_line","weather","track_temp","air_temp","humidity",
                          "race_type","distance","laps","popularity","car_no"]
                positive_fields = {"rank","starters","handicap","trial_time","race_time","st","track_temp",
                                   "air_temp","humidity","distance","laps","popularity","car_no"}
                merged_detail = {
                    f: _v34_merge(detail_target[f], detail_values[f], positive=f in positive_fields)
                    for f in fields
                }
                con.execute("""UPDATE v15_player_history_imports SET
                    player_name=?, race_no=?, race_name=?, rank=?, starters=?, surface=?, handicap=?, trial_time=?,
                    race_time=?, st=?, raw_line=?, weather=?, track_temp=?, air_temp=?, humidity=?,
                    race_type=?, distance=?, laps=?, popularity=?, car_no=? WHERE history_key=?""",
                    tuple(merged_detail[f] for f in fields) + (detail_target["history_key"],))
        con.commit()
    return changed, skipped


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

    surface_match = re.search(r"(良走路|湿走路|斑走路|風走路|荒走路)\s*/\s*(-?\d+(?:\.\d+)?)℃", text)
    if surface_match:
        meta["走路状態"] = surface_match.group(1)
        meta["走路温度"] = float(surface_match.group(2))
    else:
        sm = re.search(r"(良走路|湿走路|斑走路|風走路|荒走路)", text)
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


# ============================================================
# v3.6 結果登録時の選手履歴自動更新・調整履歴
# ============================================================
def _ensure_sqlite_columns(con, table_name, columns):
    """古いDBを壊さず、足りない列だけ追加する。"""
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")


def v36_init_history_tables(db_path=DB_PATH):
    mount_and_init_db()
    with sqlite3.connect(str(db_path)) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS player_lap_history (
            race_key TEXT NOT NULL,
            player_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            car_no INTEGER NOT NULL,
            lap_label TEXT NOT NULL,
            lap_no INTEGER,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (race_key, car_no, lap_label),
            FOREIGN KEY(player_id) REFERENCES players(player_id)
        );
        CREATE TABLE IF NOT EXISTS adjustment_log (
            adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            coefficient_changed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(version, category, description)
        );
        """)
        # CREATE TABLE IF NOT EXISTS だけでは古いDBへ新列が増えないため、起動時に安全移行する。
        _ensure_sqlite_columns(con, "adjustment_log", {
            "version": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "coefficient_changed": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT"
        })
        rows = [
            ("v3.6", "予測対象", "未登録1～8などの補助選手を、点数の正規化と6周シミュレーションの前に除外", 0),
            ("v3.6", "履歴更新", "公式結果登録時にrace_historyと詳細履歴へ各選手の結果を自動追加", 0),
            ("v3.6", "周回履歴", "グランドノートを選手別の周回順位履歴として保存", 0),
            ("v3.6", "透明性", "登録件数・重複スキップ件数と調整履歴を画面表示", 0),
            ("v3.6", "予測係数", "Ver15.2の予測係数・重みは変更していない", 0),
        ]
        con.executemany("""INSERT OR IGNORE INTO adjustment_log
            (version,category,description,coefficient_changed) VALUES(?,?,?,?)""", rows)
        con.commit()


def _v36_surface_short(value):
    v = str(value or "").strip()
    return {"良走路":"良", "湿走路":"湿", "斑走路":"斑", "風走路":"風", "荒走路":"荒"}.get(v, v)


def v36_update_player_histories(meta, results, laps=None, db_path=DB_PATH):
    """結果表を学習用履歴へ同期する。再登録は論理キーで重複させない。"""
    v36_init_history_tables(db_path)
    if results is None or results.empty:
        return {"履歴追加": 0, "履歴重複スキップ": 0, "周回履歴保存": 0}
    starters = int(len(results))
    rows=[]
    for _, r in results.iterrows():
        rows.append({
            "選手名": str(r.get("選手名", "")).strip(),
            "開催日": meta.get("開催日"),
            "開催場": meta.get("開催場"),
            "レース種別": meta.get("レース種別") or f"{meta.get('レース','')}R",
            "着順": r.get("着順"),
            "出走": starters,
            "天候": meta.get("天候", ""),
            "走路": _v36_surface_short(meta.get("走路状態")),
            "走路温度": meta.get("走路温度"),
            "気温": meta.get("気温"),
            "湿度": meta.get("湿度"),
            "車番": r.get("車番"),
            "ハンデ": r.get("ハンデ"),
            "距離": meta.get("距離", 3100),
            "周回数": meta.get("周回数", 6),
            "人気": r.get("人気"),
            "競走T": r.get("競走T"),
            "試走T": r.get("試走T"),
            "ST": r.get("ST"),
        })
    inserted, skipped = v15_save_player_history(pd.DataFrame(rows), db_path)

    key = v34_race_key(meta)
    lap_saved = 0
    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        # 同一レースの再登録は周回部分だけ置換する。
        con.execute("DELETE FROM player_lap_history WHERE race_key=?", (key,))
        if isinstance(laps, pd.DataFrame) and not laps.empty:
            name_by_car = {int(r["車番"]): str(r["選手名"]).strip() for _,r in results.iterrows()}
            for _, lr in laps.iterrows():
                car = int(lr["車番"])
                name = name_by_car.get(car, "")
                found = _v32_find_player(con, name)
                if not found:
                    continue
                player_id, canonical = found
                con.execute("""INSERT OR REPLACE INTO player_lap_history
                    (race_key,player_id,player_name,car_no,lap_label,lap_no,position,created_at)
                    VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (key, player_id, canonical, car, str(lr["周回"]), int(lr["周回番号"]), int(lr["順位"])))
                lap_saved += 1
        con.commit()
    return {"履歴追加": int(inserted), "履歴重複スキップ": int(skipped), "周回履歴保存": int(lap_saved)}


def v36_get_adjustment_log(db_path=DB_PATH):
    v36_init_history_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        return pd.read_sql_query("""SELECT version AS バージョン, category AS 分類,
            description AS 調整内容,
            CASE coefficient_changed WHEN 1 THEN '変更あり' ELSE '変更なし' END AS 係数変更
            FROM adjustment_log ORDER BY adjustment_id DESC""", con)


def v36_save_result_and_analyze(meta, results, laps=None, payouts=None, db_path=DB_PATH):
    key, comparison, analysis = v35_save_result_and_analyze(meta, results, laps, payouts, db_path)
    history_summary = v36_update_player_histories(meta, results, laps, db_path)
    analysis = dict(analysis)
    analysis.update(history_summary)
    # 予測比較が存在する場合は拡張後の解析内容も保存する。
    if "message" not in analysis:
        with sqlite3.connect(str(db_path)) as con:
            con.execute("UPDATE prediction_feedback SET analysis_json=? WHERE race_key=?",
                        (json.dumps(analysis, ensure_ascii=False), key))
            con.commit()
    return key, comparison, analysis


# ============================================================
# v3.7 風走路・縦型履歴対応
# ============================================================
def v37_init_adjustment_log(db_path=DB_PATH):
    v36_init_history_tables(db_path)
    rows = [
        ("v3.7", "走路解析", "縦型選手履歴の走路『風』『風走路』を独立条件として保存", 0),
        ("v3.7", "ハンデ解析", "『1番-m』などの-m表記を0mとして登録", 0),
        ("v3.7", "走路補正", "風走路は良走路に近い別条件として弱く共有し、湿走路とはほぼ分離", 1),
    ]
    with sqlite3.connect(str(db_path)) as con:
        con.executemany("""INSERT OR IGNORE INTO adjustment_log
            (version,category,description,coefficient_changed) VALUES(?,?,?,?)""", rows)
        con.commit()


# 調整履歴表示時にv3.7の記録も初期化する
_v36_get_adjustment_log_original = v36_get_adjustment_log
def v36_get_adjustment_log(db_path=DB_PATH):
    v37_init_adjustment_log(db_path)
    with sqlite3.connect(str(db_path)) as con:
        return pd.read_sql_query("""SELECT version AS バージョン, category AS 分類,
            description AS 調整内容,
            CASE coefficient_changed WHEN 1 THEN '変更あり' ELSE '変更なし' END AS 係数変更
            FROM adjustment_log ORDER BY adjustment_id DESC""", con)


# ============================================================
# v3.9 透明な重み調整・調整前後比較
# ============================================================
V39_FEATURES = {
    "試走": "試走換算",
    "ST": "平均ST",
    "ハンデ": "ハンデ",
    "近況": "近況信頼度",
    "走路適性": "走路点",
}
V39_DEFAULT_WEIGHTS = {"試走": 0.28, "ST": 0.18, "ハンデ": 0.12, "近況": 0.22, "走路適性": 0.20}


def v39_init_learning_tables(db_path=DB_PATH):
    with sqlite3.connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS adaptive_weights (
            feature_name TEXT PRIMARY KEY,
            current_weight REAL NOT NULL,
            initial_weight REAL NOT NULL,
            updated_at TEXT,
            update_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS prediction_feature_snapshots (
            race_key TEXT NOT NULL,
            car_no INTEGER NOT NULL,
            player_name TEXT,
            trial_feature REAL,
            st_feature REAL,
            handicap_feature REAL,
            form_feature REAL,
            surface_feature REAL,
            before_score REAL,
            after_score REAL,
            before_rank INTEGER,
            after_rank INTEGER,
            PRIMARY KEY(race_key, car_no)
        );
        CREATE TABLE IF NOT EXISTS weight_adjustment_history (
            adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_key TEXT NOT NULL,
            adjusted_at TEXT NOT NULL,
            feature_name TEXT NOT NULL,
            before_weight REAL NOT NULL,
            after_weight REAL NOT NULL,
            delta REAL NOT NULL,
            evidence_score REAL,
            reason TEXT,
            before_top3 TEXT,
            after_top3 TEXT,
            actual_top3 TEXT,
            diagnostic_exact_before INTEGER,
            diagnostic_exact_after INTEGER
        );
        """)
        now=datetime.now().isoformat(timespec="seconds")
        for name, weight in V39_DEFAULT_WEIGHTS.items():
            con.execute("""INSERT OR IGNORE INTO adaptive_weights
                (feature_name,current_weight,initial_weight,updated_at,update_count)
                VALUES(?,?,?,?,0)""", (name,weight,weight,now))
        con.commit()


def v39_get_weights(db_path=DB_PATH):
    v39_init_learning_tables(db_path)
    with sqlite3.connect(db_path) as con:
        rows=con.execute("SELECT feature_name,current_weight FROM adaptive_weights").fetchall()
    values={str(k):float(v) for k,v in rows}
    total=sum(max(0.0, values.get(k,0.0)) for k in V39_DEFAULT_WEIGHTS)
    if total <= 0: return dict(V39_DEFAULT_WEIGHTS)
    return {k:max(0.0,values.get(k,V39_DEFAULT_WEIGHTS[k]))/total for k in V39_DEFAULT_WEIGHTS}


def _v39_rank_feature(series, lower_better=False):
    x=pd.to_numeric(series,errors="coerce")
    fill=x.median() if x.notna().any() else 0.0
    x=x.fillna(fill)
    pct=x.rank(method="average",pct=True)
    return (1.0-pct if lower_better else pct).clip(0,1)


def v39_feature_frame(df):
    out=pd.DataFrame(index=df.index)
    out["試走"]=_v39_rank_feature(df.get("試走換算",pd.Series(index=df.index,dtype=float)),True)
    out["ST"]=_v39_rank_feature(df.get("平均ST",pd.Series(index=df.index,dtype=float)),True)
    # ハンデは数値が小さいほど前。能力評価とは別に展開上の有利さとして扱う。
    out["ハンデ"]=_v39_rank_feature(df.get("ハンデ",pd.Series(index=df.index,dtype=float)),True)
    out["近況"]=_v39_rank_feature(df.get("近況信頼度",pd.Series(.5,index=df.index)),False)
    out["走路適性"]=_v39_rank_feature(df.get("走路点",pd.Series(.5,index=df.index)),False)
    return out.fillna(.5)


def v39_apply_adaptive_weights(df, db_path=DB_PATH):
    df=df.copy()
    weights=v39_get_weights(db_path)
    features=v39_feature_frame(df)
    base=pd.to_numeric(df.get("改善後総合点",pd.Series(0,index=df.index)),errors="coerce").fillna(0.0)
    df["調整前総合点"]=base
    df["調整前順位"]=base.rank(method="min",ascending=False).astype(int)
    centered=sum(weights[k]*(features[k]-.5) for k in V39_DEFAULT_WEIGHTS)
    # 最大でも概ね±2点。元モデルを壊さず、蓄積データで少しずつ方向修正する。
    bonus=(centered*4.0).clip(-2.0,2.0)
    df["学習重み補正"]=bonus
    df["改善後総合点"]=base+bonus
    df["改善後順位"]=df["改善後総合点"].rank(method="min",ascending=False).astype(int)
    for k in V39_DEFAULT_WEIGHTS:
        df[f"学習特徴_{k}"]=features[k]
    return df


def v39_save_prediction_features(meta, df, db_path=DB_PATH):
    v39_init_learning_tables(db_path)
    key=v34_race_key(meta)
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM prediction_feature_snapshots WHERE race_key=?",(key,))
        for _,r in df.iterrows():
            con.execute("""INSERT INTO prediction_feature_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(
                key,int(r.get("車",r.get("車番"))),str(r.get("選手名","")),
                float(r.get("学習特徴_試走",.5)),float(r.get("学習特徴_ST",.5)),
                float(r.get("学習特徴_ハンデ",.5)),float(r.get("学習特徴_近況",.5)),
                float(r.get("学習特徴_走路適性",.5)),float(r.get("調整前総合点",0)),
                float(r.get("改善後総合点",0)),int(r.get("調整前順位",0)),int(r.get("改善後順位",0))
            ))
        con.commit()
    return key


def _v39_spearman(a,b):
    a=pd.Series(a,dtype=float); b=pd.Series(b,dtype=float)
    if len(a)<3 or a.nunique()<2 or b.nunique()<2: return 0.0
    val=a.rank().corr(b.rank())
    return 0.0 if pd.isna(val) else float(val)


def v39_adjust_weights_after_result(meta, results, db_path=DB_PATH):
    """結果を使って重みを微調整する。変更幅は1項目最大0.005、同レース再評価は診断専用。"""
    v39_init_learning_tables(db_path)
    key=v34_race_key(meta)
    with sqlite3.connect(db_path) as con:
        snap=pd.read_sql_query("SELECT * FROM prediction_feature_snapshots WHERE race_key=?",con,params=(key,))
    if snap.empty:
        return {"message":"特徴スナップショットがないため、重みは変更していません。次回予測から記録されます。"}
    merged=snap.merge(results[["車番","着順"]],left_on="car_no",right_on="車番",how="inner")
    if len(merged)<3:
        return {"message":"比較可能な選手が3人未満のため、重みは変更していません。"}
    performance=(len(merged)+1)-pd.to_numeric(merged["着順"],errors="coerce")
    colmap={"試走":"trial_feature","ST":"st_feature","ハンデ":"handicap_feature","近況":"form_feature","走路適性":"surface_feature"}
    evidence={k:_v39_spearman(merged[c],performance) for k,c in colmap.items()}
    before=v39_get_weights(db_path)
    # 正の一致度を目標配分へ。負の相関でも即ゼロにはせず下限を残す。
    strength={k:max(.05,(evidence[k]+1.0)/2.0) for k in evidence}
    stotal=sum(strength.values()); target={k:strength[k]/stotal for k in strength}
    raw={k:before[k]+max(-.005,min(.005,(target[k]-before[k])*.08)) for k in before}
    # 0.05～0.45に制限して正規化。
    raw={k:min(.45,max(.05,v)) for k,v in raw.items()}; total=sum(raw.values()); after={k:v/total for k,v in raw.items()}
    before_top3="→".join(map(str,merged.sort_values("before_rank")["car_no"].head(3).astype(int)))
    # 同じレースへ新重みを当てた診断順位。実績には含めない。
    diagnostic=sum(after[k]*merged[colmap[k]] for k in after)
    diag_df=merged.assign(diag_score=diagnostic)
    after_top3="→".join(map(str,diag_df.sort_values("diag_score",ascending=False)["car_no"].head(3).astype(int)))
    actual_top3="→".join(map(str,merged.sort_values("着順")["car_no"].head(3).astype(int)))
    now=datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        for k in before:
            delta=after[k]-before[k]
            reason=f"今回結果との順位相関 {evidence[k]:+.3f}。1回の変更幅を±0.005以内に制限。"
            con.execute("""UPDATE adaptive_weights SET current_weight=?,updated_at=?,update_count=update_count+1 WHERE feature_name=?""",(after[k],now,k))
            con.execute("""INSERT INTO weight_adjustment_history
                (race_key,adjusted_at,feature_name,before_weight,after_weight,delta,evidence_score,reason,
                 before_top3,after_top3,actual_top3,diagnostic_exact_before,diagnostic_exact_after)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                key,now,k,before[k],after[k],delta,evidence[k],reason,before_top3,after_top3,actual_top3,
                int(before_top3==actual_top3),int(after_top3==actual_top3)))
        con.commit()
    return {"race_key":key,"before":before,"after":after,"evidence":evidence,
            "before_top3":before_top3,"after_top3":after_top3,"actual_top3":actual_top3,
            "diagnostic_exact_before":before_top3==actual_top3,"diagnostic_exact_after":after_top3==actual_top3,
            "note":"同じレースへの調整後比較は答えを見た後の診断で、正式な的中実績には加算しません。"}


def v39_weight_history(db_path=DB_PATH, limit=100):
    v39_init_learning_tables(db_path)
    with sqlite3.connect(db_path) as con:
        return pd.read_sql_query("""SELECT adjusted_at AS 調整日時,race_key AS レース,
            feature_name AS 項目,before_weight AS 調整前,after_weight AS 調整後,
            delta AS 変化,evidence_score AS 結果との相関,reason AS 理由,
            before_top3 AS 調整前予測,after_top3 AS 調整後診断,actual_top3 AS 実結果
            FROM weight_adjustment_history ORDER BY adjustment_id DESC LIMIT ?""",con,params=(int(limit),))


def v39_current_weights(db_path=DB_PATH):
    w=v39_get_weights(db_path)
    return pd.DataFrame([{"項目":k,"現在の重み":v,"初期値":V39_DEFAULT_WEIGHTS[k],"初期値からの差":v-V39_DEFAULT_WEIGHTS[k]} for k,v in w.items()])


def v39_rollback_last_adjustment(db_path=DB_PATH):
    v39_init_learning_tables(db_path)
    with sqlite3.connect(db_path) as con:
        row=con.execute("SELECT race_key,adjusted_at FROM weight_adjustment_history ORDER BY adjustment_id DESC LIMIT 1").fetchone()
        if not row: return False,"戻せる調整履歴がありません。"
        race_key,adjusted_at=row
        rows=con.execute("SELECT feature_name,before_weight FROM weight_adjustment_history WHERE race_key=? AND adjusted_at=?",(race_key,adjusted_at)).fetchall()
        now=datetime.now().isoformat(timespec="seconds")
        for name,weight in rows:
            con.execute("UPDATE adaptive_weights SET current_weight=?,updated_at=? WHERE feature_name=?",(weight,now,name))
        con.execute("DELETE FROM weight_adjustment_history WHERE race_key=? AND adjusted_at=?",(race_key,adjusted_at))
        con.commit()
    return True,f"{race_key} の直前調整を元に戻しました。"

# ============================================================
# v4.0 10要素評価・前残り/追い込み/安定性/場適性/相手耐性
# ============================================================
V40_DEFAULT_WEIGHTS = {
    "試走": 0.18,
    "ST": 0.10,
    "ハンデ": 0.10,
    "近況": 0.14,
    "走路適性": 0.10,
    "前残り": 0.10,
    "追い込み": 0.08,
    "周回安定": 0.07,
    "コース適性": 0.07,
    "相手耐性": 0.06,
}


def _v40_first_existing(df, names, default=0.5):
    for name in names:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(default, index=df.index, dtype=float)


def v40_init_learning_tables(db_path=DB_PATH):
    v39_init_learning_tables(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS v40_prediction_feature_snapshots (
            race_key TEXT NOT NULL,
            car_no INTEGER NOT NULL,
            player_name TEXT,
            trial_feature REAL, st_feature REAL, handicap_feature REAL,
            form_feature REAL, surface_feature REAL, front_feature REAL,
            chase_feature REAL, stability_feature REAL, course_feature REAL,
            opponent_feature REAL, before_score REAL, after_score REAL,
            before_rank INTEGER, after_rank INTEGER,
            PRIMARY KEY(race_key, car_no)
        )""")
        for name, weight in V40_DEFAULT_WEIGHTS.items():
            con.execute("""INSERT OR IGNORE INTO adaptive_weights
                (feature_name,current_weight,initial_weight,updated_at,update_count)
                VALUES(?,?,?,?,0)""", (name, weight, weight, now))
        con.commit()


def v40_get_weights(db_path=DB_PATH):
    """10項目学習重みを返す。

    登録結果が少ない段階では、直近数レースの偶然へ過適合しやすい。
    そのため有効な登録レース数に応じて初期値へ縮約し、30レースで
    保存済み学習重みを100%採用する。
    """
    v40_init_learning_tables(db_path)
    with sqlite3.connect(db_path) as con:
        rows = con.execute("SELECT feature_name,current_weight FROM adaptive_weights").fetchall()
        try:
            race_count = int(con.execute(
                "SELECT COUNT(*) FROM v41_registration_batches WHERE status='active'"
            ).fetchone()[0])
        except sqlite3.Error:
            race_count = 0
    values = {str(k): float(v) for k, v in rows if str(k) in V40_DEFAULT_WEIGHTS}
    for k, v in V40_DEFAULT_WEIGHTS.items():
        values.setdefault(k, v)
    total = sum(max(0.0, values[k]) for k in V40_DEFAULT_WEIGHTS)
    if total <= 0:
        learned = dict(V40_DEFAULT_WEIGHTS)
    else:
        learned = {k: max(0.0, values[k]) / total for k in V40_DEFAULT_WEIGHTS}

    reliability = float(np.clip(race_count / 30.0, 0.0, 1.0))
    blended = {
        k: V40_DEFAULT_WEIGHTS[k] * (1.0 - reliability) + learned[k] * reliability
        for k in V40_DEFAULT_WEIGHTS
    }
    blended_total = sum(blended.values())
    return {k: blended[k] / blended_total for k in V40_DEFAULT_WEIGHTS}


def v40_feature_frame(df):
    out = pd.DataFrame(index=df.index)
    out["試走"] = _v39_rank_feature(_v40_first_existing(df, ["試走換算", "今回試走", "試走T"]), True)
    out["ST"] = _v39_rank_feature(_v40_first_existing(df, ["平均ST", "ST予測", "ST"]), True)
    out["ハンデ"] = _v39_rank_feature(_v40_first_existing(df, ["ハンデ"]), True)
    out["近況"] = _v39_rank_feature(_v40_first_existing(df, ["近況信頼度", "直近内容指数", "近況点"]), False)
    out["走路適性"] = _v39_rank_feature(_v40_first_existing(df, ["走路点", "走路安定指数"]), False)

    # 前残りは単純な小ハンデだけではなく、履歴から得た残存/逃げ指標を中心にする。
    front_raw = (
        _v40_first_existing(df, ["内枠残存率", "隊列残存指数"], 0.5) * 0.55
        + _v40_first_existing(df, ["逃げ成功率", "前線指揮指数"], 0.5) * 0.30
        + (1.0 - _v39_rank_feature(_v40_first_existing(df, ["ハンデ"]), False)) * 0.15
    )
    out["前残り"] = _v39_rank_feature(front_raw, False)

    chase_raw = (
        _v40_first_existing(df, ["終盤指数", "終盤力補正"], 0.5) * 0.45
        + _v40_first_existing(df, ["集団突破力", "混戦突破適性"], 0.5) * 0.40
        + _v40_first_existing(df, ["勝ち切り指数"], 0.5) * 0.15
    )
    out["追い込み"] = _v39_rank_feature(chase_raw, False)

    stable_raw = (
        _v40_first_existing(df, ["安定上位指数", "連下安定指数"], 0.5) * 0.45
        + _v40_first_existing(df, ["着順安定度", "着順安定度点"], 0.5) * 0.35
        + _v40_first_existing(df, ["ST安定性", "ST安定点"], 0.5) * 0.20
    )
    out["周回安定"] = _v39_rank_feature(stable_raw, False)

    out["コース適性"] = _v39_rank_feature(
        _v40_first_existing(df, ["会場適性点", "場適性点", "開催場点", "コース適性"]), False
    )
    out["相手耐性"] = _v39_rank_feature(
        _v40_first_existing(df, ["相手レベル耐性補正", "相手耐性", "強敵耐性指数"]), False
    )
    return out.fillna(0.5).clip(0.0, 1.0)


def v40_apply_adaptive_weights(df, db_path=DB_PATH):
    df = df.copy()
    weights = v40_get_weights(db_path)
    features = v40_feature_frame(df)
    base = pd.to_numeric(df.get("改善後総合点", pd.Series(0, index=df.index)), errors="coerce").fillna(0.0)
    df["調整前総合点"] = base
    df["調整前順位"] = base.rank(method="min", ascending=False).astype(int)

    centered = sum(weights[k] * (features[k] - 0.5) for k in V40_DEFAULT_WEIGHTS)
    # 追加評価は元モデルを破壊しない範囲。最大およそ±3点。
    bonus = (centered * 6.0).clip(-3.0, 3.0)
    df["学習重み補正"] = bonus
    df["改善後総合点"] = base + bonus
    df["改善後順位"] = df["改善後総合点"].rank(method="min", ascending=False).astype(int)
    for k in V40_DEFAULT_WEIGHTS:
        df[f"学習特徴_{k}"] = features[k]

    # なぜ上げ下げしたかを選手ごとに見える化。
    reasons = []
    for idx in df.index:
        parts = sorted(
            ((k, weights[k] * (features.loc[idx, k] - 0.5)) for k in V40_DEFAULT_WEIGHTS),
            key=lambda x: abs(x[1]), reverse=True
        )[:3]
        reasons.append(" / ".join(f"{k}{'+' if v >= 0 else '-'}" for k, v in parts))
    df["主な評価理由"] = reasons
    return df


def v40_save_prediction_features(meta, df, db_path=DB_PATH):
    v40_init_learning_tables(db_path)
    key = v34_race_key(meta)
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM v40_prediction_feature_snapshots WHERE race_key=?", (key,))
        for _, r in df.iterrows():
            con.execute("""INSERT INTO v40_prediction_feature_snapshots VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                key, int(r.get("車", r.get("車番"))), str(r.get("選手名", "")),
                *[float(r.get(f"学習特徴_{k}", 0.5)) for k in V40_DEFAULT_WEIGHTS],
                float(r.get("調整前総合点", 0)), float(r.get("改善後総合点", 0)),
                int(r.get("調整前順位", 0)), int(r.get("改善後順位", 0))
            ))
        con.commit()
    return key


def v40_adjust_weights_after_result(meta, results, db_path=DB_PATH):
    """10要素を微調整。三連単完全一致だけを的中扱いにする。"""
    v40_init_learning_tables(db_path)
    key = v34_race_key(meta)
    with sqlite3.connect(db_path) as con:
        snap = pd.read_sql_query("SELECT * FROM v40_prediction_feature_snapshots WHERE race_key=?", con, params=(key,))
    if snap.empty:
        return {"message": "特徴スナップショットがないため、重みは変更していません。次回予測から記録されます。"}
    merged = snap.merge(results[["車番", "着順"]], left_on="car_no", right_on="車番", how="inner")
    if len(merged) < 3:
        return {"message": "比較可能な選手が3人未満のため、重みは変更していません。"}

    performance = (len(merged) + 1) - pd.to_numeric(merged["着順"], errors="coerce")
    colmap = {
        "試走": "trial_feature", "ST": "st_feature", "ハンデ": "handicap_feature",
        "近況": "form_feature", "走路適性": "surface_feature", "前残り": "front_feature",
        "追い込み": "chase_feature", "周回安定": "stability_feature",
        "コース適性": "course_feature", "相手耐性": "opponent_feature",
    }
    evidence = {k: _v39_spearman(merged[c], performance) for k, c in colmap.items()}
    before = v40_get_weights(db_path)
    strength = {k: max(0.05, (evidence[k] + 1.0) / 2.0) for k in evidence}
    target_total = sum(strength.values())
    target = {k: strength[k] / target_total for k in strength}
    raw = {k: before[k] + max(-0.003, min(0.003, (target[k] - before[k]) * 0.06)) for k in before}
    raw = {k: min(0.30, max(0.025, v)) for k, v in raw.items()}
    total = sum(raw.values())
    after = {k: v / total for k, v in raw.items()}

    before_top3 = "→".join(map(str, merged.sort_values("before_rank")["car_no"].head(3).astype(int)))
    diagnostic = sum(after[k] * merged[colmap[k]] for k in after)
    after_top3 = "→".join(map(str, merged.assign(diag_score=diagnostic).sort_values("diag_score", ascending=False)["car_no"].head(3).astype(int)))
    actual_top3 = "→".join(map(str, merged.sort_values("着順")["car_no"].head(3).astype(int)))
    exact_before = before_top3 == actual_top3
    exact_after = after_top3 == actual_top3
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as con:
        for k in before:
            delta = after[k] - before[k]
            reason = f"今回結果との順位相関 {evidence[k]:+.3f}。最大変更幅±0.003、三連単完全一致のみ的中。"
            con.execute("UPDATE adaptive_weights SET current_weight=?,updated_at=?,update_count=update_count+1 WHERE feature_name=?", (after[k], now, k))
            con.execute("""INSERT INTO weight_adjustment_history
                (race_key,adjusted_at,feature_name,before_weight,after_weight,delta,evidence_score,reason,
                 before_top3,after_top3,actual_top3,diagnostic_exact_before,diagnostic_exact_after)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                key, now, k, before[k], after[k], delta, evidence[k], reason,
                before_top3, after_top3, actual_top3, int(exact_before), int(exact_after)
            ))
        con.commit()
    return {
        "race_key": key, "before": before, "after": after, "evidence": evidence,
        "before_top3": before_top3, "after_top3": after_top3, "actual_top3": actual_top3,
        "diagnostic_exact_before": exact_before, "diagnostic_exact_after": exact_after,
        "note": "三連単は順番まで完全一致した場合だけ的中です。同レースの調整後比較は診断専用です。",
    }


def v40_current_weights(db_path=DB_PATH):
    w = v40_get_weights(db_path)
    return pd.DataFrame([
        {"項目": k, "現在の重み": v, "初期値": V40_DEFAULT_WEIGHTS[k], "初期値からの差": v - V40_DEFAULT_WEIGHTS[k]}
        for k, v in w.items()
    ])

# ============================================================
# v4.1 全結果・直近重視学習 / 重複防止 / 欠損着順除外 / Undo
# ============================================================
V41_RECENT_RACES = 20
V41_DECAY = 0.90


def v41_valid_results(results):
    """有効着順だけを分析用に返す。欠車・中止・空欄等は保存可能だが分析対象外。"""
    if results is None or results.empty:
        return pd.DataFrame(), pd.DataFrame()
    work = results.copy()
    work["_finish_num"] = pd.to_numeric(work.get("着順"), errors="coerce")
    field_size = max(1, int(len(work)))
    status = work.get("結果区分", pd.Series("通常", index=work.index)).fillna("").astype(str)
    invalid_status = status.str.contains("欠車|競走中止|落車|反則|不成立|失格", regex=True)
    valid_mask = work["_finish_num"].notna() & (work["_finish_num"] >= 1) & (work["_finish_num"] <= field_size) & ~invalid_status
    valid = work.loc[valid_mask].copy()
    valid["着順"] = valid["_finish_num"].astype(int)
    excluded = work.loc[~valid_mask].copy()
    if not excluded.empty:
        excluded["除外理由"] = np.where(invalid_status.loc[excluded.index], status.loc[excluded.index], "着順なし・範囲外")
    return valid.drop(columns=["_finish_num"], errors="ignore"), excluded.drop(columns=["_finish_num"], errors="ignore")


def v41_init_tables(db_path=DB_PATH):
    v40_init_learning_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS v41_registration_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_key TEXT NOT NULL UNIQUE,
            registered_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            before_weights_json TEXT,
            valid_count INTEGER NOT NULL DEFAULT 0,
            excluded_count INTEGER NOT NULL DEFAULT 0,
            undone_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_v41_batches_status ON v41_registration_batches(status, batch_id);
        """)
        con.commit()


def v41_race_exists(meta, db_path=DB_PATH):
    v41_init_tables(db_path)
    key = v34_race_key(meta)
    with sqlite3.connect(str(db_path)) as con:
        row = con.execute("SELECT registered_at FROM result_races WHERE race_key=?", (key,)).fetchone()
    return (row is not None), key, (row[0] if row else None)


def _v41_all_race_evidence(db_path=DB_PATH):
    """全登録結果を使い、直近20件を指数減衰で強く、古い履歴を20%残して相関を集計。"""
    v41_init_tables(db_path)
    colmap = {
        "試走": "trial_feature", "ST": "st_feature", "ハンデ": "handicap_feature",
        "近況": "form_feature", "走路適性": "surface_feature", "前残り": "front_feature",
        "追い込み": "chase_feature", "周回安定": "stability_feature",
        "コース適性": "course_feature", "相手耐性": "opponent_feature",
    }
    with sqlite3.connect(str(db_path)) as con:
        races = pd.read_sql_query("""
            SELECT rr.race_key, rr.registered_at
            FROM result_races rr
            JOIN v40_prediction_feature_snapshots s ON s.race_key=rr.race_key
            LEFT JOIN v41_registration_batches b ON b.race_key=rr.race_key
            WHERE COALESCE(b.status,'active')='active'
            GROUP BY rr.race_key, rr.registered_at
            ORDER BY rr.registered_at DESC, rr.race_key DESC
        """, con)
        per_race = []
        for _, rr in races.iterrows():
            merged = pd.read_sql_query("""
                SELECT s.*, e.finish, e.result_status
                FROM v40_prediction_feature_snapshots s
                JOIN result_entries e ON e.race_key=s.race_key AND e.car_no=s.car_no
                WHERE s.race_key=?
            """, con, params=(rr["race_key"],))
            if merged.empty:
                continue
            finish = pd.to_numeric(merged["finish"], errors="coerce")
            status = merged["result_status"].fillna("").astype(str)
            n_all = len(merged)
            mask = finish.notna() & (finish >= 1) & (finish <= n_all) & ~status.str.contains("欠車|競走中止|落車|反則|不成立|失格", regex=True)
            merged = merged.loc[mask].copy()
            if len(merged) < 3:
                continue
            performance = (len(merged) + 1) - pd.to_numeric(merged["finish"], errors="coerce")
            row = {"race_key": rr["race_key"], "registered_at": rr["registered_at"]}
            for k, c in colmap.items():
                row[k] = _v39_spearman(merged[c], performance)
            per_race.append(row)
    if not per_race:
        return {k: 0.0 for k in V40_DEFAULT_WEIGHTS}, {"race_count": 0, "latest_contribution": 0.0, "recent_contribution": 0.0}
    ev = pd.DataFrame(per_race)
    recent = ev.head(V41_RECENT_RACES).copy()
    recent_weights = np.array([V41_DECAY ** i for i in range(len(recent))], dtype=float)
    recent_weights /= recent_weights.sum()
    old = ev.iloc[V41_RECENT_RACES:].copy()
    evidence = {}
    for k in V40_DEFAULT_WEIGHTS:
        recent_value = float(np.average(pd.to_numeric(recent[k], errors="coerce").fillna(0.0), weights=recent_weights))
        if old.empty:
            evidence[k] = recent_value
        else:
            old_value = float(pd.to_numeric(old[k], errors="coerce").fillna(0.0).mean())
            evidence[k] = 0.80 * recent_value + 0.20 * old_value
    latest_contribution = float(recent_weights[0] * (0.80 if not old.empty else 1.0))
    recent_contribution = 0.80 if not old.empty else 1.0
    return evidence, {
        "race_count": int(len(ev)),
        "recent_count": int(len(recent)),
        "old_count": int(len(old)),
        "latest_contribution": latest_contribution,
        "recent_contribution": recent_contribution,
    }


def v41_adjust_weights_after_result(meta, results, db_path=DB_PATH):
    """同一レースは二重学習せず、全結果を直近重視で集計して10要素を微調整。"""
    v41_init_tables(db_path)
    key = v34_race_key(meta)
    valid, excluded = v41_valid_results(results)
    if len(valid) < 3:
        return {"message": f"有効着順が{len(valid)}人のため、重みは変更していません。", "valid_count": len(valid), "excluded_count": len(excluded)}
    with sqlite3.connect(str(db_path)) as con:
        already = con.execute("SELECT 1 FROM weight_adjustment_history WHERE race_key=? LIMIT 1", (key,)).fetchone()
    if already:
        return {"message": "このレースはすでに学習済みのため、重みを二重更新していません。", "duplicate": True}

    evidence, stats = _v41_all_race_evidence(db_path)
    before = v40_get_weights(db_path)
    strength = {k: max(0.05, (evidence[k] + 1.0) / 2.0) for k in evidence}
    target_total = sum(strength.values())
    target = {k: strength[k] / target_total for k in strength}
    raw = {k: before[k] + max(-0.003, min(0.003, (target[k] - before[k]) * 0.08)) for k in before}
    raw = {k: min(0.30, max(0.025, v)) for k, v in raw.items()}
    total = sum(raw.values())
    after = {k: v / total for k, v in raw.items()}

    with sqlite3.connect(str(db_path)) as con:
        snap = pd.read_sql_query("SELECT * FROM v40_prediction_feature_snapshots WHERE race_key=?", con, params=(key,))
    merged = snap.merge(valid[["車番", "着順"]], left_on="car_no", right_on="車番", how="inner")
    colmap = {"試走":"trial_feature","ST":"st_feature","ハンデ":"handicap_feature","近況":"form_feature","走路適性":"surface_feature","前残り":"front_feature","追い込み":"chase_feature","周回安定":"stability_feature","コース適性":"course_feature","相手耐性":"opponent_feature"}
    before_top3 = "→".join(map(str, merged.sort_values("before_rank")["car_no"].head(3).astype(int))) if not merged.empty else ""
    diagnostic = sum(after[k] * merged[colmap[k]] for k in after) if not merged.empty else pd.Series(dtype=float)
    after_top3 = "→".join(map(str, merged.assign(diag_score=diagnostic).sort_values("diag_score", ascending=False)["car_no"].head(3).astype(int))) if not merged.empty else ""
    actual_top3 = "→".join(map(str, valid.sort_values("着順")["車番"].head(3).astype(int)))
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as con:
        for k in before:
            delta = after[k] - before[k]
            reason = f"全{stats['race_count']}レースを集計。直近{stats.get('recent_count',0)}件を指数減衰({V41_DECAY})で重視。"
            con.execute("UPDATE adaptive_weights SET current_weight=?,updated_at=?,update_count=update_count+1 WHERE feature_name=?", (after[k], now, k))
            con.execute("""INSERT INTO weight_adjustment_history
                (race_key,adjusted_at,feature_name,before_weight,after_weight,delta,evidence_score,reason,before_top3,after_top3,actual_top3,diagnostic_exact_before,diagnostic_exact_after)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (key, now, k, before[k], after[k], delta, evidence[k], reason, before_top3, after_top3, actual_top3, int(before_top3==actual_top3), int(after_top3==actual_top3)))
        con.commit()
    return {"race_key":key,"before":before,"after":after,"evidence":evidence,"before_top3":before_top3,"after_top3":after_top3,"actual_top3":actual_top3,"diagnostic_exact_before":before_top3==actual_top3,"diagnostic_exact_after":after_top3==actual_top3,"valid_count":len(valid),"excluded_count":len(excluded),"learning_stats":stats,"note":"全登録結果を考慮し、最近ほど強く反映しました。三連単は順番まで完全一致のみ的中です。"}


def v41_register_result(meta, results, laps=None, payouts=None, db_path=DB_PATH):
    """安全登録。レース名はキーに使わず、同一レースを拒否し、Undo情報を保存。"""
    v41_init_tables(db_path)
    exists, key, registered_at = v41_race_exists(meta, db_path)
    if exists:
        return key, pd.DataFrame(), {"message": f"同じ開催日・開催場・レース番号の結果は登録済みです（{registered_at}）。履歴追加・重み更新は行っていません。", "duplicate": True}, {"message":"重複のため学習なし", "duplicate":True}, {"duplicate":True}
    valid, excluded = v41_valid_results(results)
    before_weights = v40_get_weights(db_path)
    # 既存の保存処理は着順を整数化するため、有効着順だけを渡す。
    # 除外行は後段で着順NULLのまま保存し、試走/ST等の履歴だけ利用可能にする。
    key, comparison, analysis = v36_save_result_and_analyze(meta, valid, laps, payouts, db_path)
    if not excluded.empty:
        now_ex = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(str(db_path)) as con:
            for _, r in excluded.iterrows():
                con.execute("""INSERT OR REPLACE INTO result_entries
                    (race_key,car_no,player_name,finish,trial_time,race_time,start_time,handicap,result_status)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (
                    key, int(r.get("車番")), str(r.get("選手名", "")), None,
                    None if pd.isna(r.get("試走T")) else float(r.get("試走T")),
                    None if pd.isna(r.get("競走T")) else float(r.get("競走T")),
                    None if pd.isna(r.get("ST")) else float(r.get("ST")),
                    str(r.get("ハンデ", "")), str(r.get("結果区分", "着順なし"))
                ))
            con.commit()
        # 着順なしでも試走・ST・走路情報は履歴として保存する。
        extra_summary = v36_update_player_histories(meta, excluded, None, db_path)
        analysis["履歴追加"] = int(analysis.get("履歴追加", 0)) + int(extra_summary.get("履歴追加", 0))
        analysis["履歴重複スキップ"] = int(analysis.get("履歴重複スキップ", 0)) + int(extra_summary.get("履歴重複スキップ", 0))
    adjustment = v41_adjust_weights_after_result(meta, results, db_path)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as con:
        con.execute("""INSERT OR REPLACE INTO v41_registration_batches
            (race_key,registered_at,status,before_weights_json,valid_count,excluded_count,undone_at)
            VALUES(?,?,'active',?,?,?,NULL)""", (key, now, json.dumps(before_weights, ensure_ascii=False), len(valid), len(excluded)))
        con.commit()
    analysis = dict(analysis)
    analysis.update({"分析対象":len(valid),"分析除外":len(excluded)})
    return key, comparison, analysis, adjustment, {"duplicate":False,"valid":valid,"excluded":excluded}


def v41_registration_history(db_path=DB_PATH, limit=50):
    v41_init_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        return pd.read_sql_query("""SELECT batch_id AS ID, race_key AS レースID, registered_at AS 登録日時,
            CASE status WHEN 'active' THEN '登録中' ELSE '取消済' END AS 状態,
            valid_count AS 分析対象, excluded_count AS 除外
            FROM v41_registration_batches ORDER BY batch_id DESC LIMIT ?""", con, params=(int(limit),))


def v41_registration_detail(race_key, db_path=DB_PATH):
    """登録済み結果を再表示するための詳細取得。"""
    v41_init_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        race = pd.read_sql_query("SELECT * FROM result_races WHERE race_key=?", con, params=(race_key,))
        entries = pd.read_sql_query("SELECT * FROM result_entries WHERE race_key=? ORDER BY CASE WHEN finish IS NULL THEN 999 ELSE finish END, car_no", con, params=(race_key,))
        laps = pd.read_sql_query("SELECT * FROM result_laps WHERE race_key=? ORDER BY lap_no, position", con, params=(race_key,))
        payouts = pd.read_sql_query("SELECT * FROM result_payouts WHERE race_key=?", con, params=(race_key,))
        feedback = pd.read_sql_query("SELECT * FROM prediction_feedback WHERE race_key=?", con, params=(race_key,))
    return {"race": race, "entries": entries, "laps": laps, "payouts": payouts, "feedback": feedback}


def v41_undo_last_registration(db_path=DB_PATH):
    """最後の結果登録を、重み・結果・周回・払戻・自動追加履歴ごと取り消す。"""
    v41_init_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        batch = con.execute("SELECT * FROM v41_registration_batches WHERE status='active' ORDER BY batch_id DESC LIMIT 1").fetchone()
        if not batch:
            return False, "取り消せる結果登録がありません。"
        key = batch["race_key"]
        rr = con.execute("SELECT * FROM result_races WHERE race_key=?", (key,)).fetchone()
        entries = con.execute("SELECT * FROM result_entries WHERE race_key=?", (key,)).fetchall()
        before = json.loads(batch["before_weights_json"] or "{}")
        now = datetime.now().isoformat(timespec="seconds")
        for name, weight in before.items():
            con.execute("UPDATE adaptive_weights SET current_weight=?,updated_at=?,update_count=MAX(0,update_count-1) WHERE feature_name=?", (float(weight), now, name))
        # 自動結果登録で追加した選手履歴だけを実値照合して削除。
        if rr:
            for e in entries:
                found = _v32_find_player(con, e["player_name"])
                if found:
                    player_id = found[0]
                    rows = con.execute("SELECT * FROM race_history WHERE player_id=? AND race_date=? AND venue=? AND source='スマホ貼付登録'", (player_id, rr["race_date"], rr["venue"])).fetchall()
                    cols = [d[0] for d in con.execute("SELECT * FROM race_history LIMIT 0").description]
                    for rh in rows:
                        d = dict(zip(cols, rh))
                        same = (_v33_norm_number(d.get("finish"),0)==_v33_norm_number(e["finish"],0) and _v33_norm_number(d.get("trial_time"),3)==_v33_norm_number(e["trial_time"],3) and _v33_norm_number(d.get("race_time"),3)==_v33_norm_number(e["race_time"],3) and _v33_norm_number(d.get("start_time"),3)==_v33_norm_number(e["start_time"],3))
                        if same:
                            con.execute("DELETE FROM race_history WHERE history_id=?", (d["history_id"],))
                    # ミラー表も同じ実値で削除。
                    con.execute("""DELETE FROM v15_player_history_imports WHERE player_name=? AND race_date=? AND venue=?
                        AND COALESCE(rank,-999)=COALESCE(?,-999) AND COALESCE(trial_time,-999)=COALESCE(?,-999)
                        AND COALESCE(race_time,-999)=COALESCE(?,-999) AND COALESCE(st,-999)=COALESCE(?,-999)""",
                        (found[1], rr["race_date"], rr["venue"], e["finish"], e["trial_time"], e["race_time"], e["start_time"]))
        for table in ["result_laps","result_payouts","prediction_feedback","result_entries","player_lap_history","weight_adjustment_history"]:
            try:
                con.execute(f"DELETE FROM {table} WHERE race_key=?", (key,))
            except sqlite3.OperationalError:
                pass
        con.execute("DELETE FROM result_races WHERE race_key=?", (key,))
        con.execute("UPDATE v41_registration_batches SET status='undone',undone_at=? WHERE batch_id=?", (now, batch["batch_id"]))
        con.commit()
    return True, f"{key} の結果登録と学習を取り消しました。予測スナップショットは再登録用に残しています。"

# ============================================================
# Ver36 公式出走表（車・選手・ハンデ・近走が縦に並ぶ形式）対応
# ============================================================

_V36_TRACKS = ("川口", "伊勢崎", "浜松", "山陽", "飯塚")


def _v36_lines(text):
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("\u3000", " ").replace("\xa0", " ")
    return [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]


def _v36_is_name(value):
    s = str(value or "").strip()
    if not s or len(s) > 40:
        return False
    bad = {
        "出走表", "出場選手", "近10走", "良5走", "湿5走", "斑5走",
        "近90日", "近180日", "今年/通算", "車", "選手", "ハンデ",
    }
    if s in bad or re.fullmatch(r"\d+(?:\.\d+)?(?:%|R|m)?", s):
        return False
    if any(track in s and re.search(r"\d+期", s) for track in _V36_TRACKS):
        return False
    return bool(re.search(r"[一-龠々〆ヵヶぁ-んァ-ヶー]", s))


def v36_split_mobile_entry_blocks(text):
    """新しい公式出走表の車番単独行から、各選手ブロックを切り出す。"""
    lines = _v36_lines(text)
    starts = []
    for i, line in enumerate(lines):
        if not re.fullmatch(r"[1-8]", line):
            continue
        # 車番の次にある最初の非空行が氏名で、その数行後に所属＋期がある場合だけ採用。
        following = [x for x in lines[i + 1:i + 8] if x]
        if not following or not _v36_is_name(following[0]):
            continue
        if not any(any(t in x for t in _V36_TRACKS) and re.search(r"\d+期", x) for x in following[1:]):
            continue
        starts.append(i)

    blocks = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        block_lines = [x for x in lines[start + 1:end] if x]
        if block_lines:
            blocks.append({"車番": int(lines[start]), "lines": block_lines})
    return blocks


def _v36_float_token(value):
    s = str(value or "").strip()
    s = re.sub(r"^(?:再|再試|試)", "", s)
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else np.nan


def v36_parse_mobile_entry_block(block):
    car_no = int(block["車番"])
    lines = [x for x in block.get("lines", []) if x]
    if not lines:
        return None

    name = v15_normalize_name(lines[0])
    car_name = lines[1] if len(lines) > 1 else None
    compact = " ".join(lines)

    lg = None
    term = None
    lg_idx = None
    for i, line in enumerate(lines[:10]):
        m = re.search(r"(川口|伊勢崎|浜松|山陽|飯塚)\s*(\d{1,2})期", line)
        if m:
            lg, term, lg_idx = m.group(1), int(m.group(2)), i
            break

    age = v15_int(v15_first_match([r"(\d{1,3})歳"], compact))
    grade_num = v15_int(v15_first_match([r"(\d)級"], compact))
    rank = v15_first_match([r"\b([SAB]-\d+)\b"], compact)

    handicap = None
    trial = np.nan
    trial_dev = np.nan
    # ランク行の直後は ハンデ → 試走T → 偏差 の並び。
    rank_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\b[SAB]-\d+\b", line):
            rank_idx = i
            break
    if rank_idx is not None:
        tail = " ".join(lines[rank_idx + 1:rank_idx + 8])
        # タブ貼付では「0 再3.50 107」が同じ行になるため、並びをまとめて読む。
        seq = re.search(
            r"(?:^|\s)(0|10|20|30|40|50|60|70|80)\s+(?:再)?([3-9]\.\d{2,3}|-)\s+(\d{2,3})(?:\s|$)",
            tail,
        )
        if seq:
            handicap = int(seq.group(1))
            if seq.group(2) != "-":
                trial = float(seq.group(2))
            trial_dev = int(seq.group(3)) / 1000.0
        else:
            numeric = []
            for line in lines[rank_idx + 1:rank_idx + 8]:
                for token in line.split():
                    if re.fullmatch(r"-?\d+", token):
                        numeric.append(("int", token))
                    elif re.fullmatch(r"(?:再)?\d\.\d{2,3}|-", token):
                        numeric.append(("time", token))
            if numeric:
                first_kind, first_val = numeric[0]
                if first_kind == "int" and int(first_val) in range(0, 81, 10):
                    handicap = int(first_val)
                for kind, val in numeric[1:]:
                    if pd.isna(trial) and kind == "time" and val != "-":
                        trial = _v36_float_token(val)
                        continue
                    if pd.isna(trial_dev) and kind == "int" and 0 <= int(val) <= 999:
                        trial_dev = int(val) / 1000.0
                        break

    # フォールバック
    if handicap is None:
        hm = re.search(r"(?:^|\s)(0|10|20|30|40|50|60|70|80)(?:\s|$)", compact)
        if hm:
            handicap = int(hm.group(1))
    if pd.isna(trial):
        tm = re.search(r"(?:再)?([3-9]\.\d{2,3})", compact)
        if tm:
            trial = float(tm.group(1))

    two_rate = v15_float(v15_first_match([r"2連率\s*([0-9.]+)"], compact))
    three_rate = v15_float(v15_first_match([r"3連率\s*([0-9.]+)"], compact))

    # この形式には当日STがないためNaN。近走STを当日STとして誤用しない。
    row = {
        "車番": car_no,
        "選手名": name,
        "ハンデ": handicap,
        "試走T": trial,
        "ST": np.nan,
        "年齢": age,
        "級別": rank.split("-")[0] if rank else (str(grade_num) if grade_num else None),
        "期別": term,
        "所属": lg,
        "現ランク": rank,
        "試走偏差": trial_dev,
        "近10走2連": two_rate,
        "近10走3連": three_rate,
        "2連対率": two_rate,
        "3連対率": three_rate,
        "車名": car_name,
        "_raw": "\n".join(lines),
    }
    return row


def v36_parse_mobile_entries(text):
    rows = []
    for block in v36_split_mobile_entry_blocks(text):
        row = v36_parse_mobile_entry_block(block)
        if row:
            rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates("車番").sort_values("車番").reset_index(drop=True)
    return df


# 既存Ver35パーサーを保持し、新形式を最優先にする。
_v36_previous_parse_entries = v15_parse_entries


def v15_parse_entries(text, manual_excluded=None):
    excluded = dict(v17_detect_nonstarters(text))
    if manual_excluded is not None:
        excluded = {int(car): "手動欠車" for car in manual_excluded}

    mobile = v36_parse_mobile_entries(text)
    if not mobile.empty and mobile["車番"].nunique() >= 2:
        mobile = mobile.copy()
        mobile["出走状態"] = mobile["車番"].map(lambda x: excluded.get(int(x), "出走"))
        mobile["解析対象"] = ~mobile["車番"].astype(int).isin(excluded)
        return mobile[mobile["解析対象"]].sort_values("車番").reset_index(drop=True)

    return _v36_previous_parse_entries(text, manual_excluded=manual_excluded)


_v36_previous_parse_meta = v15_parse_race_meta


def v15_parse_race_meta(text):
    meta = _v36_previous_parse_meta(text)
    lines = _v36_lines(text)
    compact = " ".join(x for x in lines if x)

    # 「7月25日(土) 予選4R 3100m 晴」の表形式。
    m = re.search(r"(?:予選|一般|準決|優勝|選抜)?\s*(\d{1,2})R", compact)
    if m:
        meta["レース"] = int(m.group(1))
    m = re.search(r"発走予定\s*(\d{1,2}:\d{2})", compact)
    if m:
        meta["発走時刻"] = m.group(1)
    if meta.get("発走時刻"):
        hour = int(str(meta["発走時刻"]).split(":")[0])
        meta["時間帯"] = "昼" if hour < 16 else ("夕方" if hour < 18 else "夜")

    # 項目名の次行に値が並ぶ気象表を解析。
    for i, line in enumerate(lines):
        if line == "気温 湿度 走路温度 走路状況" and i + 1 < len(lines):
            vals = lines[i + 1].split()
            if len(vals) >= 4:
                meta["気温"] = _v36_float_token(vals[0])
                meta["湿度"] = _v36_float_token(vals[1])
                meta["走路温度"] = _v36_float_token(vals[2])
                meta["走路状態"] = vals[3].replace("走路", "")
        if line == "日付 レース 距離 天候" and i + 1 < len(lines):
            vals = lines[i + 1].split()
            if len(vals) >= 4:
                meta["天候"] = vals[-1]
                dm = re.search(r"(\d{1,2})月(\d{1,2})日", vals[0])
                # 年は開催期間から既に取れている。取れていなければ現在年を利用。
                if dm and not meta.get("開催日"):
                    year_m = re.search(r"(20\d{2})年", compact)
                    year = int(year_m.group(1)) if year_m else datetime.now().year
                    meta["開催日"] = f"{year:04d}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"

    # 開催場は選手所属ではなく開催文脈から優先取得。
    vm = re.search(r"開催期間.*?(川口|伊勢崎|浜松|山陽|飯塚)", compact)
    if vm:
        meta["開催場"] = vm.group(1)
    # 上記形式は開催場名がタイトルにない場合があるため、レースページ中の開催名を補助。
    if not meta.get("開催場"):
        vm = re.search(r"令和\S*年度(川口|伊勢崎|浜松|山陽|飯塚)市営", compact)
        if vm:
            meta["開催場"] = vm.group(1)

    # 開催場が本文に明示されず、選手所属だけから拾われた場合は誤判定を避けて空欄にする。
    explicit_venue = re.search(
        r"(?:令和\S*年度)?(川口|伊勢崎|浜松|山陽|飯塚)市営|(?:開催場|会場)\s*[:：]?\s*(川口|伊勢崎|浜松|山陽|飯塚)",
        compact,
    )
    if explicit_venue:
        meta["開催場"] = next((g for g in explicit_venue.groups() if g), meta.get("開催場"))
    elif "出場選手" in compact and "車 選手 ハンデ" in compact:
        meta["開催場"] = None

    # レース種別。
    for label, canonical in [("準決勝", "準決勝"), ("準決", "準決勝"), ("優勝", "優勝戦"), ("一般", "一般戦"), ("予選", "予選")]:
        if label in compact:
            meta["レース種別"] = canonical
            if not meta.get("レース名"):
                meta["レース名"] = canonical
            break
    return meta

# ============================================================
# v4.1 新公式結果レイアウト対応
# 通常-結果 / 重勝式-経過結果 / 周回順位タブ形式
# ============================================================
_v41_legacy_parse_result_text = v35_parse_result_text


def _v41_result_layout_meta(text, venue_override="", race_no_override=""):
    """新レイアウトの開催情報を解析する。結果側の日付・Rを優先する。"""
    meta = _v35_parse_meta(text, venue_override, race_no_override)

    # 開催期間の年と、結果欄の日付を組み合わせる。
    ym = re.search(r"開催期間[：:]\s*(20\d{2})年", text)
    year = int(ym.group(1)) if ym else None
    dm = re.search(r"(?m)^\s*(\d{1,2})月(\d{1,2})日\([^\n]*\)\s*[\t ]+[^\n]*?(\d{1,2})R\s*[\t ]+(\d+)m", text)
    if dm:
        if year:
            meta["開催日"] = f"{year:04d}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"
        meta["レース"] = int(dm.group(3))
        meta["距離"] = int(dm.group(4))
    else:
        # 日付・レースが改行されていても拾う。
        dm2 = re.search(r"(\d{1,2})月(\d{1,2})日\([^\n]*\)\s*[\t ]+([^\n]*?)(\d{1,2})R", text)
        if dm2:
            if year:
                meta["開催日"] = f"{year:04d}-{int(dm2.group(1)):02d}-{int(dm2.group(2)):02d}"
            meta["レース"] = int(dm2.group(4))
            race_label = dm2.group(3).strip()
            if race_label:
                meta["レース種別"] = race_label

    # 補助入力は常に最優先。
    if str(race_no_override).strip():
        meta["レース"] = v15_int(race_no_override)
    if str(venue_override).strip():
        meta["開催場"] = str(venue_override).strip()

    # 横並び気象表。
    wm = re.search(
        r"気温\s*[\t ]+湿度\s*[\t ]+走路温度\s*[\t ]+走路状況\s*\n"
        r"\s*(-?\d+(?:\.\d+)?)℃\s*[\t ]+([\d.]+)%\s*[\t ]+(-?\d+(?:\.\d+)?)℃\s*[\t ]+(良走路|湿走路|斑走路|風走路|荒走路)",
        text,
    )
    if wm:
        meta["気温"] = float(wm.group(1))
        meta["湿度"] = float(wm.group(2))
        meta["走路温度"] = float(wm.group(3))
        meta["走路状態"] = wm.group(4)

    # 天候は日付表の値を優先。
    weather = re.search(r"\d{1,2}月\d{1,2}日\([^\n]*\)\s*[\t ]+[^\n]*?R\s*[\t ]+\d+m\s*[\t ]+(晴|曇|雨|小雨|雪)", text)
    if weather:
        meta["天候"] = weather.group(1)

    # 発走予定。
    tm = re.search(r"発走予定\s*(\d{1,2}:\d{2})", text)
    if tm:
        meta["発走時刻"] = tm.group(1)
    return meta


def _v41_parse_trial_block(text, result_race_no):
    """上段試走表を解析。ただし結果レースと番号が一致するときだけ使う。"""
    m = re.search(r"(?m)^\s*(\d{1,2})R試走タイム\s*$", text)
    if not m or int(m.group(1)) != int(result_race_no or -1):
        return {}
    start = m.end()
    tail = text[start:]
    # 次の「初日」などで打ち切る。
    stop = re.search(r"(?m)^\s*(?:初日|2日目|3日目|最終日)\s*$", tail)
    if stop:
        tail = tail[:stop.start()]
    found = {}
    # 1 3.50 H0 5 3.44 H30 のように一行2車でも対応。
    for car, trial, hd in re.findall(r"(?:^|[\t ]+)([1-8])[\t ]+(?:再)?(\d\.\d{2})[\t ]+H(-?\d+)", tail, flags=re.M):
        found[int(car)] = {"試走T": float(trial), "ハンデ": int(hd)}
    return found


def _v41_parse_compact_result_entries(text, meta):
    """「着 事故 車 選手名」から始まる新しい縦型結果表を解析。"""
    hm = re.search(r"着\s+(?:事故\s+)?車\s+選手名", text)
    if not hm:
        raise ValueError("新形式の結果表見出しを取得できませんでした。")
    start = hm.end()
    end_candidates = [p for p in (text.find("払戻金", start), text.find("グランドノート", start)) if p >= 0]
    end = min(end_candidates) if end_candidates else len(text)
    block = text[start:end]
    raw_lines = block.splitlines()

    # エントリ開始行: 1\t\t6 のような「着・事故・車」。
    starts = []
    for idx, raw in enumerate(raw_lines):
        mm = re.match(r"^\s*([1-8])\s*\t\s*([^\t]*)\t\s*([1-8])\s*\t?\s*$", raw)
        if mm:
            starts.append((idx, int(mm.group(1)), mm.group(2).strip(), int(mm.group(3))))
            continue
        # コピー元によってタブが連続空白へ変換された場合。
        mm2 = re.match(r"^\s*([1-8])\s{2,}(.*?)\s{2,}([1-8])\s*$", raw)
        if mm2:
            starts.append((idx, int(mm2.group(1)), mm2.group(2).strip(), int(mm2.group(3))))
    if not starts:
        raise ValueError("結果行を解析できませんでした。")

    trial_map = _v41_parse_trial_block(text, meta.get("レース"))
    rows = []
    for pos, (idx, finish, accident, car) in enumerate(starts):
        next_idx = starts[pos + 1][0] if pos + 1 < len(starts) else len(raw_lines)
        chunk = raw_lines[idx + 1:next_idx]
        nonempty = [(j, x.strip()) for j, x in enumerate(chunk) if x.strip()]
        if not nonempty:
            continue
        name = v15_normalize_name(nonempty[0][1])
        # 2行目は通常車名。以後、数値で始まる行をハンデ等の行として扱う。
        data_line = ""
        for _, val in nonempty[1:]:
            if re.match(r"^-?\d+(?:\t|$)", val):
                data_line = val
                break
        if data_line:
            parts = [p.strip() for p in data_line.split("\t")]
            if len(parts) == 1:
                parts = [p.strip() for p in re.split(r"\s{2,}", data_line.strip())]
        else:
            parts = []
        handicap = np.nan
        trial = np.nan
        race_t = np.nan
        st_time = np.nan
        abnormal = accident
        if parts:
            try:
                handicap = int(float(parts[0]))
            except Exception:
                pass
            vals = parts[1:]
            # 列順: 試走T, 競走T, ST, 異。空欄を保持する。
            if len(vals) > 0 and re.search(r"\d", vals[0]):
                trial = _v35_float(re.sub(r"^再", "", vals[0]))
            if len(vals) > 1 and re.search(r"\d", vals[1]):
                race_t = _v35_float(vals[1])
            if len(vals) > 2 and re.search(r"\d", vals[2]):
                st_time = _v35_float(vals[2])
            if len(vals) > 3 and vals[3]:
                abnormal = vals[3]
        # 同一Rの上段試走表だけ補完に使用。
        if car in trial_map:
            if pd.isna(trial):
                trial = trial_map[car]["試走T"]
            if pd.isna(handicap):
                handicap = trial_map[car]["ハンデ"]

        rows.append({
            "着順": finish,
            "車番": car,
            "選手名": name,
            "所属": "",
            "ハンデ": handicap,
            "試走T": trial,
            "競走T": race_t,
            "ST": st_time,
            "人気": np.nan,
            "事故": abnormal,
            "結果区分": "通常" if not abnormal else abnormal,
        })
    if len(rows) < 3:
        raise ValueError("結果上位3選手を解析できませんでした。")
    return pd.DataFrame(rows).sort_values("着順").reset_index(drop=True)


def _v41_parse_compact_laps(text):
    if "グランドノート" not in text:
        return pd.DataFrame(columns=["周回", "周回番号", "順位", "車番"])
    block = text.split("グランドノート", 1)[1]
    records = []
    for raw in block.splitlines():
        line = raw.strip()
        mm = re.match(r"^(ゴール線通過|([1-9])周回)\s*[\t ]+((?:[1-8](?:\s*[\t ]+|\s+)){2,7}[1-8])\s*$", line)
        if not mm:
            continue
        label_raw = mm.group(1)
        if label_raw == "ゴール線通過":
            label, lap_no = "ゴール線", 99
        else:
            lap_no = int(mm.group(2))
            label = f"{lap_no}周目"
        cars = [int(x) for x in re.findall(r"[1-8]", mm.group(3))]
        for rank, car in enumerate(cars, 1):
            records.append({"周回": label, "周回番号": lap_no, "順位": rank, "車番": car})
    return pd.DataFrame(records, columns=["周回", "周回番号", "順位", "車番"])


def _v41_parse_compact_payouts(text):
    if "払戻金" not in text:
        return pd.DataFrame(columns=["券種", "組合せ", "払戻金", "人気"])
    block = text.split("払戻金", 1)[1]
    if "グランドノート" in block:
        block = block.split("グランドノート", 1)[0]
    known = {"単勝", "複勝", "2連複", "2連単", "ワイド", "3連複", "3連単"}
    current = ""
    rows = []
    for raw in block.splitlines():
        line = re.sub(r"[\u3000]+", " ", raw).strip()
        if not line or line in {"賭式\t払戻金\t人気", "返還"}:
            continue
        parts = [p.strip() for p in re.split(r"\t+| {2,}", line) if p.strip()]
        if not parts:
            continue
        if parts[0] in known:
            current = parts.pop(0)
        if current not in known or len(parts) < 2:
            continue
        # 複勝・ワイドの継続行にも対応。
        combo = parts[0].replace("→", "-")
        payout_idx = next((i for i, p in enumerate(parts[1:], 1) if re.fullmatch(r"[\d,]+円", p)), None)
        if payout_idx is None:
            continue
        payout = int(parts[payout_idx].replace(",", "").replace("円", ""))
        popularity = np.nan
        if payout_idx + 1 < len(parts) and re.fullmatch(r"\d+", parts[payout_idx + 1]):
            popularity = int(parts[payout_idx + 1])
        rows.append({"券種": current, "組合せ": combo, "払戻金": payout, "人気": popularity})
    return pd.DataFrame(rows, columns=["券種", "組合せ", "払戻金", "人気"])


def v35_parse_result_text(text, venue_override="", race_no_override=""):
    """旧形式と新公式サイト形式を自動判定して結果を解析する。"""
    if not str(text).strip():
        raise ValueError("結果ページを貼り付けてください。")
    is_compact = bool(re.search(r"着\s+(?:事故\s+)?車\s+選手名", text)) and "通常-結果" in text
    if not is_compact:
        return _v41_legacy_parse_result_text(text, venue_override, race_no_override)

    meta = _v41_result_layout_meta(text, venue_override, race_no_override)
    rows = _v41_parse_compact_result_entries(text, meta)
    laps = _v41_parse_compact_laps(text)
    payouts = _v41_parse_compact_payouts(text)
    if not meta.get("開催日") or not meta.get("開催場") or not meta.get("レース"):
        raise ValueError("開催日・開催場・レース番号を取得できませんでした。開催場が本文にない場合は補助入力で指定してください。")
    return meta, rows, laps, payouts

# 新形式で上位3選手しか氏名が掲載されない場合、保存済み予測と
# ゴール線順位を使って4～8着も補完する。
_v41_legacy_register_result = v41_register_result


def v41_register_result(meta, results, laps=None, payouts=None, db_path=DB_PATH):
    work = results.copy() if isinstance(results, pd.DataFrame) else pd.DataFrame(results)
    try:
        key = v34_race_key(meta)
        with sqlite3.connect(str(db_path)) as con:
            pred = pd.read_sql_query(
                "SELECT car_no, player_name FROM prediction_snapshots WHERE race_key=?",
                con,
                params=(key,),
            )
        if not pred.empty:
            name_map = {int(r.car_no): str(r.player_name or "").strip() for r in pred.itertuples()}
            if not work.empty:
                for idx, row in work.iterrows():
                    car = int(row.get("車番"))
                    if not str(row.get("選手名", "")).strip() and car in name_map:
                        work.at[idx, "選手名"] = name_map[car]
            if isinstance(laps, pd.DataFrame) and not laps.empty:
                goal = laps[laps["周回"] == "ゴール線"].sort_values("順位")
                existing = set(work["車番"].astype(int).tolist()) if not work.empty else set()
                extra = []
                for _, lr in goal.iterrows():
                    car = int(lr["車番"])
                    if car in existing or car not in name_map:
                        continue
                    extra.append({
                        "着順": int(lr["順位"]),
                        "車番": car,
                        "選手名": name_map[car],
                        "所属": "",
                        "ハンデ": "",
                        "試走T": np.nan,
                        "競走T": np.nan,
                        "ST": np.nan,
                        "人気": np.nan,
                        "事故": "",
                        "結果区分": "通常",
                    })
                if extra:
                    work = pd.concat([work, pd.DataFrame(extra)], ignore_index=True)
                    work = work.sort_values("着順").reset_index(drop=True)
    except Exception:
        # 補完に失敗しても、元の上位結果登録は続行する。
        work = results
    return _v41_legacy_register_result(meta, work, laps, payouts, db_path)

# ============================================================
# v4.2 結果ページ冒頭ヘッダー形式の厳密対応
# 例: 予選 開催期間... / 日付 レース 距離 天候 / 予選5R ...
# ============================================================
_v42_base_result_layout_meta = _v41_result_layout_meta


def _v41_result_layout_meta(text, venue_override="", race_no_override=""):
    """結果ページ冒頭の開催情報表を優先して解析する。"""
    meta = _v42_base_result_layout_meta(text, venue_override, race_no_override)
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")

    # 冒頭のレース種別と開催期間。
    first = re.search(
        r"(?m)^\s*([^\t\n]+?)\s+開催期間[：:]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日",
        src,
    )
    if first:
        race_type = first.group(1).strip()
        if race_type:
            meta["レース種別"] = race_type
            meta["レース名"] = race_type
        year = int(first.group(2))
    else:
        ym = re.search(r"開催期間[：:]\s*(20\d{2})年", src)
        year = int(ym.group(1)) if ym else None

    # 開催日目。
    day_m = re.search(r"開催\s*第\s*(\d+)\s*日目", src)
    if day_m:
        meta["開催日目"] = int(day_m.group(1))

    # 日付・レース・距離・天候の値行。
    row = re.search(
        r"日付\s*[\t ]+レース\s*[\t ]+距離\s*[\t ]+天候\s*\n"
        r"\s*(\d{1,2})月(\d{1,2})日\([^\n]*\)\s*[\t ]+([^\t\n]*?)(\d{1,2})R\s*[\t ]+(\d+)m\s*[\t ]+(晴|曇|雨|小雨|雪)",
        src,
    )
    if row:
        if year:
            meta["開催日"] = f"{year:04d}-{int(row.group(1)):02d}-{int(row.group(2)):02d}"
        label = row.group(3).strip()
        meta["レース"] = int(row.group(4))
        meta["距離"] = int(row.group(5))
        meta["天候"] = row.group(6)
        if label:
            meta["レース種別"] = label
            meta["レース名"] = label

    # 気象・走路表。タブ、連続空白の両方に対応。
    weather_row = re.search(
        r"気温\s*[\t ]+湿度\s*[\t ]+走路温度\s*[\t ]+走路状況\s*\n"
        r"\s*(-?\d+(?:\.\d+)?)℃\s*[\t ]+(-?\d+(?:\.\d+)?)%\s*[\t ]+"
        r"(-?\d+(?:\.\d+)?)℃\s*[\t ]+(良走路|湿走路|斑走路|風走路|荒走路)",
        src,
    )
    if weather_row:
        meta["気温"] = float(weather_row.group(1))
        meta["湿度"] = float(weather_row.group(2))
        meta["走路温度"] = float(weather_row.group(3))
        meta["走路状態"] = weather_row.group(4)

    # 締切・発走予定。変更表記は無視して時刻だけ保存。
    close_m = re.search(r"投票締切\s*(\d{1,2}:\d{2})", src)
    if close_m:
        meta["投票締切"] = close_m.group(1)
    start_m = re.search(r"発走予定\s*(\d{1,2}:\d{2})", src)
    if start_m:
        meta["発走時刻"] = start_m.group(1)

    # 補助入力を最後に再適用。
    if str(race_no_override).strip():
        meta["レース"] = v15_int(race_no_override)
    if str(venue_override).strip():
        meta["開催場"] = str(venue_override).strip()
    return meta


# Ver43: 結果完成形式（早見列を含む8車結果・払戻・周回順位）対応強化


# ============================================================
# Ver46: 選手単位の一括削除 / レース名差による重複統合
# ============================================================
def v46_cleanup_player_identity_duplicates(db_path=DB_PATH, player_name=None):
    """選手名＋日付＋場＋Rが同じ履歴を、レース名に関係なく1件へ統合する。

    race_history と v15_player_history_imports の両方を整理する。
    """
    mount_and_init_db()
    merged_histories = 0
    merged_imports = 0
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")

        params = []
        where = ""
        if player_name:
            key = v32_player_name_key(player_name)
            rows = con.execute("SELECT player_id, player_name FROM players").fetchall()
            ids = [int(r["player_id"]) for r in rows if v32_player_name_key(r["player_name"]) == key]
            if not ids:
                return {"merged_histories": 0, "merged_imports": 0}
            where = "WHERE player_id IN (%s)" % ",".join("?" for _ in ids)
            params = ids

        rows = con.execute(f"SELECT * FROM race_history {where} ORDER BY history_id", params).fetchall()
        groups = {}
        for r in rows:
            rn = _v45_norm_race_no(r["race_no"])
            if rn is None:
                continue
            pname = con.execute("SELECT player_name FROM players WHERE player_id=?", (r["player_id"],)).fetchone()
            pkey = v32_player_name_key(pname[0] if pname else "")
            gk = (pkey, str(r["race_date"] or "").strip(), str(r["venue"] or "").strip(), rn)
            groups.setdefault(gk, []).append(r)
        for g in groups.values():
            if len(g) < 2:
                continue
            keep, merged = _v45_merge_history_rows(con, g)
            pname = con.execute("SELECT player_name FROM players WHERE player_id=?", (keep["player_id"],)).fetchone()
            new_key = _v45_identity_record_key(pname[0] if pname else "", keep["race_date"], keep["venue"], merged.get("race_no"))
            con.execute("""UPDATE race_history SET race_no=?, race_name=?, finish=?, starters=?, surface=?, handicap=?,
                         trial_time=?, race_time=?, start_time=?, result_status=?, use_for_model=?, record_key=?
                         WHERE history_id=?""",
                        (merged.get("race_no"), merged.get("race_name"), merged.get("finish"), merged.get("starters"),
                         merged.get("surface"), merged.get("handicap"), merged.get("trial_time"), merged.get("race_time"),
                         merged.get("start_time"), merged.get("result_status") or "通常", int(merged.get("use_for_model") or 0),
                         new_key, keep["history_id"]))
            merged_histories += len(g) - 1

        # 詳細履歴も、レース名・種別・車番に関係なく同一レースへ統合。
        info = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='v15_player_history_imports'").fetchone()
        if info:
            drows = con.execute("SELECT * FROM v15_player_history_imports ORDER BY created_at, history_key").fetchall()
            dgroups = {}
            requested = v32_player_name_key(player_name) if player_name else None
            for r in drows:
                pkey = v32_player_name_key(r["player_name"])
                if requested and pkey != requested:
                    continue
                rn = _v45_norm_race_no(r["race_no"])
                if rn is None:
                    continue
                gk = (pkey, str(r["race_date"] or "").strip(), str(r["venue"] or "").strip(), rn)
                dgroups.setdefault(gk, []).append(r)
            fields = ["player_name","race_no","race_name","rank","starters","surface","handicap","trial_time",
                      "race_time","st","raw_line","weather","track_temp","air_temp","humidity",
                      "race_type","distance","laps","popularity","car_no"]
            positive = {"race_no","rank","starters","handicap","trial_time","race_time","st","track_temp",
                        "air_temp","humidity","distance","laps","popularity","car_no"}
            for g in dgroups.values():
                if len(g) < 2:
                    continue
                keep = max(g, key=lambda r: sum(_v34_has_value(r[f], positive=f in positive) for f in fields))
                merged = {f: keep[f] for f in fields}
                for r in g:
                    if r["history_key"] == keep["history_key"]:
                        continue
                    for f in fields:
                        merged[f] = _v34_merge(merged[f], r[f], positive=f in positive)
                    con.execute("DELETE FROM v15_player_history_imports WHERE history_key=?", (r["history_key"],))
                    merged_imports += 1
                con.execute("""UPDATE v15_player_history_imports SET player_name=?, race_no=?, race_name=?, rank=?, starters=?,
                             surface=?, handicap=?, trial_time=?, race_time=?, st=?, raw_line=?, weather=?, track_temp=?,
                             air_temp=?, humidity=?, race_type=?, distance=?, laps=?, popularity=?, car_no=? WHERE history_key=?""",
                            tuple(merged[f] for f in fields) + (keep["history_key"],))
        con.commit()
    return {"merged_histories": merged_histories, "merged_imports": merged_imports}


def v46_delete_player_all(player_name, db_path=DB_PATH, delete_result_rows=False):
    """指定選手の登録情報を一括削除する。

    他選手やレース本体は保持し、外部キー参照はNULLへ戻す。
    delete_result_rows=True の場合だけ、結果スナップショット内の選手行も削除する。
    """
    name = _v27_norm_player_name(player_name)
    if not name:
        return {"deleted": False, "message": "選手名が空です。"}
    mount_and_init_db()
    counts = {}
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        players = con.execute("SELECT player_id, player_name FROM players").fetchall()
        matched = [r for r in players if v32_player_name_key(r["player_name"]) == v32_player_name_key(name)]
        player_ids = [int(r["player_id"]) for r in matched]
        stored_names = [str(r["player_name"]) for r in matched] or [name]

        def delete_by_name(table, column="player_name"):
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return 0
            total = 0
            rows = con.execute(f"SELECT rowid, {column} FROM {table}").fetchall()
            ids = [int(r[0]) for r in rows if v32_player_name_key(r[1]) == v32_player_name_key(name)]
            for rid in ids:
                con.execute(f"DELETE FROM {table} WHERE rowid=?", (rid,))
            return len(ids)

        # 名前ベースの履歴・入力・予測特徴。
        for table in ("v15_player_history_imports", "v15_race_entry_inputs", "prediction_features",
                      "prediction_feature_snapshots", "prediction_snapshots", "v40_prediction_feature_snapshots"):
            try:
                counts[table] = delete_by_name(table)
            except sqlite3.OperationalError:
                counts[table] = 0

        if player_ids:
            q = ",".join("?" for _ in player_ids)
            for table in ("player_lap_history",):
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    cur = con.execute(f"DELETE FROM {table} WHERE player_id IN ({q})", player_ids)
                    counts[table] = cur.rowcount
            for table in ("lap_features", "lap_history", "race_entries"):
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    cur = con.execute(f"UPDATE {table} SET player_id=NULL WHERE player_id IN ({q})", player_ids)
                    counts[table + "_unlinked"] = cur.rowcount
            cur = con.execute(f"DELETE FROM race_history WHERE player_id IN ({q})", player_ids)
            counts["race_history"] = cur.rowcount
            cur = con.execute(f"DELETE FROM players WHERE player_id IN ({q})", player_ids)
            counts["players"] = cur.rowcount

        if delete_result_rows:
            for table in ("result_entries",):
                try:
                    counts[table] = delete_by_name(table)
                except sqlite3.OperationalError:
                    counts[table] = 0

        con.commit()
    total = sum(v for k, v in counts.items() if isinstance(v, int) and not k.endswith("_unlinked"))
    return {"deleted": total > 0, "message": f"{name} の選手情報を一括削除しました。", "counts": counts}


# ============================================================
# Ver47: 選手履歴の必須項目検証・不足行保留
# ============================================================
def _v47_normalize_date(value):
    """日付を YYYY-MM-DD に統一。不正・空欄は None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s in {"", "-", "—", "–", "―", "None", "nan", "NaT"}:
        return None
    s = s.replace("年", "/").replace("月", "/").replace("日", "")
    s = s.replace(".", "/").replace("-", "/")
    try:
        parts = [p for p in s.split("/") if p != ""]
        if len(parts) == 3 and len(parts[0]) == 2:
            parts[0] = "20" + parts[0]
            s = "/".join(parts)
        dt = pd.to_datetime(s, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _v47_normalize_venue(value):
    s = "" if value is None else str(value).strip()
    if s in {"", "-", "—", "–", "―", "None", "nan"}:
        return None
    aliases = {"川口":"川口", "伊勢崎":"伊勢崎", "浜松":"浜松", "飯塚":"飯塚", "山陽":"山陽"}
    return aliases.get(s, s)


def _v47_normalize_race_no(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    m = re.search(r"(\d{1,2})", str(value))
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 12 else None


def v47_validate_player_history(df):
    """必須項目を正規化し、有効行・保留行へ分ける。"""
    if df is None:
        df = pd.DataFrame()
    work = df.copy().reset_index(drop=True)
    for col in ["選手名", "開催日", "開催場", "レース", "レース名"]:
        if col not in work.columns:
            work[col] = None
    work["開催日"] = work["開催日"].map(_v47_normalize_date)
    work["開催場"] = work["開催場"].map(_v47_normalize_venue)
    work["レース"] = work["レース"].map(_v47_normalize_race_no)
    reasons=[]
    for i,row in work.iterrows():
        miss=[]
        if not str(row.get("選手名") or "").strip(): miss.append("選手名")
        if not row.get("開催日"): miss.append("日付")
        if not row.get("開催場"): miss.append("開催場")
        race_missing = row.get("レース") is None or pd.isna(row.get("レース"))
        race_name = str(row.get("レース名") or "").strip()
        if race_name in {"", "-", "—", "–", "―", "None", "nan"}:
            race_name = ""
        if race_missing and not race_name:
            miss.append("Rまたはレース名")
        reasons.append("・".join(miss))
    work["保留理由"] = reasons
    valid = work[work["保留理由"] == ""].drop(columns=["保留理由"]).copy()
    pending = work[work["保留理由"] != ""].copy()
    return valid.reset_index(drop=True), pending.reset_index(drop=True)


def v47_save_player_history(df, db_path=DB_PATH):
    """有効行だけ保存し、不足行は保留として返す。"""
    valid, pending = v47_validate_player_history(df)
    changed=skipped=0
    if not valid.empty:
        changed, skipped = v15_save_player_history(valid, db_path=db_path)
    return {
        "read": int(len(df) if df is not None else 0),
        "changed": int(changed),
        "skipped": int(skipped),
        "pending_count": int(len(pending)),
        "pending": pending,
        "valid": valid,
    }


def v47_player_history_breakdown(player_name, db_path=DB_PATH):
    """選手履歴の利用可・除外理由・期間を返す。"""
    result={"total":0,"usable":0,"excluded":0,"latest":None,"oldest":None,"reasons":{}}
    if not player_name or not Path(db_path).exists():
        return result
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory=sqlite3.Row
        p=_v32_find_player(con, _v27_norm_player_name(player_name))
        if not p: return result
        rows=con.execute("SELECT race_date,use_for_model,result_status FROM race_history WHERE player_id=?",(p[0],)).fetchall()
    result["total"]=len(rows)
    dates=[]
    for r in rows:
        if r["race_date"]: dates.append(str(r["race_date"]))
        if int(r["use_for_model"] or 0)==1: result["usable"]+=1
        else:
            result["excluded"]+=1
            reason=str(r["result_status"] or "その他")
            result["reasons"][reason]=result["reasons"].get(reason,0)+1
    if dates:
        result["latest"]=max(dates); result["oldest"]=min(dates)
    return result

# ============================================================
# Ver50: 重み影響の見える化・レース名自動補完強化
# ============================================================
def v50_normalize_race_name(value):
    """レース名の空白・全半角・よくある表記ゆれを整える。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(value or "")).strip()
    s = re.sub(r"\s+", "", s)
    if s in {"", "-", "—", "–", "―", "nan", "None"}:
        return None
    replacements = {
        "一般": "一般戦", "準々決勝": "準々決勝戦",
        "準決": "準決勝戦", "準決勝": "準決勝戦",
        "優勝": "優勝戦", "特一般": "特別一般戦",
    }
    return replacements.get(s, s)


def v50_infer_race_name(raw, race_type=None):
    """貼付行・縦型ブロックから大会名ではなくレース区分を推定する。"""
    candidate = v50_normalize_race_name(race_type)
    if candidate:
        return candidate
    text = str(raw or "")
    patterns = [
        # 接頭語を含む複合名称を先に拾う。例: ランチアタック準々決勝戦
        r"([^\n\t]{0,24}準々決勝戦[ABＡＢＣC]?)",
        r"([^\n\t]{0,24}準決勝戦[ABＡＢＣC]?)",
        r"([^\n\t]{0,24}(?:マイスター選抜|特別選抜戦?|選抜予選|選抜戦|特別一般戦))",
        r"(準々決勝[ABＡＢＣC]?|準決勝[ABＡＢＣC]?|準決[ABＡＢＣC]?)",
        r"(一次予選|二次予選|予選[ABＡＢＣC]?|一般戦|優勝戦)",
        r"(最終予選|予選選抜|特別予選|一般選抜|順位決定戦?)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            value = re.sub(r"^[0-9０-９]+走前\s*", "", m.group(1)).strip()
            value = re.sub(r"^(?:川口|伊勢崎|浜松|山陽|飯塚)\s*", "", value)
            return v50_normalize_race_name(value)

    # 1行全体がレース区分らしい場合は、未知の冠名付きでもそのまま採用する。
    for raw_line in text.splitlines():
        line = v50_normalize_race_name(raw_line)
        if not line or len(line) > 40:
            continue
        if re.search(r"(?:予選|一般戦?|準々決勝戦?|準決勝戦?|優勝戦?|選抜|特選|順位決定|ランチアタック)", line):
            if not re.fullmatch(r"(?:川口|伊勢崎|浜松|山陽|飯塚)", line):
                return line
    return None


_v50_parse_player_history_original = v15_parse_player_history
def v15_parse_player_history(text, player_name=None):
    df = _v50_parse_player_history_original(text, player_name=player_name)
    if df is None or df.empty:
        return df
    out = df.copy()
    if "レース名" not in out.columns:
        out["レース名"] = None
    if "レース種別" not in out.columns:
        out["レース種別"] = None
    if "_raw" not in out.columns:
        out["_raw"] = ""
    for idx in out.index:
        current = v50_normalize_race_name(out.at[idx, "レース名"])
        inferred = v50_infer_race_name(out.at[idx, "_raw"], out.at[idx, "レース種別"])
        out.at[idx, "レース名"] = current or inferred
        # レース種別側しか無い貼付形式も、保存時にレース名へ反映する。
        if not v50_normalize_race_name(out.at[idx, "レース種別"]) and out.at[idx, "レース名"]:
            out.at[idx, "レース種別"] = out.at[idx, "レース名"]
    return out


_v50_apply_weights_original = v40_apply_adaptive_weights
def v40_apply_adaptive_weights(df, db_path=DB_PATH):
    out = _v50_apply_weights_original(df, db_path=db_path)
    weights = v40_get_weights(db_path)
    features = v40_feature_frame(out)
    comments = []
    for idx in out.index:
        before_rank = int(out.at[idx, "調整前順位"])
        after_rank = int(out.at[idx, "改善後順位"])
        move = before_rank - after_rank
        contributions = []
        for name in V40_DEFAULT_WEIGHTS:
            contribution = float(weights[name] * (features.loc[idx, name] - 0.5) * 6.0)
            contributions.append((name, contribution, float(weights[name])))
        contributions.sort(key=lambda x: abs(x[1]), reverse=True)
        detail = "、".join(
            f"{name}{value:+.2f}点(重み{weight:.3f})"
            for name, value, weight in contributions[:3]
        )
        if move > 0:
            head = f"重み補正で{move}位上昇"
        elif move < 0:
            head = f"重み補正で{abs(move)}位下降"
        else:
            head = "順位変化なし"
        comments.append(f"{head}。主因: {detail}")
    out["重み調整コメント"] = comments
    return out


def _v50_softmax(values, temperature=1.0):
    x = np.asarray(values, dtype=float)
    x = np.nan_to_num(x, nan=np.nanmedian(x) if np.isfinite(x).any() else 0.0)
    scale = np.nanstd(x)
    if not np.isfinite(scale) or scale < 1e-9:
        scale = 1.0
    z = (x - np.nanmax(x)) / (scale * max(0.2, float(temperature)))
    e = np.exp(np.clip(z, -40, 0))
    return e / max(e.sum(), 1e-12)


def _v50_plackett_luce_trifecta(cars, strengths):
    rows = []
    cars = [int(c) for c in cars]
    s = np.asarray(strengths, dtype=float)
    for i, a in enumerate(cars):
        p1 = s[i] / s.sum()
        rem1 = s.sum() - s[i]
        for j, b in enumerate(cars):
            if j == i: continue
            p2 = s[j] / rem1
            rem2 = rem1 - s[j]
            for k, c in enumerate(cars):
                if k in (i, j): continue
                p3 = s[k] / rem2
                rows.append((f"{a}-{b}-{c}", p1*p2*p3*100.0))
    return dict(rows)


def v50_weight_impact_summary(df, top_n=20):
    """重み適用前後の順位と、スコアから算出した診断用確率差を返す。"""
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    before = pd.to_numeric(work.get("調整前総合点"), errors="coerce").fillna(0.0)
    after = pd.to_numeric(work.get("改善後総合点"), errors="coerce").fillna(0.0)
    pb = _v50_softmax(before, 1.15)
    pa = _v50_softmax(after, 1.15)
    # 3着内は独立確率ではなく、相対強度からの簡易近似。比較用途に限定。
    top3_b = np.clip(pb * 3.0, 0, 1) * 100
    top3_a = np.clip(pa * 3.0, 0, 1) * 100
    rows=[]
    for pos, (_, r) in enumerate(work.iterrows()):
        br=int(r.get("調整前順位",0)); ar=int(r.get("改善後順位",0))
        rows.append({
            "車": int(r.get("車", r.get("車番", 0))), "選手名": str(r.get("選手名", "")),
            "調整前順位": br, "調整後順位": ar, "順位変化": br-ar,
            "推定1着率_調整前": pb[pos]*100, "推定1着率_調整後": pa[pos]*100,
            "1着率変化": (pa[pos]-pb[pos])*100,
            "推定3着内率_調整前": top3_b[pos], "推定3着内率_調整後": top3_a[pos],
            "3着内率変化": top3_a[pos]-top3_b[pos],
            "コメント": r.get("重み調整コメント", r.get("主な評価理由", "")),
        })
    return pd.DataFrame(rows).sort_values(["調整後順位","車"]).head(int(top_n)).reset_index(drop=True)


def v50_trifecta_weight_impact(df, limit=20):
    """重み前後の三連単確率差をPlackett-Luce近似で比較する診断表。"""
    if df is None or len(df) < 3:
        return pd.DataFrame()
    cars = pd.to_numeric(df.get("車", df.get("車番")), errors="coerce").fillna(0).astype(int).tolist()
    before = _v50_softmax(pd.to_numeric(df.get("調整前総合点"), errors="coerce").fillna(0.0), 1.15)
    after = _v50_softmax(pd.to_numeric(df.get("改善後総合点"), errors="coerce").fillna(0.0), 1.15)
    b = _v50_plackett_luce_trifecta(cars, before)
    a = _v50_plackett_luce_trifecta(cars, after)
    rows=[]
    for combo in set(b)|set(a):
        rows.append({"組み合わせ":combo,"調整前確率":b.get(combo,0.0),"調整後確率":a.get(combo,0.0),"確率変化":a.get(combo,0.0)-b.get(combo,0.0)})
    frame=pd.DataFrame(rows)
    frame["上昇幅順位"] = frame["確率変化"].rank(method="min", ascending=False).astype(int)
    frame["調整後順位"] = frame["調整後確率"].rank(method="min", ascending=False).astype(int)
    return frame.sort_values(["確率変化","調整後確率"], ascending=False).head(int(limit)).reset_index(drop=True)


# ============================================================
# Ver51: 大会名を別保存し、正式レース名が無い場合だけ補完
# ============================================================
def v51_normalize_tournament_name(value):
    """大会名の空白と全半角を整える。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(value or "")).strip()
    s = re.sub(r"\s+", "", s)
    if s in {"", "-", "—", "–", "―", "nan", "None"}:
        return None
    return s


def v51_extract_tournament_name(raw):
    """縦型・表形式の元テキストから大会名を抽出する。

    予選、一般戦、準決勝戦などのレース区分は除外する。
    """
    text = str(raw or "")
    lines = [v51_normalize_tournament_name(x) for x in text.splitlines()]
    race_words = ("一般戦", "一般", "予選", "準々決勝", "準決", "決勝", "優勝戦", "優勝", "選抜", "特選", "マイスター", "ランチアタック")
    strong_patterns = (
        r"(?:特別)?SG.+", r"(?:特別)?G[ⅠⅡⅢI123].+",
        r".+(?:記念|選手権|オールスター|グランプリ|王座決定戦|王座|杯|カップ|フェスタ)$",
        r"(?:オーバー)?ミッドナイト(?:オートレース)?",
    )
    for line in lines:
        if not line or any(w in line for w in race_words):
            continue
        if re.fullmatch(r"(?:川口|伊勢崎|浜松|山陽|飯塚)", line):
            continue
        if any(re.fullmatch(p, line, flags=re.IGNORECASE) for p in strong_patterns):
            return line
    return None


def v51_ensure_tournament_columns(db_path=DB_PATH):
    """既存DBを壊さず大会名列を追加する。"""
    mount_and_init_db()
    v15_init_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        for table in ("race_history", "v15_player_history_imports"):
            cols = set(v15_columns(con, table))
            if "tournament_name" not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN tournament_name TEXT")
        con.commit()


_v51_parse_player_history_original = v15_parse_player_history
def v15_parse_player_history(text, player_name=None):
    """大会名を別列へ保存し、レース名欠損時だけ大会名で補完する。"""
    df = _v51_parse_player_history_original(text, player_name=player_name)
    if df is None or df.empty:
        return df
    out = df.copy()
    if "大会名" not in out.columns:
        out["大会名"] = None
    if "レース名" not in out.columns:
        out["レース名"] = None
    if "_raw" not in out.columns:
        out["_raw"] = ""
    for idx in out.index:
        tournament = v51_extract_tournament_name(out.at[idx, "_raw"])
        out.at[idx, "大会名"] = tournament
        # 予選・一般戦・準決勝戦などが取れている場合は必ずそちらを優先。
        race_name = v50_normalize_race_name(out.at[idx, "レース名"])
        if not race_name and tournament:
            out.at[idx, "レース名"] = tournament
    return out


_v51_save_player_history_original = v15_save_player_history
def v15_save_player_history(df, db_path=DB_PATH):
    """従来保存後に大会名を対応する履歴へ追記する。"""
    v51_ensure_tournament_columns(db_path)
    if df is None or df.empty:
        return _v51_save_player_history_original(df, db_path=db_path)

    work = df.copy()
    if "大会名" not in work.columns:
        work["大会名"] = None
    if "レース名" not in work.columns:
        work["レース名"] = None
    if "_raw" not in work.columns:
        work["_raw"] = ""
    for idx in work.index:
        tournament = v51_normalize_tournament_name(work.at[idx, "大会名"]) or v51_extract_tournament_name(work.at[idx, "_raw"])
        work.at[idx, "大会名"] = tournament
        if not v50_normalize_race_name(work.at[idx, "レース名"]) and tournament:
            work.at[idx, "レース名"] = tournament

    result = _v51_save_player_history_original(work, db_path=db_path)

    # 大会名は重複判定には使わず、同定済みの正規履歴・詳細履歴へ別列で保存する。
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        for _, row in work.iterrows():
            tournament = v51_normalize_tournament_name(row.get("大会名"))
            if not tournament:
                continue
            name = _v27_norm_player_name(row.get("選手名"))
            race_date = row.get("開催日")
            venue = row.get("開催場")
            race_no = None if pd.isna(row.get("レース")) else int(row.get("レース"))
            player = _v32_find_player(con, name)
            if not player:
                continue
            player_id = player[0]
            candidates = con.execute(
                "SELECT * FROM race_history WHERE player_id=? AND race_date=? AND venue=? ORDER BY history_id DESC",
                (player_id, race_date, venue),
            ).fetchall()
            target = None
            if race_no is not None:
                target = next((r for r in candidates if _v45_norm_race_no(r["race_no"]) == race_no), None)
            if target is None:
                incoming = {
                    "finish": None if pd.isna(row.get("着順")) else row.get("着順"),
                    "starters": None if pd.isna(row.get("出走")) else row.get("出走"),
                    "handicap": None if pd.isna(row.get("ハンデ")) else row.get("ハンデ"),
                    "trial_time": None if pd.isna(row.get("試走T")) else row.get("試走T"),
                    "race_time": None if pd.isna(row.get("競走T")) else row.get("競走T"),
                    "start_time": None if pd.isna(row.get("ST")) else row.get("ST"),
                }
                target = _v48_find_same_race_without_r(candidates, incoming)
            if target is not None:
                con.execute("UPDATE race_history SET tournament_name=? WHERE history_id=?", (tournament, target["history_id"]))

            details = con.execute(
                "SELECT * FROM v15_player_history_imports WHERE race_date=? AND venue=? ORDER BY created_at DESC",
                (race_date, venue),
            ).fetchall()
            detail_target = None
            for d in details:
                if v32_player_name_key(d["player_name"]) != v32_player_name_key(name):
                    continue
                if race_no is not None and d["race_no"] is not None and int(d["race_no"]) == race_no:
                    detail_target = d; break
                incoming_d = {
                    "finish": None if pd.isna(row.get("着順")) else row.get("着順"),
                    "starters": None if pd.isna(row.get("出走")) else row.get("出走"),
                    "handicap": None if pd.isna(row.get("ハンデ")) else row.get("ハンデ"),
                    "trial_time": None if pd.isna(row.get("試走T")) else row.get("試走T"),
                    "race_time": None if pd.isna(row.get("競走T")) else row.get("競走T"),
                    "start_time": None if pd.isna(row.get("ST")) else row.get("ST"),
                }
                if _v48_numeric_identity_match(d, incoming_d):
                    detail_target = d; break
            if detail_target is not None:
                con.execute("UPDATE v15_player_history_imports SET tournament_name=? WHERE history_key=?", (tournament, detail_target["history_key"]))
        con.commit()
    return result

# ============================================================
# Ver55: Rなしで同日・同開催場・同レース名が重なる場合は保留
# ============================================================
def _v55_clean_text(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return "" if s in {"", "-", "—", "–", "―", "None", "nan", "NaT"} else s


def _v55_race_name_key(value):
    """比較専用。一般/一般戦など既存の正規化規則を利用する。"""
    normalized = v50_normalize_race_name(value)
    return _v55_clean_text(normalized)


def _v55_player_key(value):
    try:
        return v32_player_name_key(_v27_norm_player_name(value))
    except Exception:
        return re.sub(r"\s+", "", _v55_clean_text(value))


def _v55_ambiguous_no_r_indices(df, db_path=DB_PATH):
    """Rなし行のうち、同日・同場・同レース名で候補が重なる行番号を返す。

    ・貼り付け内に同じ識別情報の行が2件以上ある
    ・DBに同じ選手・日付・場・レース名の履歴が既にある
    のどちらかなら、自動更新せずR入力待ちにする。
    """
    if df is None or df.empty:
        return set()

    work = df.copy().reset_index(drop=True)
    for col in ["選手名", "開催日", "開催場", "レース", "レース名"]:
        if col not in work.columns:
            work[col] = None

    # 基本項目を保存処理と同じ形へ寄せる。
    work["開催日"] = work["開催日"].map(_v47_normalize_date)
    work["開催場"] = work["開催場"].map(_v47_normalize_venue)
    work["レース"] = work["レース"].map(_v47_normalize_race_no)

    keys = []
    for _, row in work.iterrows():
        race_no = row.get("レース")
        no_r = race_no is None or (isinstance(race_no, float) and pd.isna(race_no))
        name_key = _v55_player_key(row.get("選手名"))
        date = _v55_clean_text(row.get("開催日"))
        venue = _v55_clean_text(row.get("開催場"))
        race_name = _v55_race_name_key(row.get("レース名"))
        keys.append((no_r, name_key, date, venue, race_name))

    ambiguous = set()

    # 貼り付け内の重複候補。
    counts = {}
    for key in keys:
        no_r, name_key, date, venue, race_name = key
        if no_r and name_key and date and venue and race_name:
            identity = (name_key, date, venue, race_name)
            counts[identity] = counts.get(identity, 0) + 1
    for idx, key in enumerate(keys):
        no_r, name_key, date, venue, race_name = key
        if no_r and counts.get((name_key, date, venue, race_name), 0) >= 2:
            ambiguous.add(idx)

    # DB内の既存候補。DBが無い場合は貼り付け内判定だけで終了。
    if not Path(db_path).exists():
        return ambiguous

    try:
        v15_init_tables(db_path)
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            for idx, key in enumerate(keys):
                no_r, name_key, date, venue, race_name = key
                if not (no_r and name_key and date and venue and race_name):
                    continue
                player = _v32_find_player(con, _v27_norm_player_name(work.at[idx, "選手名"]))
                if not player:
                    continue
                rows = con.execute(
                    "SELECT race_name FROM race_history WHERE player_id=? AND race_date=? AND venue=?",
                    (player[0], date, venue),
                ).fetchall()
                if any(_v55_race_name_key(r["race_name"]) == race_name for r in rows):
                    ambiguous.add(idx)
    except Exception:
        # 判定補助で登録全体を止めない。通常の保存処理側の整合性チェックは残る。
        pass

    return ambiguous


_v55_v47_save_player_history_original = v47_save_player_history
def v47_save_player_history(df, db_path=DB_PATH):
    """同日・同場・同レース名が重なるRなし行を、上書きせず保留へ回す。"""
    if df is None:
        df = pd.DataFrame()
    work = df.copy().reset_index(drop=True)
    ambiguous = _v55_ambiguous_no_r_indices(work, db_path=db_path)

    if not ambiguous:
        return _v55_v47_save_player_history_original(work, db_path=db_path)

    save_part = work.drop(index=sorted(ambiguous)).reset_index(drop=True)
    hold_part = work.loc[sorted(ambiguous)].copy().reset_index(drop=True)
    hold_part["保留理由"] = "同日・同開催場・同レース名の候補あり：Rを入力"

    result = _v55_v47_save_player_history_original(save_part, db_path=db_path)
    original_pending = result.get("pending")
    if original_pending is None or not isinstance(original_pending, pd.DataFrame):
        original_pending = pd.DataFrame()
    combined = pd.concat([original_pending, hold_part], ignore_index=True, sort=False)

    result["read"] = int(len(work))
    result["pending"] = combined
    result["pending_count"] = int(len(combined))
    return result


# ============================================================
# Ver56: R候補表示・結果開催場の市営見出し優先取得
# ============================================================
def _v56_result_venue_from_header(text):
    """結果ページの開催見出しから開催場を取得する。選手所属LGより優先。"""
    s = str(text or "")
    patterns = [
        (r"(?:令和[^\n]*?)?川口市営", "川口"),
        (r"(?:令和[^\n]*?)?伊勢崎市営", "伊勢崎"),
        (r"(?:令和[^\n]*?)?浜松市営", "浜松"),
        (r"(?:令和[^\n]*?)?(?:山陽小野田|山陽)市営", "山陽"),
        (r"(?:令和[^\n]*?)?飯塚市営", "飯塚"),
    ]
    for pat, venue in patterns:
        if re.search(pat, s):
            return venue
    # 市営表記がない大会見出しの補助。着順表以降は選手所属なので見ない。
    head = s.split("着順", 1)[0]
    for venue in ["川口", "伊勢崎", "浜松", "山陽", "飯塚"]:
        if re.search(rf"(?:開催|{venue}記念|{venue}オート|\b){venue}", head):
            return venue
    return None


_v56_parse_meta_original = _v35_parse_meta
def _v35_parse_meta(text, venue_override="", race_no_override=""):
    meta = _v56_parse_meta_original(text, venue_override, race_no_override)
    # 手動指定が最優先。未指定なら市営見出しから再確定する。
    if str(venue_override or "").strip():
        meta["開催場"] = str(venue_override).strip()
    else:
        header_venue = _v56_result_venue_from_header(text)
        if header_venue:
            meta["開催場"] = header_venue
    return meta


def _v56_candidate_races_for_row(row, db_path=DB_PATH):
    """Rなし保留行に表示する既存R候補を返す。自由入力用の補助情報。"""
    name = _v27_norm_player_name(row.get("選手名"))
    date = _v47_normalize_date(row.get("開催日"))
    venue = _v47_normalize_venue(row.get("開催場"))
    race_name_key = _v55_race_name_key(row.get("レース名"))
    if not (name and date and venue and race_name_key and Path(db_path).exists()):
        return []
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            player = _v32_find_player(con, name)
            if not player:
                return []
            rows = con.execute(
                "SELECT race_no, race_name FROM race_history WHERE player_id=? AND race_date=? AND venue=? ORDER BY race_no, history_id",
                (player[0], date, venue),
            ).fetchall()
        candidates=[]
        for r in rows:
            if _v55_race_name_key(r["race_name"]) != race_name_key:
                continue
            rn = _v47_normalize_race_no(r["race_no"])
            label = f"{rn}R" if rn is not None else "R未設定"
            if label not in candidates:
                candidates.append(label)
        return candidates
    except Exception:
        return []


def v56_add_r_candidates(pending, db_path=DB_PATH):
    if pending is None or not isinstance(pending, pd.DataFrame) or pending.empty:
        return pending
    out = pending.copy()
    labels=[]
    for _, row in out.iterrows():
        cands = _v56_candidate_races_for_row(row, db_path=db_path)
        labels.append(" / ".join(cands) if cands else "候補なし（新規Rを入力）")
    out["R候補"] = labels
    return out


_v56_v47_save_original = v47_save_player_history
def v47_save_player_history(df, db_path=DB_PATH):
    result = _v56_v47_save_original(df, db_path=db_path)
    result["pending"] = v56_add_r_candidates(result.get("pending"), db_path=db_path)
    result["pending_count"] = int(len(result["pending"])) if isinstance(result.get("pending"), pd.DataFrame) else 0
    return result

# ============================================================
# Ver57: 数値完全一致の重複登録防止・既存データ一括統合
# ============================================================
def _v57_exact_numeric_signature_from_values(player_name, race_date, venue, finish, handicap, trial, race_time, st):
    """レース名・Rに依存しない、完全一致走行の比較キー。"""
    values = (
        _v33_norm_number(finish, 0),
        _v33_norm_number(handicap, 0),
        _v33_norm_number(trial, 3),
        _v33_norm_number(race_time, 3),
        _v33_norm_number(st, 3),
    )
    # 試走・競走の両方と、合計4項目以上がない行は誤統合防止のため対象外。
    if values[2] is None or values[3] is None or sum(v is not None for v in values) < 4:
        return None
    return (
        _v55_player_key(player_name),
        _v55_clean_text(_v47_normalize_date(race_date)),
        _v55_clean_text(_v47_normalize_venue(venue)),
        *values,
    )


def _v57_exact_signature_from_input_row(row):
    return _v57_exact_numeric_signature_from_values(
        row.get("選手名"), row.get("開催日"), row.get("開催場"),
        row.get("着順"), row.get("ハンデ"), row.get("試走T"),
        row.get("競走T"), row.get("ST"),
    )


def _v57_existing_exact_signatures(db_path=DB_PATH):
    signatures = set()
    if not Path(db_path).exists():
        return signatures
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        if v15_table_exists(con, "race_history") and v15_table_exists(con, "players"):
            rows = con.execute("""
                SELECT p.player_name, h.race_date, h.venue, h.finish, h.handicap,
                       h.trial_time, h.race_time, h.start_time
                FROM race_history h JOIN players p ON p.player_id=h.player_id
            """).fetchall()
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["finish"],
                    r["handicap"], r["trial_time"], r["race_time"], r["start_time"]
                )
                if sig is not None:
                    signatures.add(sig)
        if v15_table_exists(con, "v15_player_history_imports"):
            rows = con.execute("""
                SELECT player_name, race_date, venue, rank, handicap,
                       trial_time, race_time, st
                FROM v15_player_history_imports
            """).fetchall()
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["rank"],
                    r["handicap"], r["trial_time"], r["race_time"], r["st"]
                )
                if sig is not None:
                    signatures.add(sig)
    return signatures


def _v57_merge_row_values(rows, fields, positive_fields=None):
    positive_fields = set(positive_fields or [])
    # 情報量が多い行を土台にする。
    def score(row):
        return sum(_v34_has_value(row[f], positive=f in positive_fields) for f in fields if f in row.keys())
    keep = max(rows, key=score)
    merged = dict(keep)
    for row in rows:
        for f in fields:
            if f in row.keys():
                merged[f] = _v34_merge(merged.get(f), row[f], positive=f in positive_fields)
    return keep, merged


def v57_cleanup_exact_numeric_duplicates(db_path=DB_PATH):
    """DB内の数値完全一致履歴を一括統合し、情報がある列を残す。"""
    mount_and_init_db()
    merged_histories = 0
    merged_imports = 0
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")

        if v15_table_exists(con, "race_history") and v15_table_exists(con, "players"):
            rows = con.execute("""
                SELECT h.*, p.player_name FROM race_history h
                JOIN players p ON p.player_id=h.player_id
                ORDER BY h.created_at, h.history_id
            """).fetchall()
            groups = {}
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["finish"],
                    r["handicap"], r["trial_time"], r["race_time"], r["start_time"]
                )
                if sig is not None:
                    groups.setdefault(sig, []).append(r)
            fields = ["race_no", "race_name", "finish", "starters", "surface", "handicap",
                      "trial_time", "race_time", "start_time", "result_status", "use_for_model", "source"]
            positives = {"race_no", "finish", "starters", "trial_time", "race_time", "start_time"}
            for group in groups.values():
                if len(group) < 2:
                    continue
                keep, merged = _v57_merge_row_values(group, fields, positives)
                # UNIQUE制約との衝突を避けるため、先に余分な行を削除。
                for r in group:
                    if r["history_id"] != keep["history_id"]:
                        con.execute("DELETE FROM race_history WHERE history_id=?", (r["history_id"],))
                        merged_histories += 1
                fallback = _v33_history_signature({
                    "race_date": keep["race_date"], "venue": keep["venue"],
                    "finish": merged.get("finish"), "handicap": merged.get("handicap"),
                    "trial_time": merged.get("trial_time"), "race_time": merged.get("race_time"),
                    "start_time": merged.get("start_time"),
                })
                record_key = _v45_identity_record_key(
                    keep["player_name"], keep["race_date"], keep["venue"], merged.get("race_no"), fallback=fallback
                )
                con.execute("""
                    UPDATE race_history SET race_no=?, race_name=?, finish=?, starters=?, surface=?, handicap=?,
                        trial_time=?, race_time=?, start_time=?, result_status=?, use_for_model=?, source=?, record_key=?
                    WHERE history_id=?
                """, tuple(merged.get(f) for f in fields) + (record_key, keep["history_id"]))

        if v15_table_exists(con, "v15_player_history_imports"):
            rows = con.execute("SELECT * FROM v15_player_history_imports ORDER BY created_at, history_key").fetchall()
            groups = {}
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["rank"],
                    r["handicap"], r["trial_time"], r["race_time"], r["st"]
                )
                if sig is not None:
                    groups.setdefault(sig, []).append(r)
            all_cols = [r[1] for r in con.execute("PRAGMA table_info(v15_player_history_imports)").fetchall()]
            fields = [c for c in all_cols if c not in {"history_key", "created_at"}]
            positives = {"race_no", "rank", "starters", "handicap", "trial_time", "race_time", "st",
                         "track_temp", "air_temp", "humidity", "distance", "laps", "popularity", "car_no"}
            for group in groups.values():
                if len(group) < 2:
                    continue
                keep, merged = _v57_merge_row_values(group, fields, positives)
                for r in group:
                    if r["history_key"] != keep["history_key"]:
                        con.execute("DELETE FROM v15_player_history_imports WHERE history_key=?", (r["history_key"],))
                        merged_imports += 1
                assignments = ", ".join(f'"{f}"=?' for f in fields)
                con.execute(
                    f'UPDATE v15_player_history_imports SET {assignments} WHERE history_key=?',
                    tuple(merged.get(f) for f in fields) + (keep["history_key"],)
                )
        con.commit()
    return {"merged_histories": merged_histories, "merged_imports": merged_imports}


_v57_v47_save_original = v47_save_player_history
def v47_save_player_history(df, db_path=DB_PATH):
    """数値完全一致行は新規登録せず、既存行への補完または重複スキップにする。"""
    if df is None:
        df = pd.DataFrame()
    work = df.copy().reset_index(drop=True)
    if work.empty:
        return _v57_v47_save_original(work, db_path=db_path)

    existing = _v57_existing_exact_signatures(db_path)
    seen_in_batch = set()
    keep_indices = []
    exact_skipped = 0
    for idx, row in work.iterrows():
        sig = _v57_exact_signature_from_input_row(row)
        if sig is not None and (sig in existing or sig in seen_in_batch):
            exact_skipped += 1
            continue
        keep_indices.append(idx)
        if sig is not None:
            seen_in_batch.add(sig)

    save_part = work.loc[keep_indices].reset_index(drop=True)
    result = _v57_v47_save_original(save_part, db_path=db_path)
    result["read"] = int(len(work))
    result["exact_duplicate_skipped"] = int(exact_skipped)
    result["skipped"] = int(result.get("skipped", 0)) + int(exact_skipped)
    return result


# ============================================================
# Ver58: 数値完全一致でもRが違う場合は確認待ち
# ============================================================
def _v58_existing_exact_races(db_path=DB_PATH):
    """数値完全一致キーごとに、既存のR候補を返す。"""
    mapping = {}
    if not Path(db_path).exists():
        return mapping
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        if v15_table_exists(con, "race_history") and v15_table_exists(con, "players"):
            rows = con.execute("""
                SELECT p.player_name, h.race_date, h.venue, h.race_no, h.finish,
                       h.handicap, h.trial_time, h.race_time, h.start_time
                FROM race_history h JOIN players p ON p.player_id=h.player_id
            """).fetchall()
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["finish"],
                    r["handicap"], r["trial_time"], r["race_time"], r["start_time"]
                )
                if sig is None:
                    continue
                rn = _v47_normalize_race_no(r["race_no"])
                mapping.setdefault(sig, set()).add(rn)
    return mapping


def _v58_confirmed(row):
    value = row.get("_v58_duplicate_confirmed", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "confirmed", "確認済み"}
    return bool(value)


_v58_v47_save_original = v47_save_player_history
def v47_save_player_history(df, db_path=DB_PATH):
    """数値一致かつR相違は、自動スキップ・統合せず確認待ちへ回す。"""
    if df is None:
        df = pd.DataFrame()
    work = df.copy().reset_index(drop=True)
    if work.empty:
        return _v58_v47_save_original(work, db_path=db_path)

    existing_map = _v58_existing_exact_races(db_path)
    hold_indices = []
    hold_candidates = {}

    for idx, row in work.iterrows():
        if _v58_confirmed(row):
            continue
        sig = _v57_exact_signature_from_input_row(row)
        if sig is None or sig not in existing_map:
            continue
        incoming_r = _v47_normalize_race_no(row.get("レース"))
        existing_r = {r for r in existing_map[sig] if r is not None}
        # 同じRなら通常の完全一致スキップでよい。Rが異なる時だけ確認する。
        if incoming_r is not None and existing_r and incoming_r not in existing_r:
            hold_indices.append(idx)
            hold_candidates[idx] = sorted(existing_r)

    if not hold_indices:
        return _v58_v47_save_original(work, db_path=db_path)

    save_part = work.drop(index=hold_indices).reset_index(drop=True)
    hold_part = work.loc[hold_indices].copy().reset_index(drop=False).rename(columns={"index": "_v58_original_index"})
    hold_part["保留理由"] = "数値完全一致の既存履歴とRが異なります：登録方法を選択"
    hold_part["重複処理"] = "選択してください"
    hold_part["R候補"] = hold_part["_v58_original_index"].map(
        lambda i: " / ".join(f"{r}R" for r in hold_candidates.get(int(i), [])) or "候補なし"
    )
    hold_part = hold_part.drop(columns=["_v58_original_index"], errors="ignore")

    result = _v58_v47_save_original(save_part, db_path=db_path)
    pending = result.get("pending")
    if pending is None or not isinstance(pending, pd.DataFrame):
        pending = pd.DataFrame()
    combined = pd.concat([pending, hold_part], ignore_index=True, sort=False)
    result["read"] = int(len(work))
    result["pending"] = combined
    result["pending_count"] = int(len(combined))
    result["r_conflict_pending"] = int(len(hold_part))
    return result


def v58_cleanup_exact_numeric_duplicates(db_path=DB_PATH):
    """Rが異なる完全一致行は統合せず、同一RまたはR欠損同士だけを統合する。"""
    mount_and_init_db()
    merged_histories = 0
    merged_imports = 0
    r_conflicts = 0
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")

        if v15_table_exists(con, "race_history") and v15_table_exists(con, "players"):
            rows = con.execute("""
                SELECT h.*, p.player_name FROM race_history h
                JOIN players p ON p.player_id=h.player_id
                ORDER BY h.created_at, h.history_id
            """).fetchall()
            groups = {}
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["finish"],
                    r["handicap"], r["trial_time"], r["race_time"], r["start_time"]
                )
                if sig is not None:
                    groups.setdefault(sig, []).append(r)
            fields = ["race_no", "race_name", "finish", "starters", "surface", "handicap",
                      "trial_time", "race_time", "start_time", "result_status", "use_for_model", "source"]
            positives = {"race_no", "finish", "starters", "trial_time", "race_time", "start_time"}
            for group in groups.values():
                if len(group) < 2:
                    continue
                distinct_r = {_v47_normalize_race_no(r["race_no"]) for r in group if _v47_normalize_race_no(r["race_no"]) is not None}
                if len(distinct_r) > 1:
                    r_conflicts += 1
                    continue
                keep, merged = _v57_merge_row_values(group, fields, positives)
                for r in group:
                    if r["history_id"] != keep["history_id"]:
                        con.execute("DELETE FROM race_history WHERE history_id=?", (r["history_id"],))
                        merged_histories += 1
                fallback = _v33_history_signature({
                    "race_date": keep["race_date"], "venue": keep["venue"],
                    "finish": merged.get("finish"), "handicap": merged.get("handicap"),
                    "trial_time": merged.get("trial_time"), "race_time": merged.get("race_time"),
                    "start_time": merged.get("start_time"),
                })
                record_key = _v45_identity_record_key(
                    keep["player_name"], keep["race_date"], keep["venue"], merged.get("race_no"), fallback=fallback
                )
                con.execute("""
                    UPDATE race_history SET race_no=?, race_name=?, finish=?, starters=?, surface=?, handicap=?,
                        trial_time=?, race_time=?, start_time=?, result_status=?, use_for_model=?, source=?, record_key=?
                    WHERE history_id=?
                """, tuple(merged.get(f) for f in fields) + (record_key, keep["history_id"]))

        if v15_table_exists(con, "v15_player_history_imports"):
            rows = con.execute("SELECT * FROM v15_player_history_imports ORDER BY created_at, history_key").fetchall()
            groups = {}
            for r in rows:
                sig = _v57_exact_numeric_signature_from_values(
                    r["player_name"], r["race_date"], r["venue"], r["rank"],
                    r["handicap"], r["trial_time"], r["race_time"], r["st"]
                )
                if sig is not None:
                    groups.setdefault(sig, []).append(r)
            all_cols = [r[1] for r in con.execute("PRAGMA table_info(v15_player_history_imports)").fetchall()]
            fields = [c for c in all_cols if c not in {"history_key", "created_at"}]
            positives = {"race_no", "rank", "starters", "handicap", "trial_time", "race_time", "st",
                         "track_temp", "air_temp", "humidity", "distance", "laps", "popularity", "car_no"}
            for group in groups.values():
                if len(group) < 2:
                    continue
                distinct_r = {_v47_normalize_race_no(r["race_no"]) for r in group if _v47_normalize_race_no(r["race_no"]) is not None}
                if len(distinct_r) > 1:
                    r_conflicts += 1
                    continue
                keep, merged = _v57_merge_row_values(group, fields, positives)
                for r in group:
                    if r["history_key"] != keep["history_key"]:
                        con.execute("DELETE FROM v15_player_history_imports WHERE history_key=?", (r["history_key"],))
                        merged_imports += 1
                assignments = ", ".join(f'"{f}"=?' for f in fields)
                con.execute(
                    f'UPDATE v15_player_history_imports SET {assignments} WHERE history_key=?',
                    tuple(merged.get(f) for f in fields) + (keep["history_key"],)
                )
        con.commit()
    return {"merged_histories": merged_histories, "merged_imports": merged_imports, "r_conflicts": r_conflicts}

# ============================================================
# Ver62: 登録済みグランドノートの全件再同期・再学習
# ============================================================
def v62_rebuild_grand_note_learning(db_path=DB_PATH):
    """
    旧版を含む result_laps の全周回データを player_lap_history へ再同期する。

    Ver60の展開学習は player_lap_history を予測時に集計するため、ここを再構築すると
    登録済みのグランドノート全件が、初周主導・位置維持・捌き・追込み・終盤・失速・
    熱走路時の前残り補正へ反映される。

    既存行は同一 (race_key, car_no, lap_label) で置換し、重複は作らない。
    result_laps に存在しない選手別周回履歴は削除しない。
    """
    v35_init_result_tables(db_path)
    v36_init_history_tables(db_path)

    stats = {
        "結果周回行": 0,
        "同期成功": 0,
        "更新": 0,
        "新規": 0,
        "選手不明": 0,
        "結果選手不明": 0,
        "対象レース": 0,
        "対象選手": 0,
    }

    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys=ON")

        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "result_laps" not in tables or "result_entries" not in tables:
            return stats

        lap_rows = con.execute("""
            SELECT rl.race_key, rl.lap_label, rl.lap_no, rl.position, rl.car_no,
                   re.player_name
              FROM result_laps rl
              LEFT JOIN result_entries re
                ON re.race_key = rl.race_key AND re.car_no = rl.car_no
             ORDER BY rl.race_key, rl.lap_no, rl.position
        """).fetchall()
        stats["結果周回行"] = len(lap_rows)
        stats["対象レース"] = len({str(r[0]) for r in lap_rows})
        stats["対象選手"] = len({(str(r[0]), int(r[4])) for r in lap_rows})

        for race_key, lap_label, lap_no, position, car_no, player_name in lap_rows:
            name = str(player_name or "").strip()
            if not name:
                stats["結果選手不明"] += 1
                continue

            found = _v32_find_player(con, name)
            if not found:
                # 結果登録済みなのに選手マスターが無い旧DBでは、安全に選手を補完する。
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO players(player_name) VALUES(?)",
                        (name,),
                    )
                    found = _v32_find_player(con, name)
                except Exception:
                    found = None
            if not found:
                stats["選手不明"] += 1
                continue

            player_id, canonical = found
            existed = con.execute("""
                SELECT 1 FROM player_lap_history
                 WHERE race_key=? AND car_no=? AND lap_label=?
            """, (race_key, int(car_no), str(lap_label))).fetchone()

            con.execute("""
                INSERT INTO player_lap_history
                    (race_key, player_id, player_name, car_no, lap_label,
                     lap_no, position, created_at)
                VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(race_key, car_no, lap_label) DO UPDATE SET
                    player_id=excluded.player_id,
                    player_name=excluded.player_name,
                    lap_no=excluded.lap_no,
                    position=excluded.position,
                    created_at=CURRENT_TIMESTAMP
            """, (
                str(race_key), int(player_id), str(canonical), int(car_no),
                str(lap_label), int(lap_no) if lap_no is not None else None,
                int(position),
            ))
            stats["同期成功"] += 1
            if existed:
                stats["更新"] += 1
            else:
                stats["新規"] += 1

        # 同じ選手名の空白表記を正規化し、今後の集計漏れを防ぐ。
        try:
            rows = con.execute("SELECT rowid, player_name FROM player_lap_history").fetchall()
            for rowid, old_name in rows:
                found = _v32_find_player(con, old_name)
                if found and str(old_name) != str(found[1]):
                    con.execute(
                        "UPDATE player_lap_history SET player_id=?, player_name=? WHERE rowid=?",
                        (int(found[0]), str(found[1]), int(rowid)),
                    )
        except Exception:
            pass

        con.commit()

    return stats


def v62_grand_note_learning_status(db_path=DB_PATH):
    """現在のグランドノート保存・展開学習対象件数を返す。"""
    v35_init_result_tables(db_path)
    v36_init_history_tables(db_path)
    out = {
        "結果周回行": 0,
        "選手別周回行": 0,
        "結果レース数": 0,
        "学習レース数": 0,
        "学習選手数": 0,
        "未同期行推定": 0,
    }
    with sqlite3.connect(str(db_path)) as con:
        try:
            out["結果周回行"] = int(con.execute("SELECT COUNT(*) FROM result_laps").fetchone()[0])
            out["結果レース数"] = int(con.execute("SELECT COUNT(DISTINCT race_key) FROM result_laps").fetchone()[0])
        except Exception:
            pass
        try:
            out["選手別周回行"] = int(con.execute("SELECT COUNT(*) FROM player_lap_history").fetchone()[0])
            out["学習レース数"] = int(con.execute("SELECT COUNT(DISTINCT race_key) FROM player_lap_history").fetchone()[0])
            out["学習選手数"] = int(con.execute("SELECT COUNT(DISTINCT player_id) FROM player_lap_history").fetchone()[0])
        except Exception:
            pass
        try:
            out["未同期行推定"] = int(con.execute("""
                SELECT COUNT(*)
                  FROM result_laps rl
                  JOIN result_entries re
                    ON re.race_key=rl.race_key AND re.car_no=rl.car_no
                  LEFT JOIN player_lap_history pl
                    ON pl.race_key=rl.race_key
                   AND pl.car_no=rl.car_no
                   AND pl.lap_label=rl.lap_label
                 WHERE pl.race_key IS NULL
                   AND COALESCE(TRIM(re.player_name),'')<>''
            """).fetchone()[0])
        except Exception:
            pass
    return out

# ============================================================
# Ver63: グランドノート未リンク診断・修復
# ============================================================
def _v63_norm_race_no(value):
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else None


def v63_grand_note_unlinked_groups(db_path=DB_PATH):
    """選手名と結び付いていないグランドノートを、レース・車番単位で返す。"""
    v35_init_result_tables(db_path)
    v36_init_history_tables(db_path)
    groups = []
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT rl.race_key, rr.race_date, rr.venue, rr.race_no,
                   rl.car_no, COUNT(*) AS lap_rows,
                   MIN(rl.position) AS best_position,
                   MAX(CASE WHEN rl.lap_label='ゴール線' THEN rl.position END) AS goal_position,
                   MAX(CASE WHEN rl.lap_label='1周目' THEN rl.position END) AS first_lap_position,
                   MAX(COALESCE(re.player_name,'')) AS player_name
              FROM result_laps rl
              LEFT JOIN result_races rr ON rr.race_key=rl.race_key
              LEFT JOIN result_entries re
                ON re.race_key=rl.race_key AND re.car_no=rl.car_no
             WHERE COALESCE(TRIM(re.player_name),'')=''
             GROUP BY rl.race_key, rr.race_date, rr.venue, rr.race_no, rl.car_no
             ORDER BY rr.race_date DESC, CAST(rr.race_no AS INTEGER) DESC, rl.car_no
        """).fetchall()

        for row in rows:
            race_no = _v63_norm_race_no(row["race_no"])
            candidates = []

            # 1) 正規履歴から同日・同場・同R・同車番を探す
            try:
                crows = con.execute("""
                    SELECT DISTINCT p.player_name, '正規履歴' AS source
                      FROM race_history rh
                      JOIN players p ON p.player_id=rh.player_id
                     WHERE rh.race_date=? AND rh.venue=?
                       AND CAST(REPLACE(COALESCE(rh.race_no,''),'R','') AS INTEGER)=?
                       AND EXISTS (
                           SELECT 1 FROM v15_player_history_imports vi
                            WHERE REPLACE(vi.player_name,' ','')=REPLACE(p.player_name,' ','')
                              AND vi.race_date=rh.race_date AND vi.venue=rh.venue
                              AND vi.race_no=? AND vi.car_no=?
                       )
                """, (row["race_date"], row["venue"], race_no or -1, race_no or -1, int(row["car_no"]))).fetchall()
                candidates.extend({"player_name": r[0], "source": r[1]} for r in crows)
            except Exception:
                pass

            # 2) 詳細履歴から同日・同場・同R・同車番を直接探す
            try:
                crows = con.execute("""
                    SELECT DISTINCT player_name, '詳細履歴' AS source
                      FROM v15_player_history_imports
                     WHERE race_date=? AND venue=? AND race_no=? AND car_no=?
                       AND COALESCE(TRIM(player_name),'')<>''
                """, (row["race_date"], row["venue"], race_no or -1, int(row["car_no"]))).fetchall()
                candidates.extend({"player_name": r[0], "source": r[1]} for r in crows)
            except Exception:
                pass

            # 重複候補を正規化して除去
            unique = []
            seen = set()
            for c in candidates:
                key = re.sub(r"[\s　]+", "", str(c["player_name"] or ""))
                if key and key not in seen:
                    seen.add(key)
                    unique.append(c)

            groups.append({
                "race_key": row["race_key"],
                "race_date": row["race_date"],
                "venue": row["venue"],
                "race_no": row["race_no"],
                "car_no": int(row["car_no"]),
                "lap_rows": int(row["lap_rows"] or 0),
                "first_lap_position": row["first_lap_position"],
                "goal_position": row["goal_position"],
                "candidates": unique,
            })
    return groups


def v63_player_name_choices(db_path=DB_PATH):
    """手動修復用の選手名一覧。"""
    with sqlite3.connect(str(db_path)) as con:
        return [r[0] for r in con.execute(
            "SELECT player_name FROM players WHERE COALESCE(TRIM(player_name),'')<>'' ORDER BY player_name"
        ).fetchall()]


def v63_repair_grand_note_link(race_key, car_no, player_name, db_path=DB_PATH):
    """結果の車番へ選手名を設定し、該当グランドノートを選手別履歴へ再同期する。"""
    name = str(player_name or "").strip()
    if not name:
        return {"ok": False, "message": "選手名を選択してください。"}
    v35_init_result_tables(db_path)
    v36_init_history_tables(db_path)
    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        lap_count = int(con.execute(
            "SELECT COUNT(*) FROM result_laps WHERE race_key=? AND car_no=?",
            (str(race_key), int(car_no)),
        ).fetchone()[0])
        if lap_count == 0:
            return {"ok": False, "message": "対象のグランドノートが見つかりません。"}

        found = _v32_find_player(con, name)
        if not found:
            con.execute("INSERT OR IGNORE INTO players(player_name) VALUES(?)", (name,))
            found = _v32_find_player(con, name)
        if not found:
            return {"ok": False, "message": "選手マスターを作成できませんでした。"}
        player_id, canonical = found

        exists = con.execute(
            "SELECT 1 FROM result_entries WHERE race_key=? AND car_no=?",
            (str(race_key), int(car_no)),
        ).fetchone()
        if exists:
            con.execute(
                "UPDATE result_entries SET player_name=? WHERE race_key=? AND car_no=?",
                (str(canonical), str(race_key), int(car_no)),
            )
        else:
            con.execute(
                "INSERT INTO result_entries(race_key,car_no,player_name,result_status) VALUES(?,?,?,'通常')",
                (str(race_key), int(car_no), str(canonical)),
            )

        laps = con.execute("""
            SELECT lap_label, lap_no, position
              FROM result_laps
             WHERE race_key=? AND car_no=?
        """, (str(race_key), int(car_no))).fetchall()
        for lap_label, lap_no, position in laps:
            con.execute("""
                INSERT INTO player_lap_history
                    (race_key, player_id, player_name, car_no, lap_label, lap_no, position, created_at)
                VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(race_key, car_no, lap_label) DO UPDATE SET
                    player_id=excluded.player_id,
                    player_name=excluded.player_name,
                    lap_no=excluded.lap_no,
                    position=excluded.position,
                    created_at=CURRENT_TIMESTAMP
            """, (
                str(race_key), int(player_id), str(canonical), int(car_no),
                str(lap_label), int(lap_no) if lap_no is not None else None,
                int(position),
            ))
        con.commit()
    return {
        "ok": True,
        "message": f"{race_key}・{car_no}番を {canonical} にリンクしました。",
        "synced_rows": lap_count,
        "player_name": canonical,
    }


def v63_auto_repair_grand_note_links(db_path=DB_PATH):
    """候補が一意に決まる未リンクだけを安全に自動修復する。"""
    groups = v63_grand_note_unlinked_groups(db_path)
    repaired = 0
    rows = 0
    manual = 0
    details = []
    for g in groups:
        if len(g.get("candidates", [])) == 1:
            candidate = g["candidates"][0]["player_name"]
            result = v63_repair_grand_note_link(g["race_key"], g["car_no"], candidate, db_path)
            if result.get("ok"):
                repaired += 1
                rows += int(result.get("synced_rows", 0))
                details.append(result.get("message", ""))
            else:
                manual += 1
        else:
            manual += 1
    return {"repaired_groups": repaired, "synced_rows": rows, "manual_groups": manual, "details": details}


def v63_db_health_report(db_path=DB_PATH):
    """グランドノート周辺を中心とした簡易DB健康診断。"""
    status = v62_grand_note_learning_status(db_path)
    groups = v63_grand_note_unlinked_groups(db_path)
    unresolved_rows = sum(int(g.get("lap_rows", 0)) for g in groups)
    total = max(int(status.get("結果周回行", 0)), 1)
    score = max(0.0, 100.0 * (1.0 - unresolved_rows / total))
    return {
        "health_score": round(score, 1),
        "unlinked_groups": len(groups),
        "unlinked_rows": unresolved_rows,
        "result_lap_rows": int(status.get("結果周回行", 0)),
        "learned_lap_rows": int(status.get("選手別周回行", 0)),
    }

# ============================================================
# Ver64 出走表読取強化
# ・予想印付き氏名、氏名末尾の所属LGに対応
# ・年齢/期、当日試走、試走偏差、近10走、車名、車級を確実に取得
# ・近10走成績を小さな当日補正として予測へ反映
# ============================================================

_v64_old_parse_entry_block = v152_parse_entry_block


def _v64_first_float(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return float(m.group(1)) if m else np.nan


def v152_parse_entry_block(block):
    car_no = int(block["車番"])
    lines = [str(x).strip() for x in block.get("lines", []) if str(x).strip()]
    if not lines:
        return None

    # まず従来処理を通し、Ver64で不足項目を正確に補完する。
    row = _v64_old_parse_entry_block(block) or {}
    joined = "\n".join(lines)
    compact = " ".join(lines)

    # 氏名・所属。先頭の予想印は除き、末尾LGは所属へ分離する。
    raw_name = lines[0]
    lg_m = re.search(r"[（(](川口|伊勢崎|浜松|山陽|飯塚)[）)]\s*$", raw_name)
    row["所属"] = lg_m.group(1) if lg_m else row.get("所属")
    row["選手名"] = v15_normalize_name(raw_name)

    # 年齢・期別。「18歳/39期」の同一行を確実に取得。
    age_term = re.search(r"(\d{1,3})歳\s*/\s*(\d{1,2})期", compact)
    if age_term:
        row["年齢"] = int(age_term.group(1))
        row["期別"] = int(age_term.group(2))

    # ハンデ/ST/当日試走。「ハンデ0m/ST0.16 3.47」に対応。
    hs = re.search(
        r"ハンデ\s*([+-]?\d+|-)\s*m\s*/\s*ST\s*([+-]?\d?\.\d{2,3})\s+([3-9]\.\d{2,3}|-)",
        compact, re.I,
    )
    if hs:
        row["ハンデ"] = 0 if hs.group(1) == "-" else int(hs.group(1))
        row["ST"] = float(hs.group(2))
        row["試走T"] = np.nan if hs.group(3) == "-" else float(hs.group(3))

    # 試走偏差と現ランクが同じ行に並ぶ形式。「0.087 B-135」
    dev_rank = re.search(r"(?:^|\s)(0\.\d{3})\s+([SAB]-\d+)(?:\s|$)", compact)
    if dev_rank:
        row["試走偏差"] = float(dev_rank.group(1))
        row["現ランク"] = dev_rank.group(2)
        row["級別"] = dev_rank.group(2).split("-")[0]

    prev = re.search(r"\(前\s*([SAB]-\d+)\)", compact)
    if prev:
        row["前ランク"] = prev.group(1)

    # 平均・最高タイム。
    for key, pattern in (
        ("平均試走T", r"平均試走T\s*([3-9]\.\d{2,3})"),
        ("平均競走T", r"平均競走T\s*([3-9]\.\d{3})"),
        ("最高競走T", r"最高競走T\s*([3-9]\.\d{3})"),
    ):
        value = _v64_first_float(pattern, compact)
        if pd.notna(value):
            row[key] = value

    # 近10走成績。最初に現れる「着順 a-b-c-d」と、その直後の2連/3連を採用。
    recent = re.search(r"着順\s*(\d+-\d+-\d+-\d+)", compact)
    if recent:
        row["近10走着順"] = recent.group(1)
    two = re.search(r"2連\s*([0-9.]+)%", compact)
    three = re.search(r"3連\s*([0-9.]+)%", compact)
    if two:
        row["近10走2連"] = float(two.group(1))
    if three:
        row["近10走3連"] = float(three.group(1))

    # 車名は「3連 xx%」の直後。タブが潰れて同じ行でも取得する。
    car_m = re.search(
        r"3連\s*[0-9.]+%\s+(.+?)(?=\s+[12]\s+着順\s*\d+-\d+-\d+-\d+|$)",
        compact,
    )
    if car_m:
        candidate = car_m.group(1).strip()
        if candidate and not re.fullmatch(r"[12]", candidate):
            row["車名"] = candidate

    # 車級は車名直後にある単独1/2。車番とは別物。
    vehicle_grade = None
    grade_m = re.search(
        r"3連\s*[0-9.]+%\s+.+?\s+([12])\s+着順\s*\d+-\d+-\d+-\d+",
        compact,
    )
    if grade_m:
        vehicle_grade = int(grade_m.group(1))
    row["車級"] = vehicle_grade

    # 競走車成績の6率。近10走2連/3連を除いた末尾6件を採用。
    all_pct = [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)%", compact)]
    if len(all_pct) >= 8:
        tail = all_pct[-6:]
        for key, value in zip(
            ["2連対率", "3連対率", "良2連対率", "良3連対率", "湿2連対率", "湿3連対率"],
            tail,
        ):
            row[key] = value

    row["_raw"] = joined
    return row


# 近10走成績を予測へ小さく反映する。極端な上書きを避け最大±0.8点。
_v64_old_context_bonus = v24_apply_race_context_bonus


def v24_apply_race_context_bonus(df, entries=None, track_temp=30.0):
    out = _v64_old_context_bonus(df, entries=entries, track_temp=track_temp)
    if out is None or out.empty or entries is None or len(entries) == 0:
        return out
    car_col = "車" if "車" in out.columns else "車番"
    if car_col not in out.columns or "車番" not in entries.columns:
        return out

    ent = entries.copy()
    cols = [c for c in ["車番", "近10走2連", "近10走3連", "平均競走T", "最高競走T"] if c in ent.columns]
    if len(cols) <= 1:
        return out
    current = ent[cols].drop_duplicates("車番")
    merged = out[[car_col]].merge(current, left_on=car_col, right_on="車番", how="left")

    two = pd.to_numeric(merged.get("近10走2連"), errors="coerce")
    three = pd.to_numeric(merged.get("近10走3連"), errors="coerce")
    if two.notna().any() or three.notna().any():
        form = two.fillna(two.median() if two.notna().any() else 20.0) * 0.45
        form += three.fillna(three.median() if three.notna().any() else 35.0) * 0.55
        center = float(form.median())
        spread = max(float(form.quantile(.85) - form.quantile(.15)), 15.0)
        bonus = np.clip((form.to_numpy(float) - center) / spread * 0.65, -0.65, 0.80)
    else:
        bonus = np.zeros(len(out), dtype=float)

    out["近10走勢い補正"] = np.round(bonus, 3)
    if "改善後総合点" in out.columns:
        out["改善後総合点"] = pd.to_numeric(out["改善後総合点"], errors="coerce").fillna(0.0) + bonus
        out["改善後順位"] = out["改善後総合点"].rank(method="min", ascending=False).astype(int)
    if "当日レース指数" in out.columns:
        out["当日レース指数"] = pd.to_numeric(out["当日レース指数"], errors="coerce").fillna(50.0) + bonus * 0.30
    if "予測競走T" in out.columns:
        out["予測競走T"] = np.round(pd.to_numeric(out["予測競走T"], errors="coerce") - bonus * 0.0008, 4)
    return out


# ============================================================
# Ver65: 天候 × 走路 × 走路温度帯 × 時間帯の複合条件学習
# ============================================================
def v65_normalize_weather(value):
    text = str(value or "").strip().replace("　", "")
    if not text:
        return None
    if "雷" in text:
        return "雷雨"
    if "雪" in text:
        return "雪"
    if "小雨" in text or "霧雨" in text:
        return "小雨"
    if "雨" in text:
        return "雨"
    if "曇" in text:
        return "曇"
    if "晴" in text:
        return "晴"
    return text[:12]


def v65_normalize_surface(value):
    text = str(value or "").strip()
    if "湿" in text:
        return "湿"
    if "斑" in text:
        return "斑"
    if "良" in text:
        return "良"
    return text or None


def v65_time_band(value, race_no=None):
    text = str(value or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        hour = int(m.group(1))
        if hour < 14:
            return "昼"
        if hour < 18:
            return "夕方"
        if hour < 22:
            return "ナイター"
        return "深夜"
    text2 = text.lower()
    if "オーバー" in text2 or "over" in text2:
        return "深夜"
    if "ミッド" in text2 or "mid" in text2:
        return "深夜"
    try:
        r = int(float(race_no))
        return "昼" if r <= 5 else "夕方" if r <= 8 else "ナイター"
    except Exception:
        return None


def v65_temp_band(value):
    try:
        t = float(value)
    except Exception:
        return None
    if t < 42: return "～41℃"
    if t < 47: return "42～46℃"
    if t < 50: return "47～49℃"
    if t < 52: return "50～51℃"
    if t < 54: return "52～53℃"
    if t < 56: return "54～55℃"
    return "56℃以上"


def v65_apply_weather_condition_learning(df, entries=None, meta=None, db_path=None):
    """天候単独と複合条件の再現性を選手別に学習して予測へ反映する。

    同じ結果を複数条件で過剰に数えないよう、最も信頼できる上位2条件だけを採用する。
    複合条件ほど最低件数を厳しくし、最大補正は±1.5点に制限する。
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    n = len(out)
    defaults = {
        "今回天候": [None] * n,
        "天候適性補正": np.zeros(n),
        "天候適性信頼度": np.zeros(n),
        "天候一致最大件数": np.zeros(n, dtype=int),
        "天候適性根拠": ["天候未取得"] * n,
        "天候条件キー": [""] * n,
    }
    for col, val in defaults.items():
        out[col] = val

    meta = meta or globals().get("LATEST_RACE_META", {}) or {}
    weather = v65_normalize_weather(meta.get("天候") or meta.get("weather"))
    surface = v65_normalize_surface(meta.get("走路状態") or meta.get("走路") or meta.get("surface"))
    temp_band = v65_temp_band(meta.get("走路温度") or meta.get("track_temp"))
    time_band = v65_time_band(
        meta.get("発走時刻") or meta.get("start_time") or meta.get("時間帯") or "",
        meta.get("レース") or meta.get("R") or meta.get("race_no")
    )
    current_key = "×".join(x for x in [weather, surface, temp_band, time_band] if x)
    out["今回天候"] = weather
    out["天候条件キー"] = current_key
    if not weather:
        return out

    car_col = "車" if "車" in out.columns else "車番" if "車番" in out.columns else None
    if car_col is None:
        return out
    ent = entries.copy() if isinstance(entries, pd.DataFrame) else pd.DataFrame()
    names = {}
    if not ent.empty and "車番" in ent.columns and "選手名" in ent.columns:
        e = ent.copy()
        e["車番"] = pd.to_numeric(e["車番"], errors="coerce")
        names = e.dropna(subset=["車番"]).set_index("車番")["選手名"].to_dict()

    db_path = db_path or globals().get("DB_PATH")
    try:
        con = sqlite3.connect(db_path)
    except Exception:
        return out

    bonuses=[]; confidences=[]; counts=[]; reasons=[]
    for _, row in out.iterrows():
        car = pd.to_numeric(pd.Series([row.get(car_col)]), errors="coerce").iloc[0]
        name = str(names.get(car, row.get("選手名", ""))).strip()
        if not name:
            bonuses.append(0.0); confidences.append(0.0); counts.append(0); reasons.append("選手名なし"); continue
        key = v32_player_name_key(name) if "v32_player_name_key" in globals() else re.sub(r"[\s　]", "", name)
        try:
            hist = pd.read_sql_query("""
                SELECT race_date, race_no, rank, starters, weather, surface,
                       track_temp, start_time, time_band, race_name, raw_line
                FROM v15_player_history_imports
                WHERE replace(replace(player_name,' ',''),'　','')=?
                  AND rank IS NOT NULL AND rank > 0
                  AND (starters IS NULL OR rank <= starters)
                  AND COALESCE(raw_line,'') NOT LIKE '%欠責%'
                  AND COALESCE(raw_line,'') NOT LIKE '%周誤%'
                  AND COALESCE(raw_line,'') NOT LIKE '%欠車%'
                  AND COALESCE(raw_line,'') NOT LIKE '%出走取消%'
                  AND COALESCE(raw_line,'') NOT LIKE '%競走中止%'
                  AND COALESCE(raw_line,'') NOT LIKE '%落車%'
                  AND COALESCE(raw_line,'') NOT LIKE '%反則%'
                  AND COALESCE(raw_line,'') NOT LIKE '%不成立%'
                  AND COALESCE(raw_line,'') NOT LIKE '%失格%'
                ORDER BY race_date DESC
                LIMIT 240
            """, con, params=(key,))
        except Exception:
            hist = pd.DataFrame()
        if hist.empty:
            bonuses.append(0.0); confidences.append(0.0); counts.append(0); reasons.append("天候履歴なし"); continue
        if "v61_filter_history_df" in globals():
            hist = v61_filter_history_df(hist, "race_date", "race_no", count_stats=False)
        if hist.empty or len(hist) < 8:
            bonuses.append(0.0); confidences.append(0.0); counts.append(len(hist)); reasons.append("履歴不足"); continue

        hist["rank"] = pd.to_numeric(hist["rank"], errors="coerce")
        hist["starters"] = pd.to_numeric(hist["starters"], errors="coerce").fillna(8).clip(lower=2)
        hist = hist[hist["rank"].notna()].copy()
        if hist.empty:
            bonuses.append(0.0); confidences.append(0.0); counts.append(0); reasons.append("有効着順なし"); continue
        hist["perf"] = ((hist["starters"] - hist["rank"]) / (hist["starters"] - 1)).clip(0, 1)
        hist["weather_n"] = hist["weather"].map(v65_normalize_weather)
        hist["surface_n"] = hist["surface"].map(v65_normalize_surface)
        hist["temp_n"] = hist["track_temp"].map(v65_temp_band)
        hist["time_n"] = [v65_time_band(tb or st, rn) for tb, st, rn in zip(hist.get("time_band", ""), hist.get("start_time", ""), hist.get("race_no", ""))]
        base = float(hist["perf"].mean())

        specs = [
            ("天候", ["weather_n"], [weather], 4, 0.55),
            ("天候×走路", ["weather_n", "surface_n"], [weather, surface], 5, 0.78),
            ("天候×温度帯", ["weather_n", "temp_n"], [weather, temp_band], 5, 0.82),
            ("天候×時間帯", ["weather_n", "time_n"], [weather, time_band], 5, 0.65),
            ("天候×走路×温度帯", ["weather_n", "surface_n", "temp_n"], [weather, surface, temp_band], 6, 1.00),
            ("天候×走路×温度帯×時間帯", ["weather_n", "surface_n", "temp_n", "time_n"], [weather, surface, temp_band, time_band], 7, 1.08),
        ]
        parts=[]
        for label, cols, vals, min_n, strength in specs:
            if any(v in (None, "") for v in vals):
                continue
            mask = pd.Series(True, index=hist.index)
            for c, v in zip(cols, vals):
                mask &= hist[c].astype(str).eq(str(v))
            nn = int(mask.sum())
            if nn < min_n:
                continue
            cond = float(hist.loc[mask, "perf"].mean())
            delta = cond - base
            conf = min(1.0, (nn - min_n + 1) / 16.0)
            cap = 0.28 if nn < 8 else 0.55 if nn < 15 else 0.95 if nn < 25 else 1.30
            effect = float(np.clip(delta * 4.1 * strength * conf, -cap, cap))
            if abs(effect) >= 0.06:
                parts.append((effect, label, nn, delta, conf))
        if not parts:
            bonuses.append(0.0); confidences.append(0.0); counts.append(0); reasons.append("顕著な天候適性なし"); continue
        parts = sorted(parts, key=lambda x: (abs(x[0]), x[2]), reverse=True)[:2]
        total = float(np.clip(sum(x[0] for x in parts), -1.5, 1.5))
        bonuses.append(total)
        confidences.append(max(x[4] for x in parts))
        counts.append(max(x[2] for x in parts))
        reasons.append(" / ".join(f"{x[1]}{'得意' if x[0] > 0 else '苦手'}({x[2]}件,{x[0]:+.2f})" for x in parts))
    con.close()

    bonus = np.asarray(bonuses, dtype=float)
    out["天候適性補正"] = np.round(bonus, 3)
    out["天候適性信頼度"] = np.round(confidences, 3)
    out["天候一致最大件数"] = counts
    out["天候適性根拠"] = reasons
    if "改善後総合点" in out.columns:
        out["改善後総合点"] = pd.to_numeric(out["改善後総合点"], errors="coerce").fillna(0.0) + bonus
        out["改善後順位"] = out["改善後総合点"].rank(method="min", ascending=False).astype(int)
    if "当日レース指数" in out.columns:
        out["当日レース指数"] = pd.to_numeric(out["当日レース指数"], errors="coerce").fillna(50.0) + bonus * 0.38
    if "予測競走T" in out.columns:
        out["予測競走T"] = np.round(pd.to_numeric(out["予測競走T"], errors="coerce") - bonus * 0.0010, 4)
    return out

# ============================================================
# Ver67: 券種別の予測分布保存・結果照合・累積確率自己評価
# ============================================================
def v67_init_ticket_feedback_tables(db_path=DB_PATH):
    with sqlite3.connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS v67_prediction_tickets (
            race_key TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            combination TEXT NOT NULL,
            probability REAL NOT NULL,
            predicted_rank INTEGER NOT NULL,
            cumulative_probability REAL NOT NULL,
            simulation_count INTEGER,
            created_at TEXT,
            PRIMARY KEY (race_key, bet_type, combination)
        );
        CREATE TABLE IF NOT EXISTS v67_ticket_feedback (
            race_key TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            actual_combination TEXT NOT NULL,
            predicted_rank INTEGER,
            individual_probability REAL,
            cumulative_probability REAL,
            total_combinations INTEGER,
            analyzed_at TEXT,
            PRIMARY KEY (race_key, bet_type)
        );
        """)
        con.commit()


def _v67_combo_text(combo, unordered=False):
    if not isinstance(combo, (tuple, list)):
        combo = (combo,)
    vals = [int(x) for x in combo]
    if unordered:
        vals = sorted(vals)
    return "-".join(map(str, vals))


def v67_save_ticket_snapshot(meta, bets, trials, db_path=DB_PATH):
    """予測時点の全組み合わせ確率を保存する。結果登録後の先読みを防ぐため予測時のみ呼ぶ。"""
    v67_init_ticket_feedback_tables(db_path)
    race_key = v34_race_key(meta)
    total = max(int(trials or 0), 1)
    mapping = {
        "2連単": ("2車単", False),
        "2連複": ("2車複", True),
        "3連複": ("三連複", True),
        "3連単": ("三連単", False),
    }
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM v67_prediction_tickets WHERE race_key=?", (race_key,))
        for bet_type, (source_key, unordered) in mapping.items():
            counter = (bets or {}).get(source_key, {})
            items = sorted(counter.items(), key=lambda x: (-int(x[1]), _v67_combo_text(x[0], unordered)))
            cumulative = 0.0
            seen = set()
            rank = 0
            for combo, count in items:
                text = _v67_combo_text(combo, unordered)
                if text in seen:
                    continue
                seen.add(text)
                rank += 1
                probability = float(count) / total * 100.0
                cumulative += probability
                con.execute("""
                    INSERT INTO v67_prediction_tickets
                    (race_key,bet_type,combination,probability,predicted_rank,cumulative_probability,simulation_count,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (race_key, bet_type, text, probability, rank, cumulative, int(count), now))
        con.commit()
    return race_key


def _v67_actual_combinations(results):
    valid = results.copy()
    valid["着順"] = pd.to_numeric(valid["着順"], errors="coerce")
    valid["車番"] = pd.to_numeric(valid["車番"], errors="coerce")
    valid = valid.dropna(subset=["着順", "車番"]).sort_values("着順")
    cars = valid["車番"].astype(int).tolist()
    if len(cars) < 3:
        return {}
    return {
        "2連単": f"{cars[0]}-{cars[1]}",
        "2連複": "-".join(map(str, sorted(cars[:2]))),
        "3連複": "-".join(map(str, sorted(cars[:3]))),
        "3連単": f"{cars[0]}-{cars[1]}-{cars[2]}",
    }


def v67_analyze_ticket_result(meta, results, db_path=DB_PATH):
    """実結果が予測確率上位から累積何%地点にあったかを券種別に保存・返却する。"""
    v67_init_ticket_feedback_tables(db_path)
    race_key = v34_race_key(meta)
    actuals = _v67_actual_combinations(results)
    rows = []
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        for bet_type, actual in actuals.items():
            hit = con.execute("""
                SELECT predicted_rank, probability, cumulative_probability
                FROM v67_prediction_tickets
                WHERE race_key=? AND bet_type=? AND combination=?
            """, (race_key, bet_type, actual)).fetchone()
            total_combos = con.execute(
                "SELECT COUNT(*) FROM v67_prediction_tickets WHERE race_key=? AND bet_type=?",
                (race_key, bet_type),
            ).fetchone()[0]
            if hit:
                rank, prob, cumulative = int(hit[0]), float(hit[1]), float(hit[2])
                con.execute("""
                    INSERT INTO v67_ticket_feedback
                    (race_key,bet_type,actual_combination,predicted_rank,individual_probability,cumulative_probability,total_combinations,analyzed_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(race_key,bet_type) DO UPDATE SET
                    actual_combination=excluded.actual_combination,
                    predicted_rank=excluded.predicted_rank,
                    individual_probability=excluded.individual_probability,
                    cumulative_probability=excluded.cumulative_probability,
                    total_combinations=excluded.total_combinations,
                    analyzed_at=excluded.analyzed_at
                """, (race_key, bet_type, actual, rank, prob, cumulative, total_combos, now))
                rows.append({"券種": bet_type, "的中組み合わせ": actual, "予測順位": rank,
                             "個別確率": prob, "上位累積確率": cumulative, "全組み合わせ数": total_combos})
            else:
                rows.append({"券種": bet_type, "的中組み合わせ": actual, "予測順位": None,
                             "個別確率": None, "上位累積確率": None, "全組み合わせ数": total_combos})
        con.commit()
    return pd.DataFrame(rows)


def v67_ticket_feedback_stats(db_path=DB_PATH):
    """券種別の的中位置平均と累積分位点を返す。"""
    v67_init_ticket_feedback_tables(db_path)
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query("""
            SELECT bet_type, cumulative_probability, predicted_rank
            FROM v67_ticket_feedback
            WHERE cumulative_probability IS NOT NULL
        """, con)
    if df.empty:
        return pd.DataFrame(columns=["券種","レース数","平均","中央値","80%カバー","90%カバー","95%カバー"])
    out = []
    for bet_type, g in df.groupby("bet_type"):
        vals = pd.to_numeric(g["cumulative_probability"], errors="coerce").dropna()
        if vals.empty:
            continue
        out.append({
            "券種": bet_type, "レース数": len(vals), "平均": float(vals.mean()),
            "中央値": float(vals.median()), "80%カバー": float(vals.quantile(.80)),
            "90%カバー": float(vals.quantile(.90)), "95%カバー": float(vals.quantile(.95)),
        })
    order = {"2連単":0,"2連複":1,"3連複":2,"3連単":3}
    return pd.DataFrame(out).sort_values("券種", key=lambda s:s.map(order)).reset_index(drop=True)


def v67_ticket_highlight_table(meta, bet_type, cutoff_pct, db_path=DB_PATH):
    v67_init_ticket_feedback_tables(db_path)
    race_key = v34_race_key(meta)
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query("""
            SELECT predicted_rank AS 順位, combination AS 組み合わせ,
                   probability AS 確率, cumulative_probability AS 累積確率
            FROM v67_prediction_tickets
            WHERE race_key=? AND bet_type=? AND cumulative_probability<=?
            ORDER BY predicted_rank
        """, con, params=(race_key, bet_type, float(cutoff_pct)+1e-9))
        # 境界を超える最初の1件も含め、指定カバー率に到達させる。
        if df.empty or (not df.empty and float(df["累積確率"].max()) + 0.01 < float(cutoff_pct)):
            extra = pd.read_sql_query("""
                SELECT predicted_rank AS 順位, combination AS 組み合わせ,
                       probability AS 確率, cumulative_probability AS 累積確率
                FROM v67_prediction_tickets
                WHERE race_key=? AND bet_type=? AND cumulative_probability>?
                ORDER BY predicted_rank LIMIT 1
            """, con, params=(race_key, bet_type, float(cutoff_pct)))
            df = pd.concat([df, extra], ignore_index=True).drop_duplicates("組み合わせ")
    return df


def v67_compress_formations(combos, bet_type):
    """強調対象を人が読みやすいフォーメーションへ圧縮する。"""
    parsed = []
    for text in combos:
        try:
            parsed.append(tuple(int(x) for x in str(text).split("-")))
        except Exception:
            continue
    if not parsed:
        return []
    lines = []
    if bet_type == "3連単":
        by_first = {}
        for a,b,c in parsed:
            item = by_first.setdefault(a, {"second": set(), "third": set(), "count": 0})
            item["second"].add(b); item["third"].add(c); item["count"] += 1
        for a, item in sorted(by_first.items()):
            s2 = "".join(map(str, sorted(item["second"])))
            s3 = "".join(map(str, sorted(item["third"])))
            lines.append(f"{a}-{s2}-{s3}（対象{item['count']}点）")
    elif bet_type == "3連複":
        lines = ["-".join(map(str, x)) for x in sorted(set(tuple(sorted(x)) for x in parsed))]
    elif bet_type == "2連単":
        by_first = {}
        for a,b in parsed:
            by_first.setdefault(a, set()).add(b)
        for a, seconds in sorted(by_first.items()):
            lines.append(f"{a}-{''.join(map(str, sorted(seconds)))}（{len(seconds)}点）")
    elif bet_type == "2連複":
        lines = ["-".join(map(str, x)) for x in sorted(set(tuple(sorted(x)) for x in parsed))]
    return lines

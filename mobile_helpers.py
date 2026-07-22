from engine import *
import engine as _engine
_canonical_player_name = _engine._canonical_player_name
_date_text = _engine._date_text
_ensure_player = _engine._ensure_player
_layer_value = _engine._layer_value
_normalize_status = _engine._normalize_status
_normalized_race_no = _engine._normalized_race_no
_num = _engine._num
_race_key = _engine._race_key
_s = _engine._s

import io, re, sqlite3
from datetime import datetime
import numpy as np
import pandas as pd

def safe_float(v, default=None):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)
    except Exception:
        return default

def normalize_name(v):
    return re.sub(r"[\s　]+", "", str(v or ""))

def get_db_history(name):
    target = normalize_name(name)
    try:
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
                    h.start_time AS ST
                FROM race_history h
                JOIN players p ON p.player_id=h.player_id
                WHERE REPLACE(REPLACE(p.player_name,' ',''),'　','')=?
                  AND COALESCE(h.use_for_model,1)=1
                ORDER BY h.race_date DESC, h.history_id DESC
            """, con, params=(target,))
        return df
    except Exception:
        return pd.DataFrame(columns=[
            "開催日","開催場","レース","着順","出走",
            "走路","ハンデ","試走T","競走T","ST"
        ])

def surface_text(v):
    s = str(v or "")
    if "湿" in s: return "湿"
    if "斑" in s: return "斑"
    if "良" in s: return "良"
    return "良"

def make_settings(ws):
    defaults = {
        4:30,5:1.35,6:0.85,7:1.5,8:0.7,9:1.4,10:1.1,11:0.75,
        12:0.04,22:0.018,29:24,31:8,32:8,33:5,34:5,35:6,
        36:8,37:6,38:4,39:2,40:1,43:6,44:20,45:0.6,
        49:6,50:6,51:4,52:6
    }
    for row, value in defaults.items():
        ws.cell(row, 2, value)

def build_input_workbook(raw_text):
    meta = v15_parse_race_meta(raw_text)
    entries = v15_parse_entries(raw_text)

    if entries.empty:
        raise ValueError("出走表を解析できませんでした。公式出走表を全文貼り付けてください。")

    wb = Workbook()
    wb.remove(wb.active)

    race_ws = wb.create_sheet("レース予測")
    race_ws.append(["レース開催日", meta.get("開催日") or datetime.now().strftime("%Y-%m-%d")])
    race_ws.append(["今回の開催場", meta.get("開催場") or ""])
    race_ws.append(["今回の走路", surface_text(meta.get("走路状態") or meta.get("走路状況"))])

    settings_ws = wb.create_sheet("設定")
    make_settings(settings_ws)

    headers = ["開催日","開催場","レース","着順","出走","走路","ハンデ","試走T","競走T","ST"]
    by_car = {int(r["車番"]): r for _, r in entries.iterrows()}
    db_status = []

    for car in range(1, 9):
        ws = wb.create_sheet(f"選手{car}")
        row = by_car.get(car)

        if row is None:
            ws["B2"] = f"欠車{car}"
            ws["E2"] = 9.99
            ws["G2"] = 0
            ws["E3"] = surface_text(meta.get("走路状態"))
            ws["I2"] = 0
            ws["I3"] = "B-999"
            history = pd.DataFrame(columns=headers)
        else:
            name = str(row.get("選手名", "")).strip()
            history = get_db_history(name)

            ws["B2"] = name
            ws["E2"] = safe_float(row.get("試走T"), safe_float(row.get("平均試走T"), 3.50))
            ws["G2"] = safe_float(row.get("ハンデ"), 0)
            ws["E3"] = surface_text(meta.get("走路状態") or meta.get("走路状況"))
            ws["I2"] = safe_float(row.get("審査P"), 0)
            ws["I3"] = str(row.get("現ランク") or "B-999")

            db_status.append({
                "車番": car,
                "選手名": name,
                "DB履歴件数": len(history),
                "状態": "取得済み" if len(history) else "履歴なし"
            })

        for c, header in enumerate(headers, 1):
            ws.cell(6, c, header)

        for r_index, (_, hrow) in enumerate(history.head(100).iterrows(), 7):
            for c, header in enumerate(headers, 1):
                value = hrow.get(header)
                if header == "開催日" and value:
                    dt = pd.to_datetime(value, errors="coerce")
                    value = None if pd.isna(dt) else dt.to_pydatetime()
                elif not isinstance(value, str) and pd.isna(value):
                    value = None
                ws.cell(r_index, c, value)

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue(), entries, pd.DataFrame(db_status), meta

def _extract_number(text, patterns):
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None

def parse_environment_meta(raw_text):
    """予測ページ・結果ページ共通の環境条件を抽出。"""
    text = str(raw_text or "").replace("\u3000", " ")
    meta = {}

    # 開催期間から年を補完
    year_match = re.search(r"開催期間[:：]?\s*(20\d{2})年", text)
    year = int(year_match.group(1)) if year_match else datetime.now().year

    # 日付
    date_match = re.search(
        r"(20\d{2})[年/\-](\d{1,2})[月/\-](\d{1,2})日?", text
    )
    if date_match:
        meta["開催日"] = (
            f"{int(date_match.group(1)):04d}-"
            f"{int(date_match.group(2)):02d}-"
            f"{int(date_match.group(3)):02d}"
        )
    else:
        short_date = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
        if short_date:
            meta["開催日"] = (
                f"{year:04d}-{int(short_date.group(1)):02d}-"
                f"{int(short_date.group(2)):02d}"
            )

    # レース番号
    race_patterns = [
        r"(?:予選|一般戦|準決勝戦?|優勝戦?|選抜戦?|特別一般戦?)?\s*(\d{1,2})\s*[RＲ]",
        r"レース\s*[:：]?\s*(\d{1,2})\s*[RＲ]?"
    ]
    for pattern in race_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            meta["レース"] = f"{int(m.group(1))}R"
            break

    # 発走時刻
    m = re.search(r"発走予定\s*(\d{1,2}:\d{2})", text)
    if m:
        meta["発走時刻"] = m.group(1)

    # 環境
    meta["気温"] = _extract_number(
        text, [r"気温\s*[:：]?\s*([\-+]?\d+(?:\.\d+)?)\s*℃"]
    )
    meta["湿度"] = _extract_number(
        text, [r"湿度\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%"]
    )
    meta["走路温度"] = _extract_number(
        text, [r"走路温度\s*[:：]?\s*([\-+]?\d+(?:\.\d+)?)\s*℃"]
    )
    meta["風速"] = _extract_number(
        text, [r"風速\s*[:：]?\s*(\d+(?:\.\d+)?)\s*m"]
    )

    weather = re.search(
        r"天候\s*(?:\r?\n|\t|[:：]\s*)?([晴曇雨雪霧]+)", text
    )
    if weather:
        meta["天候"] = weather.group(1)

    surface = re.search(
        r"走路状況\s*(?:\r?\n|\t|[:：]\s*)?"
        r"(良走路|湿走路|斑走路|良|湿|斑)", text
    )
    if surface:
        value = surface.group(1)
        meta["走路状況"] = {
            "良走路": "良", "湿走路": "湿", "斑走路": "斑"
        }.get(value, value)

    wind = re.search(
        r"風向\s*[:：]?\s*([^\s\t\r\n]+)", text
    )
    if wind:
        meta["風向"] = wind.group(1)

    return meta

def parse_full_result_text_v169(raw_text, venue=""):
    """
    ユーザー提示形式の公式結果全文を解析。
    既存パーサーを利用しつつ、環境条件と表形式結果を補強する。
    """
    text = str(raw_text or "")
    env = parse_environment_meta(text)

    # 既存パーサーを第一候補
    try:
        meta, results, lap_rows = parse_full_official_result(text)
    except Exception:
        meta, results, lap_rows = {}, [], []

    meta = dict(meta or {})
    for k, v in env.items():
        if v not in (None, ""):
            # 既存キーの英語名にも対応
            meta[k] = v

    if venue:
        meta["venue"] = venue
        meta["開催場"] = venue

    # 着順表が既存パーサーで取れない場合の補強
    if not results:
        lines = [x.strip() for x in text.splitlines()]
        start = next(
            (i for i, x in enumerate(lines) if x == "レース結果"),
            None
        )
        if start is not None:
            # 「着 事故 車 選手名」以降を探す
            head = next(
                (
                    i for i in range(start, min(len(lines), start + 30))
                    if lines[i].startswith("着") and "事故" in lines[i]
                ),
                None
            )
            if head is not None:
                i = head + 1
                parsed = []
                while i < len(lines):
                    if lines[i] in ("払戻金", "グランドノート"):
                        break

                    if re.fullmatch(r"\d+", lines[i]):
                        finish = int(lines[i])
                        block = lines[i:i+12]

                        # 車番
                        car = None
                        car_pos = None
                        for j, val in enumerate(block[1:], 1):
                            if re.fullmatch(r"[1-8]", val):
                                car = int(val)
                                car_pos = j
                                break

                        if car is not None and car_pos is not None:
                            # 車番直後の非数値を選手名として扱う
                            name = ""
                            machine = ""
                            for j in range(car_pos + 1, len(block)):
                                val = block[j]
                                if not val:
                                    continue
                                if re.fullmatch(r"\d+(?:\.\d+)?", val):
                                    break
                                if not name:
                                    name = val
                                elif not machine:
                                    machine = val
                                    break

                            nums = []
                            for val in block[car_pos + 1:]:
                                if re.fullmatch(r"\d+(?:\.\d+)?", val):
                                    nums.append(float(val))

                            if len(nums) >= 4:
                                handicap, trial, race_time, st = nums[-4:]
                                parsed.append({
                                    "着順": finish,
                                    "車番": car,
                                    "選手名": name,
                                    "競走車名": machine,
                                    "ハンデ": handicap,
                                    "試走T": trial,
                                    "競走T": race_time,
                                    "ST": st,
                                    "結果区分": "通常",
                                })
                    i += 1

                results = parsed

    # 周回順位の補強
    if not lap_rows and "グランドノート" in text:
        lap_rows = []
        for line in text.splitlines():
            cols = [c.strip() for c in re.split(r"\t+", line.strip())]
            if not cols:
                continue
            label = cols[0]
            if (
                label == "ゴール線通過"
                or re.fullmatch(r"\d+周回", label)
            ):
                order = []
                for c in cols[1:]:
                    if re.fullmatch(r"[1-8]", c):
                        order.append(int(c))
                if order:
                    lap_rows.append({
                        "周回": label,
                        "順位": order
                    })

    return meta, results, lap_rows

def environment_adjustments(meta):
    """環境条件から展開シミュレーション用の軽微な補正値を作る。"""
    surface = _s(meta.get("走路状況") or meta.get("走路"))
    track_temp = safe_float(meta.get("走路温度"), 30.0)
    humidity = safe_float(meta.get("湿度"), 60.0)
    wind_speed = safe_float(meta.get("風速"), 0.0)

    return {
        "track_temp": track_temp,
        "surface": surface,
        "humidity": humidity,
        "wind_speed": wind_speed,
        # 表示用。既存エンジンを壊さない範囲で利用
        "start_variance_factor": (
            1.10 if surface == "湿" else 1.04 if surface == "斑" else 1.0
        ),
        "overtake_factor": (
            0.92 if surface == "湿" else 0.97 if surface == "斑" else 1.0
        ),
    }

def _current_learning_weights():
    init_learning_tables()
    with sqlite3.connect(str(DB_PATH)) as con:
        rows = con.execute(
            "SELECT setting_name,current_value FROM learning_settings"
        ).fetchall()
    values = dict(rows)
    return {k: float(values.get(k, v)) for k, v in INITIAL_WEIGHTS.items()}

def apply_all_result_learning(df):
    """
    過去に「予測保存＋結果登録」が揃った全レースから学習した重みを、
    今回の総合点へ補正として反映する。
    """
    result = df.copy()
    weights = _current_learning_weights()

    raw = []
    for _, row in result.iterrows():
        raw.append([
            _layer_value(row, LEARN_FEATURES[layer])
            for layer in INITIAL_WEIGHTS
        ])
    raw = np.asarray(raw, dtype=float)
    mu = np.nanmean(raw, axis=0)
    sd = np.nanstd(raw, axis=0)
    sd = np.where(sd < 1e-8, 1.0, sd)
    x = np.nan_to_num((raw - mu) / sd)

    w = np.asarray([weights[k] for k in INITIAL_WEIGHTS], dtype=float)
    learned = x @ w
    learned_sd = float(np.std(learned))
    learned_z = (learned - float(np.mean(learned))) / (
        learned_sd if learned_sd > 1e-8 else 1.0
    )

    result["全結果学習点"] = learned
    result["全結果学習補正"] = learned_z * 3.0
    result["学習前総合点"] = pd.to_numeric(
        result["改善後総合点"], errors="coerce"
    ).fillna(0.0)
    result["改善後総合点"] = (
        result["学習前総合点"] + result["全結果学習補正"]
    )
    result["改善後順位"] = (
        result["改善後総合点"]
        .rank(method="first", ascending=False)
        .astype(int)
    )
    return result, weights

def save_prediction_for_learning(df, meta, track_temp):
    global LATEST_PREDICTION_DF, LATEST_TRACK_TEMP
    LATEST_PREDICTION_DF = df.copy()
    LATEST_TRACK_TEMP = float(track_temp)

    # 環境条件を予測DFへ付加して学習保存
    for col in [
        "天候", "走路状況", "気温", "湿度",
        "走路温度", "風向", "風速", "発走時刻"
    ]:
        LATEST_PREDICTION_DF[col] = meta.get(col)

    date_text = _date_text(meta.get("開催日"))
    venue = _s(meta.get("開催場"))
    race_no = _normalized_race_no(meta.get("レース"))

    if not date_text or not venue or not race_no:
        return None, 0

    return save_latest_prediction(date_text, venue, race_no)

def parse_tabular_player_history(raw_text, player_name):
    """日付 場 R 着 車番 走路 ハンデ 試走T 競走T ST 形式を解析。"""
    name = normalize_name(player_name)
    if not name:
        raise ValueError("表形式では選手名欄の入力が必要です。")

    text = raw_text.replace("\u3000", " ").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return pd.DataFrame()

    # タブ優先。タブがない場合は2個以上の空白で分割。
    def split_line(line):
        if "\t" in line:
            return [x.strip() for x in line.split("\t")]
        return [x.strip() for x in re.split(r"\s{2,}", line)]

    headers = split_line(lines[0])
    aliases = {
        "日付": "開催日", "開催日": "開催日",
        "場": "開催場", "開催場": "開催場",
        "R": "レース", "Ｒ": "レース", "レース": "レース",
        "着": "着順", "着順": "着順",
        "車番": "車番",
        "走路": "走路",
        "ハンデ": "ハンデ",
        "試走T": "試走T", "試走": "試走T",
        "競走T": "競走T", "競走": "競走T",
        "ST": "ST", "ＳＴ": "ST",
    }
    mapped = [aliases.get(h, h) for h in headers]

    required = {"開催日","開催場","着順","ハンデ","試走T","競走T","ST"}
    if not required.issubset(set(mapped)):
        return pd.DataFrame()

    records = []
    for line in lines[1:]:
        values = split_line(line)
        if len(values) < len(mapped):
            # 単一空白区切りのフォールバック
            values = re.split(r"\s+", line.strip())
        if len(values) < len(mapped):
            continue

        rec = dict(zip(mapped, values))
        records.append({
            "選手名": name,
            "開催日": _date_text(rec.get("開催日")),
            "開催場": _s(rec.get("開催場")),
            "レース": _normalized_race_no(rec.get("レース")),
            "着順": _num(rec.get("着順")),
            "出走": None,
            "走路": "" if _s(rec.get("走路")) in ("—","-","－") else _s(rec.get("走路")),
            "ハンデ": _s(rec.get("ハンデ")),
            "試走T": _num(rec.get("試走T")),
            "競走T": _num(rec.get("競走T")),
            "ST": _num(rec.get("ST")),
            "車番": _num(rec.get("車番")),
        })

    return pd.DataFrame(records)

def _nonempty_result_lines(text):
    return [
        line.replace("\u3000", " ").strip()
        for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.replace("\u3000", " ").strip()
    ]

def _is_plain_int(value):
    return bool(re.fullmatch(r"\d+", str(value or "").strip()))

def _is_time_number(value):
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", str(value or "").strip()))

def _normalize_result_meta(meta):
    """日本語キーと既存DB用キーを両方そろえる。"""
    meta = dict(meta or {})
    mappings = {
        "開催日": "race_date",
        "開催場": "venue",
        "レース": "race_no",
        "天候": "weather",
        "走路状況": "surface_condition",
        "走路温度": "track_temp",
        "気温": "air_temp",
        "湿度": "humidity",
        "発走時刻": "start_time_text",
    }
    for jp, en in mappings.items():
        if meta.get(jp) not in (None, ""):
            meta[en] = meta[jp]
        elif meta.get(en) not in (None, ""):
            meta[jp] = meta[en]

    if meta.get("race_no"):
        meta["race_no"] = _normalized_race_no(meta["race_no"])
        meta["レース"] = meta["race_no"]
    return meta

def parse_multiline_result_blocks(text):
    """
    公式結果の複数行形式を解析する。

    例:
      1 [事故空欄] 6
      横田 翔紀
      L・ダウ
      30 3.69 3.760 0.19
    """
    lines = _nonempty_result_lines(text)

    header_index = None
    for i, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line)
        if "着" in compact and "事故" in compact and "車" in compact and "選手名" in compact:
            header_index = i
            break
    if header_index is None:
        raise ValueError("着・事故・車・選手名の見出しが見つかりません。")

    # 次の見出し行を飛ばす
    i = header_index + 1
    if i < len(lines) and all(k in lines[i] for k in ["ハンデ", "試走T", "競走T", "ST"]):
        i += 1

    results = []
    stop_words = {"払戻金", "グランドノート", "周回/順位", "周回・順位"}

    while i < len(lines):
        if any(lines[i].startswith(word) for word in stop_words):
            break

        # 着順行は「1  6」またはタブで「1  空欄  6」
        first_parts = [p.strip() for p in re.split(r"\t+|\s{2,}", lines[i]) if p.strip()]
        if len(first_parts) == 1:
            first_parts = re.split(r"\s+", lines[i])

        if not first_parts or not _is_plain_int(first_parts[0]):
            i += 1
            continue

        finish = int(first_parts[0])
        accident = ""
        car = None

        # 1行目から車番を取得
        for token in first_parts[1:]:
            if _is_plain_int(token) and 1 <= int(token) <= 8:
                car = int(token)
                break
            if token and not accident:
                accident = token

        # 車番が次行に分離している場合
        j = i + 1
        if car is None and j < len(lines) and _is_plain_int(lines[j]) and 1 <= int(lines[j]) <= 8:
            car = int(lines[j])
            j += 1

        if car is None:
            i += 1
            continue

        # 選手名、競走車名
        if j >= len(lines):
            break
        name = lines[j].strip()
        j += 1

        machine = ""
        if j < len(lines) and not _is_time_number(lines[j]):
            machine = lines[j].strip()
            j += 1

        # 数値4個: ハンデ、試走T、競走T、ST
        numeric_tokens = []
        abnormal = ""
        while j < len(lines):
            if any(lines[j].startswith(word) for word in stop_words):
                break

            # 次の着順開始を検知
            next_parts = [p for p in re.split(r"\t+|\s+", lines[j]) if p]
            if (
                numeric_tokens
                and next_parts
                and _is_plain_int(next_parts[0])
                and 1 <= int(next_parts[0]) <= 8
                and len(numeric_tokens) >= 4
            ):
                break

            for token in re.split(r"\t+|\s+", lines[j]):
                token = token.strip()
                if not token:
                    continue
                if _is_time_number(token):
                    numeric_tokens.append(float(token))
                elif token not in {"—", "-", "－"}:
                    abnormal = token
            j += 1
            if len(numeric_tokens) >= 4:
                break

        if len(numeric_tokens) < 4:
            raise ValueError(
                f"{finish}着・{car}号車のハンデ/試走T/競走T/STを読み取れませんでした。"
            )

        handicap, trial_time, race_time, start_time = numeric_tokens[:4]
        results.append({
            "着順": finish,
            "事故": accident,
            "車番": car,
            "選手名": _canonical_player_name(name),
            "競走車名": machine,
            "年齢期": "",
            "LG": "",
            "ハンデ": str(int(handicap)) if float(handicap).is_integer() else str(handicap),
            "試走T": float(trial_time),
            "競走T": float(race_time),
            "ST": float(start_time),
            "異常": abnormal,
            "人気": None,
            "結果区分": _normalize_status(accident, abnormal),
        })
        i = j

    if not results:
        raise ValueError("着順結果を1件も読み取れませんでした。")

    # 車番の重複や着順欠落を検査
    cars = [r["車番"] for r in results]
    finishes = [r["着順"] for r in results]
    if len(cars) != len(set(cars)):
        raise ValueError(f"車番が重複しています: {cars}")
    if len(finishes) != len(set(finishes)):
        raise ValueError(f"着順が重複しています: {finishes}")

    return sorted(results, key=lambda r: r["着順"])

def parse_grand_note_v170(text):
    rows = []
    raw_lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw in raw_lines:
        cols = [c.strip() for c in raw.split("\t")]
        if len(cols) < 2:
            cols = [c.strip() for c in re.split(r"\s{2,}", raw.strip()) if c.strip()]
        if not cols:
            continue

        label = cols[0]
        if not (label == "ゴール線通過" or re.fullmatch(r"\d+周回", label)):
            continue

        cars = [int(c) for c in cols[1:] if _is_plain_int(c) and 1 <= int(c) <= 8]
        if not cars:
            continue

        is_goal = label == "ゴール線通過"
        lap_no = 10**6 if is_goal else int(re.search(r"\d+", label).group())
        rows.append({
            "label": "ゴール線通過" if is_goal else label,
            "lap_no": lap_no,
            "is_goal": is_goal,
            "cars": cars,
        })

    # 古い方からゴールへ並べる
    unique = {r["label"]: r for r in rows}
    normal = sorted(
        [r for r in unique.values() if not r["is_goal"]],
        key=lambda r: r["lap_no"]
    )
    goals = [r for r in unique.values() if r["is_goal"]]
    ordered = normal + goals
    for idx, row in enumerate(ordered, 1):
        row["lap_index"] = idx
    return ordered

def parse_full_result_text_v170(raw_text, venue=""):
    text = str(raw_text or "")
    meta = _normalize_result_meta(parse_environment_meta(text))
    if venue:
        meta["venue"] = venue.strip()
        meta["開催場"] = venue.strip()

    # 距離
    m = re.search(r"(\d{4})m", text)
    if m:
        meta["distance_m"] = int(m.group(1))

    # レース名
    m = re.search(
        r"(予選|一般戦|準決勝戦?|優勝戦?|選抜戦?|特別一般戦?)\s*(\d{1,2})[RＲ]",
        text
    )
    if m:
        meta["race_name"] = m.group(1)
        meta["race_no"] = f"{int(m.group(2))}R"
        meta["レース"] = meta["race_no"]

    results = parse_multiline_result_blocks(text)
    lap_rows = parse_grand_note_v170(text)
    return meta, results, lap_rows

def save_parsed_result_v170(meta, results, lap_rows, source="Ver17.1公式結果登録"):
    """
    解析済みデータを直接保存する。
    旧パーサーを再実行しないため、着順と選手の対応が崩れない。
    """
    init_full_result_tables()
    meta = _normalize_result_meta(meta)

    if not meta.get("race_date"):
        raise ValueError("開催日を読み取れませんでした。")
    if not meta.get("venue"):
        raise ValueError("開催場を読み取れませんでした。開催場欄へ入力してください。")
    if not meta.get("race_no"):
        raise ValueError("レース番号を読み取れませんでした。")

    mapping = {int(r["車番"]): r["選手名"] for r in results}

    # 周回順位がない場合もレース本体を保存できるよう、ゴール着順を仮の1地点にする
    rows_for_save = list(lap_rows)
    if not rows_for_save:
        goal_cars = [r["車番"] for r in sorted(results, key=lambda x: x["着順"])]
        rows_for_save = [{
            "label": "ゴール線通過",
            "lap_no": 10**6,
            "is_goal": True,
            "cars": goal_cars,
            "lap_index": 1,
        }]

    race_id, features = save_lap_data(
        rows_for_save, meta, mapping, source=source
    )

    with sqlite3.connect(str(DB_PATH)) as con:
        for r in results:
            pid = _ensure_player(con, r["選手名"])
            con.execute("""
                INSERT INTO race_entries(
                    race_id,car_no,player_id,finish,accident,age_term,lg,
                    handicap,trial_time,race_time,start_time,abnormal,
                    popularity,result_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(race_id,car_no) DO UPDATE SET
                    player_id=excluded.player_id,
                    finish=excluded.finish,
                    accident=excluded.accident,
                    age_term=excluded.age_term,
                    lg=excluded.lg,
                    handicap=excluded.handicap,
                    trial_time=excluded.trial_time,
                    race_time=excluded.race_time,
                    start_time=excluded.start_time,
                    abnormal=excluded.abnormal,
                    popularity=excluded.popularity,
                    result_status=excluded.result_status
            """, (
                race_id, r["車番"], pid, r["着順"], r.get("事故", ""),
                r.get("年齢期", ""), r.get("LG", ""), r.get("ハンデ", ""),
                r.get("試走T"), r.get("競走T"), r.get("ST"),
                r.get("異常", ""), r.get("人気"), r.get("結果区分", "通常")
            ))

    added_history = 0
    updated_history = 0
    starters = len(results)
    surface = meta.get("surface_condition", "")

    for r in results:
        history_row = {
            "開催日": meta.get("race_date", ""),
            "開催場": meta.get("venue", ""),
            "レース": meta.get("race_no", ""),
            "着順": r["着順"],
            "出走": starters,
            "走路": surface,
            "ハンデ": r.get("ハンデ", ""),
            "試走T": r.get("試走T"),
            "競走T": r.get("競走T"),
            "ST": r.get("ST"),
            "結果区分": r.get("結果区分", "通常"),
        }
        a, u = add_history_rows(
            r["選手名"], [history_row], source
        )
        added_history += a
        updated_history += u

    return (
        race_id, meta, results, features,
        added_history, updated_history
    )

def save_parsed_result_v171(meta, results, lap_rows, source="Ver17.1公式結果登録"):
    init_full_result_tables()
    meta = _normalize_result_meta(meta)

    race_date = meta.get("race_date") or meta.get("開催日")
    venue = meta.get("venue") or meta.get("開催場")
    race_no = meta.get("race_no") or meta.get("レース")

    if not race_date:
        raise ValueError("開催日を読み取れませんでした。")
    if not venue:
        raise ValueError("開催場を読み取れませんでした。開催場欄へ入力してください。")
    if not race_no:
        raise ValueError("レース番号を読み取れませんでした。")

    race_no = _normalized_race_no(race_no)
    race_key = _race_key(race_date, venue, race_no)

    meta["race_date"] = race_date
    meta["venue"] = venue
    meta["race_no"] = race_no
    meta["開催日"] = race_date
    meta["開催場"] = venue
    meta["レース"] = race_no
    meta["race_key"] = race_key

    mapping = {int(r["車番"]): r["選手名"] for r in results}

    rows_for_save = list(lap_rows)
    if not rows_for_save:
        goal_cars = [r["車番"] for r in sorted(results, key=lambda x: x["着順"])]
        rows_for_save = [{
            "label": "ゴール線通過",
            "lap_no": 10**6,
            "is_goal": True,
            "cars": goal_cars,
            "lap_index": 1,
        }]

    race_id, features = save_lap_data(rows_for_save, meta, mapping, source=source)

    with sqlite3.connect(str(DB_PATH)) as con:
        for r in results:
            pid = _ensure_player(con, r["選手名"])
            con.execute("""
                INSERT INTO race_entries(
                    race_id,car_no,player_id,finish,accident,age_term,lg,
                    handicap,trial_time,race_time,start_time,abnormal,
                    popularity,result_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(race_id,car_no) DO UPDATE SET
                    player_id=excluded.player_id,
                    finish=excluded.finish,
                    accident=excluded.accident,
                    age_term=excluded.age_term,
                    lg=excluded.lg,
                    handicap=excluded.handicap,
                    trial_time=excluded.trial_time,
                    race_time=excluded.race_time,
                    start_time=excluded.start_time,
                    abnormal=excluded.abnormal,
                    popularity=excluded.popularity,
                    result_status=excluded.result_status
            """, (
                race_id, r["車番"], pid, r["着順"], r.get("事故", ""),
                r.get("年齢期", ""), r.get("LG", ""), r.get("ハンデ", ""),
                r.get("試走T"), r.get("競走T"), r.get("ST"),
                r.get("異常", ""), r.get("人気"), r.get("結果区分", "通常")
            ))

    added_history = 0
    updated_history = 0
    starters = len(results)
    surface = meta.get("surface_condition") or meta.get("走路状況") or ""

    for r in results:
        history_row = {
            "開催日": race_date,
            "開催場": venue,
            "レース": race_no,
            "着順": r["着順"],
            "出走": starters,
            "走路": surface,
            "ハンデ": r.get("ハンデ", ""),
            "試走T": r.get("試走T"),
            "競走T": r.get("競走T"),
            "ST": r.get("ST"),
            "結果区分": r.get("結果区分", "通常"),
        }
        a, u = add_history_rows(r["選手名"], [history_row], source)
        added_history += a
        updated_history += u

    return race_id, meta, results, features, added_history, updated_history

def predict_from_text(raw_text, trials=10000, seed=20260719):
    content, entries, db_status, meta = build_input_workbook(raw_text)
    parsed_env = parse_environment_meta(raw_text)
    for k,v in parsed_env.items():
        if v not in (None, ''):
            meta[k]=v
    env_adj=environment_adjustments(meta)
    track_temp=env_adj['track_temp']
    df, bets, _ = run_model(content, 'AutoRaceAI_Streamlit_input.xlsx', int(trials), int(seed), track_temp)
    df, learned_weights = apply_all_result_learning(df)
    finish_counts, bets = simulate(df, int(trials), int(seed), track_temp=track_temp)
    result_file = create_result_excel(content, 'AutoRaceAI_Streamlit_input.xlsx', df, finish_counts, bets, int(trials), track_temp=track_temp)
    saved_key, saved_players = save_prediction_for_learning(df, meta, track_temp)
    return {
      'df':df, 'bets':bets, 'db_status':db_status, 'meta':meta, 'track_temp':track_temp,
      'learned_weights':learned_weights, 'result_file':result_file,
      'saved_key':saved_key, 'saved_players':saved_players
    }

def register_history_text(raw, player_name=None):
    df=parse_tabular_player_history(raw, player_name)
    if df is None or df.empty:
        df=v15_parse_player_history(raw, player_name=player_name)
    if df is None or df.empty:
        raise ValueError('履歴を解析できませんでした。')
    reports=[]
    for player, group in df.groupby('選手名', dropna=False):
        canonical=normalize_name(player)
        if not canonical:
            raise ValueError('選手名を判定できません。')
        rows=[]
        for _,r in group.iterrows():
            rows.append({
              '開催日':_date_text(r.get('開催日')), '開催場':_s(r.get('開催場')),
              'レース':_s(r.get('レース')), '着順':_num(r.get('着順')),
              '出走':_num(r.get('出走')), '走路':_s(r.get('走路')),
              'ハンデ':_s(r.get('ハンデ')), '試走T':_num(r.get('試走T')),
              '競走T':_num(r.get('競走T')), 'ST':_num(r.get('ST')), '結果区分':'通常'})
        added,updated=add_history_rows(canonical,rows,'Streamlit Web登録')
        reports.append({'選手名':canonical,'新規追加':added,'更新・統合':updated,'DB総履歴':len(get_db_history(canonical))})
    return df,pd.DataFrame(reports)

def preview_result_text(raw, venue='', race_no=''):
    meta,results,laps=parse_full_result_text_v170(raw,venue=venue)
    if race_no.strip():
        meta['race_no']=_normalized_race_no(race_no); meta['レース']=meta['race_no']
    return meta,results,laps

def register_result_text(raw, venue='', race_no='', learn=True):
    meta,results,laps=preview_result_text(raw,venue,race_no)
    if len(results)<2:
        raise ValueError('着順結果を2人以上解析できませんでした。')
    saved=save_parsed_result_v171(meta,results,laps,source='Streamlit公式結果登録')
    race_id,meta,results,features,added,updated=saved
    learning=None
    if learn:
        try:
            settings_df,old_loss,new_loss,races_used=learn_best_weights(search_rounds=3500,seed=42)
            learning={'settings':settings_df,'old_loss':old_loss,'new_loss':new_loss,'races_used':races_used}
        except RuntimeError as e:
            learning={'message':str(e)}
    return {'race_id':race_id,'meta':meta,'results':results,'features':features,'added':added,'updated':updated,'learning':learning}

from __future__ import annotations

import re
import unicodedata
from typing import Any

VENUES = ("川口", "伊勢崎", "浜松", "飯塚", "山陽")
PREDICTION_MARKS = "◎○▲△×注★☆"


class ParseError(ValueError):
    pass


def normalize_text(text: str) -> str:
    return (
        unicodedata.normalize("NFKC", text or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def first_match(pattern: str, text: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_player_name(name: str) -> str:
    name = name.strip()
    name = re.sub(rf"^[{re.escape(PREDICTION_MARKS)}]+", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


class EntryParser:
    """autorace.jpの公式出走表テキストを解析する。"""

    def parse(self, raw_text: str) -> dict[str, Any]:
        text = normalize_text(raw_text)
        if not text.strip():
            raise ParseError("出走表を貼り付けてください。")

        race = {
            "date": self._parse_date(text),
            "venue": self._parse_venue(text),
            "race_no": self._parse_race_no(text),
            "status": self._parse_status(text),
            "start_time": self._parse_start_time(text),
            "distance_m": self._parse_distance(text),
            "laps": self._parse_laps(text),
            "cars": self._parse_car_count(text),
            "weather": self._parse_weather(text),
            "track_condition": self._parse_track_condition(text),
            "air_temp": self._parse_air_temp(text),
            "humidity": self._parse_humidity(text),
            "track_temp": self._parse_track_temp(text),
            "wind_direction": self._parse_wind_direction(text),
            "wind_speed": self._parse_wind_speed(text),
            "players": self._parse_players(text),
            "raw_text": raw_text,
        }

        if not race["venue"] and race["race_no"] is None and not race["players"]:
            raise ParseError(
                "出走表として解析できませんでした。"
                "公式ページをできるだけ全文コピーして貼り付けてください。"
            )

        return race

    def _parse_date(self, text: str) -> str:
        patterns = [
            r"(20\d{2})年(\d{1,2})月(\d{1,2})日",
            r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                year, month, day = map(int, match.groups())
                return f"{year:04d}-{month:02d}-{day:02d}"
        return ""

    def _parse_venue(self, text: str) -> str:
        return next((venue for venue in VENUES if venue in text), "")

    def _parse_race_no(self, text: str) -> int | None:
        value = first_match(
            r"(?m)^\s*(\d{1,2})\s*[RＲ]\s*$|(?:レース|RACE)\s*(\d{1,2})",
            text,
            re.IGNORECASE,
        )
        if value:
            return to_int(value)

        match = re.search(r"(?m)^\s*(\d{1,2})\s*[RＲ]\b", text)
        return int(match.group(1)) if match else None

    def _parse_status(self, text: str) -> str:
        for status in ("確定", "発売中", "締切済", "中止"):
            if re.search(rf"(?m)^\s*{status}\s*$", text):
                return status
        return ""

    def _parse_start_time(self, text: str) -> str:
        return first_match(
            r"(\d{1,2}:\d{2})\s*発走|"
            r"(?:発走予定|発走時刻|発走)\s*[:：]?\s*(\d{1,2}:\d{2})",
            text,
        ) or self._match_group_two(
            r"(\d{1,2}:\d{2})\s*発走|"
            r"(?:発走予定|発走時刻|発走)\s*[:：]?\s*(\d{1,2}:\d{2})",
            text,
        )

    @staticmethod
    def _match_group_two(pattern: str, text: str) -> str:
        match = re.search(pattern, text)
        if not match:
            return ""
        return next((group for group in match.groups() if group), "")

    def _parse_distance(self, text: str) -> int | None:
        value = first_match(r"(\d{4})\s*m", text, re.IGNORECASE)
        return to_int(value)

    def _parse_laps(self, text: str) -> int | None:
        value = first_match(r"(\d+)\s*周", text)
        return to_int(value)

    def _parse_car_count(self, text: str) -> int | None:
        value = first_match(r"(\d+)\s*車", text)
        return to_int(value)

    def _parse_weather(self, text: str) -> str:
        # 「曇」などが独立行になっている公式形式を優先
        match = re.search(r"(?m)^\s*(晴|曇|雨|雪)\s*$", text)
        if match:
            return match.group(1)
        return first_match(r"天候\s*[:：]?\s*(晴|曇|雨|雪)", text)

    def _parse_track_condition(self, text: str) -> str:
        # 例: 湿走路 /34℃
        match = re.search(
            r"(?m)^\s*(良走路|湿走路|斑走路|濡走路|乾走路)\s*(?:/|／)?\s*-?\d*(?:\.\d+)?\s*(?:℃|°C)?\s*$",
            text,
        )
        if match:
            return match.group(1)

        return first_match(
            r"走路状況\s*[:：]?\s*(良走路|湿走路|斑走路|濡走路|乾走路|良|湿|斑|濡|乾)",
            text,
        )

    def _parse_air_temp(self, text: str) -> float | None:
        return to_float(first_match(r"気温\s*[:：]?\s*(-?\d+(?:\.\d+)?)", text))

    def _parse_humidity(self, text: str) -> float | None:
        return to_float(first_match(r"湿度\s*[:：]?\s*(\d+(?:\.\d+)?)", text))

    def _parse_track_temp(self, text: str) -> float | None:
        value = first_match(r"走路温度\s*[:：]?\s*(-?\d+(?:\.\d+)?)", text)
        if value:
            return to_float(value)

        # 例: 湿走路 /34℃
        value = first_match(
            r"(?:良走路|湿走路|斑走路|濡走路|乾走路)\s*(?:/|／)\s*(-?\d+(?:\.\d+)?)\s*(?:℃|°C)",
            text,
        )
        return to_float(value)

    def _parse_wind_direction(self, text: str) -> str:
        return first_match(r"風向\s*[:：]?\s*([^\s\n]+)", text)

    def _parse_wind_speed(self, text: str) -> float | None:
        return to_float(
            first_match(r"風速\s*[:：]?\s*(\d+(?:\.\d+)?)\s*m/?s?", text, re.IGNORECASE)
        )

    def _parse_players(self, text: str) -> list[dict[str, Any]]:
        # この公式形式では「車番<TAB>選手名」が各選手ブロックの開始。
        block_pattern = re.compile(
            r"(?ms)^\s*([1-8])\s*\t+\s*([^\n]+?)\s*\n"
            r"(?=(-?\d+)\s*m\s*/\s*ST\s*(0\.\d{1,3}))"
            r"(.*?)(?=^\s*[1-8]\s*\t+\s*[^\n]+?\s*\n(?=-?\d+\s*m\s*/\s*ST\s*0\.\d{1,3})|\Z)"
        )

        players: list[dict[str, Any]] = []
        for match in block_pattern.finditer(text):
            car_no = int(match.group(1))
            name = clean_player_name(match.group(2))
            block = match.group(5)

            handicap_st = re.match(
                r"(-?\d+)\s*m\s*/\s*ST\s*(0\.\d{1,3})",
                block,
                re.IGNORECASE,
            )
            trial = re.search(r"試\s*(3\.\d{2,3})", block)
            rank = re.search(r"\b([ASB]-\d+)\b", block)
            machine = self._parse_machine_name(block)
            avg_trial = to_float(first_match(r"平均試走T\s*(3\.\d{2,3})", block))
            avg_race = to_float(first_match(r"平均競走T\s*(3\.\d{2,3})", block))
            best_race = to_float(first_match(r"最高競走T\s*(3\.\d{2,3})", block))

            wet_rates = re.findall(r"(\d+(?:\.\d+)?)%", block)
            wet_two = to_float(wet_rates[-2]) if len(wet_rates) >= 2 else None
            wet_three = to_float(wet_rates[-1]) if len(wet_rates) >= 1 else None

            players.append(
                {
                    "車番": car_no,
                    "選手名": name,
                    "ハンデ": int(handicap_st.group(1)) if handicap_st else None,
                    "ST": float(handicap_st.group(2)) if handicap_st else None,
                    "試走T": float(trial.group(1)) if trial else None,
                    "現ランク": rank.group(1) if rank else "",
                    "車名": machine,
                    "平均試走T": avg_trial,
                    "平均競走T": avg_race,
                    "最高競走T": best_race,
                    "湿2連対率": wet_two,
                    "湿3連対率": wet_three,
                }
            )

        if players:
            return sorted(players, key=lambda row: row["車番"])

        # タブが消えた場合のフォールバック
        return self._parse_players_without_tabs(text)

    def _parse_players_without_tabs(self, text: str) -> list[dict[str, Any]]:
        pattern = re.compile(
            r"(?ms)^\s*([1-8])\s+([^\n]+?)\s*\n"
            r"(-?\d+)\s*m\s*/\s*ST\s*(0\.\d{1,3})"
            r"(.*?)(?=^\s*[1-8]\s+[^\n]+?\s*\n-?\d+\s*m\s*/\s*ST|\Z)"
        )
        players = []
        for match in pattern.finditer(text):
            block = match.group(5)
            trial = re.search(r"試\s*(3\.\d{2,3})", block)
            players.append(
                {
                    "車番": int(match.group(1)),
                    "選手名": clean_player_name(match.group(2)),
                    "ハンデ": int(match.group(3)),
                    "ST": float(match.group(4)),
                    "試走T": float(trial.group(1)) if trial else None,
                    "現ランク": first_match(r"\b([ASB]-\d+)\b", block),
                    "車名": self._parse_machine_name(block),
                    "平均試走T": to_float(first_match(r"平均試走T\s*(3\.\d{2,3})", block)),
                    "平均競走T": to_float(first_match(r"平均競走T\s*(3\.\d{2,3})", block)),
                    "最高競走T": to_float(first_match(r"最高競走T\s*(3\.\d{2,3})", block)),
                    "湿2連対率": None,
                    "湿3連対率": None,
                }
            )
        return sorted(players, key=lambda row: row["車番"])

    @staticmethod
    def _parse_machine_name(block: str) -> str:
        # 「3連 10.0%    マギーJR」の直後を車名として読む。
        match = re.search(
            r"3連\s*\d+(?:\.\d+)?%\s*\t+\s*([^\n\t]+)",
            block,
        )
        if match:
            return match.group(1).strip()

        # タブが消えた場合
        match = re.search(
            r"3連\s*\d+(?:\.\d+)?%\s+([^\n]+)",
            block,
        )
        if match:
            candidate = match.group(1).strip()
            if not candidate.startswith("着順"):
                return candidate
        return ""

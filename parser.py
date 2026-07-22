from __future__ import annotations
import re
import unicodedata

VENUES = ("川口", "伊勢崎", "浜松", "飯塚", "山陽")

class ParseError(ValueError):
    pass

def norm(text):
    return unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n").replace("\r", "\n")

def fnum(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None

class EntryParser:
    def parse(self, raw_text):
        text = norm(raw_text)
        if not text.strip():
            raise ParseError("出走表を貼り付けてください。")
        venue = next((v for v in VENUES if v in text), "")
        m = re.search(r"(\d{1,2})\s*R\b", text, re.I)
        race_no = int(m.group(1)) if m else None
        date = ""
        m = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text)
        if m:
            y, mo, d = map(int, m.groups())
            date = f"{y:04d}-{mo:02d}-{d:02d}"
        start = re.search(r"(?:発走予定|発走時刻|発走)\s*[:：]?\s*(\d{1,2}:\d{2})", text)
        weather = re.search(r"天候\s*[:：]?\s*(晴|曇|雨|雪)", text)
        track = re.search(r"走路状況\s*[:：]?\s*([^\s\n]+)", text)
        wind_dir = re.search(r"風向\s*[:：]?\s*([^\s\n]+)", text)
        players = self._players(text)
        if not venue and race_no is None and not players:
            raise ParseError("出走表として解析できませんでした。公式ページを全文コピーしてください。")
        return {
            "date": date,
            "venue": venue,
            "race_no": race_no,
            "start_time": start.group(1) if start else "",
            "weather": weather.group(1) if weather else "",
            "track_condition": track.group(1) if track else "",
            "air_temp": fnum(r"気温\s*[:：]?\s*(-?\d+(?:\.\d+)?)", text),
            "humidity": fnum(r"湿度\s*[:：]?\s*(\d+(?:\.\d+)?)", text),
            "track_temp": fnum(r"走路温度\s*[:：]?\s*(-?\d+(?:\.\d+)?)", text),
            "wind_direction": wind_dir.group(1) if wind_dir else "",
            "wind_speed": fnum(r"風速\s*[:：]?\s*(\d+(?:\.\d+)?)", text),
            "players": players,
            "raw_text": raw_text,
        }

    def _players(self, text):
        result = []
        seen = set()
        for line in [x.strip() for x in text.splitlines() if x.strip()]:
            m = re.match(r"^([1-8])\s+([^\d]{2,24}?)\s+(-?\d{1,3})(?:\s+(3\.\d{2,3}))?(?:\s+(0\.\d{2}))?$", line)
            if not m:
                continue
            car = int(m.group(1))
            if car in seen:
                continue
            result.append({
                "車番": car,
                "選手名": re.sub(r"\s+", " ", m.group(2)).strip(),
                "ハンデ": int(m.group(3)),
                "試走T": float(m.group(4)) if m.group(4) else None,
                "ST": float(m.group(5)) if m.group(5) else None,
            })
            seen.add(car)
        return sorted(result, key=lambda x: x["車番"])

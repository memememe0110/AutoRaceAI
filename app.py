from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

import requests
import streamlit as st
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
NTFY_BASE = "https://ntfy.sh"


def parse_schedule_text(text: str) -> tuple[date | None, time | None, str | None]:
    """貼り付け文から日本語の日付・発走/開始時刻・短い予定名を抽出する。"""
    source = str(text or "")

    parsed_date: date | None = None
    date_match = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", source)
    if date_match:
        try:
            parsed_date = date(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
        except ValueError:
            parsed_date = None

    parsed_time: time | None = None
    time_patterns = [
        r"(\d{1,2}):(\d{2})\s*発走",
        r"発走(?:予定)?\s*[:：]?\s*(\d{1,2}):(\d{2})",
        r"(\d{1,2}):(\d{2})\s*(?:開始|予定)",
        r"開始(?:時刻)?\s*[:：]?\s*(\d{1,2}):(\d{2})",
    ]
    for pattern in time_patterns:
        match = re.search(pattern, source)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                parsed_time = time(hour=hour, minute=minute)
                break

    parsed_title: str | None = None
    race_match = re.search(r"(?m)^\s*(\d{1,2}R)\s*$", source)
    venue_match = re.search(r"令和[^\n]*(飯塚|山陽小野田|山陽|川口|浜松|伊勢崎)", source)
    if race_match:
        race_no = race_match.group(1)
        venue = venue_match.group(1) if venue_match else ""
        if venue == "山陽小野田":
            venue = "山陽"
        parsed_title = f"{venue}{race_no}" if venue else race_no

    return parsed_date, parsed_time, parsed_title


st.set_page_config(page_title="10分前プッシュ通知", page_icon="🔔", layout="centered")
st.title("🔔 10分前プッシュ通知")
st.caption("一般予定のタイトルと日時を入力すると、10分前にntfyへ通知を予約します。")

if "event_date" not in st.session_state:
    st.session_state.event_date = datetime.now(JST).date()
if "event_time" not in st.session_state:
    now_plus_30 = datetime.now(JST) + timedelta(minutes=30)
    st.session_state.event_time = time(hour=now_plus_30.hour, minute=now_plus_30.minute)
if "event_title" not in st.session_state:
    st.session_state.event_title = ""

schedule_text = st.text_area(
    "予定情報を貼り付け（任意）",
    placeholder="例：2026年8月1日(土)\n10:39発走",
    height=120,
    help="『2026年8月1日』『10:39発走』などを読み取り、予定日と開始時刻へ自動入力します。",
)

if st.button("日時を自動入力", use_container_width=True):
    parsed_date, parsed_time, parsed_title = parse_schedule_text(schedule_text)
    if parsed_date is None and parsed_time is None:
        st.warning("日付または発走・開始時刻を読み取れませんでした。")
    else:
        if parsed_date is not None:
            st.session_state.event_date = parsed_date
        if parsed_time is not None:
            st.session_state.event_time = parsed_time
        if parsed_title and not st.session_state.event_title:
            st.session_state.event_title = parsed_title
        st.success("予定日時を自動入力しました。")
        st.rerun()

with st.form("reminder_form"):
    title = st.text_input("予定名", key="event_title", placeholder="例：オンライン面談")
    event_date = st.date_input("予定日", key="event_date")
    event_time = st.time_input("開始時刻", key="event_time", step=60)
    topic = st.text_input(
        "ntfyトピック名",
        placeholder="他人に推測されにくい長い文字列",
        help="iPhoneのntfyアプリで同じトピックを購読してください。",
    )
    submit = st.form_submit_button("10分前通知を予約", type="primary", use_container_width=True)

if submit:
    clean_title = title.strip()
    clean_topic = topic.strip()

    if not clean_title:
        st.error("予定名を入力してください。")
        st.stop()
    if not clean_topic:
        st.error("ntfyトピック名を入力してください。")
        st.stop()
    if any(ch in clean_topic for ch in "/?# "):
        st.error("トピック名には、空白や / ? # を使わないでください。")
        st.stop()

    event_dt = datetime.combine(event_date, event_time, tzinfo=JST)
    notify_dt = event_dt - timedelta(minutes=10)
    now = datetime.now(JST)

    if notify_dt <= now:
        st.error("通知予定時刻が過ぎています。開始時刻を10分以上先にしてください。")
        st.stop()
    if notify_dt - now > timedelta(days=3):
        st.error("ntfy.shの予約通知は最大3日先までです。3日以内の予定を指定してください。")
        st.stop()

    endpoint = f"{NTFY_BASE}/{quote(clean_topic, safe='')}"
    message = f"{clean_title}\n開始時刻: {event_dt.strftime('%Y/%m/%d %H:%M')}"
    headers = {
        "At": str(int(notify_dt.timestamp())),
        "Title": "予定の10分前です",
        "Priority": "high",
        "Tags": "bell",
    }

    try:
        response = requests.post(
            endpoint,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        st.error(f"通知予約に失敗しました: {exc}")
    else:
        st.success("通知を予約しました。")
        st.write(f"通知時刻: **{notify_dt.strftime('%Y/%m/%d %H:%M')}（日本時間）**")
        st.write(f"予定時刻: **{event_dt.strftime('%Y/%m/%d %H:%M')}（日本時間）**")
        st.info("iPhoneのntfyアプリで同じトピック名を購読しておいてください。")

st.divider()
st.caption("注意：公開ntfy.shのトピックは、推測されにくい長い名前にしてください。予定の機密情報は通知本文へ入れないのがおすすめです。")

from __future__ import annotations

import re
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, default_config
from ..db import fetch_horse_by_id, get_connection, get_recent_form_for_horse, init_db
from ..schema import (
    ensure_racecard_starters_columns,
    racecard_base_schema_sql,
)


RACECARD_BASE_URL = "https://racing.hkjc.com/zh-hk/local/information/racecard"

# 每場賽事摘要（由排位頁面上方資訊區解析；與馬匹列表分表儲存）
RACECARD_RACE_INFO_TABLE = "racecard_race_info"


COURSE_NAME_TO_CODE: dict[str, str] = {
    "沙田": "ST",
    "跑馬地": "HV",
    "從化": "FC",
}

# 場地代碼 → 顯示名稱（查閱過往賽日下拉選單用）
COURSE_CODE_TO_DISPLAY: dict[str, str] = {code: name for name, code in COURSE_NAME_TO_CODE.items()}

# 只保留「出馬表 table」需要的欄位（不把其它欄位存進 DB）。
RACECARD_DISPLAY_FIELDS: list[str] = [
    "馬號",
    "馬名",
    "檔位",
    "騎師",
    "練馬師",
    "負磅",
    "評分",
    "馬匹體重",
    "配備",
]

# 預測賠率（本欄會顯示在 grid，但目前不做抓取解析：寫入空值）。
RACECARD_PREDICTED_ODDS_COLUMN = "預測賠率"

# 即時賠率：初始會先用模型填一份；抓到排位資料後會再用 GraphQL 的 winOdds 重新更新（只更新既有且非後備）。
RACECARD_IMMEDIATE_ODDS_COLUMN = "即時賠率"

RACECARD_DISPLAY_COLUMNS: list[str] = (
    RACECARD_DISPLAY_FIELDS
    + [RACECARD_PREDICTED_ODDS_COLUMN, RACECARD_IMMEDIATE_ODDS_COLUMN, "後備"]
)

# 馬匹 ID：用於用 `race_records` 計算預測賠率（不顯示在 grid）。
RACECARD_HIDDEN_HORSE_ID_COLUMN = "horse_id"

# Map HKJC header label -> fixed DB/UI field.
# Use substring match because HKJC may include extra text.
_FIELD_KEYWORDS: dict[str, list[str]] = {
    "馬號": ["馬號"],
    "馬名": ["馬名"],
    "檔位": ["檔位"],
    "騎師": ["騎師", "騎手"],
    "練馬師": ["練馬師"],
    "負磅": ["負磅"],
    "評分": ["評分"],
    # Reserve table sometimes uses 排位體重 for the weight-related column.
    "馬匹體重": ["馬匹體重", "排位體重", "體重"],
    "配備": ["配備"],
}


def _map_header_label_to_field(label: str) -> str | None:
    s = (label or "").strip()
    if not s:
        return None
    for field, keywords in _FIELD_KEYWORDS.items():
        if any(kw in s for kw in keywords):
            return field
    return None


def _extract_racecard_display_fields(
    headers: list[str],
    values: list[str],
) -> dict[str, str | None]:
    out: dict[str, str | None] = {f: None for f in RACECARD_DISPLAY_FIELDS}

    # HKJC racecard 的第一個欄位通常是「馬號」（出場排序）。
    # 但由於 header label 在不同編碼下可能無法穩定匹配到「馬號」關鍵字，
    # 所以改用位置 + 數字外觀做保底。
    if values:
        v0 = (values[0] or "").strip()
        horse_no_field = RACECARD_DISPLAY_FIELDS[0]
        if v0 and re.fullmatch(r"\d{1,2}", v0):
            out[horse_no_field] = v0

    for idx, (h, v) in enumerate(zip(headers, values)):
        if idx == 0:
            continue
        field = _map_header_label_to_field(h)
        if not field:
            continue
        vv = (v or "").strip()
        out[field] = vv if vv else None
    return out


def _finish_pos_to_win_flag(finish_pos: str | None) -> bool | None:
    """
    Convert HKJC finish_pos to win flag.

    DB example: '01'..'14', 'WV', 'WV-A'...
    - numeric position 01 == win
    - WV / non-numeric => not-counted (return None)
    """
    fp = str(finish_pos or "").strip().upper()
    if not fp:
        return None
    if fp.startswith("WV"):
        return None
    m = re.fullmatch(r"0*(\d{1,2})", fp)
    if not m:
        return None
    return int(m.group(1)) == 1


def _finish_pos_to_start_flag(finish_pos: str | None) -> bool:
    """Whether finish_pos represents a numeric finishing position."""
    fp = str(finish_pos or "").strip().upper()
    if not fp:
        return False
    if fp.startswith("WV"):
        return False
    return re.fullmatch(r"0*\d{1,2}", fp) is not None


def _format_odds(odds_decimal: float) -> str:
    """Format odds with up to 2 decimals, trimming trailing zeros."""
    v = round(float(odds_decimal), 2)
    s = f"{v:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _estimate_raw_win_probability(
    conn: sqlite3.Connection,
    horse_id: str,
    meeting_race_date_yyyy_mm_dd: str,
    lookback_races: int = 12,
    weight_decay: float = 0.9,
    alpha: float = 1.0,
    beta: float = 9.0,
) -> float:
    """
    Estimate win probability from past performance.

    Heuristic:
    - consider last `lookback_races` (by race_records.id desc)
    - weight recency by `weight_decay ** idx`
    - starts = sum(weights for numeric finish_pos)
    - wins = sum(weights for finish_pos indicating win (01))
    - apply beta-like smoothing: (wins + alpha) / (starts + alpha + beta)
    - return raw probability (will be normalized among horses in the same race)
    """
    if not horse_id:
        return 1.0 / (alpha + beta)

    rows = conn.execute(
        """
        SELECT finish_pos, race_date
        FROM race_records
        WHERE horse_id = ?
        ORDER BY id DESC
        LIMIT ?;
        """,
        (horse_id, lookback_races),
    ).fetchall()

    starts_w = 0.0
    wins_w = 0.0
    for idx, r in enumerate(rows):
        w = weight_decay**idx
        fp = r["finish_pos"]
        if not _finish_pos_to_start_flag(fp):
            continue
        starts_w += w
        if _finish_pos_to_win_flag(fp):
            wins_w += w

    return (wins_w + alpha) / (starts_w + alpha + beta)


def _fill_predicted_win_odds_for_race(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
) -> None:
    horse_rows = conn.execute(
        f"""
        SELECT DISTINCT {RACECARD_HIDDEN_HORSE_ID_COLUMN} AS horse_id
        FROM racecard_starters
        WHERE race_date = ? AND racecourse = ? AND race_no = ?
          AND 後備 = '否'
          AND {RACECARD_HIDDEN_HORSE_ID_COLUMN} IS NOT NULL
          AND TRIM({RACECARD_HIDDEN_HORSE_ID_COLUMN}) <> '';
        """,
        (race_date, racecourse, race_no),
    ).fetchall()

    horse_ids = [r["horse_id"] for r in horse_rows if r["horse_id"]]
    if not horse_ids:
        return

    # 1) Estimate raw probabilities
    p_raw_by_hid: dict[str, float] = {}
    for hid in horse_ids:
        p_raw_by_hid[hid] = _estimate_raw_win_probability(
            conn, hid, meeting_race_date_yyyy_mm_dd=race_date
        )

    p_sum = sum(p_raw_by_hid.values()) or 1.0

    # 2) Normalize within the same race, convert to odds
    # Model 預測只更新「預測賠率」，「即時賠率」由 GraphQL winOdds 更新。
    updates: list[tuple[str, str, str, int, str]] = []
    for hid in horse_ids:
        p_norm = p_raw_by_hid[hid] / p_sum
        odds = 1.0 / max(p_norm, 1e-12)
        odds_str = _format_odds(odds)
        updates.append((odds_str, race_date, racecourse, race_no, hid))

    conn.executemany(
        f"""
        UPDATE racecard_starters
        SET \"{RACECARD_PREDICTED_ODDS_COLUMN}\" = ?
        WHERE race_date = ?
          AND racecourse = ?
          AND race_no = ?
          AND {RACECARD_HIDDEN_HORSE_ID_COLUMN} = ?
          AND 後備 = '否';
        """,
        updates,
    )


def _fill_predicted_win_odds_for_meeting(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
) -> None:
    race_nos = [
        int(r["race_no"])
        for r in conn.execute(
            """
            SELECT DISTINCT race_no
            FROM racecard_starters
            WHERE race_date = ? AND racecourse = ?
            ORDER BY race_no ASC;
            """,
            (race_date, racecourse),
        ).fetchall()
    ]

    for rn in race_nos:
        _fill_predicted_win_odds_for_race(conn, race_date, racecourse, rn)


IMMEDIATE_ODDS_GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"

# 必須與你提供的 query 內容「一致」：用於抓每匹 runner 的 winOdds。
IMMEDIATE_ODDS_GRAPHQL_QUERY = """fragment raceFragment on Race {
  id
  no
  raceName_ch
  raceName_en
  postTime
}

query raceMeetings($date: String, $venueCode: String) {
  raceMeetings(date: $date, venueCode: $venueCode) {
    id
    venueCode
    date
    races {
      ...raceFragment
      runners {
        id
        no
        name_ch
        name_en
        winOdds
      }
    }
  }
}
"""


def get_graphql_data(date: str, venue_code: str) -> dict[str, Any]:
    variables = {"date": date, "venueCode": venue_code}
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://bet.hkjc.com",
        "Referer": "https://bet.hkjc.com/",
    }
    payload = {"query": IMMEDIATE_ODDS_GRAPHQL_QUERY, "variables": variables}
    # Some gateways whitelist based on the raw request body;
    # use deterministic serialization to reduce mismatches.
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    resp = requests.post(IMMEDIATE_ODDS_GRAPHQL_URL, headers=headers, data=body, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    if "errors" in data and data["errors"]:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    if "data" not in data:
        raise RuntimeError(f"GraphQL response missing 'data': {data}")
    return data["data"]


def update_win_odds(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    馬號: str,
    win_odds: Any,
) -> None:
    cur = conn.execute(
        """
        UPDATE racecard_starters
           SET "即時賠率" = ?
         WHERE race_date = ?
           AND racecourse = ?
           AND race_no = ?
           AND "馬號" = ?
           AND 後備 = '否';
        """,
        (win_odds, race_date, racecourse, race_no, 馬號),
    )
    if cur.rowcount == 0:
        # 只 update 已存在紀錄；找不到就略過並提示。
        print(f"[略過] 未找到匹配：{race_date} {racecourse} 第{race_no}場 馬號={馬號} 後備=否")


def _update_immediate_odds_for_meeting_from_graphql(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse_code: str,
) -> None:
    data = get_graphql_data(date=race_date, venue_code=racecourse_code)
    meetings = data.get("raceMeetings") or []
    if not meetings:
        return

    for meeting in meetings:
        m_date = str(meeting.get("date") or race_date)
        m_venue = str(meeting.get("venueCode") or racecourse_code)

        for race in meeting.get("races") or []:
            race_no = race.get("no")
            if race_no is None:
                continue

            for runner in race.get("runners") or []:
                馬號 = runner.get("no")
                if 馬號 is None:
                    continue
                馬號_str = str(馬號)
                win_odds = runner.get("winOdds")

                update_win_odds(
                    conn=conn,
                    race_date=m_date,
                    racecourse=m_venue,
                    race_no=int(race_no),
                    馬號=馬號_str,
                    win_odds=win_odds,
                )


def _race_no_to_label(race_no: int) -> str:
    """
    Convert 1 -> '第一場', 2 -> '第二場', 10 -> '第十場', ...
    """
    cn_digit = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
    }
    if race_no in cn_digit:
        return f"第{cn_digit[race_no]}場"
    if 10 <= race_no <= 19:
        if race_no == 10:
            return "第十場"
        return f"第十{cn_digit.get(race_no % 10, '')}場"
    return f"第 {race_no} 場"


def _clean_cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _extract_horse_id_from_row(tr: Any) -> str | None:
    # In the row there are usually <a href="...horse?horseid=HK_xxxx">...</a>.
    for a in tr.find_all("a", href=True):
        href = str(a["href"])
        m = re.search(r"[?&]horseid=([^&\s]+)", href, re.IGNORECASE)
        if m:
            hid = m.group(1).strip()
            if hid:
                return hid
    return None


def _parse_latest_meeting_from_base_racecard(html: str) -> tuple[str, str] | None:
    """
    Return (racedate_yyyy_mm_dd, racecourse_code).

    We heuristically parse the first occurrence like:
      第 1 場 - ... 2026年3月22日, 星期日, 沙田, 13:00草地, ...
    """
    # Date
    m_date = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", html)
    if not m_date:
        return None
    yyyy, mm, dd = m_date.groups()
    racedate = f"{int(yyyy):04d}/{int(mm):02d}/{int(dd):02d}"

    # Course name shortly after date
    tail = html[m_date.end() : m_date.end() + 200]
    m_course = re.search(r"(沙田|跑馬地|從化)", tail)
    if not m_course:
        return None
    course_name = m_course.group(1)
    code = COURSE_NAME_TO_CODE.get(course_name)
    if not code:
        return None
    return racedate, code


def _extract_race_nos_from_base_racecard(html: str) -> list[int]:
    """
    Extract race numbers from the base racecard page.

    We look for pattern like: "第 1 場" and return sorted unique integers.
    """
    # Limit search window to reduce false matches.
    m_date = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", html)
    window = html
    if m_date:
        window = html[m_date.end() : m_date.end() + 8000]

    nos = [int(m.group(1)) for m in re.finditer(r"第\s*(\d+)\s*場", window)]
    uniq = sorted(set(nos))
    return uniq


def _race_finished(race_date: str, race_no: int, racecourse_code: str) -> bool:
    """
    Determine whether a race already has result rows.

    Note:
    HKJC 的 `localresults` 目前似乎會忽略/覆蓋傳入的 `racedate` 參數，
    導致用它判斷「該期是否已開跑/已完成」會誤判，進而跳過所有 racecard 入庫。

    在這版中改採簡單且穩定的規則：
    - `race_date` 小於今天 => 視為已完成，跳過重新抓取
    - `race_date` 等於或大於今天 => 不跳過，確保能抓到資料
    """
    # race_date expected format: YYYY/MM/DD
    try:
        dt = datetime.strptime(race_date.replace("-", "/"), "%Y/%m/%d").date()
    except Exception:
        # 如果 racedate 解析失敗，就不要跳過，避免漏抓。
        return False

    today = datetime.now().date()
    return dt < today


def _fetch_racecard_html(race_date: str, race_no: int, racecourse_code: str) -> str:
    url = RACECARD_BASE_URL
    resp = requests.get(
        url,
        params={"racedate": race_date, "RaceNo": race_no, "Racecourse": racecourse_code},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def _parse_race_event_panel_from_racecard_html(
    html: str, race_no: int
) -> dict[str, str | None]:
    """
    從 HKJC 排位頁（單場）上方資訊區解析場次資料。

    典型結構（zh-hk）：
      第 N 場 - {場事名稱}</span><br>
      {YYYY年M月D日}, {星期}, {馬場}, {HH:MM}<br>
      {草地|全天候}, "{賽道}" 賽道, {途程}<br>
      獎金: ..., 評分: ..., {班次}</div>

    若頁面為「沒有相關資料」錯誤頁，回傳全部 None。
    """
    empty: dict[str, str | None] = {
        "event_name": None,
        "calendar_date": None,
        "weekday": None,
        "meeting_venue": None,
        "post_time": None,
        "race_course_area": None,
        "track_name": None,
        "distance": None,
        "prize_money": None,
        "rating_range": None,
        "race_class": None,
    }
    if not html:
        return empty
    if "沒有相關資料" in html and "errorContainer" in html:
        return empty

    m = re.search(
        rf"第\s*{race_no}\s*場\s*-\s*([^<]+)</span><br>"
        r"([^<]+)<br>"
        r"([^<]+)<br>"
        r"([^<]+)</div>",
        html,
    )
    if not m:
        return empty

    event_name = _clean_cell_text(m.group(1))
    line1 = _clean_cell_text(m.group(2))
    line2 = _clean_cell_text(m.group(3))
    line3 = _clean_cell_text(m.group(4))

    m1 = re.match(
        r"^(\d{4}年\d{1,2}月\d{1,2}日)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*(\d{1,2}:\d{2})\s*$",
        line1,
    )
    calendar_date = weekday = meeting_venue = post_time = None
    if m1:
        calendar_date, weekday, meeting_venue, post_time = (
            _clean_cell_text(m1.group(1)),
            _clean_cell_text(m1.group(2)),
            _clean_cell_text(m1.group(3)),
            _clean_cell_text(m1.group(4)),
        )

    m2 = re.match(r"^(.+?)\s*,\s*\"([^\"]+)\"\s*賽道\s*,\s*(.+)\s*$", line2)
    race_course_area = track_name = distance = None
    if m2:
        race_course_area = _clean_cell_text(m2.group(1))
        track_name = _clean_cell_text(m2.group(2))
        distance = _clean_cell_text(m2.group(3))

    m3 = re.match(
        r"^獎金:\s*(.+?)\s*,\s*評分:\s*(.+?)\s*,\s*(.+)\s*$",
        line3,
    )
    prize_money = rating_range = race_class = None
    if m3:
        prize_money = _clean_cell_text(m3.group(1))
        rating_range = _clean_cell_text(m3.group(2))
        race_class = _clean_cell_text(m3.group(3))

    out = dict(empty)
    out.update(
        {
            "event_name": event_name or None,
            "calendar_date": calendar_date,
            "weekday": weekday,
            "meeting_venue": meeting_venue,
            "post_time": post_time,
            "race_course_area": race_course_area,
            "track_name": track_name,
            "distance": distance,
            "prize_money": prize_money,
            "rating_range": rating_range,
            "race_class": race_class,
        }
    )
    return out


def _upsert_race_event_info(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    info: dict[str, str | None],
) -> None:
    if not any(v for v in info.values() if v):
        return
    conn.execute(
        f"""
        INSERT INTO {RACECARD_RACE_INFO_TABLE} (
            race_date, racecourse, race_no,
            event_name, calendar_date, weekday, meeting_venue, post_time,
            race_course_area, track_name, distance,
            prize_money, rating_range, race_class
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(race_date, racecourse, race_no) DO UPDATE SET
            event_name=excluded.event_name,
            calendar_date=excluded.calendar_date,
            weekday=excluded.weekday,
            meeting_venue=excluded.meeting_venue,
            post_time=excluded.post_time,
            race_course_area=excluded.race_course_area,
            track_name=excluded.track_name,
            distance=excluded.distance,
            prize_money=excluded.prize_money,
            rating_range=excluded.rating_range,
            race_class=excluded.race_class;
        """,
        (
            race_date,
            racecourse,
            race_no,
            info.get("event_name"),
            info.get("calendar_date"),
            info.get("weekday"),
            info.get("meeting_venue"),
            info.get("post_time"),
            info.get("race_course_area"),
            info.get("track_name"),
            info.get("distance"),
            info.get("prize_money"),
            info.get("rating_range"),
            info.get("race_class"),
        ),
    )


def _build_race_info_header_widget(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
) -> QFrame:
    """每個分頁頂部：場次摘要；每行排 4 組「標籤＋內容」（約佔 3～4 欄資料寬）。"""
    frame = QFrame()
    frame.setStyleSheet(
        """
        QFrame#raceInfoFrame {
            background: #f4f7fc;
            border: 1px solid #b8c9e0;
            border-radius: 6px;
            padding: 6px 8px;
        }
        """
    )
    frame.setObjectName("raceInfoFrame")

    row = conn.execute(
        f"""
        SELECT event_name, calendar_date, weekday, meeting_venue, post_time,
               race_course_area, track_name, distance,
               prize_money, rating_range, race_class
        FROM {RACECARD_RACE_INFO_TABLE}
        WHERE race_date = ? AND racecourse = ? AND race_no = ?
        LIMIT 1;
        """,
        (race_date, racecourse, race_no),
    ).fetchone()

    grid = QGridLayout(frame)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(2)
    grid.setContentsMargins(4, 4, 4, 4)
    # 每行 4 組，每組佔 2 欄（標籤 | 內容）→ 共 8 欄；內容欄平均分配剩餘寬度
    pairs_per_row = 4
    for j in range(pairs_per_row):
        grid.setColumnStretch(j * 2 + 1, 1)

    pairs: list[tuple[str, str]] = [
        ("場事名稱", "event_name"),
        ("日子", "calendar_date"),
        ("星期", "weekday"),
        ("比賽場地", "meeting_venue"),
        ("時間", "post_time"),
        ("場地", "race_course_area"),
        ("賽道", "track_name"),
        ("途程", "distance"),
        ("獎金", "prize_money"),
        ("評分範圍", "rating_range"),
        ("班次", "race_class"),
    ]

    if not row:
        tip = QLabel("（此場尚無場次摘要：請按「更新資料」重新抓取，或該日賽事頁面暫無資料）")
        tip.setWordWrap(True)
        grid.addWidget(tip, 0, 0, 1, pairs_per_row * 2)
        return frame

    rdict = dict(row)
    for i, (cn, key) in enumerate(pairs):
        row_idx = i // pairs_per_row
        slot = i % pairs_per_row
        base_col = slot * 2

        lk = QLabel(f"{cn}：")
        lk.setStyleSheet("color: #1a3a5c; font-weight: 600;")
        lk.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val = rdict.get(key)
        if (
            key == "track_name"
            and val is not None
            and str(val).strip()
            and "賽道" not in str(val)
        ):
            val = f'{str(val).strip()} 賽道'
        vv = QLabel("—" if val is None or str(val).strip() == "" else str(val))
        vv.setWordWrap(True)
        vv.setStyleSheet("color: #222;")
        vv.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lk, row_idx, base_col, alignment=Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(vv, row_idx, base_col + 1)

    return frame


def _parse_racecard_tables(
    html: str,
) -> tuple[list[str], list[tuple[str | None, list[str]]], list[str], list[tuple[str | None, list[str]]]] | None:
    """
    Returns:
      main_headers, main_rows_values, reserve_headers, reserve_rows_values

    Each rows_values is aligned to its corresponding headers order.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return None

    def table_headers(table: Any) -> list[str]:
        tr = table.find("tr")
        if not tr:
            return []
        cells = tr.find_all(["th", "td"])
        if not cells:
            return []
        return [_clean_cell_text(c.get_text(" ", strip=True)) for c in cells]

    def looks_like_main(headers: list[str]) -> bool:
        joined = " ".join(headers)
        return ("馬名" in joined and ("檔位" in joined or "負磅" in joined)) or "檔位" in headers

    def looks_like_reserve(headers: list[str]) -> bool:
        joined = " ".join(headers)
        return ("排位體重" in joined and "優先參賽次序" in joined) or ("後備" in joined)

    main_table = None
    reserve_table = None
    main_headers: list[str] = []
    reserve_headers: list[str] = []

    for t in tables:
        headers = table_headers(t)
        if not headers:
            continue
        if main_table is None and looks_like_main(headers):
            main_table = t
            main_headers = headers
        elif reserve_table is None and looks_like_reserve(headers):
            reserve_table = t
            reserve_headers = headers

    if main_table is None:
        return None

    def rows_from_table(table: Any, headers: list[str]) -> list[tuple[str | None, list[str]]]:
        rows: list[tuple[str | None, list[str]]] = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds:
                continue
            values = [_clean_cell_text(td.get_text(" ", strip=True)) for td in tds]
            horse_id = _extract_horse_id_from_row(tr)
            # Align to header length (ignore extra columns).
            if len(values) >= len(headers):
                rows.append((horse_id, values[: len(headers)]))
            else:
                # If fewer cells, pad.
                padded = values + [""] * (len(headers) - len(values))
                rows.append((horse_id, padded))
        return rows

    main_rows = rows_from_table(main_table, main_headers)
    reserve_rows: list[tuple[str | None, list[str]]] = []
    if reserve_table is not None and reserve_headers:
        reserve_rows = rows_from_table(reserve_table, reserve_headers)

    return main_headers, main_rows, reserve_headers, reserve_rows


@dataclass(frozen=True, slots=True)
class LatestMeeting:
    racedate: str
    racecourse_code: str


def _upsert_racecard_for_race(
    conn: sqlite3.Connection,
    config: AppConfig,
    race_date: str,
    race_no: int,
    racecourse_code: str,
) -> int:
    html = _fetch_racecard_html(race_date, race_no, racecourse_code)
    info = _parse_race_event_panel_from_racecard_html(html, race_no)
    _upsert_race_event_info(conn, race_date, racecourse_code, race_no, info)
    parsed = _parse_racecard_tables(html)
    if not parsed:
        return 0

    main_headers, main_rows, reserve_headers, reserve_rows = parsed
    reserve_headers = reserve_headers or []

    # Delete previous rows for the race (both reserve/main).
    conn.execute(
        """
        DELETE FROM racecard_starters
        WHERE race_date = ? AND racecourse = ? AND race_no = ?;
        """,
        (race_date, racecourse_code, race_no),
    )

    def qident(col: str) -> str:
        return '"' + col.replace('"', '""') + '"'

    fixed_cols = list(RACECARD_DISPLAY_FIELDS)
    insert_cols = [
        "race_date",
        "racecourse",
        "race_no",
        "後備",
        RACECARD_HIDDEN_HORSE_ID_COLUMN,
        *fixed_cols,
        RACECARD_PREDICTED_ODDS_COLUMN,
        RACECARD_IMMEDIATE_ODDS_COLUMN,
    ]
    insert_cols_sql = [qident(c) for c in insert_cols]
    placeholders = ",".join(["?"] * len(insert_cols_sql))
    insert_sql = (
        f"INSERT INTO racecard_starters ({','.join(insert_cols_sql)}) VALUES ({placeholders});"
    )

    def insert_row(
        reserve_flag: str,
        horse_id: str | None,
        headers: list[str],
        values: list[str],
    ) -> None:
        fields = _extract_racecard_display_fields(headers, values)
        # Keep empty strings as NULL for cleaner display/sorting.
        row_vals: list[Any] = [race_date, racecourse_code, race_no, reserve_flag, horse_id]
        row_vals.extend(fields.get(c) for c in fixed_cols)
        # 預測賠率/即時賠率目前未做抓取解析：先寫入空值，後續用往績模型補齊。
        row_vals.append(None)
        row_vals.append(None)
        conn.execute(insert_sql, row_vals)

    inserted = 0
    # Main rows.
    for _hid, values in main_rows:
        insert_row("否", _hid, main_headers, values)
        inserted += 1

    # Reserve rows.
    if reserve_rows and reserve_headers:
        for _hid, values in reserve_rows:
            insert_row("是", _hid, reserve_headers, values)
            inserted += 1

    return inserted


def init_racecard_db(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        # Schema is centrally managed in hppv.schema; UI only ensures DB is initialized.
        conn.executescript(racecard_base_schema_sql(RACECARD_RACE_INFO_TABLE))
        # racecard_starters 欄位定義與兼容修復集中在 hppv.schema。
        ensure_racecard_starters_columns(
            conn=conn,
            starters_table="racecard_starters",
            display_fields=RACECARD_DISPLAY_FIELDS,
            predicted_odds_col=RACECARD_PREDICTED_ODDS_COLUMN,
            immediate_odds_col=RACECARD_IMMEDIATE_ODDS_COLUMN,
        )
    finally:
        conn.close()


def list_stored_racecard_meetings(config: AppConfig) -> list[tuple[str, str]]:
    """
    資料庫內已有排位資料的 (賽事日期, 場地代碼)，新至舊排序。
    日期格式與庫內一致：YYYY/MM/DD。
    """
    init_racecard_db(config.db_path)
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
            FROM racecard_starters
            ORDER BY race_date DESC, racecourse ASC;
            """
        ).fetchall()
        return [(str(r["race_date"]), str(r["racecourse"])) for r in rows]
    finally:
        conn.close()


def update_racecard_meeting_from_web(
    config: AppConfig, race_date: str, racecourse_code: str
) -> LatestMeeting | None:
    """
    依指定賽日 + 場地從 HKJC 下載該賽馬日各場排位並寫入 SQLite（不套用「已賽畢則跳過」邏輯，方便補抓過往）。

    race_date: YYYY/MM/DD；racecourse_code: ST / HV / FC
    """
    init_racecard_db(config.db_path)
    resp = requests.get(
        RACECARD_BASE_URL,
        params={"racedate": race_date, "Racecourse": racecourse_code},
        timeout=25,
    )
    resp.raise_for_status()
    html = resp.text
    if "沒有相關資料" in html and "errorContainer" in html:
        return None

    race_nos = _extract_race_nos_from_base_racecard(html)
    if not race_nos:
        race_nos = list(range(1, 15))

    meeting = LatestMeeting(racedate=race_date, racecourse_code=racecourse_code)
    conn = get_connection(config.db_path)
    try:
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        race_nos_csv = ",".join(str(n) for n in race_nos)
        for race_no in race_nos:
            _upsert_racecard_for_race(conn, config, race_date, race_no, racecourse_code)
            conn.commit()

        _fill_predicted_win_odds_for_meeting(conn, race_date, racecourse_code)

        try:
            conn.execute("SAVEPOINT sp_immediate_odds_graphql;")
            _update_immediate_odds_for_meeting_from_graphql(
                conn=conn,
                race_date=race_date,
                racecourse_code=racecourse_code,
            )
            conn.execute("RELEASE sp_immediate_odds_graphql;")
        except Exception as e:
            conn.execute("ROLLBACK TO sp_immediate_odds_graphql;")
            conn.execute("RELEASE sp_immediate_odds_graphql;")
            print(f"[即時賠率] GraphQL 更新失敗（略過）：{e}")

        conn.execute(
            """
            INSERT INTO racecard_meta(key, value)
            VALUES('last_update_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (now_txt,),
        )
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value) VALUES
              ('last_source_race_date', ?),
              ('last_source_racecourse_code', ?),
              ('last_source_race_nos', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (race_date, racecourse_code, race_nos_csv),
        )
        conn.commit()
        return meeting
    finally:
        conn.close()


def format_racecard_source_urls_for_meeting(
    config: AppConfig, meeting: LatestMeeting
) -> str:
    """依目前資料庫內該賽日場次組出來源 URL 字串（供標籤顯示）。"""
    conn = get_connection(config.db_path)
    try:
        race_nos = [
            str(r["race_no"])
            for r in conn.execute(
                """
                SELECT DISTINCT race_no
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC;
                """,
                (meeting.racedate, meeting.racecourse_code),
            ).fetchall()
        ]
        if not race_nos:
            return f"來源 URL：{RACECARD_BASE_URL}"
        urls: list[str] = []
        for rn in race_nos[:8]:
            urls.append(
                f"{RACECARD_BASE_URL}?racedate={meeting.racedate}&RaceNo={rn}&Racecourse={meeting.racecourse_code}"
            )
        tail = ""
        if len(race_nos) > 8:
            tail = f"…（另 {len(race_nos) - 8} 場）"
        return "來源 URL：" + "\n".join(urls) + tail
    finally:
        conn.close()


def update_latest_racecard_data(config: AppConfig) -> LatestMeeting | None:
    """
    Auto-update latest meeting:
    - infer latest meeting from base racecard page
    - fetch racecards for races in that meeting until we stop finding data
    - skip races that already have results
    """
    init_racecard_db(config.db_path)
    base_html = requests.get(RACECARD_BASE_URL, timeout=20).text
    latest = _parse_latest_meeting_from_base_racecard(base_html)
    if not latest:
        return None
    race_date, racecourse_code = latest
    meeting = LatestMeeting(racedate=race_date, racecourse_code=racecourse_code)

    race_nos = _extract_race_nos_from_base_racecard(base_html)
    if not race_nos:
        # Fallback to typical range.
        race_nos = list(range(1, 15))

    conn = get_connection(config.db_path)
    try:
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        race_nos_csv = ",".join(str(n) for n in race_nos)
        for race_no in race_nos:
            before = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ? AND race_no = ?;
                """,
                (race_date, racecourse_code, race_no),
            ).fetchone()["c"]

            # Only skip if finished AND we already have data for this race.
            # Additionally, if 馬號 欄位是空的，代表我們上次的解析/映射不完整，需要重抓補齊。
            horse_no_col = RACECARD_DISPLAY_FIELDS[0]
            qident = '"' + horse_no_col.replace('"', '""') + '"'
            missing_horse_no = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ? AND race_no = ?
                  AND ({qident} IS NULL OR TRIM({qident}) = '');
                """,
                (race_date, racecourse_code, race_no),
            ).fetchone()["c"]

            horse_id_col = RACECARD_HIDDEN_HORSE_ID_COLUMN
            missing_horse_id = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ? AND race_no = ?
                  AND ({horse_id_col} IS NULL OR TRIM({horse_id_col}) = '');
                """,
                (race_date, racecourse_code, race_no),
            ).fetchone()["c"]

            if (
                _race_finished(race_date, race_no, racecourse_code)
                and before
                and before > 0
                and missing_horse_no == 0
                and missing_horse_id == 0
            ):
                continue

            inserted = _upsert_racecard_for_race(
                conn, config, race_date, race_no, racecourse_code
            )
            conn.commit()
            _ = before  # keep for possible future logic
            _ = inserted

        # Fill predicted win odds for this meeting (only where 後備='否').
        _fill_predicted_win_odds_for_meeting(conn, race_date, racecourse_code)

        # After we have racecard rows, update "即時賠率" from HKJC GraphQL (winOdds).
        # If GraphQL is blocked/fails, keep existing odds (currently predicted).
        try:
            conn.execute("SAVEPOINT sp_immediate_odds_graphql;")
            _update_immediate_odds_for_meeting_from_graphql(
                conn=conn,
                race_date=race_date,
                racecourse_code=racecourse_code,
            )
            conn.execute("RELEASE sp_immediate_odds_graphql;")
        except Exception as e:
            conn.execute("ROLLBACK TO sp_immediate_odds_graphql;")
            conn.execute("RELEASE sp_immediate_odds_graphql;")
            print(f"[即時賠率] GraphQL 更新失敗（略過）：{e}")

        # Always update "last update time" when we have determined a meeting.
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value)
            VALUES('last_update_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (now_txt,),
        )
        conn.commit()

        # Save sources attempted (for UI display / debugging).
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value) VALUES
              ('last_source_race_date', ?),
              ('last_source_racecourse_code', ?),
              ('last_source_race_nos', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (race_date, racecourse_code, race_nos_csv),
        )
        conn.commit()
        return meeting
    finally:
        conn.close()


def get_last_update_at(config: AppConfig) -> str:
    conn = get_connection(config.db_path)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM racecard_meta WHERE key='last_update_at' LIMIT 1;"
            ).fetchone()
            return str(row["value"]) if row else ""
        except sqlite3.OperationalError:
            # Table not created yet.
            return ""
    finally:
        conn.close()


def get_racecard_meta_value(config: AppConfig, key: str) -> str:
    conn = get_connection(config.db_path)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM racecard_meta WHERE key = ? LIMIT 1;",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else ""
        except sqlite3.OperationalError:
            return ""
    finally:
        conn.close()


class RacecardPanel(QWidget):
    """
    排位（Racecard）內容區：可嵌入主視窗，亦可由 `RacecardRankWindow` 包成獨立視窗。
    """
    horse_double_clicked = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._meeting: LatestMeeting | None = None

        root = QVBoxLayout()
        # Ensure tables exist before reading meta.
        init_racecard_db(self._config.db_path)
        self._last_update_label = QLabel("上次更新時間：")
        self._source_label = QLabel("來源 URL：")
        self._update_btn = QPushButton("更新資料")
        self._update_btn.clicked.connect(self._on_update_clicked)

        root.addWidget(self._last_update_label)
        root.addWidget(self._source_label)
        root.addWidget(self._update_btn)

        # --- 查閱過往：資料庫已有賽日 / 手動補抓 ---
        hist_row = QHBoxLayout()
        hist_row.addWidget(QLabel("查閱資料庫："))
        self._history_combo = QComboBox()
        self._history_combo.setMinimumWidth(300)
        hist_row.addWidget(self._history_combo, 1)
        self._btn_load_stored = QPushButton("載入此賽日")
        self._btn_load_stored.clicked.connect(self._on_load_stored_meeting_clicked)
        hist_row.addWidget(self._btn_load_stored)
        self._btn_refresh_history = QPushButton("重新整理列表")
        self._btn_refresh_history.clicked.connect(self._refresh_history_combo)
        hist_row.addWidget(self._btn_refresh_history)
        root.addLayout(hist_row)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("手動補抓："))
        self._manual_date_edit = QLineEdit()
        self._manual_date_edit.setPlaceholderText("YYYY/MM/DD")
        self._manual_date_edit.setMaximumWidth(130)
        manual_row.addWidget(self._manual_date_edit)
        self._manual_course_combo = QComboBox()
        for code in ("ST", "HV", "FC"):
            disp = COURSE_CODE_TO_DISPLAY.get(code, code)
            self._manual_course_combo.addItem(f"{disp}（{code}）", code)
        manual_row.addWidget(self._manual_course_combo)
        self._btn_download_meeting = QPushButton("從網站補抓並載入")
        self._btn_download_meeting.clicked.connect(self._on_download_meeting_clicked)
        manual_row.addWidget(self._btn_download_meeting)
        manual_row.addStretch(1)
        root.addLayout(manual_row)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)

        self.setLayout(root)

        self._refresh_last_update_label()
        try:
            self._meeting = update_latest_racecard_data(self._config)
        except Exception as e:
            QMessageBox.warning(self, "更新失敗", f"自動抓取失敗：{e}")
        self._refresh_last_update_label()
        self._refresh_history_combo(select_meeting=self._meeting)
        self._refresh_source_label()
        self._load_grid()

    def _history_combo_display(self, race_date: str, racecourse: str) -> str:
        disp = COURSE_CODE_TO_DISPLAY.get(racecourse, racecourse)
        return f"{race_date}  {disp}（{racecourse}）"

    def _refresh_history_combo(self, select_meeting: LatestMeeting | None = None) -> None:
        self._history_combo.blockSignals(True)
        self._history_combo.clear()
        pairs = list_stored_racecard_meetings(self._config)
        for rd, rc in pairs:
            self._history_combo.addItem(self._history_combo_display(rd, rc), (rd, rc))
        self._history_combo.blockSignals(False)

        pick = select_meeting or self._meeting
        if pick and pairs:
            target = (pick.racedate, pick.racecourse_code)
            for i in range(self._history_combo.count()):
                if self._history_combo.itemData(i) == target:
                    self._history_combo.setCurrentIndex(i)
                    break

    def _on_load_stored_meeting_clicked(self) -> None:
        idx = self._history_combo.currentIndex()
        if idx < 0 or self._history_combo.count() == 0:
            QMessageBox.information(
                self,
                "查閱過往",
                "資料庫內尚無任何排位紀錄。請先按「更新資料」或使用下方「手動補抓」。",
            )
            return
        data = self._history_combo.itemData(idx)
        if not data or not isinstance(data, tuple) or len(data) != 2:
            return
        rd, rc = str(data[0]), str(data[1])
        self._meeting = LatestMeeting(racedate=rd, racecourse_code=rc)
        self._refresh_source_label()
        self._load_grid()

    def _on_download_meeting_clicked(self) -> None:
        raw = self._manual_date_edit.text().strip()
        if not re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", raw):
            QMessageBox.warning(
                self,
                "日期格式",
                "請輸入賽事日期，格式：YYYY/MM/DD\n例如：2026/03/25",
            )
            return
        # 正規化成與庫內一致（補零）
        try:
            dt = datetime.strptime(raw, "%Y/%m/%d")
            norm = dt.strftime("%Y/%m/%d")
        except ValueError:
            QMessageBox.warning(self, "日期無效", "請確認日期是否真實存在。")
            return

        code = self._manual_course_combo.currentData()
        if not code:
            code = "ST"
        code = str(code)
        try:
            meeting = update_racecard_meeting_from_web(self._config, norm, code)
            if meeting is None:
                QMessageBox.warning(
                    self,
                    "補抓失敗",
                    "HKJC 該頁回傳「沒有相關資料」或無法解析場次，請改日期／場地再試。",
                )
                return
            self._meeting = meeting
            self._manual_date_edit.setText(norm)
            self._refresh_last_update_label()
            self._refresh_history_combo(select_meeting=self._meeting)
            self._refresh_source_label()
            self._load_grid()
        except Exception as e:
            QMessageBox.critical(self, "補抓失敗", str(e))

    def _refresh_last_update_label(self) -> None:
        last = get_last_update_at(self._config)
        if last:
            self._last_update_label.setText(f"上次更新時間：{last}")
        else:
            self._last_update_label.setText("上次更新時間：尚未更新")

    def _refresh_source_label(self) -> None:
        # 以目前檢視中的賽日為準（可查閱過往），避免仍顯示 meta 裡「最後一次下載」的 URL。
        if self._meeting:
            self._source_label.setText(
                format_racecard_source_urls_for_meeting(self._config, self._meeting)
            )
            return

        race_date = get_racecard_meta_value(self._config, "last_source_race_date")
        racecourse = get_racecard_meta_value(self._config, "last_source_racecourse_code")
        race_nos_csv = get_racecard_meta_value(self._config, "last_source_race_nos")

        if not race_date or not racecourse:
            self._source_label.setText(f"來源 URL：{RACECARD_BASE_URL}")
            return

        race_nos: list[str] = []
        if race_nos_csv:
            race_nos = [x.strip() for x in race_nos_csv.split(",") if x.strip()]

        # Avoid too long UI strings.
        urls: list[str] = []
        for rn in race_nos[:8]:
            urls.append(
                f"{RACECARD_BASE_URL}?racedate={race_date}&RaceNo={rn}&Racecourse={racecourse}"
            )

        tail = ""
        if len(race_nos) > 8:
            tail = f"…（另 {len(race_nos) - 8} 場）"

        self._source_label.setText("來源 URL：" + "\n".join(urls) + tail)

    def _on_update_clicked(self) -> None:
        try:
            self._meeting = update_latest_racecard_data(self._config)
            self._refresh_last_update_label()
            self._refresh_history_combo(select_meeting=self._meeting)
            self._refresh_source_label()
            self._load_grid()
        except Exception as e:
            QMessageBox.critical(self, "更新失敗", str(e))

    def _load_grid(self) -> None:
        conn = get_connection(self._config.db_path)
        try:
            display_cols = list(RACECARD_DISPLAY_COLUMNS)
            display_cols_sql = [f'"{c}"' for c in display_cols]

            # Filter by current meeting if we have it.
            if self._meeting:
                race_date = self._meeting.racedate
                racecourse = self._meeting.racecourse_code
            else:
                # Fallback: take the latest race_date and its first course.
                row = conn.execute(
                    """
                    SELECT race_date, racecourse
                    FROM racecard_starters
                    ORDER BY race_date DESC
                    LIMIT 1;
                    """
                ).fetchone()
                if not row:
                    self._tabs.clear()
                    return
                race_date = str(row["race_date"])
                racecourse = str(row["racecourse"])

            race_nos = [
                int(rn["race_no"])
                for rn in conn.execute(
                    """
                    SELECT DISTINCT race_no
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ?
                    ORDER BY race_no ASC;
                    """,
                    (race_date, racecourse),
                ).fetchall()
            ]

            self._tabs.clear()

            if not race_nos:
                QMessageBox.information(
                    self,
                    "沒有資料",
                    "沒有抓到任何排位資料可顯示（可能是 racecard 解析失敗或該期尚未開跑）。\n\n"
                    f"{self._source_label.text()}",
                )
                return

            order_sql = "CASE WHEN 後備 = '否' THEN 0 ELSE 1 END"
            select_cols_sql = ",".join(display_cols_sql)
            horse_id_col = RACECARD_HIDDEN_HORSE_ID_COLUMN
            for race_no in race_nos:
                table = QTableWidget()
                table.setSortingEnabled(True)
                # Make row selection clearer/consistent.
                table.setSelectionBehavior(QTableWidget.SelectRows)
                table.setSelectionMode(QTableWidget.SingleSelection)
                # Highlight selected row (nice contrast).
                table.setStyleSheet(
                    """
                    QTableWidget::item:selected {
                        background: #FFF2CC;
                        color: #000000;
                    }
                    """
                )
                table.setColumnCount(len(display_cols))
                table.setHorizontalHeaderLabels(display_cols)

                rows = conn.execute(
                    f"""
                    SELECT {select_cols_sql}, {horse_id_col} AS __horse_id
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ? AND race_no = ?
                    ORDER BY {order_sql}, id ASC;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchall()

                if not rows:
                    continue

                table.setRowCount(0)
                for r in rows:
                    row_idx = table.rowCount()
                    table.insertRow(row_idx)
                    row_horse_id = str(r["__horse_id"] or "").strip()
                    for c_idx, name in enumerate(display_cols):
                        val = r[name]
                        item = QTableWidgetItem("" if val is None else str(val))
                        if c_idx == 0:
                            item.setTextAlignment(Qt.AlignCenter)
                            # 將該列 horse_id 掛在第 1 欄，供雙擊事件轉跳主畫面往績。
                            item.setData(Qt.UserRole, row_horse_id)
                        table.setItem(row_idx, c_idx, item)

                table.resizeColumnsToContents()
                table.itemDoubleClicked.connect(self._on_table_item_double_clicked)

                tab_page = QWidget()
                tab_layout = QVBoxLayout(tab_page)
                tab_layout.setContentsMargins(4, 4, 4, 4)
                tab_layout.setSpacing(8)
                tab_layout.addWidget(
                    _build_race_info_header_widget(conn, race_date, racecourse, race_no)
                )
                tab_layout.addWidget(table, 1)
                self._tabs.addTab(tab_page, _race_no_to_label(race_no))
        finally:
            conn.close()

    def _on_table_item_double_clicked(self, item: QTableWidgetItem) -> None:
        """雙擊排位中的馬匹：查詢 race_records 並即時顯示往績。"""
        if item is None:
            return
        table = item.tableWidget()
        if table is None:
            return
        first = table.item(item.row(), 0)
        if first is None:
            return
        horse_id = str(first.data(Qt.UserRole) or "").strip()
        if not horse_id:
            return
        self._show_recent_form_dialog(horse_id)
        # 保留既有事件，讓主畫面仍可在需要時切至該馬往績。
        self.horse_double_clicked.emit(horse_id)

    def _show_recent_form_dialog(self, horse_id: str, limit: int = 10) -> None:
        """顯示指定馬匹最近往績（簡化版）。"""
        rows = get_recent_form_for_horse(self._config.db_path, horse_id, limit=limit)
        horse_name = ""
        if rows:
            horse_name = str(rows[0]["name_zh"] or rows[0]["name_en"] or "").strip()
        if not horse_name:
            hr = fetch_horse_by_id(self._config.db_path, horse_id)
            if hr is not None:
                horse_name = str(hr["name_zh"] or hr["name_en"] or "").strip()

        title_name = f"{horse_name}（{horse_id}）" if horse_name else horse_id
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{title_name}｜最近往績")
        dlg.resize(980, 360)
        layout = QVBoxLayout(dlg)

        if not rows:
            layout.addWidget(QLabel("暫無往績。"))
            dlg.exec()
            return

        cols: list[tuple[str, str]] = [
            ("race_date", "日期"),
            ("venue", "場地"),
            ("race_no", "場次"),
            ("distance", "途程"),
            ("class_band", "班次"),
            ("draw", "檔位"),
            ("finish_pos", "名次"),
            ("rating", "評分"),
            ("jockey", "騎師"),
            ("trainer", "練馬師"),
            ("time", "時間"),
        ]
        table = QTableWidget(dlg)
        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels([c[1] for c in cols])
        table.setRowCount(len(rows))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        for r_idx, row in enumerate(rows):
            for c_idx, (k, _label) in enumerate(cols):
                val = row[k] if k in row.keys() else ""
                item = QTableWidgetItem("" if val is None else str(val))
                if c_idx in (1, 2, 5, 6, 7):
                    item.setTextAlignment(Qt.AlignCenter)
                table.setItem(r_idx, c_idx, item)
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        dlg.exec()


class RacecardRankWindow(QMainWindow):
    """獨立視窗：僅包一層 `RacecardPanel`（與主程式內嵌同一套 UI）。"""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setWindowTitle("HPPV｜馬匹排位（Racecard）")
        self.resize(1400, 800)
        self.setCentralWidget(RacecardPanel(config, parent=self))


def main() -> int:
    config = default_config()
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    w = RacecardRankWindow(config)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())


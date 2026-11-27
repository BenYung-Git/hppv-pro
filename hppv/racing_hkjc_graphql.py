"""HKJC racing GraphQL helpers for racecard + win odds + same-day results.

Whitelist queries only. Daily results reuse ``raceMeetings`` ``finalPosition``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

from hppv.models import RaceRecord
from hppv.schema import (
    RACECARD_IMMEDIATE_ODDS_COLUMN,
    RACECARD_ODDS_DIFF_COLUMN,
    RACECARD_PREDICTED_ODDS_COLUMN,
    qident,
)

# Official bet.hkjc.com racing client uses base-get.
GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base-get/"

# Exact whitelist body captured from bet.hkjc.com static/js (raceMeetings).
# Do not reformat or slim this string — gateway returns WHITELIST_ERROR otherwise.
RACE_MEETINGS_QUERY = (
    "fragment raceFragment on Race{id no status raceName_en raceName_ch postTime "
    "country_en country_ch distance wageringFieldSize go_en go_ch ratingType "
    "raceTrack{description_en description_ch}"
    "raceCourse{description_en description_ch displayCode}"
    "claCode raceClass_en raceClass_ch judgeSigns{value_en}}"
    "fragment racingBlockFragment on RaceMeeting{"
    "jpEsts:pmPools(oddsTypes:[WIN PLA TCE TRI FF QTT DT TT SixUP]"
    'filters:["jackpot" "estimatedDividend"])'
    "{leg{number races}oddsType jackpot estimatedDividend mergedPoolId}"
    "poolInvs:pmPools(oddsTypes:[WIN PLA QIN QPL CWA CWB CWC IWN FCT TCE TRI FF QTT "
    "DBL TBL DT TT SixUP]){id leg{races}}"
    'penetrometerReadings(filters:["first"]){reading readingTime}'
    'hammerReadings(filters:["first"]){reading readingTime}'
    'changeHistories(filters:["top3"]){type time raceNo runnerNo horseName_ch '
    "horseName_en jockeyName_ch jockeyName_en scratchHorseName_ch "
    "scratchHorseName_en handicapWeight scrResvIndicator}}"
    "query raceMeetings($date:String$venueCode:String){timeOffset{rc}"
    "activeMeetings:raceMeetings{id venueCode date status "
    "races{no postTime status wageringFieldSize}"
    "poolInvs:pmPools(oddsTypes:[WIN PLA QIN QPL CWA CWB CWC IWN FCT TCE TRI FF QTT "
    "DBL TBL DT TT SixUP]){status}}"
    "raceMeetings(date:$date venueCode:$venueCode){id status venueCode date "
    "totalNumberOfRace currentNumberOfRace dateOfWeek meetingType totalInvestment "
    "country{code namech nameen seq}"
    "races{...raceFragment runners{id no standbyNo status name_ch name_en "
    "horse{id code}color barrierDrawNumber handicapWeight currentWeight "
    "currentRating internationalRating gearInfo racingColorFileName allowance "
    "trainerPreference last6run saddleClothNo trumpCard priority finalPosition "
    "deadHeat winOdds jockey{code name_en name_ch}trainer{code name_en name_ch}}}"
    "obSt:pmPools(oddsTypes:[WIN PLA]){leg{races}oddsType comingleStatus}"
    "poolInvs:pmPools(oddsTypes:[WIN PLA QIN QPL CWA CWB CWC IWN FCT TCE TRI FF QTT "
    "DBL TBL DT TT SixUP]){id leg{number races}status sellStatus oddsType "
    "investment mergedPoolId lastUpdateTime}"
    "...racingBlockFragment pmPools(oddsTypes:[]){id}"
    'jkcInstNo:foPools(oddsTypes:[JKC]filters:["top"]){instNo}'
    'tncInstNo:foPools(oddsTypes:[TNC]filters:["top"]){instNo}}}'
)

# Exact whitelist body from bet.hkjc.com static/js (sp4e_racing_pmPool).
SP4E_RACING_PMPOOL_QUERY = """
      query sp4e_racing_pmPool($date: String, $venueCode: String, $oddsTypes: [OddsType], $raceNo: Int) {
          raceMeetings(date: $date, venueCode: $venueCode)
          {
            pmPools(oddsTypes: $oddsTypes, raceNo: $raceNo) {
              id
              status
              sellStatus
              oddsType
              lastUpdateTime
              oddsLastUpdateTime
              guarantee
              minTicketCost
              name_en
              name_ch
              leg {
                number
                races
              }
              cWinSelections {
                composite
                name_ch
                name_en
                starters
              }
              oddsNodes {
                combString
                oddsValue
                hotFavourite
                oddsDropValue
                bankerOdds {
                  combString
                  oddsValue
                }
              }
            }
          }
      }
  """

_WEEKDAY_ZH: dict[str, str] = {
    "MON": "星期一",
    "TUE": "星期二",
    "WED": "星期三",
    "THU": "星期四",
    "FRI": "星期五",
    "SAT": "星期六",
    "SUN": "星期日",
}

_VENUE_DISPLAY: dict[str, str] = {
    "ST": "沙田",
    "HV": "跑馬地",
    "FC": "從化",
}
LOCAL_RACE_VENUE_CODES: frozenset[str] = frozenset(_VENUE_DISPLAY.keys())


def venue_display_name(venue_code: str) -> str:
    """Map ST/HV/FC → Chinese race_records venue label; other values returned trimmed."""
    text = str(venue_code or "").strip()
    if not text:
        return ""
    return _VENUE_DISPLAY.get(text.upper(), text)


def venue_code_name(venue: str) -> str:
    """Map Chinese display or code → ST/HV/FC; unknown values returned trimmed as-is."""
    text = str(venue or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in _VENUE_DISPLAY:
        return upper
    for code, label in _VENUE_DISPLAY.items():
        if text == label:
            return code
    return text


def is_local_venue_code(venue: str) -> bool:
    """True only for ST / HV / FC (local HK/CHT meetings)."""
    code = venue_code_name(venue)
    return code in LOCAL_RACE_VENUE_CODES


def require_local_venue_code(venue: str) -> str:
    """Return normalized ST/HV/FC or raise RacingGraphQLError for foreign venues."""
    code = venue_code_name(venue).upper() if str(venue or "").strip() else ""
    if code not in LOCAL_RACE_VENUE_CODES:
        raise RacingGraphQLError(
            f"非本地馬場，已拒絕下載：venue={venue!r}（只接受 ST/HV/FC）"
        )
    return code


def venue_for_race_records(venue: str) -> str:
    """Normalize ST/HV/FC or Chinese label → race_records.venue Chinese text."""
    code = venue_code_name(venue)
    if code in _VENUE_DISPLAY:
        return _VENUE_DISPLAY[code]
    return str(venue or "").strip()


def other_main_venue_code(venue: str) -> str | None:
    """ST↔HV pair only. Other / unknown venues → None."""
    code = venue_code_name(venue)
    if code == "ST":
        return "HV"
    if code == "HV":
        return "ST"
    return None


def known_venue_zh_labels() -> tuple[str, ...]:
    """Chinese race_records.venue labels used by the project (ST/HV/FC)."""
    return tuple(_VENUE_DISPLAY.values())


def is_known_race_venue_label(venue: str) -> bool:
    """True when venue is already ST/HV/FC code or 沙田/跑馬地/從化."""
    text = str(venue or "").strip()
    if not text:
        return False
    code = venue_code_name(text)
    return code in _VENUE_DISPLAY


def suggest_venue_zh_from_course(course: str) -> str | None:
    """Derive 沙田/跑馬地/從化 from course when exactly one label matches.

    Prefer startswith; else unique substring. Ambiguous / none → None (no invent).
    """
    text = str(course or "").strip()
    if not text:
        return None
    labels = known_venue_zh_labels()
    starts = [v for v in labels if text.startswith(v)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1:
        return None
    contained = [v for v in labels if v in text]
    if len(contained) == 1:
        return contained[0]
    return None

PostFunc = Callable[..., Any]


class RacingGraphQLError(RuntimeError):
    """Raised when HKJC racing GraphQL returns transport or payload errors."""


@dataclass(frozen=True, slots=True)
class ActiveMeeting:
    date_iso: str  # YYYY-MM-DD
    venue_code: str
    status: str | None = None
    total_number_of_race: int | None = None


def to_graphql_date(race_date: str) -> str:
    """Convert DB/UI date (YYYY/MM/DD or YYYY-MM-DD) to GraphQL YYYY-MM-DD."""
    s = (race_date or "").strip().replace("/", "-")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"invalid race_date: {race_date!r}")
    return s


def to_db_date(date_iso: str) -> str:
    """Convert GraphQL YYYY-MM-DD to racecard DB YYYY/MM/DD."""
    s = to_graphql_date(date_iso)
    y, m, d = s.split("-")
    return f"{y}/{m}/{d}"


def to_race_records_date(date_iso: str) -> str:
    """Convert GraphQL YYYY-MM-DD to race_records date (DD/MM/YY).

    Aligns same-day GraphQL results with localresults / horse-page history keys
    so calendar-day filters and complete-row preference can de-dupe.
    """
    s = to_graphql_date(date_iso)
    y, m, d = s.split("-")
    return f"{int(d):02d}/{int(m):02d}/{y[2:]}"


def _default_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "https://bet.hkjc.com",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def _post_graphql(
    *,
    operation_name: str,
    query: str,
    variables: Mapping[str, Any],
    url: str = GRAPHQL_URL,
    timeout: float = 30,
    post_func: PostFunc | None = None,
) -> dict[str, Any]:
    payload = {
        "operationName": operation_name,
        "variables": dict(variables),
        "query": query,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    poster = post_func or requests.post
    resp = poster(url, headers=_default_headers(), data=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RacingGraphQLError(f"GraphQL errors: {data['errors']}")
    if "data" not in data or data["data"] is None:
        raise RacingGraphQLError(f"GraphQL response missing data: {data}")
    return data["data"]


def fetch_race_meetings(
    date: str,
    venue_code: str,
    *,
    url: str = GRAPHQL_URL,
    timeout: float = 30,
    post_func: PostFunc | None = None,
) -> dict[str, Any]:
    """operationName=raceMeetings; variables date + venueCode (ISO date)."""
    return _post_graphql(
        operation_name="raceMeetings",
        query=RACE_MEETINGS_QUERY,
        variables={"date": to_graphql_date(date), "venueCode": venue_code},
        url=url,
        timeout=timeout,
        post_func=post_func,
    )


def fetch_sp4e_pm_pool(
    date: str,
    venue_code: str,
    *,
    odds_types: Sequence[str] = ("WIN",),
    race_no: int | None = None,
    url: str = GRAPHQL_URL,
    timeout: float = 30,
    post_func: PostFunc | None = None,
) -> dict[str, Any]:
    """operationName=sp4e_racing_pmPool; WIN/PLA/QIN/QPL via oddsNodes."""
    variables: dict[str, Any] = {
        "date": to_graphql_date(date),
        "venueCode": venue_code,
        "oddsTypes": list(odds_types),
        "raceNo": race_no,
    }
    return _post_graphql(
        operation_name="sp4e_racing_pmPool",
        query=SP4E_RACING_PMPOOL_QUERY,
        variables=variables,
        url=url,
        timeout=timeout,
        post_func=post_func,
    )


def pick_active_meeting(data: Mapping[str, Any]) -> ActiveMeeting | None:
    """Prefer activeMeetings alias; fall back to raceMeetings list.

    Skips foreign venue codes (only ST/HV/FC).
    """
    for key in ("activeMeetings", "raceMeetings"):
        meetings = data.get(key) or []
        for m in meetings:
            date_iso = m.get("date")
            venue = m.get("venueCode")
            if not date_iso or not venue:
                continue
            if not is_local_venue_code(str(venue)):
                continue
            total = m.get("totalNumberOfRace")
            if total is None:
                races = m.get("races") or []
                total = len(races) if races else None
            return ActiveMeeting(
                date_iso=str(date_iso),
                venue_code=str(venue).strip().upper(),
                status=(str(m["status"]) if m.get("status") is not None else None),
                total_number_of_race=int(total) if total is not None else None,
            )
    return None


def _txt(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _post_time_hhmm(post_time: Any) -> str | None:
    s = _txt(post_time)
    if not s:
        return None
    # 2026-11-18T18:40:00+08:00 → 18:40
    m = re.search(r"T(\d{2}:\d{2})", s)
    if m:
        return m.group(1)
    m2 = re.fullmatch(r"(\d{1,2}:\d{2})", s)
    return m2.group(1) if m2 else s


def _calendar_date_zh(date_iso: str) -> str | None:
    try:
        y, m, d = to_graphql_date(date_iso).split("-")
        return f"{int(y)}年{int(m)}月{int(d)}日"
    except Exception:
        return None


def _distance_text(distance: Any) -> str | None:
    s = _txt(distance)
    if not s:
        return None
    if s.isdigit():
        return f"{s}米"
    return s


def _track_name(race: Mapping[str, Any]) -> str | None:
    course = race.get("raceCourse") or {}
    display = _txt(course.get("displayCode"))
    if display:
        return display
    return _txt(course.get("description_ch")) or _txt(course.get("description_en"))


def _course_area(race: Mapping[str, Any]) -> str | None:
    track = race.get("raceTrack") or {}
    return _txt(track.get("description_ch")) or _txt(track.get("description_en"))


def race_info_from_graphql(
    meeting: Mapping[str, Any],
    race: Mapping[str, Any],
) -> dict[str, str | None]:
    """Map GraphQL race/meeting fields → racecard_race_info columns.

    prize_money / rating_range are intentionally absent (no GraphQL source).
    """
    date_iso = _txt(meeting.get("date"))
    venue = _txt(meeting.get("venueCode"))
    dow = _txt(meeting.get("dateOfWeek"))
    return {
        "event_name": _txt(race.get("raceName_ch")) or _txt(race.get("raceName_en")),
        "calendar_date": _calendar_date_zh(date_iso) if date_iso else None,
        "weekday": _WEEKDAY_ZH.get((dow or "").upper(), dow),
        "meeting_venue": _VENUE_DISPLAY.get(venue or "", venue),
        "post_time": _post_time_hhmm(race.get("postTime")),
        "race_course_area": _course_area(race),
        "track_name": _track_name(race),
        "distance": _distance_text(race.get("distance")),
        "race_class": _txt(race.get("raceClass_ch")) or _txt(race.get("raceClass_en")),
        # Explicitly not mapped from GraphQL:
        # "prize_money", "rating_range"
    }


def _is_standby_runner(runner: Mapping[str, Any]) -> bool:
    status = (_txt(runner.get("status")) or "").lower()
    if status == "standby":
        return True
    if _txt(runner.get("standbyNo")) and not _txt(runner.get("no")):
        return True
    return False


def starter_fields_from_graphql(runner: Mapping[str, Any]) -> dict[str, Any]:
    """Map GraphQL runner → racecard_starters display fields (+ horse_id, 後備)."""
    standby = _is_standby_runner(runner)
    horse_no = _txt(runner.get("no"))
    if standby and not horse_no:
        horse_no = _txt(runner.get("standbyNo"))
    horse = runner.get("horse") or {}
    jockey = runner.get("jockey") or {}
    trainer = runner.get("trainer") or {}
    return {
        "後備": "是" if standby else "否",
        "horse_id": _txt(horse.get("id")),
        "馬號": horse_no,
        "馬名": _txt(runner.get("name_ch")) or _txt(runner.get("name_en")),
        "檔位": _txt(runner.get("barrierDrawNumber")),
        "騎師": _txt(jockey.get("name_ch")) or _txt(jockey.get("name_en")),
        "練馬師": _txt(trainer.get("name_ch")) or _txt(trainer.get("name_en")),
        "負磅": _txt(runner.get("handicapWeight")),
        "評分": _txt(runner.get("currentRating")),
        "馬匹體重": _txt(runner.get("currentWeight")),
        "配備": _txt(runner.get("gearInfo")),
        "win_odds_raw": _txt(runner.get("winOdds")),
    }


def _finish_pos_text(final_position: Any) -> str | None:
    """Return finish position for race_records, or None if not a finished result."""
    if final_position is None or final_position is False:
        return None
    if isinstance(final_position, bool):
        return None
    if isinstance(final_position, (int, float)):
        n = int(final_position)
        if n <= 0:
            return None
        return f"{n:02d}"
    s = _txt(final_position)
    if s is None:
        return None
    if s.isdigit():
        n = int(s)
        if n <= 0:
            return None
        return f"{n:02d}"
    # Non-numeric official marks (e.g. WV) — keep as-is when present.
    return s


def _distance_digits(distance: Any) -> str:
    s = _txt(distance)
    if not s:
        return ""
    m = re.search(r"\d+", s)
    return m.group(0) if m else s


def _course_for_race_record(
    meeting: Mapping[str, Any], race: Mapping[str, Any]
) -> str:
    """Build course text aligned with horse-page history (venue+track+\"code\")."""
    venue_code = _txt(meeting.get("venueCode")) or ""
    venue_zh = _VENUE_DISPLAY.get(venue_code, venue_code)
    track = race.get("raceTrack") or {}
    track_zh = _txt(track.get("description_ch")) or _txt(track.get("description_en")) or ""
    course = race.get("raceCourse") or {}
    display = _txt(course.get("displayCode"))
    if display:
        return f'{venue_zh}{track_zh}"{display}"'
    desc = _txt(course.get("description_ch")) or _txt(course.get("description_en")) or ""
    return f"{venue_zh}{track_zh}{desc}".strip()


def horse_row_from_graphql_runner(
    runner: Mapping[str, Any],
) -> dict[str, str | None] | None:
    """Horse row for insert_or_fill from a finished runner (incl. WV).

    Uses runner ``name_ch`` / ``name_en`` and ``horse.code``; None if the
    runner would not produce a race_records row.
    """
    if _is_standby_runner(runner):
        return None
    if not _finish_pos_text(runner.get("finalPosition")):
        return None
    horse = runner.get("horse") or {}
    horse_id = _txt(horse.get("id"))
    if not horse_id:
        return None
    return {
        "horse_id": horse_id,
        "brand_no": _txt(horse.get("code")),
        "name_zh": _txt(runner.get("name_ch")),
        "name_en": _txt(runner.get("name_en")),
        "status": None,
    }


def race_record_from_graphql_runner(
    meeting: Mapping[str, Any],
    race: Mapping[str, Any],
    runner: Mapping[str, Any],
) -> RaceRecord | None:
    """Map one finished runner → RaceRecord; None if not a written result."""
    if _is_standby_runner(runner):
        return None
    finish_pos = _finish_pos_text(runner.get("finalPosition"))
    if not finish_pos:
        return None
    horse = runner.get("horse") or {}
    horse_id = _txt(horse.get("id"))
    if not horse_id:
        return None
    date_iso = _txt(meeting.get("date"))
    if not date_iso:
        return None
    race_no = race.get("no")
    if race_no is None:
        return None
    venue_code = _txt(meeting.get("venueCode")) or ""
    if not is_local_venue_code(venue_code):
        return None
    venue_zh = _VENUE_DISPLAY.get(venue_code.upper(), "")
    jockey = runner.get("jockey") or {}
    trainer = runner.get("trainer") or {}
    return RaceRecord(
        horse_id=horse_id,
        race_date=to_race_records_date(date_iso),
        venue=venue_zh or venue_code,
        race_no=str(int(race_no)) if str(race_no).isdigit() else str(race_no),
        course=_course_for_race_record(meeting, race),
        distance=_distance_digits(race.get("distance")),
        going=_txt(race.get("go_ch")) or _txt(race.get("go_en")) or "",
        class_band=_txt(race.get("raceClass_ch")) or _txt(race.get("raceClass_en")) or "",
        draw=_txt(runner.get("barrierDrawNumber")) or "",
        rating=_txt(runner.get("currentRating")) or _txt(runner.get("internationalRating")) or "",
        handicap=_txt(runner.get("handicapWeight")) or "",
        jockey=_txt(jockey.get("name_ch")) or _txt(jockey.get("name_en")) or "",
        trainer=_txt(trainer.get("name_ch")) or _txt(trainer.get("name_en")) or "",
        finish_pos=finish_pos,
        running_pos="",
        win_odds=_txt(runner.get("winOdds")) or "",
        time="",
        prize="",
    )


def race_records_from_meeting(meeting: Mapping[str, Any]) -> list[RaceRecord]:
    """Extract finished runners from one raceMeetings meeting payload."""
    out: list[RaceRecord] = []
    seen: set[tuple[str, ...]] = set()
    for race in meeting.get("races") or []:
        for runner in race.get("runners") or []:
            rec = race_record_from_graphql_runner(meeting, race, runner)
            if rec is None:
                continue
            key = (
                rec.horse_id,
                rec.race_date,
                rec.venue,
                rec.race_no,
                rec.course,
                rec.distance,
                rec.going,
                rec.class_band,
                rec.draw,
                rec.rating,
                rec.handicap,
                rec.jockey,
                rec.trainer,
                rec.finish_pos,
                rec.running_pos,
                rec.win_odds,
                rec.time,
                rec.prize,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
    return out


def horse_rows_from_meeting(
    meeting: Mapping[str, Any],
) -> list[dict[str, str | None]]:
    """Horse rows for finished runners (incl. WV), deduped by horse_id."""
    out: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for race in meeting.get("races") or []:
        for runner in race.get("runners") or []:
            row = horse_row_from_graphql_runner(runner)
            if row is None:
                continue
            hid = row.get("horse_id") or ""
            if not hid or hid in seen:
                continue
            seen.add(hid)
            out.append(row)
    return out


def extract_win_odds_from_runners(
    meeting: Mapping[str, Any],
) -> list[tuple[int, str, Any]]:
    """Return (race_no, horse_no, win_odds) for non-standby runners with a value."""
    out: list[tuple[int, str, Any]] = []
    for race in meeting.get("races") or []:
        race_no = race.get("no")
        if race_no is None:
            continue
        for runner in race.get("runners") or []:
            if _is_standby_runner(runner):
                continue
            horse_no = _txt(runner.get("no"))
            win_odds = _txt(runner.get("winOdds"))
            if not horse_no or win_odds is None:
                continue
            out.append((int(race_no), horse_no, win_odds))
    return out


def extract_win_odds_from_sp4e_pools(
    data: Mapping[str, Any],
) -> dict[tuple[int, str], str]:
    """Map (race_no, horse_no) → oddsValue from WIN pmPools.oddsNodes."""
    out: dict[tuple[int, str], str] = {}
    for meeting in data.get("raceMeetings") or []:
        for pool in meeting.get("pmPools") or []:
            if (_txt(pool.get("oddsType")) or "").upper() != "WIN":
                continue
            leg = pool.get("leg") or {}
            races = leg.get("races") or []
            race_no = int(races[0]) if races else None
            if race_no is None:
                continue
            for node in pool.get("oddsNodes") or []:
                comb = _txt(node.get("combString"))
                odds = _txt(node.get("oddsValue"))
                if not comb or odds is None:
                    continue
                # combString is typically zero-padded horse no, e.g. "01"
                horse_no = str(int(comb)) if comb.isdigit() else comb.lstrip("0") or comb
                out[(race_no, horse_no)] = odds
    return out


def _coalesce_update_sql(columns: Iterable[str]) -> str:
    parts = [f"{c}=COALESCE(excluded.{c}, {c})" for c in columns]
    return ",\n            ".join(parts)


def upsert_race_info_from_graphql(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    race_no: int,
    info: Mapping[str, str | None],
    race_info_table: str = "racecard_race_info",
) -> None:
    """Insert/update only GraphQL-mapped race_info columns; never touch prize/rating."""
    mapped_cols = (
        "event_name",
        "calendar_date",
        "weekday",
        "meeting_venue",
        "post_time",
        "race_course_area",
        "track_name",
        "distance",
        "race_class",
    )
    if not any(info.get(c) for c in mapped_cols):
        return
    conn.execute(
        f"""
        INSERT INTO {race_info_table} (
            race_date, racecourse, race_no,
            event_name, calendar_date, weekday, meeting_venue, post_time,
            race_course_area, track_name, distance, race_class
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(race_date, racecourse, race_no) DO UPDATE SET
            {_coalesce_update_sql(mapped_cols)};
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
            info.get("race_class"),
        ),
    )


def _find_starter_row_id(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    race_no: int,
    reserve_flag: str,
    horse_no: str | None,
    horse_id: str | None,
) -> int | None:
    if horse_id:
        row = conn.execute(
            """
            SELECT id FROM racecard_starters
             WHERE race_date = ? AND racecourse = ? AND race_no = ?
               AND 後備 = ? AND horse_id = ?
             LIMIT 1;
            """,
            (race_date, racecourse, race_no, reserve_flag, horse_id),
        ).fetchone()
        if row:
            return int(row[0] if not hasattr(row, "keys") else row["id"])
    if horse_no:
        row = conn.execute(
            """
            SELECT id FROM racecard_starters
             WHERE race_date = ? AND racecourse = ? AND race_no = ?
               AND 後備 = ? AND "馬號" = ?
             LIMIT 1;
            """,
            (race_date, racecourse, race_no, reserve_flag, horse_no),
        ).fetchone()
        if row:
            return int(row[0] if not hasattr(row, "keys") else row["id"])
    return None


def upsert_starter_from_graphql(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    race_no: int,
    fields: Mapping[str, Any],
) -> None:
    """Insert or update starter GraphQL fields; never clear 預測賠率 / missing cols."""
    reserve_flag = fields.get("後備") or "否"
    horse_no = fields.get("馬號")
    horse_id = fields.get("horse_id")
    display_cols = ("馬號", "馬名", "檔位", "騎師", "練馬師", "負磅", "評分", "馬匹體重", "配備")

    row_id = _find_starter_row_id(
        conn,
        race_date=race_date,
        racecourse=racecourse,
        race_no=race_no,
        reserve_flag=str(reserve_flag),
        horse_no=_txt(horse_no),
        horse_id=_txt(horse_id),
    )

    if row_id is None:
        conn.execute(
            """
            INSERT INTO racecard_starters (
                race_date, racecourse, race_no, 後備, horse_id,
                "馬號", "馬名", "檔位", "騎師", "練馬師",
                "負磅", "評分", "馬匹體重", "配備",
                "預測賠率", "即時賠率"
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL);
            """,
            (
                race_date,
                racecourse,
                race_no,
                reserve_flag,
                horse_id,
                fields.get("馬號"),
                fields.get("馬名"),
                fields.get("檔位"),
                fields.get("騎師"),
                fields.get("練馬師"),
                fields.get("負磅"),
                fields.get("評分"),
                fields.get("馬匹體重"),
                fields.get("配備"),
            ),
        )
        return

    # Only overwrite when GraphQL provided a non-empty value.
    sets: list[str] = []
    params: list[Any] = []
    if _txt(horse_id):
        sets.append("horse_id = ?")
        params.append(horse_id)
    for col in display_cols:
        val = _txt(fields.get(col))
        if val is None:
            continue
        sets.append(f'"{col}" = ?')
        params.append(val)
    if not sets:
        return
    params.append(row_id)
    conn.execute(
        f"UPDATE racecard_starters SET {', '.join(sets)} WHERE id = ?;",
        params,
    )


def parse_odds_number(raw: Any) -> float | None:
    """Parse stored/GraphQL odds text to float. Missing/invalid → None (no invent)."""
    text = _txt(raw)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def format_odds_number(odds_decimal: float) -> str:
    """Same display convention as predicted odds: up to 2 decimals, trim zeros."""
    v = round(float(odds_decimal), 2)
    s = f"{v:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def compute_odds_diff(predicted_odds: Any, immediate_odds: Any) -> str | None:
    """預測賠率 − 即時賠率. Either side missing/invalid → None (store NULL)."""
    predicted = parse_odds_number(predicted_odds)
    immediate = parse_odds_number(immediate_odds)
    if predicted is None or immediate is None:
        return None
    return format_odds_number(predicted - immediate)


def update_immediate_win_odds(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    race_no: int,
    horse_no: str,
    win_odds: Any,
) -> int:
    """Update 即時賠率 and 賠率差異 for a non-standby starter. Returns affected rowcount."""
    immediate = _txt(win_odds)
    if immediate is None:
        return 0
    predicted_col = qident(RACECARD_PREDICTED_ODDS_COLUMN)
    immediate_col = qident(RACECARD_IMMEDIATE_ODDS_COLUMN)
    diff_col = qident(RACECARD_ODDS_DIFF_COLUMN)
    rows = conn.execute(
        f"""
        SELECT id, {predicted_col} AS predicted_odds
          FROM racecard_starters
         WHERE race_date = ?
           AND racecourse = ?
           AND race_no = ?
           AND "馬號" = ?
           AND 後備 = '否';
        """,
        (race_date, racecourse, race_no, str(horse_no)),
    ).fetchall()
    if not rows:
        return 0
    updated = 0
    for row in rows:
        row_id = int(row[0])
        predicted_odds = row[1]
        diff = compute_odds_diff(predicted_odds, immediate)
        cur = conn.execute(
            f"""
            UPDATE racecard_starters
               SET {immediate_col} = ?,
                   {diff_col} = ?
             WHERE id = ?;
            """,
            (immediate, diff, row_id),
        )
        updated += int(cur.rowcount or 0)
    return updated


def apply_meeting_racecard_to_db(
    conn: sqlite3.Connection,
    data: Mapping[str, Any],
    *,
    race_date_db: str | None = None,
    racecourse: str | None = None,
) -> tuple[str, str, list[int]]:
    """Upsert race_info + starters from raceMeetings payload.

    Returns (race_date_db, venue, race_nos). Does not delete existing rows.
    Does not write prize_money, rating_range, or 預測賠率.
    """
    meetings = data.get("raceMeetings") or []
    if not meetings:
        raise RacingGraphQLError("GraphQL：找不到 raceMeetings")
    meeting = meetings[0]
    date_iso = _txt(meeting.get("date")) or ""
    venue_raw = _txt(meeting.get("venueCode")) or (racecourse or "")
    if not date_iso or not venue_raw:
        raise RacingGraphQLError("GraphQL meeting missing date/venueCode")
    venue = require_local_venue_code(venue_raw)
    db_date = race_date_db or to_db_date(date_iso)
    race_nos: list[int] = []

    for race in meeting.get("races") or []:
        race_no = race.get("no")
        if race_no is None:
            continue
        rn = int(race_no)
        race_nos.append(rn)
        info = race_info_from_graphql(meeting, race)
        upsert_race_info_from_graphql(
            conn,
            race_date=db_date,
            racecourse=venue,
            race_no=rn,
            info=info,
        )
        for runner in race.get("runners") or []:
            fields = starter_fields_from_graphql(runner)
            if not fields.get("馬號") and not fields.get("horse_id"):
                continue
            upsert_starter_from_graphql(
                conn,
                race_date=db_date,
                racecourse=venue,
                race_no=rn,
                fields=fields,
            )
    from hppv.racing_download import (
        update_finish_pos_for_meeting,
        update_immediate_odds_from_race_records_for_meeting,
    )
    from hppv.racing_draw_statistics import update_draw_rates_for_meeting
    from hppv.racing_horse_form import (
        update_hv_attainment_rate_for_meeting,
        update_hv_same_distance_win_rates_for_meeting,
        update_hv_smoothed_weighted_rate_for_meeting,
        update_hv_weighted_win_rate_for_meeting,
    )

    update_hv_same_distance_win_rates_for_meeting(
        conn, race_date=db_date, racecourse=venue
    )
    update_draw_rates_for_meeting(conn, race_date=db_date, racecourse=venue)
    update_hv_attainment_rate_for_meeting(
        conn, race_date=db_date, racecourse=venue
    )
    update_hv_weighted_win_rate_for_meeting(
        conn, race_date=db_date, racecourse=venue
    )
    update_hv_smoothed_weighted_rate_for_meeting(
        conn, race_date=db_date, racecourse=venue
    )
    update_finish_pos_for_meeting(conn, race_date=db_date, racecourse=venue)
    update_immediate_odds_from_race_records_for_meeting(
        conn, race_date=db_date, racecourse=venue
    )
    return db_date, venue, sorted(set(race_nos))


def apply_win_odds_to_db(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    sp4e_odds: Mapping[tuple[int, str], str] | None = None,
) -> int:
    """Write 即時賠率 from sp4e WIN oddsNodes only (single odds path).

    Missing values are skipped (existing DB value kept). PLA/QIN/QPL unused.
    """
    updated = 0
    for (race_no, horse_no), odds in (sp4e_odds or {}).items():
        key_horse = str(int(horse_no)) if str(horse_no).isdigit() else str(horse_no)
        n = update_immediate_win_odds(
            conn,
            race_date=race_date,
            racecourse=racecourse,
            race_no=int(race_no),
            horse_no=key_horse,
            win_odds=odds,
        )
        updated += n
    return updated


def update_meeting_immediate_odds_from_graphql(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    post_func: PostFunc | None = None,
) -> int:
    """Update 即時賠率 via sp4e_racing_pmPool WIN only (no winOdds fallback)."""
    racecourse = require_local_venue_code(racecourse)
    date_iso = to_graphql_date(race_date)
    # Race list comes from raceMeetings; odds values only from sp4e WIN.
    data = fetch_race_meetings(date_iso, racecourse, post_func=post_func)
    meetings = data.get("raceMeetings") or []
    if not meetings:
        return 0
    meeting = meetings[0]
    m_date = _txt(meeting.get("date"))
    m_venue = require_local_venue_code(
        _txt(meeting.get("venueCode")) or racecourse
    )
    db_date = to_db_date(m_date or date_iso)
    race_nos = sorted(
        {
            int(r.get("no"))
            for r in (meeting.get("races") or [])
            if r.get("no") is not None
        }
    )
    sp4e_map: dict[tuple[int, str], str] = {}
    for rn in race_nos:
        pool_data = fetch_sp4e_pm_pool(
            date_iso,
            m_venue,
            odds_types=("WIN",),
            race_no=rn,
            post_func=post_func,
        )
        sp4e_map.update(extract_win_odds_from_sp4e_pools(pool_data))
    return apply_win_odds_to_db(
        conn,
        race_date=db_date,
        racecourse=m_venue,
        sp4e_odds=sp4e_map,
    )


def download_meeting_racecard_from_graphql(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    post_func: PostFunc | None = None,
) -> tuple[str, str, list[int]]:
    """Fetch whitelist raceMeetings and upsert racecard tables for one meeting."""
    racecourse = require_local_venue_code(racecourse)
    date_iso = to_graphql_date(race_date)
    data = fetch_race_meetings(date_iso, racecourse, post_func=post_func)
    return apply_meeting_racecard_to_db(
        conn,
        data,
        race_date_db=to_db_date(date_iso),
        racecourse=racecourse,
    )


def resolve_latest_meeting_from_graphql(
    *,
    hint_date: str | None = None,
    hint_venue: str | None = None,
    post_func: PostFunc | None = None,
) -> ActiveMeeting | None:
    """Resolve active/latest meeting via whitelist raceMeetings query.

    Hint venue must be local (ST/HV/FC); foreign hints fall back to ST for the
    GraphQL request, then ``pick_active_meeting`` skips any foreign meetings.
    """
    from datetime import date as date_cls

    date_iso = to_graphql_date(hint_date) if hint_date else date_cls.today().isoformat()
    if hint_venue and is_local_venue_code(hint_venue):
        venue = venue_code_name(hint_venue).upper()
    else:
        venue = "ST"
    data = fetch_race_meetings(date_iso, venue, post_func=post_func)
    return pick_active_meeting(data)

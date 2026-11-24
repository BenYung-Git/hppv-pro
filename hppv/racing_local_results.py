"""HKJC localresults HTML → RaceRecord (date-range / meeting results).

Verified source page:
  https://racing.hkjc.com/zh-hk/local/information/localresults
  params: RaceDate=DD/MM/YYYY, Racecourse=ST|HV, RaceNo=N

Not GraphQL — raceMeetings does not return historical meetings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlparse

import requests

from hppv.models import RaceRecord

logger = logging.getLogger(__name__)

LOCAL_RESULTS_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/localresults"
)

# Fixed whitelist body for the download Request box (not sent to GraphQL).
RESULTS_PERIOD_QUERY = "localResultsPeriod"
RESULTS_PERIOD_OPERATION = "localResultsPeriod"

DEFAULT_VENUE_CODES: tuple[str, ...] = ("ST", "HV")

_VENUE_ZH: dict[str, str] = {
    "ST": "沙田",
    "HV": "跑馬地",
    "FC": "從化",
}

_RE_HORSE_ID = re.compile(r"horseid=([A-Za-z0-9_]+)", re.I)
_RE_SEASON_RACE_NO = re.compile(r"第\s*(\d+)\s*場\s*\((\d+)\)")
_RE_CLASS_DIST = re.compile(
    r"(?P<cls>[^<\n\-]+?)\s*-\s*(?P<dist>\d+)\s*米",
)
_RE_GOING = re.compile(r"場地狀況\s*[:：]\s*([^<\n]+)")
_RE_TRACK = re.compile(r"賽道\s*[:：]\s*([^<\n]+)")
_RE_PRIZE = re.compile(r"HK\$\s*([\d,]+)")
_RE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_YMD = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_RE_DMY4 = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class RacingLocalResultsError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


GetFunc = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class LocalResultsRaceMeta:
    race_date_dmy: str  # DD/MM/YYYY as requested
    venue_code: str
    race_card_no: int
    season_race_no: str
    venue_zh: str
    course: str
    distance: str
    going: str
    class_band: str
    prize: str


def parse_iso_date(raw: str) -> date:
    s = (raw or "").strip()
    if _RE_ISO.fullmatch(s):
        return date.fromisoformat(s)
    m = _RE_YMD.fullmatch(s)
    if m:
        y, mo, d = m.groups()
        return date(int(y), int(mo), int(d))
    m = _RE_DMY4.fullmatch(s)
    if m:
        d, mo, y = m.groups()
        return date(int(y), int(mo), int(d))
    raise RacingLocalResultsError("invalid_date", f"invalid date: {raw!r}")


def to_localresults_date(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}/{d.year:04d}"


def to_race_record_date(d: date) -> str:
    """Align with horse-page history storage (DD/MM/YY)."""
    return f"{d.day:02d}/{d.month:02d}/{str(d.year)[2:]}"


def iter_dates_inclusive(start: date, end: date) -> list[date]:
    if end < start:
        raise RacingLocalResultsError(
            "invalid_date_window", "endDate must be on or after startDate"
        )
    days = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(days)]


def normalize_venue_codes(raw: Any) -> list[str]:
    if raw is None or raw == "" or raw == []:
        return list(DEFAULT_VENUE_CODES)
    if isinstance(raw, str):
        parts = [p.strip().upper() for p in re.split(r"[,|;/\s]+", raw) if p.strip()]
        return parts or list(DEFAULT_VENUE_CODES)
    if isinstance(raw, (list, tuple)):
        out = [str(v).strip().upper() for v in raw if str(v).strip()]
        return out or list(DEFAULT_VENUE_CODES)
    raise RacingLocalResultsError("invalid_fields", "venueCodes must be list or string")


def parse_results_period_variables(
    variables: dict[str, Any] | None,
) -> tuple[date, date, list[str]]:
    vars_obj = dict(variables or {})
    start_raw = vars_obj.get("startDate")
    end_raw = vars_obj.get("endDate")
    if not isinstance(start_raw, str) or not start_raw.strip():
        raise RacingLocalResultsError(
            "invalid_date_window", "variables.startDate is required"
        )
    if not isinstance(end_raw, str) or not end_raw.strip():
        raise RacingLocalResultsError(
            "invalid_date_window", "variables.endDate is required"
        )
    start = parse_iso_date(start_raw)
    end = parse_iso_date(end_raw)
    venues = normalize_venue_codes(vars_obj.get("venueCodes"))
    return start, end, venues


def _normalize_going(raw: str) -> str:
    s = re.sub(r"\s+", "", (raw or "").strip())
    if s.endswith("地") and len(s) > 1:
        s = s[:-1]
    return s


def _course_from_track(venue_zh: str, track_raw: str) -> str:
    """Build course text like 沙田草地\"B\" from 『草地 - \"B\" 賽道』."""
    t = (track_raw or "").strip()
    surface = ""
    if "全天候" in t:
        surface = "全天候"
    elif "草地" in t:
        surface = "草地"
    m = re.search(r"[\"“”']([A-Za-z0-9+]+)[\"“”']", t)
    if m:
        return f'{venue_zh}{surface}"{m.group(1)}"'
    return f"{venue_zh}{surface}{t}".strip()


def discover_race_nos(html: str) -> list[int]:
    nos = sorted({int(x) for x in re.findall(r"RaceNo=(\d+)", html, flags=re.I)})
    if nos:
        return nos
    # Fallback: single race header on page.
    m = re.search(r"第\s*(\d+)\s*場", html)
    return [int(m.group(1))] if m else []


def _plain_text(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def _parse_race_meta(html: str, *, race_date_dmy: str, venue_code: str) -> LocalResultsRaceMeta | None:
    text = _plain_text(html)
    m = _RE_SEASON_RACE_NO.search(text) or _RE_SEASON_RACE_NO.search(html)
    if not m:
        # No result header → empty / wrong day.
        if "名次" not in text and "名次" not in html:
            return None
        m2 = re.search(r"第\s*(\d+)\s*場", text) or re.search(r"第\s*(\d+)\s*場", html)
        if not m2:
            return None
        card_no = int(m2.group(1))
        season_no = str(card_no)
    else:
        card_no = int(m.group(1))
        season_no = m.group(2)

    venue_zh = _VENUE_ZH.get(venue_code.upper(), venue_code.upper())
    cls = ""
    dist = ""
    cm = _RE_CLASS_DIST.search(text)
    if cm:
        cls = cm.group("cls").strip()
        dist = cm.group("dist").strip()
    gm = _RE_GOING.search(text)
    going = _normalize_going(gm.group(1)) if gm else ""
    tm = _RE_TRACK.search(text)
    course = _course_from_track(venue_zh, tm.group(1)) if tm else venue_zh
    pm = _RE_PRIZE.search(text) or _RE_PRIZE.search(html)
    prize = pm.group(1).replace(",", "") if pm else ""
    return LocalResultsRaceMeta(
        race_date_dmy=race_date_dmy,
        venue_code=venue_code.upper(),
        race_card_no=card_no,
        season_race_no=season_no,
        venue_zh=venue_zh,
        course=course,
        distance=dist,
        going=going,
        class_band=cls,
        prize=prize,
    )


def _horse_id_from_cell(cell: Any) -> str | None:
    a = cell.find("a") if hasattr(cell, "find") else None
    href = ""
    if a is not None and a.has_attr("href"):
        href = str(a["href"])
    if href:
        m = _RE_HORSE_ID.search(href)
        if m:
            return m.group(1)
        # Query-style path without regex hit
        try:
            qs = parse_qs(urlparse(href).query)
            for key in ("horseid", "HorseId", "horseId"):
                if key in qs and qs[key]:
                    return str(qs[key][0]).strip()
        except Exception:
            pass
    text = cell.get_text(" ", strip=True) if hasattr(cell, "get_text") else str(cell)
    bm = re.search(r"\(([A-Z]\d{3})\)", text or "")
    if bm:
        return bm.group(1)
    return None


_RE_NAME_BRAND = re.compile(
    r"^(?P<name>.*?)\s*\((?P<brand>[A-Z]\d{3})\)\s*$",
    re.DOTALL,
)


def _horse_row_from_cell(cell: Any) -> dict[str, str | None] | None:
    """Build horses insert/fill row from localresults 馬名 cell (keeps WV names)."""
    horse_id = _horse_id_from_cell(cell)
    if not horse_id:
        return None
    text = cell.get_text(" ", strip=True) if hasattr(cell, "get_text") else str(cell)
    text = re.sub(r"\s+", " ", (text or "").strip())
    name_zh: str | None = None
    brand_no: str | None = None
    m = _RE_NAME_BRAND.match(text)
    if m:
        name_zh = (m.group("name") or "").strip() or None
        brand_no = m.group("brand")
    elif re.fullmatch(r"[A-Z]\d{3}", horse_id):
        brand_no = horse_id
    return {
        "horse_id": horse_id,
        "brand_no": brand_no,
        "name_zh": name_zh,
        "name_en": None,
        "status": None,
    }


def _finish_pos_text(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        if n <= 0:
            return None
        return f"{n:02d}"
    return s


def parse_localresults_race_html(
    html: str,
    *,
    race_date: date,
    venue_code: str,
) -> tuple[list[RaceRecord], list[dict[str, str | None]]]:
    """Parse one localresults race page into RaceRecord + horse rows."""
    from bs4 import BeautifulSoup

    meta = _parse_race_meta(
        html,
        race_date_dmy=to_localresults_date(race_date),
        venue_code=venue_code,
    )
    if meta is None:
        return [], []

    soup = BeautifulSoup(html, "html.parser")
    records: list[RaceRecord] = []
    horses: list[dict[str, str | None]] = []
    seen: set[tuple[str, ...]] = set()
    seen_horses: set[str] = set()
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        head_cells = rows[0].find_all(["th", "td"])
        head = [c.get_text(" ", strip=True).replace("\xa0", " ") for c in head_cells]
        head_norm = [re.sub(r"\s+", "", h) for h in head]
        if not head_norm or head_norm[0] != "名次":
            continue
        if "馬名" not in head_norm and "馬名" not in "".join(head_norm):
            continue
        # Prefer the performance table (has 獨贏賠率); skip incident table.
        joined = "".join(head_norm)
        if "獨贏" not in joined and "賠率" not in joined:
            continue

        def col(*names: str) -> int | None:
            for i, h in enumerate(head_norm):
                for n in names:
                    if n in h:
                        return i
            return None

        i_pos = col("名次")
        i_horse = col("馬名")
        i_jockey = col("騎師")
        i_trainer = col("練馬師")
        i_draw = col("檔位")
        i_handicap = col("實際負磅", "負磅")
        i_running = col("沿途")
        i_time = col("完成時間", "時間")
        i_odds = col("獨贏")
        if i_pos is None or i_horse is None:
            continue

        for row in rows[1:]:
            tds = row.find_all("td")
            if len(tds) < 3:
                continue
            texts = [c.get_text(" ", strip=True) for c in tds]

            def get(idx: int | None) -> str:
                if idx is None or idx < 0 or idx >= len(texts):
                    return ""
                return texts[idx]

            finish = _finish_pos_text(get(i_pos))
            if not finish:
                continue
            horse_id = _horse_id_from_cell(tds[i_horse])
            if not horse_id:
                continue
            horse_row = _horse_row_from_cell(tds[i_horse])
            if horse_row is not None:
                hid = horse_row.get("horse_id") or ""
                if hid and hid not in seen_horses:
                    seen_horses.add(hid)
                    horses.append(horse_row)
            rec = RaceRecord(
                horse_id=horse_id,
                race_date=to_race_record_date(race_date),
                venue=meta.venue_zh,
                race_no=meta.season_race_no,
                course=meta.course,
                distance=meta.distance,
                going=meta.going,
                class_band=meta.class_band,
                draw=get(i_draw),
                rating="",
                handicap=get(i_handicap),
                jockey=get(i_jockey),
                trainer=get(i_trainer),
                finish_pos=finish,
                running_pos=get(i_running),
                win_odds=get(i_odds),
                time=get(i_time),
                prize=meta.prize,
            )
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
            records.append(rec)
        if records:
            break
    return records, horses


def fetch_localresults_html(
    *,
    race_date: date,
    venue_code: str,
    race_no: int | None = None,
    timeout: float = 30,
    get_func: GetFunc | None = None,
) -> str:
    params: dict[str, str] = {
        "RaceDate": to_localresults_date(race_date),
        "Racecourse": venue_code.upper(),
    }
    if race_no is not None:
        params["RaceNo"] = str(int(race_no))
    getter = get_func or requests.get
    resp = getter(
        LOCAL_RESULTS_URL,
        params=params,
        headers={
            "User-Agent": "HPPV/0.1 (+requests; localresults period download)",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def fetch_meeting_race_records(
    *,
    race_date: date,
    venue_code: str,
    timeout: float = 30,
    get_func: GetFunc | None = None,
) -> tuple[list[RaceRecord], list[dict[str, str | None]], dict[str, Any]]:
    """Fetch all races for one meeting day+venue; return records + horses + meta."""
    meeting_html = fetch_localresults_html(
        race_date=race_date,
        venue_code=venue_code,
        race_no=None,
        timeout=timeout,
        get_func=get_func,
    )
    nos = discover_race_nos(meeting_html)
    meta: dict[str, Any] = {
        "race_date": to_localresults_date(race_date),
        "venue_code": venue_code.upper(),
        "race_nos": nos,
        "races_fetched": 0,
        "races_with_results": 0,
    }
    if not nos:
        # Try race 1 directly — some pages omit RaceNo links when empty.
        nos = [1]
        meta["race_nos"] = nos
        meta["race_nos_fallback"] = True

    out: list[RaceRecord] = []
    horses: list[dict[str, str | None]] = []
    seen_horses: set[str] = set()
    for rn in nos:
        try:
            html = fetch_localresults_html(
                race_date=race_date,
                venue_code=venue_code,
                race_no=rn,
                timeout=timeout,
                get_func=get_func,
            )
        except Exception as e:
            logger.warning(
                "localresults fetch failed %s %s R%s: %s",
                race_date,
                venue_code,
                rn,
                e,
            )
            meta.setdefault("fetch_errors", []).append(
                {"race_no": rn, "error": str(e)}
            )
            continue
        meta["races_fetched"] += 1
        rows, horse_rows = parse_localresults_race_html(
            html, race_date=race_date, venue_code=venue_code
        )
        if rows:
            meta["races_with_results"] += 1
            out.extend(rows)
        for h in horse_rows:
            hid = h.get("horse_id") or ""
            if not hid or hid in seen_horses:
                continue
            seen_horses.add(hid)
            horses.append(h)
    return out, horses, meta


def collect_period_race_records(
    *,
    start: date,
    end: date,
    venue_codes: Sequence[str],
    timeout: float = 30,
    get_func: GetFunc | None = None,
    on_meeting: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[RaceRecord], list[dict[str, str | None]], list[dict[str, Any]]]:
    """Walk inclusive date×venue meetings; collect RaceRecords + horse rows."""
    dates = iter_dates_inclusive(start, end)
    all_rows: list[RaceRecord] = []
    all_horses: list[dict[str, str | None]] = []
    seen_horses: set[str] = set()
    meeting_metas: list[dict[str, Any]] = []
    for d in dates:
        for venue in venue_codes:
            rows, horses, meta = fetch_meeting_race_records(
                race_date=d,
                venue_code=venue,
                timeout=timeout,
                get_func=get_func,
            )
            meeting_metas.append(meta)
            if on_meeting is not None:
                try:
                    on_meeting({**meta, "record_count": len(rows)})
                except Exception:
                    logger.exception("on_meeting callback failed")
            all_rows.extend(rows)
            for h in horses:
                hid = h.get("horse_id") or ""
                if not hid or hid in seen_horses:
                    continue
                seen_horses.add(hid)
                all_horses.append(h)
    return all_rows, all_horses, meeting_metas

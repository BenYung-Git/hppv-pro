"""HKJC course standard times + sectional times (yearly PDF → racing_standard_times)."""

from __future__ import annotations

import io
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from hppv.db import get_connection, init_db
from hppv.display_format import EMPTY_DISPLAY, format_field_display

logger = logging.getLogger(__name__)

STANDARD_TIMES_QUERY = "standardTimesPdf"
STANDARD_TIMES_OPERATION = "standardTimesPdf"

# Verified HKJC racing-news PDF URLs only (no invented path templates).
STANDARD_TIMES_PDF_URLS: dict[int, str] = {
    2022: (
        "https://res.hkjc.com/racingnews/wp-content/uploads/sites/2/2022/08/"
        "2022-StandardTimesSectionalTimes.pdf"
    ),
    2023: (
        "https://res.hkjc.com/racingnews/wp-content/uploads/sites/3/2023/08/"
        "2023-StandardTimesSectionalTimes.pdf"
    ),
    2024: (
        "https://res.hkjc.com/racingnews/wp-content/uploads/sites/3/2024/08/"
        "2024-StandardTimesSectionalTimes.pdf"
    ),
    2025: (
        "https://res.hkjc.com/racingnews/wp-content/uploads/sites/2/2025/08/"
        "2025-StandardTimes-SectionalTimes.pdf"
    ),
    2026: (
        "https://res.hkjc.com/racingnews/wp-content/uploads/sites/2/2026/08/"
        "2026-StandardTimes-SectionalTimes.pdf"
    ),
}

REGISTERED_STANDARD_TIMES_YEARS: tuple[int, ...] = tuple(
    sorted(STANDARD_TIMES_PDF_URLS.keys())
)

_HTTP_HEADERS = {
    "User-Agent": "HPPV/0.1 (+requests; standard times PDF download)",
}

_CLASS_MAP = {
    "Group Race": "Group",
    "Class 1": "Class 1",
    "Class 2": "Class 2",
    "Class 3": "Class 3",
    "Class 4": "Class 4",
    "Class 5": "Class 5",
    "Griffin Race": "Griffin",
}

_MONTHS = {
    name: idx
    for idx, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}

_VENUE_PAT = re.compile(
    r"(Sha Tin Turf Track|Happy Valley Turf Track|Sha Tin All Weather Track)"
)
_DATE_PAT = re.compile(
    r"(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})"
)
_SEC_TOK = r"(?:-|(?<![.\d])\d{1,2}\.\d{2}(?!\.\d))"
_CLS_ZH = r"(?:分級賽|第一班|第二班|第三班|第四班|第五班|新馬賽)"
_CLS_EN = r"(?P<cls>Group Race|Class [1-5]|Griffin Race)"
# Format A (2022/2023/2025/2026): time before class label (often glued).
_ROW_A = re.compile(
    r"(?:(?P<dist>\d{3,4})[ \t]+)?"
    r"(?:(?P<time>\d+\.\d{2}\.\d{2})|-)"
    r"[ \t]*"
    + _CLS_ZH
    + r"[ \t]+"
    + _CLS_EN
    + r"[ \t]+"
    r"(?P<rest>(?:"
    + _SEC_TOK
    + r")(?:[ \t]+"
    + _SEC_TOK
    + r")*)"
)
# Format B (2024): class label then time then sectionals.
_ROW_B = re.compile(
    r"(?:(?P<dist>\d{3,4})[ \t]+)?"
    + _CLS_ZH
    + r"[ \t]+"
    + _CLS_EN
    + r"[ \t]+"
    r"(?:(?P<time>\d+\.\d{2}\.\d{2})|-)"
    r"(?:[ \t]+(?P<rest>(?:"
    + _SEC_TOK
    + r")(?:[ \t]+"
    + _SEC_TOK
    + r")*))?"
)

STANDARD_TIMES_PAGE_SIZE = 50
SEC_COLUMN_COUNT = 6

_COL_SQL: dict[str, str] = {
    "id": "st.id",
    "published_on": "st.published_on",
    "source_year": "st.source_year",
    "venue_code": "st.venue_code",
    "track_type": "st.track_type",
    "distance_m": "st.distance_m",
    "class_band": "st.class_band",
    "standard_time": "st.standard_time",
    "sec_1": "st.sec_1",
    "sec_2": "st.sec_2",
    "sec_3": "st.sec_3",
    "sec_4": "st.sec_4",
    "sec_5": "st.sec_5",
    "sec_6": "st.sec_6",
}

_SORT_SQL: dict[str, str] = {
    "id": "st.id",
    "published_on": "st.published_on",
    "source_year": "st.source_year",
    "venue_code": "st.venue_code",
    "track_type": "st.track_type",
    "distance_m": "st.distance_m",
    "class_band": "st.class_band",
    "standard_time": "st.standard_time",
    "sec_1": "st.sec_1",
    "sec_2": "st.sec_2",
    "sec_3": "st.sec_3",
    "sec_4": "st.sec_4",
    "sec_5": "st.sec_5",
    "sec_6": "st.sec_6",
}

HISTORY_SEARCH_OP_APPROX = "approx"
HISTORY_SEARCH_OP_EQ = "eq"
HISTORY_SEARCH_OP_NE = "ne"
HISTORY_SEARCH_OP_CONTAINS = "contains"
HISTORY_SEARCH_OP_NOT_CONTAINS = "not_contains"
HISTORY_SEARCH_OP_STARTS = "starts"
HISTORY_SEARCH_OP_ENDS = "ends"
DEFAULT_HISTORY_SEARCH_OP = HISTORY_SEARCH_OP_APPROX
HISTORY_SEARCH_OPS_ALL = frozenset(
    {
        HISTORY_SEARCH_OP_APPROX,
        HISTORY_SEARCH_OP_EQ,
        HISTORY_SEARCH_OP_NE,
        HISTORY_SEARCH_OP_CONTAINS,
        HISTORY_SEARCH_OP_NOT_CONTAINS,
        HISTORY_SEARCH_OP_STARTS,
        HISTORY_SEARCH_OP_ENDS,
    }
)


class StandardTimesError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class StandardTimesSearchError(StandardTimesError):
    pass


# Exact race_records.class_band → racing_standard_times.class_band.
# Unlisted labels (條件限制 / 4YO / …) do not map; those starts are excluded.
_CLASS_BAND_TO_STANDARD: dict[str, str] = {
    "1": "Class 1",
    "2": "Class 2",
    "3": "Class 3",
    "4": "Class 4",
    "5": "Class 5",
    "Class 1": "Class 1",
    "Class 2": "Class 2",
    "Class 3": "Class 3",
    "Class 4": "Class 4",
    "Class 5": "Class 5",
    "第一班": "Class 1",
    "第二班": "Class 2",
    "第三班": "Class 3",
    "第四班": "Class 4",
    "第五班": "Class 5",
    "新馬賽": "Griffin",
    "GRIFFIN": "Griffin",
    "Griffin": "Griffin",
    "Griffin Race": "Griffin",
    "G1": "Group",
    "G2": "Group",
    "G3": "Group",
    "一級賽": "Group",
    "二級賽": "Group",
    "三級賽": "Group",
    "分級賽": "Group",
    "Group": "Group",
    "Group Race": "Group",
}

_VENUE_ZH_TO_CODE: dict[str, str] = {
    "跑馬地": "HV",
    "沙田": "ST",
}

# Finish / standard times: m.ss.xx or m:ss.xx (minutes may be 0).
_RE_RACE_TIME = re.compile(
    r"^(?P<min>\d+)(?:[.:])(?P<sec>\d{1,2})\.(?P<frac>\d{2})$"
)


def map_class_band_to_standard(class_band: Any) -> str | None:
    """Exact label map only; unknown / blank → None (no substitute)."""
    text = str(class_band or "").strip()
    if not text:
        return None
    return _CLASS_BAND_TO_STANDARD.get(text)


def venue_code_from_race_records_venue(venue: Any) -> str | None:
    text = str(venue or "").strip()
    if not text:
        return None
    return _VENUE_ZH_TO_CODE.get(text)


def track_type_from_race_records_course(course: Any) -> str | None:
    """Derive TURF / AWT from race_records.course text; unknown → None."""
    text = str(course or "").strip()
    if not text:
        return None
    if "全天候" in text:
        return "AWT"
    if "草地" in text:
        return "TURF"
    return None


def parse_race_time_to_seconds(raw: Any) -> float | None:
    """Parse finish/standard time text to seconds. Invalid / empty → None."""
    text = str(raw or "").strip()
    if not text or text in {"--", "---", "-", EMPTY_DISPLAY}:
        return None
    m = _RE_RACE_TIME.fullmatch(text)
    if not m:
        return None
    minutes = int(m.group("min"))
    seconds = int(m.group("sec"))
    frac = int(m.group("frac"))
    if seconds >= 60:
        return None
    return float(minutes * 60 + seconds) + (frac / 100.0)


def lookup_standard_time_seconds(
    conn: Any,
    *,
    venue_code: str,
    track_type: str,
    distance_m: int,
    class_band: str,
    as_of_iso: str,
) -> float | None:
    """Latest published_on ≤ as_of for the exact query key; empty/unparsed → None."""
    row = conn.execute(
        """
        SELECT standard_time
          FROM racing_standard_times
         WHERE venue_code = ?
           AND track_type = ?
           AND distance_m = ?
           AND class_band = ?
           AND published_on <= ?
         ORDER BY published_on DESC
         LIMIT 1
        """,
        (
            str(venue_code).strip().upper(),
            str(track_type).strip().upper(),
            int(distance_m),
            str(class_band).strip(),
            str(as_of_iso).strip(),
        ),
    ).fetchone()
    if row is None:
        return None
    raw = row["standard_time"] if hasattr(row, "keys") else row[0]
    return parse_race_time_to_seconds(raw)


@dataclass(frozen=True, slots=True)
class StandardTimesRow:
    published_on: str
    source_year: int
    venue_code: str
    track_type: str
    distance_m: int
    class_band: str
    standard_time: str
    sectionals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StandardTimesListResult:
    rows: list[dict[str, str]]
    n: int
    total_count: int
    total_pages: int
    page_size: int
    q: str
    all_matches: bool
    sort: str
    q_op: str = DEFAULT_HISTORY_SEARCH_OP
    field_keys: tuple[str, ...] = ()
    search_keys: tuple[str, ...] = ()
    published_on: str | None = None


def standard_times_schema_sql() -> str:
    return """
        CREATE TABLE IF NOT EXISTS racing_standard_times (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          published_on TEXT NOT NULL,
          source_year INTEGER NOT NULL,
          venue_code TEXT NOT NULL,
          track_type TEXT NOT NULL,
          distance_m INTEGER NOT NULL,
          class_band TEXT NOT NULL,
          standard_time TEXT NOT NULL DEFAULT '',
          sec_1 TEXT NOT NULL DEFAULT '',
          sec_2 TEXT NOT NULL DEFAULT '',
          sec_3 TEXT NOT NULL DEFAULT '',
          sec_4 TEXT NOT NULL DEFAULT '',
          sec_5 TEXT NOT NULL DEFAULT '',
          sec_6 TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          UNIQUE(venue_code, track_type, distance_m, class_band, published_on)
        );
        CREATE INDEX IF NOT EXISTS idx_racing_std_times_published
          ON racing_standard_times(published_on);
        CREATE INDEX IF NOT EXISTS idx_racing_std_times_venue_dist
          ON racing_standard_times(venue_code, track_type, distance_m);
    """


def ensure_standard_times_table(db_path: Path) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.executescript(standard_times_schema_sql())
        conn.commit()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pdf_url_for_year(year: int) -> str:
    try:
        return STANDARD_TIMES_PDF_URLS[int(year)]
    except (KeyError, TypeError, ValueError) as e:
        raise StandardTimesError(
            "year_url_unknown",
            f"no registered standard-times PDF URL for year={year!r}",
        ) from e


def resolve_download_years(raw_years: Any) -> list[int]:
    """None/empty → all registered years; otherwise validate each against URL map."""
    if raw_years is None:
        return list(REGISTERED_STANDARD_TIMES_YEARS)
    if isinstance(raw_years, (list, tuple)):
        if not raw_years:
            return list(REGISTERED_STANDARD_TIMES_YEARS)
        out: list[int] = []
        seen: set[int] = set()
        for item in raw_years:
            try:
                year = int(item)
            except (TypeError, ValueError) as e:
                raise StandardTimesError(
                    "invalid_years", f"years entry not int: {item!r}"
                ) from e
            if year not in STANDARD_TIMES_PDF_URLS:
                raise StandardTimesError(
                    "year_url_unknown",
                    f"no registered standard-times PDF URL for year={year}",
                )
            if year not in seen:
                seen.add(year)
                out.append(year)
        return out
    raise StandardTimesError("invalid_years", f"years must be list or null: {raw_years!r}")


def _detect_row_format(text: str) -> str:
    if re.search(r"Group Race\s+\d+\.\d{2}\.\d{2}", text):
        return "B"
    return "A"


def _venue_track(header: str) -> tuple[str, str]:
    if "Happy Valley" in header:
        return "HV", "TURF"
    if "All Weather" in header:
        return "ST", "AWT"
    return "ST", "TURF"


def _parse_published_on(text: str) -> str:
    match = _DATE_PAT.search(text)
    if match is None:
        raise StandardTimesError("published_on_missing", "PDF has no English publish date")
    day = int(match.group(1))
    month = _MONTHS[match.group(2)]
    year = int(match.group(3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def _pad_sectionals(tokens: list[str]) -> tuple[str, ...]:
    vals = list(tokens[:SEC_COLUMN_COUNT])
    while len(vals) < SEC_COLUMN_COUNT:
        vals.append("")
    return tuple(vals)


def parse_standard_times_pdf_bytes(
    pdf_bytes: bytes, *, source_year: int
) -> tuple[str, list[StandardTimesRow]]:
    if not pdf_bytes.startswith(b"%PDF"):
        raise StandardTimesError("not_pdf", "response body is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        raise StandardTimesError("pdf_parse_failed", str(e)) from e
    if not text.strip():
        raise StandardTimesError("pdf_empty_text", "PDF text extraction empty")

    published_on = _parse_published_on(text)
    row_re = _ROW_B if _detect_row_format(text) == "B" else _ROW_A
    parts = list(_VENUE_PAT.finditer(text))
    if not parts:
        raise StandardTimesError("venue_header_missing", "no venue track headers in PDF")

    rows: list[StandardTimesRow] = []
    seen: set[tuple[str, str, int, str]] = set()
    for i, match in enumerate(parts):
        start = match.start()
        end = parts[i + 1].start() if i + 1 < len(parts) else len(text)
        chunk = text[start:end]
        venue_code, track_type = _venue_track(match.group(1))
        cur_dist: int | None = None
        for rm in row_re.finditer(chunk):
            if rm.group("dist"):
                cur_dist = int(rm.group("dist"))
            if cur_dist is None:
                continue
            class_band = _CLASS_MAP[rm.group("cls")]
            key = (venue_code, track_type, cur_dist, class_band)
            if key in seen:
                continue
            seen.add(key)
            rest = rm.group("rest") or ""
            secs = ["" if tok == "-" else tok for tok in rest.split()]
            rows.append(
                StandardTimesRow(
                    published_on=published_on,
                    source_year=int(source_year),
                    venue_code=venue_code,
                    track_type=track_type,
                    distance_m=cur_dist,
                    class_band=class_band,
                    standard_time=rm.group("time") or "",
                    sectionals=_pad_sectionals(secs),
                )
            )
    if not rows:
        raise StandardTimesError("pdf_no_rows", f"year={source_year} produced 0 rows")
    return published_on, rows


def fetch_standard_times_pdf(year: int, *, timeout: float = 60.0) -> bytes:
    url = pdf_url_for_year(year)
    try:
        resp = requests.get(url, timeout=timeout, headers=_HTTP_HEADERS)
    except requests.RequestException as e:
        raise StandardTimesError("pdf_fetch_failed", f"year={year} {e}") from e
    if resp.status_code != 200:
        raise StandardTimesError(
            "pdf_http_error",
            f"year={year} status={resp.status_code} url={url}",
        )
    body = resp.content or b""
    if not body.startswith(b"%PDF"):
        raise StandardTimesError(
            "not_pdf",
            f"year={year} content-type={resp.headers.get('content-type')!r}",
        )
    return body


def upsert_standard_times_rows(
    db_path: Path,
    rows: list[StandardTimesRow],
    *,
    overwrite: bool = False,
) -> dict[str, int]:
    ensure_standard_times_table(db_path)
    stamp = utc_now_iso()
    inserted = 0
    updated = 0
    skipped = 0
    sql_ignore = """
        INSERT OR IGNORE INTO racing_standard_times (
          published_on, source_year, venue_code, track_type, distance_m, class_band,
          standard_time, sec_1, sec_2, sec_3, sec_4, sec_5, sec_6, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    sql_insert = """
        INSERT INTO racing_standard_times (
          published_on, source_year, venue_code, track_type, distance_m, class_band,
          standard_time, sec_1, sec_2, sec_3, sec_4, sec_5, sec_6, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    sql_update = """
        UPDATE racing_standard_times
        SET source_year = ?,
            standard_time = ?,
            sec_1 = ?, sec_2 = ?, sec_3 = ?, sec_4 = ?, sec_5 = ?, sec_6 = ?,
            updated_at = ?
        WHERE venue_code = ? AND track_type = ? AND distance_m = ?
          AND class_band = ? AND published_on = ?
    """
    lookup_sql = """
        SELECT id FROM racing_standard_times
        WHERE venue_code = ? AND track_type = ? AND distance_m = ?
          AND class_band = ? AND published_on = ?
        LIMIT 1
    """
    with get_connection(db_path) as conn:
        for row in rows:
            secs = _pad_sectionals(list(row.sectionals))
            params = (
                row.published_on,
                row.source_year,
                row.venue_code,
                row.track_type,
                row.distance_m,
                row.class_band,
                row.standard_time,
                secs[0],
                secs[1],
                secs[2],
                secs[3],
                secs[4],
                secs[5],
                stamp,
            )
            if overwrite:
                exists = conn.execute(
                    lookup_sql,
                    (
                        row.venue_code,
                        row.track_type,
                        row.distance_m,
                        row.class_band,
                        row.published_on,
                    ),
                ).fetchone()
                if exists:
                    conn.execute(
                        sql_update,
                        (
                            row.source_year,
                            row.standard_time,
                            secs[0],
                            secs[1],
                            secs[2],
                            secs[3],
                            secs[4],
                            secs[5],
                            stamp,
                            row.venue_code,
                            row.track_type,
                            row.distance_m,
                            row.class_band,
                            row.published_on,
                        ),
                    )
                    updated += 1
                else:
                    conn.execute(sql_insert, params)
                    inserted += 1
            else:
                cur = conn.execute(sql_ignore, params)
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def latest_published_on_iso(db_path: Path) -> str | None:
    ensure_standard_times_table(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT published_on AS d
            FROM racing_standard_times
            WHERE TRIM(COALESCE(published_on, '')) != ''
            ORDER BY published_on DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    text = str(row["d"] or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None


def normalize_history_search_op(raw: str | None) -> str:
    op = str(raw or "").strip().lower()
    if op in HISTORY_SEARCH_OPS_ALL:
        return op
    return DEFAULT_HISTORY_SEARCH_OP


def parse_standard_times_sort_param(raw: str | None) -> list[tuple[str, str]] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    specs: list[tuple[str, str]] = []
    for part in text.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, direction = part.split(":", 1)
        key = key.strip()
        direction = direction.strip().lower()
        if key not in _SORT_SQL:
            continue
        if direction not in ("asc", "desc"):
            continue
        specs.append((key, direction))
    return specs or None


def standard_times_sort_param(specs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{d}" for k, d in specs)


def _normalize_page(n: int | None) -> int:
    try:
        page = int(n) if n is not None else 1
    except (TypeError, ValueError):
        page = 1
    return max(1, page)


def _build_q_clause(
    q: str,
    q_op: str,
    search_keys: list[str],
    *,
    whitelist: frozenset[str],
) -> tuple[str, list[Any]]:
    q = (q or "").strip()
    if not q:
        return "", []
    keys = [k for k in search_keys if k in whitelist and k in _COL_SQL]
    if not keys:
        raise StandardTimesSearchError("no_searchable_fields")
    exprs = [_COL_SQL[k] for k in keys]
    op = normalize_history_search_op(q_op)
    params: list[Any] = []

    def lower_expr(e: str) -> str:
        return f"lower(CAST({e} AS TEXT))"

    if op == HISTORY_SEARCH_OP_APPROX:
        parts = [f"instr({lower_expr(e)}, ?) > 0" for e in exprs]
        params.extend([q.lower()] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_EQ:
        parts = [f"CAST({e} AS TEXT) = ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NE:
        parts = [f"ifnull(CAST({e} AS TEXT), '') != ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_CONTAINS:
        parts = [f"instr(CAST({e} AS TEXT), ?) > 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NOT_CONTAINS:
        parts = [f"instr(ifnull(CAST({e} AS TEXT), ''), ?) = 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_STARTS:
        parts = [f"CAST({e} AS TEXT) LIKE ?" for e in exprs]
        params.extend([q + "%"] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_ENDS:
        parts = [f"CAST({e} AS TEXT) LIKE ?" for e in exprs]
        params.extend(["%" + q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    return "", []


def _row_to_view(row: Any) -> dict[str, str]:
    keys = list(row.keys()) if hasattr(row, "keys") else []
    out: dict[str, str] = {}
    for key in _COL_SQL:
        out[key] = format_field_display(key, row[key] if key in keys else None)
    return out


def fetch_standard_times_filtered(
    db_path: Path,
    *,
    all_matches: bool = True,
    published_on: str | None = None,
    n: int | None = 1,
    q: str = "",
    q_op: str = DEFAULT_HISTORY_SEARCH_OP,
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
    page_size: int = STANDARD_TIMES_PAGE_SIZE,
    search_whitelist: frozenset[str] | None = None,
) -> StandardTimesListResult:
    """Server-paged read of racing_standard_times. Read-only."""
    from hppv.racing_layout import (
        DEFAULT_STANDARD_TIMES_SEARCH_KEYS,
        RACING_STANDARD_TIMES_TEXT_SEARCH_WHITELIST,
    )

    ensure_standard_times_table(db_path)
    page = _normalize_page(n)
    size = max(1, int(page_size or STANDARD_TIMES_PAGE_SIZE))
    whitelist = search_whitelist or RACING_STANDARD_TIMES_TEXT_SEARCH_WHITELIST
    keys = (
        list(search_keys)
        if search_keys is not None
        else list(DEFAULT_STANDARD_TIMES_SEARCH_KEYS)
    )
    specs = sort_specs if sort_specs else [("published_on", "desc"), ("distance_m", "asc")]

    where: list[str] = []
    params: list[Any] = []
    effective_pub: str | None = None
    if not all_matches:
        date_text = str(published_on or "").strip()
        if not date_text:
            date_text = latest_published_on_iso(db_path) or ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            raise StandardTimesError("invalid_date", date_text)
        effective_pub = date_text
        where.append("st.published_on = ?")
        params.append(date_text)

    q_clause, q_params = _build_q_clause(q, q_op, keys, whitelist=whitelist)
    if q_clause:
        where.append(q_clause)
        params.extend(q_params)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    order_parts: list[str] = []
    for key, direction in specs:
        expr = _SORT_SQL.get(key)
        if not expr:
            continue
        order_parts.append(f"{expr} {'ASC' if direction == 'asc' else 'DESC'}")
    order_parts.append("st.id DESC")
    order_sql = ", ".join(order_parts)
    select_cols = ",\n      ".join(f"{expr} AS {key}" for key, expr in _COL_SQL.items())
    from_sql = "FROM racing_standard_times AS st"

    with get_connection(db_path) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS c {from_sql}{where_sql}",
                params,
            ).fetchone()["c"]
            or 0
        )
        total_pages = max(1, int(math.ceil(total / size))) if total else 0
        if total_pages and page > total_pages:
            page = total_pages
        offset = (page - 1) * size if total else 0
        rows_raw = conn.execute(
            f"""
            SELECT {select_cols}
            {from_sql}
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, size, offset],
        ).fetchall()

    rows = [_row_to_view(r) for r in rows_raw]
    return StandardTimesListResult(
        rows=rows,
        n=page if total else 0,
        total_count=total,
        total_pages=total_pages,
        page_size=size,
        q=q or "",
        all_matches=bool(all_matches),
        sort=standard_times_sort_param(specs),
        q_op=normalize_history_search_op(q_op),
        field_keys=tuple(_COL_SQL.keys()),
        search_keys=tuple(keys),
        published_on=effective_pub,
    )


def standard_times_layout_i18n_maps(
    text_map: Any, keys: Any
) -> tuple[dict[str, str], dict[str, str]]:
    labels: dict[str, str] = {}
    descs: dict[str, str] = {}
    for key in keys:
        raw_label = (text_map.get(f"racing.standard_times.col.{key}") or "").strip()
        labels[str(key)] = raw_label if raw_label else str(key)
        descs[str(key)] = (
            text_map.get(f"racing.standard_times.col.desc.{key}") or ""
        ).strip()
    return labels, descs


def endpoint_host_for_year(year: int) -> str:
    url = pdf_url_for_year(year)
    return urlparse(url).netloc or url

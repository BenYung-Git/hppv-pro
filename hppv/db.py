from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from .models import RaceRecord

# HKJC：HorseId 多為 HK_YYYY_XXXX；測試或舊資料可能為 HK_1。
_RE_HKJC_HORSE_ID = re.compile(r"^HK_[A-Za-z0-9_]+$")
_RE_BRAND_NO = re.compile(r"^[A-Z]\d{3}$")
_DISALLOWED_HORSE_IDS = frozenset(
    {
        "name",
        "horse",
        "brand",
        "rating",
        "no",
        "no.",
        "change",
        "status",
        "horseid",
        "brand no",
        "brand no.",
    }
)


def is_acceptable_horse_id(horse_id: str) -> bool:
    """可寫入 horses.horse_id 的值：HK_ 形式或烙號 H123；排除表頭誤判字樣。"""

    hid = str(horse_id or "").strip()
    if len(hid) < 2 or len(hid) > 48:
        return False
    if hid.lower() in _DISALLOWED_HORSE_IDS:
        return False
    if _RE_HKJC_HORSE_ID.match(hid):
        return True
    if _RE_BRAND_NO.match(hid):
        return True
    return False


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with safe defaults.

    Note: the rest of the codebase should prefer :func:`get_connection`.
    """

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Get a SQLite connection (recommended wrapper)."""

    return connect(db_path)


def init_db(db_path: Path) -> None:
    """Create schema if it doesn't exist."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS race_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              horse_id TEXT NOT NULL,
              race_date TEXT NOT NULL,
              venue TEXT NOT NULL,
              race_no TEXT NOT NULL,
              course TEXT NOT NULL,
              distance TEXT NOT NULL,
              going TEXT NOT NULL,
              class_band TEXT NOT NULL,
              draw TEXT NOT NULL,
              rating TEXT NOT NULL,
              jockey TEXT NOT NULL,
              trainer TEXT NOT NULL,
              finish_pos TEXT NOT NULL,
              running_pos TEXT NOT NULL DEFAULT '',
              win_odds TEXT NOT NULL DEFAULT '',
              time TEXT NOT NULL,
              prize TEXT NOT NULL,
              UNIQUE(
                horse_id, race_date, venue, race_no, course, distance, going, class_band, draw,
                rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
              )
            );
            CREATE INDEX IF NOT EXISTS idx_race_records_horse_id
              ON race_records(horse_id);

            CREATE TABLE IF NOT EXISTS horses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              horse_id TEXT NOT NULL UNIQUE,
              brand_no TEXT,
              name_zh TEXT,
              name_en TEXT,
              status TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_horses_name_zh
              ON horses(name_zh);
            CREATE INDEX IF NOT EXISTS idx_horses_name_en
              ON horses(name_en);
            """
        )
        # Migration: add new race_records columns for richer fields.
        rr_cols = conn.execute("PRAGMA table_info(race_records);").fetchall()
        rr_names = {row["name"] for row in rr_cols}
        if "running_pos" not in rr_names:
            conn.execute("ALTER TABLE race_records ADD COLUMN running_pos TEXT NOT NULL DEFAULT '';")
        if "win_odds" not in rr_names:
            conn.execute("ALTER TABLE race_records ADD COLUMN win_odds TEXT NOT NULL DEFAULT '';")

        # Migration: add brand_no for existing DBs (before creating index on it).
        cols = conn.execute("PRAGMA table_info(horses);").fetchall()
        col_names = {row["name"] for row in cols}
        if "brand_no" not in col_names:
            conn.execute("ALTER TABLE horses ADD COLUMN brand_no TEXT;")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_horses_brand_no ON horses(brand_no);"
        )
        # Backfill: older rows had horse_id = brand_no.
        conn.execute("UPDATE horses SET brand_no = horse_id WHERE brand_no IS NULL OR brand_no = '';")


def upsert_race_records(db_path: Path, records: Iterable[RaceRecord]) -> int:
    """Insert records; ignore duplicates. Returns inserted row count."""

    rows = [
        (
            r.horse_id,
            r.race_date,
            r.venue,
            r.race_no,
            r.course,
            r.distance,
            r.going,
            r.class_band,
            r.draw,
            r.rating,
            r.jockey,
            r.trainer,
            r.finish_pos,
            r.running_pos,
            r.win_odds,
            r.time,
            r.prize,
        )
        for r in records
    ]
    if not rows:
        return 0

    with get_connection(db_path) as conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO race_records (
              horse_id, race_date, venue, race_no, course, distance, going, class_band, draw,
              rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?
            );
            """,
            rows,
        )
        return cur.rowcount


def fetch_race_records(db_path: Path, horse_id: str) -> list[RaceRecord]:
    """Fetch all records for a given horse_id (newest first)."""

    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT
              horse_id, race_date, venue, race_no, course, distance, going, class_band,
              draw, rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
            FROM race_records
            WHERE horse_id = ?
            ORDER BY race_date DESC, id DESC;
            """,
            (horse_id,),
        )
        return [
            RaceRecord(
                horse_id=row["horse_id"],
                race_date=row["race_date"],
                venue=row["venue"],
                race_no=row["race_no"],
                course=row["course"],
                distance=row["distance"],
                going=row["going"],
                class_band=row["class_band"],
                draw=row["draw"],
                rating=row["rating"],
                jockey=row["jockey"],
                trainer=row["trainer"],
                finish_pos=row["finish_pos"],
                running_pos=row["running_pos"] if "running_pos" in row.keys() else "",
                win_odds=row["win_odds"] if "win_odds" in row.keys() else "",
                time=row["time"],
                prize=row["prize"],
            )
            for row in cur.fetchall()
        ]


def count_race_records(db_path: Path, horse_id: str) -> int:
    """Return total number of race_records for a horse_id."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM race_records WHERE horse_id = ?;",
            (horse_id,),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0


def upsert_horses(db_path: Path, horses: Iterable[Mapping[str, str | None]]) -> int:
    """Upsert horse basics into `horses` table.

    Each mapping should provide: `horse_id`, `brand_no` (optional), `name_zh`, `name_en`, and optional `status`.
    """

    rows: list[tuple[str, str | None, str | None, str | None, str | None]] = []
    for h in horses:
        horse_id = str(h.get("horse_id") or "").strip()
        brand_no = h.get("brand_no")
        if not horse_id or not is_acceptable_horse_id(horse_id):
            continue
        rows.append(
            (
                horse_id,
                brand_no,
                h.get("name_zh"),
                h.get("name_en"),
                h.get("status"),
            )
        )

    if not rows:
        return 0

    with get_connection(db_path) as conn:
        # 若有真實 HorseId（horse_id != brand_no），先刪除舊的「horse_id = brand_no」列，避免同一馬兩筆
        for horse_id, brand_no, *_ in rows:
            if brand_no and str(horse_id) != str(brand_no):
                conn.execute("DELETE FROM horses WHERE horse_id = ?;", (brand_no,))
        cur = conn.executemany(
            """
            INSERT OR REPLACE INTO horses (horse_id, brand_no, name_zh, name_en, status)
            VALUES (?, ?, ?, ?, ?);
            """,
            rows,
        )
        return cur.rowcount


def search_horses_by_name(
    db_path: Path, keyword: str, limit: int | None = 50
) -> list[sqlite3.Row]:
    """Search horses by name keyword (Chinese/English).

    Returns rows with at least: horse_id, name_zh, name_en, status.
    """

    kw = keyword.strip()
    if not kw:
        return []

    like = f"%{kw}%"

    query = """
        SELECT horse_id, brand_no, name_zh, name_en, status
        FROM horses
        WHERE name_zh LIKE ? OR name_en LIKE ?
        ORDER BY
          CASE
            WHEN name_zh LIKE ? THEN 0
            ELSE 1
          END,
          name_zh,
          name_en
    """
    params: tuple[object, ...] = (like, like, f"%{kw}%")
    if limit is not None:
        query += " LIMIT ?;"
        params = (*params, limit)
    else:
        query += ";"

    with get_connection(db_path) as conn:
        cur = conn.execute(query, params)
        return list(cur.fetchall())


def search_horses_by_keyword(
    db_path: Path, keyword: str, limit: int | None = 50
) -> list[sqlite3.Row]:
    """以關鍵字搜尋馬匹：HorseId、Brand No.、中文名、英文名（LIKE 模糊）；精確 HorseId / 烙號優先排序。"""

    kw = keyword.strip()
    if not kw:
        return []

    like = f"%{kw}%"
    exact = kw

    query = """
        SELECT horse_id, brand_no, name_zh, name_en, status
        FROM horses
        WHERE horse_id = ?
           OR UPPER(TRIM(COALESCE(brand_no, ''))) = UPPER(?)
           OR horse_id LIKE ?
           OR COALESCE(brand_no, '') LIKE ?
           OR COALESCE(name_zh, '') LIKE ?
           OR COALESCE(name_en, '') LIKE ?
        ORDER BY
          CASE
            WHEN horse_id = ? THEN 0
            WHEN UPPER(TRIM(COALESCE(brand_no, ''))) = UPPER(?) THEN 1
            ELSE 2
          END,
          horse_id
    """
    params: tuple[object, ...] = (
        exact,
        exact,
        like,
        like,
        like,
        like,
        exact,
        exact,
    )
    if limit is not None:
        query += " LIMIT ?;"
        params = (*params, limit)
    else:
        query += ";"

    with get_connection(db_path) as conn:
        cur = conn.execute(query, params)
        return list(cur.fetchall())


def count_horses(db_path: Path) -> int:
    """Count horses currently stored in the local database."""

    with get_connection(db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) AS c FROM horses;")
        row = cur.fetchone()
        return int(row["c"]) if row else 0


def get_horse_status(db_path: Path, horse_id: str) -> str:
    """Get horse status text from horses table by horse_id."""
    if not horse_id:
        return ""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT status FROM horses WHERE horse_id = ? LIMIT 1;",
            (horse_id,),
        )
        row = cur.fetchone()
        if not row:
            return ""
        return str(row["status"] or "").strip()


def fetch_horse_by_id(db_path: Path, horse_id: str) -> sqlite3.Row | None:
    """Return one horses row by horse_id, or None if not found."""

    hid = str(horse_id or "").strip()
    if not hid:
        return None
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT horse_id, brand_no, name_zh, name_en, status
            FROM horses
            WHERE horse_id = ?
            LIMIT 1
            """,
            (hid,),
        )
        return cur.fetchone()


def fetch_horses(db_path: Path, limit: int | None = 200) -> list[sqlite3.Row]:
    """Fetch horses for browsing (no keyword).

    Returns up to `limit` rows ordered by `horse_id`.
    """

    with get_connection(db_path) as conn:
        query = """
            SELECT horse_id, brand_no, name_zh, name_en, status
            FROM horses
            ORDER BY horse_id
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?;"
            params = (limit,)
        else:
            query += ";"

        cur = conn.execute(query, params)
        return list(cur.fetchall())


def search_race_records_by_keyword(
    db_path: Path, horse_id: str, keyword: str, limit: int | None = 200
) -> list[sqlite3.Row]:
    """Search race_records rows by keyword (venue/jockey/trainer/etc)."""

    kw = keyword.strip()
    if not horse_id or not kw:
        return []

    like = f"%{kw}%"

    query = """
        SELECT
          horse_id, race_date, venue, race_no, course, distance, going, class_band,
              draw, rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
        FROM race_records
        WHERE horse_id = ?
          AND (
            venue LIKE ? OR race_no LIKE ? OR course LIKE ? OR distance LIKE ?
            OR going LIKE ? OR class_band LIKE ? OR draw LIKE ? OR rating LIKE ?
            OR jockey LIKE ? OR trainer LIKE ? OR finish_pos LIKE ? OR time LIKE ?
                OR running_pos LIKE ? OR win_odds LIKE ? OR prize LIKE ?
          )
        ORDER BY race_date DESC, id DESC
    """
    params: tuple[object, ...] = (
        horse_id,
        like,
        like,
        like,
                like,
                like,
        like,
        like,
        like,
        like,
        like,
        like,
        like,
        like,
        like,
        like,
    )
    if limit is not None:
        query += " LIMIT ?;"
        params = (*params, limit)
    else:
        query += ";"

    with get_connection(db_path) as conn:
        cur = conn.execute(query, params)
        return list(cur.fetchall())


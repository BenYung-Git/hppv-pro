from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from .models import RaceRecord
from .schema import (
    core_schema_sql,
    racecard_base_schema_sql,
    racecard_starters_schema_sql,
)

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

# race_records.race_date 字串正規化成可排序日期（與 web_utils.race_date_sort_key 邏輯對齊）
_RACE_RECORD_DATE_ORDER_EXPR = (
    "CASE "
    "WHEN rr.race_date LIKE '__/__/__' "
    "THEN ('20' || substr(rr.race_date, 7, 2) || '-' || substr(rr.race_date, 4, 2) || '-' || substr(rr.race_date, 1, 2)) "
    "WHEN rr.race_date LIKE '__/__/____' "
    "THEN (substr(rr.race_date, 7, 4) || '-' || substr(rr.race_date, 4, 2) || '-' || substr(rr.race_date, 1, 2)) "
    "WHEN rr.race_date LIKE '____/__/__' "
    "THEN (substr(rr.race_date, 1, 4) || '-' || substr(rr.race_date, 6, 2) || '-' || substr(rr.race_date, 9, 2)) "
    "ELSE rr.race_date END"
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
        conn.executescript(core_schema_sql())
        conn.executescript(racecard_base_schema_sql())
        conn.executescript(racecard_starters_schema_sql())
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


def get_recent_form_for_horse(
    db_path: Path,
    horse_id: str,
    limit: int = 10,
    sort_seq: list[tuple[str, str]] | None = None,
) -> list[sqlite3.Row]:
    """
    排位 UI 用：取得指定馬匹最近 N 場往績（新至舊）。

    資料來源以 race_records 為主，並左連接 horses 取回馬匹名稱欄位。
    """
    hid = str(horse_id or "").strip()
    if not hid:
        return []
    lim = max(1, int(limit))
    # 與 horses 頁可排序欄位對齊（僅允許白名單，避免動態 SQL 注入）
    race_date_sort_expr = _RACE_RECORD_DATE_ORDER_EXPR
    sort_sql_map: dict[str, str] = {
        "race_date": race_date_sort_expr,
        "venue": "rr.venue",
        "race_no": "rr.race_no",
        "course": "rr.course",
        "distance": "rr.distance",
        "going": "rr.going",
        "class_band": "rr.class_band",
        "draw": "rr.draw",
        "rating": "rr.rating",
        "jockey": "rr.jockey",
        "trainer": "rr.trainer",
        "finish_pos": "rr.finish_pos",
        "running_pos": "rr.running_pos",
        "win_odds": "rr.win_odds",
        "time": "rr.time",
        "prize": "rr.prize",
    }
    order_parts: list[str] = []
    for item in (sort_seq or []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        key = str(item[0]).strip()
        direction = str(item[1]).strip().lower()
        expr = sort_sql_map.get(key)
        if not expr:
            continue
        order_parts.append(f"{expr} {'DESC' if direction == 'desc' else 'ASC'}")
        if len(order_parts) >= 3:
            break
    if not order_parts:
        # 預設：賽日（正規化）新至舊；場次數值大至小；再以流水號穩定排序
        order_parts = [
            f"{race_date_sort_expr} DESC",
            "CAST(rr.race_no AS INTEGER) DESC",
        ]
    # 同鍵值下，保持新資料優先
    order_parts.append("rr.id DESC")
    order_sql = ", ".join(order_parts)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            f"""
            SELECT
              rr.race_date,
              rr.venue,
              rr.race_no,
              rr.distance,
              rr.class_band,
              rr.draw,
              rr.finish_pos,
              rr.rating,
              rr.time,
              rr.jockey,
              rr.trainer,
              rr.going,
              rr.course,
              rr.running_pos,
              rr.win_odds,
              rr.prize,
              h.name_zh,
              h.name_en
            FROM race_records AS rr
            LEFT JOIN horses AS h
              ON h.horse_id = rr.horse_id
            WHERE rr.horse_id = ?
            ORDER BY {order_sql}
            LIMIT ?;
            """,
            (hid, lim),
        )
        return list(cur.fetchall())


def count_race_records(db_path: Path, horse_id: str) -> int:
    """Return total number of race_records for a horse_id."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM race_records WHERE horse_id = ?;",
            (horse_id,),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0


def get_all_horse_results_with_default_sort(db_path: Path) -> list[dict[str, str]]:
    """
    全部馬匹往績，預設排序：馬匹編號升序；每馬內賽日（正規化）新至舊、場次數值大至小、id 新至舊。
    回傳 list[dict]，鍵為中文欄名，供匯出 Excel。
    """
    init_db(db_path)
    order_sql = (
        f"rr.horse_id ASC, {_RACE_RECORD_DATE_ORDER_EXPR} DESC, "
        "CAST(rr.race_no AS INTEGER) DESC, rr.id DESC"
    )
    sql = f"""
    SELECT
      rr.horse_id AS "馬匹編號",
      COALESCE(h.brand_no, '') AS "烙號",
      COALESCE(h.name_zh, '') AS "馬名（中）",
      COALESCE(h.name_en, '') AS "馬名（英）",
      rr.race_date AS "賽日",
      rr.venue AS "馬場",
      rr.race_no AS "場次",
      rr.course AS "跑道",
      rr.distance AS "距離",
      rr.going AS "場地狀況",
      rr.class_band AS "班次",
      rr.draw AS "檔位",
      rr.rating AS "評分",
      rr.jockey AS "騎師",
      rr.trainer AS "練馬師",
      rr.finish_pos AS "名次",
      rr.running_pos AS "沿途走位",
      rr.win_odds AS "賠率",
      rr.time AS "時間",
      rr.prize AS "獎金"
    FROM race_records AS rr
    LEFT JOIN horses AS h ON h.horse_id = rr.horse_id
    ORDER BY {order_sql};
    """
    with get_connection(db_path) as conn:
        cur = conn.execute(sql)
        return [dict(row) for row in cur.fetchall()]


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


from __future__ import annotations

import sqlite3
from textwrap import dedent


RACECARD_RACE_INFO_TABLE = "racecard_race_info"
RACECARD_STARTERS_TABLE = "racecard_starters"
RACECARD_STARTERS_BASE_COLUMNS: tuple[str, ...] = (
    "id",
    "race_date",
    "racecourse",
    "race_no",
    "後備",
    "horse_id",
)
RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS: tuple[str, ...] = (
    "馬號",
    "馬名",
    "檔位",
    "騎師",
    "練馬師",
    "負磅",
    "評分",
    "馬匹體重",
    "配備",
)


def qident(identifier: str) -> str:
    """Quote SQLite identifier safely."""
    return '"' + identifier.replace('"', '""') + '"'


def core_schema_sql() -> str:
    """DDL for core hppv tables/indexes."""
    return dedent(
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


def racecard_base_schema_sql(race_info_table: str = RACECARD_RACE_INFO_TABLE) -> str:
    """DDL for racecard meta + race info table/index."""
    race_info = qident(race_info_table)
    return dedent(
        f"""
        CREATE TABLE IF NOT EXISTS racecard_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS {race_info} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL,
            racecourse TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            event_name TEXT,
            calendar_date TEXT,
            weekday TEXT,
            meeting_venue TEXT,
            post_time TEXT,
            race_course_area TEXT,
            track_name TEXT,
            distance TEXT,
            prize_money TEXT,
            rating_range TEXT,
            race_class TEXT,
            UNIQUE(race_date, racecourse, race_no)
        );
        CREATE INDEX IF NOT EXISTS idx_racecard_race_info_meeting
          ON {race_info}(race_date, racecourse, race_no);
        """
    )


def racecard_starters_schema_sql(
    starters_table: str = RACECARD_STARTERS_TABLE,
    predicted_odds_col: str = "預測賠率",
    immediate_odds_col: str = "即時賠率",
) -> str:
    """DDL for racecard starters table/index."""
    starters = qident(starters_table)
    predicted = qident(predicted_odds_col)
    immediate = qident(immediate_odds_col)
    return dedent(
        f"""
        CREATE TABLE IF NOT EXISTS {starters} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL,
            racecourse TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            後備 TEXT NOT NULL DEFAULT '否',
            horse_id TEXT,
            "馬號" TEXT,
            "馬名" TEXT,
            "檔位" TEXT,
            "騎師" TEXT,
            "練馬師" TEXT,
            "負磅" TEXT,
            "評分" TEXT,
            "馬匹體重" TEXT,
            "配備" TEXT,
            {predicted} TEXT,
            {immediate} TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_racecard_starters_meeting
          ON {starters}(race_date, racecourse, race_no);
        """
    )


def racecard_starters_expected_columns(
    display_fields: list[str] | tuple[str, ...] = RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS,
    predicted_odds_col: str = "預測賠率",
    immediate_odds_col: str = "即時賠率",
) -> set[str]:
    """Return canonical racecard_starters columns expected by app."""
    return {
        *RACECARD_STARTERS_BASE_COLUMNS,
        *display_fields,
        predicted_odds_col,
        immediate_odds_col,
    }


SOCCER_DAILY_TABLE = "soccer_daily"
SOCCER_DAILY_COLUMNS: tuple[str, ...] = (
    "match_id",
    "match_code",
    "match_date",
    "kickoff_at",
    "competition_code",
    "competition_name_zh",
    "home_team",
    "away_team",
    "match_status",
    "home_odds",
    "draw_odds",
    "away_odds",
    "odds_status",
    "odds_fetched_at",
    # Official result fields (nullable).
    # result_fetched_at: update only after a per-match official result is validated and
    # committed; never on odds-only updates, init, filter/search, or failed/rollback paths.
    # Extra-time score_*_et columns deferred until source meaning is confirmed.
    "score_ht_home",
    "score_ht_away",
    "score_2h_home",
    "score_2h_away",
    "score_ft_home",
    "score_ft_away",
    "score_pen_home",
    "score_pen_away",
    "result_status",
    "result_fetched_at",
    "created_at",
    "updated_at",
)

# Additive columns for existing DBs (name, SQLite type). No ET columns until confirmed.
SOCCER_DAILY_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("score_ht_home", "INTEGER"),
    ("score_ht_away", "INTEGER"),
    ("score_2h_home", "INTEGER"),
    ("score_2h_away", "INTEGER"),
    ("score_ft_home", "INTEGER"),
    ("score_ft_away", "INTEGER"),
    ("score_pen_home", "INTEGER"),
    ("score_pen_away", "INTEGER"),
    ("result_status", "TEXT"),
    ("result_fetched_at", "TEXT"),
)


def soccer_daily_schema_sql() -> str:
    """DDL for soccer daily matches table/index (independent soccer.sqlite3)."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS soccer_daily (
            match_id TEXT PRIMARY KEY NOT NULL,
            match_code TEXT,
            match_date TEXT NOT NULL,
            kickoff_at TEXT NOT NULL,
            competition_code TEXT,
            competition_name_zh TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            match_status TEXT,
            home_odds REAL,
            draw_odds REAL,
            away_odds REAL,
            odds_status TEXT,
            odds_fetched_at TEXT,
            score_ht_home INTEGER,
            score_ht_away INTEGER,
            score_2h_home INTEGER,
            score_2h_away INTEGER,
            score_ft_home INTEGER,
            score_ft_away INTEGER,
            score_pen_home INTEGER,
            score_pen_away INTEGER,
            result_status TEXT,
            result_fetched_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def soccer_daily_index_sql() -> str:
    """Index DDL; run only after soccer_daily columns are verified."""
    return dedent(
        """
        CREATE INDEX IF NOT EXISTS idx_soccer_daily_date_kickoff
            ON soccer_daily (match_date, kickoff_at);
        """
    )


def soccer_daily_expected_columns() -> set[str]:
    """Return canonical soccer_daily columns for Phase I schema."""
    return set(SOCCER_DAILY_COLUMNS)


TRANSLATIONS_TABLE = "translations"
TRANSLATIONS_COLUMNS: tuple[str, ...] = (
    "TxId",
    "text_key",
    "zh_hant",
    "en",
    "zh_hans",
    "isDisable",
)


def translations_schema_sql() -> str:
    """DDL for UI translations table in soccer.sqlite3."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS translations (
            TxId INTEGER PRIMARY KEY AUTOINCREMENT,
            text_key TEXT NOT NULL UNIQUE,
            zh_hant TEXT NOT NULL,
            en TEXT NOT NULL,
            zh_hans TEXT NOT NULL,
            isDisable INTEGER NOT NULL DEFAULT 0
                CHECK (isDisable IN (0, 1))
        );

        CREATE INDEX IF NOT EXISTS idx_translations_isDisable
            ON translations (isDisable);
        """
    )


def translations_expected_columns() -> set[str]:
    """Return canonical translations columns."""
    return set(TRANSLATIONS_COLUMNS)


SOCCER_SYNC_STATE_TABLE = "soccer_sync_state"
SOCCER_SYNC_STATE_COLUMNS: tuple[str, ...] = (
    "request_key",
    "last_success_at",
)


def soccer_sync_state_schema_sql() -> str:
    """DDL for soccer request sync timestamps."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS soccer_sync_state (
            request_key TEXT PRIMARY KEY NOT NULL,
            last_success_at TEXT NOT NULL
        );
        """
    )


def soccer_sync_state_expected_columns() -> set[str]:
    return set(SOCCER_SYNC_STATE_COLUMNS)


SOCCER_LAYOUT_SETTINGS_TABLE = "soccer_layout_settings"
SOCCER_LAYOUT_SETTINGS_COLUMNS: tuple[str, ...] = (
    "layout_key",
    "settings_json",
    "updated_at",
)


def soccer_layout_settings_schema_sql() -> str:
    """DDL for soccer daily list layout settings (one row per layout_key)."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS soccer_layout_settings (
            layout_key TEXT PRIMARY KEY NOT NULL,
            settings_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def soccer_layout_settings_expected_columns() -> set[str]:
    return set(SOCCER_LAYOUT_SETTINGS_COLUMNS)


SOCCER_TASK_SETTINGS_TABLE = "soccer_task_settings"
SOCCER_TASK_SETTINGS_COLUMNS: tuple[str, ...] = (
    "task_key",
    "enabled",
    "interval_value",
    "interval_unit",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
    "last_detail_json",
    "updated_at",
)


def soccer_task_settings_schema_sql() -> str:
    """DDL for soccer web task schedule settings (separate from soccer_sync_state)."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS soccer_task_settings (
            task_key TEXT PRIMARY KEY NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            interval_value INTEGER NOT NULL DEFAULT 1,
            interval_unit TEXT NOT NULL DEFAULT 'mins',
            next_run_at TEXT,
            last_run_at TEXT,
            last_status TEXT,
            last_error TEXT,
            last_detail_json TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )


def soccer_task_settings_expected_columns() -> set[str]:
    return set(SOCCER_TASK_SETTINGS_COLUMNS)


SOCCER_DAILY_REMOVED_TABLE = "soccer_daily_removed"
SOCCER_DAILY_REMOVED_COLUMNS: tuple[str, ...] = (
    "match_id",
    "removed_at",
    "reason",
)


def soccer_daily_removed_schema_sql() -> str:
    """
    Tombstone for soccer_daily rows removed after successful official-result
    history write. Update-matches must skip re-inserting these match_ids.
    """
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS soccer_daily_removed (
            match_id TEXT PRIMARY KEY NOT NULL,
            removed_at TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        """
    )


def soccer_daily_removed_expected_columns() -> set[str]:
    return set(SOCCER_DAILY_REMOVED_COLUMNS)


def _read_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({qident(table)});").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["name"]) for row in rows}


def ensure_racecard_starters_columns(
    conn: sqlite3.Connection,
    starters_table: str = RACECARD_STARTERS_TABLE,
    display_fields: list[str] | tuple[str, ...] = RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS,
    predicted_odds_col: str = "預測賠率",
    immediate_odds_col: str = "即時賠率",
) -> None:
    """
    Ensure racecard_starters structure is compatible with current app.

    Compatibility strategy (kept from legacy UI logic):
    - If table has unexpected columns, rebuild table to canonical schema.
    - If table is missing expected columns, add them via ALTER TABLE.
    """
    expected = racecard_starters_expected_columns(
        display_fields=display_fields,
        predicted_odds_col=predicted_odds_col,
        immediate_odds_col=immediate_odds_col,
    )
    actual = _read_table_columns(conn, starters_table)
    unexpected = actual - expected
    if actual and unexpected:
        conn.execute(f"DROP TABLE IF EXISTS {qident(starters_table)};")
        actual = set()

    if not actual:
        conn.executescript(
            racecard_starters_schema_sql(
                starters_table=starters_table,
                predicted_odds_col=predicted_odds_col,
                immediate_odds_col=immediate_odds_col,
            )
        )
        return

    missing = expected - actual
    for col in missing:
        conn.execute(
            f"ALTER TABLE {qident(starters_table)} ADD COLUMN {qident(col)} TEXT;"
        )

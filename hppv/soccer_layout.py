from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hppv.schema import SOCCER_LAYOUT_SETTINGS_TABLE, soccer_layout_settings_schema_sql
from hppv.soccer_db import get_connection, table_exists

LAYOUT_KEY_DAILY_MATCHES = "daily_matches"
LAYOUT_KEY_MATCH_HISTORY = "match_history"

COLUMN_WIDTH_DEFAULT = 100
COLUMN_WIDTH_MIN = 60
COLUMN_WIDTH_MAX = 600

SORT_EMPTY = ""
SORT_ASC = "asc"
SORT_DESC = "desc"
ALLOWED_SORT = frozenset({SORT_EMPTY, SORT_ASC, SORT_DESC})

# Display-field catalog for Daily Matches.
# score_* display keys are formatted from paired soccer_daily score_*_home/away columns.
# ui_et is a layout-only placeholder until official ET meaning/columns are confirmed
# (no score_et_* DB columns yet; values always display as —).
COLUMN_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "match_date", "group": "basic", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "kickoff_hhmm", "group": "basic", "default_visible": True, "default_width": 100, "default_sort": SORT_ASC, "sortable": True},
    {"key": "match_code", "group": "basic", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "icon", "group": "basic", "default_visible": True, "default_width": 60, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "competition_code", "group": "basic", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "competition_name_zh", "group": "basic", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "home_team", "group": "basic", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "away_team", "group": "basic", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "home_odds", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "draw_odds", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "away_odds", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "score_ht", "group": "result", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "score_2h", "group": "result", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "score_ft", "group": "result", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "ui_et", "group": "result_pending", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "score_pen", "group": "result", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "result_status", "group": "result", "default_visible": False, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
)

# Match History (footylogic_matches) — independent catalog; never copy daily user settings.
# Base display keys stay first / stable. Extra physical columns are appended from
# live PRAGMA table_info so new DB fields appear without frontend edits.
# Composite score columns are display-only: no invented sort by goals / home score.
HISTORY_BASE_COLUMN_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "match_date", "group": "basic", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "kickoff_hhmm", "group": "basic", "default_visible": True, "default_width": 90, "default_sort": SORT_DESC, "sortable": True},
    {"key": "competition_name", "group": "basic", "default_visible": True, "default_width": 160, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "home_team", "group": "basic", "default_visible": True, "default_width": 140, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "away_team", "group": "basic", "default_visible": True, "default_width": 140, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "score_ht", "group": "result", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "score_ft", "group": "result", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": False},
)

# Physical columns already represented by the base display aliases above.
HISTORY_COVERED_PHYSICAL_COLUMNS: frozenset[str] = frozenset(
    {
        "match_date_hkt",
        "kickoff_hkt",
        "competition_name",
        "home_team_name",
        "home_team_id",
        "away_team_name",
        "away_team_id",
        "score_ht_home",
        "score_ht_away",
        "score_ft_home",
        "score_ft_away",
    }
)

# Large / low-signal fields stay in catalog but hidden by default.
HISTORY_DEFAULT_HIDDEN_PHYSICAL: frozenset[str] = frozenset({"event_json"})

# Backward-compatible name: base catalog only (no DB). Prefer resolve_history_column_catalog().
HISTORY_COLUMN_CATALOG = HISTORY_BASE_COLUMN_CATALOG

# Rename legacy layout-only placeholder keys to current display keys.
# ui_et is kept as-is (pending ET schema); never silently drop saved layout rows.
LEGACY_LAYOUT_KEY_MAP: dict[str, str] = {
    "ui_ht": "score_ht",
    "ui_2h": "score_2h",
    "ui_ft": "score_ft",
    "ui_pen": "score_pen",
}

COLUMN_KEYS = frozenset(c["key"] for c in COLUMN_CATALOG)
COLUMN_BY_KEY = {c["key"]: c for c in COLUMN_CATALOG}
HISTORY_COLUMN_KEYS = frozenset(c["key"] for c in HISTORY_COLUMN_CATALOG)
HISTORY_COLUMN_BY_KEY = {c["key"]: c for c in HISTORY_COLUMN_CATALOG}


def _history_extra_column_meta(col_name: str) -> dict[str, Any]:
    if col_name.startswith("score_") or col_name.startswith("final_odds_"):
        group = "result" if col_name.startswith("score_") else "odds"
        width = 110 if col_name.startswith("final_odds_") else 100
    elif col_name in {"source_key", "source", "competition_id", "season_id", "month"}:
        group = "basic"
        width = 120
    else:
        group = "meta"
        width = 140
    return {
        "key": col_name,
        "group": group,
        "default_visible": col_name not in HISTORY_DEFAULT_HIDDEN_PHYSICAL,
        "default_width": width,
        "default_sort": SORT_EMPTY,
        "sortable": col_name
        in {
            "competition_id",
            "season_id",
            "month",
            "source_key",
            "kickoff_utc",
            "source_last_updated",
            "created_at",
            "updated_at",
            "has_score",
            "score_complete",
            "result_confirm_status",
        },
    }


def history_column_catalog_from_physical(
    physical_columns: list[str] | tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    """Base display columns first, then remaining footylogic_matches columns in table order."""
    base_keys = {c["key"] for c in HISTORY_BASE_COLUMN_CATALOG}
    extras: list[dict[str, Any]] = []
    for name in physical_columns:
        if name in HISTORY_COVERED_PHYSICAL_COLUMNS:
            continue
        if name in base_keys:
            continue
        extras.append(_history_extra_column_meta(name))
    return HISTORY_BASE_COLUMN_CATALOG + tuple(extras)


def read_footylogic_matches_columns(db_path: Path | None) -> list[str]:
    """Live schema only — no cached column list."""
    if db_path is None or not Path(db_path).is_file():
        return []
    from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE

    with get_connection(Path(db_path)) as conn:
        if not table_exists(conn, FOOTYLOGIC_MATCHES_TABLE):
            return []
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({FOOTYLOGIC_MATCHES_TABLE})")]


def resolve_history_column_catalog(db_path: Path | None = None) -> tuple[dict[str, Any], ...]:
    physical = read_footylogic_matches_columns(db_path)
    if not physical:
        return HISTORY_BASE_COLUMN_CATALOG
    return history_column_catalog_from_physical(physical)


def catalog_for_layout_key(
    layout_key: str,
    *,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    if layout_key == LAYOUT_KEY_MATCH_HISTORY:
        return resolve_history_column_catalog(db_path)
    return COLUMN_CATALOG


def column_keys_for_layout_key(
    layout_key: str,
    *,
    db_path: Path | None = None,
) -> frozenset[str]:
    return frozenset(c["key"] for c in catalog_for_layout_key(layout_key, db_path=db_path))

ZEBRA_COLORS: tuple[dict[str, str], ...] = (
    {"id": "row_alt_gray", "css": "#f4f6f9"},
    {"id": "row_alt_green", "css": "#eef7f1"},
    {"id": "row_alt_blue", "css": "#eef3fb"},
    {"id": "row_alt_sand", "css": "#f7f3ea"},
)
ZEBRA_COLOR_IDS = frozenset(c["id"] for c in ZEBRA_COLORS)
ZEBRA_DEFAULT_ID = "row_alt_gray"


class SoccerLayoutError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_column_width(raw: Any) -> int:
    """Shared width rule: integer in [60, 600]. No silent clamp."""
    if isinstance(raw, bool) or raw is None:
        raise SoccerLayoutError("invalid_width", f"width invalid: {raw!r}")
    if isinstance(raw, float) and not raw.is_integer():
        raise SoccerLayoutError("invalid_width", f"width must be integer: {raw!r}")
    try:
        width = int(raw)
    except (TypeError, ValueError) as e:
        raise SoccerLayoutError("invalid_width", f"width must be integer: {raw!r}") from e
    if isinstance(raw, str) and str(raw).strip() != str(width):
        raise SoccerLayoutError("invalid_width", f"width must be integer: {raw!r}")
    if width < COLUMN_WIDTH_MIN or width > COLUMN_WIDTH_MAX:
        raise SoccerLayoutError(
            "invalid_width",
            f"width out of range [{COLUMN_WIDTH_MIN}, {COLUMN_WIDTH_MAX}]: {width}",
        )
    return width


def default_layout_settings(
    layout_key: str = LAYOUT_KEY_DAILY_MATCHES,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    columns = []
    for c in catalog_for_layout_key(layout_key, db_path=db_path):
        columns.append(
            {
                "key": c["key"],
                "visible": bool(c["default_visible"]),
                "width": int(c["default_width"]),
                "sort": c["default_sort"],
            }
        )
    return {
        "zebra_enabled": True,
        "zebra_color_id": ZEBRA_DEFAULT_ID,
        "columns": columns,
    }


def _migrate_legacy_column_list(
    cols_raw: list[Any],
    *,
    layout_key: str = LAYOUT_KEY_DAILY_MATCHES,
    db_path: Path | None = None,
) -> list[Any]:
    """
    Daily only: map legacy ui_ht/2h/ft/pen keys to score_*; keep ui_et.
    History: append missing catalog keys only (no daily legacy rename).
    """
    catalog = catalog_for_layout_key(layout_key, db_path=db_path)
    migrated: list[Any] = []
    seen: set[str] = set()
    for item in cols_raw:
        if not isinstance(item, dict):
            migrated.append(item)
            continue
        key = item.get("key")
        if layout_key == LAYOUT_KEY_DAILY_MATCHES:
            new_key = LEGACY_LAYOUT_KEY_MAP.get(str(key), key)
        else:
            new_key = key
        if new_key in seen:
            continue
        copy = dict(item)
        copy["key"] = new_key
        migrated.append(copy)
        seen.add(str(new_key))
    for c in catalog:
        if c["key"] in seen:
            continue
        migrated.append(
            {
                "key": c["key"],
                "visible": bool(c["default_visible"]),
                "width": int(c["default_width"]),
                "sort": c["default_sort"],
            }
        )
        seen.add(c["key"])
    return migrated


def _normalize_saved_settings(
    raw: Any,
    *,
    layout_key: str = LAYOUT_KEY_DAILY_MATCHES,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SoccerLayoutError("invalid_settings", "settings must be an object")
    zebra_enabled = raw.get("zebra_enabled")
    if not isinstance(zebra_enabled, bool):
        raise SoccerLayoutError("invalid_settings", "zebra_enabled must be boolean")
    zebra_color_id = raw.get("zebra_color_id")
    if zebra_color_id not in ZEBRA_COLOR_IDS:
        raise SoccerLayoutError("invalid_settings", f"unknown zebra_color_id: {zebra_color_id!r}")
    cols_raw = raw.get("columns")
    if not isinstance(cols_raw, list) or not cols_raw:
        raise SoccerLayoutError("invalid_settings", "columns must be a non-empty array")
    cols_raw = _migrate_legacy_column_list(cols_raw, layout_key=layout_key, db_path=db_path)

    catalog = catalog_for_layout_key(layout_key, db_path=db_path)
    catalog_keys = frozenset(c["key"] for c in catalog)
    by_key = {c["key"]: c for c in catalog}

    seen: set[str] = set()
    columns: list[dict[str, Any]] = []
    for item in cols_raw:
        if not isinstance(item, dict):
            raise SoccerLayoutError("invalid_settings", "column item must be object")
        key = item.get("key")
        if key not in catalog_keys:
            raise SoccerLayoutError("invalid_settings", f"unknown column key: {key!r}")
        if key in seen:
            raise SoccerLayoutError("invalid_settings", f"duplicate column key: {key!r}")
        seen.add(str(key))
        visible = item.get("visible")
        if not isinstance(visible, bool):
            raise SoccerLayoutError("invalid_settings", f"visible must be boolean for {key}")
        width = validate_column_width(item.get("width"))
        sort = item.get("sort")
        if sort not in ALLOWED_SORT:
            raise SoccerLayoutError("invalid_settings", f"invalid sort for {key}: {sort!r}")
        # Non-sortable columns must remain empty (no invented composite-score sorts).
        if not by_key[str(key)].get("sortable", True) and sort not in (None, SORT_EMPTY):
            raise SoccerLayoutError(
                "invalid_settings",
                f"sort not supported for column: {key!r}",
            )
        columns.append({"key": key, "visible": visible, "width": width, "sort": sort or SORT_EMPTY})

    missing = catalog_keys - seen
    if missing:
        raise SoccerLayoutError("invalid_settings", f"missing columns: {sorted(missing)}")

    return {
        "zebra_enabled": zebra_enabled,
        "zebra_color_id": zebra_color_id,
        "columns": columns,
    }


def ensure_layout_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(soccer_layout_settings_schema_sql())
    if not table_exists(conn, SOCCER_LAYOUT_SETTINGS_TABLE):
        raise SoccerLayoutError("table_missing", "soccer_layout_settings missing after init")


def get_layout_settings(db_path: Path, layout_key: str = LAYOUT_KEY_DAILY_MATCHES) -> dict[str, Any]:
    if not db_path.is_file():
        return default_layout_settings(layout_key, db_path=db_path)
    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_LAYOUT_SETTINGS_TABLE):
            return default_layout_settings(layout_key, db_path=db_path)
        row = conn.execute(
            "SELECT settings_json FROM soccer_layout_settings WHERE layout_key = ?;",
            (layout_key,),
        ).fetchone()
    if not row:
        return default_layout_settings(layout_key, db_path=db_path)
    try:
        parsed = json.loads(str(row["settings_json"]))
    except json.JSONDecodeError as e:
        raise SoccerLayoutError("invalid_stored_layout", "stored settings_json is not valid JSON") from e
    normalized = _normalize_saved_settings(parsed, layout_key=layout_key, db_path=db_path)
    # Persist newly appended catalog keys without resetting others / other layout_keys.
    catalog_keys = column_keys_for_layout_key(layout_key, db_path=db_path)
    stored_keys: set[str] = set()
    cols = parsed.get("columns") if isinstance(parsed, dict) else None
    if isinstance(cols, list):
        for item in cols:
            if isinstance(item, dict) and item.get("key") is not None:
                key = str(item["key"])
                if layout_key == LAYOUT_KEY_DAILY_MATCHES:
                    key = str(LEGACY_LAYOUT_KEY_MAP.get(key, key))
                stored_keys.add(key)
    if catalog_keys - stored_keys:
        try:
            save_layout_settings(db_path, normalized, layout_key=layout_key)
        except SoccerLayoutError:
            # Read path must still return normalized settings even if write-back fails.
            pass
    return normalized


def save_layout_settings(
    db_path: Path,
    settings: Any,
    *,
    layout_key: str = LAYOUT_KEY_DAILY_MATCHES,
    now_utc: str | None = None,
) -> dict[str, Any]:
    if not db_path.is_file():
        raise SoccerLayoutError("db_missing", f"database missing: {db_path}")
    normalized = _normalize_saved_settings(settings, layout_key=layout_key, db_path=db_path)
    stamp = now_utc or utc_now_iso()
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    with get_connection(db_path) as conn:
        ensure_layout_schema(conn)
        conn.execute(
            """
            INSERT INTO soccer_layout_settings (layout_key, settings_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(layout_key) DO UPDATE SET
                settings_json = excluded.settings_json,
                updated_at = excluded.updated_at;
            """,
            (layout_key, payload, stamp),
        )
        conn.commit()
    return normalized


def zebra_css_for_id(color_id: str) -> str:
    for item in ZEBRA_COLORS:
        if item["id"] == color_id:
            return item["css"]
    raise SoccerLayoutError("invalid_settings", f"unknown zebra_color_id: {color_id!r}")


def catalog_public(
    layout_key: str = LAYOUT_KEY_DAILY_MATCHES,
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Column metadata for UI (no translations). Schema-driven for match_history."""
    out = []
    for c in catalog_for_layout_key(layout_key, db_path=db_path):
        out.append(
            {
                "key": c["key"],
                "group": c["group"],
                "default_visible": c["default_visible"],
                "default_width": c["default_width"],
                "default_sort": c["default_sort"],
                "sortable": bool(c.get("sortable", True)),
            }
        )
    return out


@dataclass(frozen=True, slots=True)
class WidthLimits:
    default: int = COLUMN_WIDTH_DEFAULT
    minimum: int = COLUMN_WIDTH_MIN
    maximum: int = COLUMN_WIDTH_MAX


"""Shared layout settings entry for soccer + racing.

One read / save / catalog procedure; sport + layout_key select catalog + storage.
Missing or invalid stored settings → SharedLayoutError (no default / no silent repair).
Soccer storage: SQLite. Racing storage: JSON file. No cross-sport data merge.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from hppv.config import default_config
from hppv.schema import SOCCER_LAYOUT_SETTINGS_TABLE, soccer_layout_settings_schema_sql
from hppv.soccer_db import get_connection, table_exists
from hppv.soccer_layout import (
    ALLOWED_SORT,
    COLUMN_WIDTH_MAX,
    COLUMN_WIDTH_MIN,
    LAYOUT_KEY_DAILY_MATCHES,
    LAYOUT_KEY_MATCH_HISTORY,
    LEGACY_LAYOUT_KEY_MAP,
    SORT_ASC,
    SORT_DESC,
    SORT_EMPTY,
    ZEBRA_COLOR_IDS,
    ZEBRA_COLORS,
    catalog_for_layout_key as soccer_catalog_for_layout_key,
    utc_now_iso,
    zebra_css_for_id as soccer_zebra_css_for_id,
)
from hppv.racing_layout import (
    LAYOUT_KEY_RACING_DAILY,
    LAYOUT_KEY_RACING_HISTORY,
    catalog_for_layout_key as racing_catalog_for_layout_key,
    layout_settings_path,
)

SPORT_SOCCER = "soccer"
SPORT_RACING = "racing"
SUPPORTED_SPORTS = frozenset({SPORT_SOCCER, SPORT_RACING})

SOCCER_LAYOUT_KEYS = frozenset({LAYOUT_KEY_DAILY_MATCHES, LAYOUT_KEY_MATCH_HISTORY})
RACING_LAYOUT_KEYS = frozenset({LAYOUT_KEY_RACING_DAILY, LAYOUT_KEY_RACING_HISTORY})
ALL_LAYOUT_KEYS = SOCCER_LAYOUT_KEYS | RACING_LAYOUT_KEYS

SPORT_LAYOUT_KEYS: dict[str, frozenset[str]] = {
    SPORT_SOCCER: SOCCER_LAYOUT_KEYS,
    SPORT_RACING: RACING_LAYOUT_KEYS,
}


class SharedLayoutError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def normalize_sport(sport: str) -> str:
    s = str(sport or "").strip().lower()
    if s not in SUPPORTED_SPORTS:
        raise SharedLayoutError("unsupported_sport", f"sport={sport!r}")
    return s


def normalize_layout_key(sport: str, layout_key: str) -> str:
    sport_n = normalize_sport(sport)
    key = str(layout_key or "").strip()
    allowed = SPORT_LAYOUT_KEYS[sport_n]
    if key not in allowed:
        raise SharedLayoutError(
            "unsupported_layout_key",
            f"sport={sport_n} layout_key={layout_key!r}",
        )
    return key


def catalog_for(
    sport: str,
    layout_key: str,
    *,
    soccer_db_path: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    sport_n = normalize_sport(sport)
    key = normalize_layout_key(sport_n, layout_key)
    if sport_n == SPORT_SOCCER:
        return soccer_catalog_for_layout_key(key, db_path=soccer_db_path)
    catalog = racing_catalog_for_layout_key(key)
    if key not in RACING_LAYOUT_KEYS:
        raise SharedLayoutError("unsupported_layout_key", f"layout_key={key!r}")
    return catalog


def catalog_public(
    sport: str,
    layout_key: str,
    *,
    soccer_db_path: Path | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in catalog_for(sport, layout_key, soccer_db_path=soccer_db_path):
        out.append(
            {
                "key": c["key"],
                "group": c.get("group", ""),
                "default_visible": bool(c.get("default_visible", True)),
                "default_width": int(c.get("default_width") or 100),
                "default_sort": str(c.get("default_sort") or SORT_EMPTY),
                "sortable": bool(c.get("sortable", False)),
            }
        )
    return out


def _validate_column_width(raw: Any) -> int:
    if isinstance(raw, bool) or raw is None:
        raise SharedLayoutError("invalid_width", f"width invalid: {raw!r}")
    if isinstance(raw, float) and not raw.is_integer():
        raise SharedLayoutError("invalid_width", f"width must be integer: {raw!r}")
    try:
        width = int(raw)
    except (TypeError, ValueError) as e:
        raise SharedLayoutError("invalid_width", f"width must be integer: {raw!r}") from e
    if isinstance(raw, str) and str(raw).strip() != str(width):
        raise SharedLayoutError("invalid_width", f"width must be integer: {raw!r}")
    if width < COLUMN_WIDTH_MIN or width > COLUMN_WIDTH_MAX:
        raise SharedLayoutError(
            "invalid_width",
            f"width out of range [{COLUMN_WIDTH_MIN}, {COLUMN_WIDTH_MAX}]: {width}",
        )
    return width


def _rename_legacy_daily_keys(cols_raw: list[Any]) -> list[Any]:
    """Rename known daily legacy keys only; does not invent missing columns."""
    migrated: list[Any] = []
    seen: set[str] = set()
    for item in cols_raw:
        if not isinstance(item, dict):
            raise SharedLayoutError("invalid_settings", "column item must be object")
        key = LEGACY_LAYOUT_KEY_MAP.get(str(item.get("key")), item.get("key"))
        if key in seen:
            continue
        copy = dict(item)
        copy["key"] = key
        migrated.append(copy)
        seen.add(str(key))
    return migrated


def normalize_layout_settings(
    raw: Any,
    *,
    sport: str,
    layout_key: str,
    soccer_db_path: Path | None = None,
) -> dict[str, Any]:
    """Strict normalize. No silent defaults, clamps, or inventing missing columns."""
    sport_n = normalize_sport(sport)
    key = normalize_layout_key(sport_n, layout_key)
    if not isinstance(raw, dict):
        raise SharedLayoutError("invalid_settings", "settings must be an object")
    zebra_enabled = raw.get("zebra_enabled")
    if not isinstance(zebra_enabled, bool):
        raise SharedLayoutError("invalid_settings", "zebra_enabled must be boolean")
    zebra_color_id = raw.get("zebra_color_id")
    if zebra_color_id not in ZEBRA_COLOR_IDS:
        raise SharedLayoutError(
            "invalid_settings", f"unknown zebra_color_id: {zebra_color_id!r}"
        )
    cols_raw = raw.get("columns")
    if not isinstance(cols_raw, list) or not cols_raw:
        raise SharedLayoutError("invalid_settings", "columns must be a non-empty array")
    if key == LAYOUT_KEY_DAILY_MATCHES:
        cols_raw = _rename_legacy_daily_keys(cols_raw)

    catalog = catalog_for(sport_n, key, soccer_db_path=soccer_db_path)
    catalog_keys = frozenset(c["key"] for c in catalog)
    by_key = {c["key"]: c for c in catalog}

    seen: set[str] = set()
    columns: list[dict[str, Any]] = []
    for item in cols_raw:
        if not isinstance(item, dict):
            raise SharedLayoutError("invalid_settings", "column item must be object")
        col_key = item.get("key")
        if col_key not in catalog_keys:
            raise SharedLayoutError(
                "invalid_settings", f"unknown column key: {col_key!r}"
            )
        if col_key in seen:
            raise SharedLayoutError(
                "invalid_settings", f"duplicate column key: {col_key!r}"
            )
        seen.add(str(col_key))
        visible = item.get("visible")
        if not isinstance(visible, bool):
            raise SharedLayoutError(
                "invalid_settings", f"visible must be boolean for {col_key}"
            )
        width = _validate_column_width(item.get("width"))
        sort = item.get("sort")
        if sort not in ALLOWED_SORT:
            raise SharedLayoutError(
                "invalid_settings", f"invalid sort for {col_key}: {sort!r}"
            )
        if not by_key[str(col_key)].get("sortable", True) and sort not in (
            None,
            SORT_EMPTY,
        ):
            raise SharedLayoutError(
                "invalid_settings", f"sort not supported for column: {col_key!r}"
            )
        columns.append(
            {
                "key": col_key,
                "visible": visible,
                "width": width,
                "sort": sort or SORT_EMPTY,
            }
        )

    missing = catalog_keys - seen
    if missing:
        raise SharedLayoutError(
            "layout_incomplete",
            f"missing columns: {sorted(missing)}",
        )

    return {
        "zebra_enabled": zebra_enabled,
        "zebra_color_id": zebra_color_id,
        "columns": columns,
    }


def _load_soccer_raw(db_path: Path, layout_key: str) -> dict[str, Any]:
    if not db_path.is_file():
        raise SharedLayoutError("layout_missing", f"database missing: {db_path}")
    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_LAYOUT_SETTINGS_TABLE):
            raise SharedLayoutError(
                "layout_missing", "soccer_layout_settings table missing"
            )
        row = conn.execute(
            "SELECT settings_json FROM soccer_layout_settings WHERE layout_key = ?;",
            (layout_key,),
        ).fetchone()
    if not row:
        raise SharedLayoutError("layout_missing", f"no stored settings for {layout_key}")
    try:
        parsed = json.loads(str(row["settings_json"]))
    except json.JSONDecodeError as e:
        raise SharedLayoutError(
            "invalid_stored_layout", "stored settings_json is not valid JSON"
        ) from e
    if not isinstance(parsed, dict):
        raise SharedLayoutError("invalid_stored_layout", "stored settings must be object")
    return parsed


def _save_soccer(
    db_path: Path, layout_key: str, normalized: dict[str, Any]
) -> dict[str, Any]:
    if not db_path.is_file():
        raise SharedLayoutError("db_missing", f"database missing: {db_path}")
    stamp = utc_now_iso()
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    with get_connection(db_path) as conn:
        conn.executescript(soccer_layout_settings_schema_sql())
        if not table_exists(conn, SOCCER_LAYOUT_SETTINGS_TABLE):
            raise SharedLayoutError(
                "table_missing", "soccer_layout_settings missing after init"
            )
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
    return deepcopy(normalized)


def _load_racing_raw(layout_key: str) -> dict[str, Any]:
    path = layout_settings_path()
    if not path.exists():
        raise SharedLayoutError("layout_missing", f"layout file missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SharedLayoutError(
            "invalid_stored_layout", f"cannot read layout file: {path}"
        ) from e
    if not isinstance(data, dict):
        raise SharedLayoutError("invalid_stored_layout", "layout file root must be object")
    block = data.get(layout_key)
    if not isinstance(block, dict):
        raise SharedLayoutError("layout_missing", f"no stored settings for {layout_key}")
    return block


def _save_racing(layout_key: str, normalized: dict[str, Any]) -> dict[str, Any]:
    path = layout_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
            else:
                raise SharedLayoutError(
                    "invalid_stored_layout", "layout file root must be object"
                )
        except (OSError, json.JSONDecodeError) as e:
            raise SharedLayoutError(
                "invalid_stored_layout", f"cannot read layout file: {path}"
            ) from e
    existing[layout_key] = {
        **normalized,
        "updated_at": utc_now_iso(),
    }
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return deepcopy(normalized)


def require_layout_settings(
    sport: str,
    layout_key: str,
    *,
    soccer_db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Shared layout read. Raises SharedLayoutError when missing or invalid.
    No default_layout_settings / no auto-repair write-back.
    """
    sport_n = normalize_sport(sport)
    key = normalize_layout_key(sport_n, layout_key)
    db_path = soccer_db_path
    if sport_n == SPORT_SOCCER and db_path is None:
        db_path = default_config().soccer_db_path
    if sport_n == SPORT_SOCCER:
        assert db_path is not None
        raw = _load_soccer_raw(db_path, key)
        return normalize_layout_settings(
            raw, sport=sport_n, layout_key=key, soccer_db_path=db_path
        )
    raw = _load_racing_raw(key)
    return normalize_layout_settings(
        raw, sport=sport_n, layout_key=key, soccer_db_path=db_path
    )


def save_layout_settings(
    sport: str,
    layout_key: str,
    settings: Any,
    *,
    soccer_db_path: Path | None = None,
) -> dict[str, Any]:
    """Shared layout save for any supported sport + layout_key."""
    sport_n = normalize_sport(sport)
    key = normalize_layout_key(sport_n, layout_key)
    db_path = soccer_db_path
    if sport_n == SPORT_SOCCER and db_path is None:
        db_path = default_config().soccer_db_path
    normalized = normalize_layout_settings(
        settings, sport=sport_n, layout_key=key, soccer_db_path=db_path
    )
    if sport_n == SPORT_SOCCER:
        assert db_path is not None
        return _save_soccer(db_path, key, normalized)
    return _save_racing(key, normalized)


def zebra_css_for_id(color_id: str) -> str:
    try:
        return soccer_zebra_css_for_id(color_id)
    except Exception as e:
        raise SharedLayoutError(
            "invalid_settings", f"unknown zebra_color_id: {color_id!r}"
        ) from e


__all__ = [
    "ALL_LAYOUT_KEYS",
    "LAYOUT_KEY_DAILY_MATCHES",
    "LAYOUT_KEY_MATCH_HISTORY",
    "LAYOUT_KEY_RACING_DAILY",
    "LAYOUT_KEY_RACING_HISTORY",
    "RACING_LAYOUT_KEYS",
    "SOCCER_LAYOUT_KEYS",
    "SORT_ASC",
    "SORT_DESC",
    "SORT_EMPTY",
    "SPORT_LAYOUT_KEYS",
    "SPORT_RACING",
    "SPORT_SOCCER",
    "SUPPORTED_SPORTS",
    "SharedLayoutError",
    "ZEBRA_COLORS",
    "catalog_for",
    "catalog_public",
    "normalize_layout_key",
    "normalize_layout_settings",
    "normalize_sport",
    "require_layout_settings",
    "save_layout_settings",
    "zebra_css_for_id",
]

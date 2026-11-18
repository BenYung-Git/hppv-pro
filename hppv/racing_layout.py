"""Racing history column layout preferences (file-backed; no DB schema change)."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hppv.config import default_config
from hppv.soccer_layout import ZEBRA_COLOR_IDS, ZEBRA_DEFAULT_ID

LAYOUT_KEY_RACING_HISTORY = "racing_history"
LAYOUT_KEY_RACING_DAILY = "racing_daily"

COLUMN_WIDTH_DEFAULT = 100
COLUMN_WIDTH_MIN = 60
COLUMN_WIDTH_MAX = 600

SORT_EMPTY = ""
SORT_ASC = "asc"
SORT_DESC = "desc"
ALLOWED_SORT = frozenset({SORT_EMPTY, SORT_ASC, SORT_DESC})

# All business columns from race_records (+ horses join). ALL default_visible=True.
RACING_HISTORY_COLUMN_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "race_date", "group": "basic", "default_visible": True, "default_width": 110, "default_sort": SORT_DESC, "sortable": True},
    {"key": "horse_id", "group": "horse", "default_visible": True, "default_width": 130, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "brand_no", "group": "horse", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "name_zh", "group": "horse", "default_visible": True, "default_width": 120, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "name_en", "group": "horse", "default_visible": True, "default_width": 140, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "venue", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "race_no", "group": "race", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "course", "group": "race", "default_visible": True, "default_width": 140, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "distance", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "going", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "class_band", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "draw", "group": "race", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "rating", "group": "race", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "jockey", "group": "people", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "trainer", "group": "people", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "finish_pos", "group": "result", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "running_pos", "group": "result", "default_visible": True, "default_width": 120, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "win_odds", "group": "result", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "time", "group": "result", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "prize", "group": "result", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": False},
    # Internal PK — shown by default per product rule; listed in delivery as technical.
    {"key": "id", "group": "meta", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
)

RACING_COLUMN_KEYS = frozenset(c["key"] for c in RACING_HISTORY_COLUMN_CATALOG)
RACING_COLUMN_BY_KEY = {c["key"]: c for c in RACING_HISTORY_COLUMN_CATALOG}

# Daily races: racecard_starters LEFT JOIN racecard_race_info. ALL default_visible=True.
RACING_DAILY_COLUMN_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "race_date", "group": "meeting", "default_visible": True, "default_width": 110, "default_sort": SORT_DESC, "sortable": True},
    {"key": "racecourse", "group": "meeting", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "racecourse_name", "group": "meeting", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "race_no", "group": "meeting", "default_visible": True, "default_width": 70, "default_sort": SORT_ASC, "sortable": True},
    {"key": "post_time", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "event_name", "group": "race", "default_visible": True, "default_width": 140, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "meeting_venue", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "calendar_date", "group": "race", "default_visible": True, "default_width": 120, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "weekday", "group": "race", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "race_course_area", "group": "race", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "track_name", "group": "race", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "distance", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "race_class", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "rating_range", "group": "race", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "prize_money", "group": "race", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "horse_no", "group": "horse", "default_visible": True, "default_width": 70, "default_sort": SORT_ASC, "sortable": True},
    {"key": "horse_name", "group": "horse", "default_visible": True, "default_width": 120, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "horse_id", "group": "horse", "default_visible": True, "default_width": 130, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "draw", "group": "horse", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "jockey", "group": "people", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "trainer", "group": "people", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "handicap", "group": "horse", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "rating", "group": "horse", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "horse_weight", "group": "horse", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "gear", "group": "horse", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "predicted_odds", "group": "odds", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "immediate_odds", "group": "odds", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "reserve", "group": "horse", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "id", "group": "meta", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
)

RACING_DAILY_COLUMN_KEYS = frozenset(c["key"] for c in RACING_DAILY_COLUMN_CATALOG)
RACING_DAILY_COLUMN_BY_KEY = {c["key"]: c for c in RACING_DAILY_COLUMN_CATALOG}

_LAYOUT_CATALOGS: dict[str, tuple[dict[str, Any], ...]] = {
    LAYOUT_KEY_RACING_HISTORY: RACING_HISTORY_COLUMN_CATALOG,
    LAYOUT_KEY_RACING_DAILY: RACING_DAILY_COLUMN_CATALOG,
}

# Text-searchable display keys (stable IDs).
RACING_TEXT_SEARCH_WHITELIST: frozenset[str] = frozenset(
    {
        "horse_id",
        "brand_no",
        "name_zh",
        "name_en",
        "venue",
        "race_no",
        "course",
        "distance",
        "going",
        "class_band",
        "draw",
        "rating",
        "jockey",
        "trainer",
        "finish_pos",
        "running_pos",
        "win_odds",
        "time",
        "prize",
    }
)

DEFAULT_RACING_SEARCH_KEYS: tuple[str, ...] = (
    "name_zh",
    "name_en",
    "horse_id",
    "brand_no",
    "jockey",
    "trainer",
)

RACING_DAILY_TEXT_SEARCH_WHITELIST: frozenset[str] = frozenset(
    {
        "race_date",
        "racecourse",
        "racecourse_name",
        "race_no",
        "reserve",
        "horse_id",
        "horse_no",
        "horse_name",
        "draw",
        "jockey",
        "trainer",
        "handicap",
        "rating",
        "horse_weight",
        "gear",
        "predicted_odds",
        "immediate_odds",
        "event_name",
        "calendar_date",
        "weekday",
        "meeting_venue",
        "post_time",
        "race_course_area",
        "track_name",
        "distance",
        "prize_money",
        "rating_range",
        "race_class",
    }
)

DEFAULT_RACING_DAILY_SEARCH_KEYS: tuple[str, ...] = (
    "horse_name",
    "horse_id",
    "horse_no",
    "jockey",
    "trainer",
    "event_name",
    "racecourse_name",
)


def catalog_for_layout_key(layout_key: str) -> tuple[dict[str, Any], ...]:
    return _LAYOUT_CATALOGS.get(layout_key, RACING_HISTORY_COLUMN_CATALOG)


def layout_settings_path() -> Path:
    return Path(default_config().data_dir) / "racing_layout_settings.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def catalog_public(
    catalog: tuple[dict[str, Any], ...] | None = None,
    *,
    layout_key: str | None = None,
) -> list[dict[str, Any]]:
    if catalog is None and layout_key:
        src = catalog_for_layout_key(layout_key)
    else:
        src = catalog or RACING_HISTORY_COLUMN_CATALOG
    out: list[dict[str, Any]] = []
    for c in src:
        out.append(
            {
                "key": c["key"],
                "group": c.get("group", ""),
                "default_visible": bool(c.get("default_visible", True)),
                "default_width": int(c.get("default_width") or COLUMN_WIDTH_DEFAULT),
                "default_sort": str(c.get("default_sort") or SORT_EMPTY),
                "sortable": bool(c.get("sortable", False)),
            }
        )
    return out


def default_layout_settings(layout_key: str = LAYOUT_KEY_RACING_HISTORY) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for c in catalog_for_layout_key(layout_key):
        columns.append(
            {
                "key": c["key"],
                "visible": bool(c.get("default_visible", True)),
                "width": int(c.get("default_width") or COLUMN_WIDTH_DEFAULT),
                "sort": str(c.get("default_sort") or SORT_EMPTY),
            }
        )
    return {
        "zebra_enabled": True,
        "zebra_color_id": ZEBRA_DEFAULT_ID,
        "columns": columns,
    }


def _clamp_width(raw: Any) -> int:
    try:
        w = int(raw)
    except (TypeError, ValueError):
        return COLUMN_WIDTH_DEFAULT
    return max(COLUMN_WIDTH_MIN, min(COLUMN_WIDTH_MAX, w))


def _normalize_saved(
    settings: dict[str, Any],
    *,
    layout_key: str = LAYOUT_KEY_RACING_HISTORY,
) -> dict[str, Any]:
    base = default_layout_settings(layout_key)
    by_key = {c["key"]: c for c in (settings.get("columns") or []) if isinstance(c, dict)}
    columns: list[dict[str, Any]] = []
    for def_col in base["columns"]:
        key = def_col["key"]
        saved = by_key.get(key) or {}
        sort = str(saved.get("sort") or def_col["sort"] or SORT_EMPTY)
        if sort not in ALLOWED_SORT:
            sort = SORT_EMPTY
        # Existing preference preserved; new catalog fields default visible.
        columns.append(
            {
                "key": key,
                "visible": bool(saved["visible"]) if "visible" in saved else bool(def_col["visible"]),
                "width": _clamp_width(saved.get("width", def_col["width"])),
                "sort": sort,
            }
        )
    # Preserve user column order when all keys known
    order_keys = [str(c.get("key")) for c in (settings.get("columns") or []) if isinstance(c, dict)]
    if order_keys:
        col_map = {c["key"]: c for c in columns}
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for k in order_keys:
            if k in col_map and k not in seen:
                ordered.append(col_map[k])
                seen.add(k)
        for c in columns:
            if c["key"] not in seen:
                ordered.append(c)
        columns = ordered
    zebra_color_id = str(settings.get("zebra_color_id") or ZEBRA_DEFAULT_ID)
    if zebra_color_id not in ZEBRA_COLOR_IDS:
        zebra_color_id = ZEBRA_DEFAULT_ID
    return {
        "zebra_enabled": bool(settings.get("zebra_enabled", True)),
        "zebra_color_id": zebra_color_id,
        "columns": columns,
    }


def get_layout_settings(layout_key: str = LAYOUT_KEY_RACING_HISTORY) -> dict[str, Any]:
    path = layout_settings_path()
    if not path.exists():
        return default_layout_settings(layout_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_layout_settings(layout_key)
    if not isinstance(data, dict):
        return default_layout_settings(layout_key)
    block = data.get(layout_key)
    if not isinstance(block, dict):
        return default_layout_settings(layout_key)
    return _normalize_saved(block, layout_key=layout_key)


def save_layout_settings(
    settings: dict[str, Any],
    *,
    layout_key: str = LAYOUT_KEY_RACING_HISTORY,
) -> dict[str, Any]:
    normalized = _normalize_saved(
        settings if isinstance(settings, dict) else {},
        layout_key=layout_key,
    )
    path = layout_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing[layout_key] = {
        **normalized,
        "updated_at": _utc_now(),
    }
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return deepcopy(normalized)


def racing_search_keys_from_layout(settings: dict[str, Any] | None) -> list[str]:
    if not settings:
        return list(DEFAULT_RACING_SEARCH_KEYS)
    keys: list[str] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        if not col.get("visible"):
            continue
        if key in RACING_TEXT_SEARCH_WHITELIST:
            keys.append(key)
    return keys or list(DEFAULT_RACING_SEARCH_KEYS)


def racing_daily_search_keys_from_layout(settings: dict[str, Any] | None) -> list[str]:
    if not settings:
        return list(DEFAULT_RACING_DAILY_SEARCH_KEYS)
    keys: list[str] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        if not col.get("visible"):
            continue
        if key in RACING_DAILY_TEXT_SEARCH_WHITELIST:
            keys.append(key)
    return keys or list(DEFAULT_RACING_DAILY_SEARCH_KEYS)


def racing_sort_specs_from_layout(settings: dict[str, Any] | None) -> list[tuple[str, str]]:
    if not settings:
        return [("race_date", SORT_DESC)]
    specs: list[tuple[str, str]] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        sort = str(col.get("sort") or SORT_EMPTY)
        meta = RACING_COLUMN_BY_KEY.get(key)
        if not meta or not meta.get("sortable"):
            continue
        if sort in (SORT_ASC, SORT_DESC):
            specs.append((key, sort))
    return specs or [("race_date", SORT_DESC)]


def racing_daily_sort_specs_from_layout(
    settings: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if not settings:
        return [("race_date", SORT_DESC), ("race_no", SORT_ASC), ("horse_no", SORT_ASC)]
    specs: list[tuple[str, str]] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        sort = str(col.get("sort") or SORT_EMPTY)
        meta = RACING_DAILY_COLUMN_BY_KEY.get(key)
        if not meta or not meta.get("sortable"):
            continue
        if sort in (SORT_ASC, SORT_DESC):
            specs.append((key, sort))
    return specs or [("race_date", SORT_DESC), ("race_no", SORT_ASC), ("horse_no", SORT_ASC)]


def racing_sort_param(specs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{d}" for k, d in specs)

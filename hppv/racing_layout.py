"""Racing history column layout preferences (file-backed; no DB schema change)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hppv.config import default_config

LAYOUT_KEY_RACING_HISTORY = "racing_history"
LAYOUT_KEY_RACING_DAILY = "racing_daily"
LAYOUT_KEY_RACING_STANDARD_TIMES = "racing_standard_times"
LAYOUT_KEY_RACING_DRAW_STATISTICS = "racing_draw_statistics"

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
    {"key": "handicap", "group": "race", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
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
    {"key": "finish_pos", "group": "result", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
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
    {"key": "odds_diff", "group": "odds", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "weighted_win_rate", "group": "odds", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "smoothed_weighted_rate", "group": "odds", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "same_distance_win_rate", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "same_distance_place_rate", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "draw_win_rate", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "draw_place_rate", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "attainment_rate", "group": "odds", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "reserve", "group": "horse", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "id", "group": "meta", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
)

RACING_DAILY_COLUMN_KEYS = frozenset(c["key"] for c in RACING_DAILY_COLUMN_CATALOG)
RACING_DAILY_COLUMN_BY_KEY = {c["key"]: c for c in RACING_DAILY_COLUMN_CATALOG}

RACING_STANDARD_TIMES_COLUMN_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "published_on", "group": "meta", "default_visible": True, "default_width": 120, "default_sort": SORT_DESC, "sortable": True},
    {"key": "source_year", "group": "meta", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "venue_code", "group": "course", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "track_type", "group": "course", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "distance_m", "group": "course", "default_visible": True, "default_width": 90, "default_sort": SORT_ASC, "sortable": True},
    {"key": "class_band", "group": "course", "default_visible": True, "default_width": 100, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "standard_time", "group": "time", "default_visible": True, "default_width": 110, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "sec_1", "group": "sectional", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "sec_2", "group": "sectional", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "sec_3", "group": "sectional", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "sec_4", "group": "sectional", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "sec_5", "group": "sectional", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "sec_6", "group": "sectional", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "id", "group": "meta", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
)

RACING_STANDARD_TIMES_COLUMN_KEYS = frozenset(
    c["key"] for c in RACING_STANDARD_TIMES_COLUMN_CATALOG
)
RACING_STANDARD_TIMES_COLUMN_BY_KEY = {
    c["key"]: c for c in RACING_STANDARD_TIMES_COLUMN_CATALOG
}

RACING_DRAW_STATISTICS_COLUMN_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "venue_code", "group": "course", "default_visible": True, "default_width": 80, "default_sort": SORT_ASC, "sortable": True},
    {"key": "track_type", "group": "course", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "rail_position", "group": "course", "default_visible": True, "default_width": 100, "default_sort": SORT_ASC, "sortable": True},
    {"key": "distance_m", "group": "course", "default_visible": True, "default_width": 90, "default_sort": SORT_ASC, "sortable": True},
    {"key": "draw_no", "group": "course", "default_visible": True, "default_width": 70, "default_sort": SORT_ASC, "sortable": True},
    {"key": "starts", "group": "count", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "wins", "group": "count", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "seconds", "group": "count", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "thirds", "group": "count", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "fourths", "group": "count", "default_visible": True, "default_width": 70, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "win_pct", "group": "rate", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "quinella_pct", "group": "rate", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "place_pct", "group": "rate", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "top4_pct", "group": "rate", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "win_count", "group": "derived", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "in_place_count", "group": "derived", "default_visible": True, "default_width": 90, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "lose_count", "group": "derived", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "source", "group": "meta", "default_visible": False, "default_width": 140, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "source_url", "group": "meta", "default_visible": False, "default_width": 200, "default_sort": SORT_EMPTY, "sortable": False},
    {"key": "fetched_at", "group": "meta", "default_visible": False, "default_width": 160, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "updated_at", "group": "meta", "default_visible": False, "default_width": 160, "default_sort": SORT_EMPTY, "sortable": True},
    {"key": "id", "group": "meta", "default_visible": True, "default_width": 80, "default_sort": SORT_EMPTY, "sortable": True},
)

RACING_DRAW_STATISTICS_COLUMN_KEYS = frozenset(
    c["key"] for c in RACING_DRAW_STATISTICS_COLUMN_CATALOG
)
RACING_DRAW_STATISTICS_COLUMN_BY_KEY = {
    c["key"]: c for c in RACING_DRAW_STATISTICS_COLUMN_CATALOG
}

_LAYOUT_CATALOGS: dict[str, tuple[dict[str, Any], ...]] = {
    LAYOUT_KEY_RACING_HISTORY: RACING_HISTORY_COLUMN_CATALOG,
    LAYOUT_KEY_RACING_DAILY: RACING_DAILY_COLUMN_CATALOG,
    LAYOUT_KEY_RACING_STANDARD_TIMES: RACING_STANDARD_TIMES_COLUMN_CATALOG,
    LAYOUT_KEY_RACING_DRAW_STATISTICS: RACING_DRAW_STATISTICS_COLUMN_CATALOG,
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
        "handicap",
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
        "finish_pos",
        "draw",
        "jockey",
        "trainer",
        "handicap",
        "rating",
        "horse_weight",
        "gear",
        "predicted_odds",
        "immediate_odds",
        "odds_diff",
        "weighted_win_rate",
        "smoothed_weighted_rate",
        "same_distance_win_rate",
        "same_distance_place_rate",
        "draw_win_rate",
        "draw_place_rate",
        "attainment_rate",
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

RACING_STANDARD_TIMES_TEXT_SEARCH_WHITELIST: frozenset[str] = frozenset(
    {
        "published_on",
        "source_year",
        "venue_code",
        "track_type",
        "distance_m",
        "class_band",
        "standard_time",
        "sec_1",
        "sec_2",
        "sec_3",
        "sec_4",
        "sec_5",
        "sec_6",
    }
)

DEFAULT_STANDARD_TIMES_SEARCH_KEYS: tuple[str, ...] = (
    "venue_code",
    "track_type",
    "distance_m",
    "class_band",
    "standard_time",
    "published_on",
)

RACING_DRAW_STATISTICS_TEXT_SEARCH_WHITELIST: frozenset[str] = frozenset(
    {
        "venue_code",
        "track_type",
        "rail_position",
        "distance_m",
        "draw_no",
        "starts",
        "wins",
        "seconds",
        "thirds",
        "fourths",
        "win_pct",
        "quinella_pct",
        "place_pct",
        "top4_pct",
        "win_count",
        "in_place_count",
        "lose_count",
    }
)

DEFAULT_DRAW_STATISTICS_SEARCH_KEYS: tuple[str, ...] = (
    "venue_code",
    "track_type",
    "rail_position",
    "distance_m",
    "draw_no",
)


def catalog_for_layout_key(layout_key: str) -> tuple[dict[str, Any], ...]:
    try:
        return _LAYOUT_CATALOGS[layout_key]
    except KeyError as e:
        raise ValueError(f"unsupported racing layout_key: {layout_key!r}") from e


def layout_settings_path() -> Path:
    return Path(default_config().data_dir) / "racing_layout_settings.json"


def catalog_public(
    catalog: tuple[dict[str, Any], ...] | None = None,
    *,
    layout_key: str | None = None,
) -> list[dict[str, Any]]:
    if catalog is not None:
        src = catalog
    elif layout_key:
        src = catalog_for_layout_key(layout_key)
    else:
        raise ValueError("layout_key or catalog required")
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
    """Catalog-built seed settings (not yet persisted). Prefer require_layout_settings at runtime."""
    from hppv.shared_layout import SPORT_RACING, layout_settings_from_catalog

    return layout_settings_from_catalog(SPORT_RACING, layout_key)


def get_layout_settings(layout_key: str = LAYOUT_KEY_RACING_HISTORY) -> dict[str, Any]:
    """Racing-only wrapper. Prefer hppv.shared_layout.require_layout_settings."""
    from hppv.shared_layout import SPORT_RACING, require_layout_settings

    return require_layout_settings(SPORT_RACING, layout_key)


def save_layout_settings(
    settings: dict[str, Any],
    *,
    layout_key: str = LAYOUT_KEY_RACING_HISTORY,
) -> dict[str, Any]:
    """Racing-only wrapper. Prefer hppv.shared_layout.save_layout_settings."""
    from hppv.shared_layout import SPORT_RACING, save_layout_settings as shared_save

    return shared_save(SPORT_RACING, layout_key, settings)


def racing_search_keys_from_layout(settings: dict[str, Any] | None) -> list[str]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
    keys: list[str] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        if not col.get("visible"):
            continue
        if key in RACING_TEXT_SEARCH_WHITELIST:
            keys.append(key)
    return keys


def racing_daily_search_keys_from_layout(settings: dict[str, Any] | None) -> list[str]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
    keys: list[str] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        if not col.get("visible"):
            continue
        if key in RACING_DAILY_TEXT_SEARCH_WHITELIST:
            keys.append(key)
    return keys


def racing_sort_specs_from_layout(settings: dict[str, Any] | None) -> list[tuple[str, str]]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
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
    return specs


def racing_daily_sort_specs_from_layout(
    settings: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
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
    return specs


def racing_sort_param(specs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{d}" for k, d in specs)


def racing_standard_times_search_keys_from_layout(
    settings: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
    keys: list[str] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        if not col.get("visible"):
            continue
        if key in RACING_STANDARD_TIMES_TEXT_SEARCH_WHITELIST:
            keys.append(key)
    return keys


def racing_standard_times_sort_specs_from_layout(
    settings: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
    specs: list[tuple[str, str]] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        sort = str(col.get("sort") or SORT_EMPTY)
        meta = RACING_STANDARD_TIMES_COLUMN_BY_KEY.get(key)
        if not meta or not meta.get("sortable"):
            continue
        if sort in (SORT_ASC, SORT_DESC):
            specs.append((key, sort))
    return specs


def racing_draw_statistics_search_keys_from_layout(
    settings: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
    keys: list[str] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        if not col.get("visible"):
            continue
        if key in RACING_DRAW_STATISTICS_TEXT_SEARCH_WHITELIST:
            keys.append(key)
    return keys


def racing_draw_statistics_sort_specs_from_layout(
    settings: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if not isinstance(settings, dict):
        raise ValueError("layout_settings required")
    specs: list[tuple[str, str]] = []
    for col in settings.get("columns") or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "")
        sort = str(col.get("sort") or SORT_EMPTY)
        meta = RACING_DRAW_STATISTICS_COLUMN_BY_KEY.get(key)
        if not meta or not meta.get("sortable"):
            continue
        if sort in (SORT_ASC, SORT_DESC):
            specs.append((key, sort))
    return specs

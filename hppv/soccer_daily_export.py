"""Daily-matches export builders (XLSX / CSV / JPEG / PDF).

Uses soccer_daily via fetch_matches_filtered — never footylogic / history.
Reuses shared format builders from soccer_history_export.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from hppv.soccer_history_export import (
    EXPORT_FILE_TYPES,
    ExportColumn,
    HistoryExportError,
    HistoryExportResult,
    build_export_from_rows,
    resolve_export_columns,
)
from hppv.soccer_layout import (
    LAYOUT_KEY_DAILY_MATCHES,
    get_layout_settings,
    default_layout_settings,
)
from hppv.soccer_matches import (
    DEFAULT_STATUS_FILTER,
    EMPTY_DISPLAY,
    SoccerDataError,
    fetch_matches_filtered,
    normalize_status_filter,
    parse_sort_param,
    search_keys_from_layout,
    sort_specs_from_layout,
)
from hppv.translations import daily_column_i18n_maps, load_active_text_map, normalize_lang


@dataclass(frozen=True, slots=True)
class DailyExportRequest:
    file_type: str
    q: str
    status: str
    all_matches: bool
    match_date: date | None
    sort_param: str
    columns: tuple[ExportColumn, ...]
    lang: str


def default_daily_export_basename(*, q: str, match_date: date | None) -> str:
    """Basename identifying daily matches and including the selected date."""
    date_part = match_date.strftime("%Y%m%d") if match_date is not None else "All"
    stem = (q or "").strip()
    if stem:
        return f"{stem}-DailyMatches-{date_part}"
    return f"DailyMatches-{date_part}"


def normalize_daily_export_row(row: dict[str, str]) -> dict[str, str]:
    """Align decorative / pending columns with daily UI display rules."""
    out = dict(row)
    # Icon is an SVG in the UI; tabular export keeps a blank cell.
    if not str(out.get("icon") or "").strip():
        out["icon"] = ""
    out["ui_et"] = EMPTY_DISPLAY
    return out


def parse_daily_export_request(
    raw: dict[str, Any],
    *,
    catalog_keys: Sequence[str],
    db_path: Path,
) -> DailyExportRequest:
    file_type = str(raw.get("file_type") or "").strip().lower()
    if file_type not in EXPORT_FILE_TYPES:
        raise HistoryExportError("invalid_file_type", file_type)
    q = str(raw.get("q") or "").strip()
    status = normalize_status_filter(raw.get("status") or DEFAULT_STATUS_FILTER)
    all_matches = bool(raw.get("all_matches", True))
    match_date: date | None = None
    if not all_matches:
        date_raw = str(raw.get("date") or "").strip()
        try:
            match_date = date.fromisoformat(date_raw)
        except ValueError as e:
            raise HistoryExportError("invalid_date", date_raw) from e
    else:
        # Keep selected chrome date for naming; list filter ignores it.
        date_raw = str(raw.get("date") or "").strip()
        if date_raw:
            try:
                match_date = date.fromisoformat(date_raw)
            except ValueError:
                match_date = None
    sort_param = str(raw.get("sort") or "").strip()
    # Empty → layout default (same as daily list). Non-empty with no valid tokens → [].
    if sort_param:
        specs = parse_sort_param(sort_param)
        if specs is None:
            raise HistoryExportError("invalid_sort", sort_param)
    lang = normalize_lang(str(raw.get("lang") or ""))
    cols_raw = raw.get("columns")
    if not isinstance(cols_raw, list):
        raise HistoryExportError("invalid_columns")
    try:
        text_map = load_active_text_map(db_path, lang)
    except Exception:
        text_map = {}
    label_map, _descs = daily_column_i18n_maps(text_map, tuple(str(k) for k in catalog_keys))
    columns = resolve_export_columns(
        columns_raw=cols_raw,
        catalog_keys=catalog_keys,
        labels=label_map,
    )
    return DailyExportRequest(
        file_type=file_type,
        q=q,
        status=status,
        all_matches=all_matches,
        match_date=match_date,
        sort_param=sort_param,
        columns=columns,
        lang=lang,
    )


def fetch_daily_export_rows(
    db_path: Path,
    req: DailyExportRequest,
    *,
    layout_settings: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Load ALL filtered soccer_daily rows (same path as daily list API)."""
    layout = layout_settings
    if layout is None:
        try:
            layout = get_layout_settings(db_path, LAYOUT_KEY_DAILY_MATCHES)
        except Exception:
            layout = default_layout_settings(LAYOUT_KEY_DAILY_MATCHES)

    parsed = parse_sort_param(req.sort_param) if req.sort_param else None
    sort_specs = parsed if parsed is not None else sort_specs_from_layout(layout)
    search_keys = search_keys_from_layout(layout)

    # When all_matches, ignore date in SQL (match list API contract).
    query_date = None if req.all_matches else req.match_date
    try:
        result = fetch_matches_filtered(
            db_path,
            query_date,
            n=1,
            q=req.q,
            status=req.status,
            sort_specs=sort_specs,
            search_keys=search_keys,
        )
    except SoccerDataError as e:
        raise HistoryExportError("read_error", str(e)) from e
    return [normalize_daily_export_row(m) for m in result.matches]


def build_daily_export(
    db_path: Path,
    req: DailyExportRequest,
    *,
    layout_settings: dict[str, Any] | None = None,
) -> HistoryExportResult:
    rows = fetch_daily_export_rows(db_path, req, layout_settings=layout_settings)
    return build_export_from_rows(
        req.file_type,
        req.columns,
        rows,
        sheet_name="daily",
        pdf_title="daily-matches-export",
    )


__all__ = [
    "DailyExportRequest",
    "build_daily_export",
    "default_daily_export_basename",
    "fetch_daily_export_rows",
    "normalize_daily_export_row",
    "parse_daily_export_request",
]

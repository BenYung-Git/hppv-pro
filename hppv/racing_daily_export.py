"""Racing daily export from racecard_starters (+ race_info) for one selected race."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from hppv.config import default_config
from hppv.racing_daily import (
    fetch_racing_daily_filtered,
    fetch_racing_daily_race,
    iso_to_racecard_date,
    parse_racing_daily_sort_param,
)
from hppv.racing_history import DEFAULT_HISTORY_SEARCH_OP, normalize_history_search_op
from hppv.racing_history_export import (
    COLUMN_WIDTH_DEFAULT,
    COLUMN_WIDTH_MAX,
    COLUMN_WIDTH_MIN,
    EXPORT_FILE_TYPES,
    ExportColumn,
    RacingExportError,
    RacingExportRequest,
    _build_csv,
    _build_jpeg,
    _build_pdf,
    _build_xlsx,
    _matrix,
    validate_export_filename,
)
from hppv.racing_layout import (
    LAYOUT_KEY_RACING_DAILY,
    racing_daily_search_keys_from_layout,
)
from hppv.translations import load_active_text_map, normalize_lang


def racing_daily_column_i18n_maps(
    text_map: Mapping[str, str], keys: Sequence[str]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in keys:
        label = text_map.get(f"racing.daily.col.{key}")
        out[key] = label if label else key
    return out


def parse_racing_daily_export_request(payload: Mapping[str, Any]) -> RacingExportRequest:
    file_type = str(payload.get("file_type") or "").strip().lower()
    if file_type not in EXPORT_FILE_TYPES:
        raise RacingExportError("unsupported_type")
    filename = validate_export_filename(str(payload.get("filename") or ""), file_type)
    cols_raw = payload.get("columns")
    if not isinstance(cols_raw, list) or not cols_raw:
        raise RacingExportError("invalid_columns")
    lang = normalize_lang(str(payload.get("lang") or "zh-Hant"))
    text_map = load_active_text_map(default_config().soccer_db_path, lang)
    labels = racing_daily_column_i18n_maps(
        text_map, [str(c.get("key")) for c in cols_raw if isinstance(c, dict)]
    )
    columns: list[ExportColumn] = []
    for c in cols_raw:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or "").strip()
        if not key or key == "horse_history_icon":
            continue
        try:
            width = int(c.get("width") or COLUMN_WIDTH_DEFAULT)
        except (TypeError, ValueError):
            width = COLUMN_WIDTH_DEFAULT
        width = max(COLUMN_WIDTH_MIN, min(COLUMN_WIDTH_MAX, width))
        columns.append(ExportColumn(key=key, width=width, label=labels.get(key, key)))
    if not columns:
        raise RacingExportError("invalid_columns")

    race_date = str(payload.get("date") or "").strip() or None
    racecourse = str(payload.get("racecourse") or "").strip().upper() or None
    race_no_raw = payload.get("race_no")
    race_no: int | None = None
    if race_no_raw not in (None, ""):
        try:
            race_no = int(race_no_raw)
        except (TypeError, ValueError) as e:
            raise RacingExportError("invalid_race_no") from e
    if not race_date or not racecourse or race_no is None:
        raise RacingExportError("no_selected_race")

    return RacingExportRequest(
        file_type=file_type,
        filename=filename,
        q="",
        q_op=normalize_history_search_op(
            str(payload.get("q_op") or DEFAULT_HISTORY_SEARCH_OP)
        ),
        all_matches=False,
        race_date=race_date,
        sort=str(payload.get("sort") or ""),
        columns=tuple(columns),
        lang=lang,
        racecourse=racecourse,
        race_no=race_no,
    )


def _iter_rows(db_path: Path, req: RacingExportRequest) -> list[dict[str, str]]:
    from hppv.racing_daily import RacingDailyError, RacingDailySearchError

    sort_specs = parse_racing_daily_sort_param(req.sort)
    try:
        view = fetch_racing_daily_race(
            db_path,
            race_no=req.race_no,
            race_date=req.race_date,
            racecourse=req.racecourse,
            sort_specs=sort_specs,
        )
    except RacingDailySearchError as e:
        raise RacingExportError(e.code, e.detail) from e
    except RacingDailyError as e:
        raise RacingExportError(e.code, e.detail) from e
    if view.total_count <= 0:
        raise RacingExportError("no_rows")
    # Ensure export stays on the requested race identity.
    if (
        iso_to_racecard_date(req.race_date) != view.meeting.race_date
        or str(req.racecourse or "").upper() != view.meeting.racecourse.upper()
        or int(req.race_no or -1) != view.selected_race_no
    ):
        raise RacingExportError("race_mismatch")
    return list(view.rows)


def build_racing_daily_export(
    db_path: Path, req: RacingExportRequest
) -> tuple[bytes, str, int]:
    rows = _iter_rows(db_path, req)
    matrix = _matrix(rows, req.columns)
    if req.file_type == "xlsx":
        data = _build_xlsx(matrix)
    elif req.file_type == "csv":
        data = _build_csv(matrix)
    elif req.file_type == "pdf":
        data = _build_pdf(matrix, req.columns)
    elif req.file_type == "jpeg":
        data = _build_jpeg(matrix, req.columns)
    else:
        raise RacingExportError("unsupported_type")
    from hppv.racing_history_export import _MIME_BY_TYPE

    return data, _MIME_BY_TYPE[req.file_type], len(rows)


# Keep legacy helpers importable for tests that still call filtered list.
_ = (LAYOUT_KEY_RACING_DAILY, fetch_racing_daily_filtered, racing_daily_search_keys_from_layout)

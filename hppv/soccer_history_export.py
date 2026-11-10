"""Match-history export builders (XLSX / CSV / JPEG / PDF).

Reuses soccer_history query / sort / row formatting. Does not touch soccer_daily.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from hppv.soccer_history import (
    DEFAULT_HISTORY_SEARCH_OP,
    HISTORY_PAGE_SIZE,
    SoccerDataError,
    fetch_history_filtered,
    normalize_history_search_op,
    parse_history_sort_param,
)
from hppv.soccer_layout import COLUMN_WIDTH_DEFAULT, COLUMN_WIDTH_MAX, COLUMN_WIDTH_MIN
from hppv.translations import history_column_i18n_maps, load_active_text_map, normalize_lang

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")

EXPORT_FILE_TYPES = frozenset({"xlsx", "csv", "jpeg", "pdf"})
EXPORT_BATCH_SIZE = 500
# JPEG feasibility: Pillow practical limits (single image, no tiling).
JPEG_MAX_WIDTH_PX = 12000
JPEG_MAX_HEIGHT_PX = 12000
JPEG_MAX_PIXELS = 40_000_000
JPEG_ROW_HEIGHT_PX = 22
JPEG_HEADER_HEIGHT_PX = 28
JPEG_PAD_PX = 8
# PDF: A4 landscape, min readable font.
PDF_MIN_FONT_PT = 8.0
PDF_MARGIN_MM = 10.0
PDF_FONT_NAME = "STSong-Light"

_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXT_BY_TYPE = {
    "xlsx": ".xlsx",
    "csv": ".csv",
    "jpeg": ".jpeg",
    "pdf": ".pdf",
}
_MIME_BY_TYPE = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "jpeg": "image/jpeg",
    "pdf": "application/pdf",
}

_NOTO_SC_FONT = (
    Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.otf"
)


class HistoryExportError(RuntimeError):
    """User-facing export failure with stable error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or ""


@dataclass(frozen=True, slots=True)
class ExportColumn:
    key: str
    width: int
    label: str


@dataclass(frozen=True, slots=True)
class HistoryExportRequest:
    file_type: str
    q: str
    q_op: str
    all_matches: bool
    match_date: date | None
    sort_param: str
    columns: tuple[ExportColumn, ...]
    lang: str


@dataclass(frozen=True, slots=True)
class HistoryExportResult:
    content: bytes
    mime: str
    row_count: int
    file_type: str


def hong_kong_today_ymd() -> str:
    return datetime.now(HKT).date().strftime("%Y%m%d")


def default_export_basename(q: str) -> str:
    stem = (q or "").strip() or "All"
    return f"{stem}-{hong_kong_today_ymd()}"


def extension_for_type(file_type: str) -> str:
    if file_type not in _EXT_BY_TYPE:
        raise HistoryExportError("invalid_file_type", file_type)
    return _EXT_BY_TYPE[file_type]


def validate_export_filename(name: str, file_type: str) -> str:
    text = (name or "").strip()
    if not text:
        raise HistoryExportError("filename_blank")
    if _ILLEGAL_FILENAME_RE.search(text):
        raise HistoryExportError("filename_illegal")
    ext = extension_for_type(file_type)
    lower = text.lower()
    # Strip any known export extension once, then append the selected type.
    for other in _EXT_BY_TYPE.values():
        if lower.endswith(other):
            text = text[: -len(other)]
            break
    text = text.rstrip(" .")
    if not text:
        raise HistoryExportError("filename_blank")
    if _ILLEGAL_FILENAME_RE.search(text):
        raise HistoryExportError("filename_illegal")
    return text + ext


def ascii_fallback_filename(filename: str, file_type: str | None = None) -> str:
    """Latin-1-safe fallback for HTTP Content-Disposition filename=."""
    text = str(filename or "").strip() or "export"
    ext = ""
    lower = text.lower()
    for known in _EXT_BY_TYPE.values():
        if lower.endswith(known):
            ext = known
            text = text[: -len(known)]
            break
    if not ext and file_type in _EXT_BY_TYPE:
        ext = _EXT_BY_TYPE[str(file_type)]
    # Keep only portable ASCII filename characters; empty stem → "export".
    safe = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "-_.")) else "_" for ch in text
    )
    safe = re.sub(r"_+", "_", safe).strip("._-")
    # Require at least one ASCII letter; pure digits/punctuation → "export".
    if not safe or not any(ch.isalpha() for ch in safe):
        safe = "export"
    return safe + (ext or "")


def export_content_disposition(filename: str, file_type: str | None = None) -> str:
    """RFC 6266/5987 Content-Disposition that stays latin-1 safe for Werkzeug."""
    fallback = ascii_fallback_filename(filename, file_type=file_type)
    encoded = quote(str(filename or fallback), safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def header_safe_export_filename(filename: str) -> str:
    """Percent-encode Unicode filenames for custom response headers."""
    return quote(str(filename or ""), safe="")


def _ensure_pdf_font() -> str:
    if PDF_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT_NAME))
    return PDF_FONT_NAME


def _noto_font_path() -> Path:
    if not _NOTO_SC_FONT.is_file():
        raise HistoryExportError(
            "font_missing",
            f"missing deployable font: {_NOTO_SC_FONT}",
        )
    return _NOTO_SC_FONT


def resolve_export_columns(
    *,
    columns_raw: Sequence[dict[str, Any]],
    catalog_keys: Iterable[str],
    text_map: dict[str, str],
) -> tuple[ExportColumn, ...]:
    allowed = frozenset(str(k) for k in catalog_keys)
    labels, _descs = history_column_i18n_maps(text_map, tuple(allowed))
    out: list[ExportColumn] = []
    seen: set[str] = set()
    for item in columns_raw:
        if not isinstance(item, dict):
            raise HistoryExportError("invalid_columns")
        key = str(item.get("key") or "").strip()
        if not key:
            raise HistoryExportError("invalid_columns")
        if key not in allowed:
            raise HistoryExportError("unknown_column", key)
        if key in seen:
            raise HistoryExportError("duplicate_column", key)
        seen.add(key)
        width_raw = item.get("width", COLUMN_WIDTH_DEFAULT)
        try:
            width = int(width_raw)
        except (TypeError, ValueError) as e:
            raise HistoryExportError("invalid_column_width", key) from e
        if width < COLUMN_WIDTH_MIN or width > COLUMN_WIDTH_MAX:
            raise HistoryExportError("invalid_column_width", key)
        out.append(ExportColumn(key=key, width=width, label=labels.get(key) or key))
    if not out:
        raise HistoryExportError("no_visible_columns")
    return tuple(out)


def parse_history_export_request(
    raw: dict[str, Any],
    *,
    catalog_keys: Sequence[str],
    db_path: Path,
) -> HistoryExportRequest:
    file_type = str(raw.get("file_type") or "").strip().lower()
    if file_type not in EXPORT_FILE_TYPES:
        raise HistoryExportError("invalid_file_type", file_type)
    q = str(raw.get("q") or "").strip()
    q_op = normalize_history_search_op(raw.get("q_op") or DEFAULT_HISTORY_SEARCH_OP)
    all_matches = bool(raw.get("all_matches", True))
    match_date: date | None = None
    if not all_matches:
        date_raw = str(raw.get("date") or "").strip()
        try:
            match_date = date.fromisoformat(date_raw)
        except ValueError as e:
            raise HistoryExportError("invalid_date", date_raw) from e
    sort_param = str(raw.get("sort") or "").strip()
    specs = parse_history_sort_param(sort_param)
    if sort_param and specs is None:
        raise HistoryExportError("invalid_sort", sort_param)
    lang = normalize_lang(str(raw.get("lang") or ""))
    cols_raw = raw.get("columns")
    if not isinstance(cols_raw, list):
        raise HistoryExportError("invalid_columns")
    try:
        text_map = load_active_text_map(db_path, lang)
    except Exception:
        text_map = {}
    columns = resolve_export_columns(
        columns_raw=cols_raw,
        catalog_keys=catalog_keys,
        text_map=text_map,
    )
    return HistoryExportRequest(
        file_type=file_type,
        q=q,
        q_op=q_op,
        all_matches=all_matches,
        match_date=match_date,
        sort_param=sort_param,
        columns=columns,
        lang=lang,
    )


def iter_history_export_rows(
    db_path: Path,
    req: HistoryExportRequest,
    *,
    batch_size: int = EXPORT_BATCH_SIZE,
) -> Iterator[list[dict[str, str]]]:
    """Yield formatted row batches using the same list query path."""
    specs = parse_history_sort_param(req.sort_param)
    page = 1
    total_pages: int | None = None
    while total_pages is None or page <= total_pages:
        try:
            result = fetch_history_filtered(
                db_path,
                None if req.all_matches else req.match_date,
                n=page,
                q=req.q,
                q_op=req.q_op,
                sort_specs=specs,
                page_size=max(1, int(batch_size or HISTORY_PAGE_SIZE)),
            )
        except SoccerDataError as e:
            raise HistoryExportError("read_error", str(e)) from e
        total_pages = int(result.total_pages or 0)
        if total_pages <= 0:
            return
        yield list(result.matches)
        page += 1


def count_history_export_rows(db_path: Path, req: HistoryExportRequest) -> int:
    specs = parse_history_sort_param(req.sort_param)
    try:
        result = fetch_history_filtered(
            db_path,
            None if req.all_matches else req.match_date,
            n=1,
            q=req.q,
            q_op=req.q_op,
            sort_specs=specs,
            page_size=1,
        )
    except SoccerDataError as e:
        raise HistoryExportError("read_error", str(e)) from e
    return int(result.total_count or 0)


def _project_row(row: dict[str, str], columns: Sequence[ExportColumn]) -> list[str]:
    out: list[str] = []
    for col in columns:
        val = row.get(col.key)
        out.append("" if val is None else str(val))
    return out


def _csv_cell(value: str) -> str:
    """Neutralize formula injection; csv module handles quoting."""
    text = value if value is not None else ""
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def build_csv_export(
    db_path: Path,
    req: HistoryExportRequest,
) -> HistoryExportResult:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([c.label for c in req.columns])
    row_count = 0
    for batch in iter_history_export_rows(db_path, req):
        for row in batch:
            cells = [_csv_cell(v) for v in _project_row(row, req.columns)]
            writer.writerow(cells)
            row_count += 1
    # UTF-8 BOM for Excel-friendly Chinese CSV
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return HistoryExportResult(
        content=data,
        mime=_MIME_BY_TYPE["csv"],
        row_count=row_count,
        file_type="csv",
    )


def build_xlsx_export(
    db_path: Path,
    req: HistoryExportRequest,
) -> HistoryExportResult:
    headers = [c.label for c in req.columns]
    rows: list[list[str]] = []
    for batch in iter_history_export_rows(db_path, req):
        for row in batch:
            rows.append(_project_row(row, req.columns))
    df = pd.DataFrame(rows, columns=headers)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="history")
        worksheet = writer.sheets["history"]
        # xlsxwriter width unit ≈ character width; map UI px → roughly px/7.
        for idx, col in enumerate(req.columns):
            worksheet.set_column(idx, idx, max(8, round(col.width / 7.0, 2)))
    return HistoryExportResult(
        content=output.getvalue(),
        mime=_MIME_BY_TYPE["xlsx"],
        row_count=len(rows),
        file_type="xlsx",
    )


def _estimate_jpeg_size(req: HistoryExportRequest, row_count: int) -> tuple[int, int]:
    width = JPEG_PAD_PX * 2 + sum(max(40, c.width) for c in req.columns)
    height = (
        JPEG_PAD_PX * 2
        + JPEG_HEADER_HEIGHT_PX
        + max(0, row_count) * JPEG_ROW_HEIGHT_PX
    )
    return int(width), int(height)


def build_jpeg_export(
    db_path: Path,
    req: HistoryExportRequest,
) -> HistoryExportResult:
    from PIL import Image, ImageDraw, ImageFont

    row_count = count_history_export_rows(db_path, req)
    width, height = _estimate_jpeg_size(req, row_count)
    pixels = width * height
    if (
        width > JPEG_MAX_WIDTH_PX
        or height > JPEG_MAX_HEIGHT_PX
        or pixels > JPEG_MAX_PIXELS
    ):
        raise HistoryExportError("jpeg_too_large")

    font_path = _noto_font_path()
    font = ImageFont.truetype(str(font_path), 14)
    header_font = ImageFont.truetype(str(font_path), 14)

    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    x0 = JPEG_PAD_PX
    y = JPEG_PAD_PX

    # Header background
    draw.rectangle(
        [0, y, width, y + JPEG_HEADER_HEIGHT_PX],
        fill=(232, 246, 238),
    )
    x = x0
    for col in req.columns:
        draw.text((x + 4, y + 5), col.label, fill=(27, 61, 102), font=header_font)
        x += max(40, col.width)
    y += JPEG_HEADER_HEIGHT_PX
    draw.line([(0, y), (width, y)], fill=(200, 210, 220), width=1)

    written = 0
    for batch in iter_history_export_rows(db_path, req):
        for row in batch:
            if written >= row_count:
                break
            x = x0
            cells = _project_row(row, req.columns)
            if written % 2 == 1:
                draw.rectangle(
                    [0, y, width, y + JPEG_ROW_HEIGHT_PX],
                    fill=(244, 246, 249),
                )
            for col, cell in zip(req.columns, cells, strict=True):
                draw.text((x + 4, y + 3), cell, fill=(30, 40, 50), font=font)
                x += max(40, col.width)
            y += JPEG_ROW_HEIGHT_PX
            written += 1

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90, optimize=True)
    return HistoryExportResult(
        content=out.getvalue(),
        mime=_MIME_BY_TYPE["jpeg"],
        row_count=written,
        file_type="jpeg",
    )


def build_pdf_export(
    db_path: Path,
    req: HistoryExportRequest,
) -> HistoryExportResult:
    font_name = _ensure_pdf_font()
    page_w, page_h = landscape(A4)
    margin = PDF_MARGIN_MM * mm
    usable_w = page_w - 2 * margin
    raw_widths = [float(max(40, c.width)) for c in req.columns]
    total_raw = sum(raw_widths) or 1.0
    scale = usable_w / total_raw
    col_widths = [w * scale for w in raw_widths]

    # Keep layout width ratios; fit to page. Font floor = 8pt (configurable).
    # If fitted columns cannot host readable 8pt glyphs, refuse before building.
    font_pt = PDF_MIN_FONT_PT
    min_readable_col_pt = PDF_MIN_FONT_PT * 1.2
    if any(w < min_readable_col_pt for w in col_widths):
        raise HistoryExportError("pdf_columns_too_wide")
    if len(req.columns) * min_readable_col_pt > usable_w + 0.01:
        raise HistoryExportError("pdf_columns_too_wide")

    style = ParagraphStyle(
        name="HistoryExportCell",
        fontName=font_name,
        fontSize=font_pt,
        leading=font_pt + 2,
        encoding="utf-8",
    )
    header_style = ParagraphStyle(
        name="HistoryExportHeader",
        fontName=font_name,
        fontSize=font_pt,
        leading=font_pt + 2,
        textColor=colors.HexColor("#1b3d66"),
        encoding="utf-8",
    )

    def _p(text: str, st: ParagraphStyle) -> Paragraph:
        safe = (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return Paragraph(safe, st)

    header_row = [_p(c.label, header_style) for c in req.columns]
    data_rows: list[list[Any]] = []
    row_count = 0
    for batch in iter_history_export_rows(db_path, req):
        for row in batch:
            data_rows.append([_p(v, style) for v in _project_row(row, req.columns)])
            row_count += 1

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title="match-history-export",
    )
    table = Table([header_row, *data_rows], colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), font_pt),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f6ee")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5d0dc")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f4f6f9")],
                ),
            ]
        )
    )
    try:
        doc.build([Spacer(1, 1), table])
    except Exception as e:
        # ReportLab layout failures for over-wide tables → specified user message.
        msg = str(e).lower()
        if "too large" in msg or "layout" in msg:
            raise HistoryExportError("pdf_columns_too_wide") from e
        raise HistoryExportError("export_failed", str(e)) from e
    return HistoryExportResult(
        content=output.getvalue(),
        mime=_MIME_BY_TYPE["pdf"],
        row_count=row_count,
        file_type="pdf",
    )


def build_history_export(
    db_path: Path,
    req: HistoryExportRequest,
) -> HistoryExportResult:
    if req.file_type == "xlsx":
        return build_xlsx_export(db_path, req)
    if req.file_type == "csv":
        return build_csv_export(db_path, req)
    if req.file_type == "jpeg":
        return build_jpeg_export(db_path, req)
    if req.file_type == "pdf":
        return build_pdf_export(db_path, req)
    raise HistoryExportError("invalid_file_type", req.file_type)


__all__ = [
    "EXPORT_BATCH_SIZE",
    "EXPORT_FILE_TYPES",
    "HistoryExportError",
    "HistoryExportRequest",
    "HistoryExportResult",
    "JPEG_MAX_HEIGHT_PX",
    "JPEG_MAX_PIXELS",
    "JPEG_MAX_WIDTH_PX",
    "PDF_MIN_FONT_PT",
    "build_history_export",
    "count_history_export_rows",
    "default_export_basename",
    "ascii_fallback_filename",
    "export_content_disposition",
    "extension_for_type",
    "header_safe_export_filename",
    "hong_kong_today_ymd",
    "iter_history_export_rows",
    "parse_history_export_request",
    "validate_export_filename",
]

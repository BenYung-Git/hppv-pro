"""Racing history export (XLSX / CSV / JPEG / PDF) from race_records."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from hppv.racing_history import (
    DEFAULT_HISTORY_SEARCH_OP,
    RACING_HISTORY_PAGE_SIZE,
    RacingHistorySearchError,
    fetch_racing_history_filtered,
    normalize_history_search_op,
    parse_racing_sort_param,
)
from hppv.racing_layout import (
    COLUMN_WIDTH_DEFAULT,
    COLUMN_WIDTH_MAX,
    COLUMN_WIDTH_MIN,
    RACING_HISTORY_COLUMN_CATALOG,
    racing_search_keys_from_layout,
)
from hppv.translations import load_active_text_map, normalize_lang

HKT = ZoneInfo("Asia/Hong_Kong")
EXPORT_FILE_TYPES = frozenset({"xlsx", "csv", "jpeg", "pdf"})
EXPORT_BATCH_SIZE = 500
JPEG_MAX_WIDTH_PX = 12000
JPEG_MAX_HEIGHT_PX = 12000
JPEG_MAX_PIXELS = 40_000_000
JPEG_ROW_HEIGHT_PX = 22
JPEG_HEADER_HEIGHT_PX = 28
JPEG_PAD_PX = 8
PDF_MIN_FONT_PT = 8.0
PDF_MARGIN_MM = 10.0
PDF_FONT_NAME = "STSong-Light"

_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXT_BY_TYPE = {"xlsx": ".xlsx", "csv": ".csv", "jpeg": ".jpeg", "pdf": ".pdf"}
_MIME_BY_TYPE = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "jpeg": "image/jpeg",
    "pdf": "application/pdf",
}


class RacingExportError(RuntimeError):
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
class RacingExportRequest:
    file_type: str
    filename: str
    q: str
    q_op: str
    all_matches: bool
    race_date: str | None
    sort: str
    columns: tuple[ExportColumn, ...]
    lang: str
    racecourse: str | None = None
    race_no: int | None = None


def validate_export_filename(name: str, file_type: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise RacingExportError("invalid_filename")
    text = _ILLEGAL_FILENAME_RE.sub("_", text)
    ext = _EXT_BY_TYPE[file_type]
    if not text.lower().endswith(ext):
        text = text + ext
    return text


def header_safe_export_filename(name: str) -> str:
    return quote(name)


def export_content_disposition(filename: str) -> str:
    safe = header_safe_export_filename(filename)
    return f"attachment; filename*=UTF-8''{safe}"


def racing_column_i18n_maps(text_map: Mapping[str, str], keys: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in keys:
        label = text_map.get(f"racing.history.col.{key}")
        out[key] = label if label else key
    return out


def parse_racing_export_request(payload: Mapping[str, Any]) -> RacingExportRequest:
    file_type = str(payload.get("file_type") or "").strip().lower()
    if file_type not in EXPORT_FILE_TYPES:
        raise RacingExportError("unsupported_type")
    filename = validate_export_filename(str(payload.get("filename") or ""), file_type)
    cols_raw = payload.get("columns")
    if not isinstance(cols_raw, list) or not cols_raw:
        raise RacingExportError("invalid_columns")
    lang = normalize_lang(str(payload.get("lang") or "zh-Hant"))
    text_map = load_active_text_map(
        # translations live on soccer DB path via web; caller may pass lang only —
        # load via soccer path from config.
        __import__("hppv.config", fromlist=["default_config"]).default_config().soccer_db_path,
        lang,
    )
    labels = racing_column_i18n_maps(text_map, [str(c.get("key")) for c in cols_raw if isinstance(c, dict)])
    columns: list[ExportColumn] = []
    for c in cols_raw:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or "").strip()
        if not key:
            continue
        try:
            width = int(c.get("width") or COLUMN_WIDTH_DEFAULT)
        except (TypeError, ValueError):
            width = COLUMN_WIDTH_DEFAULT
        width = max(COLUMN_WIDTH_MIN, min(COLUMN_WIDTH_MAX, width))
        columns.append(ExportColumn(key=key, width=width, label=labels.get(key, key)))
    if not columns:
        raise RacingExportError("invalid_columns")
    all_matches = bool(payload.get("all_matches", True))
    race_date = str(payload.get("date") or "").strip() or None
    return RacingExportRequest(
        file_type=file_type,
        filename=filename,
        q=str(payload.get("q") or ""),
        q_op=normalize_history_search_op(str(payload.get("q_op") or DEFAULT_HISTORY_SEARCH_OP)),
        all_matches=all_matches,
        race_date=None if all_matches else race_date,
        sort=str(payload.get("sort") or ""),
        columns=tuple(columns),
        lang=lang,
    )


def _iter_rows(db_path: Path, req: RacingExportRequest) -> list[dict[str, str]]:
    sort_specs = parse_racing_sort_param(req.sort)
    search_keys = racing_search_keys_from_layout(
        {
            "columns": [
                {"key": c.key, "visible": True, "width": c.width, "sort": ""}
                for c in req.columns
            ]
        }
    )
    out: list[dict[str, str]] = []
    page = 1
    while True:
        result = fetch_racing_history_filtered(
            db_path,
            all_matches=req.all_matches,
            race_date=req.race_date,
            n=page,
            q=req.q,
            q_op=req.q_op,
            sort_specs=sort_specs,
            search_keys=search_keys,
            page_size=EXPORT_BATCH_SIZE,
        )
        out.extend(result.rows)
        if not result.total_pages or page >= result.total_pages:
            break
        page += 1
    return out


def _matrix(rows: list[dict[str, str]], columns: Sequence[ExportColumn]) -> list[list[str]]:
    header = [c.label for c in columns]
    body = [[str(r.get(c.key, "") or "") for c in columns] for r in rows]
    return [header, *body]


def _build_xlsx(matrix: list[list[str]]) -> bytes:
    df = pd.DataFrame(matrix[1:], columns=matrix[0])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _build_csv(matrix: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(matrix)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _build_pdf(matrix: list[list[str]], columns: Sequence[ExportColumn]) -> bytes:
    pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT_NAME))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=PDF_MARGIN_MM * mm,
        rightMargin=PDF_MARGIN_MM * mm,
        topMargin=PDF_MARGIN_MM * mm,
        bottomMargin=PDF_MARGIN_MM * mm,
    )
    style = ParagraphStyle(name="cell", fontName=PDF_FONT_NAME, fontSize=PDF_MIN_FONT_PT, leading=10)
    data = [[Paragraph(str(cell), style) for cell in row] for row in matrix]
    widths = [max(40, c.width * 0.6) for c in columns]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.93, 0.95, 0.98)),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTNAME", (0, 0), (-1, -1), PDF_FONT_NAME),
                ("FONTSIZE", (0, 0), (-1, -1), PDF_MIN_FONT_PT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    doc.build([Spacer(1, 2 * mm), table])
    return buf.getvalue()


def _build_jpeg(matrix: list[list[str]], columns: Sequence[ExportColumn]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    rows = len(matrix)
    cols = len(columns)
    col_w = [max(60, int(c.width)) for c in columns]
    width = sum(col_w) + JPEG_PAD_PX * 2
    height = JPEG_HEADER_HEIGHT_PX + max(0, rows - 1) * JPEG_ROW_HEIGHT_PX + JPEG_PAD_PX * 2
    if width > JPEG_MAX_WIDTH_PX or height > JPEG_MAX_HEIGHT_PX or width * height > JPEG_MAX_PIXELS:
        raise RacingExportError("image_too_large")
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("msyh.ttc", 14)
    except OSError:
        font = ImageFont.load_default()
    y = JPEG_PAD_PX
    for r_i, row in enumerate(matrix):
        x = JPEG_PAD_PX
        row_h = JPEG_HEADER_HEIGHT_PX if r_i == 0 else JPEG_ROW_HEIGHT_PX
        bg = (238, 243, 251) if r_i == 0 else ((255, 255, 255) if r_i % 2 else (244, 246, 249))
        draw.rectangle([0, y, width, y + row_h], fill=bg)
        for c_i, cell in enumerate(row):
            draw.text((x + 4, y + 4), str(cell), fill=(29, 42, 58), font=font)
            x += col_w[c_i]
        y += row_h
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def build_racing_history_export(db_path: Path, req: RacingExportRequest) -> tuple[bytes, str, int]:
    try:
        rows = _iter_rows(db_path, req)
    except RacingHistorySearchError as e:
        raise RacingExportError(e.code, e.detail) from e
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
    return data, _MIME_BY_TYPE[req.file_type], len(rows)

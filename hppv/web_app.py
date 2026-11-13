

from __future__ import annotations
print(">>> USING WEB_APP FROM:", __file__)

from pathlib import Path
from typing import Any
import io
import json
import logging
import re
from urllib.parse import urlencode
from datetime import date, datetime

import pandas as pd
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

from hppv import reports

from hppv.config import default_config
from hppv.db import (
    count_horses,
    count_race_records,
    fetch_horse_by_id,
    fetch_horses,
    fetch_race_records,
    get_recent_form_for_horse,
    get_horse_status,
    get_connection,
    init_db,
    sqlite3,
    search_horses_by_keyword,
)
from hppv.pp_preset import (
    find_preset,
    load_pp_preset_list,
    save_pp_preset_default,
    save_pp_preset_list,
    slug_preset_id,
)
from hppv.soccer_db import SoccerSchemaMismatchError, init_soccer_db
from hppv.soccer_matches import (
    DEFAULT_STATUS_FILTER,
    STATUS_FILTER_MAPPING_CONNECTED,
    SoccerDataError,
    fetch_matches_filtered,
    hong_kong_today,
    normalize_status_filter,
    parse_all_matches_flag,
    parse_requested_date,
    resolve_all_matches_arg,
    parse_sort_param,
    search_keys_from_layout,
    shift_date,
    sort_specs_from_layout,
)
from hppv.soccer_history import (
    DEFAULT_HISTORY_SEARCH_OP,
    HISTORY_PAGE_SIZE,
    default_history_sort_specs,
    fetch_history_filtered,
    history_sort_param,
    history_sort_specs_from_layout,
    normalize_history_search_op,
    parse_history_sort_param,
)
from hppv.soccer_daily_export import (
    build_daily_export,
    parse_daily_export_request,
)
from hppv.soccer_history_export import (
    HistoryExportError,
    build_history_export,
    export_content_disposition,
    header_safe_export_filename,
    parse_history_export_request,
    validate_export_filename,
)
from hppv.soccer_graphql import (
    SoccerGraphQLSendError,
    parse_and_validate_send_payload,
    send_soccer_graphql,
)
from hppv.soccer_ingest import (
    HISTORY_SCOPE_PAYLOAD_ALL,
    REQUEST_KEY_MATCHES_AND_ODDS,
    REQUEST_KEY_MATCH_RESULTS,
    REQUEST_KEY_MATCH_RESULTS_BY_YEAR,
    REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
    SoccerIngestError,
    format_hkt_display,
    get_last_success_at,
    ingest_matches_and_odds,
    ingest_official_match_results,
    ingest_official_results_to_history,
    update_odds_for_existing_matches,
)
from hppv.soccer_layout import (
    LAYOUT_KEY_DAILY_MATCHES,
    LAYOUT_KEY_MATCH_HISTORY,
    SoccerLayoutError,
    ZEBRA_COLORS,
    catalog_public,
    default_layout_settings,
    get_layout_settings,
    save_layout_settings,
    zebra_css_for_id,
    COLUMN_WIDTH_DEFAULT,
    COLUMN_WIDTH_MAX,
    COLUMN_WIDTH_MIN,
)
from hppv.soccer_requests import (
    SOCCER_REQUEST_PRESETS,
    SoccerRequestPresetError,
    ensure_placeholder_preset_files,
    load_soccer_request_preset,
)
from hppv.soccer_task_scheduler import (
    SoccerTaskError,
    download_match_results_complete,
    get_update_matches_scheduler,
    get_update_odds_scheduler,
    get_update_results_scheduler,
    soccer_shared_job_lock,
)
from hppv.translations import (
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    TranslationError,
    create_translation,
    history_column_i18n_maps,
    list_translations,
    load_active_text_map,
    normalize_lang,
    set_translation_disabled,
    translate,
    update_translation,
)
from hppv.web_utils import format_race_date_display, race_date_sort_key

BASE_DIR = Path(__file__).resolve().parent



# 與 horses_list 往績 sorters 欄位一致（save_pp_preset 驗證用）
HORSES_RECORD_SORT_KEYS: frozenset[str] = frozenset(
    {
        "race_date",
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
        "flag",
    }
)
TEMPLATE_DIR = BASE_DIR / "web_templates"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
# 開發時改模板後不必手動重啟（仍建議重啟以避免多程序混淆）
app.config["TEMPLATES_AUTO_RELOAD"] = True
# Session for language preference only (no user accounts).
app.secret_key = "hppv-web-session-key"
cfg = default_config()
DB_PATH = cfg.db_path
SOCCER_DB_PATH = cfg.soccer_db_path
SOCCER_REQUESTS_DIR = cfg.soccer_requests_dir
RANKINGS_UI_VERSION = "rankings-ui-v3"

# One-time process init for soccer DB / translations seed (not per-request DDL).
try:
    init_soccer_db(SOCCER_DB_PATH)
except SoccerSchemaMismatchError as _soccer_schema_err:
    print(">>> SOCCER DB SCHEMA MISMATCH:", _soccer_schema_err)

try:
    ensure_placeholder_preset_files(SOCCER_REQUESTS_DIR)
except OSError as _soccer_req_err:
    print(">>> SOCCER REQUEST PRESET INIT FAILED:", _soccer_req_err)

# Single-process soccer task schedulers (not multi-worker safe).
_update_matches_scheduler = get_update_matches_scheduler(
    SOCCER_DB_PATH, SOCCER_REQUESTS_DIR
)
_update_odds_scheduler = get_update_odds_scheduler(
    SOCCER_DB_PATH, SOCCER_REQUESTS_DIR
)
_update_results_scheduler = get_update_results_scheduler(
    SOCCER_DB_PATH, SOCCER_REQUESTS_DIR
)
try:
    _update_matches_scheduler.start()
except Exception as _soccer_task_err:
    print(">>> SOCCER UPDATE_MATCHES SCHEDULER START FAILED:", _soccer_task_err)
try:
    _update_odds_scheduler.start()
except Exception as _soccer_odds_err:
    print(">>> SOCCER UPDATE_ODDS SCHEDULER START FAILED:", _soccer_odds_err)
try:
    _update_results_scheduler.start()
except Exception as _soccer_results_err:
    print(">>> SOCCER UPDATE_RESULTS SCHEDULER START FAILED:", _soccer_results_err)

@app.get("/ping")
def ping():
    return "pong"



def _compute_web_build_tag() -> str:
    """
    產生可見版本戳記：
    - 任何 web_app.py / web_templates/*.html 更新後，標記會改變
    - 方便前端快速判斷是否載入新版本
    """
    targets = [Path(__file__), *TEMPLATE_DIR.glob("*.html")]
    latest_ts = 0.0
    for p in targets:
        try:
            ts = p.stat().st_mtime
        except OSError:
            continue
        if ts > latest_ts:
            latest_ts = ts
    if latest_ts <= 0:
        return "web-unknown"
    return "web-" + datetime.fromtimestamp(latest_ts).strftime("%Y%m%d-%H%M%S")


WEB_BUILD_TAG = _compute_web_build_tag()


@app.get("/api/debug/web_app_info")
def api_debug_web_app_info() -> Any:
    """診斷：確認目前 process 載入嘅 web_app / 模板係邊個檔案（排查仍見舊版）。"""
    rankings_path = TEMPLATE_DIR / "rankings.html"
    has_stamp = False
    try:
        has_stamp = "build-stamp" in rankings_path.read_text(encoding="utf-8")
    except OSError:
        pass
    return jsonify(
        {
            "ok": True,
            "web_app_py": str(Path(__file__).resolve()),
            "template_dir": str(TEMPLATE_DIR.resolve()),
            "rankings_html": str(rankings_path.resolve()),
            "rankings_template_has_build_stamp": has_stamp,
            "RANKINGS_UI_VERSION": RANKINGS_UI_VERSION,
            "WEB_BUILD_TAG": WEB_BUILD_TAG,
            "db_path": str(DB_PATH),
        }
    )


@app.context_processor
def inject_web_build_tag() -> dict[str, str]:
    return {"web_build_tag": WEB_BUILD_TAG}


@app.context_processor
def inject_translations() -> dict[str, Any]:
    lang = normalize_lang(session.get("lang"))
    try:
        text_map = load_active_text_map(SOCCER_DB_PATH, lang)
    except Exception:
        text_map = {}

    def t(key: str) -> str:
        return translate(text_map, key)

    return {
        "t": t,
        "current_lang": lang,
        "supported_langs": SUPPORTED_LANGS,
    }


@app.after_request
def add_version_header(resp):
    resp.headers["X-HPPV-Web-Build"] = WEB_BUILD_TAG
    return resp


def _race_meeting_snapshot() -> dict[str, Any]:
    race_date = "—"
    racecourse_code = "—"
    race_count = 0
    stored_meetings = 0

    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            meta_rows = conn.execute(
                """
                SELECT key, value FROM racecard_meta
                WHERE key IN (
                    'last_source_race_date',
                    'last_source_racecourse_code'
                );
                """
            ).fetchall()
    except Exception:
        meta_rows = []

    meta = {str(r["key"]): str(r["value"]) for r in meta_rows}
    race_date = meta.get("last_source_race_date", "—").strip() or "—"
    racecourse_code = meta.get("last_source_racecourse_code", "—").strip() or "—"

    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if race_date != "—" and racecourse_code != "—":
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT race_no) AS c
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ?;
                    """,
                    (race_date, racecourse_code),
                ).fetchone()
                race_count = int(row["c"]) if row else 0

            row2 = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT race_date, racecourse
                    FROM racecard_starters
                    GROUP BY race_date, racecourse
                ) x;
                """
            ).fetchone()
            stored_meetings = int(row2["c"]) if row2 else 0
    except Exception:
        pass

    return {
        "race_date": race_date,
        "racecourse": racecourse_code,
        "race_count": race_count,
        "stored_meetings": stored_meetings,
    }


def _racecourse_display(code: str) -> str:
    mapping = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}
    c = str(code or "").strip().upper()
    return mapping.get(c, c or "—")


def _message_board_images() -> list[str]:
    assets_dir = BASE_DIR / "assets" / "message_board"
    if not assets_dir.is_dir():
        return []
    files = [
        p.name for p in assets_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif")
    ]
    files.sort()
    return files


def _latest_rankings_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "race_date": "—",
        "racecourse": "—",
        "race_count": 0,
        "races": [],
    }
    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            meeting = conn.execute(
                """
                SELECT race_date, racecourse
                FROM racecard_starters
                GROUP BY race_date, racecourse
                ORDER BY race_date DESC, racecourse ASC
                LIMIT 1;
                """
            ).fetchone()
            if not meeting:
                return payload

            race_date = str(meeting["race_date"] or "")
            racecourse = str(meeting["racecourse"] or "")
            rows = conn.execute(
                """
                SELECT
                    race_no,
                    horse_id,
                    "馬號" AS horse_no,
                    "馬名" AS horse_name,
                    "檔位" AS draw,
                    "騎師" AS jockey,
                    "練馬師" AS trainer,
                    "評分" AS rating,
                    "負磅" AS handicap,
                    "後備" AS reserve
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC, CAST("馬號" AS INTEGER) ASC, "馬號" ASC;
                """,
                (race_date, racecourse),
            ).fetchall()
    except Exception:
        return payload

    races_map: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        race_no = int(row["race_no"] or 0)
        races_map.setdefault(race_no, []).append(
            {
                "horse_id": str(row["horse_id"] or ""),
                "horse_no": str(row["horse_no"] or ""),
                "horse_name": str(row["horse_name"] or ""),
                "draw": str(row["draw"] or ""),
                "jockey": str(row["jockey"] or ""),
                "trainer": str(row["trainer"] or ""),
                "rating": str(row["rating"] or ""),
                "handicap": str(row["handicap"] or ""),
                "reserve": str(row["reserve"] or ""),
            }
        )

    races = [
        {
            "race_no": race_no,
            "title": f"第 {race_no} 場｜{_racecourse_display(racecourse)}｜距離：—｜班次：—",
            "rows": races_map[race_no],
        }
        for race_no in sorted(races_map.keys())
    ]
    return {
        "race_date": race_date,
        "racecourse": _racecourse_display(racecourse),
        "race_count": len(races),
        "races": races,
    }


@app.get("/assets/message_board/<path:filename>")
def message_image(filename: str):
    folder = BASE_DIR / "assets" / "message_board"
    return send_from_directory(str(folder), filename)


@app.get("/")
def index() -> str:
    init_db(DB_PATH)
    try:
        with get_connection(DB_PATH) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM race_records;").fetchone()
            race_records_total = int(row["c"]) if row else 0
    except Exception:
        race_records_total = 0
    snap = _race_meeting_snapshot()
    stats = {
        "active_horses": count_horses(DB_PATH),
        "race_records_total": race_records_total,
        "race_date": snap["race_date"],
        "racecourse": _racecourse_display(snap["racecourse"]),
        "race_count": snap["race_count"],
        "stored_meetings": snap["stored_meetings"],
    }
    return render_template(
        "dashboard.html",
        stats=stats,
        images=_message_board_images(),
        db_path=str(DB_PATH),
    )


def _layout_context() -> dict[str, Any]:
    layout_error = ""
    try:
        layout = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_DAILY_MATCHES)
    except SoccerLayoutError as e:
        layout = default_layout_settings(LAYOUT_KEY_DAILY_MATCHES)
        layout_error = e.code
        logging.getLogger(__name__).warning("soccer layout load failed: %s %s", e.code, e.detail)
    zebra_css = zebra_css_for_id(layout["zebra_color_id"]) if layout.get("zebra_enabled") else ""

    history_layout_error = ""
    try:
        history_layout = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_MATCH_HISTORY)
    except SoccerLayoutError as e:
        history_layout = default_layout_settings(
            LAYOUT_KEY_MATCH_HISTORY, db_path=SOCCER_DB_PATH
        )
        history_layout_error = e.code
        logging.getLogger(__name__).warning(
            "soccer history layout load failed: %s %s", e.code, e.detail
        )
    history_zebra_css = (
        zebra_css_for_id(history_layout["zebra_color_id"])
        if history_layout.get("zebra_enabled")
        else ""
    )
    history_catalog = catalog_public(
        LAYOUT_KEY_MATCH_HISTORY, db_path=SOCCER_DB_PATH
    )
    lang = normalize_lang(session.get("lang"))
    try:
        history_text_map = load_active_text_map(SOCCER_DB_PATH, lang)
    except Exception:
        history_text_map = {}
    history_col_labels, history_col_descs = history_column_i18n_maps(
        history_text_map,
        [str(c["key"]) for c in history_catalog],
    )
    return {
        "layout_settings": layout,
        "layout_catalog": catalog_public(LAYOUT_KEY_DAILY_MATCHES),
        "layout_zebra_colors": list(ZEBRA_COLORS),
        "layout_zebra_css": zebra_css,
        "layout_error": layout_error,
        "layout_width_default": COLUMN_WIDTH_DEFAULT,
        "layout_width_min": COLUMN_WIDTH_MIN,
        "layout_width_max": COLUMN_WIDTH_MAX,
        "status_filter_mapping_connected": STATUS_FILTER_MAPPING_CONNECTED,
        "history_layout_settings": history_layout,
        "history_layout_catalog": history_catalog,
        "history_col_labels": history_col_labels,
        "history_col_descs": history_col_descs,
        "history_layout_zebra_css": history_zebra_css,
        "history_layout_error": history_layout_error,
    }


def _soccer_list_query_state(layout_settings: dict[str, Any]) -> dict[str, Any]:
    """Parse daily list filter from URL (date / all_matches handled separately)."""
    q = (request.args.get("q") or "").strip()
    status = normalize_status_filter(request.args.get("status"))
    # Prefer n= (record cursor); accept legacy page= as the same cursor value.
    n_raw = request.args.get("n")
    if n_raw is None or str(n_raw).strip() == "":
        n_raw = request.args.get("page")
    sort_raw = request.args.get("sort")
    parsed_sort = parse_sort_param(sort_raw)
    sort_specs = (
        parsed_sort if parsed_sort is not None else sort_specs_from_layout(layout_settings)
    )
    return {
        "q": q,
        "status": status,
        "n_raw": n_raw,
        "sort_specs": sort_specs,
        "search_keys": search_keys_from_layout(layout_settings),
    }


_SOCCER_TABS = frozenset({"matches", "history", "download", "tasks"})

# Prefixed history URL keys (daily keeps unprefixed date/all_matches/q/…).
_HISTORY_URL_KEYS = frozenset(
    {
        "history_date",
        "history_all_matches",
        "history_q",
        "history_q_op",
        "history_n",
        "history_sort",
    }
)


def _soccer_active_tab(raw: str | None = None) -> tuple[str, bool, bool]:
    """
    Return (tab, open_daily_layout, open_history_layout).
    Legacy tab=layout → matches + daily layout.
    tab=history-layout → history + history layout (distinct from daily).
    """
    tab = (raw if raw is not None else request.args.get("tab") or "").strip().lower()
    if tab == "layout":
        return "matches", True, False
    if tab == "history-layout":
        return "history", False, True
    if tab in _SOCCER_TABS:
        return tab, False, False
    return "matches", False, False


def _args_has(args: Any, key: str) -> bool:
    try:
        return key in args
    except TypeError:
        return False


def _resolve_soccer_tab_scopes(
    args: Any, *, active_tab: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Isolate daily vs history list criteria from one request.args.

    New URLs: daily uses date/all_matches/q/…; history uses history_* keys.
    Legacy (no history_*): unprefixed date/all_matches/q apply only to the
    active tab; the other tab gets defaults (all_matches=True, today).
    Never silently apply one date/all_matches pair to both tabs.
    """
    today = hong_kong_today()
    has_history_prefix = any(_args_has(args, k) for k in _HISTORY_URL_KEYS)

    def _default_scope() -> dict[str, Any]:
        return {
            "all_matches": True,
            "selected": today,
            "date_invalid": False,
            "compat_legacy": False,
        }

    if has_history_prefix:
        daily_all = resolve_all_matches_arg(args)
        daily_selected, daily_invalid = parse_requested_date(args.get("date"))
        if daily_invalid and not daily_all:
            daily_scope = {
                "all_matches": daily_all,
                "selected": None,
                "date_invalid": True,
                "compat_legacy": False,
            }
        else:
            daily_scope = {
                "all_matches": daily_all,
                "selected": today if (daily_invalid or daily_selected is None) else daily_selected,
                "date_invalid": False,
                "compat_legacy": False,
            }
        if _args_has(args, "history_all_matches"):
            hist_all = parse_all_matches_flag(args.get("history_all_matches"))
        else:
            hist_all = True
        hist_selected, hist_invalid = parse_requested_date(args.get("history_date"))
        if hist_invalid and not hist_all:
            hist_scope = {
                "all_matches": hist_all,
                "selected": None,
                "date_invalid": True,
                "compat_legacy": False,
            }
        else:
            hist_scope = {
                "all_matches": hist_all,
                "selected": today if (hist_invalid or hist_selected is None) else hist_selected,
                "date_invalid": False,
                "compat_legacy": False,
            }
        return daily_scope, hist_scope

    # Legacy shared params → active tab only.
    legacy_all = resolve_all_matches_arg(args)
    legacy_selected, legacy_invalid = parse_requested_date(args.get("date"))
    if active_tab == "history":
        daily_scope = _default_scope()
        if legacy_invalid and not legacy_all:
            hist_scope = {
                "all_matches": legacy_all,
                "selected": None,
                "date_invalid": True,
                "compat_legacy": True,
            }
        else:
            hist_scope = {
                "all_matches": legacy_all,
                "selected": today if (legacy_invalid or legacy_selected is None) else legacy_selected,
                "date_invalid": False,
                "compat_legacy": True,
            }
        return daily_scope, hist_scope

    hist_scope = _default_scope()
    if legacy_invalid and not legacy_all:
        daily_scope = {
            "all_matches": legacy_all,
            "selected": None,
            "date_invalid": True,
            "compat_legacy": False,
        }
    else:
        daily_scope = {
            "all_matches": legacy_all,
            "selected": today if (legacy_invalid or legacy_selected is None) else legacy_selected,
            "date_invalid": False,
            "compat_legacy": False,
        }
    return daily_scope, hist_scope


def _history_list_query_state(layout_settings: dict[str, Any]) -> dict[str, Any]:
    """History list args — prefixed history_* with legacy fallback when absent."""
    args = request.args
    has_prefix = any(_args_has(args, k) for k in _HISTORY_URL_KEYS)
    if has_prefix:
        n_raw = args.get("history_n")
        q = (args.get("history_q") or "").strip()
        q_op = normalize_history_search_op(args.get("history_q_op"))
        parsed_sort = parse_history_sort_param(args.get("history_sort"))
    else:
        # Legacy: only when history is the active consumer (caller still decides).
        n_raw = args.get("n")
        if n_raw is None or str(n_raw).strip() == "":
            n_raw = args.get("page")
        q = (args.get("q") or "").strip()
        q_op = normalize_history_search_op(args.get("q_op"))
        parsed_sort = parse_history_sort_param(args.get("sort"))
    sort_specs = (
        parsed_sort
        if parsed_sort is not None
        else history_sort_specs_from_layout(layout_settings)
    )
    return {
        "n_raw": n_raw,
        "q": q,
        "q_op": q_op,
        "sort_specs": sort_specs,
        "sort": history_sort_param(sort_specs),
    }


def _empty_history_page_meta(
    *,
    all_matches: bool,
    q: str = "",
    q_op: str = DEFAULT_HISTORY_SEARCH_OP,
    sort: str = "",
) -> dict[str, Any]:
    return {
        "n": 1,
        "total_count": 0,
        "total_pages": 0,
        "page_size": HISTORY_PAGE_SIZE,
        "q": q,
        "q_op": normalize_history_search_op(q_op),
        "all_matches": all_matches,
        "sort": sort or history_sort_param(default_history_sort_specs()),
    }


@app.get("/soccer/daily")
def soccer_daily() -> str:
    """Soccer database UI (daily matches + related tabs)."""
    active_tab, open_layout_panel, open_history_layout_panel = _soccer_active_tab()
    daily_scope, hist_scope = _resolve_soccer_tab_scopes(
        request.args, active_tab=active_tab
    )
    all_matches = bool(daily_scope["all_matches"])
    date_invalid = bool(daily_scope["date_invalid"])
    selected = daily_scope["selected"]
    matches: list[dict[str, str]] = []
    load_error = False
    history_matches: list[dict[str, str]] = []
    history_load_error = False
    history_date_invalid = bool(hist_scope["date_invalid"])
    history_all_matches = bool(hist_scope["all_matches"])
    history_selected = hist_scope["selected"]
    history_selected_date = (
        "" if history_selected is None else history_selected.isoformat()
    )
    sync_at = get_last_success_at(SOCCER_DB_PATH, REQUEST_KEY_MATCHES_AND_ODDS)
    sync_at_hkt = None
    if sync_at:
        try:
            sync_at_hkt = format_hkt_display(sync_at)
        except Exception:
            sync_at_hkt = None
    layout_ctx = _layout_context()
    list_state = _soccer_list_query_state(layout_ctx["layout_settings"])
    # When legacy params belong to history, do not reuse shared q/n for daily.
    if hist_scope.get("compat_legacy") and active_tab == "history":
        list_state = {
            **list_state,
            "q": "",
            "n_raw": 1,
            "status": DEFAULT_STATUS_FILTER,
        }
    hist_state = _history_list_query_state(layout_ctx["history_layout_settings"])
    if not any(_args_has(request.args, k) for k in _HISTORY_URL_KEYS):
        if active_tab != "history":
            # Prefixed absent + not on history → history list defaults (ignore legacy q).
            hist_state = {
                "n_raw": 1,
                "q": "",
                "q_op": DEFAULT_HISTORY_SEARCH_OP,
                "sort_specs": history_sort_specs_from_layout(
                    layout_ctx["history_layout_settings"]
                ),
                "sort": history_sort_param(
                    history_sort_specs_from_layout(layout_ctx["history_layout_settings"])
                ),
            }

    # Daily chrome (never overwritten by history date).
    if date_invalid or selected is None:
        selected_date = ""
        prev_date = ""
        next_date = ""
    else:
        selected_date = selected.isoformat()
        prev_date = shift_date(selected, -1).isoformat()
        next_date = shift_date(selected, 1).isoformat()

    page_meta = {
        "n": 1,
        "total_count": 0,
        "q": list_state["q"],
        "status": list_state["status"],
        "status_filter_applied": False,
        "all_matches": all_matches,
        "loaded": False,
    }
    history_page_meta = _empty_history_page_meta(
        all_matches=history_all_matches,
        q=hist_state["q"],
        q_op=hist_state["q_op"],
        sort=hist_state["sort"],
    )
    history_page_meta["loaded"] = False

    load_daily = active_tab == "matches" or open_layout_panel
    load_history = active_tab == "history" or open_history_layout_panel

    if load_history:
        if history_date_invalid and not history_all_matches:
            history_selected_date = ""
        else:
            if history_selected is None:
                history_selected = hong_kong_today()
                history_selected_date = history_selected.isoformat()
            try:
                hist = fetch_history_filtered(
                    SOCCER_DB_PATH,
                    None if history_all_matches else history_selected,
                    n=hist_state["n_raw"],
                    q=hist_state["q"],
                    q_op=hist_state["q_op"],
                    sort_specs=hist_state["sort_specs"],
                )
                history_matches = hist.matches
                history_page_meta = {
                    "n": hist.n,
                    "total_count": hist.total_count,
                    "total_pages": hist.total_pages,
                    "page_size": hist.page_size,
                    "q": hist.q,
                    "q_op": hist.q_op,
                    "all_matches": history_all_matches,
                    "sort": hist.sort,
                    "loaded": True,
                }
            except SoccerDataError as e:
                history_load_error = True
                history_page_meta["loaded"] = True
                logging.getLogger(__name__).exception("soccer history read failed: %s", e)

    if load_daily:
        if date_invalid and not all_matches:
            page_meta["loaded"] = True
            return render_template(
                "soccer_daily.html",
                date_invalid=True,
                load_error=False,
                matches=[],
                selected_date="",
                prev_date="",
                next_date="",
                sync_at_hkt=sync_at_hkt,
                page_meta=page_meta,
                all_matches=all_matches,
                active_tab=active_tab,
                open_layout_panel=open_layout_panel,
                open_history_layout_panel=open_history_layout_panel,
                history_matches=history_matches,
                history_load_error=history_load_error,
                history_date_invalid=history_date_invalid,
                history_page_meta=history_page_meta,
                history_all_matches=history_all_matches,
                history_selected_date=history_selected_date
                or hong_kong_today().isoformat(),
                **layout_ctx,
            )
        assert selected is not None
        try:
            result = fetch_matches_filtered(
                SOCCER_DB_PATH,
                None if all_matches else selected,
                n=list_state["n_raw"],
                q=list_state["q"],
                status=list_state["status"],
                sort_specs=list_state["sort_specs"],
                search_keys=list_state["search_keys"],
            )
            matches = result.matches
            page_meta = {
                "n": result.n,
                "total_count": result.total_count,
                "q": result.q,
                "status": result.status,
                "status_filter_applied": result.status_filter_applied,
                "all_matches": all_matches,
                "loaded": True,
            }
        except SoccerDataError as e:
            load_error = True
            page_meta["loaded"] = True
            logging.getLogger(__name__).exception("soccer daily read failed: %s", e)

    return render_template(
        "soccer_daily.html",
        date_invalid=False,
        load_error=load_error,
        matches=matches,
        selected_date=selected_date or hong_kong_today().isoformat(),
        prev_date=prev_date
        or shift_date(hong_kong_today(), -1).isoformat(),
        next_date=next_date
        or shift_date(hong_kong_today(), 1).isoformat(),
        sync_at_hkt=sync_at_hkt,
        page_meta=page_meta,
        all_matches=all_matches,
        active_tab=active_tab,
        open_layout_panel=open_layout_panel,
        open_history_layout_panel=open_history_layout_panel,
        history_matches=history_matches,
        history_load_error=history_load_error,
        history_date_invalid=history_date_invalid,
        history_page_meta=history_page_meta,
        history_all_matches=history_all_matches,
        history_selected_date=history_selected_date
        or hong_kong_today().isoformat(),
        **layout_ctx,
    )


@app.get("/soccer/history")
def soccer_history():
    """Match-history tab entry."""
    args = request.args.to_dict(flat=True)
    args["tab"] = "history"
    return redirect(url_for("soccer_daily", **args))


@app.post("/lang")
def set_language():
    """Remember UI language in the browser session (POST only)."""
    lang = normalize_lang(request.form.get("lang") or DEFAULT_LANG)
    session["lang"] = lang
    next_url = (request.form.get("next") or "").strip() or url_for("index")
    if not next_url.startswith("/"):
        next_url = url_for("index")
    return redirect(next_url)


@app.get("/api/soccer/request-presets/<preset_id>")
def api_soccer_request_preset(preset_id: str) -> Any:
    """Load a registered soccer GraphQL preset into the client (read-only; no network)."""
    try:
        _payload, display = load_soccer_request_preset(SOCCER_REQUESTS_DIR, preset_id)
    except SoccerRequestPresetError as e:
        logging.getLogger(__name__).warning(
            "soccer request preset load failed id=%s code=%s detail=%s",
            preset_id,
            e.code,
            e.detail,
        )
        return jsonify({"ok": False, "error": e.code}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("soccer request preset unexpected error: %s", e)
        return jsonify({"ok": False, "error": "read_failed"}), 500
    return jsonify({"ok": True, "display": display})


def _ingest_public_dict(request_key: str) -> dict[str, Any]:
    ingest: dict[str, Any] = {
        "status": "not_attempted",
        "code": "",
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "had_complete": 0,
        "had_incomplete": 0,
        "skipped_missing": 0,
        "results_applied": False,
        "request_key": request_key,
        "ht_applied": 0,
        "ft_applied": 0,
        "sh_applied": 0,
        "skipped_status": 0,
        "skipped_unconfirmed": 0,
        "skipped_insufficient": 0,
        "skipped_no_kickoff": 0,
        "skipped_not_in_daily": 0,
        "daily_deleted": 0,
        "match_num_total": None,
        "matches_returned": None,
        "last_success_at": get_last_success_at(SOCCER_DB_PATH, request_key),
        "last_success_at_hkt": None,
        "detail": "",
        "date_window": None,
        "fetch_mode": None,
        "history_scope": None,
        "download_status": None,
    }
    if ingest["last_success_at"]:
        try:
            ingest["last_success_at_hkt"] = format_hkt_display(
                str(ingest["last_success_at"])
            )
        except Exception:
            ingest["last_success_at_hkt"] = None
    return ingest


def _apply_ingest_result(ingest: dict[str, Any], ing: Any, request_key: str) -> None:
    ingest.update(
        {
            "status": "success" if ing.code == "success" else "skipped",
            "code": ing.code,
            "processed": ing.processed,
            "inserted": ing.inserted,
            "updated": ing.updated,
            "had_complete": ing.had_complete,
            "had_incomplete": ing.had_incomplete,
            "skipped_missing": getattr(ing, "skipped_missing", 0) or 0,
            "results_applied": ing.results_applied,
            "request_key": ing.request_key,
            "ht_applied": ing.ht_applied,
            "ft_applied": ing.ft_applied,
            "sh_applied": ing.sh_applied,
            "skipped_status": ing.skipped_status,
            "skipped_unconfirmed": ing.skipped_unconfirmed,
            "skipped_insufficient": getattr(ing, "skipped_insufficient", 0) or 0,
            "skipped_no_kickoff": getattr(ing, "skipped_no_kickoff", 0) or 0,
            "skipped_not_in_daily": getattr(ing, "skipped_not_in_daily", 0) or 0,
            "daily_deleted": getattr(ing, "daily_deleted", 0) or 0,
            "match_num_total": ing.match_num_total,
            "matches_returned": ing.matches_returned,
            "last_success_at": ing.last_success_at
            or get_last_success_at(SOCCER_DB_PATH, request_key),
            "detail": ing.detail,
        }
    )
    if ingest["last_success_at"]:
        ingest["last_success_at_hkt"] = format_hkt_display(
            str(ingest["last_success_at"])
        )


def _api_soccer_manual_match_results(payload: Any) -> Any:
    """
    Manual「下載正式賽果」: payload date window → complete download (+ day chunk)
    → ingest_official_results_to_history (payload_all). Shares task job lock.
    Does not rewrite dates to soccer_daily MIN/MAX.
    """
    vars_in = dict(payload.variables or {})
    start_date = vars_in.get("startDate")
    end_date = vars_in.get("endDate")
    if not isinstance(start_date, str) or not start_date.strip():
        return jsonify({"ok": False, "error": "invalid_fields"}), 400
    if not isinstance(end_date, str) or not end_date.strip():
        return jsonify({"ok": False, "error": "invalid_fields"}), 400
    start_date = start_date.strip()
    end_date = end_date.strip()

    ingest = _ingest_public_dict(REQUEST_KEY_MATCH_RESULTS)
    ingest["history_scope"] = HISTORY_SCOPE_PAYLOAD_ALL
    ingest["date_window"] = {"startDate": start_date, "endDate": end_date}

    try:
        with soccer_shared_job_lock():
            response_obj, variables, fetch_meta = download_match_results_complete(
                payload,
                start_date=start_date,
                end_date=end_date,
            )
            ingest["download_status"] = "success"
            ingest["fetch_mode"] = fetch_meta.get("fetch_mode")
            if isinstance(fetch_meta.get("date_window"), dict):
                ingest["date_window"] = fetch_meta["date_window"]
            ing = ingest_official_results_to_history(
                SOCCER_DB_PATH,
                response_obj,
                request_key=REQUEST_KEY_MATCH_RESULTS,
                variables=variables,
                history_scope=HISTORY_SCOPE_PAYLOAD_ALL,
            )
            _apply_ingest_result(ingest, ing, REQUEST_KEY_MATCH_RESULTS)
    except SoccerTaskError as e:
        logging.getLogger(__name__).warning(
            "manual match_results download failed code=%s detail=%s",
            e.code,
            e.detail,
        )
        ingest["download_status"] = "failed"
        ingest["status"] = "failed"
        ingest["code"] = e.code
        ingest["detail"] = e.detail
        status = 429 if e.code == "rate_limited" else 400
        return jsonify({"ok": False, "error": e.code, "ingest": ingest}), status
    except SoccerIngestError as e:
        logging.getLogger(__name__).warning(
            "manual match_results ingest failed code=%s detail=%s",
            e.code,
            e.detail,
        )
        ingest["download_status"] = (
            "failed" if e.code == "incomplete_results" else "success"
        )
        ingest["status"] = "failed"
        ingest["code"] = e.code
        ingest["detail"] = e.detail
        return jsonify(
            {
                "ok": True,
                "outcome": "success" if ingest["download_status"] == "success" else "http_error",
                "http_status": 200 if ingest["download_status"] == "success" else None,
                "elapsed_ms": 0,
                "body": "",
                "truncated": False,
                "match_count": ingest.get("matches_returned") or 0,
                "has_had_odds": False,
                "ingest": ingest,
            }
        )
    except Exception as e:
        logging.getLogger(__name__).exception(
            "manual match_results unexpected: %s", e
        )
        return jsonify({"ok": False, "error": "read_failed"}), 500

    body_text = json.dumps(response_obj, ensure_ascii=False)
    match_count = int(ingest.get("matches_returned") or fetch_meta.get("match_count") or 0)
    return jsonify(
        {
            "ok": True,
            "outcome": "success",
            "http_status": fetch_meta.get("http_status"),
            "elapsed_ms": fetch_meta.get("elapsed_ms") or 0,
            "body": body_text,
            "truncated": bool(fetch_meta.get("truncated")),
            "match_count": match_count,
            "has_had_odds": False,
            "ingest": ingest,
        }
    )


@app.post("/api/soccer/graphql/send")
def api_soccer_graphql_send() -> Any:
    """
    Validate Request-box payload, POST to HKJC GraphQL, and ingest matches/odds
    into soccer_daily when the response is a complete matches payload.
    No login/auth (same as other Web APIs).

    match_results uses the shared history ingest path (same as update_results task)
    with the payload date window — not soccer_daily MIN/MAX.
    """
    raw = request.get_data(as_text=True) or ""
    # Prefer JSON body field `payload` (string) or raw JSON object.
    data = request.get_json(silent=True)
    request_key = REQUEST_KEY_MATCHES_AND_ODDS
    if isinstance(data, dict):
        rk = data.get("request_key")
        if isinstance(rk, str) and rk.strip():
            request_key = rk.strip()
        if request_key not in SOCCER_REQUEST_PRESETS:
            return jsonify({"ok": False, "error": "unsupported_type"}), 400
        if isinstance(data.get("payload"), str):
            raw = data["payload"]
        elif "query" in data:
            raw = json.dumps(
                {k: data[k] for k in ("query", "variables", "operationName") if k in data},
                ensure_ascii=False,
            )

    try:
        payload = parse_and_validate_send_payload(raw)
    except SoccerGraphQLSendError as e:
        logging.getLogger(__name__).warning(
            "soccer graphql send rejected/failed code=%s detail=%s",
            e.code,
            e.detail,
        )
        status = 429 if e.code == "rate_limited" else 400
        return jsonify({"ok": False, "error": e.code}), status
    except Exception as e:
        logging.getLogger(__name__).exception("soccer graphql send unexpected: %s", e)
        return jsonify({"ok": False, "error": "read_failed"}), 500

    if request_key == REQUEST_KEY_MATCH_RESULTS:
        return _api_soccer_manual_match_results(payload)

    try:
        result = send_soccer_graphql(payload)
    except SoccerGraphQLSendError as e:
        logging.getLogger(__name__).warning(
            "soccer graphql send rejected/failed code=%s detail=%s",
            e.code,
            e.detail,
        )
        status = 429 if e.code == "rate_limited" else 400
        return jsonify({"ok": False, "error": e.code}), status
    except Exception as e:
        logging.getLogger(__name__).exception("soccer graphql send unexpected: %s", e)
        return jsonify({"ok": False, "error": "read_failed"}), 500

    ingest = _ingest_public_dict(request_key)

    # Ingest only from backend-validated complete JSON object (never truncated text).
    if result.truncated:
        ingest["status"] = "skipped"
        ingest["code"] = "truncated"
        ingest["detail"] = "response truncated"
    elif result.outcome != "success":
        ingest["status"] = "skipped"
        ingest["code"] = result.outcome
        ingest["detail"] = "response not eligible for ingest"
    else:
        try:
            response_obj = json.loads(result.body_text)
            if request_key == REQUEST_KEY_MATCH_RESULTS_BY_YEAR:
                # by_year unchanged: still soccer_daily official-results path.
                ing = ingest_official_match_results(
                    SOCCER_DB_PATH,
                    response_obj,
                    request_key=request_key,
                    variables=payload.variables,
                    allow_insert=True,
                )
            elif request_key == REQUEST_KEY_UPDATE_ODDS_AND_RESULTS:
                ing = update_odds_for_existing_matches(
                    SOCCER_DB_PATH, response_obj, request_key=request_key
                )
            else:
                ing = ingest_matches_and_odds(
                    SOCCER_DB_PATH, response_obj, request_key=request_key
                )
            _apply_ingest_result(ingest, ing, request_key)
        except SoccerIngestError as e:
            logging.getLogger(__name__).warning(
                "soccer ingest failed code=%s detail=%s", e.code, e.detail
            )
            ingest["status"] = "failed"
            ingest["code"] = e.code
            ingest["detail"] = e.detail
        except Exception as e:
            logging.getLogger(__name__).exception("soccer ingest unexpected: %s", e)
            ingest["status"] = "failed"
            ingest["code"] = "transaction_failed"
            ingest["detail"] = "unexpected ingest error"

    return jsonify(
        {
            "ok": True,
            "outcome": result.outcome,
            "http_status": result.http_status,
            "elapsed_ms": result.elapsed_ms,
            "body": result.body_text,
            "truncated": result.truncated,
            "match_count": result.match_count,
            "has_had_odds": result.has_had_odds,
            "ingest": ingest,
        }
    )


@app.get("/api/soccer/tasks/update-matches")
def api_soccer_task_update_matches_get() -> Any:
    """Return persisted update_matches task state (backend is source of truth)."""
    try:
        state = _update_matches_scheduler.get_state()
    except SoccerTaskError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("update_matches task get failed: %s", e)
        return jsonify({"ok": False, "error": "read_failed"}), 500
    return jsonify({"ok": True, "task": state.to_public_dict()})


@app.put("/api/soccer/tasks/update-matches")
def api_soccer_task_update_matches_put() -> Any:
    """Save update_matches settings and re-arm in-process schedule."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    enabled = bool(data.get("enabled"))
    try:
        state = _update_matches_scheduler.apply_settings(
            enabled=enabled,
            interval_value=data.get("interval_value"),
            interval_unit=data.get("interval_unit"),
        )
    except SoccerTaskError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("update_matches task save failed: %s", e)
        return jsonify({"ok": False, "error": "save_failed"}), 500
    return jsonify({"ok": True, "task": state.to_public_dict()})


@app.get("/api/soccer/tasks/update-odds")
def api_soccer_task_update_odds_get() -> Any:
    """Return persisted update_odds task state (backend is source of truth)."""
    try:
        state = _update_odds_scheduler.get_state()
    except SoccerTaskError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("update_odds task get failed: %s", e)
        return jsonify({"ok": False, "error": "read_failed"}), 500
    return jsonify({"ok": True, "task": state.to_public_dict()})


@app.put("/api/soccer/tasks/update-odds")
def api_soccer_task_update_odds_put() -> Any:
    """Save update_odds settings and re-arm in-process schedule."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    enabled = bool(data.get("enabled"))
    try:
        state = _update_odds_scheduler.apply_settings(
            enabled=enabled,
            interval_value=data.get("interval_value"),
            interval_unit=data.get("interval_unit"),
        )
    except SoccerTaskError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("update_odds task save failed: %s", e)
        return jsonify({"ok": False, "error": "save_failed"}), 500
    return jsonify({"ok": True, "task": state.to_public_dict()})


@app.get("/api/soccer/tasks/update-results")
def api_soccer_task_update_results_get() -> Any:
    """Return persisted update_results task state (backend is source of truth)."""
    try:
        state = _update_results_scheduler.get_state()
    except SoccerTaskError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("update_results task get failed: %s", e)
        return jsonify({"ok": False, "error": "read_failed"}), 500
    return jsonify({"ok": True, "task": state.to_public_dict()})


@app.put("/api/soccer/tasks/update-results")
def api_soccer_task_update_results_put() -> Any:
    """Save update_results settings and re-arm in-process schedule."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    enabled = bool(data.get("enabled"))
    try:
        state = _update_results_scheduler.apply_settings(
            enabled=enabled,
            interval_value=data.get("interval_value"),
            interval_unit=data.get("interval_unit"),
        )
    except SoccerTaskError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("update_results task save failed: %s", e)
        return jsonify({"ok": False, "error": "save_failed"}), 500
    return jsonify({"ok": True, "task": state.to_public_dict()})


@app.get("/api/soccer/sync-state")
def api_soccer_sync_state() -> Any:
    key = (request.args.get("request_key") or REQUEST_KEY_MATCHES_AND_ODDS).strip()
    ts = get_last_success_at(SOCCER_DB_PATH, key)
    hkt = None
    if ts:
        try:
            hkt = format_hkt_display(ts)
        except Exception:
            hkt = None
    return jsonify({"ok": True, "request_key": key, "last_success_at": ts, "last_success_at_hkt": hkt})


@app.get("/api/soccer/history-matches")
def api_soccer_history_matches() -> Any:
    """Paged Footylogic history list. Never reads quarantine or soccer_daily."""
    all_matches = resolve_all_matches_arg(request.args)
    selected, invalid = parse_requested_date(request.args.get("date"))
    if all_matches:
        if invalid or selected is None:
            selected = hong_kong_today()
    elif invalid or selected is None:
        return jsonify({"ok": False, "error": "invalid_date"}), 400
    try:
        hist_layout = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_MATCH_HISTORY)
    except SoccerLayoutError:
        hist_layout = default_layout_settings(
            LAYOUT_KEY_MATCH_HISTORY, db_path=SOCCER_DB_PATH
        )
    hist_state = _history_list_query_state(hist_layout)
    try:
        result = fetch_history_filtered(
            SOCCER_DB_PATH,
            None if all_matches else selected,
            n=hist_state["n_raw"],
            q=hist_state["q"],
            q_op=hist_state["q_op"],
            sort_specs=hist_state["sort_specs"],
        )
    except SoccerDataError as e:
        logging.getLogger(__name__).exception("soccer history-matches api failed: %s", e)
        return jsonify({"ok": False, "error": "read_error", "detail": str(e)}), 500
    return jsonify(
        {
            "ok": True,
            "source": "footylogic_matches",
            "date": selected.isoformat(),
            "all_matches": all_matches,
            "matches": result.matches,
            "n": result.n,
            "total_count": result.total_count,
            "total_pages": result.total_pages,
            "page_size": result.page_size,
            "q": result.q,
            "q_op": result.q_op,
            "sort": result.sort,
            "field_keys": list(result.field_keys),
            "layout_settings": hist_layout,
            "catalog": catalog_public(
                LAYOUT_KEY_MATCH_HISTORY, db_path=SOCCER_DB_PATH
            ),
        }
    )


@app.get("/api/soccer/daily-matches")
def api_soccer_daily_matches() -> Any:
    all_matches = resolve_all_matches_arg(request.args)
    selected, invalid = parse_requested_date(request.args.get("date"))
    if all_matches:
        if invalid or selected is None:
            selected = hong_kong_today()
    elif invalid or selected is None:
        return jsonify({"ok": False, "error": "invalid_date"}), 400
    try:
        layout = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_DAILY_MATCHES)
    except SoccerLayoutError:
        layout = default_layout_settings(LAYOUT_KEY_DAILY_MATCHES)
    list_state = _soccer_list_query_state(layout)
    try:
        result = fetch_matches_filtered(
            SOCCER_DB_PATH,
            None if all_matches else selected,
            n=list_state["n_raw"],
            q=list_state["q"],
            status=list_state["status"],
            sort_specs=list_state["sort_specs"],
            search_keys=list_state["search_keys"],
        )
    except SoccerDataError as e:
        logging.getLogger(__name__).exception("soccer daily-matches api failed: %s", e)
        return jsonify({"ok": False, "error": "read_error", "detail": str(e)}), 500
    return jsonify(
        {
            "ok": True,
            "date": selected.isoformat(),
            "all_matches": all_matches,
            "matches": result.matches,
            "n": result.n,
            "total_count": result.total_count,
            "q": result.q,
            "status": result.status,
            "status_filter_applied": result.status_filter_applied,
            "status_filter_mapping": {
                "connected": STATUS_FILTER_MAPPING_CONNECTED,
                "reason": (
                    None
                    if STATUS_FILTER_MAPPING_CONNECTED
                    else "source_status_map_unverified"
                ),
            },
        }
    )


@app.get("/api/soccer/layout/daily-matches")
def api_soccer_layout_get() -> Any:
    try:
        settings = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_DAILY_MATCHES)
    except SoccerLayoutError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    return jsonify(
        {
            "ok": True,
            "layout_key": LAYOUT_KEY_DAILY_MATCHES,
            "settings": settings,
            "catalog": catalog_public(LAYOUT_KEY_DAILY_MATCHES),
            "zebra_colors": list(ZEBRA_COLORS),
            "width": {
                "default": COLUMN_WIDTH_DEFAULT,
                "min": COLUMN_WIDTH_MIN,
                "max": COLUMN_WIDTH_MAX,
            },
        }
    )


@app.post("/api/soccer/layout/daily-matches")
def api_soccer_layout_save() -> Any:
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "settings" not in data:
        return jsonify({"ok": False, "error": "invalid_settings"}), 400
    try:
        saved = save_layout_settings(
            SOCCER_DB_PATH, data["settings"], layout_key=LAYOUT_KEY_DAILY_MATCHES
        )
    except SoccerLayoutError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("soccer layout save failed: %s", e)
        return jsonify({"ok": False, "error": "save_failed"}), 500
    return jsonify({"ok": True, "settings": saved})


@app.get("/api/soccer/layout/match-history")
def api_soccer_history_layout_get() -> Any:
    try:
        settings = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_MATCH_HISTORY)
    except SoccerLayoutError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    catalog = catalog_public(LAYOUT_KEY_MATCH_HISTORY, db_path=SOCCER_DB_PATH)
    lang = normalize_lang(session.get("lang"))
    try:
        text_map = load_active_text_map(SOCCER_DB_PATH, lang)
    except Exception:
        text_map = {}
    labels, descs = history_column_i18n_maps(
        text_map, [str(c["key"]) for c in catalog]
    )
    return jsonify(
        {
            "ok": True,
            "layout_key": LAYOUT_KEY_MATCH_HISTORY,
            "settings": settings,
            "catalog": catalog,
            "col_labels": labels,
            "col_descs": descs,
            "zebra_colors": list(ZEBRA_COLORS),
            "width": {
                "default": COLUMN_WIDTH_DEFAULT,
                "min": COLUMN_WIDTH_MIN,
                "max": COLUMN_WIDTH_MAX,
            },
        }
    )


@app.post("/api/soccer/layout/match-history")
def api_soccer_history_layout_save() -> Any:
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "settings" not in data:
        return jsonify({"ok": False, "error": "invalid_settings"}), 400
    try:
        saved = save_layout_settings(
            SOCCER_DB_PATH, data["settings"], layout_key=LAYOUT_KEY_MATCH_HISTORY
        )
    except SoccerLayoutError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("soccer history layout save failed: %s", e)
        return jsonify({"ok": False, "error": "save_failed"}), 500
    return jsonify({"ok": True, "layout_key": LAYOUT_KEY_MATCH_HISTORY, "settings": saved})


@app.post("/api/soccer/history-export")
def api_soccer_history_export() -> Any:
    """Build a history export file from a frozen filter/layout snapshot."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    catalog = catalog_public(LAYOUT_KEY_MATCH_HISTORY, db_path=SOCCER_DB_PATH)
    catalog_keys = [str(c["key"]) for c in catalog]
    try:
        export_req = parse_history_export_request(
            data, catalog_keys=catalog_keys, db_path=SOCCER_DB_PATH
        )
        filename = validate_export_filename(
            str(data.get("filename") or ""), export_req.file_type
        )
        result = build_history_export(SOCCER_DB_PATH, export_req)
    except HistoryExportError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("soccer history export failed: %s", e)
        return jsonify({"ok": False, "error": "export_failed", "detail": str(e)}), 500
    resp = make_response(result.content)
    resp.headers["Content-Type"] = result.mime
    resp.headers["X-HPPV-Export-Rows"] = str(result.row_count)
    # Custom / disposition headers must stay latin-1 safe (no raw CJK).
    resp.headers["X-HPPV-Export-Filename"] = header_safe_export_filename(filename)
    # Informational only; browser writes via showSaveFilePicker using local handle.
    resp.headers["Content-Disposition"] = export_content_disposition(
        filename, file_type=export_req.file_type
    )
    return resp


@app.post("/api/soccer/daily-export")
def api_soccer_daily_export() -> Any:
    """Build a daily-matches export from soccer_daily (never history/footylogic)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    catalog = catalog_public(LAYOUT_KEY_DAILY_MATCHES, db_path=SOCCER_DB_PATH)
    catalog_keys = [str(c["key"]) for c in catalog]
    try:
        try:
            daily_layout = get_layout_settings(SOCCER_DB_PATH, LAYOUT_KEY_DAILY_MATCHES)
        except SoccerLayoutError:
            daily_layout = default_layout_settings(LAYOUT_KEY_DAILY_MATCHES)
        export_req = parse_daily_export_request(
            data, catalog_keys=catalog_keys, db_path=SOCCER_DB_PATH
        )
        filename = validate_export_filename(
            str(data.get("filename") or ""), export_req.file_type
        )
        result = build_daily_export(
            SOCCER_DB_PATH, export_req, layout_settings=daily_layout
        )
    except HistoryExportError as e:
        return jsonify({"ok": False, "error": e.code, "detail": e.detail}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("soccer daily export failed: %s", e)
        return jsonify({"ok": False, "error": "export_failed", "detail": str(e)}), 500
    resp = make_response(result.content)
    resp.headers["Content-Type"] = result.mime
    resp.headers["X-HPPV-Export-Rows"] = str(result.row_count)
    resp.headers["X-HPPV-Export-Filename"] = header_safe_export_filename(filename)
    resp.headers["Content-Disposition"] = export_content_disposition(
        filename, file_type=export_req.file_type
    )
    return resp


@app.get("/settings")
def settings_page() -> str:
    """System settings shell; Phase I only exposes Interface Text tab."""
    return render_template("settings.html")


@app.get("/api/translations")
def api_translations_list() -> Any:
    q = (request.args.get("q") or "").strip()
    status_raw = (request.args.get("status") or "all").strip()
    status = status_raw if status_raw in {"all", "active", "disabled"} else "all"
    try:
        items = list_translations(SOCCER_DB_PATH, q=q, status=status)  # type: ignore[arg-type]
    except Exception as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    return jsonify({"ok": True, "items": items})


@app.post("/api/translations")
def api_translations_create() -> Any:
    data = request.get_json(silent=True) or {}
    try:
        item = create_translation(
            SOCCER_DB_PATH,
            text_key=str(data.get("text_key") or ""),
            zh_hant=str(data.get("zh_hant") or ""),
            en=str(data.get("en") or ""),
            zh_hans=str(data.get("zh_hans") or ""),
        )
    except TranslationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    return jsonify({"ok": True, "item": item})


@app.post("/api/translations/<int:tx_id>/update")
def api_translations_update(tx_id: int) -> Any:
    data = request.get_json(silent=True) or {}
    try:
        item = update_translation(
            SOCCER_DB_PATH,
            tx_id,
            zh_hant=str(data.get("zh_hant") or ""),
            en=str(data.get("en") or ""),
            zh_hans=str(data.get("zh_hans") or ""),
        )
    except TranslationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    return jsonify({"ok": True, "item": item})


@app.post("/api/translations/<int:tx_id>/toggle")
def api_translations_toggle(tx_id: int) -> Any:
    data = request.get_json(silent=True) or {}
    disabled = bool(data.get("isDisable"))
    try:
        item = set_translation_disabled(SOCCER_DB_PATH, tx_id, disabled)
    except TranslationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    return jsonify({"ok": True, "item": item})


@app.get("/rankings")
def rankings() -> str:
    init_db(DB_PATH)
    payload = _latest_rankings_payload()
    rankings_path = (TEMPLATE_DIR / "rankings.html").resolve()
    html = render_template(
        "rankings.html",
        payload=payload,
        ui_version=RANKINGS_UI_VERSION,
        debug_web_app_py=str(Path(__file__).resolve()),
        debug_rankings_html=str(rankings_path),
    )
    resp = make_response(html)
    # 避免瀏覽器長期快取排位頁，導致仍顯示舊版（無 Build 區塊）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/api/horse/<horse_id>/recent_form")
def api_horse_recent_form(horse_id: str):
    init_db(DB_PATH)
    limit_raw = (request.args.get("limit") or "").strip()
    try:
        limit = int(limit_raw) if limit_raw else 10
    except ValueError:
        limit = 10
    if limit <= 0 or limit > 50:
        limit = 10
    # 排位彈窗往績固定用「最新在前」：race_date DESC
    sort_seq_for_recent: list[tuple[str, str]] = [("race_date", "desc")]
    try:
        rows = get_recent_form_for_horse(
            DB_PATH,
            horse_id,
            limit=limit,
            sort_seq=sort_seq_for_recent,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    records: list[dict[str, str]] = []
    for r in rows:
        records.append(
            {
                "race_date": str(r["race_date"] or ""),
                "venue": str(r["venue"] or ""),
                "race_no": str(r["race_no"] or ""),
                "course": str(r["course"] or ""),
                "distance": str(r["distance"] or ""),
                "going": str(r["going"] or ""),
                "class_band": str(r["class_band"] or ""),
                "draw": str(r["draw"] or ""),
                "rating": str(r["rating"] or ""),
                "jockey": str(r["jockey"] or ""),
                "trainer": str(r["trainer"] or ""),
                "finish_pos": str(r["finish_pos"] or ""),
                "running_pos": str(r["running_pos"] or ""),
                "win_odds": str(r["win_odds"] or ""),
                "time": str(r["time"] or ""),
                "prize": str(r["prize"] or ""),
            }
        )
    return jsonify({"ok": True, "records": records})


@app.get("/reports/export")
def export_report():
    report_code = (request.args.get("report") or "").strip()
    if not report_code:
        abort(400, description="missing report code")
    try:
        report_def = reports.get_report_definition(report_code)
    except KeyError:
        abort(404, description=f"unknown report: {report_code}")
    df, filename_prefix = report_def.generate()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name=report_def.sheet_name)
    output.seek(0)
    today_str = date.today().strftime("%Y%m%d")
    filename = f"{filename_prefix}-{today_str}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/horses")
def horses_list() -> str:
    init_db(DB_PATH)
    q = (request.args.get("q") or "").strip()
    horse_id_query = (request.args.get("horse_id") or "").strip()
    panel_state = (request.args.get("panel") or "").strip().lower()
    def _to_number(text: str) -> float:
        t = str(text or "").strip()
        if not t:
            return float("-inf")
        m = re.search(r"-?\d+(?:\.\d+)?", t.replace(",", ""))
        return float(m.group(0)) if m else float("-inf")

    sorters = {
        "race_date": ("Race Date", lambda x: race_date_sort_key(x["race_date"])),
        "venue": ("Venue", lambda x: str(x["venue"] or "").lower()),
        "race_no": ("Race No.", lambda x: _to_number(x["race_no"])),
        "course": ("Course", lambda x: str(x["course"] or "").lower()),
        "distance": ("Distance", lambda x: _to_number(x["distance"])),
        "going": ("Going", lambda x: str(x["going"] or "").lower()),
        "class_band": ("Class", lambda x: str(x["class_band"] or "").lower()),
        "draw": ("Draw", lambda x: _to_number(x["draw"])),
        "rating": ("Rating", lambda x: _to_number(x["rating"])),
        "jockey": ("Jockey", lambda x: str(x["jockey"] or "").lower()),
        "trainer": ("Trainer", lambda x: str(x["trainer"] or "").lower()),
        "finish_pos": ("Finish Pos", lambda x: _to_number(x["finish_pos"])),
        "running_pos": ("Running Pos", lambda x: _to_number(x["running_pos"])),
        "win_odds": ("Win Odds", lambda x: _to_number(x["win_odds"])),
        "time": ("Time", lambda x: str(x["time"] or "")),
        "prize": ("Prize", lambda x: _to_number(x["prize"])),
        "flag": ("Flag", lambda x: str(x["flag"] or "").lower()),
    }

    pp_presets = load_pp_preset_list()
    pp_preset_id_param = (request.args.get("pp_preset_id") or "").strip()
    active_preset = find_preset(pp_presets, pp_preset_id_param) or pp_presets[0]
    pp_active_preset_id = str(active_preset["id"])
    active_sort_source = [
        (sk, dk)
        for sk, dk in (active_preset.get("sort_seq") or [])
        if sk in sorters and dk in ("asc", "desc")
    ][:3]
    active_show_source = [k for k in (active_preset.get("show_cols") or []) if k in sorters]
    pp_visible_request = (request.args.get("pp_visible") or "").strip()

    raw_seq: list[tuple[str, str]] = []
    for i in (1, 2, 3):
        sk = (request.args.get(f"sort{i}") or "").strip()
        dk = (request.args.get(f"dir{i}") or "asc").strip().lower()
        if dk not in ("asc", "desc"):
            dk = "asc"
        if sk and sk in sorters:
            raw_seq.append((sk, dk))
    # dedupe keys, keep first occurrence, max 3 keys
    seen: set[str] = set()
    sort_seq: list[tuple[str, str]] = []
    for key, direction in raw_seq:
        if key in seen:
            continue
        seen.add(key)
        sort_seq.append((key, direction))
        if len(sort_seq) >= 3:
            break
    if not sort_seq:
        seen_pp: set[str] = set()
        for sk, dk in active_sort_source:
            if sk not in sorters or dk not in ("asc", "desc"):
                continue
            if sk in seen_pp:
                continue
            seen_pp.add(sk)
            sort_seq.append((sk, dk))
            if len(sort_seq) >= 3:
                break
        if not sort_seq:
            sort_seq = [("race_date", "desc")]

    clicked_col = (request.args.get("clicked_col") or "").strip()
    selected_col = (request.args.get("selected_col") or (sort_seq[0][0] if sort_seq else "")).strip()
    if clicked_col and clicked_col in sorters:
        idx = next((i for i, (k, _d) in enumerate(sort_seq) if k == clicked_col), -1)
        if idx != -1:
            k, d = sort_seq[idx]
            sort_seq[idx] = (k, "desc" if d == "asc" else "asc")
        else:
            default_dir = "desc" if clicked_col == "race_date" else "asc"
            # 新欄位接在排序鍵列表**末尾**（先點的欄位維持在較前），避免「永遠插在主鍵後面」
            # 把先前點的副鍵（如 jockey）擠到後面。
            insert_pos = len(sort_seq)
            sort_seq.insert(insert_pos, (clicked_col, default_dir))
            # 最多 3 鍵：保留主鍵 sort_seq[0]，超出則從索引 1 開始移除最舊的副鍵
            while len(sort_seq) > 3:
                sort_seq.pop(1)

    rows = fetch_horses(DB_PATH, limit=None)

    def _resolve_query_to_horse_id(keyword: str) -> str:
        kw = keyword.strip()
        if not kw:
            return ""
        kw_up = kw.upper()
        # 優先：HorseId / Brand No. 精準匹配
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            brand = str(r["brand_no"] or "").strip()
            if hid.upper() == kw_up:
                return hid
            if brand.upper() == kw_up:
                return hid
        # 次優先：中文名包含
        for r in rows:
            zh = str(r["name_zh"] or "").strip()
            if kw and kw in zh:
                return str(r["horse_id"] or "")
        # 次優先：英文名包含（不分大小寫）
        for r in rows:
            en = str(r["name_en"] or "").strip().upper()
            if kw_up and kw_up in en:
                return str(r["horse_id"] or "")
        # 最後 fallback：沿用既有搜尋函式的第一筆
        hit = search_horses_by_keyword(DB_PATH, kw, limit=1)
        if hit:
            return str(hit[0]["horse_id"] or "")
        return ""

    selected_horse_id = ""
    if rows:
        valid_ids = {str(r["horse_id"] or "") for r in rows}
        resolved_by_q = _resolve_query_to_horse_id(q) if q else ""
        # 有輸入關鍵字時，優先用 q 定位；找不到才回退到 horse_id 參數。
        if resolved_by_q and resolved_by_q in valid_ids:
            selected_horse_id = resolved_by_q
        elif horse_id_query and horse_id_query in valid_ids:
            selected_horse_id = horse_id_query
        else:
            selected_horse_id = str(rows[0]["horse_id"] or "")

    records_view: list[dict[str, str]] = []
    selected_status = ""
    if selected_horse_id:
        selected_status = get_horse_status(DB_PATH, selected_horse_id)
        selected_records = fetch_race_records(DB_PATH, selected_horse_id)
        rows_unsorted = [
            {
                "race_date": format_race_date_display(r.race_date),
                "venue": r.venue,
                "race_no": r.race_no,
                "course": r.course,
                "distance": r.distance,
                "going": r.going,
                "class_band": r.class_band,
                "draw": r.draw,
                "rating": r.rating,
                "jockey": r.jockey,
                "trainer": r.trainer,
                "finish_pos": r.finish_pos,
                "running_pos": r.running_pos,
                "win_odds": r.win_odds,
                "time": r.time,
                "prize": r.prize,
                "flag": selected_status,
            }
            for r in selected_records
        ]
        records_view = list(rows_unsorted)
        # stable multi-key sort: apply from lowest priority to highest
        for key, direction in reversed(sort_seq):
            records_view.sort(key=sorters[key][1], reverse=(direction == "desc"))

    base_header_params: dict[str, str] = {"q": q, "horse_id": selected_horse_id}
    if pp_active_preset_id:
        base_header_params["pp_preset_id"] = pp_active_preset_id
    if panel_state == "open":
        base_header_params["panel"] = "open"
    if sort_seq:
        base_header_params["selected_col"] = sort_seq[0][0]
    for idx, (sk, dk) in enumerate(sort_seq, start=1):
        base_header_params[f"sort{idx}"] = sk
        base_header_params[f"dir{idx}"] = dk
    sort_labels = {k: v[0] for k, v in sorters.items()}
    sort_summary = "  ".join(
        [f"{i+1}. {sort_labels[k]}({'↑' if d == 'asc' else '↓'})" for i, (k, d) in enumerate(sort_seq)]
    )
    sort_state: dict[str, dict[str, Any]] = {}
    for i, (k, d) in enumerate(sort_seq, start=1):
        sort_state[k] = {"order": i, "dir": d, "selected": True}
    for k in sorters.keys():
        if k not in sort_state:
            sort_state[k] = {"order": 999, "dir": "asc", "selected": False}

    pp_show_cols_default = list(active_show_source)
    if not pp_show_cols_default:
        pp_show_cols_default = list(sorters.keys())
    if pp_visible_request:
        parsed_vis: list[str] = []
        seen_vis: set[str] = set()
        for part in pp_visible_request.split(","):
            vk = part.strip()
            if vk in sorters and vk not in seen_vis:
                seen_vis.add(vk)
                parsed_vis.append(vk)
        if parsed_vis:
            pp_show_cols_default = parsed_vis
    pp_sort_seq_default = [
        (sk, dk)
        for sk, dk in active_sort_source
        if sk in sorters and dk in ("asc", "desc")
    ][:3]

    pp_presets_meta = [{"id": p["id"], "name": p["name"]} for p in pp_presets]

    pp_visible_param = ""
    if set(pp_show_cols_default) != set(active_show_source):
        pp_visible_param = ",".join(pp_show_cols_default)
    if pp_visible_param:
        base_header_params["pp_visible"] = pp_visible_param
    base_header_qs = urlencode(base_header_params)

    return render_template(
        "horses_list.html",
        horses=rows,
        q=q,
        selected_horse_id=selected_horse_id,
        selected_status=selected_status,
        records=records_view,
        sort_seq=sort_seq,
        sort_labels=sort_labels,
        sort_summary=sort_summary,
        sort_state=sort_state,
        base_header_qs=base_header_qs,
        panel_state=panel_state,
        pp_presets_meta=pp_presets_meta,
        pp_active_preset_id=pp_active_preset_id,
        pp_visible_param=pp_visible_param,
        pp_show_cols_default=pp_show_cols_default,
        pp_sort_seq_default=pp_sort_seq_default,
        sortable_fields=[
            ("race_date", "Race Date"),
            ("jockey", "Jockey"),
            ("trainer", "Trainer"),
            ("class_band", "Class"),
            ("rating", "Rating"),
            ("win_odds", "Odds"),
            ("draw", "Draw"),
            ("race_no", "Race No."),
            ("distance", "Distance"),
            ("finish_pos", "Finish Pos"),
            ("running_pos", "Running Pos"),
            ("time", "Time"),
            ("prize", "Prize"),
            ("venue", "Venue"),
            ("course", "Course"),
            ("going", "Going"),
            ("flag", "Flag"),
        ],
    )


@app.post("/horses/save_pp_preset")
def save_pp_preset() -> tuple[Any, int] | Any:
    """建立／更新往績 preset 列表；無 mode 時相容第 2 階段（覆寫 default）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    show_raw = data.get("show_cols")
    sort_raw = data.get("sort_seq")
    if not isinstance(show_raw, list):
        show_raw = []
    if not isinstance(sort_raw, list):
        sort_raw = []

    show_cols: list[str] = []
    seen_show: set[str] = set()
    for k in show_raw:
        if not isinstance(k, str) or k not in HORSES_RECORD_SORT_KEYS:
            continue
        if k in seen_show:
            continue
        seen_show.add(k)
        show_cols.append(k)

    sort_seq_tuples: list[tuple[str, str]] = []
    seen_sort: set[str] = set()
    for item in sort_raw[:10]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        sk = str(item[0]).strip()
        dk = str(item[1]).strip().lower()
        if sk not in HORSES_RECORD_SORT_KEYS or dk not in ("asc", "desc"):
            continue
        if sk in seen_sort:
            continue
        seen_sort.add(sk)
        sort_seq_tuples.append((sk, dk))
        if len(sort_seq_tuples) >= 3:
            break

    if not show_cols:
        return jsonify({"ok": False, "error": "no_show_cols"}), 400
    if not sort_seq_tuples:
        sort_seq_tuples = [("race_date", "desc")]

    mode = str(data.get("mode") or "").lower()

    try:
        if mode == "create":
            name = (data.get("name") or "").strip()
            if not name:
                return jsonify({"ok": False, "error": "name_required"}), 400
            presets = load_pp_preset_list()
            existing_ids = {str(p["id"]) for p in presets}
            new_id = slug_preset_id(name, existing_ids)
            presets.append(
                {
                    "id": new_id,
                    "name": name,
                    "is_builtin": False,
                    "show_cols": list(show_cols),
                    "sort_seq": list(sort_seq_tuples),
                }
            )
            save_pp_preset_list(presets)
            return jsonify({"ok": True, "id": new_id})

        if mode == "update":
            preset_id = (data.get("id") or "").strip()
            if not preset_id:
                return jsonify({"ok": False, "error": "id_required"}), 400
            presets = load_pp_preset_list()
            target = find_preset(presets, preset_id)
            if target is None:
                return jsonify({"ok": False, "error": "preset_not_found"}), 404
            target["show_cols"] = list(show_cols)
            target["sort_seq"] = list(sort_seq_tuples)
            if preset_id == "default":
                save_pp_preset_default(
                    {"show_cols": list(show_cols), "sort_seq": list(sort_seq_tuples)}
                )
            else:
                save_pp_preset_list(presets)
            return jsonify({"ok": True, "id": preset_id})

        save_pp_preset_default(
            {"show_cols": list(show_cols), "sort_seq": list(sort_seq_tuples)}
        )
    except OSError as e:
        return jsonify({"ok": False, "error": "write_failed", "detail": str(e)}), 500
    return jsonify({"ok": True})


@app.get("/horses/<horse_id>")
def horse_detail(horse_id: str) -> str:
    init_db(DB_PATH)
    horse = fetch_horse_by_id(DB_PATH, horse_id)
    if horse is None:
        abort(404)

    records = fetch_race_records(DB_PATH, horse_id)
    status = get_horse_status(DB_PATH, horse_id)
    records_view = [
        {
            "race_date": format_race_date_display(r.race_date),
            "venue": r.venue,
            "race_no": r.race_no,
            "course": r.course,
            "distance": r.distance,
            "going": r.going,
            "class_band": r.class_band,
            "draw": r.draw,
            "rating": r.rating,
            "jockey": r.jockey,
            "trainer": r.trainer,
            "finish_pos": r.finish_pos,
            "running_pos": r.running_pos,
            "win_odds": r.win_odds,
            "time": r.time,
            "prize": r.prize,
            "flag": status,
        }
        for r in records
    ]
    return render_template(
        "horse_detail.html",
        horse=horse,
        status=status,
        records=records_view,
        records_total=count_race_records(DB_PATH, horse_id),
    )


def main() -> None:
    app.run(host="0.0.0.0", port=800, debug=False)


if __name__ == "__main__":
    main()

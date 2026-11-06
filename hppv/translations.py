from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal

from hppv.schema import (
    TRANSLATIONS_TABLE,
    translations_expected_columns,
    translations_schema_sql,
)
from hppv.soccer_db import get_connection

logger = logging.getLogger(__name__)

LangCode = Literal["zh-Hant", "en", "zh-Hans"]
SUPPORTED_LANGS: tuple[LangCode, ...] = ("zh-Hant", "en", "zh-Hans")
DEFAULT_LANG: LangCode = "zh-Hant"

# External lang code -> DB column
LANG_COLUMN: dict[str, str] = {
    "zh-Hant": "zh_hant",
    "en": "en",
    "zh-Hans": "zh_hans",
}

StatusFilter = Literal["all", "active", "disabled"]


class TranslationError(ValueError):
    """Validation / business rule error for translations maintenance."""


# Seed only used at init; never used as runtime fallback.
INITIAL_TRANSLATIONS: tuple[tuple[str, str, str, str], ...] = (
    # Nav / settings entry
    ("nav.system_settings", "系統設定", "System Settings", "系统设定"),
    ("nav.soccer_daily", "足球每日賽事", "Daily Soccer Matches", "足球每日赛事"),
    ("nav.language", "語言", "Language", "语言"),
    ("lang.zh_hant", "繁體中文", "Traditional Chinese", "繁体中文"),
    ("lang.en", "English", "English", "English"),
    ("lang.zh_hans", "簡體中文", "Simplified Chinese", "简体中文"),
    # Settings page
    ("settings.title", "系統設定", "System Settings", "系统设定"),
    ("settings.tab.interface_text", "介面文字", "Interface Text", "界面文字"),
    # Toolbar / common actions
    ("ui.search", "搜尋", "Search", "搜索"),
    ("ui.status", "狀態", "Status", "状态"),
    ("ui.status.all", "全部", "All", "全部"),
    ("ui.status.active", "生效", "Active", "生效"),
    ("ui.status.disabled", "失效", "Disabled", "失效"),
    ("ui.add", "＋新增", "+ Add", "＋新增"),
    ("ui.edit", "編輯", "Edit", "编辑"),
    ("ui.disable", "失效", "Disable", "失效"),
    ("ui.enable", "生效", "Enable", "生效"),
    ("ui.copy", "複製", "Copy", "复制"),
    ("ui.save", "儲存", "Save", "保存"),
    ("ui.cancel", "取消", "Cancel", "取消"),
    # Table columns
    ("translations.col.id", "識別編號", "ID", "识别编号"),
    ("translations.col.text_key", "文字鍵", "Text Key", "文字键"),
    ("translations.col.zh_hant", "繁體中文", "Traditional Chinese", "繁体中文"),
    ("translations.col.en", "英文", "English", "英文"),
    ("translations.col.zh_hans", "簡體中文", "Simplified Chinese", "简体中文"),
    ("translations.col.status", "狀態", "Status", "状态"),
    # Form labels / dialogs
    ("translations.dialog.add", "新增介面文字", "Add Interface Text", "新增界面文字"),
    ("translations.dialog.edit", "編輯介面文字", "Edit Interface Text", "编辑界面文字"),
    ("translations.dialog.copy", "複製介面文字", "Copy Interface Text", "复制界面文字"),
    ("translations.form.text_key", "文字鍵", "Text Key", "文字键"),
    ("translations.form.zh_hant", "繁體中文", "Traditional Chinese", "繁体中文"),
    ("translations.form.en", "英文", "English", "英文"),
    ("translations.form.zh_hans", "簡體中文", "Simplified Chinese", "简体中文"),
    # Messages
    ("translations.msg.saved", "已儲存", "Saved", "已保存"),
    ("translations.msg.cancelled", "已取消", "Cancelled", "已取消"),
    ("translations.msg.select_required", "請先選取一筆記錄", "Please select a record first", "请先选择一条记录"),
    ("translations.err.key_required", "文字鍵為必填", "Text key is required", "文字键为必填"),
    ("translations.err.key_duplicate", "文字鍵已存在", "Text key already exists", "文字键已存在"),
    ("translations.err.content_required", "三語內容均不可空白", "All language fields are required", "三语内容均不可空白"),
    ("translations.err.not_found", "找不到記錄", "Record not found", "找不到记录"),
    ("translations.empty", "沒有符合條件的記錄", "No matching records", "没有符合条件的记录"),
    # Soccer Phase I surfaces
    ("soccer.daily.title", "足球每日賽事", "Daily Soccer Matches", "足球每日赛事"),
    ("soccer.daily.under_construction", "功能建置中", "Under construction", "功能建设中"),
    # Soccer daily matches UI
    ("soccer.daily.prev_day", "前一天", "Previous day", "前一天"),
    ("soccer.daily.next_day", "後一天", "Next day", "后一天"),
    ("soccer.daily.date", "日期", "Date", "日期"),
    ("soccer.daily.update", "更新資料", "Update data", "更新数据"),
    ("soccer.daily.download_not_available", "下載功能尚未開放", "Download is not available yet", "下载功能尚未开放"),
    ("soccer.daily.sync_hint", "可於「下載資料」Tab 發送請求同步賽事", "Send a request in the Download Data tab to sync matches", "可在「下载数据」Tab 发送请求同步赛事"),
    ("soccer.daily.update_disabled_hint", "請使用「下載資料」Tab 的發送功能同步資料", "Use Send in the Download Data tab to sync data", "请使用「下载数据」Tab 的发送功能同步数据"),
    ("soccer.daily.col.time", "時間", "Time", "时间"),
    ("soccer.daily.col.match_code", "賽事編號", "Match No.", "赛事编号"),
    ("soccer.daily.col.icon", "圖示", "Icon", "图标"),
    ("soccer.daily.col.competition_code", "賽事代碼", "Comp. Code", "赛事代码"),
    ("soccer.daily.col.competition_name", "賽事名稱", "Competition", "赛事名称"),
    ("soccer.daily.col.home", "主隊", "Home", "主队"),
    ("soccer.daily.col.away", "客隊", "Away", "客队"),
    ("soccer.daily.vs", "對", "vs", "对"),
    ("soccer.daily.odds_group", "主客和", "1X2", "主客和"),
    ("soccer.daily.odds.home", "主勝", "Home", "主胜"),
    ("soccer.daily.odds.draw", "和", "Draw", "和"),
    ("soccer.daily.odds.away", "客勝", "Away", "客胜"),
    ("soccer.daily.empty", "此日期尚未有賽事資料", "No match data stored for this date yet", "此日期尚未有赛事数据"),
    ("soccer.daily.read_error", "賽事資料讀取失敗，請稍後重試", "Failed to load match data. Please try again later.", "赛事数据读取失败，请稍后重试"),
    ("soccer.daily.odds_disclaimer", "賠率為資料庫已存資料，非即時更新。", "Odds are stored database values, not live updates.", "赔率为数据库已存数据，非即时更新。"),
    ("soccer.daily.invalid_date", "日期格式無效，請使用 YYYY-MM-DD。", "Invalid date. Please use YYYY-MM-DD.", "日期格式无效，请使用 YYYY-MM-DD。"),
    ("soccer.daily.icon_alt", "足球", "Football", "足球"),
    # Soccer daily tabs / download UI
    ("soccer.daily.tab.matches", "每日賽事", "Daily Matches", "每日赛事"),
    ("soccer.daily.tab.download", "下載資料", "Download Data", "下载数据"),
    ("soccer.daily.tab.tasks", "任務", "Tasks", "任务"),
    ("soccer.daily.tab.layout", "版面設定", "Layout", "版面设定"),
    ("soccer.daily.task.update_odds", "更新賠率", "Update odds", "更新赔率"),
    ("soccer.daily.task.update_matches", "更新賽事", "Update matches", "更新赛事"),
    ("soccer.daily.task.last_updated", "上次更新", "Last updated", "上次更新"),
    ("soccer.daily.task.next_update", "下次更新", "Next update", "下次更新"),
    ("soccer.daily.task.not_scheduled", "尚未排程", "Not scheduled", "尚未排程"),
    ("soccer.daily.task.unit.mins", "mins", "mins", "mins"),
    ("soccer.daily.task.unit.hour", "hour", "hour", "hour"),
    ("soccer.daily.task.unit.day", "day", "day", "day"),
    ("soccer.daily.request_type", "請求類型", "Request type", "请求类型"),
    ("soccer.daily.request_type.matches_odds", "下載賽事及賠率", "Download matches and odds", "下载赛事及赔率"),
    ("soccer.daily.request_type.update_odds_results", "更新賠率及賽果", "Update odds and results", "更新赔率及赛果"),
    ("soccer.daily.request_type.match_results", "下載正式賽果", "Download official results", "下载正式赛果"),
    (
        "soccer.daily.request_type.match_results_by_year",
        "指定下載賽果年份 19??–20??",
        "Download results by year 19??–20??",
        "指定下载赛果年份 19??–20??",
    ),
    ("soccer.daily.ingest_date_range", "日期範圍", "Date range", "日期范围"),
    ("soccer.daily.ingest_completeness_full", "完整性：全量（endIndex=0 且數量相符）", "Completeness: full (endIndex=0 and counts match)", "完整性：全量（endIndex=0 且数量相符）"),
    ("soccer.daily.ingest_completeness_page", "完整性：分頁結果（不可視為全年／全範圍已齊）", "Completeness: paged result (not a full-range download)", "完整性：分页结果（不可视为全年／全范围已齐）"),
    ("soccer.daily.ingest_completeness_incomplete", "完整性：不完整或驗證失敗", "Completeness: incomplete or validation failed", "完整性：不完整或验证失败"),
    ("soccer.daily.ingest_range_hint", "範圍過大可能逾時或截斷；請縮短 startDate／endDate 後再送出（本系統不會自動分段）。", "Large ranges may time out or truncate; shorten startDate/endDate and resend (no automatic splitting).", "范围过大可能超时或截断；请缩短 startDate／endDate 后再送出（本系统不会自动分段）。"),
    ("soccer.daily.ingest_download_vs_saved", "來源下載筆數／成功入庫場數", "Source rows downloaded / matches saved", "来源下载笔数／成功入库场数"),
    ("soccer.daily.filter.status", "狀態", "Status", "状态"),
    ("soccer.daily.filter.results", "賽果", "Results", "赛果"),
    ("soccer.daily.filter.prematch", "未開賽", "Not started", "未开赛"),
    ("soccer.daily.filter.inplay", "比賽中", "In play", "比赛中"),
    ("soccer.daily.filter.void", "無效", "Void", "无效"),
    ("soccer.daily.filter.mapping_pending", "狀態來源映射尚未接入；目前只切換顯示模式，未按來源狀態篩選資料庫記錄。", "Source status mapping is not connected yet; only display mode is switched, database rows are not filtered by source status.", "状态来源映射尚未接入；目前只切换显示模式，未按来源状态筛选数据库记录。"),
    ("soccer.daily.result.void_mark", "球賽無效", "Match void", "球赛无效"),
    ("soccer.daily.result.not_connected", "賽果資料尚未接入", "Match results are not connected yet", "赛果数据尚未接入"),
    ("soccer.daily.result.ht", "上半場", "1st half", "上半场"),
    ("soccer.daily.result.2h", "下半場", "2nd half", "下半场"),
    ("soccer.daily.result.ft", "全場", "Full time", "全场"),
    ("soccer.daily.result.et", "加時", "Extra time", "加时"),
    ("soccer.daily.result.pen", "十二碼", "Penalties", "十二码"),
    ("soccer.daily.col.ht", "上半場", "1st half", "上半场"),
    ("soccer.daily.col.2h", "下半場", "2nd half", "下半场"),
    ("soccer.daily.col.ft", "全場", "Full time", "全场"),
    ("soccer.daily.col.pen", "十二碼", "Penalties", "十二码"),
    ("soccer.daily.col.et", "加時", "Extra time", "加时"),
    ("soccer.daily.col.result_status", "賽果狀態", "Result status", "赛果状态"),
    ("soccer.daily.col.desc.kickoff_hhmm", "開賽時間（香港）", "Kick-off time (HKT)", "开赛时间（香港）"),
    ("soccer.daily.col.desc.match_code", "賽事編號", "Match code", "赛事编号"),
    ("soccer.daily.col.desc.icon", "足球圖示", "Football icon", "足球图标"),
    ("soccer.daily.col.desc.competition_code", "賽事代碼", "Competition code", "赛事代码"),
    ("soccer.daily.col.desc.competition_name_zh", "賽事名稱", "Competition name", "赛事名称"),
    ("soccer.daily.col.desc.home_team", "主隊", "Home team", "主队"),
    ("soccer.daily.col.desc.away_team", "客隊", "Away team", "客队"),
    ("soccer.daily.col.desc.home_odds", "主勝賠率", "Home odds", "主胜赔率"),
    ("soccer.daily.col.desc.draw_odds", "和局賠率", "Draw odds", "和局赔率"),
    ("soccer.daily.col.desc.away_odds", "客勝賠率", "Away odds", "客胜赔率"),
    ("soccer.daily.col.desc.score_ht", "上半場主客入球（正式來源接入前為空）", "1st-half home/away goals (empty until official source connected)", "上半场主客入球（正式来源接入前为空）"),
    ("soccer.daily.col.desc.score_2h", "下半場單獨主客入球（正式來源接入前為空）", "2nd-half period home/away goals (empty until official source connected)", "下半场单独主客入球（正式来源接入前为空）"),
    ("soccer.daily.col.desc.score_ft", "正常時間全場主客入球（正式來源接入前為空）", "Full-time regulation home/away goals (empty until official source connected)", "正常时间全场主客入球（正式来源接入前为空）"),
    ("soccer.daily.col.desc.score_pen", "互射十二碼主客結果（正式來源接入前為空）", "Penalty shoot-out home/away (empty until official source connected)", "互射十二码主客结果（正式来源接入前为空）"),
    ("soccer.daily.col.desc.ui_et", "加時比分（版面保留欄；正式來源含義未核實前不入庫）", "Extra-time score (layout slot retained; not stored until source meaning verified)", "加时比分（版面保留栏；正式来源含义未核实前不入库）"),
    ("soccer.daily.col.desc.result_status", "賽果狀態（與 match_status／odds_status 分開；來源未核實前為空）", "Result status (separate from match_status/odds_status; empty until source verified)", "赛果状态（与 match_status／odds_status 分开；来源未核实前为空）"),
    ("soccer.daily.search_label", "搜尋", "Search", "搜索"),
    ("soccer.daily.search_placeholder", "搜尋目前顯示欄位…", "Search visible columns…", "搜索目前显示栏位…"),
    ("soccer.daily.search_no_match", "沒有符合搜尋條件的賽事", "No matches for this search", "没有符合搜索条件的赛事"),
    ("soccer.daily.pager.first", "首頁", "First page", "首页"),
    ("soccer.daily.pager.prev", "上一頁", "Previous page", "上一页"),
    ("soccer.daily.pager.next", "下一頁", "Next page", "下一页"),
    ("soccer.daily.pager.last", "最後一頁", "Last page", "最后一页"),
    ("soccer.daily.pager.nav", "賽事列表分頁", "Match list pagination", "赛事列表分页"),
    ("soccer.daily.pager.status", "目前場次 / 總場數", "Current match / total matches", "当前场次 / 总场数"),
    ("soccer.daily.ingest_odds_section", "賠率更新結果", "Odds update result", "赔率更新结果"),
    ("soccer.daily.ingest_results_section", "正式賽果入庫結果", "Official results ingest result", "正式赛果入库结果"),
    ("soccer.daily.ingest_results_pending", "賽果尚未接入／未更新。", "Match results are not connected / not updated.", "赛果尚未接入／未更新。"),
    ("soccer.daily.ingest_skipped_missing", "找不到既有賽事（已跳過）", "Existing matches not found (skipped)", "找不到既有赛事（已跳过）"),
    ("soccer.daily.ingest_skipped_status", "非本輪狀態（已跳過）", "Status out of scope this round (skipped)", "非本轮状态（已跳过）"),
    ("soccer.daily.ingest_skipped_unconfirmed", "未確認正式比分（已跳過）", "Official scores not confirmed (skipped)", "未确认正式比分（已跳过）"),
    ("soccer.daily.ingest_skipped_insufficient", "缺必要來源欄位未能建檔（已跳過）", "Missing required source fields; not created (skipped)", "缺必要来源栏位未能建档（已跳过）"),
    ("soccer.daily.ingest_history_inserted", "新建歷史賽事", "Historical matches created", "新建历史赛事"),
    ("soccer.daily.ingest_ht_applied", "已寫入上半場", "Half-time scores written", "已写入上半场"),
    ("soccer.daily.ingest_ft_applied", "已寫入正常時間全場", "Full-time scores written", "已写入正常时间全场"),
    ("soccer.daily.ingest_sh_applied", "已寫入下半場（FT−HT）", "Second-half scores written (FT−HT)", "已写入下半场（FT−HT）"),
    ("soccer.daily.ingest_match_num_total", "來源 matchNumByDate.total", "Source matchNumByDate.total", "来源 matchNumByDate.total"),
    ("soccer.daily.ingest_matches_returned", "回應 matches 數量", "Response matches count", "回应 matches 数量"),
    ("soccer.daily.ingest_no_existing", "回應沒有可更新的既有賽事，未更新資料", "No existing matches to update; no data changed", "回应没有可更新的既有赛事，未更新数据"),
    ("soccer.daily.sync_results_label", "上次正式賽果更新", "Last official results update", "上次正式赛果更新"),
    ("soccer.daily.layout.title", "UI 版面設定", "UI layout settings", "UI 版面设定"),
    ("soccer.daily.layout.table_name_label", "表格名稱", "Table name", "表格名称"),
    ("soccer.daily.layout.table_name_value", "每日賽事", "Daily Matches", "每日赛事"),
    ("soccer.daily.layout.zebra_label", "列表間隔行底色", "Alternating row colour", "列表间隔行底色"),
    ("soccer.daily.layout.zebra_enable", "啟用", "Enable", "启用"),
    ("soccer.daily.layout.columns_title", "欄位顯示排序", "Column display order", "栏位显示排序"),
    ("soccer.daily.layout.save", "儲存版面設定", "Save layout", "保存版面设定"),
    ("soccer.daily.layout.cancel", "取消", "Cancel", "取消"),
    ("soccer.daily.layout.col_visible", "顯示", "Show", "显示"),
    ("soccer.daily.layout.col_name", "欄位名稱", "Column name", "栏位名称"),
    ("soccer.daily.layout.col_desc", "欄名說明", "Description", "栏名说明"),
    ("soccer.daily.layout.col_width", "欄寬", "Width", "栏宽"),
    ("soccer.daily.layout.col_sort", "排序方向", "Sort", "排序方向"),
    ("soccer.daily.layout.sort_empty", "空", "None", "空"),
    ("soccer.daily.layout.sort_asc", "升序", "Ascending", "升序"),
    ("soccer.daily.layout.sort_desc", "降序", "Descending", "降序"),
    ("soccer.daily.layout.move_up", "上移", "Move up", "上移"),
    ("soccer.daily.layout.move_down", "下移", "Move down", "下移"),
    ("soccer.daily.layout.drag", "拖曳", "Drag", "拖曳"),
    ("soccer.daily.layout.saved", "版面設定已儲存", "Layout saved", "版面设定已保存"),
    ("soccer.daily.layout.save_failed", "版面設定儲存失敗", "Failed to save layout", "版面设定保存失败"),
    ("soccer.daily.layout.width_invalid", "欄寬必須為 60 至 600 的整數", "Width must be an integer from 60 to 600", "栏宽必须为 60 至 600 的整数"),
    ("soccer.daily.layout.color.row_alt_gray", "淺灰", "Light grey", "浅灰"),
    ("soccer.daily.layout.color.row_alt_green", "淺綠", "Light green", "浅绿"),
    ("soccer.daily.layout.color.row_alt_blue", "淺藍", "Light blue", "浅蓝"),
    ("soccer.daily.layout.color.row_alt_sand", "淺沙", "Light sand", "浅沙"),
    ("soccer.daily.sync_odds_label", "賠率更新時間", "Odds updated at", "赔率更新时间"),
    ("soccer.daily.download_soccer", "下載足球賽事", "Download soccer matches", "下载足球赛事"),
    ("soccer.daily.submit_request", "送出請求", "Submit request", "送出请求"),
    ("soccer.daily.request_label", "Request", "Request", "Request"),
    ("soccer.daily.status_label", "Status", "Status", "Status"),
    ("soccer.daily.request_placeholder", "在此輸入或貼上 GraphQL 請求內容", "Enter or paste the GraphQL request here", "在此输入或粘贴 GraphQL 请求内容"),
    ("soccer.daily.request_not_connected", "下載請求尚未接通，請待確認 endpoint 及請求格式後再啟用。", "Download request is not connected yet. Enable after endpoint and request format are confirmed.", "下载请求尚未接通，请待确认 endpoint 及请求格式后再启用。"),
    ("soccer.daily.download_action_pending", "「下載足球賽事」操作行為尚未確認，暫不執行。", "The Download Soccer Matches action is not confirmed yet and will not run.", "「下载足球赛事」操作行为尚未确认，暂不执行。"),
    ("soccer.daily.load_preset_hint", "載入預設請求至 Request，不會立即下載。", "Loads the default request into Request; does not download immediately.", "加载默认请求至 Request，不会立即下载。"),
    ("soccer.daily.load_preset_loading", "正在載入預設請求…", "Loading default request…", "正在加载默认请求…"),
    ("soccer.daily.load_preset_success", "已載入預設請求，尚未送出", "Default request loaded; not submitted yet", "已加载默认请求，尚未送出"),
    ("soccer.daily.load_preset_empty_query", "尚未設定足球 GraphQL 請求", "Soccer GraphQL request is not configured yet", "尚未设定足球 GraphQL 请求"),
    ("soccer.daily.load_preset_file_missing", "預設請求檔案不存在", "Default request file does not exist", "默认请求文件不存在"),
    ("soccer.daily.load_preset_invalid_json", "預設請求 JSON 格式錯誤", "Default request JSON is invalid", "默认请求 JSON 格式错误"),
    ("soccer.daily.load_preset_invalid_fields", "預設請求內容格式不正確", "Default request content format is incorrect", "默认请求内容格式不正确"),
    ("soccer.daily.load_preset_unsupported", "不支援的請求類型", "Unsupported request type", "不支持的请求类型"),
    ("soccer.daily.load_preset_failed", "預設請求讀取失敗", "Failed to read the default request", "默认请求读取失败"),
    ("soccer.daily.load_preset_overwrite_confirm", "Request 內容已被修改，確定要以預設請求覆蓋嗎？", "Request content was modified. Overwrite it with the default request?", "Request 内容已被修改，确定要以默认请求覆盖吗？"),
    # Send / GraphQL status
    ("soccer.daily.send", "發送", "Send", "发送"),
    ("soccer.daily.sending", "正在發送請求…", "Sending request…", "正在发送请求…"),
    ("soccer.daily.send_complete", "請求完成", "Request completed", "请求完成"),
    ("soccer.daily.send_partial_errors", "請求完成（回應含 GraphQL 錯誤）", "Request completed (response contains GraphQL errors)", "请求完成（回应含 GraphQL 错误）"),
    ("soccer.daily.send_graphql_errors", "GraphQL 回應有錯誤", "GraphQL response has errors", "GraphQL 回应有错误"),
    ("soccer.daily.send_http_error", "上游 HTTP 錯誤", "Upstream HTTP error", "上游 HTTP 错误"),
    ("soccer.daily.send_non_json", "回應不是 JSON", "Response is not JSON", "回应不是 JSON"),
    ("soccer.daily.send_timeout", "請求逾時", "Request timed out", "请求逾时"),
    ("soccer.daily.send_connection_failed", "連線失敗", "Connection failed", "连接失败"),
    ("soccer.daily.send_rate_limited", "請求過於頻繁，請稍後再試", "Too many requests; please try again later", "请求过于频繁，请稍后再试"),
    ("soccer.daily.send_invalid_json", "JSON 格式錯誤", "Invalid JSON", "JSON 格式错误"),
    ("soccer.daily.send_invalid_payload", "請求內容不正確", "Request payload is invalid", "请求内容不正确"),
    ("soccer.daily.send_operation_not_allowed", "只允許 GraphQL query", "Only GraphQL query is allowed", "只允许 GraphQL query"),
    ("soccer.daily.send_payload_too_large", "請求內容過大", "Request payload is too large", "请求内容过大"),
    ("soccer.daily.status_result", "執行結果", "Result", "执行结果"),
    ("soccer.daily.status_http", "HTTP 狀態", "HTTP status", "HTTP 状态"),
    ("soccer.daily.status_elapsed", "耗時", "Elapsed", "耗时"),
    ("soccer.daily.status_body", "回應內容", "Response body", "回应内容"),
    ("soccer.daily.status_truncated", "（回應已截斷）", "(response truncated)", "（回应已截断）"),
    ("soccer.daily.send_no_matches", "請求完成（未見賽事資料）", "Request completed (no matches found)", "请求完成（未见赛事数据）"),
    # Ingest / sync time
    ("soccer.daily.updated_at_label", "更新資料時間", "Data updated at", "更新数据时间"),
    ("soccer.daily.updated_at_never", "尚未更新", "Not updated yet", "尚未更新"),
    ("soccer.daily.ingest_success", "入庫成功", "Ingest succeeded", "入库成功"),
    ("soccer.daily.ingest_failed", "入庫失敗，資料已回復至交易前狀態", "Ingest failed; database rolled back", "入库失败，数据已恢复至交易前状态"),
    ("soccer.daily.ingest_skipped", "未入庫", "Not ingested", "未入库"),
    ("soccer.daily.ingest_processed", "處理賽事", "Matches processed", "处理赛事"),
    ("soccer.daily.ingest_inserted", "新增", "Inserted", "新增"),
    ("soccer.daily.ingest_updated", "更新", "Updated", "更新"),
    ("soccer.daily.ingest_had_ok", "採用完整 HAD 賠率", "Complete HAD odds applied", "采用完整 HAD 赔率"),
    ("soccer.daily.ingest_had_missing", "未取得完整 HAD 賠率", "Complete HAD odds missing", "未取得完整 HAD 赔率"),
    ("soccer.daily.ingest_empty", "請求完成，回應沒有賽事，未執行入庫", "Request completed with no matches; ingest skipped", "请求完成，回应没有赛事，未执行入库"),
    ("soccer.daily.ingest_structure", "回應結構不符，未入庫", "Response structure mismatch; not ingested", "回应结构不符，未入库"),
    ("soccer.daily.ingest_truncated", "回應已截斷，未入庫", "Response truncated; not ingested", "回应已截断，未入库"),
    ("soccer.daily.ingest_graphql_errors", "GraphQL 有錯誤，未入庫", "GraphQL errors present; not ingested", "GraphQL 有错误，未入库"),
    ("soccer.daily.ingest_validation", "資料驗證失敗，未入庫", "Data validation failed; not ingested", "数据验证失败，未入库"),
    ("soccer.daily.ingest_init_required", "資料庫未初始化（缺少同步狀態表），請執行初始化", "Database not initialized (sync state table missing); please run init", "数据库未初始化（缺少同步状态表），请执行初始化"),
    ("soccer.daily.ingest_section", "入庫結果", "Ingest result", "入库结果"),
    ("soccer.daily.request_result", "請求結果", "Request result", "请求结果"),
)


def normalize_lang(lang: str | None) -> LangCode:
    if lang in LANG_COLUMN:
        return lang  # type: ignore[return-value]
    return DEFAULT_LANG


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def report_translations_schema_diff(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1;",
        (TRANSLATIONS_TABLE,),
    ).fetchone()
    if row is None:
        return None
    cols = {
        str(r["name"])
        for r in conn.execute(f"PRAGMA table_info({_quote_ident(TRANSLATIONS_TABLE)});")
    }
    expected = translations_expected_columns()
    missing = sorted(expected - cols)
    unexpected = sorted(cols - expected)
    if not missing and not unexpected:
        return None
    # Allow additive migration only for isDisable when that is the sole missing column.
    if missing == ["isDisable"] and not unexpected:
        return None
    parts = [f"表 `{TRANSLATIONS_TABLE}` 結構與預期不符。"]
    if missing:
        parts.append("缺少欄位: " + ", ".join(missing))
    if unexpected:
        parts.append("多餘欄位: " + ", ".join(unexpected))
    parts.append("不會刪表或重建；請手動處理後再初始化。")
    return "\n".join(parts)


def ensure_translations_schema(conn: sqlite3.Connection) -> None:
    """Create translations table / add isDisable if missing. Idempotent."""
    diff = report_translations_schema_diff(conn)
    if diff is not None:
        from hppv.soccer_db import SoccerSchemaMismatchError

        raise SoccerSchemaMismatchError(diff)

    conn.executescript(translations_schema_sql())
    cols = {
        str(r["name"])
        for r in conn.execute(f"PRAGMA table_info({_quote_ident(TRANSLATIONS_TABLE)});")
    }
    if "isDisable" not in cols:
        conn.execute(
            f'ALTER TABLE {_quote_ident(TRANSLATIONS_TABLE)} '
            f'ADD COLUMN {_quote_ident("isDisable")} INTEGER NOT NULL DEFAULT 0;'
        )


def seed_initial_translations(conn: sqlite3.Connection) -> int:
    """Insert missing seed keys only; never overwrite existing rows. Returns inserted count."""
    inserted = 0
    for text_key, zh_hant, en, zh_hans in INITIAL_TRANSLATIONS:
        exists = conn.execute(
            "SELECT 1 FROM translations WHERE text_key = ? LIMIT 1;",
            (text_key,),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO translations (text_key, zh_hant, en, zh_hans, isDisable)
            VALUES (?, ?, ?, ?, 0);
            """,
            (text_key, zh_hant, en, zh_hans),
        )
        inserted += 1
    return inserted


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "TxId": int(row["TxId"]),
        "text_key": str(row["text_key"]),
        "zh_hant": str(row["zh_hant"]),
        "en": str(row["en"]),
        "zh_hans": str(row["zh_hans"]),
        "isDisable": bool(int(row["isDisable"])),
    }


def list_translations(
    db_path: Path,
    *,
    q: str = "",
    status: StatusFilter = "all",
) -> list[dict[str, Any]]:
    """List translations for maintenance UI (includes disabled)."""
    clauses: list[str] = []
    params: list[Any] = []
    if status == "active":
        clauses.append("isDisable = 0")
    elif status == "disabled":
        clauses.append("isDisable = 1")

    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            "(text_key LIKE ? OR zh_hant LIKE ? OR en LIKE ? OR zh_hans LIKE ?)"
        )
        params.extend([like, like, like, like])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT TxId, text_key, zh_hant, en, zh_hans, isDisable "
        f"FROM translations{where} ORDER BY text_key COLLATE NOCASE;"
    )
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]


def get_translation_by_id(db_path: Path, tx_id: int) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT TxId, text_key, zh_hant, en, zh_hans, isDisable "
            "FROM translations WHERE TxId = ?;",
            (tx_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def _require_nonempty(label: str, value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise TranslationError(label)
    return text


def create_translation(
    db_path: Path,
    *,
    text_key: str,
    zh_hant: str,
    en: str,
    zh_hans: str,
) -> dict[str, Any]:
    key = _require_nonempty("key_required", text_key)
    zh_hant_v = _require_nonempty("content_required", zh_hant)
    en_v = _require_nonempty("content_required", en)
    zh_hans_v = _require_nonempty("content_required", zh_hans)
    try:
        with get_connection(db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO translations (text_key, zh_hant, en, zh_hans, isDisable)
                VALUES (?, ?, ?, ?, 0);
                """,
                (key, zh_hant_v, en_v, zh_hans_v),
            )
            tx_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT TxId, text_key, zh_hant, en, zh_hans, isDisable "
                "FROM translations WHERE TxId = ?;",
                (tx_id,),
            ).fetchone()
    except sqlite3.IntegrityError as e:
        raise TranslationError("key_duplicate") from e
    assert row is not None
    return row_to_dict(row)


def update_translation(
    db_path: Path,
    tx_id: int,
    *,
    zh_hant: str,
    en: str,
    zh_hans: str,
) -> dict[str, Any]:
    zh_hant_v = _require_nonempty("content_required", zh_hant)
    en_v = _require_nonempty("content_required", en)
    zh_hans_v = _require_nonempty("content_required", zh_hans)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE translations
            SET zh_hant = ?, en = ?, zh_hans = ?
            WHERE TxId = ?;
            """,
            (zh_hant_v, en_v, zh_hans_v, tx_id),
        )
        if cur.rowcount == 0:
            raise TranslationError("not_found")
        row = conn.execute(
            "SELECT TxId, text_key, zh_hant, en, zh_hans, isDisable "
            "FROM translations WHERE TxId = ?;",
            (tx_id,),
        ).fetchone()
    assert row is not None
    return row_to_dict(row)


def set_translation_disabled(db_path: Path, tx_id: int, disabled: bool) -> dict[str, Any]:
    flag = 1 if disabled else 0
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE translations SET isDisable = ? WHERE TxId = ?;",
            (flag, tx_id),
        )
        if cur.rowcount == 0:
            raise TranslationError("not_found")
        row = conn.execute(
            "SELECT TxId, text_key, zh_hant, en, zh_hans, isDisable "
            "FROM translations WHERE TxId = ?;",
            (tx_id,),
        ).fetchone()
    assert row is not None
    return row_to_dict(row)


def load_active_text_map(db_path: Path, lang: str) -> dict[str, str]:
    """Load active translations for one language (request-scoped use)."""
    lang_n = normalize_lang(lang)
    col = LANG_COLUMN[lang_n]
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT text_key, {_quote_ident(col)} AS txt "
            "FROM translations WHERE isDisable = 0;"
        ).fetchall()
    return {str(r["text_key"]): str(r["txt"]) for r in rows}


def translate(text_map: dict[str, str], key: str) -> str:
    """Resolve one key; missing/disabled -> [key] + warning. No cross-lang fallback."""
    value = text_map.get(key)
    if value is None or value == "":
        logger.warning("Missing or disabled translation key: %s", key)
        return f"[{key}]"
    return value


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
    ("nav.soccer_daily", "足球資料庫", "Soccer Database", "足球数据库"),
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
    ("soccer.daily.title", "足球資料庫", "Soccer Database", "足球数据库"),
    ("soccer.daily.under_construction", "功能建置中", "Under construction", "功能建设中"),
    # Soccer daily matches UI
    ("soccer.daily.prev_day", "前一天", "Previous day", "前一天"),
    ("soccer.daily.next_day", "後一天", "Next day", "后一天"),
    ("soccer.daily.date", "日期", "Date", "日期"),
    ("soccer.daily.update", "更新資料", "Update data", "更新数据"),
    ("soccer.daily.download_not_available", "下載功能尚未開放", "Download is not available yet", "下载功能尚未开放"),
    ("soccer.daily.sync_hint", "可於「下載資料」Tab 發送請求同步賽事", "Send a request in the Download Data tab to sync matches", "可在「下载数据」Tab 发送请求同步赛事"),
    ("soccer.daily.update_disabled_hint", "請使用「下載資料」Tab 的發送功能同步資料", "Use Send in the Download Data tab to sync data", "请使用「下载数据」Tab 的发送功能同步数据"),
    ("soccer.daily.col.date", "比賽日期", "Match Date", "比赛日期"),
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
    ("soccer.daily.tab.history", "球賽往績", "Match History", "球赛往绩"),
    (
        "soccer.history.empty",
        "沒有符合條件的往績賽事",
        "No historical matches for this filter",
        "没有符合条件的往绩赛事",
    ),
    (
        "soccer.history.read_error",
        "往績資料讀取失敗，請稍後重試",
        "Failed to load match history. Please try again later.",
        "往绩数据读取失败，请稍后重试",
    ),
    (
        "soccer.history.hint",
        "資料來自 Footylogic 往績庫（唯讀），與每日賽事分開。",
        "Data is from the Footylogic history store (read-only), separate from Daily Matches.",
        "数据来自 Footylogic 往绩库（只读），与每日赛事分开。",
    ),
    (
        "soccer.history.search_placeholder",
        "搜尋主隊或客隊…",
        "Search home or away team…",
        "搜索主队或客队…",
    ),
    (
        "soccer.history.search_op.label",
        "條件",
        "Condition",
        "条件",
    ),
    (
        "soccer.history.search_op.approx",
        "近似",
        "Approximate",
        "近似",
    ),
    (
        "soccer.history.search_op.eq",
        "等於",
        "Equals",
        "等于",
    ),
    (
        "soccer.history.search_op.ne",
        "不等於",
        "Not equal",
        "不等于",
    ),
    (
        "soccer.history.search_op.contains",
        "包含",
        "Contains",
        "包含",
    ),
    (
        "soccer.history.search_op.not_contains",
        "不包含",
        "Does not contain",
        "不包含",
    ),
    (
        "soccer.history.search_op.starts",
        "開頭是",
        "Starts with",
        "开头是",
    ),
    (
        "soccer.history.search_op.ends",
        "結尾是",
        "Ends with",
        "结尾是",
    ),
    # Hidden operators — kept for future UI; not rendered yet.
    (
        "soccer.history.search_op.gt",
        "大於",
        "Greater than",
        "大于",
    ),
    (
        "soccer.history.search_op.gte",
        "大於等於",
        "Greater than or equal",
        "大于等于",
    ),
    (
        "soccer.history.search_op.lt",
        "小於",
        "Less than",
        "小于",
    ),
    (
        "soccer.history.search_op.lte",
        "小於等於",
        "Less than or equal",
        "小于等于",
    ),
    (
        "soccer.history.search_op.between",
        "介乎",
        "Between",
        "介于",
    ),
    (
        "soccer.history.search_op.empty",
        "為空",
        "Is empty",
        "为空",
    ),
    (
        "soccer.history.search_op.not_empty",
        "不為空",
        "Is not empty",
        "不为空",
    ),
    (
        "soccer.history.pager.status",
        "目前頁 / 總頁數",
        "Current page / total pages",
        "当前页 / 总页数",
    ),
    (
        "soccer.history.pager.records",
        "（{n} 筆）",
        "({n} Records)",
        "（{n} 条）",
    ),
    (
        "soccer.history.pager.nav",
        "往績列表分頁",
        "History list pagination",
        "往绩列表分页",
    ),
    (
        "soccer.history.layout.mode_title",
        "球賽往績 — 版面設定",
        "Match History — Layout Settings",
        "球赛往绩 — 版面设置",
    ),
    (
        "soccer.history.layout.table_name_value",
        "球賽往績",
        "Match History",
        "球赛往绩",
    ),
    # Match History column labels (schema keys; presence of columns is catalog-driven).
    ("soccer.history.col.match_date", "比賽日期", "Match Date", "比赛日期"),
    ("soccer.history.col.kickoff_hhmm", "開賽時間", "Kick-off", "开赛时间"),
    ("soccer.history.col.competition_name", "賽事名稱", "Competition", "赛事名称"),
    ("soccer.history.col.home_team", "主隊", "Home", "主队"),
    ("soccer.history.col.away_team", "客隊", "Away", "客队"),
    ("soccer.history.col.score_ht", "半場", "HT", "半场"),
    ("soccer.history.col.score_ft", "全場", "FT", "全场"),
    ("soccer.history.col.source_key", "來源鍵", "Source Key", "来源键"),
    ("soccer.history.col.source", "資料來源", "Source", "数据来源"),
    ("soccer.history.col.competition_id", "賽事 ID", "Competition ID", "赛事 ID"),
    ("soccer.history.col.season_id", "球季 ID", "Season ID", "球季 ID"),
    ("soccer.history.col.month", "月份", "Month", "月份"),
    ("soccer.history.col.kickoff_utc", "開賽時間（UTC）", "Kick-off (UTC)", "开赛时间（UTC）"),
    ("soccer.history.col.has_score", "已有比分", "Has Score", "已有比分"),
    ("soccer.history.col.score_complete", "比分完整", "Score Complete", "比分完整"),
    (
        "soccer.history.col.result_confirm_status",
        "賽果確認狀態",
        "Result Confirm Status",
        "赛果确认状态",
    ),
    ("soccer.history.col.event_json", "事件 JSON", "Event JSON", "事件 JSON"),
    (
        "soccer.history.col.source_last_updated",
        "來源最後更新",
        "Source Last Updated",
        "来源最后更新",
    ),
    ("soccer.history.col.created_at", "建立時間", "Created At", "建立时间"),
    ("soccer.history.col.updated_at", "更新時間", "Updated At", "更新时间"),
    ("soccer.history.col.score_2h_home", "下半場主隊", "2H Home", "下半场主队"),
    ("soccer.history.col.score_2h_away", "下半場客隊", "2H Away", "下半场客队"),
    ("soccer.history.col.score_et_home", "加時主隊", "ET Home", "加时主队"),
    ("soccer.history.col.score_et_away", "加時客隊", "ET Away", "加时客队"),
    ("soccer.history.col.score_pens_home", "互射主隊", "Pens Home", "互射主队"),
    ("soccer.history.col.score_pens_away", "互射客隊", "Pens Away", "互射客队"),
    ("soccer.history.col.final_odds_home", "終盤主勝", "Final Odds Home", "终盘主胜"),
    ("soccer.history.col.final_odds_draw", "終盤和局", "Final Odds Draw", "终盘和局"),
    ("soccer.history.col.final_odds_away", "終盤客勝", "Final Odds Away", "终盘客胜"),
    (
        "soccer.history.col.final_odds_timestamp",
        "終盤時間戳",
        "Final Odds Timestamp",
        "终盘时间戳",
    ),
    (
        "soccer.history.col.final_odds_source",
        "終盤來源",
        "Final Odds Source",
        "终盘来源",
    ),
    # Match History column descriptions
    (
        "soccer.history.col.desc.match_date",
        "比賽日期（footylogic_matches.match_date_hkt）",
        "Match date (footylogic_matches.match_date_hkt)",
        "比赛日期（footylogic_matches.match_date_hkt）",
    ),
    (
        "soccer.history.col.desc.kickoff_hhmm",
        "開賽時間（HKT HH:mm，來自 kickoff_hkt）",
        "Kick-off time (HKT HH:mm from kickoff_hkt)",
        "开赛时间（HKT HH:mm，来自 kickoff_hkt）",
    ),
    (
        "soccer.history.col.desc.competition_name",
        "賽事名稱；缺值顯示 competition_id",
        "Competition name; falls back to competition_id",
        "赛事名称；缺值显示 competition_id",
    ),
    (
        "soccer.history.col.desc.home_team",
        "主隊名稱；缺值顯示 home_team_id",
        "Home team name; falls back to home_team_id",
        "主队名称；缺值显示 home_team_id",
    ),
    (
        "soccer.history.col.desc.away_team",
        "客隊名稱；缺值顯示 away_team_id",
        "Away team name; falls back to away_team_id",
        "客队名称；缺值显示 away_team_id",
    ),
    (
        "soccer.history.col.desc.score_ht",
        "上半場比分（組合顯示）",
        "Half-time score (combined display)",
        "上半场比分（组合显示）",
    ),
    (
        "soccer.history.col.desc.score_ft",
        "全場比分（組合顯示）",
        "Full-time score (combined display)",
        "全场比分（组合显示）",
    ),
    (
        "soccer.history.col.desc.source_key",
        "往績列唯一鍵（來源系統識別）",
        "Unique history row key from the source system",
        "往绩行唯一键（来源系统识别）",
    ),
    (
        "soccer.history.col.desc.source",
        "資料來源標記（例如 footylogic）",
        "Data source marker (e.g. footylogic)",
        "数据来源标记（例如 footylogic）",
    ),
    (
        "soccer.history.col.desc.competition_id",
        "賽事編號（來源 competitionId）",
        "Competition id from source competitionId",
        "赛事编号（来源 competitionId）",
    ),
    (
        "soccer.history.col.desc.season_id",
        "球季編號（來源 seasonId）",
        "Season id from source seasonId",
        "球季编号（来源 seasonId）",
    ),
    (
        "soccer.history.col.desc.month",
        "歸檔月份鍵（YYYY-MM，用於增量下載）",
        "Archive month key (YYYY-MM) used for incremental download",
        "归档月份键（YYYY-MM，用于增量下载）",
    ),
    (
        "soccer.history.col.desc.kickoff_utc",
        "開賽時間（UTC ISO 字串）",
        "Kick-off time as UTC ISO string",
        "开赛时间（UTC ISO 字符串）",
    ),
    (
        "soccer.history.col.desc.has_score",
        "是否已寫入任何比分欄位",
        "Whether any score fields are present",
        "是否已写入任何比分字段",
    ),
    (
        "soccer.history.col.desc.score_complete",
        "比分是否被視為完整可顯示",
        "Whether the score is treated as complete for display",
        "比分是否被视为完整可显示",
    ),
    (
        "soccer.history.col.desc.result_confirm_status",
        "賽果確認／衍生狀態代碼",
        "Result confirmation / derivation status code",
        "赛果确认／衍生状态代码",
    ),
    (
        "soccer.history.col.desc.event_json",
        "原始事件 JSON（體積大，預設隱藏）",
        "Raw event JSON (large; hidden by default)",
        "原始事件 JSON（体积大，默认隐藏）",
    ),
    (
        "soccer.history.col.desc.source_last_updated",
        "來源端最後更新時間",
        "Last update time reported by the source",
        "来源端最后更新时间",
    ),
    (
        "soccer.history.col.desc.created_at",
        "本列寫入本地庫的時間",
        "When this row was first written locally",
        "本行写入本地库的时间",
    ),
    (
        "soccer.history.col.desc.updated_at",
        "本列最近更新時間",
        "When this row was last updated locally",
        "本行最近更新时间",
    ),
    (
        "soccer.history.col.desc.score_2h_home",
        "下半場主隊進球（衍生／來源欄）",
        "Second-half home goals (derived or source)",
        "下半场主队进球（衍生／来源字段）",
    ),
    (
        "soccer.history.col.desc.score_2h_away",
        "下半場客隊進球（衍生／來源欄）",
        "Second-half away goals (derived or source)",
        "下半场客队进球（衍生／来源字段）",
    ),
    (
        "soccer.history.col.desc.score_et_home",
        "加時賽主隊進球",
        "Extra-time home goals",
        "加时赛主队进球",
    ),
    (
        "soccer.history.col.desc.score_et_away",
        "加時賽客隊進球",
        "Extra-time away goals",
        "加时赛客队进球",
    ),
    (
        "soccer.history.col.desc.score_pens_home",
        "互射十二碼主隊進球",
        "Penalty-shootout home goals",
        "互射十二码主队进球",
    ),
    (
        "soccer.history.col.desc.score_pens_away",
        "互射十二碼客隊進球",
        "Penalty-shootout away goals",
        "互射十二码客队进球",
    ),
    (
        "soccer.history.col.desc.final_odds_home",
        "終盤主勝賠率",
        "Final home win odds",
        "终盘主胜赔率",
    ),
    (
        "soccer.history.col.desc.final_odds_draw",
        "終盤和局賠率",
        "Final draw odds",
        "终盘和局赔率",
    ),
    (
        "soccer.history.col.desc.final_odds_away",
        "終盤客勝賠率",
        "Final away win odds",
        "终盘客胜赔率",
    ),
    (
        "soccer.history.col.desc.final_odds_timestamp",
        "終盤賠率時間戳",
        "Timestamp of the final odds snapshot",
        "终盘赔率时间戳",
    ),
    (
        "soccer.history.col.desc.final_odds_source",
        "終盤賠率來源標記",
        "Source marker for the final odds snapshot",
        "终盘赔率来源标记",
    ),
    (
        "soccer.history.layout.sort_unsupported",
        "此欄位不支援排序",
        "Sort is not supported for this column",
        "此栏位不支持排序",
    ),
    ("soccer.history.export.tip", "匯出資料", "Export data", "导出数据"),
    ("soccer.daily.export.tip", "匯出資料", "Export data", "导出数据"),
    (
        "soccer.daily.export.title",
        "每日賽事 — 匯出資料",
        "Daily Matches — Export Data",
        "每日赛事 — 导出数据",
    ),
    (
        "soccer.history.export.title",
        "球賽往績 — 匯出資料",
        "Match History — Export Data",
        "球赛往绩 — 导出数据",
    ),
    (
        "soccer.history.export.output_path",
        "輸出路徑",
        "Output path",
        "输出路径",
    ),
    (
        "soccer.history.export.path_unselected",
        "尚未選擇儲存位置",
        "No save location selected",
        "尚未选择保存位置",
    ),
    (
        "soccer.history.export.path_selected",
        "已選擇本機儲存位置",
        "Local save location selected",
        "已选择本机保存位置",
    ),
    (
        "soccer.history.export.path_hint",
        "完整路徑請在系統「另存新檔」視窗確認。",
        "Confirm the full path in the system Save As dialog.",
        "完整路径请在系统“另存为”窗口确认。",
    ),
    (
        "soccer.history.export.path_need_reselect",
        "檔名或格式已變更，請重新瀏覽選擇儲存位置。",
        "File name or type changed. Please browse again to choose the save location.",
        "文件名或格式已变更，请重新浏览选择保存位置。",
    ),
    (
        "soccer.history.export.path_required",
        "請先按「瀏覽」選擇本機儲存位置。",
        "Please click Browse first to choose a local save location.",
        "请先按“浏览”选择本机保存位置。",
    ),
    (
        "soccer.history.export.ext_mismatch",
        "另存檔名的副檔名與所選檔案類型不符，請重新瀏覽選擇。",
        "The saved file extension does not match the selected file type. Please browse again.",
        "另存文件名的扩展名与所选文件类型不符，请重新浏览选择。",
    ),
    (
        "soccer.history.export.filename",
        "輸出檔案名稱",
        "Output file name",
        "输出文件名称",
    ),
    ("soccer.history.export.file_type", "檔案類型", "File type", "文件类型"),
    ("soccer.history.export.browse", "瀏覽", "Browse", "浏览"),
    (
        "soccer.history.export.browse_tip",
        "選擇本機儲存位置",
        "Choose local save location",
        "选择本机保存位置",
    ),
    (
        "soccer.history.export.confirm_tip",
        "確定匯出",
        "Confirm export",
        "确定导出",
    ),
    (
        "soccer.history.export.leave_tip",
        "離開",
        "Leave",
        "离开",
    ),
    (
        "soccer.history.export.csv_width_note",
        "CSV 無法保存欄寬或視覺格式；仍會輸出目前顯示欄位、欄位次序及資料排序。",
        "CSV cannot preserve column widths or visual formatting; visible columns, column order and data sort are still applied.",
        "CSV 无法保存栏宽或视觉格式；仍会输出目前显示字段、字段次序及数据排序。",
    ),
    (
        "soccer.history.export.picker_unsupported",
        "此瀏覽器不支援選擇本機儲存位置（需要 File System Access API）。請改用支援的瀏覽器（例如最新版 Chrome 或 Edge）。",
        "This browser cannot choose a local save location (File System Access API required). Please use a supported browser such as the latest Chrome or Edge.",
        "此浏览器不支持选择本机保存位置（需要 File System Access API）。请改用支持的浏览器（例如最新版 Chrome 或 Edge）。",
    ),
    (
        "soccer.history.export.busy",
        "匯出進行中…",
        "Export in progress…",
        "导出进行中…",
    ),
    (
        "soccer.history.export.done",
        "匯出完成。",
        "Export completed.",
        "导出完成。",
    ),
    (
        "soccer.history.export.failed",
        "匯出失敗",
        "Export failed",
        "导出失败",
    ),
    (
        "soccer.history.export.filename_blank",
        "檔案名稱不可空白。",
        "File name cannot be blank.",
        "文件名称不可空白。",
    ),
    (
        "soccer.history.export.filename_illegal",
        "檔案名稱含有不合法字元，請修正後再試。",
        "File name contains illegal characters. Please correct it and try again.",
        "文件名称含有不合法字符，请修正后再试。",
    ),
    (
        "soccer.history.export.jpeg_too_large",
        "資料量超出單張 JPEG 可匯出範圍，請縮小搜尋／篩選範圍，或改用 XLSX／CSV／PDF。",
        "The result set exceeds what a single JPEG can export. Narrow the search/filter, or use XLSX/CSV/PDF instead.",
        "数据量超出单张 JPEG 可导出范围，请缩小搜索／筛选范围，或改用 XLSX／CSV／PDF。",
    ),
    (
        "soccer.history.export.pdf_columns_too_wide",
        "顯示欄位過闊，無法以可讀尺寸匯出 PDF，請減少顯示欄位、調整欄寬，或改用 XLSX。",
        "Visible columns are too wide for a readable PDF. Hide some columns, adjust widths, or use XLSX instead.",
        "显示字段过宽，无法以可读尺寸导出 PDF，请减少显示字段、调整栏宽，或改用 XLSX。",
    ),
    (
        "soccer.history.export.no_visible_columns",
        "目前沒有顯示中的欄位可匯出。",
        "There are no visible columns to export.",
        "目前没有显示中的字段可导出。",
    ),
    (
        "soccer.history.export.read_error",
        "讀取往績資料失敗，請稍後重試。",
        "Failed to read history data. Please try again later.",
        "读取往绩数据失败，请稍后重试。",
    ),
    (
        "soccer.history.export.write_error",
        "寫入本機檔案失敗。",
        "Failed to write the local file.",
        "写入本机文件失败。",
    ),
    (
        "soccer.history.export.picker_cancel",
        "已取消選擇儲存位置。",
        "Save location selection cancelled.",
        "已取消选择保存位置。",
    ),
    ("soccer.daily.tab.download", "下載資料", "Download Data", "下载数据"),
    ("soccer.daily.tab.tasks", "任務", "Tasks", "任务"),
    # Button label on Daily Matches toolbar (not a top-level tab).
    ("soccer.daily.tab.layout", "版面設定", "Layout", "版面设定"),
    ("soccer.daily.task.update_odds", "更新賠率", "Update odds", "更新赔率"),
    ("soccer.daily.task.update_matches", "更新賽事", "Update matches", "更新赛事"),
    ("soccer.daily.task.update_results", "更新賽果", "Update results", "更新赛果"),
    ("soccer.daily.task.last_updated", "上次更新", "Last updated", "上次更新"),
    ("soccer.daily.task.next_update", "下次更新", "Next update", "下次更新"),
    ("soccer.daily.task.not_scheduled", "尚未排程", "Not scheduled", "尚未排程"),
    ("soccer.daily.task.unit.mins", "mins", "mins", "mins"),
    ("soccer.daily.task.unit.hour", "hour", "hour", "hour"),
    ("soccer.daily.task.unit.day", "day", "day", "day"),
    (
        "soccer.daily.task.status.idle",
        "待機",
        "Idle",
        "待机",
    ),
    (
        "soccer.daily.task.status.running",
        "執行中",
        "Running",
        "执行中",
    ),
    (
        "soccer.daily.task.status.success",
        "成功",
        "Success",
        "成功",
    ),
    (
        "soccer.daily.task.status.failed",
        "失敗",
        "Failed",
        "失败",
    ),
    (
        "soccer.daily.task.status.skipped",
        "已跳過",
        "Skipped",
        "已跳过",
    ),
    (
        "soccer.daily.task.save_ok",
        "任務設定已保存。",
        "Task settings saved.",
        "任务设定已保存。",
    ),
    (
        "soccer.daily.task.save_failed",
        "任務設定保存失敗。",
        "Failed to save task settings.",
        "任务设定保存失败。",
    ),
    (
        "soccer.daily.task.scope_note",
        "更新賽事重用 matches_and_odds（來源預設窗口、endIndex=40 切片），非完整球賽下載；不含正式賽果。",
        "Update matches reuses matches_and_odds (source default window, endIndex=40 slice); not a full catalog download; official results excluded.",
        "更新赛事重用 matches_and_odds（来源默认窗口、endIndex=40 切片），非完整球赛下载；不含正式赛果。",
    ),
    (
        "soccer.daily.task.odds_scope_note",
        "更新賠率重用 update_odds_and_results／matches_and_odds.json（endIndex=40）；只更新資料庫既有賽事的 HAD；不新增球賽、不改基本欄位、不含正式賽果。",
        "Update odds reuses update_odds_and_results / matches_and_odds.json (endIndex=40); HAD only on existing DB matches; no INSERT, no basic-field overwrite, no official results.",
        "更新赔率重用 update_odds_and_results／matches_and_odds.json（endIndex=40）；只更新数据库既有赛事的 HAD；不新增球赛、不改基本字段、不含正式赛果。",
    ),
    (
        "soccer.daily.task.results_scope_note",
        "更新賽果重用 match_results；日期窗為每日賽事 MIN/MAX match_date（不完整則按日切塊）。有已確認 FT 即寫入往績（source=hkjc；不依 status）後刪每日並寫 tombstone；僅 HT 不刪。",
        "Update results reuses match_results; date window = daily MIN/MAX match_date (day-chunk if incomplete). Confirmed FT writes history (source=hkjc; status ignored) then deletes daily + tombstone; HT-only keeps daily.",
        "更新赛果重用 match_results；日期窗为每日赛事 MIN/MAX match_date（不完整则按日切块）。有已确认 FT 即写入往绩（source=hkjc；不依 status）后删每日并写 tombstone；仅 HT 不删。",
    ),
    (
        "soccer.daily.task.single_process_note",
        "排程僅支援單程序 Web；多 worker 環境尚未具備跨程序防重複。各任務共用程序內下載／入庫鎖以串列執行。",
        "Scheduler supports single-process Web only; multi-worker duplicate prevention is not available. Tasks share an in-process download/ingest lock and run serially.",
        "排程仅支持单进程 Web；多 worker 环境尚未具备跨进程防重复。各任务共用进程内下载／入库锁以串行执行。",
    ),
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
    ("soccer.daily.ingest_range_hint_match_results", "日期請在 Request 的 startDate／endDate 修改；送出後若整段不完整會按日切塊重抓。自動任務仍只用每日表待處理日期範圍。", "Edit startDate/endDate in the Request box; incomplete windows are re-fetched day by day. The auto task still uses pending daily-table dates only.", "日期请在 Request 的 startDate／endDate 修改；送出后若整段不完整会按日切块重抓。自动任务仍只用每日表待处理日期范围。"),
    ("soccer.daily.ingest_download_vs_saved", "來源下載筆數／成功入庫場數", "Source rows downloaded / matches saved", "来源下载笔数／成功入库场数"),
    ("soccer.daily.download_section", "下載結果", "Download result", "下载结果"),
    ("soccer.daily.download_success", "下載成功", "Download succeeded", "下载成功"),
    ("soccer.daily.download_failed", "下載失敗", "Download failed", "下载失败"),
    ("soccer.daily.ingest_history_section", "往績入庫結果", "History ingest result", "往绩入库结果"),
    ("soccer.daily.ingest_history_success", "往績入庫成功", "History ingest succeeded", "往绩入库成功"),
    ("soccer.daily.ingest_history_no_change", "沒有往績資料變動（0 筆新增／更新）", "No history rows changed (0 inserted/updated)", "没有往绩资料变动（0 笔新增／更新）"),
    ("soccer.daily.ingest_daily_deleted", "每日已刪除", "Daily rows deleted", "每日已删除"),
    ("soccer.daily.ingest_skipped_no_kickoff", "缺有效開賽時間（已跳過）", "Missing valid kick-off time (skipped)", "缺有效开赛时间（已跳过）"),
    ("soccer.daily.ingest_history_updated", "往績更新", "History updated", "往绩更新"),
    ("soccer.daily.ingest_fetch_mode", "下載模式", "Fetch mode", "下载模式"),
    ("soccer.daily.filter.status", "狀態", "Status", "状态"),
    ("soccer.daily.filter.all_matches", "所有賽事", "All Matches", "所有赛事"),
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
    ("soccer.daily.col.desc.match_date", "比賽日期（每場 soccer_daily.match_date）", "Match date (per-row soccer_daily.match_date)", "比赛日期（每场 soccer_daily.match_date）"),
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
    (
        "soccer.daily.layout.mode_title",
        "每日賽事 — 版面設定",
        "Daily Matches — Layout",
        "每日赛事 — 版面设定",
    ),
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


# Product label renames: update only when all three langs still match the previous seed.
SEED_LABEL_RENAMES: tuple[
    tuple[str, tuple[str, str, str], tuple[str, str, str]],
    ...,
] = (
    (
        "nav.soccer_daily",
        ("足球每日賽事", "Daily Soccer Matches", "足球每日赛事"),
        ("足球資料庫", "Soccer Database", "足球数据库"),
    ),
    (
        "soccer.daily.title",
        ("足球每日賽事", "Daily Soccer Matches", "足球每日赛事"),
        ("足球資料庫", "Soccer Database", "足球数据库"),
    ),
    (
        "soccer.daily.col.date",
        ("日期", "Date", "日期"),
        ("比賽日期", "Match Date", "比赛日期"),
    ),
)


def apply_seed_label_renames(conn: sqlite3.Connection) -> int:
    """Apply known product renames without clobbering user-customized labels."""
    updated = 0
    for text_key, old, new in SEED_LABEL_RENAMES:
        cur = conn.execute(
            """
            UPDATE translations
            SET zh_hant = ?, en = ?, zh_hans = ?
            WHERE text_key = ?
              AND zh_hant = ? AND en = ? AND zh_hans = ?;
            """,
            (new[0], new[1], new[2], text_key, old[0], old[1], old[2]),
        )
        updated += int(cur.rowcount or 0)
    return updated


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


def history_column_i18n_maps(
    text_map: dict[str, str],
    catalog_keys: list[str] | tuple[str, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Labels/descriptions for history layout UI and table headers.

    Catalog remains the source of which columns exist. Missing translations
    fall back to the field key (label) or empty string (description).
    """
    labels: dict[str, str] = {}
    descs: dict[str, str] = {}
    for key in catalog_keys:
        raw_label = (text_map.get(f"soccer.history.col.{key}") or "").strip()
        labels[key] = raw_label if raw_label else key
        raw_desc = (text_map.get(f"soccer.history.col.desc.{key}") or "").strip()
        descs[key] = raw_desc
    return labels, descs


# Daily Matches catalog key → translation key (matches soccer_daily.html COL_LABEL).
_DAILY_COLUMN_LABEL_TEXT_KEYS: dict[str, str] = {
    "match_date": "soccer.daily.col.date",
    "kickoff_hhmm": "soccer.daily.col.time",
    "match_code": "soccer.daily.col.match_code",
    "icon": "soccer.daily.col.icon",
    "competition_code": "soccer.daily.col.competition_code",
    "competition_name_zh": "soccer.daily.col.competition_name",
    "home_team": "soccer.daily.col.home",
    "away_team": "soccer.daily.col.away",
    "home_odds": "soccer.daily.odds.home",
    "draw_odds": "soccer.daily.odds.draw",
    "away_odds": "soccer.daily.odds.away",
    "score_ht": "soccer.daily.col.ht",
    "score_2h": "soccer.daily.col.2h",
    "score_ft": "soccer.daily.col.ft",
    "ui_et": "soccer.daily.col.et",
    "score_pen": "soccer.daily.col.pen",
    "result_status": "soccer.daily.col.result_status",
}


def daily_column_i18n_maps(
    text_map: dict[str, str],
    catalog_keys: list[str] | tuple[str, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Labels/descriptions for daily-matches layout UI and export headers.

    Uses the same text-key aliases as the daily Matches table (not history keys).
    """
    labels: dict[str, str] = {}
    descs: dict[str, str] = {}
    for key in catalog_keys:
        label_text_key = _DAILY_COLUMN_LABEL_TEXT_KEYS.get(key) or f"soccer.daily.col.{key}"
        raw_label = (text_map.get(label_text_key) or "").strip()
        labels[key] = raw_label if raw_label else key
        raw_desc = (text_map.get(f"soccer.daily.col.desc.{key}") or "").strip()
        descs[key] = raw_desc
    return labels, descs


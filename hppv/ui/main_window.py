from __future__ import annotations

import json
import re
import sys
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTime, QTimer, Signal, QStringListModel
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, default_config, horses_data_source_urls
from ..db import (
    count_horses,
    count_race_records,
    fetch_horse_by_id,
    fetch_horses,
    fetch_race_records,
    get_horse_status,
    init_db,
    search_horses_by_keyword,
    search_horses_by_name,
    upsert_race_records,
)
from ..scraper import (
    HorsesUpdateStats,
    scrape_horse_past_performance,
    update_horses_table_from_web,
)
from .horses_update_dialog import HorsesUpdateProgressDialog
from .race_fetch_dialog import (
    RaceFetchAllProgressDialog,
    RaceFetchAllStats,
    RaceFetchProgressDialog,
    RaceFetchStats,
)
from .racecard_rank_viewer import RacecardPanel, update_latest_racecard_data


@dataclass(frozen=True, slots=True)
class UiStrings:
    title: str = "HPPV｜賽馬往績資料庫系統"
    horse_id_label: str = "HorseId："
    fetch_button: str = "抓取並入庫"
    load_button: str = "從資料庫載入"


@dataclass(frozen=True, slots=True)
class TaskSettings:
    enable_horses_task: bool = False
    horses_task_time: str = "03:00"
    enable_racecard_task: bool = False
    racecard_task_time: str = "04:00"
    last_run_horses: str = ""
    last_run_racecard: str = ""
    # 任務執行前備份資料庫（避免 schema/資料更新影響測試回歸）
    enable_backup: bool = False
    backup_folder: str = ""
    # 使用 datetime.strftime 格式：Hppv + 日期 + hhMM
    backup_filename_template: str = "Hppv_%Y%m%d_%H%M.sqlite3"
    # 每日備份時間（用於自動先備份，再做下載）
    backup_time: str = "02:55"
    last_backup: str = ""


class _BackgroundTaskWorker(QObject):
    finished_ok = Signal(str)
    finished_error = Signal(str)

    def __init__(self, fn) -> None:  # noqa: ANN001
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            msg = str(self._fn())
            self.finished_ok.emit(msg)
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))


class TaskSettingsDialog(QDialog):
    """任務設定：每日定時下載（馬匹+往績、排位）。"""

    def __init__(self, parent, settings: TaskSettings) -> None:
        super().__init__(parent)
        self.setWindowTitle("任務設定")
        self.resize(560, 280)

        self._cb_horses = QCheckBox("啟用：每日自動下載馬匹資料及往績")
        self._cb_horses.setChecked(settings.enable_horses_task)
        self._time_horses = QTimeEdit()
        self._time_horses.setDisplayFormat("HH:mm")
        self._time_horses.setTime(QTime.fromString(settings.horses_task_time, "HH:mm"))

        self._cb_racecard = QCheckBox("啟用：每日自動下載排位資料")
        self._cb_racecard.setChecked(settings.enable_racecard_task)
        self._time_racecard = QTimeEdit()
        self._time_racecard.setDisplayFormat("HH:mm")
        self._time_racecard.setTime(QTime.fromString(settings.racecard_task_time, "HH:mm"))

        self._cb_backup = QCheckBox("啟用：任務執行前先備份資料庫")
        self._cb_backup.setChecked(settings.enable_backup)
        self._backup_folder = QLineEdit()
        self._backup_folder.setPlaceholderText("例如：data/backup")
        self._backup_folder.setText(settings.backup_folder or "")
        self._backup_folder.setMinimumWidth(260)
        self._backup_filename = QLineEdit()
        self._backup_filename.setReadOnly(True)
        # 備份檔名預覽：以「設定的備份時間」產生 hhMM
        backup_time = getattr(settings, "backup_time", "02:55") or "02:55"
        hh_str, mm_str = backup_time.split(":") if ":" in backup_time else ("02", "55")
        now_dt = datetime.now()
        backup_dt = datetime(
            now_dt.year, now_dt.month, now_dt.day, int(hh_str), int(mm_str)
        )
        self._backup_filename.setText(backup_dt.strftime(settings.backup_filename_template))

        self._time_backup = QTimeEdit()
        self._time_backup.setDisplayFormat("HH:mm")
        self._time_backup.setTime(QTime.fromString(getattr(settings, "backup_time", "02:55"), "HH:mm"))

        def _sync_backup_times_enabled() -> None:
            enabled = self._cb_backup.isChecked()
            self._time_backup.setEnabled(enabled)
            self._backup_folder.setEnabled(enabled)

        def _sync_backup_filename_preview() -> None:
            backup_time = self._time_backup.time().toString("HH:mm")
            hh_str, mm_str = (
                backup_time.split(":") if ":" in backup_time else ("02", "55")
            )
            now_dt = datetime.now()
            backup_dt = datetime(
                now_dt.year, now_dt.month, now_dt.day, int(hh_str), int(mm_str)
            )
            self._backup_filename.setText(
                backup_dt.strftime(settings.backup_filename_template)
            )

        self._cb_backup.toggled.connect(_sync_backup_times_enabled)
        self._time_backup.timeChanged.connect(_sync_backup_filename_preview)
        _sync_backup_times_enabled()
        _sync_backup_filename_preview()

        form = QFormLayout()
        form.addRow(self._cb_horses)
        form.addRow("馬匹/往績時間：", self._time_horses)
        form.addRow(self._cb_racecard)
        form.addRow("排位時間：", self._time_racecard)
        form.addRow(self._cb_backup)
        form.addRow("預備份資料夾：", self._backup_folder)
        form.addRow("備份檔案：", self._backup_filename)
        form.addRow("備份時間：", self._time_backup)

        note = QLabel("說明：任務會在背景執行，下載期間仍可操作畫面。")
        note.setStyleSheet("color: #666; font-size: 12px;")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addStretch()
        root.addWidget(buttons)

    def result_settings(self, old: TaskSettings) -> TaskSettings:
        return TaskSettings(
            enable_horses_task=self._cb_horses.isChecked(),
            horses_task_time=self._time_horses.time().toString("HH:mm"),
            enable_racecard_task=self._cb_racecard.isChecked(),
            racecard_task_time=self._time_racecard.time().toString("HH:mm"),
            last_run_horses=old.last_run_horses,
            last_run_racecard=old.last_run_racecard,
            enable_backup=self._cb_backup.isChecked(),
            backup_folder=self._backup_folder.text().strip(),
            backup_filename_template=old.backup_filename_template,
            backup_time=self._time_backup.time().toString("HH:mm"),
            last_backup=old.last_backup,
        )


class DateSortItem(QTableWidgetItem):
    """Date item that sorts by numeric yyyymmdd key."""

    def __lt__(self, other: object) -> bool:
        if isinstance(other, QTableWidgetItem):
            a = self.data(Qt.UserRole)
            b = other.data(Qt.UserRole)
            if isinstance(a, int) and isinstance(b, int):
                return a < b
        return super().__lt__(other)


def _headers_with_sort_arrow(
    base_headers: list[str], section: int, order: Qt.SortOrder
) -> list[str]:
    labels = list(base_headers)
    if 0 <= section < len(labels):
        labels[section] = f"{labels[section]} {'↑' if order == Qt.AscendingOrder else '↓'}"
    return labels


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._ui = UiStrings()
        self._task_settings_path = self._config.data_dir / "task_settings.json"
        self._task_settings = self._load_task_settings()
        self._task_threads: list[QThread] = []
        self._running_jobs: set[str] = set()

        init_db(self._config.db_path)

        self.setWindowTitle(self._ui.title)
        self.resize(1200, 720)
        self._build_menu_bar()

        self._horse_id_input = QComboBox()
        self._horse_id_input.setEditable(True)
        self._horse_id_input.lineEdit().setPlaceholderText(
            "HorseId、烙號（如 H123）或馬名關鍵字；可下拉或輸入後離開欄位自動對應"
        )
        self._horse_completion_map: dict[str, str] = {}
        le_horse = self._horse_id_input.lineEdit()
        self._horse_completer = QCompleter(le_horse)
        self._horse_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._horse_completer.setFilterMode(Qt.MatchContains)
        self._horse_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._horse_completer.setMaxVisibleItems(20)
        self._horse_completer.setModel(QStringListModel())
        self._horse_completer.activated[str].connect(self._on_horse_completer_activated)
        le_horse.setCompleter(self._horse_completer)
        le_horse.editingFinished.connect(self._on_horse_id_line_finished)
        self._horse_id_input.currentTextChanged.connect(self._on_horse_id_changed)
        # 縮窄 HorseId / Brand No. ComboBox 寬度，保留輸入與下拉功能
        self._horse_id_input.setMinimumWidth(260)
        self._horse_id_input.setMaximumWidth(360)
        self._all_horses_checkbox = QCheckBox("所有馬匹")
        self._all_horses_checkbox.toggled.connect(self._on_all_horses_toggled)

        self._race_year_combo = QComboBox()
        self._race_year_combo.addItem("全部年份", userData="")
        self._race_year_combo.setToolTip(
            "僅用於「抓取並入庫」時要寫入哪一年度的往績；下方「往績」表格一律顯示該馬全部紀錄。"
        )
        # 年份僅用於「抓取並入庫」篩選，不影響下方往績表格顯示
        self._all_years_checkbox = QCheckBox("所有年份")
        self._all_years_checkbox.setChecked(True)
        self._all_years_checkbox.setToolTip(
            "勾選：抓取時不限制年份（等同「全部年份」）。不影響下方往績表格顯示。"
        )
        self._all_years_checkbox.toggled.connect(self._on_all_years_toggled)

        self._horse_name_search_input = QLineEdit()
        self._horse_name_search_input.setPlaceholderText("輸入馬名關鍵字（中文/英文）")

        self._db_path_field = QLineEdit()
        self._db_path_field.setReadOnly(True)
        self._db_path_field.setText(str(self._config.db_path))

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(50, 50000)
        self._limit_spin.setValue(200)
        self._limit_spin.setSingleStep(50)
        # Grid 要顯示全部，不再套用 LIMIT。
        self._limit_spin.setVisible(False)

        self._show_all_btn = QPushButton("顯示全部")
        self._show_all_btn.clicked.connect(self._on_show_all_clicked)
        self._show_all_btn.setVisible(False)

        self._fetch_btn = QPushButton(self._ui.fetch_button)
        self._fetch_btn.clicked.connect(self._on_fetch_clicked)

        self._load_btn = QPushButton(self._ui.load_button)
        self._load_btn.clicked.connect(self._on_load_clicked)

        self._effect_combo = QComboBox()
        self._effect_combo.addItems(
            [
                "① 經典淺色",
                "② 深色夜間",
                "③ 高對比藍",
                "④ 極簡灰白",
                "⑤ 柔和紫調",
            ]
        )
        self._effect_combo.setMinimumWidth(150)
        self._effect_combo.currentIndexChanged.connect(self._on_effect_changed)

        self._search_horse_btn = QPushButton("搜尋馬名")
        self._search_horse_btn.clicked.connect(self._on_search_horses_clicked)

        self._select_horse_btn = QPushButton("選擇")
        self._select_horse_btn.clicked.connect(self._on_select_horse_clicked)

        self._update_horses_btn = QPushButton("更新馬匹資料")
        self._update_horses_btn.clicked.connect(self._on_update_horses_clicked)

        self._horses_table = QTableWidget(0, 5)
        self._horses_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._horses_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._horses_table.setSelectionMode(QTableWidget.SingleSelection)
        self._horses_table.setAlternatingRowColors(True)
        self._horses_table.verticalHeader().setVisible(False)
        self._horses_table.setSortingEnabled(True)
        self._horses_headers = ["HorseId", "Brand No.", "Name EN", "Name ZH", "Status"]
        self._horses_table.setHorizontalHeaderLabels(self._horses_headers)
        # 預設以中文名稱（Name ZH）排序，提升日常查閱效率。
        self._horses_table.horizontalHeader().setSortIndicator(3, Qt.AscendingOrder)
        self._horses_table.horizontalHeader().sortIndicatorChanged.connect(
            self._on_horses_sort_changed
        )
        self._horses_table.setMaximumHeight(200)
        self._horses_table.setStyleSheet(
            "QTableWidget::item:selected { background-color: #5b9bd5; color: white; }"
        )
        self._horses_table.itemDoubleClicked.connect(self._on_horse_table_item_double_clicked)

        self._horses_table.itemSelectionChanged.connect(self._on_horses_selection_changed)

        top = QHBoxLayout()
        top.addWidget(QLabel(self._ui.horse_id_label))
        top.addWidget(self._horse_id_input, 1)
        top.addWidget(self._all_horses_checkbox)
        self._race_year_label = QLabel("抓取年份：")
        top.addWidget(self._race_year_label)
        top.addWidget(self._race_year_combo)
        top.addWidget(self._all_years_checkbox)
        top.addWidget(self._fetch_btn)
        top.addWidget(self._load_btn)
        top.addWidget(QLabel("畫面效果："))
        top.addWidget(self._effect_combo)

        search_row = QHBoxLayout()
        db_label = QLabel("DB：")
        search_row.addWidget(db_label)
        search_row.addWidget(self._db_path_field, 2)

        self._search_label = QLabel("馬名搜尋：")
        search_row.addWidget(self._search_label)
        search_row.addWidget(self._horse_name_search_input, 1)

        self._limit_label = QLabel("顯示前")
        search_row.addWidget(self._limit_label)
        search_row.addWidget(self._limit_spin)
        self._limit_unit_label = QLabel("筆")
        search_row.addWidget(self._limit_unit_label)
        search_row.addWidget(self._show_all_btn)

        self._search_horse_btn.setText("搜尋")
        search_row.addWidget(self._search_horse_btn)
        self._select_horse_btn.setText("選擇")
        search_row.addWidget(self._select_horse_btn)

        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(self._on_clear_clicked)
        search_row.addWidget(clear_btn)
        search_row.addWidget(self._update_horses_btn)

        # Grid Row N/M：N = 目前 Grid 筆數，M = 總筆數
        self._display_count_label = QLabel("Grid Row 0/0")
        self._display_count_label.setStyleSheet("font-weight: bold;")
        search_row.addWidget(self._display_count_label)

        # Grid 上方：顯示目前資料表是從哪些 URL 抓資料
        self._data_source_label = QLabel("")
        self._data_source_label.setWordWrap(True)
        self._data_source_label.setStyleSheet(
            "color: #444; font-size: 11px; padding: 4px 0;"
        )

        self._race_headers = [
            "日期",
            "場地",
            "場次",
            "跑道/途程",
            "途程",
            "場地狀況",
            "班/級別",
            "檔",
            "評分",
            "騎師",
            "練馬師",
            "名次",
            "沿途走位",
            "獨贏賠率",
            "時間",
            "獎金",
            "Flag",
        ]

        # Detail form (selected row fields).
        self._horse_id_detail = QLineEdit()
        self._horse_id_detail.setReadOnly(True)
        self._brand_no_detail = QLineEdit()
        self._brand_no_detail.setReadOnly(True)
        self._horse_name_en_detail = QLineEdit()
        self._horse_name_en_detail.setReadOnly(True)
        self._horse_name_zh_detail = QLineEdit()
        self._horse_name_zh_detail.setReadOnly(True)
        self._horse_status_detail = QLineEdit()
        self._horse_status_detail.setReadOnly(True)

        # 馬匹明細（分頁 1）
        horse_form = QFormLayout()
        horse_form.addRow("HorseId", self._horse_id_detail)
        horse_form.addRow("Brand No.", self._brand_no_detail)
        horse_form.addRow("Name EN", self._horse_name_en_detail)
        horse_form.addRow("Name ZH", self._horse_name_zh_detail)
        horse_form.addRow("Status", self._horse_status_detail)
        self._horse_detail_widget = QWidget()
        self._horse_detail_widget.setLayout(horse_form)

        # 分頁 2：往績以 Grid 顯示（與上方馬匹選取同步）
        self._detail_race_table = QTableWidget(0, 17)
        self._detail_race_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._detail_race_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._detail_race_table.setSelectionMode(QTableWidget.SingleSelection)
        self._detail_race_table.setAlternatingRowColors(True)
        self._detail_race_table.verticalHeader().setVisible(False)
        self._detail_race_table.setSortingEnabled(True)
        self._detail_race_table.setHorizontalHeaderLabels(self._race_headers)
        self._detail_race_table.horizontalHeader().sortIndicatorChanged.connect(
            self._on_detail_race_sort_changed
        )
        self._detail_race_table.setStyleSheet(
            "QTableWidget::item:selected { background-color: #5b9bd5; color: white; }"
        )

        race_tab = QWidget()
        race_tab_layout = QVBoxLayout()
        race_tab_layout.setContentsMargins(0, 0, 0, 0)
        race_tab_layout.addWidget(self._detail_race_table, 1)
        race_tab.setLayout(race_tab_layout)

        # 明細區容器：分頁（馬匹資料 / 往績 Grid）
        self._detail_tabs = QTabWidget()
        self._detail_tabs.addTab(self._horse_detail_widget, "馬匹資料")
        self._detail_tabs.addTab(race_tab, "往績")
        self._detail_tabs.currentChanged.connect(self._on_detail_tab_changed)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addLayout(search_row)
        root.addWidget(self._data_source_label)
        root.addWidget(self._horses_table, 1)
        root.addWidget(self._detail_tabs, 1)

        workspace = QWidget()
        workspace.setLayout(root)

        # 頂層分頁：馬匹／往績 與 排位（內嵌同一主視窗，不另開視窗）
        self._main_shell_tabs = QTabWidget()
        self._main_shell_tabs.setObjectName("mainShellTabs")
        # 文件式分頁列在 Windows 上較穩定；並為 TabBar 設 objectName 供樣式選擇器使用
        # （勿用「QTabWidget > QTabBar」子選擇器，部分環境套不到，會導致分頁無法點選）
        self._main_shell_tabs.setDocumentMode(True)
        self._main_shell_tabs.tabBar().setObjectName("mainShellTabBar")
        self._main_shell_tabs.addTab(workspace, "馬匹／往績")
        self._racecard_panel = RacecardPanel(self._config, parent=self)
        self._racecard_panel.horse_double_clicked.connect(self._on_racecard_horse_double_clicked)
        self._main_shell_tabs.addTab(self._racecard_panel, "排位")
        self._main_shell_tabs.currentChanged.connect(self._on_main_shell_tab_changed)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.addWidget(self._main_shell_tabs)
        self.setCentralWidget(shell)

        tb = QToolBar("Main")
        tb.setMovable(False)
        # 置中：左右 Expanding 占位，中間再加入的 widget / action 會落在橫向正中
        sp_left = QWidget()
        sp_left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(sp_left)
        self._toolbar_center = QWidget()
        center_lay = QHBoxLayout(self._toolbar_center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(8)
        self._current_view_label = QLabel("目前開啟：馬匹／往績")
        self._current_view_label.setStyleSheet("font-weight: 600; padding: 2px 10px;")
        center_lay.addWidget(self._current_view_label)
        tb.addWidget(self._toolbar_center)
        sp_right = QWidget()
        sp_right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(sp_right)
        self.addToolBar(tb)

        self.setStatusBar(QStatusBar())
        horses_total = count_horses(self._config.db_path)
        self.statusBar().showMessage(f"DB：{self._config.db_path}｜現役馬匹總數 {horses_total}")

        # 往績模式時，目前馬匹的總筆數（用於 顯示 N/M 的 M）
        self._race_records_total_for_horse: int = 0

        self._refresh_horse_id_combo()
        self._refresh_race_year_combo()
        self._update_data_source_label()
        self._on_search_clicked()
        self._refresh_sort_header_labels()
        self._on_effect_changed(0)
        self._on_main_shell_tab_changed(self._main_shell_tabs.currentIndex())
        self._start_task_scheduler()

    def _on_effect_changed(self, idx: int) -> None:
        """切換主畫面視覺效果（不影響資料與功能流程）。"""
        if idx == 1:  # 深色夜間
            qss = """
QMainWindow, QWidget { background: #1e1f25; color: #e8eaf0; }
QLineEdit, QComboBox, QSpinBox, QTableWidget, QTabWidget::pane {
    background: #2a2d36; color: #f0f2f6; border: 1px solid #444a5a;
}
QPushButton { background: #2f3b56; color: #f0f2f6; border: 1px solid #4d5f8a; padding: 4px 8px; }
QPushButton:hover { background: #3b4a70; }
QHeaderView::section { background: #303542; color: #f0f2f6; border: 1px solid #4a5061; }
QStatusBar { background: #1a1b20; color: #d9deea; border-top: 1px solid #3a3f4d; }
"""
            select_style = "QTableWidget::item:selected { background-color: #4e6db4; color: white; }"
        elif idx == 2:  # 高對比藍
            qss = """
QMainWindow, QWidget { background: #f4f8ff; color: #0d2d62; }
QLineEdit, QComboBox, QSpinBox, QTableWidget, QTabWidget::pane {
    background: #ffffff; color: #0d2d62; border: 1px solid #1e5cc6;
}
QPushButton { background: #1e5cc6; color: #ffffff; border: 1px solid #1549a1; padding: 4px 8px; font-weight: 600; }
QPushButton:hover { background: #2b6edb; }
QHeaderView::section { background: #dbe9ff; color: #0d2d62; border: 1px solid #1e5cc6; font-weight: 600; }
QStatusBar { background: #eaf2ff; color: #0d2d62; border-top: 1px solid #1e5cc6; }
"""
            select_style = "QTableWidget::item:selected { background-color: #0d47a1; color: white; }"
        elif idx == 3:  # 極簡灰白
            qss = """
QMainWindow, QWidget { background: #f7f7f8; color: #24262b; }
QLineEdit, QComboBox, QSpinBox, QTableWidget, QTabWidget::pane {
    background: #ffffff; color: #24262b; border: 1px solid #d8dbe2;
}
QPushButton { background: #ffffff; color: #2b2f38; border: 1px solid #c9ced8; padding: 4px 8px; }
QPushButton:hover { background: #f0f2f6; }
QHeaderView::section { background: #f1f3f7; color: #2b2f38; border: 1px solid #d8dbe2; }
QStatusBar { background: #ffffff; color: #3b3f48; border-top: 1px solid #d8dbe2; }
"""
            select_style = "QTableWidget::item:selected { background-color: #dce7f8; color: #1d2d4f; }"
        elif idx == 4:  # 柔和紫調
            qss = """
QMainWindow, QWidget { background: #f7f4ff; color: #3f2d66; }
QLineEdit, QComboBox, QSpinBox, QTableWidget, QTabWidget::pane {
    background: #ffffff; color: #3f2d66; border: 1px solid #c8b9ef;
}
QPushButton { background: #8c6bd6; color: #ffffff; border: 1px solid #7557ba; padding: 4px 8px; }
QPushButton:hover { background: #9b7be2; }
QHeaderView::section { background: #ece3ff; color: #3f2d66; border: 1px solid #c8b9ef; }
QStatusBar { background: #f1eaff; color: #4a3478; border-top: 1px solid #c8b9ef; }
"""
            select_style = "QTableWidget::item:selected { background-color: #7f5cc7; color: white; }"
        else:  # 經典淺色
            qss = """
QMainWindow, QWidget { background: #ffffff; color: #1f2d3d; }
QLineEdit, QComboBox, QSpinBox, QTableWidget, QTabWidget::pane {
    background: #ffffff; color: #1f2d3d; border: 1px solid #cfd6e4;
}
QPushButton { background: #f7f9fd; color: #1f2d3d; border: 1px solid #c3cde0; padding: 4px 8px; }
QPushButton:hover { background: #e9eff9; }
QHeaderView::section { background: #eef3fb; color: #1f2d3d; border: 1px solid #cfd6e4; }
QStatusBar { background: #f7f9fd; color: #1f2d3d; border-top: 1px solid #cfd6e4; }
"""
            select_style = "QTableWidget::item:selected { background-color: #5b9bd5; color: white; }"

        self.setStyleSheet(qss + self._main_shell_tabs_style(idx))
        self._horses_table.setStyleSheet(select_style)
        self._detail_race_table.setStyleSheet(select_style)

    def _build_menu_bar(self) -> None:
        bar = self.menuBar()
        m_settings = bar.addMenu("系統設定")
        a_task = QAction("任務設定", self)
        a_task.triggered.connect(self._open_task_settings_dialog)
        m_settings.addAction(a_task)

    def _load_task_settings(self) -> TaskSettings:
        default_backup_folder = str(self._config.data_dir / "backup")
        if not self._task_settings_path.is_file():
            return TaskSettings(
                enable_backup=False,
                backup_folder=default_backup_folder,
            )
        try:
            raw = json.loads(self._task_settings_path.read_text(encoding="utf-8"))
            return TaskSettings(
                enable_horses_task=bool(raw.get("enable_horses_task", False)),
                horses_task_time=str(raw.get("horses_task_time", "03:00")),
                enable_racecard_task=bool(raw.get("enable_racecard_task", False)),
                racecard_task_time=str(raw.get("racecard_task_time", "04:00")),
                last_run_horses=str(raw.get("last_run_horses", "")),
                last_run_racecard=str(raw.get("last_run_racecard", "")),
                enable_backup=bool(raw.get("enable_backup", False)),
                backup_folder=str(raw.get("backup_folder", default_backup_folder)),
                backup_filename_template=str(
                    raw.get("backup_filename_template", "Hppv_%Y%m%d_%H%M.sqlite3")
                ),
                backup_time=str(
                    raw.get("backup_time")
                    or raw.get("horses_backup_time")
                    or "02:55"
                ),
                last_backup=str(
                    raw.get("last_backup")
                    or raw.get("last_backup_horses")
                    or ""
                ),
            )
        except Exception:
            return TaskSettings(
                enable_backup=False,
                backup_folder=default_backup_folder,
            )

    def _save_task_settings(self) -> None:
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "enable_horses_task": self._task_settings.enable_horses_task,
            "horses_task_time": self._task_settings.horses_task_time,
            "enable_racecard_task": self._task_settings.enable_racecard_task,
            "racecard_task_time": self._task_settings.racecard_task_time,
            "last_run_horses": self._task_settings.last_run_horses,
            "last_run_racecard": self._task_settings.last_run_racecard,
            "enable_backup": self._task_settings.enable_backup,
            "backup_folder": self._task_settings.backup_folder,
            "backup_filename_template": self._task_settings.backup_filename_template,
            "backup_time": self._task_settings.backup_time,
            "last_backup": self._task_settings.last_backup,
        }
        self._task_settings_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _open_task_settings_dialog(self) -> None:
        dlg = TaskSettingsDialog(self, self._task_settings)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._task_settings = dlg.result_settings(self._task_settings)
        self._save_task_settings()
        self.statusBar().showMessage(
            "任務設定已儲存："
            f"馬匹/往績({self._task_settings.horses_task_time})="
            f"{'啟用' if self._task_settings.enable_horses_task else '停用'}，"
            f"排位({self._task_settings.racecard_task_time})="
            f"{'啟用' if self._task_settings.enable_racecard_task else '停用'}"
        )

    def open_task_settings_dialog(self) -> None:
        """供外部（如 Dashboard）呼叫的任務設定入口。"""
        self._open_task_settings_dialog()

    def _start_task_scheduler(self) -> None:
        self._task_timer = QTimer(self)
        self._task_timer.timeout.connect(self._check_scheduled_tasks)
        self._task_timer.start(30000)
        QTimer.singleShot(1500, self._check_scheduled_tasks)

    def _check_scheduled_tasks(self) -> None:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        # 先備份（只有一個備份時間），確保下載前已備份（若備份啟用）
        if (
            self._task_settings.enable_backup
            and (self._task_settings.enable_horses_task or self._task_settings.enable_racecard_task)
            and hhmm >= self._task_settings.backup_time
            and self._task_settings.last_backup != today
        ):
            self._run_background_job("backup")

        allow_download = (not self._task_settings.enable_backup) or (
            self._task_settings.last_backup == today
        )

        # 再下載
        if (
            allow_download
            and self._task_settings.enable_horses_task
            and hhmm >= self._task_settings.horses_task_time
            and self._task_settings.last_run_horses != today
        ):
            self._run_background_job("horses")

        if (
            allow_download
            and self._task_settings.enable_racecard_task
            and hhmm >= self._task_settings.racecard_task_time
            and self._task_settings.last_run_racecard != today
        ):
            self._run_background_job("racecard")

    def _run_background_job(self, job: str) -> None:
        if job in self._running_jobs:
            return
        self._running_jobs.add(job)

        if job == "horses":
            fn = self._run_horses_and_records_job
        elif job == "racecard":
            fn = self._run_racecard_job
        elif job == "backup":
            fn = self._run_backup_job
        else:
            self._running_jobs.discard(job)
            return
        worker = _BackgroundTaskWorker(fn)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def on_ok(msg: str) -> None:
            today = datetime.now().strftime("%Y-%m-%d")
            self._task_settings = TaskSettings(
                enable_horses_task=self._task_settings.enable_horses_task,
                horses_task_time=self._task_settings.horses_task_time,
                enable_racecard_task=self._task_settings.enable_racecard_task,
                racecard_task_time=self._task_settings.racecard_task_time,
                last_run_horses=today if job == "horses" else self._task_settings.last_run_horses,
                last_run_racecard=today if job == "racecard" else self._task_settings.last_run_racecard,
                enable_backup=self._task_settings.enable_backup,
                backup_folder=self._task_settings.backup_folder,
                backup_filename_template=self._task_settings.backup_filename_template,
                backup_time=self._task_settings.backup_time,
                last_backup=today if job == "backup" else self._task_settings.last_backup,
            )
            self._save_task_settings()
            self.statusBar().showMessage(msg)

        def on_err(err: str) -> None:
            self.statusBar().showMessage(f"自動任務失敗（{job}）：{err}")

        def on_done() -> None:
            self._running_jobs.discard(job)
            thread.quit()

        worker.finished_ok.connect(on_ok)
        worker.finished_ok.connect(lambda _: on_done())
        worker.finished_error.connect(on_err)
        worker.finished_error.connect(lambda _: on_done())
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._task_threads.append(thread)

    def _run_backup_job(self) -> str:
        """每日只做一次整個 DB 備份（以設定的備份時間命名檔案）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if not self._task_settings.enable_backup:
            return "備份：已停用"
        if self._task_settings.last_backup == today:
            return "備份：跳過（今日已備份）"

        src_db = self._config.db_path
        if not src_db.exists():
            return "備份：跳過（DB 不存在）"

        folder_text = (self._task_settings.backup_folder or "").strip()
        if not folder_text:
            folder_text = str(self._config.data_dir / "backup")
        backup_dir = Path(folder_text)
        if not backup_dir.is_absolute():
            p = str(backup_dir).replace("\\", "/").lower()
            if p == "data" or p.startswith("data/"):
                backup_dir = self._config.data_dir.parent / backup_dir
            else:
                backup_dir = self._config.data_dir / backup_dir

        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            # 檔名依「設定的備份時間」產生（而非執行當下的時間）
            backup_time = self._task_settings.backup_time or "00:00"
            hh_str, mm_str = backup_time.split(":") if ":" in backup_time else ("00", "00")
            hh = int(hh_str)
            mm = int(mm_str)
            now = datetime.now()
            backup_dt = datetime(now.year, now.month, now.day, hh, mm)
            fname = backup_dt.strftime(self._task_settings.backup_filename_template)
            dst_db = backup_dir / fname
            shutil.copy2(str(src_db), str(dst_db))
            return f"備份完成：{dst_db.name}"
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(str(e)) from e

    def _run_horses_and_records_job(self) -> str:
        horses_stats = update_horses_table_from_web(self._config.db_path)
        rows = fetch_horses(self._config.db_path, limit=None)
        fetched_total = 0
        saved_total = 0
        inserted_total = 0
        for row in rows:
            horse_id = str(row["horse_id"] or "").strip()
            if not horse_id:
                continue
            try:
                result = scrape_horse_past_performance(
                    horse_id, self._config.hkjc_horse_url_template, db_path=self._config.db_path
                )
                inserted = upsert_race_records(self._config.db_path, result.records)
                fetched_total += len(result.records)
                saved_total += len(result.records)
                inserted_total += inserted
            except Exception:
                continue
        return (
            "自動任務完成：馬匹+往績｜"
            + f"馬匹寫入 {horses_stats.written}｜"
            + f"往績抓到 {fetched_total}｜新增 {inserted_total}"
        )

    def _run_racecard_job(self) -> str:
        meeting = update_latest_racecard_data(self._config)
        return f"自動任務完成：排位｜賽日 {meeting.racedate} {meeting.racecourse_code}"

    # 兼容舊的函式名稱（現已改成唯一備份時間 + 單一 backup job）
    def _run_horses_backup_job(self) -> str:
        return self._run_backup_job()

    def _run_racecard_backup_job(self) -> str:
        return self._run_backup_job()

    def show_horses_shell(self) -> None:
        """顯示主視窗並切到「馬匹／往績」頂層分頁（供 Dashboard 選單等呼叫）。"""
        self._main_shell_tabs.setCurrentIndex(0)
        self._on_main_shell_tab_changed(0)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_racecard_shell(self) -> None:
        """顯示主視窗並切到內嵌「排位」頂層分頁。"""
        self._main_shell_tabs.setCurrentIndex(1)
        self._on_main_shell_tab_changed(1)
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_main_shell_tab_changed(self, index: int) -> None:
        """切換主殼分頁時，明確顯示目前開啟項目。"""
        tab_name = "排位" if index == 1 else "馬匹／往績"
        if hasattr(self, "_current_view_label"):
            self._current_view_label.setText(f"目前開啟：{tab_name}")
        self.statusBar().showMessage(f"已切換至：{tab_name}")

    def _on_racecard_horse_double_clicked(self, horse_id: str) -> None:
        """在排位表雙擊馬匹時：切回主頁並自動開啟該馬往績分頁。"""
        hid = str(horse_id or "").strip()
        if not hid:
            return
        self._main_shell_tabs.setCurrentIndex(0)
        self._on_main_shell_tab_changed(0)
        self._select_horse_id(hid)
        self._detail_tabs.setCurrentIndex(1)
        self.statusBar().showMessage(f"已從排位轉到往績：{hid}")

    def _main_shell_tabs_style(self, idx: int) -> str:
        """頂層「馬匹／往績 | 排位」分頁：內嵌區塊邊框與分頁列樣式（與畫面效果一致）。"""
        themes = {
            1: ("#4a5061", "#303542", "#f0f2f6", "#5c6578"),  # 深色
            2: ("#1e5cc6", "#dbe9ff", "#0d2d62", "#6b8fc9"),  # 高對比藍
            3: ("#d8dbe2", "#f1f3f7", "#24262b", "#8a909c"),  # 極簡灰白
            4: ("#c8b9ef", "#ece3ff", "#3f2d66", "#9b87c4"),  # 柔和紫
        }
        border, sel_bg, sel_fg, unsel_fg = themes.get(idx, ("#cfd6e4", "#eef3fb", "#1f2d3d", "#5a6a7d"))
        return f"""
QTabWidget#mainShellTabs::pane {{
  border: 2px solid {border};
  border-radius: 10px;
  padding: 6px;
  background: transparent;
}}
QTabBar#mainShellTabBar::tab {{
  min-width: 120px;
  padding: 8px 16px;
  margin-right: 4px;
  color: {unsel_fg};
  background: transparent;
  border: 1px solid transparent;
  border-bottom: none;
  border-top-left-radius: 8px;
  border-top-right-radius: 8px;
}}
QTabBar#mainShellTabBar::tab:selected {{
  color: {sel_fg};
  background: {sel_bg};
  border-color: {border};
  font-weight: 600;
}}
"""

    def _horse_id(self) -> str:
        """實際用於查詢的 HorseId：下拉選定用 userData；手打時僅用補全表／HK_ 格式（其餘離開欄位後才解析）。"""
        idx = self._horse_id_input.currentIndex()
        if idx >= 0:
            data = self._horse_id_input.itemData(idx)
            if data:
                return str(data).strip()
        text = self._horse_id_input.currentText().strip()
        if not text:
            return ""
        if text in self._horse_completion_map:
            return self._horse_completion_map[text]
        if re.fullmatch(r"HK_[A-Za-z0-9_]+", text, flags=re.IGNORECASE):
            return text
        return text

    def _resolve_input_to_horse_id(self, text: str) -> str:
        """將輸入對應為 HorseId；無法唯一對應時保留原字串（例如關鍵字過寬）。"""
        t = text.strip()
        if not t:
            return ""
        if t in self._horse_completion_map:
            return self._horse_completion_map[t]
        if re.fullmatch(r"HK_[A-Za-z0-9_]+", t, flags=re.IGNORECASE):
            return t
        if fetch_horse_by_id(self._config.db_path, t) is not None:
            return t
        rows = search_horses_by_keyword(self._config.db_path, t, limit=40)
        if not rows:
            return t
        if len(rows) == 1:
            return str(rows[0]["horse_id"])
        exact_brand = [r for r in rows if str(r["brand_no"] or "").strip().upper() == t.upper()]
        if len(exact_brand) == 1:
            return str(exact_brand[0]["horse_id"])
        exact_zh = [r for r in rows if str(r["name_zh"] or "").strip() == t]
        if len(exact_zh) == 1:
            return str(exact_zh[0]["horse_id"])
        exact_en = [r for r in rows if str(r["name_en"] or "").strip().upper() == t.upper()]
        if len(exact_en) == 1:
            return str(exact_en[0]["horse_id"])
        return t

    def _rebuild_horse_completion_map(self) -> None:
        """補全清單：HorseId、烙號、中英文名、與下拉顯示字串 -> horse_id。"""
        self._horse_completion_map.clear()
        rows = fetch_horses(self._config.db_path, limit=None)
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            if not hid:
                continue
            brand = str(r["brand_no"] or "").strip()
            zh = str(r["name_zh"] or "").strip()
            en = str(r["name_en"] or "").strip()
            display = "｜".join(p for p in [brand or hid, zh, en] if p)

            def add_key(k: str) -> None:
                k2 = k.strip()
                if not k2:
                    return
                if k2 in self._horse_completion_map and self._horse_completion_map[k2] != hid:
                    return
                self._horse_completion_map[k2] = hid

            add_key(hid)
            add_key(brand)
            add_key(zh)
            add_key(en)
            add_key(display)

        model = QStringListModel(sorted(self._horse_completion_map.keys()))
        self._horse_completer.setModel(model)

    def _on_horse_completer_activated(self, text: str) -> None:
        hid = self._horse_completion_map.get(text.strip())
        if hid:
            self._select_horse_id(hid)

    def _on_horse_id_line_finished(self) -> None:
        """離開輸入欄時：若可唯一解析為 HorseId，則正規化顯示並同步。"""
        if self._horse_id_input.signalsBlocked():
            return
        raw = self._horse_id_input.currentText().strip()
        if not raw:
            return
        resolved = self._resolve_input_to_horse_id(raw)
        if resolved and resolved != raw:
            self._select_horse_id(resolved)

    def _refresh_horse_id_combo(self) -> None:
        """Refresh HorseId combo items from horses table."""
        current = self._horse_id()
        rows = fetch_horses(self._config.db_path, limit=None)
        ids: list[str] = []
        seen: set[str] = set()
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            if not hid or hid in seen:
                continue
            ids.append(hid)
            seen.add(hid)

        self._horse_id_input.blockSignals(True)
        self._horse_id_input.clear()
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            if not hid:
                continue
            brand_no = str(r["brand_no"] or "").strip()
            name_zh = str(r["name_zh"] or "").strip()
            name_en = str(r["name_en"] or "").strip()
            # 顯示：Brand No. + 馬名；內部 userData 仍存 horse_id
            display = "｜".join(
                [p for p in [brand_no or hid, name_zh or "", name_en or ""] if p]
            )
            self._horse_id_input.addItem(display, userData=hid)
        if current:
            idx = self._horse_id_input.findData(current)
            if idx >= 0:
                self._horse_id_input.setCurrentIndex(idx)
            else:
                # 保留手動輸入值（可能不在 horses 清單內）
                self._horse_id_input.setEditText(current)
        self._horse_id_input.blockSignals(False)
        self._rebuild_horse_completion_map()

    def _refresh_race_year_combo(self) -> None:
        """重建「抓取年份」下拉（僅影響抓取／下載，不影響往績表格）。"""
        current_year = str(self._race_year_combo.currentData() or "")
        current = datetime.now().year
        # 允許預先選未來年份（例如來季/下季）
        years = [str(y) for y in range(current + 5, 1999, -1)]

        self._race_year_combo.blockSignals(True)
        self._race_year_combo.clear()
        self._race_year_combo.addItem("全部年份", userData="")
        for y in years:
            self._race_year_combo.addItem(f"{y}年", userData=y)

        if current_year and current_year in years:
            idx = self._race_year_combo.findData(current_year)
            if idx >= 0:
                self._race_year_combo.setCurrentIndex(idx)
        elif current_year:
            self._race_year_combo.addItem(f"{current_year}年", userData=current_year)
            self._race_year_combo.setCurrentIndex(self._race_year_combo.count() - 1)
        else:
            self._race_year_combo.setCurrentIndex(0)
        self._race_year_combo.blockSignals(False)

    def _selected_race_year(self) -> str:
        if self._all_years_checkbox.isChecked():
            return ""
        return str(self._race_year_combo.currentData() or "").strip()

    @staticmethod
    def _extract_year(date_text: str) -> str:
        """Extract year from race_date text (supports dd/mm/yy and yyyy)."""
        if not date_text:
            return ""
        # 1) direct 4-digit year
        m4 = re.search(r"(19|20)\d{2}", date_text)
        if m4:
            return m4.group(0)
        # 2) trailing 2-digit year in date like 01/03/26
        m2 = re.search(r"\b\d{1,2}/\d{1,2}/(\d{2})\b", date_text)
        if m2:
            yy = int(m2.group(1))
            return f"{2000 + yy:04d}" if yy <= 69 else f"{1900 + yy:04d}"
        return ""

    @staticmethod
    def _format_race_date_display(date_text: str) -> str:
        """Format race date to include 4-digit year for display."""
        if not date_text:
            return ""
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})$", date_text.strip())
        if not m:
            return date_text
        dd, mm, yy = m.groups()
        yy_num = int(yy)
        yyyy = 2000 + yy_num if yy_num <= 69 else 1900 + yy_num
        return f"{int(dd):02d}/{int(mm):02d}/{yyyy:04d}"

    @staticmethod
    def _race_date_sort_key(date_text: str) -> int:
        """Return sortable yyyymmdd key for race date."""
        t = (date_text or "").strip()
        m4 = re.match(r"^(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})$", t)
        if m4:
            dd, mm, yyyy = m4.groups()
            return int(f"{int(yyyy):04d}{int(mm):02d}{int(dd):02d}")
        m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})$", t)
        if m2:
            dd, mm, yy = m2.groups()
            yy_num = int(yy)
            yyyy = 2000 + yy_num if yy_num <= 69 else 1900 + yy_num
            return int(f"{yyyy:04d}{int(mm):02d}{int(dd):02d}")
        return 0

    def _on_all_years_toggled(self, checked: bool) -> None:
        self._race_year_combo.setEnabled(not checked)

    def _on_all_horses_toggled(self, checked: bool) -> None:
        # 「所有馬匹」只影響馬匹範圍，不強制年份；年份仍由右側 controls 決定。
        self._horse_id_input.setEnabled(not checked)
        self._all_years_checkbox.setEnabled(True)
        # 勾選「所有馬匹」時，預設改為可選年份（避免看起來被強制所有年份）
        if checked and self._all_years_checkbox.isChecked():
            self._all_years_checkbox.setChecked(False)
        self._race_year_combo.setEnabled(not self._all_years_checkbox.isChecked())

    def _select_horse_row_by_horse_id(self, horse_id: str) -> None:
        """Select the matching row in horses grid."""
        if not horse_id:
            return
        for row_idx in range(self._horses_table.rowCount()):
            item = self._horses_table.item(row_idx, 0)
            if item and item.text().strip() == horse_id:
                self._horses_table.setCurrentCell(row_idx, 0)
                self._horses_table.scrollToItem(item)
                break

    def _on_horse_id_changed(self, text: str) -> None:
        """Sync grid/details when HorseId combo changes."""
        _ = text
        horse_id = self._horse_id()
        self._refresh_race_year_combo()
        if not horse_id:
            self._sync_selected_horse_panels()
            self._update_display_count_label()
            return
        self._select_horse_row_by_horse_id(horse_id)
        self._sync_selected_horse_panels()
        self._update_display_count_label()

    def _update_data_source_label(self) -> None:
        """更新 Grid 上方「從哪些 URL 抓資料」說明。"""
        urls = horses_data_source_urls()
        tpl = self._config.hkjc_horse_url_template
        self._data_source_label.setText(
            "馬匹資料來源（更新馬匹資料）：\n"
            + "\n".join(urls)
            + "\n\n往績來源（抓取並入庫）：\n"
            + tpl
            + "\n（將 {horse_id} 替換為馬匹 HorseId 後請求）"
        )

    def _on_horses_sort_changed(self, section: int, order: Qt.SortOrder) -> None:
        self._horses_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(self._horses_headers, section, order)
        )

    def _on_detail_race_sort_changed(self, section: int, order: Qt.SortOrder) -> None:
        self._detail_race_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(self._race_headers, section, order)
        )

    def _refresh_sort_header_labels(self) -> None:
        h1 = self._horses_table.horizontalHeader()
        self._horses_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(
                self._horses_headers, h1.sortIndicatorSection(), h1.sortIndicatorOrder()
            )
        )
        h2 = self._detail_race_table.horizontalHeader()
        self._detail_race_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(
                self._race_headers, h2.sortIndicatorSection(), h2.sortIndicatorOrder()
            )
        )

    def _update_display_count_label(self) -> None:
        """更新「Grid Row N/M」：N = 目前選中列（current row），M = 總列數（row count）。"""
        cur = self._horses_table.currentRow()
        n = cur + 1 if cur >= 0 else "—"
        m = self._horses_table.rowCount()
        self._display_count_label.setText(f"Grid Row {n}/{m}")

    def _select_horse_id(self, horse_id: str) -> None:
        le = self._horse_id_input.lineEdit()
        self._horse_id_input.blockSignals(True)
        le.blockSignals(True)
        try:
            idx = self._horse_id_input.findData(horse_id)
            if idx >= 0:
                self._horse_id_input.setCurrentIndex(idx)
            else:
                self._horse_id_input.setEditText(horse_id)
        finally:
            le.blockSignals(False)
            self._horse_id_input.blockSignals(False)
        self.statusBar().showMessage(f"已選擇：{horse_id}")
        self._refresh_race_year_combo()
        self._select_horse_row_by_horse_id(horse_id)
        self._sync_selected_horse_panels()
        self._update_display_count_label()

    def _on_search_horses_clicked(self) -> None:
        self._on_search_clicked()

    def _on_horses_selection_changed(self) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx >= 0:
            item = self._horses_table.item(row_idx, 0)
            if item:
                hid = item.text().strip()
                if hid and hid != self._horse_id():
                    self._select_horse_id(hid)
                    return
        self._sync_selected_horse_panels()
        self._update_display_count_label()

    def _on_select_horse_clicked(self) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx < 0:
            QMessageBox.warning(self, "未選擇", "請先在搜尋結果中選擇一筆。")
            return

        item = self._horses_table.item(row_idx, 0)
        if item is None:
            QMessageBox.warning(self, "資料錯誤", "未找到該筆的 horse_id。")
            return

        horse_id = item.text().strip()
        if horse_id is None:
            QMessageBox.warning(self, "資料錯誤", "該項目的 horse_id 取得失敗。")
            return

        self._select_horse_id(str(horse_id))

    def _on_horse_table_item_double_clicked(self, _item: QTableWidgetItem) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx < 0:
            return
        item = self._horses_table.item(row_idx, 0)
        if item is None:
            return
        self._select_horse_id(item.text().strip())

    def _on_update_horses_clicked(self) -> None:
        started_at = datetime.now()
        before_total = count_horses(self._config.db_path)
        self.statusBar().showMessage(f"更新馬匹資料中…（開始：{started_at:%Y-%m-%d %H:%M:%S}）")

        dlg = HorsesUpdateProgressDialog(self, self._config.db_path)

        def on_ok(obj: object) -> None:
            stats = obj if isinstance(obj, HorsesUpdateStats) else None
            if stats is None:
                QMessageBox.critical(self, "更新失敗", "更新回傳資料格式不正確。")
                return

            finished_at = datetime.now()
            after_total = count_horses(self._config.db_path)
            added = max(after_total - before_total, 0)
            self.statusBar().showMessage(
                f"馬匹資料更新完成：抓到 {stats.total_found}｜寫入 {stats.written}｜新增 {added}｜現役馬匹總數 {after_total}"
            )
            QMessageBox.information(
                self,
                "更新完成",
                "\n".join(
                    [
                        f"開始時間：{started_at:%Y-%m-%d %H:%M:%S}",
                        f"結束時間：{finished_at:%Y-%m-%d %H:%M:%S}",
                        f"總搜尋到：{stats.total_found} 筆",
                        f"寫入（INSERT OR REPLACE）：{stats.written} 筆",
                        f"新增（DB 總數差值）：{added} 筆",
                        f"現役馬匹總數 {after_total}",
                    ]
                ),
            )
            self._refresh_horse_id_combo()
            self._on_search_clicked()

        dlg.finished_ok.connect(on_ok)
        dlg.start()

    def _on_clear_clicked(self) -> None:
        self._horse_name_search_input.setText("")
        self._on_search_clicked()

    def _on_show_all_clicked(self) -> None:
        total = count_horses(self._config.db_path)
        # Allow 0 (empty) gracefully.
        self._limit_spin.setValue(max(total, 50) if total else 50)
        self._on_search_clicked()

    def _on_search_clicked(self) -> None:
        keyword = self._horse_name_search_input.text().strip()
        # Grid 顯示全部：不在 DB query 加入 LIMIT。
        limit: int | None = None

        self._horses_table.setSortingEnabled(False)
        rows = (
            search_horses_by_name(self._config.db_path, keyword, limit=limit)
            if keyword
            else fetch_horses(self._config.db_path, limit=limit)
        )
        self._horses_table.setRowCount(0)
        for r in rows:
            row_idx = self._horses_table.rowCount()
            self._horses_table.insertRow(row_idx)
            values = [
                str(r["horse_id"]),
                r["brand_no"] or "",
                r["name_en"] or "",
                r["name_zh"] or "",
                r["status"] or "",
            ]
            for col, v in enumerate(values):
                item = QTableWidgetItem(v)
                if col in (0, 1):
                    item.setTextAlignment(Qt.AlignCenter)
                self._horses_table.setItem(row_idx, col, item)
        self._horses_table.setSortingEnabled(True)
        # 套用預設排序：Name ZH 升序。
        self._horses_table.sortItems(3, Qt.AscendingOrder)
        self._refresh_sort_header_labels()
        if rows:
            self._horses_table.selectRow(0)
        self._sync_selected_horse_panels()
        total = count_horses(self._config.db_path)
        if not rows:
            self.statusBar().showMessage("未找到符合馬名的資料（horses 表可能尚未更新）。")
        else:
            self.statusBar().showMessage(f"現役馬匹總數 {total}（目前列表 {len(rows)}）")
        self._update_display_count_label()

    def _sync_selected_horse_panels(self) -> None:
        """選定馬匹時同步：分頁 1 馬匹表單 + 分頁 2 往績 Grid（不強制切換目前分頁）。"""
        self._update_horses_details()
        self._load_race_records_into_detail_table(self._horse_id())

    def _load_race_records_into_detail_table(self, horse_id: str) -> None:
        """依目前 HorseId 填入下方「往績」分頁 Grid（一律顯示全部往績，不依上方抓取年份篩選）。"""
        table = self._detail_race_table
        if not horse_id:
            table.setRowCount(0)
            self._race_records_total_for_horse = 0
            self._refresh_sort_header_labels()
            return

        rows = fetch_race_records(self._config.db_path, horse_id)

        table.setSortingEnabled(False)
        table.setRowCount(0)
        flag_status = get_horse_status(self._config.db_path, horse_id)

        for r in rows:
            row_idx = table.rowCount()
            table.insertRow(row_idx)
            values = [
                self._format_race_date_display(r.race_date),
                r.venue,
                r.race_no,
                r.course,
                r.distance,
                r.going,
                r.class_band,
                r.draw,
                r.rating,
                r.jockey,
                r.trainer,
                r.finish_pos,
                r.running_pos,
                r.win_odds,
                r.time,
                r.prize,
                flag_status,
            ]
            for col, v in enumerate(values):
                if col == 0:
                    item = DateSortItem(str(v))
                    item.setData(Qt.UserRole, self._race_date_sort_key(str(v)))
                else:
                    item = QTableWidgetItem(str(v))
                if col in (0, 2, 7, 8, 11, 13, 16):
                    item.setTextAlignment(Qt.AlignCenter)
                table.setItem(row_idx, col, item)

        table.resizeColumnsToContents()
        table.setSortingEnabled(True)
        self._refresh_sort_header_labels()
        if table.rowCount() > 0:
            table.selectRow(0)
        self._race_records_total_for_horse = count_race_records(self._config.db_path, horse_id)

    def _on_detail_tab_changed(self, index: int) -> None:
        """切換明細分頁時重新整理該頁資料（與目前選定馬匹同步）。"""
        if index == 0:
            self._update_horses_details()
        elif index == 1:
            self._load_race_records_into_detail_table(self._horse_id())

    def _clear_horse_detail_fields(self) -> None:
        self._horse_id_detail.setText("")
        self._brand_no_detail.setText("")
        self._horse_name_en_detail.setText("")
        self._horse_name_zh_detail.setText("")
        self._horse_status_detail.setText("")

    def _update_horses_details(self) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx < 0:
            self._clear_horse_detail_fields()
            return
        self._horse_id_detail.setText(self._horses_table.item(row_idx, 0).text() if self._horses_table.item(row_idx, 0) else "")
        self._brand_no_detail.setText(self._horses_table.item(row_idx, 1).text() if self._horses_table.item(row_idx, 1) else "")
        self._horse_name_en_detail.setText(self._horses_table.item(row_idx, 2).text() if self._horses_table.item(row_idx, 2) else "")
        self._horse_name_zh_detail.setText(self._horses_table.item(row_idx, 3).text() if self._horses_table.item(row_idx, 3) else "")
        self._horse_status_detail.setText(self._horses_table.item(row_idx, 4).text() if self._horses_table.item(row_idx, 4) else "")

    def _on_fetch_clicked(self) -> None:
        if self._all_horses_checkbox.isChecked():
            selected_year = self._selected_race_year()
            year_text = selected_year if selected_year else "所有年份"
            self.statusBar().showMessage(f"抓取中…（所有馬匹 / {year_text}）")
            dlg_all = RaceFetchAllProgressDialog(
                self,
                self._config.db_path,
                self._config.hkjc_horse_url_template,
                selected_year,
            )

            def on_ok_all(obj: object) -> None:
                stats = obj if isinstance(obj, RaceFetchAllStats) else None
                if stats is None:
                    QMessageBox.critical(self, "抓取失敗", "抓取回傳資料格式不正確。")
                    return
                year_text2 = stats.selected_year if stats.selected_year else "所有年份"
                self.statusBar().showMessage(
                    f"完成（所有馬匹 / {year_text2}）：馬匹 {stats.success_horses}/{stats.total_horses} 成功，"
                    f"失敗 {stats.failed_horses}；抓到 {stats.fetched} 筆，寫入 {stats.saved} 筆，新增 {stats.inserted} 筆。"
                )
                self._on_search_clicked()

            dlg_all.finished_ok.connect(on_ok_all)
            dlg_all.start()
            return

        horse_id = self._horse_id()
        if not horse_id:
            QMessageBox.warning(self, "需要 HorseId", "請先輸入 HorseId。")
            return

        selected_year = self._selected_race_year()
        self.statusBar().showMessage(f"抓取中…（{horse_id}）")
        dlg = RaceFetchProgressDialog(
            self,
            self._config.db_path,
            horse_id,
            self._config.hkjc_horse_url_template,
            selected_year,
        )

        def on_ok(obj: object) -> None:
            stats = obj if isinstance(obj, RaceFetchStats) else None
            if stats is None:
                QMessageBox.critical(self, "抓取失敗", "抓取回傳資料格式不正確。")
                return
            year_text = f"（年份：{stats.selected_year}）" if stats.selected_year else "（年份：全部）"
            self.statusBar().showMessage(
                f"完成{year_text}：抓到 {stats.fetched} 筆，寫入 {stats.saved} 筆，新增 {stats.inserted} 筆。"
            )
            if stats.selected_year and stats.saved == 0:
                valid_years = [
                    y for y in stats.available_years if re.fullmatch(r"(19|20)\d{2}", str(y))
                ]
                years_txt = "、".join(valid_years) if valid_years else "（無可用年份）"
                tip = (
                    f"{stats.horse_id} 在 {stats.selected_year} 年沒有往績。"
                    if stats.fetched > 0
                    else f"{stats.horse_id} 目前抓不到任何往績資料。"
                )
                QMessageBox.information(
                    self,
                    "年份無資料",
                    f"{tip}\n可用年份：{years_txt}",
                )
            self._load_into_table(stats.horse_id)

        dlg.finished_ok.connect(on_ok)
        dlg.start()

    def _on_load_clicked(self) -> None:
        horse_id = self._horse_id()
        if not horse_id:
            QMessageBox.warning(self, "需要 HorseId", "請先輸入 HorseId。")
            return
        self._load_into_table(horse_id)

    def _load_into_table(self, horse_id: str) -> None:
        """從資料庫載入往績至「往績」分頁 Grid（與抓取完成後刷新相同）。"""
        self._load_race_records_into_detail_table(horse_id)
        n = self._detail_race_table.rowCount()
        self.statusBar().showMessage(f"已載入：{horse_id} 共 {n} 筆。")
        self._update_display_count_label()


def main() -> int:
    """供 `python -m hppv.ui.main_window` 直接啟動主視窗（與 `python -m hppv` 相同）。"""
    app = QApplication(sys.argv)
    w = MainWindow(default_config())
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from datetime import datetime
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from ..db import count_horses, fetch_horses, search_horses_by_name
from ..scraper import HorsesUpdateStats, update_horses_table_from_web
from .horses_update_dialog import HorsesUpdateProgressDialog


class HorsesDialog(QDialog):
    """馬匹資料瀏覽/搜尋 UI（對應 horses table）。"""

    def __init__(self, db_path, on_select_horse_id: Callable[[str], None]) -> None:
        super().__init__()
        self._db_path = db_path
        self._on_select = on_select_horse_id

        self.setWindowTitle("馬匹資料管理（horses）")
        self.resize(1100, 700)

        self._db_total_label = QLabel("")
        self._db_path_label = QLabel(str(self._db_path))

        self._keyword_input = QLineEdit()
        self._keyword_input.setPlaceholderText(
            "輸入馬名關鍵字（中文/英文；不含則顯示全部）"
        )

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(10, 5000)
        self._limit_spin.setValue(200)
        # Grid 要顯示全部，不再套用 LIMIT。
        self._limit_spin.setVisible(False)

        self._search_btn = QPushButton("搜尋")
        self._search_btn.clicked.connect(self._on_search_clicked)

        self._refresh_btn = QPushButton("重新整理")
        self._refresh_btn.clicked.connect(self._reload_default)

        self._update_btn = QPushButton("更新馬匹資料")
        self._update_btn.clicked.connect(self._on_update_clicked)

        self._table = QTableWidget(0, 5)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setHorizontalHeaderLabels(
            ["HorseId", "Brand No.", "Name EN", "Name ZH", "Status"]
        )
        self._table.itemSelectionChanged.connect(self._sync_details_from_selection)
        self._table.itemDoubleClicked.connect(self._on_item_double_clicked)

        self._id_field = QLineEdit()
        self._id_field.setReadOnly(True)
        self._brand_no_field = QLineEdit()
        self._brand_no_field.setReadOnly(True)
        self._name_en_field = QLineEdit()
        self._name_en_field.setReadOnly(True)
        self._name_zh_field = QLineEdit()
        self._name_zh_field.setReadOnly(True)
        self._status_field = QLineEdit()
        self._status_field.setReadOnly(True)

        form = QFormLayout()
        form.addRow("HorseId", self._id_field)
        form.addRow("Brand No.", self._brand_no_field)
        form.addRow("Name EN", self._name_en_field)
        form.addRow("Name ZH", self._name_zh_field)
        form.addRow("Status", self._status_field)

        choose_btn = QPushButton("選擇此馬 → 回主畫面")
        choose_btn.clicked.connect(self._on_choose_clicked)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(self.close)

        # Layout
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("資料來源："))
        header_row.addWidget(QLabel("SQLite"))
        header_row.addWidget(QLabel("DB："))
        header_row.addWidget(self._db_path_label, 1)

        sub_header_row = QHBoxLayout()
        sub_header_row.addWidget(QLabel("horses 共："))
        sub_header_row.addWidget(self._db_total_label)
        sub_header_row.addStretch(1)

        self._limit_label = QLabel("顯示前")
        self._limit_unit_label = QLabel("筆")

        # Grid 要顯示全部，不再套用 LIMIT。
        self._limit_label.setVisible(False)
        self._limit_unit_label.setVisible(False)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜尋："))
        search_row.addWidget(self._keyword_input, 1)
        search_row.addWidget(self._limit_label)
        search_row.addWidget(self._limit_spin)
        search_row.addWidget(self._limit_unit_label)

        search_row.addWidget(self._search_btn)
        search_row.addWidget(self._refresh_btn)
        search_row.addWidget(self._update_btn)

        tabs = QTabWidget()
        horses_tab = QWidget()
        tab_layout = QVBoxLayout()
        tab_layout.addLayout(header_row)
        tab_layout.addLayout(sub_header_row)
        tab_layout.addLayout(search_row)
        tab_layout.addWidget(self._table, 1)
        horses_tab.setLayout(tab_layout)
        tabs.addTab(horses_tab, "horses")

        details_row = QVBoxLayout()
        details_row.addLayout(form)
        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        buttons_row.addWidget(choose_btn)
        buttons_row.addWidget(close_btn)
        details_row.addLayout(buttons_row)

        root = QVBoxLayout()
        root.addWidget(tabs, 1)
        root.addLayout(details_row)
        self.setLayout(root)

        self._reload_default()

    def _set_status_total(self) -> None:
        total = count_horses(self._db_path)
        self._db_total_label.setText(str(total))

    def _populate_table(self, rows: list) -> None:
        self._table.setRowCount(0)
        for r in rows:
            row_idx = self._table.rowCount()
            self._table.insertRow(row_idx)

            horse_id = str(r["horse_id"]) if r["horse_id"] is not None else ""
            brand_no = r["brand_no"] or ""
            name_en = r["name_en"] or ""
            name_zh = r["name_zh"] or ""
            status = r["status"] or ""

            values = [horse_id, brand_no, name_en, name_zh, status]
            for col, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                if col in (0, 1):
                    item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(row_idx, col, item)

        self._table.resizeColumnsToContents()

    def _selected_horse_id(self) -> str | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        if item is None:
            return None
        v = item.text().strip()
        return v or None

    def _sync_details_from_selection(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return

        id_item = self._table.item(row, 0)
        brand_no_item = self._table.item(row, 1)
        en_item = self._table.item(row, 2)
        zh_item = self._table.item(row, 3)
        status_item = self._table.item(row, 4)
        self._id_field.setText(id_item.text().strip() if id_item else "")
        self._brand_no_field.setText(brand_no_item.text().strip() if brand_no_item else "")
        self._name_en_field.setText(en_item.text().strip() if en_item else "")
        self._name_zh_field.setText(zh_item.text().strip() if zh_item else "")
        self._status_field.setText(status_item.text().strip() if status_item else "")

    def _reload_default(self) -> None:
        self._set_status_total()

        keyword = self._keyword_input.text().strip()
        limit: int | None = None  # Grid 顯示全部，不在 DB query 加 LIMIT。
        if keyword:
            rows = search_horses_by_name(self._db_path, keyword, limit=limit)
        else:
            rows = fetch_horses(self._db_path, limit=limit)

        self._populate_table(rows)
        self._sync_details_from_selection()

    def _on_search_clicked(self) -> None:
        self._reload_default()

    def _on_update_clicked(self) -> None:
        self._update_btn.setEnabled(False)
        started_at = datetime.now()
        before_total = count_horses(self._db_path)

        dlg = HorsesUpdateProgressDialog(self, self._db_path)

        def on_ok(obj: object) -> None:
            self._update_btn.setEnabled(True)
            stats = obj if isinstance(obj, HorsesUpdateStats) else None
            if stats is None:
                QMessageBox.critical(self, "更新失敗", "更新回傳資料格式不正確。")
                return

            self._set_status_total()
            finished_at = datetime.now()
            after_total = count_horses(self._db_path)
            added = max(after_total - before_total, 0)
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
                        f"horses 總筆數：{after_total} 筆",
                    ]
                ),
            )
            self._reload_default()

        dlg.finished_ok.connect(on_ok)
        dlg.start()

    def _on_choose_clicked(self) -> None:
        horse_id = self._selected_horse_id()
        if not horse_id:
            QMessageBox.warning(self, "未選擇", "請在表格選擇一筆。")
            return
        self._on_select(horse_id)
        self.close()

    def _on_item_double_clicked(self, _item: QTableWidgetItem) -> None:
        self._on_choose_clicked()


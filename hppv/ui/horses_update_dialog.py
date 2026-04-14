from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..db import upsert_horses
from ..scraper import HorsesUpdateStats, fetch_all_horses_basic_info


class _HorsesUpdateWorker(QObject):
    stage_changed = Signal(str)
    finished_ok = Signal(object)  # HorsesUpdateStats
    finished_error = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            self.stage_changed.emit("抓取馬匹清單中…")
            basics = fetch_all_horses_basic_info()
            if self._cancelled:
                self.finished_error.emit("已取消。")
                return

            self.stage_changed.emit(f"寫入資料庫中…（共 {len(basics)} 筆）")
            written = upsert_horses(
                self._db_path,
                [
                    {
                        "horse_id": h.horse_id,
                        "brand_no": h.brand_no,
                        "name_zh": h.name_zh,
                        "name_en": h.name_en,
                        "status": h.status,
                    }
                    for h in basics
                ],
            )
            self.finished_ok.emit(HorsesUpdateStats(total_found=len(basics), written=written))
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))


class HorsesUpdateProgressDialog(QDialog):
    """更新 horses table 的進度視窗（背景執行、不中斷 UI）。"""

    finished_ok = Signal(object)  # HorsesUpdateStats

    def __init__(self, parent, db_path: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("更新馬匹資料")
        self.setModal(True)
        self.resize(520, 160)

        self._status_label = QLabel("準備中…")
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._cancel_btn)

        root = QVBoxLayout()
        root.addWidget(self._status_label)
        root.addWidget(self._progress)
        root.addLayout(row)
        self.setLayout(root)

        self._thread = QThread(self)
        self._worker = _HorsesUpdateWorker(db_path)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.stage_changed.connect(self._status_label.setText)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)

        self._worker.finished_ok.connect(lambda _: self._thread.quit())
        self._worker.finished_error.connect(lambda _: self._thread.quit())
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> None:
        self._thread.start()
        self.exec()

    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("取消中…")
        self._worker.cancel()

    def _on_finished_ok(self, stats: object) -> None:
        self.finished_ok.emit(stats)
        self.accept()

    def _on_finished_error(self, msg: str) -> None:
        QMessageBox.critical(self, "更新失敗", msg)
        self.reject()


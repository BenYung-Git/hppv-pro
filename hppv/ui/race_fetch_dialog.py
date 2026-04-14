from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..db import fetch_horses, upsert_race_records
from ..scraper import resolve_hkjc_fetch_ids, scrape_horse_past_performance


@dataclass(frozen=True, slots=True)
class RaceFetchStats:
    horse_id: str
    fetched: int
    saved: int
    inserted: int
    selected_year: str
    available_years: list[str]


@dataclass(frozen=True, slots=True)
class RaceFetchAllStats:
    total_horses: int
    success_horses: int
    failed_horses: int
    fetched: int
    saved: int
    inserted: int
    selected_year: str


def _extract_year(date_text: str) -> str:
    if not date_text:
        return ""
    m4 = re.search(r"(19|20)\d{2}", date_text)
    if m4:
        return m4.group(0)
    m2 = re.search(r"\b\d{1,2}/\d{1,2}/(\d{2})\b", date_text)
    if m2:
        yy = int(m2.group(1))
        return f"{2000 + yy:04d}" if yy <= 69 else f"{1900 + yy:04d}"
    return ""


class _RaceFetchWorker(QObject):
    stage_changed = Signal(str)
    finished_ok = Signal(object)  # RaceFetchStats
    finished_error = Signal(str)

    def __init__(self, db_path: Path, horse_id: str, url_template: str, selected_year: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._horse_id = horse_id
        self._url_template = url_template
        self._selected_year = selected_year
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            url_hid, store_hid = resolve_hkjc_fetch_ids(self._db_path, self._horse_id)
            hint = f"（HKJC：{url_hid}）" if url_hid != self._horse_id else ""
            self.stage_changed.emit(f"抓取往績中…（{self._horse_id}{hint}）")
            result = scrape_horse_past_performance(
                self._horse_id, self._url_template, db_path=self._db_path
            )
            if self._cancelled:
                self.finished_error.emit("已取消。")
                return

            records_to_save = (
                [r for r in result.records if _extract_year((r.race_date or "").strip()) == self._selected_year]
                if self._selected_year
                else result.records
            )
            self.stage_changed.emit(f"寫入資料庫中…（{len(records_to_save)} 筆）")
            inserted = upsert_race_records(self._db_path, records_to_save)
            available_years = sorted(
                {y for y in (_extract_year((r.race_date or "").strip()) for r in result.records) if y},
                reverse=True,
            )

            self.finished_ok.emit(
                RaceFetchStats(
                    horse_id=store_hid,
                    fetched=len(result.records),
                    saved=len(records_to_save),
                    inserted=inserted,
                    selected_year=self._selected_year,
                    available_years=available_years,
                )
            )
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))


class _RaceFetchAllWorker(QObject):
    stage_changed = Signal(str)
    finished_ok = Signal(object)  # RaceFetchAllStats
    finished_error = Signal(str)

    def __init__(self, db_path: Path, url_template: str, selected_year: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._url_template = url_template
        self._selected_year = selected_year
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            horses = fetch_horses(self._db_path, limit=None)
            total = len(horses)
            if total == 0:
                self.finished_ok.emit(
                    RaceFetchAllStats(
                        total_horses=0,
                        success_horses=0,
                        failed_horses=0,
                        fetched=0,
                        saved=0,
                        inserted=0,
                        selected_year=self._selected_year,
                    )
                )
                return

            success = 0
            failed = 0
            fetched_total = 0
            saved_total = 0
            inserted_total = 0

            for i, row in enumerate(horses, start=1):
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
                horse_id = str(row["horse_id"] or "").strip()
                if not horse_id:
                    continue
                self.stage_changed.emit(f"抓取中…（{i}/{total}：{horse_id}）")
                try:
                    result = scrape_horse_past_performance(
                        horse_id, self._url_template, db_path=self._db_path
                    )
                    records_to_save = (
                        [r for r in result.records if _extract_year((r.race_date or "").strip()) == self._selected_year]
                        if self._selected_year
                        else result.records
                    )
                    inserted = upsert_race_records(self._db_path, records_to_save)
                    success += 1
                    fetched_total += len(result.records)
                    saved_total += len(records_to_save)
                    inserted_total += inserted
                except Exception:
                    failed += 1
                    continue

            self.finished_ok.emit(
                RaceFetchAllStats(
                    total_horses=total,
                    success_horses=success,
                    failed_horses=failed,
                    fetched=fetched_total,
                    saved=saved_total,
                    inserted=inserted_total,
                    selected_year=self._selected_year,
                )
            )
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))


class RaceFetchProgressDialog(QDialog):
    """抓取並入庫（往績）的進度視窗。"""

    finished_ok = Signal(object)  # RaceFetchStats

    def __init__(self, parent, db_path: Path, horse_id: str, url_template: str, selected_year: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("抓取並入庫")
        self.setModal(True)
        self.resize(520, 160)

        self._status_label = QLabel("準備中…")
        self._started_at = datetime.now()
        self._started_label = QLabel(f"開始時間：{self._started_at:%Y-%m-%d %H:%M:%S}")
        self._elapsed_label = QLabel("已執行：00:00:00")
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_elapsed)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._cancel_btn)

        root = QVBoxLayout()
        root.addWidget(self._status_label)
        root.addWidget(self._started_label)
        root.addWidget(self._elapsed_label)
        root.addWidget(self._progress)
        root.addLayout(row)
        self.setLayout(root)

        self._thread = QThread(self)
        self._worker = _RaceFetchWorker(db_path, horse_id, url_template, selected_year)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.stage_changed.connect(self._status_label.setText)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)

        self._worker.finished_ok.connect(lambda _: self._thread.quit())
        self._worker.finished_error.connect(lambda _: self._thread.quit())
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> None:
        self._timer.start(1000)
        self._thread.start()
        self.exec()

    def _update_elapsed(self) -> None:
        sec = int((datetime.now() - self._started_at).total_seconds())
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        self._elapsed_label.setText(f"已執行：{h:02d}:{m:02d}:{s:02d}")

    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("取消中…")
        self._worker.cancel()

    def _on_finished_ok(self, stats: object) -> None:
        self._timer.stop()
        self.finished_ok.emit(stats)
        self.accept()

    def _on_finished_error(self, msg: str) -> None:
        self._timer.stop()
        QMessageBox.critical(self, "抓取失敗", msg)
        self.reject()


class RaceFetchAllProgressDialog(QDialog):
    """抓取全部馬匹往績（所有年份）的進度視窗。"""

    finished_ok = Signal(object)  # RaceFetchAllStats

    def __init__(self, parent, db_path: Path, url_template: str, selected_year: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("抓取全部馬匹往績")
        self.setModal(True)
        self.resize(560, 170)

        self._status_label = QLabel("準備中…")
        self._started_at = datetime.now()
        self._started_label = QLabel(f"開始時間：{self._started_at:%Y-%m-%d %H:%M:%S}")
        self._elapsed_label = QLabel("已執行：00:00:00")
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_elapsed)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._cancel_btn)

        root = QVBoxLayout()
        root.addWidget(self._status_label)
        root.addWidget(self._started_label)
        root.addWidget(self._elapsed_label)
        root.addWidget(self._progress)
        root.addLayout(row)
        self.setLayout(root)

        self._thread = QThread(self)
        self._worker = _RaceFetchAllWorker(db_path, url_template, selected_year)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.stage_changed.connect(self._status_label.setText)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)

        self._worker.finished_ok.connect(lambda _: self._thread.quit())
        self._worker.finished_error.connect(lambda _: self._thread.quit())
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> None:
        self._timer.start(1000)
        self._thread.start()
        self.exec()

    def _update_elapsed(self) -> None:
        sec = int((datetime.now() - self._started_at).total_seconds())
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        self._elapsed_label.setText(f"已執行：{h:02d}:{m:02d}:{s:02d}")

    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("取消中…")
        self._worker.cancel()

    def _on_finished_ok(self, stats: object) -> None:
        self._timer.stop()
        self.finished_ok.emit(stats)
        self.accept()

    def _on_finished_error(self, msg: str) -> None:
        self._timer.stop()
        QMessageBox.critical(self, "抓取失敗", msg)
        self.reject()


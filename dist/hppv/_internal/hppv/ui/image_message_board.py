"""
HPPV 訊息板：以圖像為主、定時自動輪播（循環顯示，適合「最新資訊」展示）。

用法（通常由 dashboard_window 嵌入）：
    board = ImageMessageBoard()
    board.set_image_paths([r"C:\\path\\a.png", r"C:\\path\\b.png"])
    board.set_interval_ms(4000)
    board.start()
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


def _make_placeholder_pixmap(index: int, total: int, w: int = 800, h: int = 360) -> QPixmap:
    """無檔案時用色塊＋文字產生示範圖。"""
    colors = [
        QColor("#1e3a5f"),
        QColor("#2d5a4a"),
        QColor("#5a3d6b"),
        QColor("#6b4a2d"),
        QColor("#3d4a6b"),
    ]
    c = colors[index % len(colors)]
    pm = QPixmap(w, h)
    pm.fill(c)
    p = QPainter(pm)
    p.setPen(QColor("#ffffff"))
    p.setFont(QFont("Microsoft JhengHei", 28, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, f"示範圖像 {index + 1} / {total}")
    p.setFont(QFont("Microsoft JhengHei", 12))
    p.setPen(QColor("#ffffff"))
    p.drawText(
        pm.rect().adjusted(0, 80, 0, 0),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        "（請替換為實際 PNG/JPG 路徑）",
    )
    p.end()
    return pm


class ImageMessageBoard(QWidget):
    """
    訊息板主體：以 QLabel 全幅顯示圖像，QStackedWidget 切換；QTimer 循環輪播。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paths: list[str] = []
        self._pixmaps: list[QPixmap] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._show_next)
        self._interval_ms = 4000
        self._current = 0

        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._indicator = QLabel("")
        self._indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._indicator.setStyleSheet(
            "color: #888; font-size: 12px; padding: 4px; background: #f5f5f5; border-radius: 4px;"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(self._stack, 1)
        root.addWidget(self._indicator)

        self.setStyleSheet(
            """
            ImageMessageBoard {
                background: #eaeaea;
                border: 1px solid #ccc;
                border-radius: 8px;
            }
            """
        )

    def set_interval_ms(self, ms: int) -> None:
        self._interval_ms = max(500, int(ms))
        if self._timer.isActive():
            self._timer.setInterval(self._interval_ms)

    def set_image_paths(self, paths: Sequence[str | Path]) -> None:
        """設定圖檔路徑；若檔案不存在則略過該張，若全部失敗則使用內建色塊示範。"""
        self.stop()
        self._paths = [str(p) for p in paths]
        self._pixmaps = []
        self._clear_stack()

        valid: list[str] = []
        for p in self._paths:
            path = Path(p)
            if path.is_file():
                pm = QPixmap(str(path))
                if not pm.isNull():
                    valid.append(str(path))
                    self._pixmaps.append(pm)

        n = len(self._pixmaps)
        if n == 0:
            # 示範三張
            for i in range(3):
                self._pixmaps.append(_make_placeholder_pixmap(i, 3))
            n = 3

        for i in range(n):
            lab = QLabel()
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setScaledContents(False)
            lab.setMinimumHeight(320)
            lab.setPixmap(self._scaled_for_label(self._pixmaps[i], lab))
            self._stack.addWidget(lab)

        self._stack.setCurrentIndex(0)
        self._current = 0
        self._update_indicator()

    def _clear_stack(self) -> None:
        while self._stack.count():
            w = self._stack.widget(0)
            self._stack.removeWidget(w)
            w.deleteLater()

    def _scaled_for_label(self, pm: QPixmap, lab: QLabel) -> QPixmap:
        """依訊息板可用區域縮放，盡量放大顯示比例。"""
        w = max(480, self.width() - 24) if self.width() > 0 else 900
        # 扣除底部指示列約 36px，其餘高度給圖像
        avail_h = max(0, self.height() - 40) if self.height() > 0 else 420
        h = max(280, int(avail_h * 0.92)) if self.height() > 0 else 400
        return pm.scaled(
            w,
            h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        # 視窗大小改變時重算縮圖
        for i in range(self._stack.count()):
            w = self._stack.widget(i)
            if isinstance(w, QLabel) and i < len(self._pixmaps):
                w.setPixmap(self._scaled_for_label(self._pixmaps[i], w))

    def _show_next(self) -> None:
        n = self._stack.count()
        if n <= 1:
            return
        self._current = (self._current + 1) % n
        self._stack.setCurrentIndex(self._current)
        self._update_indicator()

    def _update_indicator(self) -> None:
        n = self._stack.count()
        if n == 0:
            self._indicator.setText("")
            return
        dots = " ".join("●" if i == self._current else "○" for i in range(n))
        self._indicator.setText(f"{dots}　{self._current + 1}/{n}")

    def start(self) -> None:
        if self._stack.count() <= 1:
            return
        self._timer.start(self._interval_ms)

    def stop(self) -> None:
        self._timer.stop()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._stack.count() > 1 and not self._timer.isActive():
            self._timer.start(self._interval_ms)

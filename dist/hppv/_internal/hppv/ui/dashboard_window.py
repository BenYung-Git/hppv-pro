"""
HPPV 主畫面 Dashboard（純 UI 預覽）

- 頂部：圖像訊息板（自動輪播，無標題列；見 image_message_board.ImageMessageBoard）
- 五種版面／配色效果，可用下拉選單切換（不執行其他視窗）
- 選單「賽事」可開啟主程式視窗（內嵌排位／馬匹往績）；Dashboard 其餘仍為預覽

執行：python -m hppv.ui.dashboard_window
"""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .image_message_board import ImageMessageBoard

HPPV_VERSION = "v0.1 (Preview)"


class HoverMenuBar(QMenuBar):
    """
    頂層選單：滑鼠移到有子選單的項目時即展開（不需先點擊）。
    Windows 上需關閉原生選單列，Qt 自繪後 hover 行為才一致。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setNativeMenuBar(False)
        self.setMouseTracking(True)
        self._last_hover_action: QAction | None = None
        # 選單列置中：左右 Expanding 占位，中間為 addMenu 項目（Qt 預設為靠左）
        left_balance = QWidget(self)
        left_balance.setObjectName("menuBarBalanceSpacer")
        left_balance.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        left_balance.setMinimumWidth(0)
        self.setCornerWidget(left_balance, Qt.Corner.TopLeftCorner)
        right_balance = QWidget(self)
        right_balance.setObjectName("menuBarBalanceSpacer")
        right_balance.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        right_balance.setMinimumWidth(0)
        self.setCornerWidget(right_balance, Qt.Corner.TopRightCorner)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        act = self.actionAt(event.position().toPoint())
        if act is not None and act.menu() is not None:
            if act is not self._last_hover_action:
                self._last_hover_action = act
                self.setActiveAction(act)


def _default_message_board_jpegs() -> list[str]:
    """
    專案內 `hppv/assets/message_board/horse_*.jpg`（馬主題相片）。
    若檔案不存在則回傳空列表，訊息板會改為內建色塊示範。
    """
    base = Path(__file__).resolve().parent.parent / "assets" / "message_board"
    names = ("horse_1.jpg", "horse_2.jpg", "horse_3.jpg")
    paths = [str(base / n) for n in names]
    if all(Path(p).is_file() for p in paths):
        return paths
    return []


def _stat_card(title: str, value: str, subtitle: str = "") -> QFrame:
    f = QFrame()
    f.setMinimumHeight(96)
    v = QVBoxLayout(f)
    t = QLabel(title)
    t.setObjectName("statTitle")
    num = QLabel(value)
    num.setObjectName("statValue")
    v.addWidget(t)
    v.addWidget(num)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("statSub")
        v.addWidget(s)
    v.addStretch()
    return f


class DashboardWindow(QMainWindow):
    """主畫面：Dashboard + 樣式切換 + 賽事選單（僅 UI）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("HPPV｜主畫面（Dashboard 預覽）")
        self.resize(1100, 720)
        self.setMenuBar(HoverMenuBar(self))
        self._main_win = None  # 延遲建立，避免與 Dashboard 同時載入過重
        self._active_horses_total = self._load_active_horses_total()
        self._race_meeting_date, self._race_meeting_course, self._race_count, self._stored_meetings = (
            self._load_racecard_snapshot()
        )
        self._build_menu_bar()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(12)
        root.setContentsMargins(16, 12, 16, 16)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("版面樣式："))
        self._style_combo = QComboBox()
        self._style_combo.addItems(
            [
                "① 經典卡片（藍白）",
                "② 深色儀表（霓虹邊）",
                "③ 極簡留白（淺灰）",
                "④ 資訊密度（KPI 帶狀）",
                "⑤ 玻璃擬態（漸層底）",
            ]
        )
        self._style_combo.setMinimumWidth(280)
        bar.addWidget(self._style_combo)
        bar.addSpacing(12)
        bar.addWidget(QLabel("訊息圖像大小："))
        self._message_size_combo = QComboBox()
        self._message_size_combo.addItems(
            [
                "① 自適應（預設）",
                "② 小",
                "③ 中",
                "④ 大",
                "⑤ 特大",
            ]
        )
        self._message_size_combo.setMinimumWidth(180)
        self._message_size_combo.setToolTip("調整上方「最新訊息圖像」顯示區高度；圖像會自動等比縮放。")
        bar.addWidget(self._message_size_combo)
        bar.addStretch()
        root.addLayout(bar)

        self._message_board = ImageMessageBoard()
        self._message_board.setMinimumHeight(400)
        self._message_board.set_interval_ms(4000)
        self._message_board.set_image_paths(_default_message_board_jpegs())
        self._message_board.start()
        root.addWidget(self._message_board, 1)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_classic_cards())
        self._stack.addWidget(self._page_dark_neon())
        self._stack.addWidget(self._page_minimal())
        self._stack.addWidget(self._page_dense_kpi())
        self._stack.addWidget(self._page_glass_gradient())

        self._style_combo.currentIndexChanged.connect(self._stack.setCurrentIndex)
        self._message_size_combo.currentIndexChanged.connect(self._apply_message_board_size)
        root.addWidget(self._stack, 2)
        self._apply_message_board_size(0)

        # 選單列樣式（含置中用 spacer 底色）；版面樣式下拉已移除「菜單顏色」時仍套用經典選單列
        self._apply_menu_style(0)
        self.statusBar().showMessage("已就緒：請從『賽事』選單開啟項目。")

    def _load_active_horses_total(self) -> int:
        """讀取目前資料庫現役馬匹總數（Dashboard 顯示用）。"""
        try:
            from ..config import default_config
            from ..db import count_horses

            return int(count_horses(default_config().db_path))
        except Exception:
            return 0

    def _active_horses_total_text(self) -> str:
        return f"{self._active_horses_total:,}"

    def _active_horses_total_short(self) -> str:
        n = self._active_horses_total
        if n >= 1000:
            return f"{n / 1000:.1f}k".rstrip("0").rstrip(".")
        return str(n)

    def _load_racecard_snapshot(self) -> tuple[str, str, int, int]:
        """
        讀取排位快照：最新賽日、場地、場次與已存賽日數。
        回傳 (race_date, racecourse, race_count, stored_meetings)。
        """
        try:
            from ..config import default_config

            db_path = default_config().db_path
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                latest = conn.execute(
                    """
                    SELECT race_date, racecourse, COUNT(DISTINCT race_no) AS race_count
                    FROM racecard_starters
                    GROUP BY race_date, racecourse
                    ORDER BY race_date DESC, racecourse ASC
                    LIMIT 1;
                    """
                ).fetchone()
                stored = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM (
                        SELECT race_date, racecourse
                        FROM racecard_starters
                        GROUP BY race_date, racecourse
                    ) x;
                    """
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return ("—", "—", 0, 0)

        if not latest:
            return ("—", "—", 0, 0)

        race_date = str(latest["race_date"] or "—").strip() or "—"
        racecourse = str(latest["racecourse"] or "—").strip() or "—"
        race_count = int(latest["race_count"] or 0)
        stored_meetings = int(stored["c"] or 0) if stored else 0
        return (race_date, racecourse, race_count, stored_meetings)

    def _racecourse_display(self, code: str) -> str:
        mapping = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}
        c = str(code or "").strip().upper()
        return mapping.get(c, c or "—")

    def _meeting_text(self) -> str:
        return f"{self._race_meeting_date}｜{self._racecourse_display(self._race_meeting_course)}"

    def _apply_message_board_size(self, idx: int) -> None:
        """切換「最新訊息圖像」顯示尺寸（高度）。"""
        # 以高度控制為主；ImageMessageBoard 內部會依可用區域自動等比縮放。
        size_map = {
            1: 300,  # 小
            2: 380,  # 中
            3: 460,  # 大
            4: 560,  # 特大
        }
        h = size_map.get(idx, 400)  # 自適應預設基準
        self._message_board.setMinimumHeight(h)
        # 設為不限制上限，讓使用者拉大視窗時仍可放大展示。
        self._message_board.setMaximumHeight(16777215)
        self._message_board.update()

    def _build_menu_bar(self) -> None:
        bar = self.menuBar()
        m_race = bar.addMenu("賽事")
        a_rc = QAction("排位表…", self)
        a_rr = QAction("往績／馬匹…", self)
        a_rc.triggered.connect(self._on_menu_open_racecard_shell)
        a_rr.triggered.connect(self._on_menu_open_horses_shell)
        m_race.addAction(a_rc)
        m_race.addAction(a_rr)
        m_race.addSeparator()
        a_tip = QAction("（將開啟主程式視窗：SQLite + 網路抓取）", self)
        a_tip.setEnabled(False)
        m_race.addAction(a_tip)

        m_tools = bar.addMenu("工具")
        m_tools.addAction("（預留）")
        m_settings = bar.addMenu("系統設定")
        a_task_settings = QAction("任務設定", self)
        a_task_settings.triggered.connect(self._show_task_settings_dialog)
        m_settings.addAction(a_task_settings)
        m_help = bar.addMenu("說明")
        a_intro = QAction("查閱系統簡介", self)
        a_intro.triggered.connect(self._show_system_intro_dialog)
        m_help.addAction(a_intro)
        a_about = QAction("關於 HPPV…", self)
        a_about.triggered.connect(self._show_about_hppv_dialog)
        m_help.addAction(a_about)

    def _ensure_main_window(self):
        """建立或重用主程式視窗（與 `python -m hppv` 相同）。"""
        if self._main_win is None:
            from ..config import default_config
            from .main_window import MainWindow

            busy = QProgressDialog("正在開啟主程式與排位資料，請稍候…", "", 0, 0, self)
            busy.setWindowTitle("載入中")
            busy.setWindowModality(Qt.WindowModality.WindowModal)
            busy.setCancelButton(None)
            busy.setMinimumDuration(0)
            busy.show()
            QApplication.processEvents()
            try:
                self._main_win = MainWindow(default_config())
            finally:
                busy.close()
        return self._main_win

    def _on_menu_open_racecard_shell(self) -> None:
        self._ensure_main_window().show_racecard_shell()
        self.statusBar().showMessage("已開啟：主程式 > 排位")

    def _on_menu_open_horses_shell(self) -> None:
        self._ensure_main_window().show_horses_shell()
        self.statusBar().showMessage("已開啟：主程式 > 馬匹／往績")

    def _build_menu_display_stack(self) -> QStackedWidget:
        """不同菜單顯示方式預覽（純 UI，不改動功能路由）。"""
        stack = QStackedWidget()
        stack.addWidget(self._menu_view_traditional())
        stack.addWidget(self._menu_view_tree())
        stack.addWidget(self._menu_view_list())
        stack.addWidget(self._menu_view_quick_buttons())
        stack.addWidget(self._menu_view_cards())
        return stack

    def _menu_view_traditional(self) -> QWidget:
        w = QFrame()
        w.setStyleSheet("QFrame{background:#f7f9fc;border:1px solid #d7deeb;border-radius:8px;}")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 8, 12, 8)
        for text in ["Dashboard", "排位表", "往績", "馬匹", "說明"]:
            lbl = QLabel(text)
            lbl.setStyleSheet("padding:4px 10px; color:#32445f;")
            lay.addWidget(lbl)
        lay.addStretch()
        return w

    def _menu_view_tree(self) -> QWidget:
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setMaximumHeight(140)
        tree.setStyleSheet(
            "QTreeWidget{background:#fbfcff;border:1px solid #d7deeb;border-radius:8px;padding:4px;}"
        )
        root_race = QTreeWidgetItem(["賽事"])
        root_race.addChild(QTreeWidgetItem(["排位表"]))
        root_race.addChild(QTreeWidgetItem(["往績"]))
        root_data = QTreeWidgetItem(["資料"])
        root_data.addChild(QTreeWidgetItem(["馬匹資料"]))
        root_data.addChild(QTreeWidgetItem(["統計總覽"]))
        root_help = QTreeWidgetItem(["說明"])
        root_help.addChild(QTreeWidgetItem(["查閱系統簡介"]))
        root_help.addChild(QTreeWidgetItem(["關於 HPPV"]))
        tree.addTopLevelItems([root_race, root_data, root_help])
        tree.expandAll()
        return tree

    def _menu_view_list(self) -> QWidget:
        lst = QListWidget()
        lst.setMaximumHeight(130)
        lst.setStyleSheet(
            "QListWidget{background:#ffffff;border:1px solid #d7deeb;border-radius:8px;padding:4px;}"
            "QListWidget::item{padding:6px 8px;}"
        )
        for it in ["首頁總覽", "排位查閱", "往績查閱", "馬匹資料", "系統說明"]:
            lst.addItem(QListWidgetItem(it))
        lst.setCurrentRow(0)
        return lst

    def _menu_view_quick_buttons(self) -> QWidget:
        w = QFrame()
        w.setStyleSheet("QFrame{background:#f9fafc;border:1px solid #d7deeb;border-radius:8px;}")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 8)
        for text in ["首頁", "排位", "往績", "馬匹", "說明"]:
            btn = QPushButton(text)
            btn.setStyleSheet(
                "QPushButton{background:#eef3fb;border:1px solid #c8d5ea;border-radius:6px;padding:6px 12px;}"
                "QPushButton:hover{background:#e1ebf9;}"
            )
            lay.addWidget(btn)
        lay.addStretch()
        return w

    def _menu_view_cards(self) -> QWidget:
        w = QFrame()
        w.setStyleSheet("QFrame{background:#f6f8fc;border:1px solid #d7deeb;border-radius:8px;}")
        grid = QGridLayout(w)
        grid.setContentsMargins(10, 8, 10, 8)
        items = ["排位", "往績", "馬匹", "統計", "說明", "設定"]
        for i, text in enumerate(items):
            card = QLabel(text)
            card.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.setStyleSheet(
                "background:#ffffff;border:1px solid #cfd9ea;border-radius:6px;padding:10px;color:#2f4261;"
            )
            grid.addWidget(card, i // 3, i % 3)
        return w

    def _apply_menu_style(self, idx: int) -> None:
        """切換選單列視覺效果（僅 UI 展示）。"""
        if idx == 1:  # 深色
            qss = """
QMenuBar { background: #1d1f27; color: #f2f4f8; border-bottom: 1px solid #34384a; }
QMenuBar::item { background: transparent; padding: 6px 12px; }
QMenuBar::item:selected { background: #2f3b56; border-radius: 4px; }
QWidget#menuBarBalanceSpacer { background: #1d1f27; }
QMenu { background: #252938; color: #f2f4f8; border: 1px solid #3b4359; }
QMenu::item:selected { background: #3a4b76; }
"""
        elif idx == 2:  # 極簡
            qss = """
QMenuBar { background: #ffffff; color: #222; border-bottom: 1px solid #e6e6e6; }
QMenuBar::item { padding: 4px 10px; margin: 0px 2px; }
QMenuBar::item:selected { background: #f3f3f3; }
QWidget#menuBarBalanceSpacer { background: #ffffff; }
QMenu { background: #ffffff; color: #222; border: 1px solid #dcdcdc; }
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: #f1f4f8; color: #111; }
"""
        elif idx == 3:  # 高對比
            qss = """
QMenuBar { background: #0d47a1; color: #ffffff; border-bottom: 1px solid #08357a; font-weight: 600; }
QMenuBar::item { padding: 6px 12px; }
QMenuBar::item:selected { background: #1565c0; }
QWidget#menuBarBalanceSpacer { background: #0d47a1; }
QMenu { background: #ffffff; color: #0d47a1; border: 1px solid #0d47a1; }
QMenu::item:selected { background: #e3f2fd; color: #0b3c85; }
"""
        elif idx == 4:  # 圓角
            qss = """
QMenuBar { background: #f7f8fb; color: #334; border-bottom: 1px solid #d8dce6; }
QMenuBar::item { padding: 6px 12px; margin: 3px 4px; border-radius: 8px; }
QMenuBar::item:selected { background: #e7ebf5; }
QWidget#menuBarBalanceSpacer { background: #f7f8fb; }
QMenu { background: #ffffff; color: #334; border: 1px solid #d8dce6; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 7px 16px; border-radius: 6px; }
QMenu::item:selected { background: #edf1fa; }
"""
        else:  # 經典
            qss = """
QMenuBar { background: #ffffff; color: #1f2d3d; border-bottom: 1px solid #cfd6e4; }
QMenuBar::item { padding: 5px 10px; }
QMenuBar::item:selected { background: #e9eff9; }
QWidget#menuBarBalanceSpacer { background: #ffffff; }
QMenu { background: #ffffff; color: #1f2d3d; border: 1px solid #cfd6e4; }
QMenu::item:selected { background: #e9eff9; }
"""
        self.menuBar().setStyleSheet(qss)

    def _show_system_intro_dialog(self) -> None:
        intro_text = """
<h3>HPPV 系統簡介</h3>
<p><b>目的</b><br>
HPPV 旨在整合「排位表、馬匹資料、往績紀錄與即時資訊」，提供一個可視化且可持續擴充的賽馬資料工作台，協助使用者更快掌握賽事重點與資料變化。</p>

<p><b>主要功能</b></p>
<ul>
  <li><b>排位表管理：</b>自動抓取最新排位、支援查閱已儲存賽日、可手動補抓指定日期與場地。</li>
  <li><b>馬匹與往績整合：</b>以馬匹為核心檢視歷史賽績，便於對照當日出賽馬與過往表現。</li>
  <li><b>賠率欄位：</b>提供預測賠率與即時賠率欄位，方便後續策略比較與視覺化展示。</li>
  <li><b>Dashboard 展示：</b>支援多種版面風格切換，快速預覽統計重點與資訊版型。</li>
  <li><b>訊息板輪播：</b>以圖像方式循環顯示最新資訊，適合主畫面公告或焦點內容展示。</li>
</ul>

<p><b>使用建議</b><br>
先更新或補抓排位資料，再從主畫面切換版面風格與檢視重點指標；若需分析指定歷史賽日，可透過「查閱過往」快速載入。</p>
""".strip()

        dlg = QDialog(self)
        dlg.setWindowTitle("系統簡介")
        dlg.resize(780, 560)
        lay = QVBoxLayout(dlg)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(intro_text)
        lay.addWidget(browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        dlg.exec()

    def _show_task_settings_dialog(self) -> None:
        main = self._ensure_main_window()
        main.open_task_settings_dialog()
        self.statusBar().showMessage("已開啟：主程式 > 系統設定 > 任務設定")

    def _show_about_hppv_dialog(self) -> None:
        about_text = f"""
<h3>關於 HPPV</h3>
<p><b>系統名稱：</b>HPPV (Horse Performance &amp; Position Viewer)<br>
<b>版本：</b>{HPPV_VERSION}<br>
<b>定位：</b>本地化賽馬資料整合與檢視平台</p>

<p><b>系統目的</b><br>
整合「排位表、馬匹資料、往績」三大資料來源，提供單一入口進行查閱、比對與後續分析準備。</p>

<p><b>核心功能</b></p>
<ul>
  <li>排位表抓取與歷史賽日查閱</li>
  <li>馬匹資料與往績整合檢視</li>
  <li>預測賠率／即時賠率欄位展示</li>
  <li>Dashboard 多版面視覺化預覽</li>
  <li>主畫面圖像輪播訊息板</li>
</ul>

<p><b>資料來源</b><br>
香港賽馬會公開頁面及相關公開資料介面（以實際頁面內容為準）。</p>

<p><b>免責聲明</b><br>
本系統僅供資料整理與研究使用，不構成任何投注建議。</p>
""".strip()

        dlg = QDialog(self)
        dlg.setWindowTitle("關於 HPPV")
        dlg.resize(700, 520)
        lay = QVBoxLayout(dlg)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(about_text)
        lay.addWidget(browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        dlg.exec()

    def _wrap_scroll(self, inner: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(inner)
        return sc

    # --- 樣式 ① 經典卡片 ---
    def _page_classic_cards(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(
            """
            QWidget#classicRoot { background: #f5f7fb; }
            QFrame#classicCard {
                background: #ffffff;
                border: 1px solid #d0d8e8;
                border-radius: 10px;
                padding: 16px;
            }
            QLabel#statTitle { color: #5a6b85; font-size: 13px; }
            QLabel#statValue { color: #1a3a6e; font-size: 26px; font-weight: 700; }
            QLabel#statSub { color: #8899aa; font-size: 11px; }
            QLabel#dashTitle { color: #1a3a6e; font-size: 20px; font-weight: 700; }
            QLabel#dashHint { color: #6b7c90; font-size: 12px; }
            """
        )
        w.setObjectName("classicRoot")
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("總覽", objectName="dashTitle"))
        lay.addWidget(
            QLabel("示範統計（假資料，僅看版面）", objectName="dashHint")
        )

        grid = QGridLayout()
        grid.setSpacing(16)
        cards = [
            ("現役馬匹總數", self._active_horses_total_text(), "上次同步：—"),
            ("往績紀錄", "91,203", "含本地賽事"),
            ("已存排位賽日", f"{self._stored_meetings}", "資料庫內不重複日期"),
            ("賽日 / 場地 / 場次", f"{self._race_count}", self._meeting_text()),
        ]
        for i, (t, v, s) in enumerate(cards):
            f = QFrame()
            f.setObjectName("classicCard")
            vbox = QVBoxLayout(f)
            tt = QLabel(t)
            tt.setObjectName("statTitle")
            vv = QLabel(v)
            vv.setObjectName("statValue")
            ss = QLabel(s)
            ss.setObjectName("statSub")
            vbox.addWidget(tt)
            vbox.addWidget(vv)
            vbox.addWidget(ss)
            grid.addWidget(f, i // 2, i % 2)

        lay.addLayout(grid)
        lay.addStretch()
        return self._wrap_scroll(w)

    # --- 樣式 ② 深色 + 霓虹邊 ---
    def _page_dark_neon(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(
            """
            QWidget#darkRoot { background: #12121c; }
            QFrame#neonCard {
                background: #1a1a28;
                border: 1px solid #3dffc8;
                border-radius: 8px;
                padding: 14px;
            }
            QLabel#dTitle { color: #8a9bb0; font-size: 12px; letter-spacing: 1px; }
            QLabel#dValue { color: #e8fff6; font-size: 24px; font-weight: 600; }
            QLabel#dSub { color: #5c6d82; font-size: 11px; }
            QLabel#dHead { color: #3dffc8; font-size: 18px; font-weight: 700; }
            """
        )
        w.setObjectName("darkRoot")
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("DASHBOARD // HKJC", objectName="dHead"))

        row = QHBoxLayout()
        for title, val in [
            ("HORSES", self._active_horses_total_short()),
            ("RECORDS", "91k"),
            ("MEETINGS", "38"),
            ("RACES", "11"),
        ]:
            f = QFrame()
            f.setObjectName("neonCard")
            f.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            vb = QVBoxLayout(f)
            vb.addWidget(QLabel(title, objectName="dTitle"))
            vb.addWidget(QLabel(val, objectName="dValue"))
            vb.addWidget(QLabel("demo", objectName="dSub"))
            row.addWidget(f)
        lay.addLayout(row)

        note = QLabel("深色主題適合長時間盯盤；邊框可用品牌色替換。")
        note.setStyleSheet("color: #6a7d92; font-size: 12px; margin-top: 12px;")
        lay.addWidget(note)
        lay.addStretch()
        return self._wrap_scroll(w)

    # --- 樣式 ③ 極簡留白 ---
    def _page_minimal(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(
            """
            QWidget#minRoot { background: #fafafa; }
            QFrame#minLine {
                background: #ffffff;
                border: none;
                border-bottom: 1px solid #e8e8e8;
                padding: 20px 8px;
            }
            QLabel#mLabel { color: #888; font-size: 12px; }
            QLabel#mVal { color: #222; font-size: 22px; font-weight: 300; }
            QLabel#mPageTitle { color: #111; font-size: 22px; font-weight: 300; letter-spacing: 2px; }
            """
        )
        w.setObjectName("minRoot")
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("統 計", objectName="mPageTitle"))
        lay.addSpacing(8)

        items = [
            ("現役馬匹總數", self._active_horses_total_text()),
            ("往績", "91,203"),
            ("已存排位賽日", f"{self._stored_meetings}"),
            ("賽日/場地", self._meeting_text()),
        ]
        for label, val in items:
            line = QFrame()
            line.setObjectName("minLine")
            hl = QHBoxLayout(line)
            hl.addWidget(QLabel(label, objectName="mLabel"))
            hl.addStretch()
            hl.addWidget(QLabel(val, objectName="mVal"))
            lay.addWidget(line)

        tip = QLabel("大量留白、細字重，適合列印或投影簡報風格。")
        tip.setStyleSheet("color: #aaa; font-size: 11px; margin: 16px 8px;")
        lay.addWidget(tip)
        lay.addStretch()
        return self._wrap_scroll(w)

    # --- 樣式 ④ 資訊密度 KPI ---
    def _page_dense_kpi(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(
            """
            QWidget#denseRoot { background: #eef1f5; }
            QFrame#kpiBar {
                background: #0d47a1;
                color: white;
                border-radius: 4px;
                padding: 10px 16px;
            }
            QFrame#denseBox {
                background: #fff;
                border: 1px solid #ccd3dd;
                border-radius: 4px;
                padding: 10px;
            }
            QLabel#kpiText { color: white; font-size: 13px; }
            QLabel#kpiNum { color: #ffeb3b; font-size: 20px; font-weight: 700; }
            QLabel#secTitle { font-weight: 700; color: #333; }
            QLabel#cell { font-size: 12px; color: #444; }
            """
        )
        w.setObjectName("denseRoot")
        main = QVBoxLayout(w)

        kpi = QFrame()
        kpi.setObjectName("kpiBar")
        kh = QHBoxLayout(kpi)
        kh.addWidget(QLabel("今日重點｜現役馬匹總數", objectName="kpiText"))
        kh.addWidget(QLabel(self._active_horses_total_text(), objectName="kpiNum"))
        kh.addWidget(QLabel("　往績", objectName="kpiText"))
        kh.addWidget(QLabel("91,203", objectName="kpiNum"))
        kh.addStretch()
        kh.addWidget(
            QLabel(
                f"最新賽日 {self._race_meeting_date}｜{self._racecourse_display(self._race_meeting_course)}｜場次 {self._race_count}",
                objectName="kpiText",
            )
        )
        main.addWidget(kpi)

        row2 = QHBoxLayout()
        left = QFrame()
        left.setObjectName("denseBox")
        gl = QVBoxLayout(left)
        gl.addWidget(QLabel("快速指標", objectName="secTitle"))
        for line in [
            "• 七日內新增馬匹：128",
            "• 往績筆數週增：+2.1%",
            "• 資料庫大小：約 48 MB（示意）",
        ]:
            gl.addWidget(QLabel(line, objectName="cell"))
        right = QFrame()
        right.setObjectName("denseBox")
        gr = QVBoxLayout(right)
        gr.addWidget(QLabel("待辦／提醒（示意）", objectName="secTitle"))
        gr.addWidget(QLabel("□ 同步最新排位\n□ 更新評分表\n□ 備份 SQLite", objectName="cell"))
        row2.addWidget(left, 1)
        row2.addWidget(right, 1)
        main.addLayout(row2)

        foot = QLabel("適合一屏多看：上方 KPI、下方再拆區塊或圖表。")
        foot.setStyleSheet("color: #666; font-size: 11px; margin-top: 8px;")
        main.addWidget(foot)
        main.addStretch()
        return self._wrap_scroll(w)

    # --- 樣式 ⑤ 玻璃 + 漸層 ---
    def _page_glass_gradient(self) -> QWidget:
        outer = QWidget()
        outer.setStyleSheet(
            """
            QWidget#gradRoot {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #667eea, stop:0.5 #764ba2, stop:1 #f093fb);
                border-radius: 0px;
            }
            QFrame#glassPanel {
                background: rgba(255,255,255,0.22);
                border: 1px solid rgba(255,255,255,0.35);
                border-radius: 14px;
                padding: 18px;
            }
            QLabel#gTitle { color: rgba(255,255,255,0.95); font-size: 18px; font-weight: 700; }
            QLabel#gStat { color: rgba(255,255,255,0.9); font-size: 14px; }
            QLabel#gBig { color: white; font-size: 32px; font-weight: 800; }
            """
        )
        outer.setObjectName("gradRoot")
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(24, 24, 24, 24)

        title = QLabel("HPPV 儀表板")
        title.setObjectName("gTitle")
        lay.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(14)
        stats = [
            ("現役馬匹總數", self._active_horses_total_text()),
            ("往績", "91k+"),
            ("賽日/場地", self._meeting_text()),
            ("場次", str(self._race_count)),
        ]
        for i, (a, b) in enumerate(stats):
            p = QFrame()
            p.setObjectName("glassPanel")
            vb = QVBoxLayout(p)
            vb.addWidget(QLabel(a, objectName="gStat"))
            vb.addWidget(QLabel(b, objectName="gBig"))
            grid.addWidget(p, i // 2, i % 2)
        lay.addLayout(grid)

        hint = QLabel("玻璃擬態＋漸層底，適合做「亮色系」品牌展示。")
        hint.setStyleSheet("color: rgba(255,255,255,0.75); font-size: 12px; margin-top: 8px;")
        lay.addWidget(hint)
        lay.addStretch()
        return self._wrap_scroll(outer)


def main() -> int:
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft JhengHei", 10))
    w = DashboardWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

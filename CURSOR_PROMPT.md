## 給 Cursor 的完整 Prompt（可直接貼上）

你是 Cursor 內的 AI coding agent。請在目前工作區（專案根目錄 `Hppv`）建立並維護一個可執行的 Python 專案，用於抓取香港賽馬馬匹往績並以桌面 GUI 顯示。

### 專案背景與目標

- **中文名稱**：賽馬往績資料庫系統
- **英文簡稱**：HPPV（Horse Past Performance Viewer）

### 主要功能（必做）

- 輸入香港馬會 `HorseId`
- 從 HKJC 馬匹資料頁抓取該馬匹的歷史賽績（只做「瀏覽」，不做分析）
- 將賽績存入 SQLite（Python 標準庫 `sqlite3`）
- 使用桌面 GUI（PySide6）顯示指定馬匹的所有賽績記錄（表格）

### 技術與實作要求（必遵守）

- 語言：Python 3.11+
- GUI：PySide6
- DB：SQLite（只用 `sqlite3`）
- 爬蟲：`requests` + `beautifulsoup4`
- 嚴格使用 type hints，關鍵函式寫簡短 docstring
- DB 連線使用 context manager（`with`）

### 輸出（完成標準）

- `pip install -r requirements.txt` 後可 `python -m hppv` 啟動 GUI
- 能輸入 `HorseId`，抓取後寫入 SQLite，並可從 DB 載入顯示
- 重要模組都有 type hints；DB 使用 `with`；關鍵函式有簡短 docstring


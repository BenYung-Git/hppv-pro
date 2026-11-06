
## 賽馬往績資料庫系統（HPPV）

用來從香港馬會（HKJC）馬匹頁抓取指定馬匹的歷史賽績，存入 SQLite，並用桌面 GUI 顯示全部記錄（只瀏覽、不分析）。

### 快速開始（Windows / PowerShell）

建立虛擬環境並安裝依賴：

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

如果你的 PowerShell 禁止載入 `Activate.ps1`（執行原則限制），可以改用不需要啟動虛擬環境的方式：

```bash
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

啟動 GUI：

```bash
python -m hppv
```

若未成功啟用虛擬環境，也可以直接用 venv 內的 Python：

```bash
.\.venv\Scripts\python.exe -m hppv
```

啟動只讀 Web 版（不抓資料、不排程、不寫入 DB）：

```bash
python -m hppv.web_app
```

或（使用 uvicorn 啟動）：

```bash
uvicorn hppv.web_app:app --port 800
```

資料庫會建立在：

- `data/hppv.sqlite3`

### 執行測試（可選）

```bash
pip install pytest
pytest -q
```

### 內部測試版打包（Windows，免安裝 Python）

若要給不會 Python 的內部同事測試，請在專案根目錄執行：

```bash
scripts\build_windows.bat
```

打包完成後，將整個 `dist/HPPV` 資料夾交給同事；對方只需雙擊 `HPPV.exe`。

補充：

- 打包設定檔：`HPPV.spec`
- 測試同事指引：`README_測試版.txt`
- 打包後資料庫預設放在執行檔同層的 `data/hppv.sqlite3`

### 注意

- HKJC 網頁結構可能變動；`hppv/scraper.py` 使用「表格欄位/標題關鍵字」的啟發式解析。
- 若抓取結果欄位不正確，可優先調整 `parse_past_performance_from_html()` 中的欄位對應啟發式。
- 往績頁網址需使用 **`HK_` 開頭的 HorseId**；若 `horses` 表內仍以烙號（如 `D241`）當主鍵，程式會依資料庫與 HKJC 烙號對照自動換成正確 ID 再下載。建議仍定期按「更新馬匹資料」讓主鍵統一為 `HK_…`。

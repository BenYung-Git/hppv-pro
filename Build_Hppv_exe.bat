@echo off
REM ===== HPPV 一鍵打包成 exe =====
setlocal

REM 切到本批次檔所在的資料夾（確保在 C:\Hppv）
cd /d "%~dp0"

echo [1/5] 啟動虛擬環境...
IF NOT EXIST ".venv\Scripts\python.exe" (
    echo 找不到 .venv\Scripts\python.exe，請先在此資料夾建立虛擬環境：
    echo   python -m venv .venv
    echo 然後再執行本批次檔。
    pause
    goto :EOF
)

call ".venv\Scripts\activate.bat"

echo [2/5] 確認已安裝 PyInstaller...
python -m pip show pyinstaller >NUL 2>&1
IF ERRORLEVEL 1 (
    echo 尚未安裝 PyInstaller，現在安裝中...
    python -m pip install pyinstaller
)

echo [3/5] 清除舊的 build/dist...
IF EXIST "build" rd /s /q build
IF EXIST "dist" rd /s /q dist
IF EXIST "__pycache__" rd /s /q __pycache__

echo [4/5] 開始用 PyInstaller 打包（資料夾版）...
pyinstaller ^
  --name hppv ^
  --windowed ^
  --noconfirm ^
  --clean ^
  --add-data "hppv;hppv" ^
  -m hppv

IF ERRORLEVEL 1 (
    echo 打包失敗，請往上查看錯誤訊息。
    pause
    goto :EOF
)

echo [5/5] 完成！輸出路徑：
echo   %CD%\dist\hppv\hppv.exe
echo.
echo 請到 dist\hppv 資料夾裡測試執行 hppv.exe。
pause

endlocal

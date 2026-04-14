@echo off
setlocal

REM Build HPPV test package for Windows.

cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing .venv\Scripts\python.exe
  echo Create venv first:
  echo   py -m venv .venv
  echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

echo [1/5] Upgrade pip...
.\.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 exit /b 1

echo [2/5] Install dependencies (including PyInstaller)...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

echo [3/5] Clean previous build outputs...
if exist "build" rmdir /s /q "build"
if exist "dist\HPPV" rmdir /s /q "dist\HPPV"

echo [4/5] Build executable...
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean "HPPV.spec"
if errorlevel 1 exit /b 1

echo [5/5] Build finished
echo.
echo Output folder:
echo   dist\HPPV
echo.
echo [6/6] Create zip package...
rem Use PowerShell to generate timestamp safely across locales.
powershell -NoProfile -Command ^
  "$src='dist\HPPV';" ^
  "if (!(Test-Path $src)) { Write-Host '[WARN] dist\HPPV not found.'; exit 1 };" ^
  "$zip='HPPV_test_' + (Get-Date -Format 'yyyyMMdd_HHmm') + '.zip';" ^
  "$dst=Join-Path 'dist' $zip;" ^
  "Compress-Archive -Path (Join-Path $src '*') -DestinationPath $dst -Force;" ^
  "Write-Host 'Zip file:';" ^
  "Write-Host ('  ' + $dst);"
if errorlevel 1 (
  echo [WARN] Zip creation failed. You can still share dist\HPPV folder directly.
) else (
  rem Output is printed by PowerShell.
)
echo.
echo For testers:
echo   Step 1 - Zip and share the whole dist\HPPV folder
echo   Step 2 - Unzip and run HPPV.exe
echo.
endlocal

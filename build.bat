@echo off
chcp 65001 >nul
echo ================================================
echo   Build ARAM-collector.exe (maintainer tool)
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python！
    pause
    exit /b 1
)

echo [1/3] 安裝 pyinstaller 和依賴...
pip install pyinstaller -q
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [錯誤] 安裝失敗
    pause
    exit /b 1
)

echo [2/3] 打包 exe...
pyinstaller ARAM-collector.spec --noconfirm
if errorlevel 1 (
    echo [錯誤] 打包失敗
    pause
    exit /b 1
)

echo [3/3] 完成！
echo.
echo   輸出: dist\ARAM-collector.exe
echo.
echo   測試: dist\ARAM-collector.exe --help
echo   收集: dist\ARAM-collector.exe run --platform TW2
echo.
pause

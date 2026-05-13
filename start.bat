@echo off
chcp 65001 >nul
echo ================================================
echo   ARAM Mayhem 資料收集器
echo ================================================
echo.

:: 確認 Python 存在
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python！
    echo 請先安裝 Python 3.10 以上版本：https://www.python.org/downloads/
    echo 安裝時記得勾選 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/3] 安裝依賴套件...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [錯誤] 安裝失敗，請確認網路連線。
    pause
    exit /b 1
)

echo [2/3] 開始收集資料（請確認 League Client 已登入）...
echo.
python collect.py --platform TW2
if errorlevel 1 (
    echo.
    echo [錯誤] 收集失敗。請確認 League Client 有開著並已登入。
    pause
    exit /b 1
)

echo.
echo [3/3] 完成！
echo.
echo 產出檔案：my_games.parquet
echo.
echo 請到以下連結上傳資料（把 my_games.parquet 拖進留言框）：
echo https://github.com/Lanternko/ARAM-mayhem-collector/discussions
echo.
pause

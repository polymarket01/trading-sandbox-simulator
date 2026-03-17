@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   极简买一卖一机器人 (Top-of-Book Bot)
echo ========================================
echo.
echo 请确保已先运行 run.bat 启动沙盒！
echo 按 Ctrl+C 可停止机器人
echo.
where python >nul 2>&1
if %errorlevel% equ 0 (
    python top_of_book_bot.py
) else (
    py top_of_book_bot.py
)
pause

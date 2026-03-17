@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   操盘手训练流动性模拟器
echo ========================================
echo.
echo 首次运行会自动下载依赖，请耐心等待...
echo 启动成功后，浏览器打开 http://localhost:5173
echo 按 Ctrl+C 可停止
echo.
where python >nul 2>&1
if %errorlevel% equ 0 (
    python run_sandbox.py
) else (
    where py >nul 2>&1
    if %errorlevel% equ 0 (
        py run_sandbox.py
    ) else (
        echo [错误] 未找到 Python，请先安装 Python 3.10+
        echo 下载地址: https://www.python.org/downloads/
        echo 安装时务必勾选 "Add Python to PATH"
        pause
        exit /b 1
    )
)
pause

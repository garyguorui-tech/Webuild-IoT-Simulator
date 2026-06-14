@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Niagara 风格现场设备模拟器 + 实时监控面板  一键启动
echo ============================================================

rem ---- 选择 Python 解释器 --------------------------------------------------
set "PY=python"
where python >nul 2>&1
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  ) else (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
  )
)

rem ---- 首次运行：创建虚拟环境 ---------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境 .venv ...
  "%PY%" -m venv .venv
  if errorlevel 1 ( echo [错误] 创建虚拟环境失败 & pause & exit /b 1 )
)

rem ---- 安装/更新依赖 ------------------------------------------------------
echo [2/3] 安装依赖（首次较慢，之后秒开）...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 ( echo [错误] 依赖安装失败 & pause & exit /b 1 )

rem ---- 启动设备群总览（自动打开浏览器） -----------------------------------
echo [3/3] 启动设备群监控总览 http://127.0.0.1:8000 （Ctrl-C 退出）
".venv\Scripts\python.exe" run_fleet.py --web-port 8000 --open

pause

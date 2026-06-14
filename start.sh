#!/usr/bin/env bash
# 一键启动（Git Bash / Linux / macOS）：建环境 -> 装依赖 -> 起模拟器+面板 -> 开浏览器
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  Niagara 风格现场设备模拟器 + 实时监控面板  一键启动"
echo "============================================================"

# 选择 Python
PY="python"
command -v python >/dev/null 2>&1 || PY="python3"

# Windows 上的 venv 可执行目录是 Scripts，类 Unix 是 bin
if [ -f ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
elif [ -f ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
else
  echo "[1/3] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
  if [ -f ".venv/Scripts/python.exe" ]; then VENV_PY=".venv/Scripts/python.exe"; else VENV_PY=".venv/bin/python"; fi
fi

echo "[2/3] 安装依赖（首次较慢）..."
"$VENV_PY" -m pip install -q --disable-pip-version-check -r requirements.txt

echo "[3/3] 启动设备群监控总览 http://127.0.0.1:8000 （Ctrl-C 退出）"
"$VENV_PY" run_fleet.py --web-port 8000 --open

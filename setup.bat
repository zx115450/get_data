@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === ACM 出数据工具 初始化 ===
echo.
where python >nul 2>&1
if errorlevel 1 (
  echo 未找到 python，请先安装 Python 3.10+ 并勾选 Add to PATH
  pause
  exit /b 1
)
python setup.py
echo.
pause

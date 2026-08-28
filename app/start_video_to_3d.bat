@echo off
setlocal
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo 未找到 Node.js 22 或更高版本，请先安装 Node.js。
  pause
  exit /b 1
)
node server.mjs
pause


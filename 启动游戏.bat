@echo off
chcp 65001 >nul
title Misknow
cd /d "%~dp0"

rem 页面文件名：打包版 index.html，开发根目录 ourgame7.html
set "PAGE=%~dp0index.html"
if not exist "%PAGE%" set "PAGE=%~dp0ourgame7.html"

rem 找 Python（跳过 Microsoft Store 的占位 python.exe，否则会弹出应用商店）
set "PY="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY goto nopython
echo "%PY%" | find /i "WindowsApps" >nul && goto nopython

echo 正在启动《Misknow》，浏览器会自动打开...
"%PY%" server.py
if errorlevel 1 goto fallback
echo.
echo 服务已停止。按任意键关闭窗口...
pause >nul
exit /b

:nopython
echo.
echo  [!] 这台电脑没有安装 Python —— 不影响游玩。
echo      正在直接用浏览器打开游戏（内置对话，玩法与交易完整）。
echo      只有「云端 API」和「本地 Ollama」才需要 Python。
echo.
start "" "%PAGE%"
echo.
echo 游戏已在浏览器打开。按任意键关闭这个窗口。
pause >nul
exit /b

:fallback
echo.
echo  [!] 本地服务没能启动，改用浏览器直接打开游戏（内置对话）。
start "" "%PAGE%"
echo.
echo 按任意键关闭这个窗口。
pause >nul
exit /b

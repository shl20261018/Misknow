@echo off
chcp 65001 >nul
title 一百枚金币 · 本地AI
cd /d "%~dp0"
echo 正在启动《一百枚金币》，浏览器会自动打开...
python server.py
echo.
echo 服务已停止。按任意键关闭窗口...
pause >nul

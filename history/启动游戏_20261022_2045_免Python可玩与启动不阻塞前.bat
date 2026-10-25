@echo off
chcp 65001 >nul
title Misknow · 本地AI
cd /d "%~dp0"
echo 正在启动《Misknow》，浏览器会自动打开...
python server.py
echo.
echo 服务已停止。按任意键关闭窗口...
pause >nul

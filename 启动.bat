@echo off
REM 双击启动 UIA XPath 测试工具（bat 绕开 IDE Run Code 插件的多行拼接 bug）
chcp 65001 >nul
cd /d "%~dp0"
start "" "D:\Python\Python314\pythonw.exe" -X utf8 app.py

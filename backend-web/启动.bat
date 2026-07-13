@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Backend-Web] 先停止已有进程...
call "%~dp0停止.bat"
echo [Backend-Web] 启动服务...
python main.py

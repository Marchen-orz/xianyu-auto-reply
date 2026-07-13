@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [WebSocket] 先停止已有进程...
call "%~dp0停止.bat"
echo [WebSocket] 启动服务...
python main.py

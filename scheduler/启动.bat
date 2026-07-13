@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Scheduler] 先停止已有进程...
call "%~dp0停止.bat"
echo [Scheduler] 启动服务...
python main.py

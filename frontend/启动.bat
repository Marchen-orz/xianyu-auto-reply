@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Frontend] 先停止已有进程...
call "%~dp0停止.bat"
echo [Frontend] 启动服务...
npm run dev

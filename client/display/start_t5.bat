@echo off
chcp 65001 >nul
rem T5 取件屏桥接一键启动（密钥从环境变量读取，不写进文件）
set "PY=python"
set "DEVICE=printer-001"
set "PORT=COM4"
set "BACKEND=https://silnk.cn"

if "%PRINTER_KEY%"=="" (
  echo [缺少密钥] 请先执行:  set PRINTER_KEY=你的设备密钥
  pause
  exit /b 1
)

echo 启动桥接: %DEVICE% %PORT% -> %BACKEND%
"%PY%" -u t5ai_poll.py --device %DEVICE% --port %PORT% --backend %BACKEND% --key "%PRINTER_KEY%"
pause

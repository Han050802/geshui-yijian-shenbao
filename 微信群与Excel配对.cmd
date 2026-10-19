@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "runtime-wechat\WeChatReceipt.exe" (
    echo 未找到微信运行组件，请完整解压 Windows 运行包。
    pause
    exit /b 1
)
start "" "runtime-wechat\WeChatReceipt.exe"

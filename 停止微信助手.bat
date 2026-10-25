@echo off
rem Stop WeChatVision WebUI (only processes running run.py; WeChat untouched).
wmic process where "name='python.exe' and commandline like '%run.py%'" call terminate >nul 2>&1
echo WeChatVision WebUI stopped.
timeout /t 2 >nul

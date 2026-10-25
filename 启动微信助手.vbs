' WeChatVision silent launcher -- double-click me, no black console window.
' WebUI log: logs\webui_*.log (one file per launch; run.py auto-cleans files
' older than 3 days). Browser pops up automatically when the page is ready.
' Already running? Double-click again: no second copy, just reopen the page.
' Stop: double-click the "stop WeChatVision" batch file in the same folder.
' NOTE: keep this file ASCII-only. WSH parses .vbs as ANSI when there is no
' BOM; UTF-8 Chinese comments silently swallow the code line after them
' (2026-10-25 incident: launcher looked alive but python never started).
Dim shell, fso, root, logDir
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = root & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
shell.CurrentDirectory = root
' 0 = hidden window; False = do not wait (run.py opens the browser itself).
' Log name uses %RANDOM%: a fixed shared log file is held open by the running
' service, and a second double-click would fail to open it for writing, so
' python would never even start (2026-10-25 fix).
shell.Run "cmd /c chcp 65001 >nul & python run.py > logs\webui_%RANDOM%.log 2>&1", 0, False

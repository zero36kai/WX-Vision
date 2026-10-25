@echo off
rem Silent launcher: delegates to the VBS launcher in this folder (zero black
rem window). run.py has a built-in single-instance guard: repeated double-
rem clicks will NOT start a second copy -- they just reopen the console page.
rem For a visible debug console run: python run.py
for %%f in ("%~dp0*.vbs") do wscript.exe "%%f"
exit /b 0

@echo off
REM PeekaBaby — debug-starter: draait de app met console + devtools + logging,
REM zonder de exe te herbouwen. Handig om streamingproblemen te onderzoeken.
cd /d "%~dp0"
set PEEKABABY_DEBUG=1
".venv\Scripts\python.exe" peekababy.py
echo.
echo (venster gesloten) — logs staan in peekababy.log / go2rtc.log / bridge.log
pause

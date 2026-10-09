@echo off
rem Night City Haptics: starts the companion and opens its control page.
rem Needs Python 3.9+ from python.org or the Microsoft Store. Nothing else.
where py >nul 2>nul && (start "" /min py -3 "%~dp0nch.py" %* & exit /b 0)
where python >nul 2>nul && (start "" /min python "%~dp0nch.py" %* & exit /b 0)
echo Python 3 is not installed. Get it from https://www.python.org/downloads/ and run this again.
pause

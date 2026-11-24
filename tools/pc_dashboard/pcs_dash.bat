@echo off
rem Double-click: start the PCS dashboard (finds the CH340, opens the browser). Close this window to stop it.
rem Add --demo for fake data, or a port: pcs_dash.bat COM4
py "%~dp0pcs_dash.py" %*
if errorlevel 1 pause

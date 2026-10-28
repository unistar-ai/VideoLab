@echo off
setlocal
cd /d "%~dp0"

if "%VIDEOLAB_COMFY_HOME%"=="" set "VIDEOLAB_COMFY_HOME=%~dp0..\ComfyUI_windows_portable"

set "PY=%VIDEOLAB_COMFY_HOME%\python_embeded\pythonw.exe"
if not exist "%PY%" set "PY=%VIDEOLAB_COMFY_HOME%\python_embeded\python.exe"
if not exist "%PY%" set "PY=python"

start "" "%PY%" "%~dp0app.py" --comfy-home "%VIDEOLAB_COMFY_HOME%"
exit /b 0

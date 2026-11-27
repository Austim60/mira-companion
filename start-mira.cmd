@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "mira_conda=D:\miniconda3\condabin\conda.bat"
if not exist "%mira_conda%" set "mira_conda=conda.bat"
call "%mira_conda%" activate mira
if errorlevel 1 goto failed
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python scripts\start_admin.py
if errorlevel 1 goto failed
popd
exit /b 0

:failed
echo Mira could not start. See the error above.
pause
popd
exit /b 1

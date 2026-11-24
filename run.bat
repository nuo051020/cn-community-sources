@echo off
REM 独立包入口（Windows）。
REM 运行时：系统 Python 3.11+。
REM 可选依赖：pip install requests aiotieba
REM 用法：run.bat source-adapter <命令>   或   run.bat <命令>
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%adapter;%PYTHONPATH%"
set "ANYSEARCH_SKILL_DIR=%ROOT%skills\anysearch"

if "%1"=="source-adapter" shift

where python3 >nul 2>nul
if %errorlevel%==0 ( set "PY=python3" ) else ( set "PY=python" )

"%PY%" -m source_adapter %1 %2 %3 %4 %5 %6 %7 %8 %9
endlocal

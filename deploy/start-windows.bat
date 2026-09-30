@echo off
REM EbeyParser for Windows: double-click to run 24/7 (restarts itself after a crash).
REM Autostart: see README, section "Работа 24/7" (Task Scheduler) or put a shortcut into shell:startup.
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title EbeyParser

echo ============================================================
echo  EbeyParser. Программа сама отключает в этом окне "выделение
echo  мышью" (QuickEdit), из-за которого Windows замораживает
echo  программу после клика. Если окно всё же "замерло" - нажми Esc.
echo ============================================================

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.11+ с https://www.python.org/downloads/
    echo и при установке отметь галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

REM Dependencies are (re)installed when pyproject.toml changed since the last install: a new
REM version may need new packages (e.g. Pillow). The stamp stores a hash of pyproject.toml,
REM because files unpacked from a ZIP keep old modification times.
if not exist .venv\Scripts\python.exe goto fresh
.venv\Scripts\python.exe -c "import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest(); s=pathlib.Path('.venv/.deps-stamp'); sys.exit(0 if s.is_file() and s.read_text().strip()==h else 1)" >nul 2>nul
if errorlevel 1 goto install
.venv\Scripts\python.exe -m ebeyparser --help >nul 2>nul
if errorlevel 1 goto repair
goto ready

:repair
echo Окружение .venv повреждено - пересоздаю...
rmdir /s /q .venv
:fresh
echo Создаю окружение .venv...
python -m venv .venv
if errorlevel 1 goto fail
:install
echo Устанавливаю зависимости - первый запуск или новая версия, это займёт пару минут...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -e .
if errorlevel 1 goto fail
.venv\Scripts\python.exe -c "import hashlib,pathlib; pathlib.Path('.venv/.deps-stamp').write_text(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest())"
.venv\Scripts\python.exe -m ebeyparser --help >nul 2>nul
if errorlevel 1 goto fail
goto ready

:fail
echo Не удалось установить зависимости. Проверь интернет и запусти ещё раз.
pause
exit /b 1

:ready
rem Без config.yaml программа создаст его сама, а настройка пройдёт в браузере.

:loop
.venv\Scripts\python.exe -m ebeyparser run
if errorlevel 3 if not errorlevel 4 (
    echo EbeyParser уже запущен в другом окне или в Планировщике - второй экземпляр не нужен.
    pause
    exit /b 3
)
echo EbeyParser остановился, перезапуск через 30 секунд... Закрой окно, чтобы выйти.
timeout /t 30 /nobreak >nul
goto loop

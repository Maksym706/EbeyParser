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
echo  EbeyParser. НЕ кликай мышкой внутрь этого окна: в Windows
echo  клик включает "выделение" (QuickEdit), и программа замирает,
echo  пока не нажмёшь Enter или Esc. Отключить навсегда: правый
echo  клик по заголовку окна - Свойства - снять "Выделение мышью".
echo ============================================================

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.11+ с https://www.python.org/downloads/
    echo и при установке отметь галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

REM Repair a broken virtual environment (moved folder, updated Python, interrupted install).
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m ebeyparser --help >nul 2>nul
    if errorlevel 1 (
        echo Окружение .venv повреждено - переустанавливаю...
        rmdir /s /q .venv
    )
)
if not exist .venv\Scripts\python.exe (
    echo Устанавливаю зависимости, это займёт пару минут...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip >nul
    .venv\Scripts\python.exe -m pip install -e .
    if errorlevel 1 (
        echo Не удалось установить зависимости. Проверь интернет и запусти ещё раз.
        pause
        exit /b 1
    )
)

if not exist config.yaml (
    .venv\Scripts\python.exe -m ebeyparser setup
    if not exist config.yaml .venv\Scripts\python.exe -m ebeyparser init
)

:loop
.venv\Scripts\python.exe -m ebeyparser run
if errorlevel 3 if not errorlevel 4 (
    echo EbeyParser уже запущен в другом окне или в Планировщике - второй экземпляр не нужен.
    pause
    exit /b 3
)
echo EbeyParser остановился, перезапуск через 30 секунд... (закрой окно, чтобы выйти)
timeout /t 30 /nobreak >nul
goto loop

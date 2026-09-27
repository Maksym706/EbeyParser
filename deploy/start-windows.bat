@echo off
REM Запуск EbeyParser на Windows. Двойной клик — и работает, пока открыто окно.
REM Для автозапуска: Win+R -> shell:startup -> положи туда ярлык на этот файл.
cd /d "%~dp0\.."
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\pip install -e .
)
if not exist config.yaml .venv\Scripts\python -m ebeyparser init
:loop
.venv\Scripts\python -m ebeyparser run
echo EbeyParser остановился, перезапуск через 30 секунд...
timeout /t 30 /nobreak >nul
goto loop

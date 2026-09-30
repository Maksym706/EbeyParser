#!/usr/bin/env bash
# EbeyParser on a Linux home server (Ubuntu / Debian / Raspberry Pi OS), 24/7.
#
#   bash deploy/install-linux.sh              install or repair: Python venv, packages, systemd service
#   bash deploy/install-linux.sh --with-ai    + a small local AI: Ollama and the models for this server
#   bash deploy/install-linux.sh --dry-run    only show what would be done
#
# Run it as your normal user (it asks for the sudo password for the service). Running it again is
# safe: it only changes what is missing or outdated. Settings are made later in the web panel.
# Other options: --no-service (only the venv), --lan (open a panel that is still "this computer
# only" to the home network), --yes (don't ask), --python PATH.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ebeyparser
UNIT_PATH=/etc/systemd/system/$SERVICE.service
OLLAMA_URL=http://127.0.0.1:11434
DRY=0 WITH_AI=0 SERVICE_ON=1 LAN=0 YES=0 PY=""

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf '✖ %s\n' "$*" >&2; exit 1; }
run() {  # run a command, or only print it with --dry-run
    if [ "$DRY" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}
ask() {  # ask "question" -> 0 = yes
    [ "$YES" = 1 ] && return 0
    [ "$DRY" = 1 ] && { say "[dry-run] спросил бы: $1 [д/Н]"; return 0; }
    local answer
    read -r -p "$1 [д/Н] " answer || return 1
    case "${answer,,}" in д|да|y|yes|j|ja) return 0 ;; *) return 1 ;; esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --with-ai) WITH_AI=1 ;;
        --no-service) SERVICE_ON=0 ;;
        --lan) LAN=1 ;;
        --yes|-y) YES=1 ;;
        --python) PY="${2:-}"; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) die "Неизвестный параметр: $1 (см. --help)" ;;
    esac
    shift
done

if [ "$(id -u)" = 0 ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        say "Запущено через sudo — продолжаю от пользователя $SUDO_USER (sudo спросит пароль, когда нужно)."
        exec sudo -u "$SUDO_USER" -H bash "$0" "$@"
    fi
    die "Запусти от обычного пользователя, не от root: bash deploy/install-linux.sh (программа не должна работать от root)."
fi
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"
cd "$DIR"
[ "$DRY" = 1 ] && say "Режим --dry-run: ничего не меняю, только показываю команды."

# ------------------------------------------------------------------ 1. Python 3.11+
step "1/4 Python 3.11 или новее"
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
if [ -z "$PY" ]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then PY="$(command -v "$candidate")"; break; fi
    done
fi
if [ -z "$PY" ] || ! python_ok "$PY"; then
    say "✖ Нужен Python 3.11 или новее, а нашёлся: $(python3 --version 2>&1 || echo 'никакой')."
    say "  Debian 12 / Raspberry Pi OS:  sudo apt install python3 python3-venv"
    say "  Ubuntu 22.04:                 sudo apt install python3.11 python3.11-venv"
    exit 1
fi
say "✔ $("$PY" --version) ($PY)"
if ! "$PY" -c 'import ensurepip, venv' 2>/dev/null; then
    ver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    say "✖ Не хватает модуля venv. Установи: sudo apt install python${ver}-venv   и запусти скрипт ещё раз."
    exit 1
fi

# ------------------------------------------------------------------ 2. venv + packages
step "2/4 Окружение .venv и пакеты"
VPY="$DIR/.venv/bin/python"
if [ -x "$VPY" ] && ! "$VPY" -c 'import sys' 2>/dev/null; then
    say "Окружение .venv повреждено — создаю заново."
    run rm -rf "$DIR/.venv"
fi
if [ ! -x "$VPY" ] || [ "$DRY" = 1 ] && [ ! -x "$VPY" ]; then
    run "$PY" -m venv "$DIR/.venv"
fi
run "$VPY" -m pip install --quiet --upgrade pip
run "$VPY" -m pip install --quiet -e "$DIR"
if [ "$DRY" = 0 ]; then
    "$VPY" -m ebeyparser --help >/dev/null || die "Программа не запускается после установки — пришли вывод выше."
    say "✔ Пакеты установлены"
fi

# ------------------------------------------------------------------ 3. systemd service
step "3/4 Автозапуск (systemd)"
if [ "$SERVICE_ON" = 0 ]; then
    say "Пропускаю (--no-service). Запуск вручную: $VPY -m ebeyparser run --server"
elif ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
    say "⚠ systemd не найден — автозапуск не настроен. Запуск вручную: $VPY -m ebeyparser run --server"
    SERVICE_ON=0
else
    unit="$(sed -e "s#@USER@#$RUN_USER#g" -e "s#@GROUP@#$RUN_GROUP#g" -e "s#@DIR@#$DIR#g" "$DIR/deploy/ebeyparser.service")"
    if [ -f "$UNIT_PATH" ] && [ "$(cat "$UNIT_PATH")" = "$unit" ]; then
        say "✔ Сервис $SERVICE уже настроен"
    else
        say "Записываю $UNIT_PATH (нужен пароль sudo)"
        if [ "$DRY" = 1 ]; then
            say "[dry-run] sudo tee $UNIT_PATH <<сервис для $RUN_USER в $DIR>>"
        else
            printf '%s\n' "$unit" | sudo tee "$UNIT_PATH" >/dev/null
        fi
        run sudo systemctl daemon-reload
    fi
    run sudo systemctl enable --quiet "$SERVICE"
    if [ "$LAN" = 1 ]; then
        run "$VPY" -m ebeyparser access lan --no-qr
    fi
    if systemctl is-active --quiet "$SERVICE"; then
        run sudo systemctl restart "$SERVICE"
    else
        run sudo systemctl start "$SERVICE"
    fi
    say "✔ EbeyParser работает и сам запустится после перезагрузки. Лог: journalctl -u $SERVICE -f"
fi

# ------------------------------------------------------------------ 4. local AI (opt-in)
step "4/4 Нейросеть на этом сервере"
if [ "$WITH_AI" = 0 ]; then
    say "Пропускаю. Маленькая нейросеть-разведчик на процессоре этого сервера: bash deploy/install-linux.sh --with-ai"
    say "(что она потянет: $VPY -m ebeyparser server-models). Модель для фото на игровом ПК"
    say "подключается в панели: Настройки → Нейросеть."
else
    if [ -x "$VPY" ]; then
        "$VPY" -m ebeyparser server-models || true
    fi
    say ""
    say "Что будет сделано:"
    if command -v ollama >/dev/null 2>&1; then
        say "  • Ollama уже установлена ($(ollama --version 2>/dev/null | tail -1))"
    else
        say "  • установлю Ollama официальным скриптом: curl -fsSL https://ollama.com/install.sh | sh"
        say "    (сервис ollama, слушает только 127.0.0.1:11434; занимает ~1–4 ГБ на диске)"
    fi
    say "  • настройки Ollama для слабого сервера: модель остаётся в памяти, не больше 2 моделей, 1 запрос за раз,"
    say "    низкий приоритет процессора (/etc/systemd/system/ollama.service.d/ebeyparser.conf)"
    say "  • скачаю модели из списка выше (~2–4 ГБ) и включу разведчика в программе"
    if ask "Продолжить?"; then
        if ! command -v ollama >/dev/null 2>&1; then
            command -v curl >/dev/null 2>&1 || die "Нужен curl: sudo apt install curl"
            if [ "$DRY" = 1 ]; then
                say "[dry-run] curl -fsSL https://ollama.com/install.sh | sh"
            else
                curl -fsSL https://ollama.com/install.sh | sh
            fi
        fi
        dropin="[Service]
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_KEEP_ALIVE=24h
Environment=OLLAMA_MAX_LOADED_MODELS=2
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_CONTEXT_LENGTH=8192
Nice=10"
        if [ "$DRY" = 1 ]; then
            say "[dry-run] sudo tee /etc/systemd/system/ollama.service.d/ebeyparser.conf"
            printf '%s\n' "$dropin" | sed 's/^/[dry-run]   /'
        elif [ "$(cat /etc/systemd/system/ollama.service.d/ebeyparser.conf 2>/dev/null)" != "$dropin" ]; then
            sudo mkdir -p /etc/systemd/system/ollama.service.d
            printf '%s\n' "$dropin" | sudo tee /etc/systemd/system/ollama.service.d/ebeyparser.conf >/dev/null
            sudo systemctl daemon-reload
            sudo systemctl restart ollama
        fi
        run "$VPY" -m ebeyparser server-models --pull --ollama "$OLLAMA_URL" --wait 60
        if [ "$SERVICE_ON" = 1 ]; then
            run sudo systemctl restart "$SERVICE"
            say "✔ Разведчик включится сам (Настройки → Нейросеть покажет модель и скорость)."
        else
            say "Включи разведчика в панели: Настройки → Нейросеть → Разведчик, адрес $OLLAMA_URL"
        fi
    else
        say "Хорошо, без локальной нейросети."
    fi
fi

# ------------------------------------------------------------------ the link for the phone
if [ "$DRY" = 0 ] && [ "$SERVICE_ON" = 1 ]; then
    for _ in $(seq 1 30); do [ -f "$DIR/config.yaml" ] && break; sleep 1; done
    say ""
    "$VPY" -m ebeyparser access || true
    if systemctl is-active --quiet ufw 2>/dev/null; then
        say ""
        say "⚠ Включён файрвол ufw. Открой порт панели для домашней сети:"
        say "   sudo ufw allow from 192.168.0.0/16 to any port 8000 proto tcp"
    fi
fi
say ""
say "Готово. Обновление: bash deploy/update-linux.sh · ссылки для телефона: $VPY -m ebeyparser access"

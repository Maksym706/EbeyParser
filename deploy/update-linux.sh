#!/usr/bin/env bash
# Update EbeyParser on a Linux home server: a copy of the database, the new version (git pull),
# the packages it needs, restart. Settings, keys and the database stay as they are.
#
#   bash deploy/update-linux.sh             update
#   bash deploy/update-linux.sh --dry-run   only show what would be done
#
# Docker instead: git pull && docker compose up -d --build
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=ebeyparser
VPY="$DIR/.venv/bin/python"
KEEP_BACKUPS=3
DRY=0

say() { printf '%s\n' "$*"; }
die() { printf '✖ %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi; }

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) die "Неизвестный параметр: $arg (см. --help)" ;;
    esac
done
[ "$(id -u)" = 0 ] && die "Запусти от обычного пользователя, не от root: bash deploy/update-linux.sh"
[ -x "$VPY" ] || die "Программа ещё не установлена: bash deploy/install-linux.sh"
cd "$DIR"
[ "$DRY" = 1 ] && say "Режим --dry-run: ничего не меняю, только показываю команды."

# 1. a consistent copy of the database (works while the program runs), the last $KEEP_BACKUPS kept
say "== 1/4 Копия базы"
db="$("$VPY" -c 'from ebeyparser.config import load_config; print(load_config("config.yaml").db_path)' 2>/dev/null || echo data/ebeyparser.sqlite3)"
if [ -f "$db" ]; then
    backups="$(dirname "$db")/backups"
    target="$backups/ebeyparser-$(date +%Y%m%d-%H%M).sqlite3"
    run mkdir -p "$backups"
    run "$VPY" -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$db" "$target"
    say "✔ $target"
    if [ "$DRY" = 0 ]; then
        # shellcheck disable=SC2012  # file names are our own timestamps
        ls -1t "$backups"/ebeyparser-*.sqlite3 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
    fi
else
    say "Базы ещё нет — пропускаю."
fi

# 2. the new version
say "== 2/4 Новая версия"
if [ -d .git ]; then
    run git pull --ff-only || die "git pull не получился (изменения в файлах программы?). Проверь: git status"
else
    say "⚠ Это не git-копия. Скачай новый ZIP, распакуй поверх этой папки (config.yaml, .env и data/ не трогай)"
    say "  и запусти этот скрипт ещё раз."
fi

# 3. packages (new versions may need new ones)
say "== 3/4 Пакеты"
run "$VPY" -m pip install --quiet --upgrade pip
run "$VPY" -m pip install --quiet -e "$DIR"
[ "$DRY" = 1 ] || "$VPY" -m ebeyparser --help >/dev/null || die "Новая версия не запускается — пришли вывод выше."

# 4. restart
say "== 4/4 Перезапуск"
if command -v systemctl >/dev/null 2>&1 && systemctl cat "$SERVICE" >/dev/null 2>&1; then
    # the service file may have changed with the new version
    if [ -f "$DIR/deploy/install-linux.sh" ] && ! grep -q -- "run --server" "/etc/systemd/system/$SERVICE.service" 2>/dev/null; then
        say "Сервис устарел — обновляю его: bash deploy/install-linux.sh"
        run bash "$DIR/deploy/install-linux.sh" --yes
    else
        run sudo systemctl restart "$SERVICE"
    fi
    say "✔ Обновлено и перезапущено. Лог: journalctl -u $SERVICE -f"
else
    say "Сервиса $SERVICE нет — перезапусти программу: $VPY -m ebeyparser run --server"
fi

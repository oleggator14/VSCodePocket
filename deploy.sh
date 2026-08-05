#!/usr/bin/env bash
# Выкладка CodePocket в прод.
#
# Репозиторий и прод — разные каталоги: разработка идёт здесь, сервис работает
# из DEPLOY_DIR. Раньше правки делались прямо в рабочем каталоге systemd, то
# есть любое незакоммиченное изменение сразу оказывалось в проде.
#
#   ./deploy.sh              — выложить в /opt/codepocket и перезапустить
#   DEPLOY_DIR=/srv/cp ./deploy.sh   — другой каталог
#   ./deploy.sh --dry-run    — показать, что будет скопировано, и выйти
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/codepocket}"
SERVICE="${SERVICE:-codepocket}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запускать под root (нужен доступ к $DEPLOY_DIR и systemctl)"
[[ -f "$SRC/server.py" ]] || die "не найден server.py — запускайте из репозитория"

# 1. Прогоняем тесты: выкладывать заведомо сломанное незачем
say "→ тесты"
( cd "$SRC" && python3 -m unittest discover -s tests -q ) || die "тесты не прошли, выкладка отменена"

# 2. Синтаксис — отдельно, потому что сервис падает на нём уже в проде
say "→ проверка синтаксиса"
python3 -m py_compile "$SRC/server.py" "$SRC/agent.py" || die "код не компилируется"

# 3. Незакоммиченные правки — не блокируем, но говорим вслух
if command -v git >/dev/null && git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then
        printf '\033[33mВнимание:\033[0m в репозитории есть незакоммиченные правки\n'
    fi
    say "→ версия: $(git -C "$SRC" rev-parse --short HEAD) $(git -C "$SRC" log -1 --format=%s)"
fi

FILES=(server.py agent.py static)
if [[ $DRY_RUN -eq 1 ]]; then
    say "→ было бы скопировано в $DEPLOY_DIR:"
    printf '   %s\n' "${FILES[@]}"
    exit 0
fi

# 4. Копируем код. Данные (/var/lib/codepocket) и настройки
#    (/etc/codepocket.env) живут отдельно и не затрагиваются.
say "→ копирование в $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"
for f in "${FILES[@]}"; do
    cp -a "$SRC/$f" "$DEPLOY_DIR/"
done

# 5. Перезапуск и проверка, что сервис действительно поднялся
say "→ перезапуск $SERVICE"
systemctl restart "$SERVICE"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    say "✓ готово, сервис работает"
    systemctl status "$SERVICE" --no-pager --lines=0 | head -3
else
    printf '\033[31mСервис не поднялся.\033[0m Последние строки журнала:\n'
    journalctl -u "$SERVICE" -n 20 --no-pager
    exit 1
fi

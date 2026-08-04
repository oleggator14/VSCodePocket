# CodePocket

Мобильная веб-IDE: код пишется с телефона, а выполняется на вашем сервере.
Экраны — Главная (серверы и проекты), Файлы (редактор), Консоль (терминал по
SSH), ИИ (Claude и Codex), Профиль.

## Состав

| Файл | Что это |
|---|---|
| `server.py` | весь бэкенд: HTTP+WebSocket, SSH к серверам пользователей, чаты агентов |
| `agent.py` | агент через Anthropic API (запасной режим, когда своего сервера нет) |
| `static/index.html` | всё приложение целиком: разметка, стили, логика |
| `static/vendor/` | CodeMirror и xterm.js локально, без CDN |

## Развёртывание

```bash
cp -r server.py agent.py static /opt/codepocket/
systemctl restart codepocket
```

Настройки — в `/etc/codepocket.env` (в репозиторий не входит):
`IDE_DATA_DIR`, `IDE_DEV_INVITE`, `ANTHROPIC_API_KEY`, `TG_BOT_TOKEN` и прочее.
Данные пользователей — в `/var/lib/codepocket/` (тоже вне репозитория).

## Чего в репозитории нет и не должно быть

Ключей, паролей, `users.json`, `secret.key`, `.env`. Всё это лежит на сервере
и закрыто через `.gitignore`.

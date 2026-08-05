#!/usr/bin/env python3
"""
Телеграм-бот CodePocket: отвечает на /start и /help.

Отдельный маленький процесс рядом с приложением. Нужен потому, что мини-апп
сам по себе на команды не отвечает: пока бот молчит, человек пишет /start и
не получает ничего — ни объяснения, ни кнопки.

Только стандартная библиотека, как и весь проект. Long polling, без вебхуков:
не нужен ни отдельный домен, ни маршрут в Caddy.

Переменные окружения (общий файл /etc/codepocket.env):
  TG_BOT_TOKEN   токен бота (обязательно)
  CP_APP_URL     адрес мини-аппа для кнопки (по умолчанию берётся из BotFather)
  CP_BOT_ADMIN   Telegram ID, кому слать ошибки (необязательно)
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
APP_URL = os.environ.get("CP_APP_URL", "")
API = "https://api.telegram.org/bot%s/" % TOKEN

# ── тексты ──────────────────────────────────────────────────────────────────
# Главное сообщение. Человек читает его ДО того, как что-то увидел, поэтому
# сначала — что нужно от него (свой сервер), и только потом возможности.
# Без этого он открывает приложение, видит пустой экран и уходит.
START = """<b>LightCode</b> — редактор кода, терминал и ИИ-агенты для вашего сервера. Прямо в Telegram, с телефона.

<b>Что нужно, чтобы начать</b>
Своя машина с доступом по SSH — подойдёт любой VPS. Код лежит на ней, команды выполняются на ней, агенты правят файлы там же. Мы ничего не выполняем у себя и не забираем ваши файлы в чужое облако.

<b>Три шага</b>
1. Откройте приложение кнопкой меню
2. Подключите сервер. Нет SSH-ключа — приложение создаст его само и покажет одну команду, которую надо вставить в консоль сервера
3. Дальше редактор, терминал и агенты просто заработают

<b>Что внутри</b>
<b>Файлы</b> — редактор с подсветкой, поиском и историей версий: любую правку можно откатить.
<b>Консоль</b> — полноценный терминал сервера. Держит сессию в tmux: закрыли приложение — процессы продолжают работать.
<b>ИИ</b> — Claude и Codex. Пишете задачу, они читают код, правят его и запускают. Если на сервере уже стоит Claude Code, приложение подхватит те же беседы, что идут у вас в VS Code: начали за компьютером — продолжили с телефона.

<b>Про оплату</b>
Агенты работают на вашем сервере под вашей подпиской или по вашему ключу. Мы за них не платим и денег за это не берём.

/help — показать это снова"""

DESCRIPTION = (
    "Редактор кода, терминал и ИИ-агенты для вашего сервера — с телефона.\n\n"
    "Нужен свой VPS с доступом по SSH: код лежит и выполняется на нём. "
    "Claude и Codex правят файлы прямо там, под вашей подпиской.\n\n"
    "Нажмите «Запустить», чтобы начать."
)
SHORT_DESCRIPTION = ("Редактор, терминал и ИИ-агенты для вашего сервера. "
                     "С телефона, через SSH.")
COMMANDS = [
    {"command": "start", "description": "Что это и как начать"},
    {"command": "help", "description": "Показать инструкцию"},
]


def call(method, payload=None, timeout=65):
    """Вызов Bot API. Возвращает result или None."""
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(API + method, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.load(r)
        return j.get("result") if j.get("ok") else None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print("API %s: HTTP %s %s" % (method, e.code, body), flush=True)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("API %s: %s" % (method, e), flush=True)
    return None


def app_url():
    """Адрес мини-аппа: из переменной окружения либо из кнопки меню бота."""
    if APP_URL:
        return APP_URL
    mb = call("getChatMenuButton", {}) or {}
    return ((mb.get("web_app") or {}).get("url") or "").strip()


def reply(chat_id, text):
    url = app_url()
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": True}}
    if url:
        # web_app-кнопку можно слать только в приватный чат
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": "Открыть LightCode", "web_app": {"url": url}}]]}
    if call("sendMessage", payload) is None and "reply_markup" in payload:
        # не прошло с кнопкой (например, чат не приватный) — отправим без неё
        payload.pop("reply_markup")
        call("sendMessage", payload)


def setup_profile():
    """Описание и список команд. Описание — это то, что человек видит в пустом
    чате ещё до нажатия «Запустить», поэтому оно должно объяснять суть само."""
    call("setMyDescription", {"description": DESCRIPTION})
    call("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
    call("setMyCommands", {"commands": COMMANDS})
    print("профиль бота обновлён", flush=True)


def handle(update):
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat or not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/start", "/help"):
        reply(chat, START)


def main():
    if not TOKEN:
        raise SystemExit("не задан TG_BOT_TOKEN")
    # Сеть на старте может быть ещё не поднята, а у машины нет маршрута в IPv6 —
    # Telegram резолвится и в IPv6 тоже, и попытка иногда возвращает
    # «Network is unreachable». Раньше это роняло процесс на первой же неудаче,
    # и сервис уходил в бесконечный перезапуск. Просто ждём и пробуем снова.
    me, wait = None, 2
    while not me:
        me = call("getMe", {}, timeout=15)
        if me:
            break
        print("Telegram недоступен, повтор через %d с" % wait, flush=True)
        time.sleep(wait)
        wait = min(wait * 2, 60)
    print("бот @%s запущен" % me.get("username"), flush=True)
    setup_profile()

    offset = 0
    backoff = 1
    while True:
        # long polling: соединение висит до 50 с и возвращает всё, что накопилось
        upd = call("getUpdates", {"offset": offset, "timeout": 50,
                                  "allowed_updates": ["message"]})
        if upd is None:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)   # сеть моргнула — не долбим API
            continue
        backoff = 1
        for u in upd:
            offset = max(offset, u.get("update_id", 0) + 1)
            try:
                handle(u)
            except Exception as e:                  # один плохой апдейт не роняет бота
                print("ошибка обработки:", e, flush=True)


if __name__ == "__main__":
    main()

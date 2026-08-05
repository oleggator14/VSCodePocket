#!/usr/bin/env python3
"""
claude-ide — мини-IDE для команды: редактор + терминал + Claude, ноль установок у пользователей.

Один файл, только стандартная библиотека Python (никаких pip install).
Запускается под root (нужно для создания Linux-пользователей и запуска
терминалов от их имени). Наружу выставляется через Caddy/nginx по HTTPS.

Переменные окружения:
  IDE_PORT            порт (по умолчанию 9500)
  IDE_HOST            адрес (по умолчанию 127.0.0.1 — наружу только через прокси!)
  IDE_INVITE_CODE     инвайт-код для регистрации (обязательно)
  ANTHROPIC_API_KEY   общий ключ Claude API — попадает в окружение терминалов
  IDE_DATA_DIR        где хранить базу пользователей (по умолчанию /var/lib/claude-ide)
"""

import base64
import binascii
import fcntl
import hashlib
import hmac
import io
import json
import os
import pty
import re
import resource
import secrets
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
import ipaddress
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# ----------------------------------------------------------------------------
# конфигурация
# ----------------------------------------------------------------------------
PORT = int(os.environ.get("IDE_PORT", "9500"))
HOST = os.environ.get("IDE_HOST", "127.0.0.1")
INVITE_CODE = os.environ.get("IDE_INVITE_CODE", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATA_DIR = os.environ.get("IDE_DATA_DIR", "/var/lib/claude-ide")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

import agent as agent_mod

# роли и регистрация
DEV_INVITE = os.environ.get("IDE_DEV_INVITE", INVITE_CODE)      # инвайт разработчиков
OPEN_SIGNUP = os.environ.get("IDE_OPEN_SIGNUP", "0") == "1"     # открытая регистрация рядовых
# общий (локальный) терминал на нашем сервере. По умолчанию ВЫКЛ:
# каждый работает на своём сервере по SSH. Включить: CP_ALLOW_LOCAL_TERMINAL=1
ALLOW_LOCAL_TERMINAL = os.environ.get("CP_ALLOW_LOCAL_TERMINAL", "0") == "1"
# прокидывать ли общий ANTHROPIC_API_KEY в интерактивный терминал пользователя.
# По умолчанию ВЫКЛ: иначе любой вошедший может сделать `echo $ANTHROPIC_API_KEY`
# и забрать общий платный ключ. Включить осознанно: CP_TERMINAL_API_KEY=1
EXPOSE_KEY_IN_TERMINAL = os.environ.get("CP_TERMINAL_API_KEY", "0") == "1"
# Telegram Mini App: токен бота (для проверки подписи входа) и список разрешённых
# Telegram-ID (через запятую). Если список пуст — первый вошедший становится
# владельцем (dev), остальные не допускаются, пока их ID не добавят в список.
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
# кого пускать: если список НЕ пуст — только эти ID; если пуст — вход открыт всем.
TG_ALLOWED_IDS = set(x.strip() for x in
                     os.environ.get("TG_ALLOWED_IDS", "").split(",") if x.strip())
# кому давать роль разработчика (dev); остальные входят как обычные участники (user).
TG_ADMIN_IDS = set(x.strip() for x in
                   os.environ.get("TG_ADMIN_IDS", "").split(",") if x.strip())
# квоты токенов Claude в сутки
DAILY_TOKENS = int(os.environ.get("CP_DAILY_TOKENS", "300000"))       # совместимость
DAILY_TOKENS_DEV = int(os.environ.get("CP_DAILY_TOKENS_DEV", str(DAILY_TOKENS)))
DAILY_TOKENS_USER = int(os.environ.get("CP_DAILY_TOKENS_USER", "60000"))
PROJECT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,30}$")
TEMPLATES = {
    "python": {"main.py": '# Ваш первый скрипт\nprint("Привет из CodePocket!")\n'},
    "site": {
        "index.html": "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Мой сайт</title>\n<link rel=\"stylesheet\" href=\"style.css\">\n</head>\n"
        "<body>\n<h1>Привет!</h1>\n<button onclick=\"go()\">Нажми</button>\n"
        "<script src=\"app.js\"></script>\n</body>\n</html>\n",
        "style.css": "body{font-family:sans-serif;text-align:center;margin-top:40px}\n"
        "h1{color:#e07a3f}\nbutton{padding:10px 20px;font-size:16px}\n",
        "app.js": "function go(){ alert('Работает!'); }\n"},
    "bot": {"bot.py": '# Telegram-бот (заготовка)\n# установка: pip install python-telegram-bot\n'
            'TOKEN = "ВСТАВЬТЕ_ТОКЕН_БОТА"\nprint("настройте токен и попросите Claude дописать бота")\n'},
    "api": {"app.py": '# FastAPI-сервис (заготовка)\n# pip install fastapi uvicorn; запуск: uvicorn app:app --reload\n'
            'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get("/")\n'
            'def root():\n    return {"ok": True, "msg": "Привет из CodePocket"}\n'},
    "flask": {"app.py": '# Flask-сайт (заготовка)\n# pip install flask; запуск: python app.py\n'
              'from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route("/")\n'
              'def home():\n    return "<h1>Привет из Flask!</h1>"\n\n\n'
              'if __name__ == "__main__":\n    app.run(host="0.0.0.0", port=8000)\n'},
    "node": {"index.js": '// Node.js-скрипт\nconsole.log("Привет из Node!");\n',
             "package.json": '{\n  "name": "my-app",\n  "version": "1.0.0",\n'
             '  "main": "index.js",\n  "scripts": { "start": "node index.js" }\n}\n'},
    "bash": {"run.sh": '#!/usr/bin/env bash\n# Скрипт\necho "Привет из bash!"\ndate\n'},
    "data": {"analyze.py": '# Анализ данных\n# pip install pandas\nimport pandas as pd\n\n'
             'df = pd.DataFrame({"город": ["Москва", "СПб"], "цена": [55, 53]})\n'
             'print(df)\nprint("Средняя:", df["цена"].mean())\n'},
    "cpp": {"main.cpp": '// C++\n// компиляция: g++ main.cpp -o main && ./main\n'
            '#include <iostream>\nusing namespace std;\n\nint main() {\n'
            '    cout << "Привет из C++!" << endl;\n    return 0;\n}\n'},
    "empty": {},
}
_agent_busy = set()       # пользователи, у которых агент сейчас работает
_agent_busy_lock = threading.Lock()

# ---- защита от перебора: rate-limit по IP и блокировка PIN ----
_rl = {}                  # ключ -> список времён попыток
_rl_lock = threading.Lock()


def rate_ok(key, limit, window):
    """True, если попытка в пределах лимита. Иначе False."""
    now = time.time()
    with _rl_lock:
        arr = _rl.setdefault(key, [])
        arr[:] = [t for t in arr if now - t < window]
        if len(arr) >= limit:
            return False
        arr.append(now)
        return True


def audit(event, **kw):
    """Пишет строку в аудит-лог входов/регистраций/подключений.
    Лог содержит имена и IP — открываем строго 0600."""
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        line = json.dumps({"ts": int(time.time()), "event": event, **kw},
                          ensure_ascii=False)
        path = os.path.join(DATA_DIR, "audit.log")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,20}$")
RESERVED = {"root", "admin", "daemon", "bin", "sys", "sync", "games", "man", "lp",
            "mail", "news", "uucp", "proxy", "backup", "nobody", "sshd", "ubuntu",
            "caddy", "www-data", "systemd", "messagebus", "syslog"}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 МБ — крупнее в редактор не отдаём
ATTACH_MAX = 12 * 1024 * 1024    # 12 МБ — предел на одно вложение в чат
SESSION_TTL = 60 * 60 * 24 * 90  # 90 дней
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ----------------------------------------------------------------------------
# хранилище пользователей: /var/lib/claude-ide/users.json
# ----------------------------------------------------------------------------
_lock = threading.Lock()


def _db_path():
    return os.path.join(DATA_DIR, "users.json")


def db_load():
    try:
        with open(_db_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"users": {}, "sessions": {}}


def write_private(path, data, mode=0o600):
    """Атомарная запись файла, который с первой секунды доступен только root.

    Раньше временный файл создавался с правами по umask (0644) и права
    сужались уже ПОСЛЕ подмены — в этом окне users.json с хэшами PIN и
    шифрованными доступами к серверам мог прочитать любой пользователь
    машины, а shell у них есть."""
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def db_save(db):
    write_private(_db_path(), json.dumps(db, indent=1, ensure_ascii=False))


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 100_000).hex()


def user_exists_in_system(name):
    try:
        import pwd
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def get_uid_gid_home(name):
    import pwd
    p = pwd.getpwnam(name)
    return p.pw_uid, p.pw_gid, p.pw_dir


def create_linux_user(name):
    useradd = shutil.which("useradd") or "/usr/sbin/useradd"
    subprocess.run(
        [useradd, "-m", "-s", "/bin/bash", "-c", "claude-ide user", name],
        check=True, capture_output=True,
    )
    # чтобы пользователи не читали файлы друг друга
    _, _, home = get_uid_gid_home(name)
    os.chmod(home, 0o750)


def parse_device(ua):
    """Короткое имя устройства из user-agent."""
    ua = ua or ""
    if "iPhone" in ua:
        os_ = "iPhone"
    elif "iPad" in ua:
        os_ = "iPad"
    elif "Android" in ua:
        os_ = "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Mac OS" in ua or "Macintosh" in ua:
        os_ = "Mac"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = "устройство"
    if "Chrome" in ua and "Edg" not in ua:
        br = "Chrome"
    elif "Edg" in ua:
        br = "Edge"
    elif "Firefox" in ua:
        br = "Firefox"
    elif "Safari" in ua:
        br = "Safari"
    elif "OPR" in ua or "Opera" in ua:
        br = "Opera"
    else:
        br = ""
    return (os_ + (" · " + br if br else "")).strip()


def user_resources(name):
    """Оперативная память (МБ, RSS всех процессов пользователя), диск (МБ,
    размер домашней папки), число процессов."""
    ram_kb, procs = 0, 0
    try:
        r = subprocess.run(["ps", "-u", name, "-o", "rss="],
                           capture_output=True, text=True, timeout=5)
        for ln in r.stdout.split():
            try:
                ram_kb += int(ln)
                procs += 1
            except ValueError:
                pass
    except (subprocess.SubprocessError, OSError):
        pass
    disk_kb = 0
    try:
        import pwd
        home = pwd.getpwnam(name).pw_dir
        r = subprocess.run(["du", "-sk", "--exclude=.cache", home],
                           capture_output=True, text=True, timeout=8)
        disk_kb = int(r.stdout.split()[0])
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, IndexError):
        pass
    return {"ram_mb": round(ram_kb / 1024, 1), "procs": procs,
            "disk_mb": round(disk_kb / 1024, 1)}


# ----------------------------------------------------------------------------
# определение города/страны по IP (примерно, через ip-api.com, с кэшем)
# ----------------------------------------------------------------------------
_geo_cache = {}
_geo_lock = threading.Lock()
GEO_TTL = 7 * 24 * 3600  # результат живёт неделю


def _geo_path():
    return os.path.join(DATA_DIR, "geo.json")


def _geo_load_disk():
    try:
        with open(_geo_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _geo_save_disk():
    try:
        # кэш привязан к IP пользователей — не отдаём его остальным на машине
        write_private(_geo_path(), json.dumps(_geo_cache, ensure_ascii=False))
    except OSError:
        pass


def _is_trusted_proxy(ip):
    """Наш ли это обратный прокси. Приложение слушает 127.0.0.1 и стоит за
    Caddy на той же машине, поэтому доверяем только петле."""
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _ip_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def geo_lookup(ip):
    """Примерное местоположение по IP. Кэшируется, чтобы не дёргать сервис зря."""
    ip = (ip or "").strip()
    if not ip:
        return {}
    if not _ip_public(ip):
        return {"local": True}
    now = int(time.time())
    with _geo_lock:
        if not _geo_cache:
            _geo_cache.update(_geo_load_disk())
        c = _geo_cache.get(ip)
        if c and now - c.get("ts", 0) < GEO_TTL:
            return c
    data = {}
    try:
        url = ("http://ip-api.com/json/" + quote(ip) +
               "?fields=status,country,regionName,city,isp&lang=ru")
        req = urllib.request.Request(url, headers={"User-Agent": "CodePocket"})
        with urllib.request.urlopen(req, timeout=6) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        if j.get("status") == "success":
            data = {"country": j.get("country", ""),
                    "region": j.get("regionName", ""),
                    "city": j.get("city", ""),
                    "isp": j.get("isp", ""), "ts": now}
    except (urllib.error.URLError, OSError, ValueError):
        data = {}
    if data:
        with _geo_lock:
            _geo_cache[ip] = data
            _geo_save_disk()
    return data


MAX_SESSIONS_PER_USER = 10   # больше — это уже мусор, а не устройства


def prune_sessions(db, username):
    """Оставляет пользователю только свежие сессии.

    Каждый вход заводил новую запись, а Telegram Mini App логинится при каждом
    открытии — у активных пользователей накапливались десятки «устройств»
    (одному досталось 62), и список входов переставал что-либо значить."""
    mine = [(t, s) for t, s in db["sessions"].items()
            if s.get("user") == username]
    mine.sort(key=lambda x: -(x[1].get("seen") or x[1].get("ts") or 0))
    for t, _ in mine[MAX_SESSIONS_PER_USER:]:
        db["sessions"].pop(t, None)


def find_session(db, username, ua, ip):
    """Токен живой сессии этого же пользователя с того же устройства.

    Нужен, чтобы повторный вход с уже знакомого устройства продлевал текущую
    сессию, а не плодил новую запись рядом."""
    now = int(time.time())
    dev = parse_device(ua)
    best, best_seen = None, -1
    for t, s in db["sessions"].items():
        if s.get("user") != username or s.get("device") != dev:
            continue
        if now - (s.get("ts") or 0) > SESSION_TTL:
            continue
        seen = s.get("seen") or s.get("ts") or 0
        if seen > best_seen:
            best, best_seen = t, seen
    return best


def new_session(db, username, ip="", ua="", reuse=True, current=None):
    now = int(time.time())
    # Точный случай: браузер уже прислал живую cookie этого же пользователя
    # (Telegram логинится при каждом открытии) — продлеваем ровно её.
    if current:
        s = db["sessions"].get(current)
        if s and s.get("user") == username and now - (s.get("ts") or 0) <= SESSION_TTL:
            s["seen"] = now
            s["ip"] = ip or s.get("ip", "")
            return current
    # иначе — то же устройство того же человека, чтобы не плодить записи
    if reuse:
        tok = find_session(db, username, ua, ip)
        if tok:
            s = db["sessions"][tok]
            s["seen"] = now
            s["ip"] = ip or s.get("ip", "")
            return tok
    token = secrets.token_urlsafe(32)
    db["sessions"][token] = {"user": username, "ts": now, "seen": now,
                             "ip": ip, "ua": ua, "device": parse_device(ua)}
    # подчистим протухшие
    for t in list(db["sessions"]):
        if now - db["sessions"][t]["ts"] > SESSION_TTL:
            del db["sessions"][t]
    prune_sessions(db, username)
    # история входов в записи пользователя (последние 20)
    u = db["users"].get(username)
    if u is not None:
        logins = u.setdefault("logins", [])
        logins.append({"ts": now, "ip": ip, "device": parse_device(ua)})
        u["logins"] = logins[-20:]
    return token


def session_user(handler):
    """Достаёт пользователя из cookie. Возвращает username или None."""
    cookie = handler.headers.get("Cookie", "")
    token = None
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ide_session":
            token = v
    if not token:
        return None
    now = int(time.time())
    with _lock:
        db = db_load()
        sess = db["sessions"].get(token)
        if not sess:
            return None
        if now - sess["ts"] > SESSION_TTL:
            return None
        # Отмечаем, что сессией пользуются. Поле seen раньше ставилось один раз
        # при создании и больше не двигалось — «последняя активность» в списке
        # входов показывала дату регистрации устройства, а не последний заход.
        # Пишем не чаще раза в 5 минут, чтобы не дёргать базу на каждый запрос.
        if now - (sess.get("seen") or 0) > 300:
            sess["seen"] = now
            db_save(db)
        return sess["user"]


# ----------------------------------------------------------------------------
# безопасная работа с путями внутри домашней папки
# ----------------------------------------------------------------------------
def safe_path(home, rel):
    rel = (rel or "").lstrip("/")
    target = os.path.realpath(os.path.join(home, rel))
    home_real = os.path.realpath(home)
    if target != home_real and not target.startswith(home_real + os.sep):
        raise PermissionError("выход за пределы домашней папки")
    return target


# ----------------------------------------------------------------------------
# проекты, квоты, история чата
# ----------------------------------------------------------------------------
def projects_dir(home):
    d = os.path.join(home, "projects")
    return d


def ensure_projects_dir(username):
    uid, gid, home = get_uid_gid_home(username)
    d = projects_dir(home)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        os.chown(d, uid, gid)
    return d


def user_role(db, username):
    # По умолчанию — обычный участник. Раньше здесь было "dev", и любая запись
    # без поля role молча получала права администратора: ровно это происходило
    # бы при переносе базы из старой claude-ide, где роли ещё не было.
    return (db["users"].get(username) or {}).get("role", "user")


def role_quota(role):
    return DAILY_TOKENS_DEV if role == "dev" else DAILY_TOKENS_USER


def quota_state(db, username):
    """Возвращает (used, quota) на сегодня, сбрасывая счётчик при смене даты."""
    today = time.strftime("%Y-%m-%d")
    u = db["users"].get(username) or {}
    usage = u.get("usage") or {}
    if usage.get("date") != today:
        usage = {"date": today, "tokens": 0}
        u["usage"] = usage
    return usage.get("tokens", 0), role_quota(u.get("role", "user"))


def quota_add(username, tokens):
    with _lock:
        db = db_load()
        used, _ = quota_state(db, username)
        db["users"][username]["usage"]["tokens"] = used + tokens
        db_save(db)


AGENTS = ("claude", "codex")


def norm_agent(agent):
    """Имя агента, приведённое к известному списку. Всё неизвестное — claude.
    Значение приходит из запроса и попадает в имя файла, поэтому свободный
    текст сюда пускать нельзя."""
    return agent if agent in AGENTS else "claude"


def chat_path(username, project, agent="claude"):
    # оба куска идут в путь: проект проверяем регуляркой, агента — списком
    if not PROJECT_RE.match(project or ""):
        raise ValueError("плохое имя проекта")
    agent = norm_agent(agent)
    d = os.path.join(DATA_DIR, "chats", username)
    os.makedirs(d, mode=0o700, exist_ok=True)
    suffix = "" if agent == "claude" else "." + agent   # claude — старый файл
    return os.path.join(d, project + suffix + ".json")


def chat_load(username, project, agent="claude"):
    try:
        with open(chat_path(username, project, agent)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"api": [], "ui": []}


_chat_write_lock = threading.Lock()


def chat_save(username, project, data, agent="claude"):
    # держим историю в разумных пределах
    data["api"] = data["api"][-40:]
    data["ui"] = data["ui"][-200:]
    # пишем атомарно: сначала во временный файл, потом подменяем. Иначе обрыв
    # или параллельная запись оставляют обрезанный JSON — а он читается как
    # пустая история, то есть переписка «пропадает».
    write_private(chat_path(username, project, agent),
                  json.dumps(data, ensure_ascii=False))


def chat_update(username, project, agent, apply_fn):
    """Прочитать → изменить → записать под общим замком.
    Нужно всякий раз, когда правится ОДНО поле: без этого фоновая правка
    записывает снимок, снятый до чужой отправки, и затирает свежие реплики.
    apply_fn возвращает False — значит менять ничего не надо."""
    with _chat_write_lock:
        data = chat_load(username, project, agent)
        if apply_fn(data) is False:
            return None
        chat_save(username, project, data, agent)
        return data


# ----------------------------------------------------------------------------
# история версий файлов (снапшоты при сохранении)
# ----------------------------------------------------------------------------
HISTORY_KEEP = 20               # сколько версий хранить на файл
HISTORY_MAX_BYTES = 512 * 1024  # не снапшотим файлы крупнее


def _history_dir(username, relpath):
    # ключ файла — хэш относительного пути, чтобы не городить вложенность
    h = hashlib.sha1(relpath.encode()).hexdigest()[:16]
    d = os.path.join(DATA_DIR, "history", username, h)
    return d


def history_snapshot(username, relpath, content_bytes):
    """Сохраняет предыдущую версию файла перед перезаписью."""
    if len(content_bytes) > HISTORY_MAX_BYTES or b"\x00" in content_bytes[:4096]:
        return
    d = _history_dir(username, relpath)
    os.makedirs(d, exist_ok=True)
    # метка времени в миллисекундах — чтобы частые сохранения не затирались
    ts = int(time.time() * 1000)
    fn = os.path.join(d, f"{ts}.snap")
    while os.path.exists(fn):
        ts += 1
        fn = os.path.join(d, f"{ts}.snap")
    try:
        with open(fn, "wb") as f:
            f.write(content_bytes)
        # запомним оригинальный путь для отображения
        with open(os.path.join(d, "path.txt"), "w") as f:
            f.write(relpath)
    except OSError:
        return
    # чистим старые
    snaps = sorted(g for g in os.listdir(d) if g.endswith(".snap"))
    for old in snaps[:-HISTORY_KEEP]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass


def history_list(username, relpath):
    d = _history_dir(username, relpath)
    if not os.path.isdir(d):
        return []
    out = []
    for g in sorted((x for x in os.listdir(d) if x.endswith(".snap")), reverse=True):
        ts = int(g[:-5])
        out.append({"ts": ts, "size": os.path.getsize(os.path.join(d, g))})
    return out


def history_get(username, relpath, ts):
    d = _history_dir(username, relpath)
    fn = os.path.join(d, f"{int(ts)}.snap")
    if not os.path.isfile(fn):
        return None
    with open(fn, "rb") as f:
        return f.read()


# ----------------------------------------------------------------------------
# живая проверка кода (диагностика ошибок)
# ----------------------------------------------------------------------------
def lint_python(username, code):
    """
    Проверяет Python-код на ошибки, НЕ выполняя его.
    py_compile ловит синтаксис, pyflakes (если есть) — неиспользуемое/неизвестное.
    Возвращает список {line, col, msg, type}.
    """
    import tempfile
    diags = []
    uid, gid, home = get_uid_gid_home(username)
    fd, tmp = tempfile.mkstemp(suffix=".py", dir="/tmp")
    try:
        os.write(fd, code.encode())
        os.close(fd)
        # 0640 и группа пользователя: читает только он сам, а не все на машине
        try:
            os.chown(tmp, 0, gid)
            os.chmod(tmp, 0o640)
        except OSError:
            os.chmod(tmp, 0o600)

        def demote():
            # setgroups ОБЯЗАТЕЛЕН и обязан идти первым: без него дочерний
            # процесс сохраняет дополнительные группы root и остаётся в них
            # даже после setuid
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

        # 1) синтаксис — compile() без записи байткода и без исполнения кода
        check = ("import sys\n"
                 "try:\n"
                 "    compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')\n"
                 "except SyntaxError as e:\n"
                 "    print(f'{e.lineno or 1}\\t{e.msg}')\n"
                 "    sys.exit(3)\n")
        r = subprocess.run(
            ["python3", "-c", check, tmp],
            capture_output=True, text=True, timeout=10,
            preexec_fn=demote, errors="replace")
        if r.returncode == 3 and r.stdout.strip():
            parts = r.stdout.strip().split("\t", 1)
            line = int(parts[0]) if parts[0].isdigit() else 1
            msg = parts[1] if len(parts) > 1 else "синтаксическая ошибка"
            diags.append({"line": line, "col": 1, "msg": msg, "type": "error"})
            return diags  # дальше проверять смысла нет

        # 2) pyflakes — неизвестные имена, неиспользованный импорт (если установлен)
        try:
            r2 = subprocess.run(
                ["python3", "-m", "pyflakes", tmp],
                capture_output=True, text=True, timeout=10,
                preexec_fn=demote, errors="replace")
            for ln in (r2.stdout or "").splitlines():
                mm = re.match(r'.+?:(\d+):(?:(\d+):)?\s*(.+)', ln)
                if mm:
                    diags.append({"line": int(mm.group(1)),
                                  "col": int(mm.group(2) or 1),
                                  "msg": mm.group(3).strip(), "type": "warn"})
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        try:
            os.remove(tmp + "c")
        except OSError:
            pass
    return diags


# ----------------------------------------------------------------------------
# «свои серверы»: шифрованное хранение доступов и SSH-подключение
# ----------------------------------------------------------------------------
_srv_key_cache = None


def _server_secret_key():
    """32-байтный ключ шифрования, хранится в data-dir с правами 0600."""
    global _srv_key_cache
    if _srv_key_cache is not None:
        return _srv_key_cache
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        with open(path, "rb") as f:
            _srv_key_cache = f.read()
    except FileNotFoundError:
        _srv_key_cache = secrets.token_bytes(32)
        # 0600 с момента создания, а не после записи
        write_private(path, _srv_key_cache)
    return _srv_key_cache


def _keystream(nonce, n):
    key = _server_secret_key()
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def _mac_key():
    """Отдельный ключ для HMAC, выведенный из мастер-ключа."""
    return hashlib.sha256(_server_secret_key() + b"cp-mac-v1").digest()


def enc_secret(plaintext):
    """Шифрует строку по схеме encrypt-then-MAC (защита от подделки).
    Возвращает base64(0x01 | nonce | ciphertext | HMAC-SHA256)."""
    data = plaintext.encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(nonce, len(data))))
    tag = hmac.new(_mac_key(), nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x01" + nonce + ct + tag).decode()


def dec_secret(blob):
    """Расшифровывает строку, записанную enc_secret. Формат один:
    0x01 | nonce(16) | ct | HMAC-SHA256(32). Не сошёлся тег — данные повреждены
    или подделаны, и мы отказываемся их читать.

    Прежняя версия при неверном теге молча падала в старый формат без проверки
    целостности — то есть аутентификацию можно было обойти, просто испортив
    тег. Записи старого формата переводятся разово (migrate_secrets)."""
    raw = base64.b64decode(blob)
    if raw[:1] != b"\x01" or len(raw) < 1 + 16 + 32:
        raise ValueError("секрет в неизвестном формате")
    nonce, tag, ct = raw[1:17], raw[-32:], raw[17:-32]
    expect = hmac.new(_mac_key(), nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, tag):
        raise ValueError("секрет повреждён или подделан")
    return bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct)))).decode()


def _dec_legacy(blob):
    """Чтение старого формата без тега — только для разовой миграции."""
    raw = base64.b64decode(blob)
    nonce, ct = raw[:16], raw[16:]
    return bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct)))).decode()


def migrate_secrets():
    """Переводит секреты старого формата (без HMAC) в новый. Выполняется один
    раз при старте: после этого dec_secret работает только с проверкой тега."""
    fields = ("ai_claude_key", "ai_codex_key")
    moved = 0
    with _lock:
        db = db_load()
        for urec in db.get("users", {}).values():
            for srv in urec.get("servers") or ():
                blob = srv.get("secret")
                if not blob:
                    continue
                try:
                    dec_secret(blob)
                    continue                      # уже новый формат
                except (ValueError, binascii.Error):
                    pass
                try:
                    srv["secret"] = enc_secret(_dec_legacy(blob))
                    moved += 1
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    pass                          # не расшифровался — оставляем как есть
            for f in fields:
                blob = urec.get(f)
                if not blob:
                    continue
                try:
                    dec_secret(blob)
                    continue
                except (ValueError, binascii.Error):
                    pass
                try:
                    urec[f] = enc_secret(_dec_legacy(blob))
                    moved += 1
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    pass
        if moved:
            db_save(db)
    return moved


def user_servers(db, username):
    return db["users"].get(username, {}).setdefault("servers", [])


def server_public(s):
    """Версия записи сервера для отдачи в интерфейс — без секретов.
    Публичный ключ отдаём (он не секрет) — чтобы показать команду установки."""
    return {"id": s["id"], "name": s["name"], "host": s["host"],
            "port": s["port"], "user": s["user"], "auth": s["auth"],
            "pubkey": s.get("pubkey", ""), "generated": s.get("generated", False),
            "workdir": s.get("workdir", ""), "recent_dirs": s.get("recent_dirs", [])}


def gen_ssh_keypair(comment="codepocket"):
    """Генерирует пару ключей ed25519. Возвращает (приватный, публичный) или None."""
    keygen = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"
    d = tempfile.mkdtemp(prefix="cpkey_")
    try:
        kf = os.path.join(d, "id")
        r = subprocess.run([keygen, "-t", "ed25519", "-N", "", "-C", comment,
                            "-f", kf], capture_output=True, timeout=15)
        if r.returncode != 0:
            return None
        with open(kf) as f:
            priv = f.read()
        with open(kf + ".pub") as f:
            pub = f.read().strip()
        return priv, pub
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def gen_password(length=20):
    """Надёжный пароль без похожих символов (0/O, 1/l/I)."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    specials = "!@#%*-_=+"
    pw = [secrets.choice(alphabet) for _ in range(length - 3)]
    pw += [secrets.choice(specials) for _ in range(3)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


def tcp_ping(host, port, timeout=4):
    """Проверка доступности сервера: пробуем TCP-подключение к host:port."""
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"online": True, "ms": int((time.time() - t0) * 1000)}
    except (OSError, ValueError, OverflowError):
        return {"online": False}


def get_server(username, server_id):
    with _lock:
        db = db_load()
        for s in user_servers(db, username):
            if s["id"] == server_id:
                return s
    return None


def ai_key_get(username, agent):
    """Расшифрованный API-ключ пользователя для агента (claude|codex) или ''."""
    with _lock:
        db = db_load()
        u = db["users"].get(username) or {}
    blob = u.get("ai_%s_key" % agent)
    if not blob:
        return ""
    try:
        return dec_secret(blob)
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# WebSocket: рукопожатие и фреймы (RFC 6455, без зависимостей)
# ----------------------------------------------------------------------------
def ws_accept_key(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_read_frame(rfile):
    """Возвращает (opcode, payload) или None при закрытии соединения."""
    head = rfile.read(2)
    if len(head) < 2:
        return None
    b1, b2 = head
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    if length > 1024 * 1024:
        return None
    mask = rfile.read(4) if masked else b"\x00" * 4
    data = bytearray(rfile.read(length))
    if masked:
        for i in range(len(data)):
            data[i] ^= mask[i % 4]
    return opcode, bytes(data)


def ws_send(conn, lock, payload, opcode=2):
    """Отправка фрейма клиенту (сервер не маскирует)."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    with lock:
        conn.sendall(bytes(header) + payload)


# ----------------------------------------------------------------------------
# лимиты ресурсов на пользователя (память/процессы/CPU/диск)
# ----------------------------------------------------------------------------
# Память и число процессов ограничиваем на КАЖДЫЙ терминал через cgroup
# (systemd-run --scope) — это честный лимит по реально занятой памяти (RSS).
# Если systemd-run недоступен, откатываемся на rlimit (защита от форк-бомб
# и огромных файлов). Диск — мягкая квота: показываем и предупреждаем.
LIMITS_DEFAULT = {
    "mem_mb": 512,     # потолок оперативной памяти на терминал
    "tasks": 200,      # максимум процессов у пользователя (защита от форк-бомбы)
    "cpu_pct": 80,     # потолок CPU, % одного ядра
    "disk_mb": 1024,   # мягкая квота на домашнюю папку
    "fsize_mb": 200,   # максимальный размер одного файла
}
_LIMIT_BOUNDS = {
    "mem_mb": (128, 8192), "tasks": (32, 4096), "cpu_pct": (10, 400),
    "disk_mb": (128, 51200), "fsize_mb": (10, 4096),
}


def _limits_path():
    return os.path.join(DATA_DIR, "limits.json")


def limits_load():
    L = dict(LIMITS_DEFAULT)
    try:
        with open(_limits_path()) as f:
            saved = json.load(f)
        for k in LIMITS_DEFAULT:
            if isinstance(saved.get(k), (int, float)):
                L[k] = int(saved[k])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return L


def limits_save(new):
    L = limits_load()
    for k, (lo, hi) in _LIMIT_BOUNDS.items():
        if k in new and isinstance(new[k], (int, float)):
            L[k] = max(lo, min(hi, int(new[k])))
    write_private(_limits_path(), json.dumps(L, indent=1), mode=0o644)
    return L


SYSTEMD_RUN = shutil.which("systemd-run")
_sd_ok = None


def systemd_scoping_works():
    """Проверяем один раз, что systemd-run --scope с cgroup-лимитами реально
    работает на этой машине (полная форма аргументов, под nobody)."""
    global _sd_ok
    if _sd_ok is not None:
        return _sd_ok
    _sd_ok = False
    if SYSTEMD_RUN:
        try:
            probe = [SYSTEMD_RUN, "--quiet", "--collect", "--scope",
                     "--uid=nobody", "--gid=nogroup",
                     "-p", "MemoryMax=64M", "-p", "MemorySwapMax=64M",
                     "-p", "TasksMax=32", "-p", "CPUQuota=50%",
                     "--setenv=HOME=/tmp", "--setenv=TERM=dumb",
                     "/bin/true"]
            r = subprocess.run(probe, capture_output=True, timeout=10)
            _sd_ok = (r.returncode == 0)
        except (subprocess.SubprocessError, OSError):
            _sd_ok = False
    return _sd_ok


def _systemd_prefix(username, gid, home, env, L):
    """Аргументы systemd-run, оборачивающие команду в cgroup с лимитами."""
    args = [SYSTEMD_RUN, "--quiet", "--collect", "--scope",
            "--uid=" + username, "--gid=" + str(gid),
            "-p", "MemoryMax=%dM" % L["mem_mb"],
            "-p", "MemorySwapMax=%dM" % L["mem_mb"],
            "-p", "TasksMax=%d" % L["tasks"],
            "-p", "CPUQuota=%d%%" % L["cpu_pct"]]
    # окружение — только через --setenv (иначе systemd-run его не пробросит)
    for k, v in env.items():
        args.append("--setenv=%s=%s" % (k, v))
    return args


def ensure_user_venv(username):
    """Личное python-окружение пользователя ~/.venv, чтобы `pip install`
    работал без root и без ошибки externally-managed (PEP 668).
    Создаётся один раз. --system-site-packages — чтобы видеть системные
    библиотеки, но ставить свои поверх. Возвращает путь venv или None."""
    try:
        uid, gid, home = get_uid_gid_home(username)
    except KeyError:
        return None
    venv = os.path.join(home, ".venv")
    if os.path.exists(os.path.join(venv, "bin", "python")):
        return venv
    runuser = shutil.which("runuser") or "/usr/sbin/runuser"
    if not os.path.exists(runuser):
        return None
    try:
        subprocess.run([runuser, "-u", username, "--",
                        "python3", "-m", "venv", "--system-site-packages", venv],
                       capture_output=True, timeout=45)
    except (subprocess.SubprocessError, OSError):
        pass
    return venv if os.path.exists(os.path.join(venv, "bin", "python")) else None


def _apply_rlimits(L):
    """Резервная защита без cgroup: число процессов и размер файла.
    RLIMIT_AS сознательно НЕ ставим — он считает виртуальную память и ломает
    node/компиляторы; память ограничивает cgroup (основной путь)."""
    def _set(res, val):
        try:
            resource.setrlimit(res, (val, val))
        except (ValueError, OSError):
            pass
    _set(resource.RLIMIT_NPROC, L["tasks"])
    _set(resource.RLIMIT_FSIZE, L["fsize_mb"] * 1024 * 1024)
    _set(resource.RLIMIT_CORE, 0)


# ----------------------------------------------------------------------------
# терминал: PTY под учёткой пользователя
# ----------------------------------------------------------------------------
def spawn_shell(username):
    """Возвращает (pid, fd мастера PTY)."""
    uid, gid, home = get_uid_gid_home(username)
    venv = ensure_user_venv(username)
    venv_bin = f"{home}/.venv/bin:" if venv else ""
    env = {
        "HOME": home,
        "USER": username,
        "LOGNAME": username,
        "SHELL": "/bin/bash",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        # ~/.venv первым — python/pip берутся из личного окружения (pip install
        # работает без root); PIP_BREAK_SYSTEM_PACKAGES — на случай системного pip
        "VIRTUAL_ENV": f"{home}/.venv" if venv else "",
        "PIP_BREAK_SYSTEM_PACKAGES": "1",
        "PATH": f"{venv_bin}{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    if not venv:
        env.pop("VIRTUAL_ENV", None)
    if ANTHROPIC_API_KEY and EXPOSE_KEY_IN_TERMINAL:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

    L = limits_load()
    use_sd = systemd_scoping_works()

    pid, fd = pty.fork()
    if pid == 0:  # дочерний процесс
        if use_sd:
            # systemd-run сам переключит пользователя и наложит cgroup-лимиты.
            # Мы лишь заходим в домашнюю папку (root может войти в 0750 home).
            try:
                os.chdir(home)
            except OSError:
                pass
            argv = _systemd_prefix(username, gid, home, env, L) + ["/bin/bash", "-l"]
            try:
                os.execve(argv[0], argv, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
            except Exception:
                os._exit(1)
            os._exit(1)
        else:
            try:
                os.initgroups(username, gid)
                os.setgid(gid)
                os.setuid(uid)
                _apply_rlimits(L)
                os.chdir(home)
            except Exception:
                os._exit(1)
            os.execve("/bin/bash", ["bash", "-l"], env)
            os._exit(1)
    return pid, fd


def spawn_ssh(username, server):
    """
    Терминал на УДАЛЁННОМ сервере пользователя по SSH.
    Запускается под учёткой пользователя (чтобы временный ключ не был доступен
    другим), затем внутри exec-ается ssh/sshpass.
    Возвращает (pid, fd, cleanup) — cleanup удаляет временный файл ключа.
    """
    uid, gid, home = get_uid_gid_home(username)
    host = server["host"]
    port = str(server.get("port") or 22)
    ruser = server["user"]
    auth = server["auth"]
    # ValueError здесь ловит вызывающий (_handle_ws) и показывает понятный текст
    secret = dec_secret(server["secret"])

    keyfile = None
    # закрепление host-ключа (TOFU): первый раз принимаем, потом сверяем.
    # known_hosts в домашней папке пользователя (ssh бежит под ним).
    known = os.path.join(home, ".cp_known_" + server["id"])
    if not os.path.exists(known):
        try:
            fd_k = os.open(known, os.O_CREAT | os.O_WRONLY, 0o600)
            os.fchown(fd_k, uid, gid)
            os.close(fd_k)
        except OSError:
            pass
    common = ["-tt",
              "-o", "StrictHostKeyChecking=accept-new",
              "-o", "GlobalKnownHostsFile=/dev/null",
              "-o", "UserKnownHostsFile=" + known,
              "-o", "ConnectTimeout=15",
              "-o", "ServerAliveInterval=30",
              "-p", port]
    env = {"HOME": home, "USER": username, "TERM": "xterm-256color",
           "LANG": "C.UTF-8",
           "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}

    # держим сессию в tmux на СЕРВЕРЕ ПОЛЬЗОВАТЕЛЯ — тогда при перезагрузке
    # страницы (обрыв ssh) Claude/Codex/процессы продолжают жить, а мы
    # переподключаемся к той же сессии. Если tmux нет — обычный shell.
    # Полоса состояния tmux («[cpkt] 0:bash* "хост" 12:47 05-Aug-26») съедает
    # на телефоне целую строку из полутора десятков и ничего не сообщает: имя
    # сессии и хост человек и так видит в шапке приложения. Гасим её и заодно
    # включаем мышь — тогда прокрутка вывода работает жестом, а не только
    # клавишами. Настройки задаются для нашей сессии, чужие tmux не трогаем.
    tmux_opts = ("set -g status off \\; "
                 "set -g mouse on \\; "
                 "set -g history-limit 20000")
    wd = server.get("workdir") or ""
    if wd:
        wq = shlex.quote(wd)
        remote_cmd = ('command -v tmux >/dev/null 2>&1 && '
                      'exec tmux new-session -A -s cpkt -c %s \\; %s || '
                      '{ cd %s 2>/dev/null; exec "${SHELL:-/bin/bash}" -l; }'
                      % (wq, tmux_opts, wq))
    else:
        remote_cmd = ('command -v tmux >/dev/null 2>&1 && '
                      'exec tmux new-session -A -s cpkt \\; %s || '
                      'exec "${SHELL:-/bin/bash}" -l' % tmux_opts)

    if auth == "key":
        # временный файл ключа в домашней папке пользователя, 0600
        fd_key, keyfile = _mkstemp_for(uid, gid, home)
        os.write(fd_key, secret.encode() + (b"" if secret.endswith("\n") else b"\n"))
        os.close(fd_key)
        argv = ["ssh"] + common + ["-o", "IdentitiesOnly=yes",
                                   "-i", keyfile, f"{ruser}@{host}", remote_cmd]
        prog = shutil.which("ssh") or "/usr/bin/ssh"
    else:  # password
        env["SSHPASS"] = secret
        argv = ["sshpass", "-e", "ssh"] + common + [
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no", f"{ruser}@{host}", remote_cmd]
        prog = shutil.which("sshpass") or "/usr/bin/sshpass"

    L = limits_load()
    pid, fd = pty.fork()
    if pid == 0:  # дочерний процесс
        try:
            os.initgroups(username, gid)
            os.setgid(gid)
            os.setuid(uid)
            _apply_rlimits(L)
            os.chdir(home)
        except Exception:
            os._exit(1)
        try:
            os.execvpe(prog, argv, env)
        except Exception:
            os._exit(1)
        os._exit(1)

    def cleanup():
        if keyfile:
            try:
                os.remove(keyfile)
            except OSError:
                pass
    return pid, fd, cleanup


def _mkstemp_for(uid, gid, home):
    """Временный файл 0600 во владении пользователя."""
    name = os.path.join(home, ".cp_key_" + secrets.token_hex(6))
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchown(fd, uid, gid)
    except OSError:
        pass
    return fd, name


# ----------------------------------------------------------------------------
# чат-агент, работающий через `claude` на СЕРВЕРЕ ПОЛЬЗОВАТЕЛЯ (по подписке Max)
# ----------------------------------------------------------------------------
# Шум мультиплексора ssh — НЕ ошибка сервера. Когда мастер-соединение истекает
# (ControlPersist) или его в этот момент поднимает соседний поток, ssh пишет
# «Control socket connect(...): Connection refused» и молча подключается заново.
# Если не вырезать эту строку, диагностика принимает её за «сервер выключен» —
# ровно та ложная тревога «Сервер не отвечает по SSH», хотя сервер жив.
_SSH_NOISE = ("control socket", "controlpath", "muxserver", "mux_client",
              "control master", "multiplex", "disabling multiplexing",
              "no such file or directory: " + os.path.join(DATA_DIR, "mux"),
              "warning: permanently added", "pseudo-terminal will not be allocated")


def ssh_clean_err(err_text):
    """Убирает служебные строки ssh (мультиплексор, known_hosts) — остаётся
    только то, что действительно говорит о проблеме с сервером."""
    if isinstance(err_text, bytes):
        err_text = err_text.decode("utf-8", "replace")
    keep = [ln for ln in (err_text or "").splitlines()
            if ln.strip() and not any(n in ln.lower() for n in _SSH_NOISE)]
    return "\n".join(keep).strip()


# живые ssh-процессы по пользователям — чтобы кнопка «Стоп» могла их прервать
_procs_lock = threading.Lock()
_user_procs = {}          # username -> set(Popen)
_agent_cancel = set()     # пользователи, попросившие остановить агента
_cur_user = threading.local()   # какой пользователь обслуживается в этом потоке


def ssh_owner(username):
    """Пометить поток: все ssh-команды в нём принадлежат этому пользователю
    (нужно, чтобы «Стоп» убил именно его процессы)."""
    _cur_user.name = username


def agent_cancel_requested(username):
    with _procs_lock:
        return username in _agent_cancel


def agent_cancel_clear(username):
    with _procs_lock:
        _agent_cancel.discard(username)


def agent_cancel(username):
    """Остановить работу агента: помечаем отмену и убиваем его ssh-процессы."""
    with _procs_lock:
        _agent_cancel.add(username)
        procs = list(_user_procs.get(username) or ())
    for pr in procs:
        try:
            pr.kill()
        except Exception:
            pass
    return len(procs)


_mux_locks_guard = threading.Lock()
_mux_locks = {}


def _mux_spawn(sock, argv_head, env, host_arg):
    """Поднять фоновое мастер-соединение для повторного использования.
    Запускаем ОТДЕЛЬНЫМ процессом с /dev/null на месте stdin/stdout/stderr —
    тогда он может жить сколько угодно и не блокировать чтение вывода команд.
    Всё «best effort»: не получилось — команды просто пойдут напрямую."""
    with _mux_locks_guard:
        lk = _mux_locks.setdefault(sock, threading.Lock())
    if not lk.acquire(blocking=False):
        return                      # мастера уже поднимает соседний поток
    try:
        if os.path.exists(sock):
            return
        argv = argv_head + ["-M", "-N", "-f",
                            "-o", "ControlPersist=600", host_arg]
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
        for _ in range(60):         # ждём появления сокета, но не дольше ~6 с
            if os.path.exists(sock):
                return
            time.sleep(0.1)
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        lk.release()


# ── живой прогресс агента ────────────────────────────────────────────────────
# Пока агент работает, он присылает события построчно. Складываем их сюда, а
# приложение коротко опрашивает /api/chat-progress и показывает «что происходит
# прямо сейчас» — так же, как это видно в VS Code.
_prog_lock = threading.Lock()
_progress = {}      # username -> {"seq": int, "steps": [...], "note": str}


def progress_reset(username, note=""):
    with _prog_lock:
        _progress[username] = {"seq": 0, "steps": [], "note": note}


def progress_add(username, step):
    """Добавить шаг. Если это результат уже показанного вызова — дополняем его."""
    with _prog_lock:
        p = _progress.get(username)
        if p is None:
            p = _progress[username] = {"seq": 0, "steps": [], "note": ""}
        p["seq"] += 1
        step = dict(step, n=p["seq"])
        p["steps"].append(step)
        del p["steps"][:-80]        # держим только хвост


def progress_patch(username, key, patch):
    """Дополнить ранее добавленный шаг (пришёл вывод команды)."""
    with _prog_lock:
        p = _progress.get(username)
        if not p:
            return
        for st in reversed(p["steps"]):
            if st.get("key") == key:
                st.update(patch)
                p["seq"] += 1
                st["n"] = p["seq"]
                return


def progress_note(username, note):
    with _prog_lock:
        p = _progress.setdefault(username, {"seq": 0, "steps": [], "note": ""})
        p["note"] = note
        p["seq"] += 1


def progress_get(username, since=0):
    with _prog_lock:
        p = _progress.get(username)
        if not p:
            return {"seq": 0, "steps": [], "note": ""}
        return {"seq": p["seq"], "note": p["note"],
                "steps": [s for s in p["steps"] if s.get("n", 0) > since]}


def _mux_dir():
    """Каталог для управляющих сокетов SSH: только root, права 0700.

    Живёт в DATA_DIR, а не в /tmp: /tmp общий и доступен на запись всем
    пользователям сервера, поэтому предсказуемое имя там позволяет подложить
    свой каталог или ссылку и подключиться к чужому мастер-соединению.
    Возвращает путь или None, если безопасный каталог получить не удалось —
    тогда вызывающий код работает без мультиплексирования."""
    path = os.path.join(DATA_DIR, "mux")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
        # каталог, наш, и никто посторонний в него не пишет
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != 0
                or st.st_mode & 0o077):
            os.chmod(path, 0o700)
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
                return None
        return path
    except OSError:
        return None


def ssh_exec(server, remote_cmd, stdin_bytes=None, timeout=300, on_line=None):
    """Выполнить команду на сервере пользователя по SSH, вернуть (rc, out, err).
    on_line(bytes) — если задан, вызывается на каждую строку stdout по мере её
    прихода (нужно для «живого» показа работы агента)."""
    host = server["host"]
    port = str(server.get("port") or 22)
    ruser = server["user"]
    auth = server["auth"]
    try:
        secret = dec_secret(server["secret"])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return 255, b"", ("ssh: сохранённый доступ к серверу не читается — "
                          "задайте его заново в разделе «Серверы»").encode()
    d = tempfile.mkdtemp(prefix="cpssh_")
    known = os.path.join(d, "known")
    # мультиплексирование: держим ОДНО подключение и переиспользуем его для всех
    # команд (история, Codex, файлы). Меньше новых коннектов → сервер/файрвол
    # (fail2ban) не отбивает по «слишком много подключений».
    #
    # Каталог сокетов лежит в DATA_DIR (владелец root, 0700), а НЕ в /tmp:
    # у пользователей на этой машине есть shell, а предсказуемый путь в общем
    # /tmp позволял бы подложить туда свой каталог или символьную ссылку и
    # перехватить чужое мастер-соединение SSH (то есть доступ к чужим серверам).
    mux = _mux_dir()
    if mux is None:
        mux = d
    # ВАЖНО: ControlMaster=auto здесь ставить нельзя. Тогда первая же команда сама
    # становится мастером, уходит в фон по ControlPersist и уносит с собой наши
    # stdout/stderr — communicate() ждёт EOF на трубах, которых фоновый мастер не
    # отпускает, и висит до конца ControlPersist. Поэтому:
    #   • команда всегда обычный клиент (ControlMaster=no) — выходит сразу;
    #   • сокета нет → клиент честно подключается сам (это штатный откат);
    #   • мастера поднимаем ОТДЕЛЬНО, с /dev/null вместо труб (_mux_spawn).
    sock = mux + "/" + hashlib.sha1(
        ("%s@%s:%s" % (ruser, host, port)).encode()).hexdigest()[:16]
    common = ["-o", "StrictHostKeyChecking=accept-new",
              "-o", "GlobalKnownHostsFile=/dev/null",
              "-o", "UserKnownHostsFile=" + known,
              "-o", "ControlMaster=no", "-o", "ControlPath=" + sock,
              "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
              "-o", "ConnectTimeout=15", "-p", port]
    env = dict(os.environ)
    try:
        if auth == "key":
            kf = os.path.join(d, "key")
            with open(kf, "w") as f:
                f.write(secret if secret.endswith("\n") else secret + "\n")
            os.chmod(kf, 0o600)
            head = (["ssh"] + common + ["-o", "IdentitiesOnly=yes",
                    "-o", "BatchMode=yes", "-i", kf])
        else:
            env["SSHPASS"] = secret
            head = (["sshpass", "-e", "ssh"] + common +
                    ["-o", "PreferredAuthentications=password",
                     "-o", "PubkeyAuthentication=no"])
        argv = head + [f"{ruser}@{host}", remote_cmd]
        # заранее поднимем общий канал — следующие команды пойдут по нему мгновенно
        if not os.path.exists(sock):
            _mux_spawn(sock, head, env, f"{ruser}@{host}")
        owner = getattr(_cur_user, "name", None)
        last = (255, b"", b"")
        for attempt in range(3):
            pr = None
            try:
                pr = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env)
                if owner:
                    with _procs_lock:
                        _user_procs.setdefault(owner, set()).add(pr)
                if on_line is not None:
                    # читаем вывод построчно, пока команда идёт — так приложение
                    # показывает работу агента в реальном времени, а не в конце
                    chunks, errbuf = [], []

                    def _drain_err():
                        try:
                            for ln in iter(pr.stderr.readline, b""):
                                errbuf.append(ln)
                        except Exception:
                            pass
                    th = threading.Thread(target=_drain_err, daemon=True)
                    th.start()
                    deadline = time.time() + timeout
                    try:
                        pr.stdin.close()
                    except Exception:
                        pass
                    try:
                        for ln in iter(pr.stdout.readline, b""):
                            chunks.append(ln)
                            try:
                                on_line(ln)
                            except Exception:
                                pass
                            if time.time() > deadline:
                                pr.kill()
                                break
                    except Exception:
                        pass
                    try:
                        pr.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pr.kill()
                    th.join(timeout=2)
                    last = (pr.returncode if pr.returncode is not None else 255,
                            b"".join(chunks), b"".join(errbuf))
                else:
                    try:
                        out, err_b = pr.communicate(input=stdin_bytes, timeout=timeout)
                    except subprocess.TimeoutExpired:
                        pr.kill()
                        out, err_b = pr.communicate()
                        last = (255, out or b"", (err_b or b"") + b"\nssh: command timed out")
                    else:
                        last = (pr.returncode, out, err_b)
            except (subprocess.SubprocessError, OSError) as e:
                last = (255, b"", str(e).encode())
            finally:
                if pr is not None and owner:
                    with _procs_lock:
                        s = _user_procs.get(owner)
                        if s:
                            s.discard(pr)
                            if not s:
                                _user_procs.pop(owner, None)
            if last[0] == 0:
                return last
            # пользователь нажал «Стоп» — не повторяем, это не сбой сети
            if owner and agent_cancel_requested(owner):
                return (255, last[1], b"__CANCELLED__")
            # шум мультиплексора вырезаем ДО разбора: иначе «Control socket
            # connect: Connection refused» читается как «сервер недоступен»
            errtxt = ssh_clean_err(last[2]).lower()
            # повторяем только при сетевых сбоях подключения, а не при ошибке команды
            transient = ("connection refused" in errtxt or "connection reset" in errtxt
                         or "connection timed out" in errtxt or "timed out" in errtxt
                         or "connection closed" in errtxt or "no route to host" in errtxt
                         or "broken pipe" in errtxt or "kex_exchange" in errtxt)
            if not transient or attempt == 2:
                return last
            time.sleep(1.5 * (attempt + 1))
        return last
    except (subprocess.SubprocessError, OSError) as e:
        return 255, b"", str(e).encode()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def ssh_alive(server, timeout=6):
    """Быстрая проверка «сервер реально жив»: TCP + приветствие sshd.
    Не требует ключей и занимает доли секунды."""
    try:
        with socket.create_connection(
                (server["host"], int(server.get("port") or 22)), timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).startswith(b"SSH-")
    except (OSError, ValueError):
        return False


# Строки, которые печатает САМ ssh-клиент. Всё остальное в stderr — это вывод
# команды, запущенной на сервере, и о доступности сервера он ничего не говорит.
_SSH_OWN = ("ssh:", "ssh_exchange_identification", "kex_exchange_identification",
            "connect to host", "permission denied (publickey",
            "no supported authentication", "host key verification failed",
            "connection closed by remote host", "banner exchange")


def ssh_fail_reason(server, rc, err_text):
    """Объясняет сбой ТОЛЬКО если виноват сам SSH. Три фильтра подряд:
      1) ssh отдаёт 255 на своих ошибках — код команды выглядит иначе;
      2) сообщение должно быть от ssh-клиента, а не от программы на сервере
         (codex/claude тоже пишут «Connection refused», когда не достучались
         до OpenAI/Anthropic — раньше мы принимали это за «VPS выключен»);
      3) прежде чем обвинять сервер — стучимся к нему и проверяем.
    Возвращает подсказку либо None (тогда показываем настоящую ошибку команды)."""
    if isinstance(err_text, bytes):
        err_text = err_text.decode("utf-8", "replace")
    if "__CANCELLED__" in (err_text or ""):
        return "⏹ Остановлено по вашей команде."
    if rc != 255:
        return None
    clean = ssh_clean_err(err_text)
    low = clean.lower()
    if not any(mark in low for mark in _SSH_OWN):
        return None
    hint = ssh_error_hint(clean)
    if hint and hint.startswith("🖥") and ssh_alive(server):
        # сервер отвечает прямо сейчас — значит дело не в нём
        return None
    return hint


def ssh_error_hint(err_text):
    """Превращает сырую SSH-ошибку в понятное объяснение (на чьей стороне беда).
    Возвращает строку-подсказку или None, если это не сетевая проблема SSH."""
    if isinstance(err_text, bytes):
        err_text = err_text.decode("utf-8", "replace")
    if "__CANCELLED__" in (err_text or ""):
        return "⏹ Остановлено по вашей команде."
    # сначала выкидываем служебный шум ssh — он не говорит ничего о сервере
    e = ssh_clean_err(err_text).lower()
    if not e:
        return None
    if "connection refused" in e:
        return ("🖥 Сервер не отвечает по SSH (Connection refused). Он выключен, "
                "перегружен или SSH-порт закрыт — это сторона СЕРВЕРА, не приложения. "
                "Проверьте, что сервер включён и оплачен.")
    if "no route to host" in e:
        return ("🖥 Нет сети до сервера (No route to host) — сервер недоступен. "
                "Это сторона сервера, не приложения.")
    if ("timed out" in e or "timeout" in e) and "connect" in e:
        return ("🖥 Сервер не ответил вовремя (timeout) — перегружен или недоступен. "
                "Это сторона сервера, не приложения.")
    if ("permission denied" in e or "authentication failed" in e
            or "no supported authentication" in e):
        return ("🔑 SSH отказал в доступе (неверный ключ или пароль). Проверьте "
                "данные сервера в разделе «Серверы».")
    if ("connection reset" in e or "connection closed" in e
            or "broken pipe" in e or "kex_exchange" in e):
        return ("🖥 SSH-соединение оборвалось (сервер перезапустился или сеть "
                "моргнула). Обычно помогает повтор.")
    return None


_TAR_EXCLUDES = ["--exclude=.git", "--exclude=node_modules",
                 "--exclude=__pycache__", "--exclude=.venv", "--exclude=.cp_*"]


def _rdir(proj):
    """Путь проекта на сервере пользователя, готовый для вставки в shell.
    $HOME раскрывается шеллом (нельзя брать ~ в кавычки — не раскроется);
    proj прошёл PROJECT_RE, поэтому спецсимволов в нём нет."""
    return '"$HOME/codepocket/%s"' % proj


def remote_workdir(server, proj):
    """Каталог на сервере, где работают терминал и агент. Если у сервера задана
    рабочая папка (режим «как в VS Code») — используем её; иначе старое
    поведение: $HOME/codepocket/<proj>."""
    wd = (server or {}).get("workdir")
    if wd:
        return shlex.quote(wd)
    return _rdir(proj)


def _rel_ok(rel):
    """Безопасный относительный путь внутри рабочей папки (без выхода вверх)."""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    parts = [pp for pp in rel.split("/") if pp not in ("", ".")]
    if any(pp == ".." for pp in parts):
        raise ValueError("недопустимый путь")
    return "/".join(parts)


def _tool_step(name, inp):
    """Превращает вызов инструмента в карточку для приложения — как в VS Code:
    заголовок «что сделал», цель (файл или команда) и место под вывод."""
    inp = inp if isinstance(inp, dict) else {}
    name = str(name or "инструмент")
    path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
    short = os.path.basename(path) if path else ""
    st = {"role": "tool", "tool": name, "title": name, "target": "",
          "body": "", "out": "", "kind": "tool"}
    if name == "Bash":
        st["title"] = inp.get("description") or "Команда"
        st["target"] = (inp.get("command") or "")[:2000]
        st["kind"] = "bash"
    elif name in ("Read", "NotebookRead"):
        st["title"] = "Прочитал"
        st["target"] = short or path
        st["kind"] = "read"
    elif name == "Write":
        st["title"] = "Создал файл"
        st["target"] = short or path
        st["body"] = (inp.get("content") or "")[:4000]
        st["kind"] = "write"
    elif name in ("Edit", "MultiEdit", "NotebookEdit"):
        st["title"] = "Изменил"
        st["target"] = short or path
        st["body"] = json.dumps(
            {"old": (inp.get("old_string") or "")[:2000],
             "new": (inp.get("new_string") or inp.get("new_source") or "")[:2000]},
            ensure_ascii=False)
        st["kind"] = "edit"
    elif name in ("Grep", "Glob"):
        st["title"] = "Поиск"
        st["target"] = str(inp.get("pattern") or inp.get("query") or "")[:300]
        st["kind"] = "search"
    elif name in ("WebFetch", "WebSearch"):
        st["title"] = "Веб"
        st["target"] = str(inp.get("url") or inp.get("query") or "")[:300]
        st["kind"] = "web"
    elif name in ("Task", "Agent"):
        st["title"] = "Подзадача агенту"
        st["target"] = str(inp.get("description") or "")[:300]
        st["kind"] = "task"
    elif name == "TodoWrite":
        st["title"] = "План задач"
        todos = inp.get("todos")
        if isinstance(todos, list):
            st["target"] = " · ".join(str(t.get("content", ""))[:60]
                                      for t in todos[:6] if isinstance(t, dict))[:300]
        st["kind"] = "todo"
    else:
        st["target"] = short or str(inp.get("command") or "")[:200]
    return st


def parse_transcript_lines(text, with_tools=False):
    """Разбирает JSONL-транскрипт Claude/Codex в список реплик [{role,text}].
    Терпимо к формату (message/payload/data), отсекает служебные вставки.
    with_tools=True — добавляет шаги работы агента (какие команды он выполнял,
    какие файлы читал и правил), чтобы показать их так же, как в VS Code."""
    turns = []
    pending = {}        # tool_use_id -> индекс шага, который ждёт свой результат
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("__"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if with_tools:
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            blocks = msg.get("content") if isinstance(msg, dict) else None
            mrole = (msg.get("role") if isinstance(msg, dict) else None) or obj.get("type")
            # Блоки идут в том же порядке, что и в сообщении: сначала пояснение
            # агента, потом действие. Иначе карточка работы встаёт ПЕРЕД текстом,
            # который её объясняет, и лента читается задом наперёд.
            if isinstance(blocks, list) and any(
                    isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                    for b in blocks):
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and mrole in ("assistant", "model"):
                        t = re.sub(r"<system-reminder>.*?</system-reminder>", "",
                                   b.get("text") or "", flags=re.S).strip()
                        if t:
                            turns.append({"role": "assistant", "text": t[:4000]})
                    elif bt == "tool_use":
                        turns.append(_tool_step(b.get("name"), b.get("input")))
                        if b.get("id"):
                            pending[b["id"]] = len(turns) - 1
                    elif bt == "tool_result":
                        idx = pending.pop(b.get("tool_use_id"), None)
                        if idx is None:
                            continue
                        c = b.get("content")
                        if isinstance(c, list):
                            c = "".join(x.get("text", "") for x in c
                                        if isinstance(x, dict))
                        turns[idx]["out"] = str(c or "")[:4000]
                        if b.get("is_error"):
                            turns[idx]["error"] = True
                continue   # эта строка разобрана поблочно
        node = obj
        for key in ("message", "payload", "data"):
            if isinstance(obj.get(key), dict):
                node = obj[key]
                break
        role = (node.get("role") or obj.get("role")
                or node.get("type") or obj.get("type"))
        content = node.get("content")
        if content is None:
            content = node.get("text")
        txt = ""
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    txt += (b.get("text") or b.get("input_text")
                            or b.get("output_text") or "")
                elif isinstance(b, str):
                    txt += b
        # харнесс вставляет <system-reminder>…</system-reminder> в реплики user —
        # это не часть переписки; вырезаем блок, оставляя настоящий текст (если он
        # был рядом). Иначе такой служебный текст становится заголовком чата.
        if "<system-reminder>" in txt:
            txt = re.sub(r"<system-reminder>.*?</system-reminder>", "",
                         txt, flags=re.S)
        if role in ("user", "human"):
            role = "user"
        elif role in ("assistant", "model"):
            role = "assistant"
        else:
            role = None
        if not (role and txt.strip()):
            continue
        if ("<environment_context>" in txt or "<recommended_plugins>" in txt
                or "<user_instructions>" in txt or "</cwd>" in txt):
            continue
        t = txt.strip()[:4000]
        if (turns and turns[-1].get("role") == role
                and turns[-1].get("text") == t):
            continue
        turns.append({"role": role, "text": t})
    return turns


def claude_stream_handler(user):
    """Разбирает поток событий `claude --output-format stream-json` на лету и
    складывает их в живой прогресс: что агент говорит, что запускает, что
    получил в ответ. Возвращает (обработчик_строки, состояние)."""
    state = {"answer": None, "sid": None}

    def on_line(raw):
        try:
            ev = json.loads(raw.decode("utf-8", "replace").strip() or "{}")
        except (ValueError, json.JSONDecodeError):
            return
        t = ev.get("type")
        if t == "system":
            progress_note(user, "готовлю окружение…")
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    progress_add(user, {"kind": "say", "title": "",
                                        "text": b["text"][:1500]})
                elif b.get("type") == "tool_use":
                    st = _tool_step(b.get("name"), b.get("input"))
                    st["key"] = b.get("id") or ""
                    progress_add(user, st)
                    progress_note(user, st["title"] +
                                  ((" · " + st["target"][:70]) if st["target"] else ""))
        elif t == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "".join(x.get("text", "") for x in c
                                    if isinstance(x, dict))
                    progress_patch(user, b.get("tool_use_id"),
                                   {"out": str(c or "")[:3000],
                                    "error": bool(b.get("is_error"))})
        elif t == "result":
            state["answer"] = ev.get("result") or ""
            state["sid"] = ev.get("session_id") or state["sid"]
            progress_note(user, "")

    return on_line, state


def codex_stream_handler(user):
    """То же для `codex exec --json`: у него события в поле type/payload."""
    state = {"answer": None, "sid": None}
    seq = {"n": 0}

    def on_line(raw):
        try:
            ev = json.loads(raw.decode("utf-8", "replace").strip() or "{}")
        except (ValueError, json.JSONDecodeError):
            return
        pl = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
        t = pl.get("type") or ev.get("type")
        if t == "session_meta" or ev.get("session_id"):
            state["sid"] = (pl.get("session_id") or ev.get("session_id")
                            or state["sid"])
        if t in ("exec_command_begin", "command_begin"):
            cmd = pl.get("command")
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            seq["n"] += 1
            st = _tool_step("Bash", {"command": str(cmd or "")})
            st["key"] = str(pl.get("call_id") or pl.get("id") or seq["n"])
            progress_add(user, st)
            progress_note(user, "Команда · " + str(cmd or "")[:70])
        elif t in ("exec_command_end", "command_end"):
            out = pl.get("stdout") or pl.get("output") or ""
            progress_patch(user, str(pl.get("call_id") or pl.get("id") or seq["n"]),
                           {"out": str(out)[:3000],
                            "error": bool(pl.get("exit_code"))})
        elif t in ("patch_apply_begin", "apply_patch"):
            st = _tool_step("Edit", {"file_path": str(pl.get("path") or "")})
            progress_add(user, st)
            progress_note(user, "Правка файла")
        elif t == "agent_message":
            msg = pl.get("message") or pl.get("text") or ""
            if str(msg).strip():
                progress_add(user, {"kind": "say", "title": "",
                                    "text": str(msg)[:1500]})
                state["answer"] = str(msg)
        elif t == "agent_reasoning":
            txt = str(pl.get("text") or "")[:300]
            if txt.strip():
                progress_note(user, txt.replace("\n", " ")[:110])
        elif t == "task_complete":
            last = pl.get("last_agent_message")
            if last:
                state["answer"] = str(last)
            err = pl.get("error") or {}
            if isinstance(err, dict) and err.get("message"):
                state["error"] = str(err["message"])[:400]
            progress_note(user, "")

    return on_line, state


# Готовим PATH так, чтобы найти claude/codex где бы их ни поставили: nvm (как это
# делает VS Code), npm -g, ~/.local/bin, /usr/local/bin. Иначе в неинтерактивной
# SSH-сессии команды «не видны» и приложение думает, что их нет.
REMOTE_PATH_PREP = (
    '[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1; '
    'export PATH="$HOME/.local/bin:$(npm prefix -g 2>/dev/null)/bin:'
    '/usr/local/bin:/usr/bin:/bin:$PATH"; '
    # если codex/claude всё ещё не на PATH — ищем бинарник где угодно (nvm, npm -g,
    # папки VS Code) и добавляем его каталог в PATH. Так приложение использует то,
    # что уже стоит на сервере, без переустановки.
    'for _b in codex claude; do command -v "$_b" >/dev/null 2>&1 || { '
    '_p=$(find "$HOME/.nvm" "$HOME/.local" "$HOME/.npm-global" /usr/local/lib '
    '/usr/lib/node_modules "$HOME/.vscode-server" "$HOME/.cursor-server" '
    '-maxdepth 7 -type f -name "$_b" 2>/dev/null | head -1); '
    '[ -n "$_p" ] && export PATH="$(dirname "$_p"):$PATH"; }; done; '
    # Прокси пользователя. Обычно он прописан в ~/.bashrc, а .bashrc читает только
    # ИНТЕРАКТИВНЫЙ шелл. Поэтому в терминале VS Code агент прокси видит и работает,
    # а из приложения (bash -lc, неинтерактивный) — нет, и упирается в «не могу
    # подключиться к OpenAI/Anthropic». Вытаскиваем эти переменные явно.
    'for _f in /etc/environment "$HOME/.profile" "$HOME/.bash_profile" '
    '"$HOME/.bashrc"; do [ -r "$_f" ] || continue; '
    '_e=$(grep -hE \'^[[:space:]]*(export[[:space:]]+)?'
    '(http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY|all_proxy|ALL_PROXY|'
    'no_proxy|NO_PROXY|GLOBAL_AGENT_HTTP_PROXY)=\' "$_f" 2>/dev/null '
    '| sed \'s/^[[:space:]]*//; s/^export[[:space:]]*//; s/^/export /\'); '
    '[ -n "$_e" ] && eval "$_e"; done; unset _f _e _b _p; true; '
)


def remote_push_project(server, proj, local_dir):
    """Заливаем текущее состояние проекта на сервер пользователя (чистой копией)."""
    tar = subprocess.run(["tar", "czf", "-", "-C", local_dir] + _TAR_EXCLUDES + ["."],
                         capture_output=True).stdout
    rdir = _rdir(proj)
    # накладываем актуальные файлы редактора поверх (без rm -rf — чтобы не
    # стирать то, что агент поставил сам: node_modules, venv, сборки)
    remote = "mkdir -p %s && tar xzf - -C %s" % (rdir, rdir)
    rc, out, err = ssh_exec(server, "bash -lc " + shlex.quote(remote),
                            stdin_bytes=tar, timeout=180)
    return rc == 0


def remote_pull_project(server, proj, local_dir, uid, gid):
    """Забираем изменённые агентом файлы обратно в проект на нашем сервере."""
    rdir = _rdir(proj)
    remote = ("cd %s 2>/dev/null && tar czf - %s . 2>/dev/null"
              % (rdir, " ".join(_TAR_EXCLUDES)))
    rc, out, err = ssh_exec(server, "bash -lc " + shlex.quote(remote), timeout=180)
    if not out:
        return
    try:
        # Архив приходит с чужой машины, поэтому распаковываем его максимально
        # недоверчиво: без абсолютных путей и «..», не наследуя владельца и
        # права из архива, и не следуя за символьными ссылками наружу.
        subprocess.run(["tar", "xzf", "-", "-C", local_dir,
                        "--no-absolute-names", "--no-same-owner",
                        "--no-same-permissions", "--no-overwrite-dir",
                        "--exclude=../*", "--exclude=/*"],
                       input=out, capture_output=True, timeout=60)
        subprocess.run(["chown", "-RhP", "%d:%d" % (uid, gid), local_dir],
                       capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        pass


def changed_files(proj_dir, since):
    """Файлы проекта, изменённые после времени since (для карточек в чате)."""
    out = []
    skip = {".git", "node_modules", "__pycache__", ".venv"}
    for root, dirs, files in os.walk(proj_dir):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if fn.startswith(".cp_"):
                continue
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
                if st.st_mtime >= since:
                    out.append({"rel": os.path.relpath(fp, proj_dir),
                                "size": st.st_size})
            except OSError:
                pass
        if len(out) > 12:
            break
    out.sort(key=lambda x: x["rel"])
    return out[:12]


def run_claude_remote(server, proj, message, session_id=None, api_key=""):
    """Запускает `claude -p` в проекте на сервере пользователя.
    Если задан api_key — работает по API-ключу, иначе по подписке (вход в CLI).
    Возвращает (текст_ответа, новый_session_id, ошибка_или_None)."""
    rdir = remote_workdir(server, proj)
    keyenv = ("ANTHROPIC_API_KEY=" + shlex.quote(api_key) + " ") if api_key else ""
    # IS_SANDBOX=1 снимает запрет bypass-разрешений под root (сервер пользователя
    # — управляемая им среда). Без него claude под root отказывается работать.
    # stream-json: claude присылает события по мере работы (что запускает, что
    # прочитал, что ответил) — из них строится живой прогресс в приложении.
    base = (REMOTE_PATH_PREP + 'cd %s && '
            '%sIS_SANDBOX=1 claude -p %s --permission-mode bypassPermissions '
            '--output-format stream-json --verbose'
            % (rdir, keyenv, shlex.quote(message)))

    # В режиме рабочей папки продолжаем ПОСЛЕДНЮЮ беседу в этой папке (--continue) —
    # ту же, что ведётся в VS Code / терминале на сервере. Так переписка единая
    # между устройствами. Вне режима папки — по нашей сохранённой сессии (--resume).
    folder = bool((server or {}).get("workdir"))

    user = getattr(_cur_user, "name", None) or "-"
    on_line, st = claude_stream_handler(user)

    def _run(extra):
        cmd = base + extra
        return ssh_exec(server, "bash -lc " + shlex.quote(cmd), timeout=600,
                        on_line=on_line)

    if session_id:
        # выбранная списком (или ранее запомненная) беседа — продолжаем именно её,
        # это важнее авто-подбора --continue (та берёт последнюю в папке и замыкается
        # на наше же сообщение)
        rc, out, err = _run(" --resume " + shlex.quote(session_id))
        if rc != 0:                       # сессия протухла — откат на последнюю в папке / новую
            rc, out, err = _run(" --continue" if folder else "")
            if rc != 0 and folder:
                rc, out, err = _run("")
    elif folder:
        rc, out, err = _run(" --continue")
        if rc != 0:                       # прошлой беседы в папке ещё нет — начнём новую
            rc, out, err = _run("")
    else:
        rc, out, err = _run("")
    text = out.decode("utf-8", "replace").strip()
    if rc != 0 and not text:
        e = ssh_clean_err(err)[:400]   # без служебных строк ssh
        hint = ssh_fail_reason(server, rc, e)   # виноват ли САМ ssh (а не claude)
        if hint:
            return None, None, hint
        if "command not found" in e or "claude:" in e:
            return None, None, "на сервере не установлен claude (поставьте: claude в терминале и войдите)"
        return None, None, e or "claude завершился с ошибкой на вашем сервере"
    # ответ уже собран из потока событий
    if st["answer"] is not None:
        return (st["answer"] or "(пустой ответ)"), (st["sid"] or session_id), None
    # запасной путь: последняя строка-событие или обычный JSON
    for ln in reversed(text.splitlines()):
        try:
            obj = json.loads(ln)
        except (ValueError, json.JSONDecodeError):
            continue
        ans = obj.get("result") or obj.get("assistant_message")
        if ans:
            return ans, (obj.get("session_id") or session_id), None
    return (text or "(пустой ответ)"), session_id, None


def _clean_codex_output(text):
    """Убирает служебный баннер `codex exec` (workdir/model/session id …) и эхо
    запроса, оставляя только сам ответ агента. Если формат неожиданный —
    возвращает как есть (безопасный откат)."""
    lines = text.splitlines()
    # баннер завершается второй строкой из дефисов (--------)
    dash = [i for i, ln in enumerate(lines) if ln.strip().startswith("----")]
    if len(dash) >= 2:
        lines = lines[dash[1] + 1:]
    # эхо запроса (user + сам текст) завершается ISO-таймстампом — ответ идёт после
    ts = next((i for i, ln in enumerate(lines)
               if re.match(r'^\s*\d{4}-\d\d-\d\dT[\d:.]+Z\s*$', ln)), None)
    if ts is not None:
        lines = lines[ts + 1:]
    return "\n".join(lines).strip()


CODEX_SID_RE = re.compile(r'session[ _]?id\s*[:=]\s*([0-9a-fA-F-]{36})')

# uuid в имени файла сессии: у Codex это rollout-<ISO>-<uuid>.jsonl, у Claude —
# просто <uuid>.jsonl. Достаём id, чтобы продолжать ровно эту беседу.
_UUID_RE = re.compile(
    r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')


def session_id_from_file(fpath):
    m = _UUID_RE.search(os.path.basename(fpath or ""))
    return m.group(1) if m else None


def run_codex_remote(server, proj, message, session_id=None, api_key=""):
    """Запускает `codex exec` в проекте на сервере пользователя.
    Если задан api_key — по ключу OpenAI, иначе по подписке ChatGPT.
    Возвращает (текст, новый_session_id, ошибка_или_None)."""
    rdir = remote_workdir(server, proj)
    keyenv = (("OPENAI_API_KEY=%s CODEX_API_KEY=%s " %
               (shlex.quote(api_key), shlex.quote(api_key))) if api_key else "")
    # danger-full-access — без песочницы (workspace-write под root не даёт запись).
    # Ограничение — cd в папку проекта + суть задачи; риск как у Claude bypass.
    # --json: codex печатает события построчно — показываем их вживую
    base = (REMOTE_PATH_PREP + 'cd %s && '
            '%scodex exec --json --sandbox danger-full-access --skip-git-repo-check'
            % (rdir, keyenv))

    # Продолжаем ТУ ЖЕ сессию Codex, а не заводим новую на каждое сообщение —
    # иначе написанное здесь уходит в отдельный rollout и не видно в VS Code.
    #
    # `resume --last` для этого не годится: он берёт самую свежую сессию вообще,
    # без привязки к папке, — на живом тесте так и вышло (ответ ушёл в чужой
    # rollout, в VS Code его не видно). Поэтому ищем id явно: среди транскриптов
    # ~/.codex/sessions берём те, где встречается путь нашей рабочей папки, и из
    # них самый недавно изменённый — это и есть беседа, которую сейчас ведут в
    # VS Code. id лежит в имени файла: rollout-<ISO>-<uuid>.jsonl.
    # Тот же приём, что --continue у Claude. Вне режима папки — по сохранённому id.
    wd = (server or {}).get("workdir")
    folder = bool(wd)
    discover = ""
    if session_id:
        # Пользователь выбрал конкретную беседу списком (или мы её уже запомнили) —
        # продолжаем ИМЕННО её. Это главнее авто-подбора по папке: сам подбор
        # «самой свежей сессии папки» замыкается на себя (наше же сообщение делает
        # сессию приложения самой свежей), поэтому нужен явно закреплённый id.
        resume_arg = " resume " + shlex.quote(session_id)
    elif folder:
        # Явно закреплённой сессии ещё нет (первое открытие папки) — как стартовое
        # значение берём последнюю сессию этой папки по совпадению cwd. Дальше id
        # запоминается в chat["remote_session"] и приходит сюда как session_id.
        needle = shlex.quote('"' + wd + '"')
        discover = (
            'SID=$(grep -rlF %s "$HOME/.codex/sessions" '
            '--include="rollout-*.jsonl" 2>/dev/null '
            '| xargs -r ls -1t 2>/dev/null | head -1 '
            '| sed -n "s/.*rollout-.*-\\([0-9a-fA-F-]\\{36\\}\\)\\.jsonl$/\\1/p"); '
            % needle)
        # ${SID:+...} — если ничего не нашли, сессии в этой папке ещё нет,
        # codex просто начнёт новую (первое сообщение в проекте)
        resume_arg = ' ${SID:+resume "$SID"}'
    else:
        resume_arg = ""

    user = getattr(_cur_user, "name", None) or "-"
    on_line, cst = codex_stream_handler(user)

    def _run(extra, pre=""):
        cmd = pre + base + extra + " " + shlex.quote(message)
        return ssh_exec(server, "bash -lc " + shlex.quote(cmd), timeout=600,
                        on_line=on_line)

    rc, out, err = _run(resume_arg, discover)
    # продолжать нечего (первое сообщение в папке) или сессия протухла — начнём новую
    if rc != 0 and resume_arg and not out.strip():
        rc, out, err = _run("")
    text = out.decode("utf-8", "replace").strip()
    errtext = err.decode("utf-8", "replace").strip()
    # id сессии codex печатает в баннере `codex exec` — запоминаем его, чтобы
    # продолжать эту же беседу и вне режима рабочей папки
    m = CODEX_SID_RE.search(text) or CODEX_SID_RE.search(errtext)
    sid = cst.get("sid") or (m.group(1) if m else session_id)
    # ошибка, о которой codex сообщил сам в событии task_complete
    if cst.get("error") and not cst.get("answer"):
        return None, sid, cst["error"]
    combined = text + "\n" + errtext
    # 1) СНАЧАЛА проверяем, не отвалился ли сам SSH (сервер недоступен) — иначе
    # эту ошибку легко перепутать с «codex не установлен» или ошибкой агента.
    if rc != 0 and not text:
        hint = ssh_fail_reason(server, rc, errtext)
        if hint:
            return None, sid, hint
    # Codex по подписке иногда не поднимает свой вебсокет-эндпоинт (responses_
    # websocket). Сеть при этом жива (обычный API отвечает). Даём понятную
    # подсказку вместо немого баннера без ответа.
    if not api_key and ("responses_websocket" in combined
                        or "failed to connect to websocket" in combined):
        return None, sid, ("Codex не смог подключиться к OpenAI по подписке "
                      "(вебсокет). Впишите API-ключ OpenAI в окне ИИ — тогда "
                      "Codex пойдёт по обычному API, как Claude.")
    # ключ принят, но на аккаунте OpenAI нет баланса (предоплатный API)
    low = combined.lower()
    if ("quota exceeded" in low or "insufficient_quota" in low
            or "check your plan and billing" in low):
        return None, sid, ("Ключ OpenAI принят, авторизация прошла — но на аккаунте "
                      "нет доступного баланса (Quota exceeded). Пополните счёт: "
                      "platform.openai.com → Settings → Billing, и Codex "
                      "заработает без других настроек.")
    if rc != 0 and not text:
        e = ssh_clean_err(errtext)[:400]
        if "not found" in e and "codex" in e:
            return None, sid, "на сервере не установлен codex (поставьте: npm i -g @openai/codex и войдите)"
        return None, sid, e or "codex завершился с ошибкой на вашем сервере"
    # с --json ответ уже собран из потока событий; иначе чистим обычный вывод
    answer = cst.get("answer") or _clean_codex_output(text)
    # ответа нет, но и явной ошибки нет — почти всегда это тот же обрыв вебсокета
    if not answer and not api_key:
        return None, sid, ("Codex по подписке не вернул ответ (обычно это обрыв "
                      "вебсокета OpenAI). Впишите API-ключ OpenAI в окне ИИ — "
                      "и Codex заработает надёжно, как Claude.")
    return (answer or text or "(пустой ответ)"), sid, None


# ----------------------------------------------------------------------------
# автоустановка Claude Code и Codex на сервере пользователя
# ----------------------------------------------------------------------------
AI_INSTALL_SCRIPT = r"""set +e
echo '=== CodePocket: установка Claude Code и Codex ==='
have_node=0
if command -v node >/dev/null 2>&1; then
  MAJ=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
  if [ "${MAJ:-0}" -ge 18 ] 2>/dev/null; then have_node=1; echo "[node] уже установлен $(node -v)"; fi
fi
if [ "$have_node" = 0 ]; then
  echo '[node] устанавливаю Node.js 20 LTS...'
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs
  elif command -v dnf >/dev/null 2>&1; then
    curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - && dnf install -y nodejs
  elif command -v yum >/dev/null 2>&1; then
    curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - && yum install -y nodejs
  else
    export NVM_DIR="$HOME/.nvm"; mkdir -p "$NVM_DIR"
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
    . "$NVM_DIR/nvm.sh" && nvm install 20 && nvm use 20
  fi
fi
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"
if ! command -v node >/dev/null 2>&1; then
  echo 'ОШИБКА: Node.js установить не удалось (нужен ли доступ root на сервере?)'
  echo CLAUDE_FAIL; echo CODEX_FAIL; exit 0
fi
echo "[node] node $(node -v), npm $(npm -v)"
echo '[claude] npm install -g @anthropic-ai/claude-code ...'
npm install -g @anthropic-ai/claude-code 2>&1 | tail -4
echo '[codex] npm install -g @openai/codex ...'
npm install -g @openai/codex 2>&1 | tail -4
export PATH="$(npm prefix -g 2>/dev/null)/bin:$HOME/.local/bin:$PATH"; hash -r 2>/dev/null
echo '=== ИТОГ ==='
if command -v claude >/dev/null 2>&1; then echo "CLAUDE_OK $(claude --version 2>/dev/null | head -1)"; else echo CLAUDE_FAIL; fi
if command -v codex >/dev/null 2>&1; then echo "CODEX_OK $(codex --version 2>/dev/null | head -1)"; else echo CODEX_FAIL; fi
"""


def install_ai_on_server(server):
    """Ставит Node.js + Claude Code + Codex на сервере пользователя по SSH.
    Возвращает (лог, {'claude': bool, 'codex': bool})."""
    rc, out, err = ssh_exec(server, "bash -lc " + shlex.quote(AI_INSTALL_SCRIPT),
                            timeout=600)
    log = out.decode("utf-8", "replace")
    e = err.decode("utf-8", "replace").strip()
    if e:
        log += "\n" + e
    installed = {"claude": "CLAUDE_OK" in log, "codex": "CODEX_OK" in log}
    return log.strip()[-6000:], installed


def verify_telegram_init_data(init_data, max_age=86400):
    """Совместимая обёртка: только пользователь или None."""
    user, _ = check_telegram_init_data(init_data, max_age)
    return user


def check_telegram_init_data(init_data, max_age=86400):
    """Проверяет подпись Telegram WebApp initData (по документации Telegram).

    Возвращает (пользователь, причина_отказа). Причина нужна, чтобы неудачный
    вход было видно в аудит-логе: раньше любой отказ выглядел одинаково —
    «не удалось проверить подпись» — и понять, дело в токене бота, в протухших
    данных или в подписи, было нельзя.

    Возраст данных мерим по auth_date. Telegram отдаёт initData один раз при
    открытии, а WebView может держать страницу сутками и переиспользовать
    старое значение — такие попытки честно отбиваем как stale."""
    if not TG_BOT_TOKEN:
        return None, "нет TG_BOT_TOKEN на сервере"
    if not init_data:
        return None, "пустой initData"
    try:
        from urllib.parse import parse_qsl
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None, "initData не разбирается"
    their_hash = data.pop("hash", None)
    if not their_hash:
        return None, "в initData нет hash"
    # signature — поле сторонней валидации Telegram, в подписи бота не участвует
    data.pop("signature", None)
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    age = int(time.time() - auth_date) if auth_date else -1
    if max_age and auth_date and age > max_age:
        return None, "данные устарели (%d ч)" % (age // 3600)
    check_string = "\n".join("%s=%s" % (k, data[k]) for k in sorted(data))
    secret = hmac.new(b"WebAppData", TG_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, their_hash):
        return None, "подпись не сошлась (проверьте, что TG_BOT_TOKEN от того же бота)"
    try:
        user = json.loads(data.get("user", "") or "{}")
    except (ValueError, json.JSONDecodeError):
        user = {}
    if not user.get("id"):
        return None, "в initData нет пользователя"
    return user, None


def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, 0x5414, struct.pack("HHHH", rows, cols, 0, 0))  # TIOCSWINSZ


# ----------------------------------------------------------------------------
# HTTP-обработчик
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-ide"

    # --- утилиты ответов ---
    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        # Телефон часто засыпает/теряет сеть, пока мы ждём ответа агента (до 600 с).
        # К моменту ответа сокет уже закрыт — write падает с BrokenPipe и роняет
        # обработчик ПОСЛЕ того, как ответ уже сохранён в историю чата. Гасим тихо:
        # ошибка всё равно записана в chat["ui"], клиент увидит её при перезагрузке.
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        # Content-Length приходит от клиента: без нижней границы "-1" превращал
        # read() в чтение до конца соединения, то есть в способ съесть память
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("плохой Content-Length")
        if n < 0:
            raise ValueError("плохой Content-Length")
        if n > MAX_FILE_SIZE + 4096:
            raise ValueError("слишком большое тело запроса")
        return self.rfile.read(n)

    def _need_user(self):
        u = session_user(self)
        if not u:
            self._err("нужна авторизация", 401)
            return None
        return u

    def log_message(self, fmt, *args):
        pass  # не спамим в journald каждым запросом

    # --- статика ---
    def _serve_static(self, relpath):
        path = os.path.realpath(os.path.join(STATIC_DIR, relpath.lstrip("/")))
        root = os.path.realpath(STATIC_DIR)
        # сравниваем с разделителем: иначе каталог-сосед вида static-old
        # проходит проверку по префиксу
        if (path != root and not path.startswith(root + os.sep)) \
                or not os.path.isfile(path):
            self._err("не найдено", 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8", ".js": "application/javascript",
            ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml",
            ".woff2": "font/woff2", ".map": "application/json",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # HTML не кэшируем — чтобы обновления интерфейса были видны сразу;
        # тяжёлые библиотеки (vendor) кэшируем надолго.
        if path.endswith(".html"):
            # никакого кэша для HTML — iOS Safari иначе показывает старый интерфейс
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # --- GET ---
    def do_GET(self):
        url = urlparse(self.path)
        p = url.path

        if p in ("", "/", "/index.html"):
            # Отмечаем сам факт загрузки страницы. Нужно, чтобы отличать
            # «приложение не работает» от «клиент показывает страницу из
            # своего кэша и на сервер не ходит вовсе»: во втором случае
            # записи просто не будет. Одна строка на открытие — не шумно.
            audit("page_open", ip=self._client_ip(),
                  ua=(self.headers.get("User-Agent") or "")[:90],
                  q=(url.query or "")[:40])
            return self._serve_static("index.html")
        if p.startswith("/vendor/"):
            return self._serve_static(p)

        if p == "/ws/term":
            return self._handle_ws()

        if p == "/api/me":
            u = session_user(self)
            role = None
            avatar = ""
            email = ""
            if u:
                with _lock:
                    db = db_load()
                    role = user_role(db, u)
                    urec = db["users"].get(u) or {}
                    avatar = urec.get("avatar", "")
                    email = urec.get("email", "")
            return self._json({"user": u, "role": role, "avatar": avatar,
                               "email": email,
                               "claude": bool(ANTHROPIC_API_KEY),
                               "open_signup": OPEN_SIGNUP})

        user = self._need_user()
        if not user:
            return
        _, _, home = get_uid_gid_home(user)
        q = parse_qs(url.query)
        rel = (q.get("path") or [""])[0]

        if p == "/api/aistatus":
            # статус входа Claude/Codex: есть ли ключ + вошёл ли по подписке на сервере
            with _lock:
                db = db_load()
                urec = db["users"].get(user) or {}
            out = {"claude_key": bool(urec.get("ai_claude_key")),
                   "codex_key": bool(urec.get("ai_codex_key")),
                   "claude_sub": None, "codex_sub": None,
                   "node": None, "claude_installed": None,
                   "codex_installed": None}
            srv = get_server(user, (q.get("server") or [""])[0])
            if srv:
                chk = (REMOTE_PATH_PREP +
                       'command -v node >/dev/null 2>&1 && echo N; '
                       'command -v claude >/dev/null 2>&1 && echo CI; '
                       'command -v codex >/dev/null 2>&1 && echo XI; '
                       'test -s "$HOME/.claude/.credentials.json" && echo C; '
                       'test -s "$HOME/.codex/auth.json" && echo X')
                rc, o, e = ssh_exec(srv, "bash -lc " + shlex.quote(chk), timeout=25)
                # разбираем по отдельным токенам (строкам), чтобы «C» не совпало с «CI»
                toks = set(o.decode("utf-8", "replace").split())
                out["node"] = "N" in toks
                out["claude_installed"] = "CI" in toks
                out["codex_installed"] = "XI" in toks
                out["claude_sub"] = "C" in toks
                out["codex_sub"] = "X" in toks
            return self._json(out)

        if p == "/api/projects":
            d = ensure_projects_dir(user)
            items = []
            for name in sorted(os.listdir(d)):
                full = os.path.join(d, name)
                if os.path.isdir(full):
                    items.append({"name": name,
                                  "mtime": int(os.path.getmtime(full))})
            items.sort(key=lambda x: -x["mtime"])
            return self._json({"projects": items,
                               "templates": list(TEMPLATES.keys())})

        if p == "/api/usage":
            with _lock:
                db = db_load()
                used, quota = quota_state(db, user)
                role = user_role(db, user)
                db_save(db)
            return self._json({"used": used, "quota": quota, "role": role,
                               "model": agent_mod.MODEL,
                               "claude": bool(ANTHROPIC_API_KEY)})

        if p == "/api/search":
            proj = (q.get("project") or [""])[0]
            query = (q.get("q") or [""])[0]
            if not PROJECT_RE.match(proj):
                return self._err("плохое имя проекта")
            if len(query) < 2:
                return self._err("запрос слишком короткий (минимум 2 символа)")
            base = os.path.join(projects_dir(home), proj)
            if not os.path.isdir(base):
                return self._err("проект не найден", 404)
            results = []
            ql = query.lower()
            hit = 0
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in
                           (".git", "__pycache__", "node_modules", ".venv")]
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    rp = os.path.relpath(fp, base)
                    try:
                        if os.path.getsize(fp) > 1024 * 1024:
                            continue
                        with open(fp, "rb") as f:
                            raw = f.read()
                        if b"\x00" in raw[:2048]:
                            continue
                        text = raw.decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    for i, ln in enumerate(text.splitlines(), 1):
                        if ql in ln.lower():
                            results.append({"file": rp, "line": i,
                                            "text": ln.strip()[:200]})
                            hit += 1
                            if hit >= 200:
                                break
                    if hit >= 200:
                        break
                if hit >= 200:
                    break
            return self._json({"results": results, "truncated": hit >= 200})

        if p == "/api/history":
            # список версий файла
            try:
                safe_path(home, rel)
            except PermissionError as e:
                return self._err(str(e), 403)
            return self._json({"versions": history_list(user, rel)})

        if p == "/api/history/get":
            ts = (q.get("ts") or ["0"])[0]
            try:
                safe_path(home, rel)
            except PermissionError as e:
                return self._err(str(e), 403)
            data = history_get(user, rel, ts)
            if data is None:
                return self._err("версия не найдена", 404)
            return self._json({"content": data.decode("utf-8", errors="replace")})

        if p == "/api/servers":
            with _lock:
                db = db_load()
                servers = [server_public(s) for s in user_servers(db, user)]
            return self._json({"servers": servers})

        if p == "/api/sessions":
            cur = self._cookie_token()
            with _lock:
                db = db_load()
                out = []
                for tok, s in db["sessions"].items():
                    if s.get("user") != user:
                        continue
                    out.append({"device": s.get("device") or "устройство",
                                "ip": s.get("ip", ""),
                                "ts": s.get("ts", 0),
                                "current": tok == cur})
                out.sort(key=lambda x: -x["ts"])
                logins = list((db["users"].get(user) or {}).get("logins", []))
            logins.sort(key=lambda x: -x.get("ts", 0))
            return self._json({"active": out, "history": logins})

        # ---- админ-панель (только для роли dev) ----
        if p.startswith("/api/admin/"):
            with _lock:
                if user_role(db_load(), user) != "dev":
                    return self._err("нужны права разработчика", 403)

            if p == "/api/admin/users":
                today = time.strftime("%Y-%m-%d")
                with _lock:
                    db = db_load()
                    # активные сессии по пользователям
                    sess_by_user = {}
                    for s in db["sessions"].values():
                        sess_by_user.setdefault(s.get("user"), 0)
                        sess_by_user[s.get("user")] += 1
                    users = []
                    for name, u in db["users"].items():
                        usage = u.get("usage") or {}
                        tokens = usage.get("tokens", 0) if usage.get("date") == today else 0
                        logins = u.get("logins") or []
                        last = logins[-1] if logins else {}
                        try:
                            import pwd
                            home = pwd.getpwnam(name).pw_dir
                            pdir = os.path.join(home, "projects")
                            nproj = len([d for d in os.listdir(pdir)
                                         if os.path.isdir(os.path.join(pdir, d))]) \
                                if os.path.isdir(pdir) else 0
                        except (KeyError, OSError):
                            nproj = 0
                        res = user_resources(name)
                        users.append({
                            "name": name, "role": u.get("role", "user"),
                            "created": u.get("created", 0),
                            "tokens_today": tokens,
                            "projects": nproj,
                            "servers": len(u.get("servers", [])),
                            "sessions": sess_by_user.get(name, 0),
                            "last_login": last.get("ts", 0),
                            "last_device": last.get("device", ""),
                            "last_ip": last.get("ip", ""),
                            "locked": u.get("locked_until", 0) > int(time.time()),
                            "ram_mb": res["ram_mb"], "disk_mb": res["disk_mb"],
                            "procs": res["procs"],
                        })
                    users.sort(key=lambda x: -x["last_login"])
                return self._json({"users": users, "me": user,
                                   "limits": limits_load()})

            if p == "/api/admin/limits":
                return self._json({"limits": limits_load(),
                                   "cgroup": systemd_scoping_works()})

            if p == "/api/admin/geoip":
                ip = (q.get("ip") or [""])[0].strip()
                return self._json({"ip": ip, "geo": geo_lookup(ip)})

            if p == "/api/admin/sessions":
                with _lock:
                    db = db_load()
                    out = [{"user": s.get("user"), "device": s.get("device", ""),
                            "ip": s.get("ip", ""), "ts": s.get("ts", 0)}
                           for s in db["sessions"].values()]
                out.sort(key=lambda x: -x["ts"])
                return self._json({"sessions": out})

            if p == "/api/admin/serverinfo":
                st = os.statvfs("/")
                disk_total = st.f_blocks * st.f_frsize
                disk_free = st.f_bavail * st.f_frsize
                disk_used = disk_total - disk_free
                mem = {}
                try:
                    with open("/proc/meminfo") as f:
                        for ln in f:
                            k, _, v = ln.partition(":")
                            mem[k] = int(v.strip().split()[0]) * 1024
                except OSError:
                    pass
                mem_total = mem.get("MemTotal", 0)
                mem_used = mem_total - mem.get("MemAvailable", 0)
                swap_total = mem.get("SwapTotal", 0)
                swap_used = swap_total - mem.get("SwapFree", 0)
                with _lock:
                    db = db_load()
                    tu, ts = len(db["users"]), len(db["sessions"])
                return self._json({
                    "disk_total": disk_total, "disk_used": disk_used,
                    "mem_total": mem_total, "mem_used": mem_used,
                    "swap_total": swap_total, "swap_used": swap_used,
                    "total_users": tu, "total_sessions": ts})

            return self._err("не найдено", 404)

        if p == "/api/download":
            proj = (q.get("project") or [""])[0]
            if not PROJECT_RE.match(proj):
                return self._err("плохое имя проекта")
            base = os.path.join(projects_dir(home), proj)
            if not os.path.isdir(base):
                return self._err("проект не найден", 404)
            buf = io.BytesIO()
            total = 0
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for root, dirs, files in os.walk(base):
                    dirs[:] = [d for d in dirs if d not in
                               (".git", "__pycache__", "node_modules", ".venv")]
                    for fn in files:
                        fp = os.path.join(root, fn)
                        try:
                            total += os.path.getsize(fp)
                            if total > 50 * 1024 * 1024:
                                return self._err("проект больше 50 МБ", 413)
                            z.write(fp, os.path.join(proj, os.path.relpath(fp, base)))
                        except OSError:
                            pass
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{proj}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/chat-progress":   # что агент делает прямо сейчас
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            with _agent_busy_lock:
                busy = user in _agent_busy
            data = progress_get(user, since)
            data["busy"] = busy
            return self._json(data)

        if p == "/api/chat":
            proj = (q.get("project") or [""])[0]
            agent = (q.get("agent") or ["claude"])[0]
            if agent not in ("claude", "codex"):
                agent = "claude"
            if not PROJECT_RE.match(proj):
                return self._err("плохое имя проекта")
            data = chat_load(user, proj, agent)
            with _agent_busy_lock:
                busy = user in _agent_busy
            return self._json({"ui": data["ui"], "busy": busy})

        try:
            target = safe_path(home, rel)
        except PermissionError as e:
            return self._err(str(e), 403)

        if p == "/api/tree":
            if not os.path.isdir(target):
                return self._err("не папка", 400)
            items = []
            try:
                for name in sorted(os.listdir(target)):
                    full = os.path.join(target, name)
                    items.append({
                        "name": name,
                        "dir": os.path.isdir(full),
                        "path": os.path.relpath(full, home),
                    })
            except OSError as e:
                return self._err(str(e), 500)
            items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
            return self._json({"items": items})

        if p == "/api/file":
            if not os.path.isfile(target):
                return self._err("файл не найден", 404)
            if os.path.getsize(target) > MAX_FILE_SIZE:
                return self._err("файл больше 2 МБ — откройте его в терминале", 413)
            with open(target, "rb") as f:
                raw = f.read()
            if b"\x00" in raw[:8192]:
                return self._err("бинарный файл — в редакторе не открыть", 415)
            return self._json({"content": raw.decode("utf-8", errors="replace")})

        return self._err("не найдено", 404)

    # --- POST/PUT ---
    def do_POST(self):
        url = urlparse(self.path)
        p = url.path

        # Все POST меняют состояние. SameSite=Lax уже отсекает межсайтовую
        # отправку кук, проверка Origin — второй рубеж на тот же случай.
        if not self._origin_ok():
            return self._err("запрос с чужого источника", 403)

        if p == "/api/diag":
            # Маячок с клиента: что именно видит страница в момент запуска.
            # Нужен, потому что «форма вместо входа» может значить и «SDK не
            # загрузился», и «Telegram не передал данные» — снаружи это
            # неразличимо. Содержимое initData НЕ принимаем, только признаки.
            if not rate_ok("diag:" + self._client_ip(), 30, 600):
                return self._json({"ok": True})
            try:
                d = json.loads(self._body() or b"{}")
            except (ValueError, json.JSONDecodeError):
                d = {}
            if d.get("jsError"):
                # ошибка JS на клиенте — она убивает весь скрипт разом
                audit("client_js_error", ip=self._client_ip(),
                      err=str(d.get("jsError"))[:160], at=str(d.get("at"))[:60],
                      ua=(self.headers.get("User-Agent") or "")[:70])
                return self._json({"ok": True})
            audit("client_diag", ip=self._client_ip(),
                  ua=(self.headers.get("User-Agent") or "")[:70],
                  sdk=bool(d.get("sdk")), sdk_init_len=int(d.get("sdkInitLen") or 0),
                  hash_len=int(d.get("hashLen") or 0),
                  hash_tg=bool(d.get("hashTg")), stored=bool(d.get("stored")),
                  platform=str(d.get("platform") or "")[:20],
                  ver=str(d.get("ver") or "")[:12],
                  search=str(d.get("search") or "")[:40])
            return self._json({"ok": True})

        if p == "/api/register":
            return self._register()
        if p == "/api/login":
            return self._login()
        if p == "/api/tg-login":
            return self._tg_login()

        user = self._need_user()
        if not user:
            return
        uid, gid, home = get_uid_gid_home(user)

        try:
            data = json.loads(self._body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err("плохой JSON")

        if p == "/api/servers":
            name = (data.get("name") or "").strip()[:40]
            host = (data.get("host") or "").strip()[:255]
            port = data.get("port") or 22
            ruser = (data.get("user") or "root").strip()[:32]
            auth = data.get("auth")
            secret = data.get("secret") or ""
            try:
                port = int(port)
                assert 1 <= port <= 65535
            except (ValueError, AssertionError):
                return self._err("порт: число 1–65535")
            if not name or not host:
                return self._err("укажите название и адрес сервера")
            if auth not in ("key", "password") or not secret:
                return self._err("нужен ключ или пароль")
            entry = {"id": secrets.token_hex(8), "name": name, "host": host,
                     "port": port, "user": ruser, "auth": auth,
                     "secret": enc_secret(secret)}
            with _lock:
                db = db_load()
                servers = user_servers(db, user)
                if len(servers) >= 20:
                    return self._err("слишком много серверов (лимит 20)")
                servers.append(entry)
                db_save(db)
            return self._json({"ok": True, "server": server_public(entry)})

        if p == "/api/servers/delete":
            sid = data.get("id")
            with _lock:
                db = db_load()
                servers = user_servers(db, user)
                db["users"][user]["servers"] = [s for s in servers if s["id"] != sid]
                db_save(db)
            return self._json({"ok": True})

        if p == "/api/servers/update":
            sid = data.get("id")
            with _lock:
                db = db_load()
                srv = next((s for s in user_servers(db, user) if s["id"] == sid), None)
                if not srv:
                    return self._err("сервер не найден", 404)
                if data.get("name"):
                    srv["name"] = data["name"].strip()[:40]
                if data.get("host"):
                    srv["host"] = data["host"].strip()[:255]
                if data.get("user"):
                    srv["user"] = data["user"].strip()[:32]
                if data.get("port"):
                    try:
                        pn = int(data["port"])
                        if 1 <= pn <= 65535:
                            srv["port"] = pn
                    except (ValueError, TypeError):
                        pass
                # смена доступа — по желанию
                na, ns = data.get("auth"), data.get("secret")
                if na in ("key", "password") and ns:
                    srv["auth"] = na
                    srv["secret"] = enc_secret(ns)
                    srv.pop("pubkey", None)
                    srv["generated"] = False
                db_save(db)
                return self._json({"ok": True, "server": server_public(srv)})

        if p == "/api/servers/ping":
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            return self._json(tcp_ping(srv["host"], srv["port"]))

        if p == "/api/servers/genkey":
            name = (data.get("name") or "").strip()[:40]
            host = (data.get("host") or "").strip()[:255]
            ruser = (data.get("user") or "root").strip()[:32]
            port = data.get("port") or 22
            try:
                port = int(port)
                assert 1 <= port <= 65535
            except (ValueError, AssertionError):
                return self._err("порт: число 1–65535")
            if not name or not host:
                return self._err("укажите название и адрес сервера")
            pair = gen_ssh_keypair("codepocket-" + user)
            if not pair:
                return self._err("не удалось сгенерировать ключ на сервере", 500)
            priv, pub = pair
            entry = {"id": secrets.token_hex(8), "name": name, "host": host,
                     "port": port, "user": ruser, "auth": "key",
                     "secret": enc_secret(priv), "pubkey": pub,
                     "generated": True}
            with _lock:
                db = db_load()
                servers = user_servers(db, user)
                if len(servers) >= 20:
                    return self._err("слишком много серверов (лимит 20)")
                servers.append(entry)
                db_save(db)
            # готовая команда для установки ключа на сервер пользователя
            install = ('mkdir -p ~/.ssh && echo "' + pub +
                       '" >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && '
                       'chmod 600 ~/.ssh/authorized_keys')
            return self._json({"ok": True, "server": server_public(entry),
                               "pubkey": pub, "install": install})

        if p == "/api/servers/to-key":
            # Перевести сервер с пароля на ключ: пока пароль ещё известен, сами
            # кладём публичный ключ в authorized_keys и дальше ходим по ключу.
            # Это убирает повторные парольные логины (их режут sshd и fail2ban —
            # отсюда «сервер отваливается») и ничего не требует от пользователя.
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            if srv.get("auth") == "key":
                return self._json({"ok": True, "already": True})
            pair = gen_ssh_keypair("codepocket-" + user)
            if not pair:
                return self._err("не удалось сгенерировать ключ", 500)
            priv, pub = pair
            ssh_owner(user)
            script = ('mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
                      'touch ~/.ssh/authorized_keys && '
                      'chmod 600 ~/.ssh/authorized_keys && '
                      '{ grep -qxF %s ~/.ssh/authorized_keys || '
                      'echo %s >> ~/.ssh/authorized_keys; }'
                      % (shlex.quote(pub), shlex.quote(pub)))
            rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script), timeout=45)
            ssh_owner(None)
            if rc != 0:
                return self._err(ssh_fail_reason(srv, rc, err)
                                 or ("не удалось записать ключ на сервер: "
                                     + (ssh_clean_err(err)[:200] or "ошибка SSH")), 502)
            # проверяем, что вход по ключу реально работает, и только потом
            # заменяем пароль — иначе можно остаться без доступа
            probe = dict(srv, auth="key", secret=enc_secret(priv))
            rc2, _o2, err2 = ssh_exec(probe, "true", timeout=30)
            if rc2 != 0:
                return self._err("ключ записан, но вход по нему не прошёл — "
                                 "пароль оставил как был ("
                                 + (ssh_clean_err(err2)[:150] or "нет деталей") + ")", 502)
            with _lock:
                db = db_load()
                for s_ in user_servers(db, user):
                    if s_.get("id") == srv.get("id"):
                        s_["auth"] = "key"
                        s_["secret"] = enc_secret(priv)
                        s_["pubkey"] = pub
                        s_["generated"] = True
                        break
                db_save(db)
            audit("server_to_key", user=user, ip=self._client_ip())
            return self._json({"ok": True})

        if p == "/api/servers/setup-ai":
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            log, installed = install_ai_on_server(srv)
            return self._json({"ok": bool(installed["claude"] or installed["codex"]),
                               "installed": installed, "log": log})

        if p == "/api/servers/copy-ai-auth":
            # копируем готовый вход Claude/Codex с одного своего сервера на другой
            # (обход логина там, где device-auth/браузер не проходят). Токены не
            # сохраняем: читаем с источника → сразу пишем на цель.
            src = get_server(user, data.get("from"))
            dst = get_server(user, data.get("to"))
            if not src or not dst:
                return self._err("сервер не найден", 404)
            if data.get("from") == data.get("to"):
                return self._err("источник и цель совпадают", 400)
            agent = data.get("agent") or "both"
            targets = []
            if agent in ("codex", "both"):
                targets.append((".codex", "auth.json", "Codex"))
            if agent in ("claude", "both"):
                targets.append((".claude", ".credentials.json", "Claude"))
            results = []
            for subdir, fname, label in targets:
                rc, out, err = ssh_exec(src, "bash -lc " + shlex.quote(
                    'f="$HOME/%s/%s"; [ -s "$f" ] && base64 "$f" || echo __NONE__'
                    % (subdir, fname)), timeout=30)
                text = out.decode("utf-8", "replace").strip()
                if "__NONE__" in text or not text:
                    results.append({"agent": label, "ok": False,
                                    "err": "на исходном сервере нет входа"})
                    continue
                try:
                    blob = base64.b64decode(text)
                    b64 = base64.b64encode(blob).decode()
                except Exception:
                    results.append({"agent": label, "ok": False,
                                    "err": "не удалось прочитать файл входа"})
                    continue
                script = (
                    'mkdir -p "$HOME/%s" && chmod 700 "$HOME/%s" 2>/dev/null; '
                    'printf %%s %s | base64 -d > "$HOME/%s/%s" && '
                    'chmod 600 "$HOME/%s/%s" && echo __OK__'
                    % (subdir, subdir, shlex.quote(b64),
                       subdir, fname, subdir, fname))
                rc2, out2, err2 = ssh_exec(dst, "bash -lc " + shlex.quote(script),
                                           timeout=30)
                ok = "__OK__" in out2.decode("utf-8", "replace")
                results.append({"agent": label, "ok": ok,
                                "err": None if ok else
                                (err2.decode("utf-8", "replace").strip()[:200]
                                 or "не удалось записать на целевой сервер")})
            any_ok = any(r["ok"] for r in results)
            return self._json({"ok": any_ok, "results": results})

        if p == "/api/servers/browse":
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            path = (data.get("path") or "").strip()
            if path:
                if not path.startswith("/") or "\n" in path:
                    return self._err("нужен абсолютный путь")
                target = shlex.quote(path)
            else:
                target = '"$HOME"'
            script = ('cd %s 2>/dev/null || { echo __CPERR__; exit 0; }; '
                      'pwd; echo __CPSEP__; ls -1Ap 2>/dev/null | grep "/$"' % target)
            rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script), timeout=25)
            text = out.decode("utf-8", "replace")
            if "__CPERR__" in text or "__CPSEP__" not in text:
                return self._err("папка недоступна на сервере", 400)
            head, _, rest = text.partition("__CPSEP__")
            lines = [l for l in head.strip().splitlines() if l.strip()]
            cur = lines[-1] if lines else "/"
            dirs = sorted(set(ln[:-1] for ln in rest.splitlines() if ln.endswith("/")))
            parent = "/" if cur == "/" else (cur.rsplit("/", 1)[0] or "/")
            return self._json({"path": cur, "parent": parent, "dirs": dirs,
                               "workdir": srv.get("workdir", "")})

        if p == "/api/servers/workdir":
            sid = data.get("id")
            wd = (data.get("dir") or "").strip()
            if wd and (not wd.startswith("/") or "\n" in wd):
                return self._err("нужен абсолютный путь без переносов строк")
            with _lock:
                db = db_load()
                srv = next((s for s in user_servers(db, user) if s["id"] == sid), None)
                if not srv:
                    return self._err("сервер не найден", 404)
                srv["workdir"] = wd
                if wd:
                    rec = [d for d in srv.get("recent_dirs", []) if d != wd]
                    rec.insert(0, wd)
                    srv["recent_dirs"] = rec[:8]
                db_save(db)
                pub = server_public(srv)
            return self._json({"ok": True, "server": pub})

        if p == "/api/servers/mkdir":   # создать папку на сервере (из выбора папок)
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            path = (data.get("path") or "").strip()
            if not path.startswith("/") or "\n" in path:
                return self._err("нужен абсолютный путь")
            rc, out, err = ssh_exec(srv, "bash -lc " +
                                    shlex.quote("mkdir -p %s" % shlex.quote(path)),
                                    timeout=20)
            if rc != 0:
                return self._err("не удалось создать папку: " +
                                 err.decode("utf-8", "replace")[:200], 500)
            return self._json({"ok": True, "path": path})

        if p == "/api/rfs":   # файлы прямо на сервере, в рабочей папке (как VS Code)
            srv = get_server(user, data.get("id"))
            if not srv or not srv.get("workdir"):
                return self._err("для сервера не задана рабочая папка", 400)
            op = data.get("op")
            base = srv["workdir"].rstrip("/") or "/"
            try:
                rel = _rel_ok(data.get("rel") if data.get("rel") is not None
                              else data.get("path") or "")
            except ValueError as e:
                return self._err(str(e), 400)
            path = base + ("/" + rel if rel else "")

            def _sh(script, stdin=None, timeout=30):
                return ssh_exec(srv, "bash -lc " + shlex.quote(script),
                                stdin_bytes=stdin, timeout=timeout)

            if op == "tree":
                rc, out, err = _sh('cd %s 2>/dev/null || exit 3; ls -1Ap'
                                   % shlex.quote(path), timeout=25)
                if rc == 3:
                    return self._err("папка недоступна на сервере", 404)
                items = []
                for ln in out.decode("utf-8", "replace").splitlines():
                    if not ln or ln in ("./", "../", ".", ".."):
                        continue
                    isdir = ln.endswith("/")
                    nm = ln[:-1] if isdir else ln
                    items.append({"name": nm, "dir": isdir,
                                  "path": (rel + "/" + nm) if rel else nm})
                items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
                return self._json({"items": items})

            if op == "read":
                rc, out, err = _sh('p=%s; [ -f "$p" ] || exit 4; head -c %d "$p" | base64'
                                   % (shlex.quote(path), MAX_FILE_SIZE + 10), timeout=30)
                if rc == 4:
                    return self._err("файл не найден", 404)
                try:
                    raw = base64.b64decode(out)
                except Exception:
                    raw = b""
                if len(raw) > MAX_FILE_SIZE:
                    return self._err("файл больше 2 МБ — откройте в терминале", 413)
                if b"\x00" in raw[:8192]:
                    return self._err("бинарный файл — в редакторе не открыть", 415)
                return self._json({"content": raw.decode("utf-8", "replace")})

            if op == "write":
                content = data.get("content", "")
                body = content.encode("utf-8")
                if len(body) > MAX_FILE_SIZE:
                    return self._err("файл больше 2 МБ", 413)
                parent = path.rsplit("/", 1)[0] or "/"
                rc, out, err = _sh('mkdir -p %s && cat > %s'
                                   % (shlex.quote(parent), shlex.quote(path)),
                                   stdin=body, timeout=40)
                if rc != 0:
                    return self._err("не удалось сохранить на сервере", 500)
                return self._json({"ok": True})

            if op == "upload":   # бинарное вложение (фото/файл) — пишем как есть
                try:
                    body = base64.b64decode(data.get("b64") or "")
                except Exception:
                    return self._err("плохие данные файла", 400)
                if len(body) > ATTACH_MAX:
                    return self._err("файл больше %d МБ" % (ATTACH_MAX // 1048576), 413)
                parent = path.rsplit("/", 1)[0] or "/"
                rc, out, err = _sh('mkdir -p %s && cat > %s'
                                   % (shlex.quote(parent), shlex.quote(path)),
                                   stdin=body, timeout=60)
                if rc != 0:
                    return self._err(ssh_fail_reason(srv, rc, err) or
                                     "не удалось загрузить файл на сервер", 502)
                return self._json({"ok": True, "path": path})

            if op == "mkfile":
                if not rel:
                    return self._err("нужно имя файла", 400)
                parent = path.rsplit("/", 1)[0] or "/"
                rc, out, err = _sh('mkdir -p %s && { [ -e %s ] && exit 5; :> %s; }'
                                   % (shlex.quote(parent), shlex.quote(path),
                                      shlex.quote(path)), timeout=20)
                if rc == 5:
                    return self._err("файл уже существует", 409)
                if rc != 0:
                    return self._err("не удалось создать файл", 500)
                return self._json({"ok": True})

            if op == "rm":
                if not rel:
                    return self._err("нельзя удалить корневую папку", 400)
                rc, out, err = _sh('rm -rf %s' % shlex.quote(path), timeout=25)
                return self._json({"ok": rc == 0})

            if op == "rename":
                if not rel:
                    return self._err("нужен путь", 400)
                try:
                    to = _rel_ok(data.get("to") or "")
                except ValueError as e:
                    return self._err(str(e), 400)
                if not to:
                    return self._err("нужно новое имя", 400)
                top = base + "/" + to
                rc, out, err = _sh('mkdir -p %s && mv %s %s'
                                   % (shlex.quote(top.rsplit("/", 1)[0] or "/"),
                                      shlex.quote(path), shlex.quote(top)), timeout=20)
                return self._json({"ok": rc == 0})

            return self._err("неизвестная операция", 400)

        if p == "/api/rchat-import":   # подтянуть беседу из папки (VS Code / терминал)
            srv = get_server(user, data.get("id"))
            if not srv or not srv.get("workdir"):
                return self._err("нужна рабочая папка", 400)
            agent = norm_agent(data.get("agent"))
            wd = srv["workdir"]
            needle = '"' + wd + '"'   # путь в кавычках — как поле cwd в транскриптах
            base_dir = ('"$HOME/.codex"' if agent == "codex"
                        else '"$HOME/.claude/projects"')
            # Если за этим чатом закреплена конкретная сессия — подтягиваем ТОЛЬКО её,
            # а не все беседы папки. Иначе после закрепления обратно приползут реплики
            # из других сессий той же папки и засорят закреплённый вид.
            pinned = None
            _pp = (data.get("project") or "").strip()
            if _pp and PROJECT_RE.match(_pp):
                pinned = (chat_load(user, _pp, agent) or {}).get("remote_session")
            if pinned and _UUID_RE.fullmatch(pinned or ""):
                # pinned прошёл _UUID_RE — только hex и дефисы, безопасно вставить в glob
                script = (
                    'F=$(grep -rlF %s %s --include="*%s*.jsonl" '
                    '--exclude=history.jsonl --exclude=session_index.jsonl '
                    '2>/dev/null | head -1); '
                    '[ -z "$F" ] && { echo __NONE__; exit 0; }; '
                    '{ echo __CPFILE__; tail -c 400000 "$F"; echo; } | base64'
                    % (shlex.quote(needle), base_dir, pinned))
                rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script),
                                        timeout=45)
                text = out.decode("utf-8", "replace")
                raw = b""
                if "__NONE__" not in text and text.strip():
                    try:
                        raw = base64.b64decode(text)
                    except Exception:
                        raw = b""
                turns = []
                for block in raw.decode("utf-8", "replace").split("__CPFILE__"):
                    block = block.strip()
                    if block:
                        turns.extend(parse_transcript_lines(block))
                return self._json({"turns": turns[-400:], "available": []})
            # Берём ТОЛЬКО ОДНУ — самую свежую — беседу этой папки.
            # Раньше сюда склеивались 12 последних сессий подряд: в одной ленте
            # оказывались куски разных разговоров (и разных вкладок) — это и есть
            # «показывает всю переписку смешанно». Один чат = одна сессия;
            # остальные доступны через список «Чаты».
            # Исключаем history.jsonl/session_index.jsonl (команды/метаданные).
            script = (
                'F=$(grep -rlF %s %s --include="*.jsonl" '
                '--exclude=history.jsonl --exclude=session_index.jsonl 2>/dev/null '
                '| xargs -r ls -1t 2>/dev/null | head -1); '
                '[ -z "$F" ] && { echo __NONE__; exit 0; }; '
                'echo "__CPPATH__$F"; '
                'echo "__CPMTIME__$(stat -c %%Y "$F" 2>/dev/null || echo 0)"; '
                'tail -c 400000 "$F" | base64'
                % (shlex.quote(needle), base_dir))
            rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script), timeout=45)
            if rc != 0 and not out.strip():
                hint = ssh_fail_reason(srv, rc, err)
                if hint:
                    return self._err(hint, 502)
            text = out.decode("utf-8", "replace")
            fpath, mtime, b64 = "", "", []
            for ln in text.splitlines():
                if ln.startswith("__CPPATH__"):
                    fpath = ln[len("__CPPATH__"):].strip()
                elif ln.startswith("__CPMTIME__"):
                    mtime = ln[len("__CPMTIME__"):].strip()
                elif ln.strip() and not ln.startswith("__"):
                    b64.append(ln.strip())
            raw = b""
            if "__NONE__" not in text and b64:
                try:
                    raw = base64.b64decode("".join(b64))
                except Exception:
                    raw = b""
            turns = parse_transcript_lines(raw.decode("utf-8", "replace"),
                                           with_tools=True)
            if turns:
                # Закрепляем эту сессию за чатом: дальше и подтягивание, и отправка
                # идут ровно в неё (claude --resume <id>). Без закрепления каждая
                # загрузка могла попасть в другую сессию папки — отсюда и «каша».
                sid = session_id_from_file(fpath)
                if sid and _pp and PROJECT_RE.match(_pp):
                    def _pin(ch):
                        # идёт отправка — не лезем: её история сейчас в памяти
                        # другого потока, и наша запись затёрла бы новые реплики
                        with _agent_busy_lock:
                            if user in _agent_busy:
                                return False
                        if ch.get("remote_session") == sid:
                            return False
                        ch["remote_session"] = sid
                    chat_update(user, _pp, agent, _pin)
                return self._json({"turns": turns[-400:], "available": [],
                                   "session": sid, "mtime": mtime})
            # истории для этой папки нет — подскажем, для каких папок она есть
            avail_cmd = ('grep -rhoE \'"cwd" *: *"[^"]*"\' ' + base_dir +
                         ' --include="*.jsonl" 2>/dev/null | '
                         'sed -E \'s/.*: *"([^"]*)".*/\\1/\' | sort -u | head -40')
            rc2, out2, _ = ssh_exec(srv, "bash -lc " + shlex.quote(avail_cmd),
                                    timeout=25)
            available = [l.strip() for l in
                         out2.decode("utf-8", "replace").splitlines() if l.strip()]
            # диагностика: полностью показываем структуру Codex и все файлы сессий
            dbg_cmd = ('echo "== CODEX DIRS =="; find "$HOME/.codex" -maxdepth 3 -type d '
                       '2>/dev/null | head -20; '
                       'echo "== ALL JSONL =="; find "$HOME/.claude" "$HOME/.codex" '
                       '-name "*.jsonl" 2>/dev/null | head -25; '
                       'echo "== SAMPLE (codex) =="; find "$HOME/.codex" -name "*.jsonl" '
                       '2>/dev/null | grep -v session_index | head -1 | xargs -r head -c 700')
            rc3, out3, _ = ssh_exec(srv, "bash -lc " + shlex.quote(dbg_cmd), timeout=25)
            debug = out3.decode("utf-8", "replace")[:2200]
            return self._json({"turns": [], "available": available, "debug": debug})

        if p == "/api/rchat-list":   # список ВСЕХ чатов сервера (как в VS Code)
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            agent = norm_agent(data.get("agent"))
            if agent == "codex":
                # у Codex сессии лежат в разных местах; исключаем только индекс и
                # плагиновые фикстуры, history.jsonl оставляем (там бывает переписка)
                find_expr = ('find "$HOME/.codex" -name "*.jsonl" '
                             '-not -name session_index.jsonl -not -path "*/.tmp/*"')
            else:
                find_expr = ('find "$HOME/.claude/projects" -name "*.jsonl" '
                             '-not -name history.jsonl -not -name session_index.jsonl')
            # строгий счётчик = только НАБРАННЫЕ сообщения (role:user + строковый
            # content). Иначе tool_result-строки (тоже type/role user) раздувают
            # число: в реальном чате бывает 1500 строк, а сообщений — полторы сотни.
            strict = shlex.quote('"role" *: *"user" *, *"content" *: *"')
            loose = shlex.quote('"role" *: *"user"|"type" *: *"user"')
            # для заголовка берём именно строки-реплики user (а не первые 4 КБ файла):
            # так первое НАСТОЯЩЕЕ сообщение попадёт в заголовок, даже если файл
            # начинается со служебных строк (system-reminder, tool_result).
            script = (
                'for f in $(%s 2>/dev/null | xargs -r ls -1t 2>/dev/null | head -40); '
                'do c=$(grep -cE %s "$f" 2>/dev/null); '
                '[ "${c:-0}" = 0 ] && c=$(grep -cE %s "$f" 2>/dev/null); '
                'echo "__F__ $f $(stat -c %%Y "$f" 2>/dev/null) ${c:-0}"; '
                'grep -E %s "$f" 2>/dev/null | head -n 12 | head -c 5000; '
                'echo; echo "__E__"; done | base64'
                % (find_expr, strict, loose, loose))
            rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script), timeout=45)
            if rc != 0 and not out.strip():
                hint = ssh_fail_reason(srv, rc, err)
                if hint:
                    return self._err(hint, 502)
            text = out.decode("utf-8", "replace")
            try:
                raw = base64.b64decode(text) if text.strip() else b""
            except Exception:
                raw = b""
            chats = []
            cur = None
            buf = []

            def _flush():
                if not cur:
                    return
                tns = parse_transcript_lines("\n".join(buf))
                cnt = cur.get("count", 0) or len(tns)
                if cnt <= 0 and not tns:
                    return
                title = next((t["text"] for t in tns if t["role"] == "user"),
                             tns[0]["text"] if tns else "(без названия)")
                chats.append({"file": cur["file"], "mtime": cur["mtime"],
                              "folder": cur.get("cwd", ""),
                              "title": (title or "(без названия)")[:120],
                              "count": cnt})
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.startswith("__F__ "):
                    _flush()
                    parts = line[6:].rsplit(" ", 2)
                    fpath = parts[0]
                    mt = parts[1] if len(parts) >= 2 and parts[1].isdigit() else "0"
                    cnt = parts[2] if len(parts) >= 3 and parts[2].isdigit() else "0"
                    cur = {"file": fpath, "mtime": int(mt), "cwd": "",
                           "count": int(cnt)}
                    buf = []
                elif line.strip() == "__E__":
                    _flush()
                    cur = None
                    buf = []
                elif cur is not None:
                    buf.append(line)
                    if not cur["cwd"] and '"cwd"' in line:
                        m = re.search(r'"cwd"\s*:\s*"([^"]*)"', line)
                        if m:
                            cur["cwd"] = m.group(1)
            _flush()
            chats = [c for c in chats if c["count"] > 0]
            return self._json({"chats": chats})

        if p == "/api/rchat-load":   # загрузить один выбранный чат целиком
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            f = (data.get("file") or "").strip()
            if "\n" in f or ".." in f or not f.startswith("/"):
                return self._err("плохой путь", 400)
            script = ('case %s in "$HOME/.claude/"*|"$HOME/.codex/"*) '
                      'tail -c 900000 %s | base64;; *) echo __DENY__;; esac'
                      % (shlex.quote(f), shlex.quote(f)))
            rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script), timeout=30)
            if rc != 0 and not out.strip():
                hint = ssh_fail_reason(srv, rc, err)
                if hint:
                    return self._err(hint, 502)
            text = out.decode("utf-8", "replace")
            if "__DENY__" in text or not text.strip():
                return self._err("файл недоступен", 400)
            try:
                raw = base64.b64decode(text)
            except Exception:
                return self._json({"turns": []})
            turns = parse_transcript_lines(raw.decode("utf-8", "replace"),
                                           with_tools=True)
            return self._json({"turns": turns[-500:]})

        if p == "/api/rchat-bind":   # закрепить выбранную сессию за чатом приложения
            srv = get_server(user, data.get("id"))
            if not srv:
                return self._err("сервер не найден", 404)
            agent = data.get("agent") or "claude"
            if agent not in ("claude", "codex"):
                agent = "claude"
            proj = (data.get("project") or "").strip()
            if not PROJECT_RE.match(proj):
                return self._err("плохое имя проекта", 400)
            f = (data.get("file") or "").strip()
            if "\n" in f or ".." in f or not f.startswith("/"):
                return self._err("плохой путь", 400)
            sid = session_id_from_file(f)
            if not sid:
                return self._err("не удалось определить id сессии по имени файла", 400)
            # читаем транскрипт выбранной беседы — чтобы история приложения совпала
            # с ней (и обе стороны продолжали ровно эту сессию)
            script = ('case %s in "$HOME/.claude/"*|"$HOME/.codex/"*) '
                      'tail -c 900000 %s | base64;; *) echo __DENY__;; esac'
                      % (shlex.quote(f), shlex.quote(f)))
            rc, out, err = ssh_exec(srv, "bash -lc " + shlex.quote(script), timeout=30)
            if rc != 0 and not out.strip():
                hint = ssh_fail_reason(srv, rc, err)
                if hint:
                    return self._err(hint, 502)
            text = out.decode("utf-8", "replace")
            turns = []
            if "__DENY__" not in text and text.strip():
                try:
                    raw = base64.b64decode(text)
                    turns = parse_transcript_lines(raw.decode("utf-8", "replace"),
                                           with_tools=True)
                except Exception:
                    turns = []
            # не перетираем историю прямо во время активной отправки агентом
            with _agent_busy_lock:
                busy = user in _agent_busy
            if busy:
                return self._err("агент сейчас отвечает — повторите через секунду", 429)
            chat = chat_load(user, proj, agent)
            chat["remote_session"] = sid
            ui = []
            for t in turns[-400:]:
                if t.get("role") == "tool":
                    ui.append(dict(t, type="work"))
                elif t.get("role") == "user":
                    ui.append({"type": "user", "text": t["text"]})
                elif t.get("role") != "sep":
                    ui.append({"type": "text", "text": t["text"]})
            chat["ui"] = ui
            chat_save(user, proj, chat, agent)
            return self._json({"turns": turns[-400:], "session": sid})

        # ---- админ-действия (только dev) ----
        if p.startswith("/api/admin/"):
            with _lock:
                if user_role(db_load(), user) != "dev":
                    return self._err("нужны права разработчика", 403)
            target_user = (data.get("username") or "").strip().lower()

            if p == "/api/admin/reset-pin":
                with _lock:
                    db = db_load()
                    u = db["users"].get(target_user)
                    if not u:
                        return self._err("пользователь не найден", 404)
                    pin = f"{secrets.randbelow(1_000_000):06d}"
                    u["salt"] = secrets.token_hex(8)
                    u["pin"] = hash_pin(pin, u["salt"])
                    u["fails"] = 0
                    u.pop("locked_until", None)
                    db_save(db)
                audit("admin_reset_pin", admin=user, target=target_user,
                      ip=self._client_ip())
                return self._json({"ok": True, "pin": pin})

            if p == "/api/admin/set-role":
                role = data.get("role")
                if role not in ("dev", "user"):
                    return self._err("роль: dev или user")
                with _lock:
                    db = db_load()
                    u = db["users"].get(target_user)
                    if not u:
                        return self._err("пользователь не найден", 404)
                    u["role"] = role
                    db_save(db)
                audit("admin_set_role", admin=user, target=target_user,
                      role=role, ip=self._client_ip())
                return self._json({"ok": True})

            if p == "/api/admin/delete-user":
                if target_user == user:
                    return self._err("нельзя удалить самого себя", 400)
                with _lock:
                    db = db_load()
                    if target_user not in db["users"]:
                        return self._err("пользователь не найден", 404)
                    db["users"].pop(target_user, None)
                    db["sessions"] = {t: s for t, s in db["sessions"].items()
                                      if s.get("user") != target_user}
                    db_save(db)
                userdel = shutil.which("userdel") or "/usr/sbin/userdel"
                try:
                    subprocess.run([userdel, "-r", "-f", target_user],
                                   capture_output=True, timeout=30)
                except (subprocess.SubprocessError, OSError):
                    pass
                audit("admin_delete_user", admin=user, target=target_user,
                      ip=self._client_ip())
                return self._json({"ok": True})

            if p == "/api/admin/unlock":
                with _lock:
                    db = db_load()
                    u = db["users"].get(target_user)
                    if u:
                        u["fails"] = 0
                        u.pop("locked_until", None)
                        db_save(db)
                return self._json({"ok": True})

            if p == "/api/admin/limits":
                with _lock:
                    L = limits_save(data)
                audit("admin_set_limits", admin=user, limits=L,
                      ip=self._client_ip())
                return self._json({"ok": True, "limits": L})

            return self._err("не найдено", 404)

        if p == "/api/sessions/revoke":
            # выйти на всех других устройствах — оставить только текущую сессию
            cur = self._cookie_token()
            with _lock:
                db = db_load()
                db["sessions"] = {t: s for t, s in db["sessions"].items()
                                  if s.get("user") != user or t == cur}
                db_save(db)
            audit("revoke_sessions", user=user, ip=self._client_ip())
            return self._json({"ok": True})

        if p == "/api/logout":
            cookie = self.headers.get("Cookie", "")
            token = None
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ide_session":
                    token = v
            if token:
                with _lock:
                    db = db_load()
                    db["sessions"].pop(token, None)
                    db_save(db)
            return self._json({"ok": True},
                              cookie="ide_session=; Path=/; Max-Age=0")

        if p == "/api/lint":
            code = data.get("code") or ""
            lang = data.get("lang") or ""
            if len(code) > 200_000:
                return self._json({"diags": []})
            if lang == "python":
                return self._json({"diags": lint_python(user, code)})
            return self._json({"diags": []})

        if p == "/api/history/restore":
            rel = data.get("path") or ""
            ts = data.get("ts")
            try:
                target = safe_path(home, rel)
            except PermissionError as e:
                return self._err(str(e), 403)
            content = history_get(user, rel, ts)
            if content is None:
                return self._err("версия не найдена", 404)
            # текущую версию тоже снапшотим перед откатом
            try:
                if os.path.isfile(target):
                    with open(target, "rb") as f:
                        history_snapshot(user, rel, f.read())
                with open(target, "wb") as f:
                    f.write(content)
                os.chown(target, uid, gid)
            except OSError as e:
                return self._err(str(e), 500)
            return self._json({"ok": True,
                               "content": content.decode("utf-8", errors="replace")})

        if p == "/api/avatar":
            av = (data.get("avatar") or "").strip()
            # ждём data:image/...;base64,... — небольшую картинку
            if av and not av.startswith("data:image/"):
                return self._err("нужна картинка")
            if len(av) > 400_000:
                return self._err("картинка великовата (уменьшите до ~250 КБ)")
            with _lock:
                db = db_load()
                u = db["users"].get(user)
                if not u:
                    return self._err("пользователь не найден", 404)
                if av:
                    u["avatar"] = av
                else:
                    u.pop("avatar", None)
                db_save(db)
            return self._json({"ok": True})

        if p == "/api/email":
            em = (data.get("email") or "").strip()[:120]
            if em and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
                return self._err("похоже, это не email")
            with _lock:
                db = db_load()
                u = db["users"].get(user)
                if not u:
                    return self._err("пользователь не найден", 404)
                if em:
                    u["email"] = em
                else:
                    u.pop("email", None)
                db_save(db)
            return self._json({"ok": True})

        if p == "/api/aikeys":
            agent = data.get("agent")
            key = (data.get("key") or "").strip()
            if agent not in ("claude", "codex"):
                return self._err("agent: claude или codex")
            with _lock:
                db = db_load()
                u = db["users"].get(user)
                if not u:
                    return self._err("пользователь не найден", 404)
                fld = "ai_%s_key" % agent
                if key:
                    u[fld] = enc_secret(key)
                else:
                    u.pop(fld, None)
                db_save(db)
            return self._json({"ok": True})

        if p == "/api/pin":
            old = (data.get("old") or "").strip()
            new = (data.get("new") or "").strip()
            if not (new.isdigit() and len(new) == 6):
                return self._err("новый PIN — ровно 6 цифр")
            with _lock:
                db = db_load()
                u = db["users"].get(user)
                if not u or not secrets.compare_digest(hash_pin(old, u["salt"]), u["pin"]):
                    time.sleep(1)
                    return self._err("текущий PIN неверный", 403)
                u["salt"] = secrets.token_hex(8)
                u["pin"] = hash_pin(new, u["salt"])
                db_save(db)
            audit("pin_change", user=user, ip=self._client_ip())
            return self._json({"ok": True})

        if p == "/api/account/delete":
            pin = (data.get("pin") or "").strip()
            with _lock:
                db = db_load()
                u = db["users"].get(user)
                if not u or not secrets.compare_digest(hash_pin(pin, u["salt"]), u["pin"]):
                    time.sleep(1)
                    return self._err("PIN неверный", 403)
                db["users"].pop(user, None)
                db["sessions"] = {t: s for t, s in db["sessions"].items()
                                  if s.get("user") != user}
                db_save(db)
            # удаляем Linux-учётку с файлами
            userdel = shutil.which("userdel") or "/usr/sbin/userdel"
            try:
                subprocess.run([userdel, "-r", "-f", user],
                               capture_output=True, timeout=30)
            except (subprocess.SubprocessError, OSError):
                pass
            audit("account_delete", user=user, ip=self._client_ip())
            return self._json({"ok": True},
                              cookie="ide_session=; Path=/; Max-Age=0")

        if p == "/api/projects":
            name = (data.get("name") or "").strip()
            template = data.get("template") or "python"
            if not PROJECT_RE.match(name):
                return self._err("имя проекта: латиница/цифры/дефис, до 31 символа")
            if template not in TEMPLATES:
                return self._err("неизвестный шаблон")
            d = ensure_projects_dir(user)
            proj = os.path.join(d, name)
            if os.path.exists(proj):
                return self._err("проект с таким именем уже есть", 409)
            os.makedirs(proj)
            os.chown(proj, uid, gid)
            for fname, content in TEMPLATES[template].items():
                fp = os.path.join(proj, fname)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chown(fp, uid, gid)
            return self._json({"ok": True, "name": name})

        if p == "/api/chat-stop":   # прервать работу агента (кнопка «Стоп»)
            killed = agent_cancel(user)
            proj = (data.get("project") or "").strip()
            agent = data.get("agent") or "claude"
            if agent not in ("claude", "codex"):
                agent = "claude"
            if PROJECT_RE.match(proj):
                chat_update(user, proj, agent, lambda ch: ch["ui"].append(
                    {"type": "error", "text": "⏹ Остановлено вами"}))
            return self._json({"ok": True, "killed": killed})

        if p == "/api/chat":
            proj = (data.get("project") or "").strip()
            message = (data.get("message") or "").strip()
            if not PROJECT_RE.match(proj):
                return self._err("плохое имя проекта")
            # вложения: список путей, уже залитых на сервер/в проект. Агент читает
            # их с диска сам — в сообщение подставляем понятный ему список путей.
            atts = data.get("attachments") or []
            if not isinstance(atts, list):
                atts = []
            atts = [str(a)[:400] for a in atts[:10] if str(a or "").strip()]
            if atts:
                lines = "\n".join("- " + a for a in atts)
                message = (message + "\n\nПрикреплённые файлы (прочитай их с диска):\n"
                           + lines).strip()
            if not message or len(message) > 8000:
                return self._err("пустое или слишком длинное сообщение")
            proj_dir = os.path.join(projects_dir(home), proj)
            # какой агент: claude (по умолчанию) или codex
            agent = data.get("agent") or "claude"
            if agent not in ("claude", "codex"):
                agent = "claude"
            # агент через свой сервер пользователя или (только claude) через API
            srv = get_server(user, data.get("server")) if data.get("server") else None
            # в режиме рабочей папки (как VS Code) локальный проект не требуется
            if not (srv and srv.get("workdir")) and not os.path.isdir(proj_dir):
                return self._err("проект не найден", 404)
            if agent == "codex" and not srv:
                return self._err("Codex работает только на вашем сервере — "
                                 "выберите сервер вверху терминала", 400)

            used = quota = 0
            if agent == "claude" and not srv:   # квота — только API-агент Claude
                with _lock:
                    db = db_load()
                    used, quota = quota_state(db, user)
                    db_save(db)
                if used >= quota:
                    return self._err("дневной лимит Claude исчерпан — "
                                     "обновится завтра", 429)

            with _agent_busy_lock:
                if user in _agent_busy:
                    return self._err("агент ещё работает над прошлым "
                                     "сообщением — подождите", 429)
                _agent_busy.add(user)
            agent_cancel_clear(user)   # новая отправка — снимаем прошлую отмену
            ssh_owner(user)            # чтобы «Стоп» знал, чьи ssh-процессы убивать
            progress_reset(user, "думаю…")
            try:
                chat = chat_update(user, proj, agent, lambda ch:
                                   ch["ui"].append({"type": "user", "text": message}))
                # Сохраняем сообщение пользователя СРАЗУ, до запуска агента (агент
                # может думать до 600 с). Тогда при обрыве связи телефон, опрашивая
                # чат, увидит своё сообщение и статус «занято», а после — ответ,
                # который допишется в конце. Без этого промежуточного сохранения
                # история на диске пустует всё время работы агента.

                if srv:   # ── агент на СЕРВЕРЕ ПОЛЬЗОВАТЕЛЯ (Max / ChatGPT) ──
                    inplace = bool(srv.get("workdir"))  # режим «как VS Code»
                    if not inplace and not remote_push_project(srv, proj, proj_dir):
                        msg = ("не удалось подключиться к вашему серверу — "
                               "проверьте доступ в 🖥 Серверы")
                        chat_update(user, proj, agent, lambda ch:
                                    ch["ui"].append({"type": "error", "text": msg}))
                        return self._err(msg, 502)
                    t0 = time.time()
                    akey = ai_key_get(user, agent)
                    if agent == "codex":
                        answer, sid, rerr = run_codex_remote(
                            srv, proj, message, chat.get("remote_session"), akey)
                    else:
                        answer, sid, rerr = run_claude_remote(
                            srv, proj, message, chat.get("remote_session"), akey)
                    if not inplace:
                        remote_pull_project(srv, proj, proj_dir, uid, gid)
                    # id сессии запоминаем и при ошибке — беседа на сервере могла
                    # успеть создаться, и продолжать надо именно её (для обоих агентов)
                    if sid:
                        chat["remote_session"] = sid
                    if agent_cancel_requested(user):
                        agent_cancel_clear(user)
                        if sid:
                            chat_update(user, proj, agent, lambda ch:
                                        ch.__setitem__("remote_session", sid))
                        return self._err("⏹ Остановлено вами", 499)
                    if rerr:
                        def _rerr(ch):
                            ch["ui"].append({"type": "error", "text": rerr})
                            if sid:
                                ch["remote_session"] = sid
                        chat_update(user, proj, agent, _rerr)
                        return self._err(rerr, 502)
                    steps = [{"type": "text", "text": answer}]
                    # карточки изменённых файлов — только в синк-режиме; при рабочей
                    # папке файлы остаются на сервере (VS Code-режим)
                    if not inplace:
                        for f in changed_files(proj_dir, t0 - 3):
                            steps.append({"type": "file", "path": "projects/%s/%s" % (proj, f["rel"]),
                                          "name": f["rel"], "size": f["size"]})
                    # Сохраняем работу агента в историю: иначе живые карточки
                    # («выполнил команду», «изменил файл») пропадали сразу после
                    # ответа, и при следующем открытии чата их уже не было.
                    work = []
                    for w in progress_get(user).get("steps") or []:
                        if w.get("kind") == "say":
                            t = str(w.get("text") or "").strip()
                            if t and t != str(answer or "").strip():
                                work.append({"type": "text", "text": t})
                        elif w.get("title") or w.get("target"):
                            work.append(dict(w, type="work"))

                    def _done(ch):
                        ch["ui"].extend(work)
                        ch["ui"].extend(steps)
                        if sid:
                            ch["remote_session"] = sid
                    chat_update(user, proj, agent, _done)
                    return self._json({"steps": steps, "remote": True})

                # ── Claude через Anthropic API (нужен ANTHROPIC_API_KEY) ──
                try:
                    steps, api_hist, usage = agent_mod.run_agent(
                        user, proj_dir, chat["api"], message,
                        should_stop=lambda: agent_cancel_requested(user),
                        images=data.get("images") or None)
                except RuntimeError as e:
                    # текст берём в переменную: имя e существует только внутри
                    # except-блока, а лямбда ссылалась на него напрямую
                    emsg = str(e)
                    chat_update(user, proj, agent, lambda ch:
                                ch["ui"].append({"type": "error", "text": emsg}))
                    return self._err(emsg, 502)
                def _apidone(ch):
                    ch["api"] = api_hist
                    ch["ui"].extend(steps)
                chat_update(user, proj, agent, _apidone)
                total = usage["in"] + usage["out"]
                quota_add(user, total)
                return self._json({"steps": steps,
                                   "used": used + total, "quota": quota})
            finally:
                agent_cancel_clear(user)
                ssh_owner(None)   # снимаем метку: «Стоп» не должен цеплять
                                  # обычные команды терминала в этом же потоке
                with _agent_busy_lock:
                    _agent_busy.discard(user)

        if p == "/api/fs":
            op = data.get("op")
            try:
                target = safe_path(home, data.get("path", ""))
                if op == "mkdir":
                    os.makedirs(target, exist_ok=True)
                    os.chown(target, uid, gid)
                elif op == "mkfile":
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if os.path.exists(target):
                        return self._err("уже существует", 409)
                    with open(target, "w"):
                        pass
                    os.chown(target, uid, gid)
                elif op == "rm":
                    if target == os.path.realpath(home):
                        return self._err("нельзя удалить домашнюю папку", 403)
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    else:
                        os.remove(target)
                elif op == "rename":
                    new = safe_path(home, data.get("to", ""))
                    os.rename(target, new)
                else:
                    return self._err("неизвестная операция")
            except PermissionError as e:
                return self._err(str(e), 403)
            except OSError as e:
                return self._err(str(e), 500)
            return self._json({"ok": True})

        return self._err("не найдено", 404)

    def do_PUT(self):
        url = urlparse(self.path)
        if url.path != "/api/file":
            return self._err("не найдено", 404)
        user = self._need_user()
        if not user:
            return
        uid, gid, home = get_uid_gid_home(user)
        q = parse_qs(url.query)
        rel = (q.get("path") or [""])[0]
        try:
            target = safe_path(home, rel)
            body = self._body()
            if len(body) > MAX_FILE_SIZE:
                return self._err("файл больше 2 МБ", 413)
            # снапшот предыдущей версии для истории
            if os.path.isfile(target):
                try:
                    with open(target, "rb") as f:
                        prev = f.read()
                    if prev != body:
                        history_snapshot(user, rel, prev)
                except OSError:
                    pass
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(body)
            os.chown(target, uid, gid)
        except PermissionError as e:
            return self._err(str(e), 403)
        except OSError as e:
            return self._err(str(e), 500)
        return self._json({"ok": True})

    # --- регистрация и вход ---
    def _client_ip(self):
        """IP клиента. За обратным прокси берём ПОСЛЕДНИЙ элемент
        X-Forwarded-For — его дописал наш Caddy. Первый элемент присылает сам
        клиент, и раньше мы брали именно его: подделав заголовок, кто угодно
        сбрасывал себе счётчик попыток входа и подменял записи в аудит-логе.
        Заголовку верим только когда соединение пришло от локального прокси."""
        peer = self.client_address[0]
        if not _is_trusted_proxy(peer):
            return peer
        xff = self.headers.get("X-Forwarded-For") or ""
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        return hops[-1] if hops else peer

    def _origin_ok(self):
        """Пришёл ли запрос с нашей же страницы.

        Отклоняем только КОНКРЕТНЫЙ чужой источник. Отсутствующий или
        непрозрачный (`null`) Origin пропускаем: так ведут себя не-браузерные
        клиенты и встроенный Telegram Mini App — он живёт в песочнице iframe,
        и браузер шлёт оттуда `Origin: null`. Первая версия этой проверки
        отбивала такие запросы, и вход через Telegram переставал работать:
        человек видел форму с инвайт-кодом вместо своего аккаунта.

        Защиту это не ослабляет: чужая вкладка всегда присылает свой реальный
        источник, а от встраивания нас закрывает CSP frame-ancestors, куда
        Telegram внесён явно. Плюс cookie сессии помечена SameSite=Lax."""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or origin.lower() == "null":
            return True
        try:
            o = urlparse(origin)
        except ValueError:
            return False
        if not o.hostname:
            return False
        # Сравниваем имя И порт. Порт по умолчанию подразумевается схемой:
        # https://site и https://site:443 — один источник, а https://site:8443
        # уже другой, и раньше он проходил, потому что сверялось только имя.
        host = (self.headers.get("Host") or "").lower()
        hname, _, hport = host.partition(":")
        page_https = self._is_https()
        oport = o.port or (443 if o.scheme == "https" else 80)
        hport = int(hport) if hport.isdigit() else (443 if page_https else 80)
        if o.hostname.lower() == hname and oport == hport:
            return True
        # Telegram открывает мини-апп со своих доменов
        h = o.hostname.lower()
        return h == "telegram.org" or h.endswith(".telegram.org")

    def _cookie_token(self):
        for part in (self.headers.get("Cookie", "")).split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ide_session":
                return v
        return None

    def _is_https(self):
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def end_headers(self):
        # базовые заголовки безопасности на все ответы (кроме WS-рукопожатия,
        # которое пишется напрямую в сокет и сюда не заходит)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # встраивать в iframe можно только нам самим и Telegram (Mini App).
        # Это защищает от кликджекинга, но не мешает Telegram Web открыть приложение.
        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'self' https://web.telegram.org "
                         "https://*.telegram.org")
        if self._is_https():
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        super().end_headers()

    def _session_cookie(self, token):
        sec = "; Secure" if self._is_https() else ""
        return (f"ide_session={token}; Path=/; HttpOnly; SameSite=Lax{sec}; "
                f"Max-Age={SESSION_TTL}")

    def _register(self):
        ip = self._client_ip()
        if not rate_ok("reg:" + ip, 5, 3600):
            return self._err("слишком много регистраций с вашего адреса — "
                             "попробуйте позже", 429)
        try:
            data = json.loads(self._body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err("плохой JSON")
        invite = (data.get("invite") or "").strip()
        username = (data.get("username") or "").strip().lower()

        # определяем роль по способу регистрации
        if DEV_INVITE and secrets.compare_digest(invite, DEV_INVITE):
            role = "dev"
        elif OPEN_SIGNUP:
            role = "user"
        else:
            return self._err("нужен инвайт-код (открытая регистрация выключена)", 403)

        if not USERNAME_RE.match(username) or username in RESERVED:
            return self._err("имя: 3–21 символ, латиница/цифры/дефис, начинается с буквы")

        with _lock:
            db = db_load()
            if username in db["users"]:
                return self._err("имя занято — если это вы, войдите со своим PIN", 409)
            if user_exists_in_system(username):
                return self._err("имя занято на сервере, выберите другое", 409)
            try:
                create_linux_user(username)
            except subprocess.CalledProcessError as e:
                return self._err("не удалось создать пользователя: " +
                                 e.stderr.decode(errors="replace")[:200], 500)
            pin = f"{secrets.randbelow(1_000_000):06d}"
            salt = secrets.token_hex(8)
            db["users"][username] = {"salt": salt, "pin": hash_pin(pin, salt),
                                     "role": role, "created": int(time.time())}
            token = new_session(db, username, ip, self.headers.get("User-Agent", ""),
                                current=self._cookie_token())
            db_save(db)
        audit("register", user=username, role=role, ip=ip)
        return self._json({"ok": True, "user": username, "pin": pin, "role": role},
                          cookie=self._session_cookie(token))

    def _login(self):
        ip = self._client_ip()
        if not rate_ok("login:" + ip, 20, 900):
            return self._err("слишком много попыток входа — подождите", 429)
        try:
            data = json.loads(self._body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err("плохой JSON")
        username = (data.get("username") or "").strip().lower()
        pin = (data.get("pin") or "").strip()

        bad = False
        with _lock:
            db = db_load()
            u = db["users"].get(username)
            now = int(time.time())
            if u and u.get("locked_until", 0) > now:
                left = u["locked_until"] - now
                return self._err(f"аккаунт временно заблокирован, ждите {left} с", 429)
            if not u or not secrets.compare_digest(hash_pin(pin, u["salt"]), u["pin"]):
                if u:
                    u["fails"] = u.get("fails", 0) + 1
                    if u["fails"] >= 5:
                        u["locked_until"] = now + 300  # 5 минут после 5 ошибок
                        u["fails"] = 0
                        audit("lockout", user=username, ip=ip)
                    db_save(db)
                bad = True
            else:
                u["fails"] = 0
                u.pop("locked_until", None)
                token = new_session(db, username, ip, self.headers.get("User-Agent", ""),
                                current=self._cookie_token())
                db_save(db)
        # задержку от перебора держим ВНЕ глобального замка,
        # иначе поток неудачных входов подвешивает весь сервер
        if bad:
            time.sleep(1)
            return self._err("неверное имя или PIN", 403)
        audit("login", user=username, ip=ip)
        return self._json({"ok": True, "user": username},
                          cookie=self._session_cookie(token))

    def _tg_login(self):
        """Автовход через Telegram Mini App: проверяем подпись initData,
        находим/создаём аккаунт, привязанный к Telegram-ID, выдаём сессию."""
        ip = self._client_ip()
        if not rate_ok("tglogin:" + ip, 30, 900):
            return self._err("слишком много попыток — подождите", 429)
        if not TG_BOT_TOKEN:
            return self._err("вход через Telegram не настроен на сервере "
                             "(не задан TG_BOT_TOKEN)", 400)
        try:
            data = json.loads(self._body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err("плохой JSON")
        raw = data.get("initData") or ""
        tg_user, why = check_telegram_init_data(raw)
        if not tg_user:
            # Неудачный вход раньше нигде не фиксировался: в аудите были только
            # успехи, и «у меня просит инвайт-код» нечем было объяснить.
            audit("tg_login_fail", reason=why, ip=ip, initdata_len=len(raw),
                  ua=self.headers.get("User-Agent", "")[:80])
            return self._err("Telegram: " + (why or "подпись не проверена"), 403)
        tg_id = str(tg_user["id"])
        token = None
        username = None
        with _lock:
            db = db_load()
            for uname, urec in db["users"].items():
                if str(urec.get("tg_id") or "") == tg_id:
                    username = uname
                    break
            if not username:
                # доступ: если задан белый список — пускаем только его; иначе всем
                if TG_ALLOWED_IDS and tg_id not in TG_ALLOWED_IDS:
                    audit("tg_login_fail", reason="ID не в TG_ALLOWED_IDS",
                          tg_id=tg_id, ip=ip)
                    return self._err("Telegram ID " + tg_id + " не в списке "
                                     "разрешённых на сервере", 403)
                # роль: разработчик (dev) — только у админов; первый вошедший при
                # пустом списке админов становится владельцем-разработчиком,
                # все остальные входят как обычные участники (user)
                if tg_id in TG_ADMIN_IDS:
                    role = "dev"
                elif not TG_ADMIN_IDS and not db.get("_tg_owner_claimed"):
                    role = "dev"
                    db["_tg_owner_claimed"] = True
                else:
                    role = "user"
                username = "tg" + tg_id
                if username in db["users"] or user_exists_in_system(username):
                    username = "tg" + tg_id + secrets.token_hex(2)
                try:
                    create_linux_user(username)
                except subprocess.CalledProcessError as e:
                    return self._err("не удалось создать пользователя: " +
                                     e.stderr.decode(errors="replace")[:200], 500)
                pin = f"{secrets.randbelow(1_000_000):06d}"
                salt = secrets.token_hex(8)
                db["users"][username] = {
                    "salt": salt, "pin": hash_pin(pin, salt),
                    "role": role, "created": int(time.time()), "tg_id": tg_id,
                    "tg_name": (tg_user.get("username")
                                or tg_user.get("first_name") or "")[:40]}
                audit("tg_register", user=username, tg_id=tg_id, role=role, ip=ip)
            token = new_session(db, username, ip, self.headers.get("User-Agent", ""),
                                current=self._cookie_token())
            db_save(db)
        audit("tg_login", user=username, tg_id=tg_id, ip=ip)
        return self._json({"ok": True, "user": username},
                          cookie=self._session_cookie(token))

    # --- WebSocket-терминал ---
    def _handle_ws(self):
        # Origin проверяем ДО авторизации: соединение даёт интерактивный shell,
        # и открывать его по запросу с чужой страницы нельзя ни при каких куках.
        if not self._origin_ok():
            audit("ws_bad_origin", origin=self.headers.get("Origin", ""),
                  ip=self._client_ip())
            return self._err("запрос с чужого источника", 403)
        user = session_user(self)
        key = self.headers.get("Sec-WebSocket-Key")
        if not user or not key or \
           "websocket" not in (self.headers.get("Upgrade") or "").lower():
            return self._err("нужна авторизация", 401)

        # цель: локальный сервер или один из «своих серверов» пользователя
        q = parse_qs(urlparse(self.path).query)
        target = (q.get("target") or [""])[0]
        server = None
        if target and target != "local":
            server = get_server(user, target)
            if not server:
                return self._err("сервер не найден", 404)
        if not server and not ALLOW_LOCAL_TERMINAL:
            # общий сервер отключён — работа идёт на своём сервере пользователя
            return self._err("добавьте свой сервер — работа идёт на нём", 400)
        if server:
            # доступ проверяем ДО рукопожатия: после ответа 101 отдать
            # осмысленную ошибку по HTTP уже нельзя
            try:
                dec_secret(server["secret"])
            except (ValueError, binascii.Error, UnicodeDecodeError):
                return self._err("сохранённый доступ к серверу не читается — "
                                 "задайте его заново в разделе «Серверы»", 400)

        # рукопожатие
        resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n")
        self.connection.sendall(resp.encode())
        self.close_connection = True

        ssh_cleanup = None
        if server:
            pid, fd, ssh_cleanup = spawn_ssh(user, server)
        else:
            pid, fd = spawn_shell(user)
        send_lock = threading.Lock()
        alive = threading.Event()
        alive.set()

        def pump_pty():
            while alive.is_set():
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    ws_send(self.connection, send_lock, chunk, opcode=2)
                except OSError:
                    break
            alive.clear()
            try:
                ws_send(self.connection, send_lock, b"", opcode=8)
            except OSError:
                pass

        t = threading.Thread(target=pump_pty, daemon=True)
        t.start()

        try:
            while alive.is_set():
                frame = ws_read_frame(self.rfile)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 8:      # close
                    break
                if opcode == 9:      # ping -> pong
                    ws_send(self.connection, send_lock, payload, opcode=10)
                    continue
                if opcode != 1:      # ждём текстовые JSON-сообщения
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if msg.get("t") == "i":
                    try:
                        os.write(fd, msg.get("d", "").encode())
                    except OSError:
                        break
                elif msg.get("t") == "r":
                    try:
                        set_winsize(fd, int(msg.get("rows", 24)),
                                    int(msg.get("cols", 80)))
                    except (OSError, ValueError):
                        pass
                elif msg.get("t") == "ka":
                    # отвечаем на keep-alive текстовым pong — по нему клиент
                    # понимает, что соединение живое; молчание = «зомби»-сокет,
                    # клиент сам переподключится (иначе консоль висит после
                    # рестарта сервера за прокси, где TCP-close не доходит)
                    try:
                        ws_send(self.connection, send_lock,
                                b'{"t":"ka"}', opcode=1)
                    except OSError:
                        break
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # клиент оборвал соединение — просто прибираемся
        finally:
            alive.clear()
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            if ssh_cleanup:
                ssh_cleanup()

            def reap():
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            threading.Thread(target=reap, daemon=True).start()


def main():
    if not DEV_INVITE and not OPEN_SIGNUP:
        raise SystemExit("Задайте IDE_DEV_INVITE (инвайт разработчиков) "
                         "или включите IDE_OPEN_SIGNUP=1")
    if os.geteuid() != 0:
        raise SystemExit("Запускать под root: нужен для создания пользователей")
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    moved = migrate_secrets()
    if moved:
        print(f"секретов переведено в формат с проверкой целостности: {moved}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"claude-ide слушает {HOST}:{PORT}, статика: {STATIC_DIR}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

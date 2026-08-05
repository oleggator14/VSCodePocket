#!/usr/bin/env python3
"""
CodePocket: агент Claude, работающий с проектом пользователя.

Только стандартная библиотека. Вызывает Anthropic Messages API напрямую
через urllib. Инструменты агента: список файлов, чтение, запись, запуск команд
в папке проекта под учёткой пользователя.
"""

import json
import os
import pwd
import subprocess
import urllib.request
import urllib.error

API_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CP_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("CP_MAX_TOKENS", "16000"))
MAX_STEPS = int(os.environ.get("CP_MAX_STEPS", "12"))      # итераций цикла на 1 сообщение
CMD_TIMEOUT = int(os.environ.get("CP_CMD_TIMEOUT", "60"))  # сек на команду
OUT_LIMIT = 12_000                                          # символов вывода команды
FILE_LIMIT = 48_000                                         # символов файла для чтения

SYSTEM_PROMPT = (
    "Ты — Claude, встроенный в мобильную IDE CodePocket. Пользователь пишет с телефона, "
    "поэтому отвечай кратко и по делу, по-русски. Ты работаешь с файлами проекта через "
    "инструменты: смотри файлы, вноси правки, запускай код и проверяй результат сам. "
    "Меняя файл, пиши его целиком через write_file. После правок по возможности запусти "
    "код или тесты, чтобы убедиться, что всё работает. Не выходи за пределы папки проекта."
)

TOOLS = [
    {"name": "list_files", "description": "Список всех файлов проекта (рекурсивно).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "Прочитать файл проекта.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "путь относительно папки проекта"}},
         "required": ["path"]}},
    {"name": "write_file", "description": "Записать файл целиком (создаёт при отсутствии).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "run_command", "description":
        f"Выполнить shell-команду в папке проекта (таймаут {CMD_TIMEOUT}с). "
        "Подходит для запуска кода, установки пакетов, git.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
]


def _safe(base, rel):
    rel = (rel or "").lstrip("/")
    t = os.path.realpath(os.path.join(base, rel))
    b = os.path.realpath(base)
    if t != b and not t.startswith(b + os.sep):
        raise PermissionError("путь вне папки проекта")
    return t


def _demote(username):
    p = pwd.getpwnam(username)

    def fn():
        os.initgroups(username, p.pw_gid)
        os.setgid(p.pw_gid)
        os.setuid(p.pw_uid)
    return fn, p


def _as_user(username, fn, *args):
    """Выполняет fn в дочернем процессе под учёткой пользователя и возвращает
    её результат (строку).

    Нужно для чтения и записи файлов. Проверка пути через _safe() сама по себе
    не защищает: между realpath() и open() пользователь — а shell у него есть —
    успевает подменить каталог символьной ссылкой, и root запишет файл куда
    угодно. Под правами самого пользователя такая подмена ничего не даёт: ядро
    проверит доступ ещё раз, уже по-настоящему."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                                  # ребёнок
        code = 1
        try:
            os.close(r)
            _demote(username)[0]()
            os.write(w, fn(*args).encode()[:200_000])
            code = 0
        except BaseException as e:
            try:
                os.write(w, ("ОШИБКА: %s" % e).encode()[:4000])
            except OSError:
                pass
        finally:
            try:
                os.close(w)
            except OSError:
                pass
            os._exit(code)
    os.close(w)
    chunks = []
    with os.fdopen(r, "rb") as f:
        while True:
            b = f.read(65536)
            if not b:
                break
            chunks.append(b)
    os.waitpid(pid, 0)
    return b"".join(chunks).decode("utf-8", errors="replace")


class ProjectTools:
    """Исполнение инструментов в папке проекта под учёткой пользователя."""

    def __init__(self, username, project_dir):
        self.user = username
        self.dir = project_dir

    def list_files(self, _inp):
        out = []
        for root, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if d not in
                       {".git", "__pycache__", "node_modules", ".venv"}]
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.dir))
            if len(out) > 400:
                out.append("... (обрезано)")
                break
        return "\n".join(sorted(out)) or "(проект пуст)"

    def read_file(self, inp):
        rel = inp.get("path", "")
        _safe(self.dir, rel)          # ранний отказ с понятным текстом

        def do():
            p = _safe(self.dir, rel)  # повторно, уже без прав root
            if not os.path.isfile(p):
                return f"ОШИБКА: файла нет: {rel}"
            with open(p, "rb") as f:
                raw = f.read(FILE_LIMIT * 4)
            if b"\x00" in raw[:8192]:
                return "ОШИБКА: бинарный файл"
            text = raw.decode("utf-8", errors="replace")
            return text[:FILE_LIMIT] + ("\n...(обрезано)" if len(text) > FILE_LIMIT else "")

        return _as_user(self.user, do)

    def write_file(self, inp):
        rel = inp.get("path", "")
        content = inp.get("content", "")
        _safe(self.dir, rel)

        def do():
            p = _safe(self.dir, rel)
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            # O_NOFOLLOW: если на месте файла оказалась ссылка наружу —
            # отказываемся, а не идём по ней
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                         0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            return f"OK: записано {len(content)} символов в {rel}"

        return _as_user(self.user, do)

    def run_command(self, inp):
        cmd = inp.get("command", "")
        fn, p = _demote(self.user)
        env = {"HOME": p.pw_dir, "USER": self.user, "PATH":
               f"{p.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8"}
        try:
            r = subprocess.run(["bash", "-c", cmd], cwd=self.dir, env=env,
                               preexec_fn=fn, capture_output=True,
                               timeout=CMD_TIMEOUT, text=True, errors="replace")
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            out = out[:OUT_LIMIT] + ("\n...(обрезано)" if len(out) > OUT_LIMIT else "")
            return f"[код выхода {r.returncode}]\n{out or '(нет вывода)'}"
        except subprocess.TimeoutExpired:
            return f"ОШИБКА: команда не уложилась в {CMD_TIMEOUT} секунд и была прервана"

    def execute(self, name, inp):
        try:
            fn = {"list_files": self.list_files, "read_file": self.read_file,
                  "write_file": self.write_file, "run_command": self.run_command}.get(name)
            if not fn:
                return f"ОШИБКА: неизвестный инструмент {name}"
            return fn(inp)
        except PermissionError as e:
            return f"ОШИБКА: {e}"
        except OSError as e:
            return f"ОШИБКА: {e}"


def _api_call(messages):
    """Один вызов Anthropic Messages API. Возвращает распарсенный JSON."""
    body = json.dumps({
        "model": MODEL,
        # у текущих моделей размышления включены по умолчанию и считаются в
        # тот же лимит, что и ответ: при 4096 ответ обрывался на полуслове
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": TOOLS,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        API_BASE.rstrip("/") + "/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "x-api-key": API_KEY,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Anthropic API: HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"нет связи с Anthropic API: {e.reason}")


def run_agent(username, project_dir, history, user_message,
              should_stop=None, images=None):
    """
    Агентный цикл. history — список сообщений в формате API (продолжаем его).
    should_stop — функция без аргументов: вернула True → прерываем цикл (кнопка
    «Стоп» в приложении). images — [{'media_type','data'}] для картинок-вложений.
    Возвращает (steps_ui, history, usage) где steps_ui — события для интерфейса,
    usage — {'in': n, 'out': n} суммарно за это сообщение.
    """
    if not API_KEY:
        raise RuntimeError("на сервере не задан ANTHROPIC_API_KEY")

    tools = ProjectTools(username, project_dir)
    if images:
        content = [{"type": "image",
                    "source": {"type": "base64",
                               "media_type": im.get("media_type", "image/jpeg"),
                               "data": im.get("data", "")}}
                   for im in images[:5] if im.get("data")]
        content.append({"type": "text", "text": user_message})
        history = list(history) + [{"role": "user", "content": content}]
    else:
        history = list(history) + [{"role": "user", "content": user_message}]
    steps, usage = [], {"in": 0, "out": 0}
    stopped = False

    for _ in range(MAX_STEPS):
        if should_stop and should_stop():
            stopped = True
            break
        resp = _api_call(history)
        u = resp.get("usage", {})
        usage["in"] += u.get("input_tokens", 0)
        usage["out"] += u.get("output_tokens", 0)

        content = resp.get("content", [])
        history.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                steps.append({"type": "text", "text": b["text"]})

        if resp.get("stop_reason") != "tool_use" or not tool_uses:
            break

        results = []
        for tu in tool_uses:
            if should_stop and should_stop():
                stopped = True
                break
            name, inp = tu.get("name"), tu.get("input") or {}
            result = tools.execute(name, inp)
            label = {
                "list_files": "📂 посмотрел файлы проекта",
                "read_file": f"👀 прочитал {inp.get('path', '')}",
                "write_file": f"📝 изменил {inp.get('path', '')}",
                "run_command": f"▶ выполнил: {inp.get('command', '')[:80]}",
            }.get(name, name)
            preview = ""
            if name == "run_command":
                preview = result[:400]
            steps.append({"type": "tool", "label": label, "preview": preview})
            results.append({"type": "tool_result", "tool_use_id": tu.get("id"),
                            "content": result})
        if stopped:
            break
        history.append({"role": "user", "content": results})
    else:
        steps.append({"type": "text",
                      "text": "(достиг лимита шагов за одно сообщение — продолжите запросом «продолжай»)"})

    if stopped:
        steps.append({"type": "error", "text": "⏹ Остановлено вами"})

    return steps, history, usage

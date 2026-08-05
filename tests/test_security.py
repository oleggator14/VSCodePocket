#!/usr/bin/env python3
"""Смоук-тесты защитных мест CodePocket.

Только стандартная библиотека, как и весь проект. Запуск из корня репозитория:

    python3 -m unittest discover -s tests -v

Тесты покрывают то, где ошибка стоит дороже всего: границы путей, шифрование
секретов, роли, доверие к заголовкам от прокси и права на файлы с секретами.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

# каждый прогон — своя папка данных: тесты не должны видеть боевую базу
_DATA = tempfile.mkdtemp(prefix="cptest_")
os.environ["IDE_DATA_DIR"] = _DATA
os.environ.setdefault("IDE_DEV_INVITE", "test-invite")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


class SafePath(unittest.TestCase):
    """Выход за пределы домашней папки должен отбиваться."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cphome_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_normal_paths_resolve_inside_home(self):
        self.assertEqual(server.safe_path(self.home, "projects/a.py"),
                         os.path.join(os.path.realpath(self.home), "projects/a.py"))
        self.assertEqual(server.safe_path(self.home, "/projects/a.py"),
                         os.path.join(os.path.realpath(self.home), "projects/a.py"))

    def test_dotdot_is_rejected(self):
        for bad in ("../etc/passwd", "a/../../etc/passwd", "../../root/.ssh/id_rsa"):
            with self.assertRaises(PermissionError, msg=bad):
                server.safe_path(self.home, bad)

    def test_symlink_out_is_rejected(self):
        os.symlink("/etc", os.path.join(self.home, "escape"))
        with self.assertRaises(PermissionError):
            server.safe_path(self.home, "escape/passwd")

    def test_sibling_dir_with_shared_prefix_is_rejected(self):
        # /home/bob-evil не должен считаться «внутри» /home/bob
        sibling = self.home + "-evil"
        os.makedirs(sibling, exist_ok=True)
        self.addCleanup(shutil.rmtree, sibling, ignore_errors=True)
        with self.assertRaises(PermissionError):
            server.safe_path(self.home, "../" + os.path.basename(sibling))


class Secrets(unittest.TestCase):
    """Шифрование доступов к серверам: encrypt-then-MAC, подделка не проходит."""

    def test_roundtrip(self):
        for text in ("hunter2", "", "ключ с юникодом ✓", "x" * 5000):
            self.assertEqual(server.dec_secret(server.enc_secret(text)), text)

    def test_ciphertext_differs_each_time(self):
        # nonce случайный — одинаковый текст не даёт одинаковый шифротекст
        self.assertNotEqual(server.enc_secret("same"), server.enc_secret("same"))

    def test_tampered_tag_is_rejected(self):
        import base64
        raw = bytearray(base64.b64decode(server.enc_secret("секрет")))
        raw[-1] ^= 0x01                       # портим последний байт HMAC
        with self.assertRaises(ValueError):
            server.dec_secret(base64.b64encode(bytes(raw)).decode())

    def test_tampered_ciphertext_is_rejected(self):
        import base64
        raw = bytearray(base64.b64decode(server.enc_secret("секрет")))
        raw[20] ^= 0x01                       # портим сам шифротекст
        with self.assertRaises(ValueError):
            server.dec_secret(base64.b64encode(bytes(raw)).decode())

    def test_legacy_format_is_not_silently_accepted(self):
        # старый формат без тега больше не читается напрямую — только миграцией
        import base64
        import secrets as _s
        nonce = _s.token_bytes(16)
        ct = bytes(a ^ b for a, b in
                   zip(b"legacy", server._keystream(nonce, 6)))
        with self.assertRaises(ValueError):
            server.dec_secret(base64.b64encode(nonce + ct).decode())


class ChatPaths(unittest.TestCase):
    """Имя агента и проекта попадают в путь — оба должны быть ограничены."""

    def test_known_agents(self):
        self.assertEqual(server.norm_agent("claude"), "claude")
        self.assertEqual(server.norm_agent("codex"), "codex")

    def test_unknown_agent_falls_back(self):
        for bad in ("../../etc/passwd", "", None, "GPT", "claude/../x"):
            self.assertEqual(server.norm_agent(bad), "claude")

    def test_path_stays_in_chats_dir(self):
        base = os.path.realpath(os.path.join(_DATA, "chats", "bob"))
        for agent in ("claude", "codex", "../../../etc/cron.d/pwn"):
            p = os.path.realpath(server.chat_path("bob", "proj", agent))
            self.assertTrue(p.startswith(base + os.sep), p)

    def test_bad_project_name_is_rejected(self):
        for bad in ("../etc", "a/b", "", "проект", ".hidden"):
            with self.assertRaises(ValueError, msg=bad):
                server.chat_path("bob", bad, "claude")


class Roles(unittest.TestCase):
    """Запись без роли не должна давать права разработчика."""

    def test_missing_role_is_plain_user(self):
        db = {"users": {"bob": {"salt": "x", "pin": "y"}}, "sessions": {}}
        self.assertEqual(server.user_role(db, "bob"), "user")

    def test_unknown_user_is_plain_user(self):
        self.assertEqual(server.user_role({"users": {}, "sessions": {}}, "ghost"),
                         "user")

    def test_explicit_dev_is_kept(self):
        db = {"users": {"ann": {"role": "dev"}}, "sessions": {}}
        self.assertEqual(server.user_role(db, "ann"), "dev")

    def test_quota_follows_role(self):
        self.assertEqual(server.role_quota("dev"), server.DAILY_TOKENS_DEV)
        self.assertEqual(server.role_quota("user"), server.DAILY_TOKENS_USER)
        self.assertEqual(server.role_quota(None), server.DAILY_TOKENS_USER)


class TrustedProxy(unittest.TestCase):
    """X-Forwarded-For принимаем только от локального Caddy."""

    def test_loopback_is_trusted(self):
        self.assertTrue(server._is_trusted_proxy("127.0.0.1"))
        self.assertTrue(server._is_trusted_proxy("::1"))

    def test_outside_is_not_trusted(self):
        for ip in ("8.8.8.8", "10.0.0.5", "192.168.1.1", "", "не-адрес"):
            self.assertFalse(server._is_trusted_proxy(ip), ip)


class OriginCheck(unittest.TestCase):
    """Отсекаем только конкретный чужой источник. Пустой и `null` проходят —
    иначе ломается вход из Telegram Mini App, который живёт в песочнице."""

    def _h(self, origin, host="code.example.com", https=True):
        """Handler без сокета: подставляем только заголовки."""
        h = server.Handler.__new__(server.Handler)
        hdrs = {"Host": host}
        if https:
            hdrs["X-Forwarded-Proto"] = "https"
        if origin is not None:
            hdrs["Origin"] = origin
        h.headers = hdrs
        return h

    def test_same_origin_passes(self):
        # порт по умолчанию подразумевается схемой: с :443 и без — одно и то же
        for o in ("https://code.example.com", "https://code.example.com:443"):
            self.assertTrue(server.Handler._origin_ok(self._h(o)), o)
        # на http-странице так же работает 80-й
        self.assertTrue(server.Handler._origin_ok(
            self._h("http://code.example.com", https=False)))

    def test_plain_http_origin_on_https_page_is_rejected(self):
        # схема — часть источника: http://site и https://site разные
        self.assertFalse(server.Handler._origin_ok(
            self._h("http://code.example.com", https=True)))

    def test_absent_and_null_pass(self):
        # не-браузерный клиент и Telegram Mini App в песочнице
        self.assertTrue(server.Handler._origin_ok(self._h(None)))
        self.assertTrue(server.Handler._origin_ok(self._h("null")))
        self.assertTrue(server.Handler._origin_ok(self._h("")))

    def test_telegram_origins_pass(self):
        for o in ("https://web.telegram.org", "https://k.web.telegram.org",
                  "https://telegram.org"):
            self.assertTrue(server.Handler._origin_ok(self._h(o)), o)

    def test_foreign_origin_is_rejected(self):
        for o in ("https://evil.example", "http://code.example.com.evil.net",
                  "https://telegram.org.evil.net", "https://nottelegram.org"):
            self.assertFalse(server.Handler._origin_ok(self._h(o)), o)

    def test_other_port_is_rejected(self):
        self.assertFalse(server.Handler._origin_ok(
            self._h("https://code.example.com:8443", host="code.example.com:443")))


class PrivateFiles(unittest.TestCase):
    """Файлы с секретами создаются сразу с правами 0600."""

    def test_mode_is_600_and_content_matches(self):
        path = os.path.join(_DATA, "sub", "secret.json")
        server.write_private(path, json.dumps({"pin": "1234"}))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(json.load(f)["pin"], "1234")

    def test_no_temp_file_left_behind(self):
        path = os.path.join(_DATA, "atomic.json")
        server.write_private(path, "{}")
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_overwrite_keeps_mode(self):
        path = os.path.join(_DATA, "twice.json")
        server.write_private(path, "1")
        server.write_private(path, "2")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(f.read(), "2")


class MuxDir(unittest.TestCase):
    """Каталог управляющих сокетов SSH — только root, вне /tmp."""

    def test_lives_in_data_dir_and_is_locked_down(self):
        d = server._mux_dir()
        if d is None:
            self.skipTest("каталог не создался (нет прав) — нечего проверять")
        # ключевое: путь внутри DATA_DIR, а не в общем /tmp с предсказуемым именем
        self.assertEqual(os.path.dirname(d), server.DATA_DIR)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(os.stat(d).st_uid, os.getuid())

    def test_group_and_other_have_no_access(self):
        d = server._mux_dir()
        if d is None:
            self.skipTest("каталог не создался (нет прав)")
        self.assertEqual(os.stat(d).st_mode & 0o077, 0)


class Usernames(unittest.TestCase):
    """Имя пользователя становится Linux-учёткой — проверка должна быть строгой."""

    def test_valid(self):
        for ok in ("bob", "ann-2", "user_name", "a1b"):
            self.assertTrue(server.USERNAME_RE.match(ok), ok)

    def test_invalid(self):
        for bad in ("ab", "-bob", "1bob", "Bob", "bob!", "боб", "a" * 22,
                    "bob/../root", "bob bob"):
            self.assertFalse(server.USERNAME_RE.match(bad), bad)

    def test_system_names_reserved(self):
        for name in ("root", "daemon", "www-data", "sshd"):
            self.assertIn(name, server.RESERVED)


class ProjectNames(unittest.TestCase):
    def test_valid(self):
        for ok in ("app", "my-proj", "a_b1", "X"):
            self.assertTrue(server.PROJECT_RE.match(ok), ok)

    def test_invalid(self):
        for bad in ("../x", "a/b", "1app", "", ".git", "app.py", "a" * 32):
            self.assertFalse(server.PROJECT_RE.match(bad), bad)


class RemotePaths(unittest.TestCase):
    """Относительные пути на удалённом сервере."""

    def test_normalizes(self):
        self.assertEqual(server._rel_ok("/a/./b"), "a/b")
        self.assertEqual(server._rel_ok("a\\b"), "a/b")
        self.assertEqual(server._rel_ok(""), "")

    def test_rejects_dotdot(self):
        for bad in ("../etc", "a/../../b", "..", "a/.."):
            with self.assertRaises(ValueError, msg=bad):
                server._rel_ok(bad)


class RateLimit(unittest.TestCase):
    def test_blocks_after_limit(self):
        key = "test:" + os.urandom(4).hex()
        self.assertTrue(all(server.rate_ok(key, 3, 60) for _ in range(3)))
        self.assertFalse(server.rate_ok(key, 3, 60))


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Проверка фронтенда: скрипт приложения должен доживать до конца.

Весь код приложения — ОДИН инлайновый скрипт. Любая ошибка на верхнем уровне
убивает его целиком: разметка (включая форму входа) уже отрисована и остаётся
на экране, а ни один запрос на сервер не уходит. Снаружи это выглядит как
«приложение просит инвайт-код» и неотличимо от проблемы со входом.

Ровно так и случилось: функция aiBlocked() читала переменную TARGET, которая
объявлена через let НИЖЕ по файлу, а вызывалась выше — обращение к let-биндингу
до его объявления бросает ReferenceError (временная мёртвая зона), и приложение
умирало на старте.

Тест исполняет скрипт в Node с заглушкой DOM и требует, чтобы он дошёл до
конца. Пропускается, если Node недоступен.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")

# Заглушка браузера: ровно столько, чтобы скрипт мог выполниться. Нам не нужна
# работающая страница — нужно поймать ошибки уровня «скрипт не дожил до конца».
STUB = r"""
// Универсальная заглушка: неизвестное свойство отдаёт функцию-пустышку,
// которая сама себя возвращает. Так скрипт может звать что угодно и на чём
// угодно, не падая — а нас интересует ровно одно: дожил ли он до конца.
const noop = function(){ return undefined; };
function auto(seed){
  const base = Object.assign({}, seed || {});
  const fn = function(){ return auto(); };
  return new Proxy(fn, {
    get(t, k){
      if (k === Symbol.toPrimitive) return () => '';
      if (k === 'then') return undefined;            // не притворяемся промисом
      if (k === Symbol.iterator) return function*(){};
      if (k in base) return base[k];
      if (typeof k === 'symbol') return undefined;
      return auto();
    },
    set(t, k, v){ base[k] = v; return true; },
    has(){ return true; },
    apply(){ return auto(); },
    construct(){ return auto(); },
  });
}
function el(id){
  return auto({
    id:id, tagName:'DIV', type:'', value:'', textContent:'', innerHTML:'',
    dataset:{}, files:[], checked:false, disabled:false, rows:1,
    scrollTop:0, scrollHeight:0, clientHeight:0, offsetWidth:0, offsetHeight:0,
    children:[], childNodes:[], parentNode:null, nextSibling:null,
    classList: auto({contains:()=>false}),
    style: auto({}),
    appendChild:(c)=>c, querySelectorAll:()=>[], closest:()=>null,
    getAttribute:()=>null, hasAttribute:()=>false,
    getBoundingClientRect:()=>({top:0,bottom:0,left:0,right:0,width:0,height:0}),
  });
}
global.window = global;
global.addEventListener = noop; global.removeEventListener = noop;
global.dispatchEvent = noop; global.scrollTo = noop; global.open = noop;
global.innerWidth = 390; global.innerHeight = 844; global.scrollY = 0; global.scrollX = 0;
global.devicePixelRatio = 2;
global.btoa = s=>Buffer.from(s,'binary').toString('base64');
global.atob = s=>Buffer.from(s,'base64').toString('binary');
global.document = auto({
  getElementById:id=>el(id), createElement:t=>el(t),
  querySelector:()=>el('q'), querySelectorAll:()=>[],
  addEventListener:noop, removeEventListener:noop,
  body:el('body'), documentElement:el('html'), head:el('head'),
  createTextNode:t=>({textContent:t}),
  hidden:false, visibilityState:'visible', activeElement:el('a'), cookie:'',
});
global.location = {href:'https://x/', pathname:'/', hash:'', search:'',
                   host:'x', hostname:'x', protocol:'https:', origin:'https://x',
                   reload:noop, replace:noop, assign:noop};
global.history = auto({});
global.navigator = auto({userAgent:'node', platform:'node', language:'ru'});
global.localStorage = global.sessionStorage = {
  _d:{}, getItem(k){return this._d[k]===undefined?null:this._d[k];},
  setItem(k,v){this._d[k]=String(v);}, removeItem(k){delete this._d[k];}, clear(){this._d={};}
};
global.fetch = () => new Promise(()=>{});          // запросы не завершаем
global.XMLHttpRequest = function(){ return auto({}); };
global.WebSocket = function(){ return auto({}); };
global.requestAnimationFrame = () => 0;
global.matchMedia = () => auto({matches:false});
global.visualViewport = {height:844, offsetTop:0, addEventListener:noop};
global.getComputedStyle = () => auto({});
global.alert = global.confirm = global.prompt = noop;
global.setTimeout = () => 0;                       // таймеры не запускаем
global.setInterval = () => 0;
global.clearTimeout = global.clearInterval = noop;
global.Telegram = undefined;
global.FileReader = function(){ return auto({}); };
global.Image = function(){ return el('img'); };
global.Blob = function(){ return auto({}); };
global.URL = global.URL || auto({});
// библиотеки, которые в проде приходят из vendor/
global.CodeMirror = auto({fromTextArea:()=>auto({getValue:()=>''}), commands:{}});
global.Terminal = function(){ return auto({cols:80, rows:24}); };
global.FitAddon = auto({});
"""


def node_available():
    return shutil.which("node") is not None


def inline_script():
    with open(INDEX, encoding="utf-8") as fh:
        html = fh.read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        html, re.S)
    return blocks


def js_functions(*sigs):
    """Вырезает названные функции из инлайнового скрипта, чтобы проверять их
    в Node по отдельности."""
    src = inline_script()[0]
    out = []
    for sig in sigs:
        i = src.index(sig)
        if sig.startswith("const"):
            out.append(src[i:src.index("\n", i) + 1])
        else:
            out.append(src[i:src.index("\n}", i) + 2] + "\n")
    return "\n".join(out)


@unittest.skipUnless(node_available(), "нет node — проверка пропущена")
class FrontendBoots(unittest.TestCase):

    def test_single_inline_script(self):
        # если скриптов станет несколько, ошибка перестанет валить всё разом —
        # но и этот тест надо будет переписать, поэтому фиксируем ожидание
        self.assertEqual(len(inline_script()), 1,
                         "ожидался один инлайновый скрипт")

    def test_script_runs_to_completion(self):
        code = inline_script()[0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "run.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(STUB)
                f.write("\ntry{\n")
                f.write(code)
                f.write("\n}catch(e){ console.error('THREW:'+e.constructor.name"
                        "+': '+e.message); process.exit(3); }\n")
                f.write("console.log('OK');\n")
            r = subprocess.run(["node", path], capture_output=True,
                               text=True, timeout=60)
        if r.returncode == 3:
            self.fail("скрипт приложения упал на верхнем уровне:\n  " +
                      (r.stderr.strip().splitlines() or [""])[-1])
        self.assertEqual(r.returncode, 0,
                         "node вышел с кодом %d:\n%s" % (r.returncode,
                                                         r.stderr[-800:]))
        self.assertIn("OK", r.stdout)

    def test_no_use_before_let_declaration(self):
        """Грубая проверка на ту же ловушку: верхнеуровневый вызов функции,
        читающей let-переменную, объявленную ниже."""
        code = inline_script()[0]
        lets = {}
        for m in re.finditer(r"^(?:let|const)\s+([A-Za-z_$][\w$]*)", code, re.M):
            lets.setdefault(m.group(1), m.start())
        # функции, объявленные до своей let-переменной, но вызванные на верхнем
        # уровне раньше объявления — исполняемый тест выше поймает это точнее;
        # здесь лишь фиксируем, что список переменных вообще разобрался
        self.assertIn("TARGET", lets, "не нашли объявление TARGET — тест устарел")



@unittest.skipUnless(node_available(), "нет node — проверка пропущена")
class KeyboardState(unittest.TestCase):
    """Признак «клавиатура на экране» ломался трижды, поэтому проверяем его
    отдельно и в обеих средах:

      Safari:   window.innerHeight остаётся полной, просаживается только
                visualViewport.height;
      Telegram: window.innerHeight просаживается ВМЕСТЕ с ней — сравнение с
                innerHeight даёт ноль, и класс не ставится вовсе. Именно так
                панель клавиш оставалась на экране и наезжала на нижнее меню.
    """

    def _run(self, script):
        code = inline_script()[0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "kbd.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(STUB)
                f.write("\nvar CLS=new Set();\n")
                f.write("document.body.classList={add:c=>CLS.add(c),"
                        "remove:c=>CLS.delete(c),contains:c=>CLS.has(c),"
                        "toggle:(c,on)=>{on?CLS.add(c):CLS.delete(c);}};\n")
                f.write("try{\n" + code + "\n}catch(e){"
                        "console.error('THREW:'+e.message);process.exit(3);}\n")
                f.write(script)
            return subprocess.run(["node", path], capture_output=True,
                                  text=True, timeout=60)

    def test_safari_like(self):
        # полный экран 800, клавиатура забирает 300 -> видимая часть 500
        r = self._run(
            "global.innerHeight=800;\n"
            "updateKbdState(800); console.log('idle:'+CLS.has('kbd'));\n"
            "updateKbdState(500); console.log('open:'+CLS.has('kbd'));\n"
            "updateKbdState(800); console.log('closed:'+CLS.has('kbd'));\n")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("idle:false", r.stdout)
        self.assertIn("open:true", r.stdout)
        self.assertIn("closed:false", r.stdout)

    def test_telegram_like_innerheight_also_shrinks(self):
        # тут innerHeight просаживается вместе с видимой высотой
        r = self._run(
            "global.innerHeight=800;\n"
            "updateKbdState(800); console.log('idle:'+CLS.has('kbd'));\n"
            "global.innerHeight=500;\n"
            "updateKbdState(500); console.log('open:'+CLS.has('kbd'));\n"
            "global.innerHeight=800;\n"
            "updateKbdState(800); console.log('closed:'+CLS.has('kbd'));\n")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("idle:false", r.stdout)
        self.assertIn("open:true", r.stdout,
                      "класс не поставился — ровно тот сбой, что был в Telegram")
        self.assertIn("closed:false", r.stdout)

    def test_stays_open_while_keyboard_slides_away(self):
        # фокус уже ушёл, но место ещё занято: панель не должна вернуться
        r = self._run(
            "global.innerHeight=800;\n"
            "updateKbdState(800);\n"
            "updateKbdState(500);\n"
            "updateKbdState(560); console.log('mid:'+CLS.has('kbd'));\n"
            "updateKbdState(800); console.log('done:'+CLS.has('kbd'));\n")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("mid:true", r.stdout,
                      "класс снялся, пока клавиатура ещё уезжала")
        self.assertIn("done:false", r.stdout)

    def test_terminal_focus_keeps_kbd_state(self):
        """Касание терминала уводит фокус в его СКРЫТОЕ поле. Настоящим полем
        оно не считается — но клавиатура при этом на экране, и панели вылезать
        не должны. Раньше состояние снималось, и поверх клавиатуры выезжали
        нижнее меню и панель клавиш."""
        r = self._run(
            "global.innerHeight=800;\n"
            "updateKbdState(800);\n"
            "updateKbdState(500); console.log('typing:'+CLS.has('kbd'));\n"
            # фокус ушёл в скрытое поле терминала: настоящего поля нет,
            # но высота всё ещё просевшая
            "updateKbdState(500); console.log('terminal:'+CLS.has('kbd'));\n"
            "updateKbdState(800); console.log('closed:'+CLS.has('kbd'));\n")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("typing:true", r.stdout)
        self.assertIn("terminal:true", r.stdout,
                      "панели вылезли поверх открытой клавиатуры")
        self.assertIn("closed:false", r.stdout)


@unittest.skipUnless(node_available(), "нет node — проверка пропущена")
class ConsoleLinks(unittest.TestCase):
    """Ссылку для входа в агента нельзя было скопировать: терминал разбивает
    длинную строку на несколько экранных, а мы соединяли их переводом строки —
    URL рвался пополам. Проверяем, что склейка возвращает его целиком."""

    URL = ("https://claude.ai/oauth/authorize?client_id=9d1c250a"
           "&response_type=code&redirect_uri=https%3A%2F%2Fconsole.anthropic.com"
           "%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key"
           "&code_challenge=AbCdEf123456")

    def _run(self, rows_js, tail):
        src = inline_script()[0]
        text_fn = re.search(r"function consoleText\(\)\{[\s\S]*?\n\}", src).group(0)
        # consoleLinks опирается на помощников склейки — берём их тоже,
        # иначе тест падает не на логике, а на отсутствии функции
        links_fn = js_functions("const BOXCHARS", "function deboxLine",
                                "function looksLikeUrlTail",
                                "function joinBrokenUrls",
                                "function consoleLinks")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "links.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(rows_js)
                f.write(text_fn.replace("const buf=term.buffer.active;", "const buf=BUF;"))
                f.write("\n" + links_fn + "\n")
                f.write(tail)
            return subprocess.run(["node", path], capture_output=True,
                                  text=True, timeout=30)

    def _rows(self, url, width=58):
        parts = [url[i:i + width] for i in range(0, len(url), width)]
        js = ["const R=[{t:'Use the url below to sign in:',w:false}];"]
        for i, p in enumerate(parts):
            js.append("R.push({t:%r,w:%s});" % (p, "true" if i else "false"))
        js.append("R.push({t:'Paste code here >',w:false});")
        js.append("global.BUF={length:R.length,getLine:i=>"
                  "({translateToString:()=>R[i].t,isWrapped:R[i].w})};")
        return "\n".join(js).replace("'", '"') + "\n"

    def test_wrapped_url_is_joined_back(self):
        r = self._run(self._rows(self.URL),
                      "const L=consoleLinks(consoleText());"
                      "console.log(L.length===1 && L[0]===%s ? 'OK' : 'BAD:'+L[0]);"
                      % ('"' + self.URL + '"'))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("OK", r.stdout,
                      "ссылка собралась неверно: " + r.stdout.strip())

    def test_no_links_gives_empty(self):
        rows = ("global.BUF={length:2,getLine:i=>({translateToString:()=>"
                '["hello","world"][i],isWrapped:false})};\n')
        r = self._run(rows, "console.log('N='+consoleLinks(consoleText()).length);")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("N=0", r.stdout)

    def test_trailing_punctuation_trimmed(self):
        rows = ("global.BUF={length:1,getLine:()=>({translateToString:()=>"
                '"open https://example.com/auth?a=1.",isWrapped:false})};\n')
        r = self._run(rows, "console.log('U='+consoleLinks(consoleText())[0]);")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("U=https://example.com/auth?a=1", r.stdout)
        self.assertNotIn("a=1.", r.stdout)


@unittest.skipUnless(node_available(), "нет node — проверка пропущена")
class BrokenUrlJoin(unittest.TestCase):
    """Ссылка входа рвалась по строкам. Признака переноса недостаточно:
    полноэкранные программы (Claude Code, Codex) рисуют каждую визуальную
    строку отдельно и обрамляют рамкой — флага переноса на них нет, и URL
    обрывался там, где кончалась строка (у пользователя — на «code=»)."""

    def _fns(self):
        return js_functions("const BOXCHARS", "function deboxLine",
                            "function looksLikeUrlTail", "function joinBrokenUrls",
                            "function consoleLinks")

    def _links(self, lines):
        js = self._fns() + ("\nconsole.log(JSON.stringify(consoleLinks(%s.join('\\n'))));"
                            % json.dumps(lines))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(js)
            r = subprocess.run(["node", path], capture_output=True, text=True,
                               timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    URL = ("https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a"
           "&response_type=code&scope=org%3Acreate_api_key")

    def test_boxed_url_broken_at_code(self):
        got = self._links([
            "\u2502 https://claude.ai/oauth/authorize?code=  \u2502",
            "\u2502 true&client_id=9d1c250a&response_type=c  \u2502",
            "\u2502 ode&scope=org%3Acreate_api_key           \u2502",
        ])
        self.assertEqual(got[:1], [self.URL])

    def test_two_urls_do_not_merge(self):
        got = self._links(["https://a.example/one", "https://b.example/two"])
        self.assertEqual(sorted(got),
                         ["https://a.example/one", "https://b.example/two"],
                         "две отдельные ссылки склеились в одну")

    def test_prose_after_url_not_appended(self):
        got = self._links(["https://example.com/a?x=1",
                           "Paste code here if prompted"])
        self.assertEqual(got[:1], ["https://example.com/a?x=1"])

    def test_plain_wrap_without_box(self):
        got = self._links(["https://example.com/very/long/path?token=",
                           "abc123def456"])
        self.assertEqual(got[:1],
                         ["https://example.com/very/long/path?token=abc123def456"])

    def test_no_links(self):
        self.assertEqual(self._links(["hello", "world"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

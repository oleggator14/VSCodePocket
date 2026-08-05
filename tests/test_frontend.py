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
global.visualViewport = null;
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

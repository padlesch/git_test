#!/usr/bin/env python3
"""Мини-интерфейс для Forgejo: просмотр задач (issues) и массовое добавление.

Только стандартная библиотека. Запуск:  python forgejo_tasks.py
Откроется http://127.0.0.1:8765 (слушает только localhost, токен браузеру не передаётся).

Настройки — константы ниже; флаги (--url, --token, --repo, --port) и переменные
окружения FORGEJO_URL / FORGEJO_TOKEN их переопределяют.

Формат списка задач для массового добавления (одна задача — одна строка):
    Название
    Название | описание
    Название | описание | метка1, метка2
  Пустые строки и строки, начинающиеся с #, пропускаются; маркеры «- », «* », «- [ ] »
  в начале строки удаляются; «\\n» в описании превращается в перевод строки.
"""
import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ====== НАСТРОЙКИ: впишите свои значения ======
FORGEJO_URL = ""  # адрес вашего Forgejo, например https://forgejo.example.com
FORGEJO_TOKEN = ""  # персональный токен (нужны права на чтение/запись issues)
DEFAULT_REPO = ""  # "владелец/репозиторий", выбранный в списке по умолчанию
PORT = 8765
# ===============================================

PAGE_SIZE = 50
MAX_PAGES = 20
MAX_TASKS = 200
MAX_BODY = 1_000_000
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class Forgejo:
    def __init__(self, base_url, token="", timeout=15):
        self.api = base_url.rstrip("/") + "/api/v1"
        self.token = token
        self.timeout = timeout

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "token " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp), resp.headers
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                text = json.loads(text).get("message", text)
            except (ValueError, AttributeError):
                pass
            raise ApiError(e.code, str(text)[:300]) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"нет связи с сервером: {e.reason}") from None
        except ValueError:
            raise ApiError(0, "сервер вернул не JSON (проверьте адрес)") from None

    def pages(self, path):
        out = []
        sep = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            data, headers = self.call("GET", f"{path}{sep}limit={PAGE_SIZE}&page={page}")
            out += data
            total = int(headers.get("X-Total-Count", -1))
            if not data or (total >= 0 and len(out) >= total) or (total < 0 and len(data) < PAGE_SIZE):
                break
        return out


def parse_tasks(text):
    tasks = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*+]\s+(?:\[[ xX]\]\s+)?", "", line)
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        labels = []
        if len(parts) > 2:
            labels = [x.strip() for x in parts[2].split(",") if x.strip()]
        body = parts[1].replace("\\n", "\n") if len(parts) > 1 else ""
        tasks.append({"title": parts[0], "body": body, "labels": labels})
    return tasks


def check_repo(repo):
    if not REPO_RE.match(repo or "") or any(set(part) == {"."} for part in repo.split("/")):
        raise ValueError("репозиторий должен быть в виде владелец/имя")
    return repo


def label_map(client, repo):
    labels = []
    try:  # метки организации (для личного репозитория вернётся 404 — это нормально)
        labels += client.pages(f"/orgs/{urllib.parse.quote(repo.split('/')[0])}/labels")
    except ApiError:
        pass
    labels += client.pages(f"/repos/{repo}/labels")
    return {l["name"].lower(): l["id"] for l in labels}


def create_tasks(client, repo, text, dry_run=True, skip_duplicates=True):
    check_repo(repo)
    tasks = parse_tasks(text)
    if not tasks:
        raise ValueError("в списке нет задач")
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"слишком много задач за раз (максимум {MAX_TASKS})")
    existing = set()
    if skip_duplicates:
        existing = {i["title"].strip().lower()
                    for i in client.pages(f"/repos/{repo}/issues?state=open&type=issues")}
    labels = label_map(client, repo) if any(t["labels"] for t in tasks) else {}

    results, abort = [], None
    for t in tasks:
        res = {"title": t["title"], "status": "", "warnings": []}
        ids = []
        for name in t["labels"]:
            if name.lower() in labels:
                ids.append(labels[name.lower()])
            else:
                res["warnings"].append(f"нет метки «{name}»")
        key = t["title"].lower()
        if abort:
            res.update(status="error", error=abort)
        elif skip_duplicates and key in existing:
            res["status"] = "duplicate"
        elif dry_run:
            res["status"] = "planned"
            existing.add(key)
        else:
            try:
                issue, _ = client.call("POST", f"/repos/{repo}/issues",
                                       {"title": t["title"], "body": t["body"], "labels": ids})
                res.update(status="created", number=issue.get("number"), url=issue.get("html_url", ""))
                existing.add(key)
            except ApiError as e:
                res.update(status="error", error=str(e))
                if e.status in (401, 403):  # дальше нет смысла стучаться
                    abort = str(e)
        results.append(res)
    return results


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):  # защита от DNS rebinding: принимаем только localhost
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def handle_api(self, route, query, body):
        client = self.server.client
        if route == "/api/config":
            me = None
            if client.token:
                try:
                    me = client.call("GET", "/user")[0].get("login")
                except ApiError:
                    pass
            return {"host": urllib.parse.urlparse(client.api).netloc, "user": me,
                    "has_token": bool(client.token), "default_repo": self.server.default_repo}
        if route == "/api/repos":
            repos = client.pages("/user/repos")
            return [{"full_name": r["full_name"], "open_issues": r.get("open_issues_count", 0)}
                    for r in sorted(repos, key=lambda r: r["full_name"].lower())]
        if route == "/api/issues":
            repo = check_repo(query.get("repo", [""])[0])
            state = query.get("state", ["open"])[0]
            if state not in ("open", "closed", "all"):
                raise ValueError("state: open, closed или all")
            return client.pages(f"/repos/{repo}/issues?state={state}&type=issues")
        if route == "/api/create":
            return create_tasks(client, body.get("repo", ""), body.get("text", ""),
                                bool(body.get("dry_run", True)), bool(body.get("skip_duplicates", True)))
        return None

    def respond(self, method):
        if not self.host_ok():
            return self.send_json(403, {"error": "Forbidden host"})
        url = urllib.parse.urlparse(self.path)
        body = {}
        if method == "POST":
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send_json(415, {"error": "ожидается application/json"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.send_json(413, {"error": "слишком большой запрос"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "некорректный JSON"})
        if method == "GET" and url.path == "/":
            data = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if not url.path.startswith("/api/") or (url.path == "/api/create") != (method == "POST"):
            return self.send_json(404, {"error": "не найдено"})
        try:
            result = self.handle_api(url.path, urllib.parse.parse_qs(url.query), body)
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except ApiError as e:
            return self.send_json(502, {"error": str(e)})
        if result is None:
            return self.send_json(404, {"error": "не найдено"})
        self.send_json(200, result)

    def do_GET(self):
        self.respond("GET")

    def do_POST(self):
        self.respond("POST")


def make_server(client, port=PORT, default_repo=""):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.client = client
    srv.default_repo = default_repo
    return srv


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=os.environ.get("FORGEJO_URL") or FORGEJO_URL)
    p.add_argument("--token", default=os.environ.get("FORGEJO_TOKEN") or FORGEJO_TOKEN)
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    if not args.url:
        p.error("впишите адрес в FORGEJO_URL в начале файла или передайте --url")
    try:
        srv = make_server(Forgejo(args.url, args.token), args.port, args.repo)
    except OSError as e:
        sys.exit(f"Не удалось занять порт {args.port}: {e}. Укажите другой: --port 8766")
    url = f"http://127.0.0.1:{srv.server_port}"
    print(f"Открой {url}  (остановка: Ctrl+C)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forgejo Tasks</title>
<style>
:root{--bg:#f4f5f7;--surface:#fff;--text:#1c2230;--muted:#586174;--line:#d9dde5;--accent:#0f766e;--accent-fg:#fff;
--danger:#b42318;--warn:#8a5a00;--ok:#0f766e;--radius:8px}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--surface:#1a1f28;--text:#e6e9ef;--muted:#9aa3b2;--line:#2c3441;
--accent:#2dd4bf;--accent-fg:#06201d;--danger:#f97066;--warn:#e3b341;--ok:#2dd4bf}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--surface);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
header h1{font-size:16px;margin:0 8px 0 0}
header .meta{color:var(--muted);font-size:13px;margin-left:auto}
main{max-width:1200px;margin:16px auto;padding:0 16px;display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px;align-items:start}
@media (max-width:900px){main{grid-template-columns:minmax(0,1fr)}}
section{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px}
h2{font-size:15px;margin:0 0 12px}
select,input,textarea,button{font:inherit;color:var(--text)}
select,input[type=search],textarea{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 10px}
select{max-width:100%}
textarea{width:100%;min-height:200px;resize:vertical;font:13px/1.5 ui-monospace,Consolas,monospace}
button{border:1px solid var(--line);background:var(--surface);border-radius:6px;padding:7px 14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg);font-weight:600}
button:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.tabs button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.tabs input{flex:1;min-width:140px}
ul{list-style:none;margin:0;padding:0}
li.issue{padding:10px 0;border-top:1px solid var(--line)}
li.issue:first-child{border-top:0}
li.issue a{color:var(--text);font-weight:600;text-decoration:none}
li.issue a:hover{text-decoration:underline}
.sub{color:var(--muted);font-size:13px}
.chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:99px;padding:0 8px;font-size:12px;margin-left:6px}
.chip i{width:8px;height:8px;border-radius:50%;display:inline-block}
.closed{color:var(--muted)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
.hint{color:var(--muted);font-size:13px;margin:0 0 8px}
.hint code{background:var(--bg);padding:1px 5px;border-radius:4px}
#status{margin-top:12px;font-size:14px}
.err{color:var(--danger)}
.res{padding:6px 0;border-top:1px solid var(--line);font-size:14px}
.res a{color:var(--accent)}
.res b{display:inline-block;min-width:150px}
.created,.planned{color:var(--ok)}.duplicate{color:var(--muted)}.error{color:var(--danger)}.warn{color:var(--warn)}
</style>
</head>
<body>
<header>
  <h1>Forgejo Tasks</h1>
  <label for="repo" class="sub">Репозиторий</label>
  <select id="repo"></select>
  <button id="reload" type="button">Обновить</button>
  <span class="meta" id="who"></span>
</header>
<main>
  <section aria-labelledby="h-list">
    <h2 id="h-list">Задачи</h2>
    <div class="tabs">
      <button type="button" data-state="open" aria-pressed="true">Открытые</button>
      <button type="button" data-state="closed" aria-pressed="false">Закрытые</button>
      <button type="button" data-state="all" aria-pressed="false">Все</button>
      <input type="search" id="filter" placeholder="Фильтр по названию" aria-label="Фильтр по названию">
    </div>
    <div class="sub" id="count" aria-live="polite"></div>
    <ul id="issues"></ul>
  </section>
  <section aria-labelledby="h-add">
    <h2 id="h-add">Добавить задачи списком</h2>
    <p class="hint">Одна задача — одна строка: <code>Название | описание | метка1, метка2</code>.
      Описание и метки необязательны. Строки с <code>#</code> игнорируются.</p>
    <textarea id="text" aria-label="Список задач" placeholder="Настроить CI | проверить пайплайн | bug&#10;Обновить README"></textarea>
    <div class="row">
      <label><input type="checkbox" id="dedupe" checked> пропускать дубликаты (по названию среди открытых)</label>
    </div>
    <div class="row">
      <button type="button" id="preview">Предпросмотр</button>
      <button type="button" id="create" class="primary">Создать</button>
    </div>
    <div id="status" role="status" aria-live="polite"></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  for (const k of kids) e.append(k);
  return e;
}
async function api(path, body) {
  const opt = body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error: r.statusText}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};
let state = 'open', issues = [];

function renderIssues() {
  const q = $('#filter').value.trim().toLowerCase();
  const shown = issues.filter(i => i.title.toLowerCase().includes(q));
  $('#count').textContent = `Показано ${shown.length} из ${issues.length}`;
  const ul = $('#issues');
  ul.replaceChildren(...shown.map(i => {
    const a = el('a', {textContent: `#${i.number} ${i.title}`});
    if (/^https?:\/\//.test(i.html_url || '')) { a.href = i.html_url; a.target = '_blank'; a.rel = 'noopener'; }
    const title = el('div', {className: i.state === 'closed' ? 'closed' : ''}, a);
    for (const l of i.labels || []) {
      const dot = el('i');
      if (/^[0-9a-f]{6}$/i.test(l.color || '')) dot.style.background = '#' + l.color;
      title.append(el('span', {className: 'chip'}, dot, l.name));
    }
    const who = (i.assignees || []).map(u => u.login).join(', ');
    const sub = el('div', {className: 'sub', textContent:
      `${i.state === 'closed' ? 'закрыта' : 'открыта'} · обновлена ${new Date(i.updated_at).toLocaleDateString('ru-RU')}` +
      (who ? ` · ${who}` : '') + (i.comments ? ` · комментариев: ${i.comments}` : '')});
    return el('li', {className: 'issue'}, title, sub);
  }));
}
async function loadIssues() {
  const repo = $('#repo').value;
  if (!repo) return;
  $('#count').textContent = 'Загрузка…';
  try {
    issues = await api(`/api/issues?repo=${encodeURIComponent(repo)}&state=${state}`);
    renderIssues();
  } catch (e) {
    issues = []; $('#issues').replaceChildren();
    $('#count').replaceChildren(el('span', {className: 'err', textContent: e.message}));
  }
}
const LABEL = {created: 'создана', planned: 'будет создана', duplicate: 'дубликат, пропущена', error: 'ошибка'};
function showResults(list, dry) {
  const box = $('#status');
  const n = s => list.filter(r => r.status === s).length;
  const head = dry
    ? `Предпросмотр: создать ${n('planned')}, пропустить ${n('duplicate')}`
    : `Готово: создано ${n('created')}, пропущено ${n('duplicate')}, ошибок ${n('error')}`;
  box.replaceChildren(el('b', {textContent: head}), ...list.map(r => {
    const row = el('div', {className: 'res'}, el('b', {className: r.status, textContent: LABEL[r.status] || r.status}));
    if (r.url && /^https?:\/\//.test(r.url)) row.append(el('a', {href: r.url, target: '_blank', rel: 'noopener', textContent: `#${r.number} ${r.title}`}));
    else row.append(r.title);
    for (const w of r.warnings) row.append(el('span', {className: 'warn', textContent: ` · ${w}`}));
    if (r.error) row.append(el('span', {className: 'err', textContent: ` · ${r.error}`}));
    return row;
  }));
}
async function submit(dry) {
  const text = $('#text').value;
  if (!text.trim()) { $('#status').textContent = 'Список пуст'; return; }
  const btns = [$('#preview'), $('#create')];
  btns.forEach(b => b.disabled = true);
  try {
    const res = await api('/api/create', {repo: $('#repo').value, text, dry_run: dry, skip_duplicates: $('#dedupe').checked});
    showResults(res, dry);
    if (!dry) loadIssues();
  } catch (e) {
    $('#status').replaceChildren(el('span', {className: 'err', textContent: e.message}));
  } finally { btns.forEach(b => b.disabled = false); }
}
async function init() {
  const cfg = await api('/api/config');
  $('#who').textContent = `${cfg.host}${cfg.user ? ' · ' + cfg.user : ''}`;
  if (!cfg.has_token) $('#status').textContent = 'Токен не задан: список репозиториев и создание задач недоступны.';
  const sel = $('#repo');
  try {
    const repos = await api('/api/repos');
    sel.replaceChildren(...repos.map(r => el('option', {value: r.full_name, textContent: `${r.full_name} (${r.open_issues})`})));
    const want = cfg.default_repo || store.get('repo');
    if (want && repos.some(r => r.full_name === want)) sel.value = want;
  } catch (e) {
    $('#count').replaceChildren(el('span', {className: 'err', textContent: e.message}));
    return;
  }
  loadIssues();
}
$('#repo').onchange = () => { store.set('repo', $('#repo').value); loadIssues(); };
$('#reload').onclick = loadIssues;
$('#filter').oninput = renderIssues;
$('#preview').onclick = () => submit(true);
$('#create').onclick = () => submit(false);
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  state = b.dataset.state;
  document.querySelectorAll('.tabs button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  loadIssues();
});
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())

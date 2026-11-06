"""Тесты forgejo_tasks.py на локальном моке Forgejo API.  Запуск: python -m unittest -v"""
import http.client
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import forgejo_tasks as ft

TOKEN = "secret"


class State:
    issues = []
    created = []


class Mock(BaseHTTPRequestHandler):
    def reply(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        return self.headers.get("Authorization") == f"token {TOKEN}"

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self.authed():
            return self.reply(401, {"message": "token is required"})
        if u.path == "/api/v1/user":
            return self.reply(200, {"login": "tester"})
        if u.path == "/api/v1/user/repos":
            return self.reply(200, [{"full_name": "o/r", "open_issues_count": 3}])
        if u.path == "/api/v1/repos/o/r/labels":
            return self.reply(200, [{"id": 7, "name": "Bug"}])
        if u.path == "/api/v1/orgs/o/labels":
            return self.reply(404, {"message": "not an org"})
        if u.path == "/api/v1/repos/o/r/issues":
            limit, page = int(q["limit"][0]), int(q["page"][0])
            items = State.issues[(page - 1) * limit: page * limit]
            return self.reply(200, items, {"X-Total-Count": str(len(State.issues))})
        self.reply(404, {"message": "nope"})

    def do_POST(self):
        if not self.authed():
            return self.reply(401, {"message": "token is required"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["title"] == "boom":
            return self.reply(422, {"message": "validation failed"})
        assert all(isinstance(i, int) for i in body["labels"])
        State.created.append(body)
        n = 100 + len(State.created)
        self.reply(201, {"number": n, "html_url": f"http://x/o/r/issues/{n}"})

    def log_message(self, *a):
        pass


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ParseTest(unittest.TestCase):
    def test_parse(self):
        text = "# коммент\n\n- [ ] Один | описание\\nстрока 2 | bug, ui\n* Два\n |пусто\n"
        self.assertEqual(ft.parse_tasks(text), [
            {"title": "Один", "body": "описание\nстрока 2", "labels": ["bug", "ui"]},
            {"title": "Два", "body": "", "labels": []},
        ])


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = serve(Mock)
        client = ft.Forgejo(f"http://127.0.0.1:{cls.mock.server_port}", TOKEN)
        cls.app = ft.make_server(client, 0, "o/r")
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app.server_port}"
        ft.PAGE_SIZE = 2  # проверить пагинацию

    @classmethod
    def tearDownClass(cls):
        cls.mock.shutdown()
        cls.app.shutdown()
        ft.PAGE_SIZE = 50

    def setUp(self):
        State.issues = [{"number": i, "title": f"Task {i}", "state": "open"} for i in range(1, 6)]
        State.created = []

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_index_and_config(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("Forgejo Tasks", r.read().decode())
        cfg = self.get("/api/config")
        self.assertEqual((cfg["user"], cfg["default_repo"], cfg["has_token"]), ("tester", "o/r", True))

    def test_repos_and_paginated_issues(self):
        self.assertEqual(self.get("/api/repos")[0]["full_name"], "o/r")
        self.assertEqual(len(self.get("/api/issues?repo=o/r")), 5)

    def test_bad_repo(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/issues?repo=../etc")
        self.assertEqual(cm.exception.code, 400)

    def test_dry_run_then_create(self):
        text = "Task 2 | dup\nНовая | описание | bug, nolabel\nНовая\nboom"
        code, res = self.post("/api/create", {"repo": "o/r", "text": text, "dry_run": True})
        self.assertEqual([r["status"] for r in res], ["duplicate", "planned", "duplicate", "planned"])
        self.assertEqual(State.created, [])
        self.assertEqual(res[1]["warnings"], ["нет метки «nolabel»"])
        code, res = self.post("/api/create", {"repo": "o/r", "text": text, "dry_run": False})
        self.assertEqual([r["status"] for r in res], ["duplicate", "created", "duplicate", "error"])
        self.assertIn("422", res[3]["error"])
        self.assertEqual(State.created, [{"title": "Новая", "body": "описание", "labels": [7]}])

    def test_no_dedupe(self):
        _, res = self.post("/api/create", {"repo": "o/r", "text": "Task 1", "dry_run": False,
                                           "skip_duplicates": False})
        self.assertEqual(res[0]["status"], "created")

    def test_empty_list(self):
        code, res = self.post("/api/create", {"repo": "o/r", "text": "# только коммент"})
        self.assertEqual(code, 400)

    def test_guards(self):
        c = http.client.HTTPConnection("127.0.0.1", self.app.server_port)
        c.request("GET", "/api/config", headers={"Host": "evil.example"})
        self.assertEqual(c.getresponse().status, 403)
        c = http.client.HTTPConnection("127.0.0.1", self.app.server_port)
        c.request("POST", "/api/create", body="{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(c.getresponse().status, 415)


class BadTokenTest(unittest.TestCase):
    def test_auth_abort(self):
        mock = serve(Mock)
        try:
            client = ft.Forgejo(f"http://127.0.0.1:{mock.server_port}", "bad")
            with self.assertRaises(ft.ApiError) as cm:
                ft.create_tasks(client, "o/r", "A", dry_run=False)
            self.assertEqual(cm.exception.status, 401)
        finally:
            mock.shutdown()

    def test_server_down(self):
        with self.assertRaises(ft.ApiError):
            ft.Forgejo("http://127.0.0.1:1", "x").call("GET", "/user")


if __name__ == "__main__":
    unittest.main()

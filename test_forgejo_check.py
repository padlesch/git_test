"""Тест forgejo_check.py на локальном моке Forgejo API.  Запуск: python3 -m unittest -v"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import forgejo_check

TOKEN = "secret"


class Mock(BaseHTTPRequestHandler):
    def do_GET(self):
        authed = self.headers.get("Authorization") == f"token {TOKEN}"
        routes = {
            "/api/v1/version": (200, {"version": "11.0.0+gitea-1.22.0"}),
            "/api/v1/user": (200, {"login": "tester"}) if authed else (401, {"message": "no"}),
            "/api/v1/user/repos?limit=5": (200, [{"full_name": "tester/demo"}]) if authed else (401, {}),
        }
        code, body = routes.get(self.path, (404, {}))
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_no_token(self):
        self.assertEqual(forgejo_check.main(["--url", self.url]), 0)

    def test_good_token(self):
        self.assertEqual(forgejo_check.main(["--url", self.url, "--token", TOKEN]), 0)

    def test_bad_token(self):
        self.assertEqual(forgejo_check.main(["--url", self.url, "--token", "bad"]), 1)

    def test_server_down(self):
        self.assertEqual(forgejo_check.main(["--url", "http://127.0.0.1:1"]), 1)


if __name__ == "__main__":
    unittest.main()

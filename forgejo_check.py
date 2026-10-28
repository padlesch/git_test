#!/usr/bin/env python3
"""Проверка доступности Forgejo-сервера через API (только stdlib).

Переменные окружения:
  FORGEJO_URL    базовый URL, напр. https://codeberg.org (обязательно)
  FORGEJO_TOKEN  персональный токен (необязательно; нужен для /user и /user/repos)

Запуск:  python3 forgejo_check.py [--url URL] [--token TOKEN]
Код возврата: 0 — все проверки пройдены, 1 — есть ошибки.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


class Forgejo:
    def __init__(self, base_url, token=None, timeout=10):
        self.api = base_url.rstrip("/") + "/api/v1"
        self.token = token
        self.timeout = timeout

    def get(self, path):
        req = urllib.request.Request(self.api + path)
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", "token " + self.token)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    def version(self):
        return self.get("/version")["version"]

    def me(self):
        return self.get("/user")

    def my_repos(self, limit=5):
        return self.get(f"/user/repos?limit={limit}")


def run_check(name, fn):
    try:
        result = fn()
    except urllib.error.HTTPError as e:
        print(f"[FAIL] {name}: HTTP {e.code} {e.reason}")
    except Exception as e:  # сеть, DNS, TLS, невалидный JSON
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    else:
        print(f"[ OK ] {name}: {result}")
        return True
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=os.environ.get("FORGEJO_URL"))
    p.add_argument("--token", default=os.environ.get("FORGEJO_TOKEN"))
    args = p.parse_args(argv)
    if not args.url:
        p.error("укажите --url или переменную FORGEJO_URL")

    api = Forgejo(args.url, args.token)
    results = [run_check("version", api.version)]
    if args.token:
        results.append(run_check("auth (/user)", lambda: api.me()["login"]))
        results.append(run_check(
            "repos (/user/repos)",
            lambda: [r["full_name"] for r in api.my_repos()] or "нет репозиториев",
        ))
    else:
        print("[SKIP] auth/repos: токен не задан")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

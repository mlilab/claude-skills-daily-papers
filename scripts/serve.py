#!/usr/bin/env python3
"""Serve the digest folder over HTTP.

usage: serve.py [--host 127.0.0.1] [--port 8000] [--status] [--stop]

Writes <output_dir>/.server.json with pid/host/port so later runs can detect a live server.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import signal
import socket
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from common import load_config


# Only what the rendered site needs is served. Hidden files (.publish/ holds the GitHub token and
# the site key), working data (candidates, selections, full texts, PDFs, summaries JSON, logs)
# and directory listings stay private even to other users of this machine.
SITE_EXT = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_head(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        if any(p.startswith(".") for p in parts):
            self.send_error(404)
            return None
        if parts and not path.endswith("/") and "." in parts[-1] \
                and os.path.splitext(parts[-1])[1].lower() not in SITE_EXT:
            self.send_error(404)
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def urls(host, port):
    out = [f"http://localhost:{port}/"]
    if host in ("0.0.0.0", "::"):
        try:
            out.append(f"http://{socket.gethostbyname(socket.gethostname())}:{port}/")
        except OSError:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    root = cfg["output_dir"]
    root.mkdir(parents=True, exist_ok=True)
    state_file = root / ".server.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else None
    running = state and alive(state["pid"])

    if args.status or args.stop:
        if not running:
            print(json.dumps({"running": False}))
            return
        if args.stop:
            os.kill(state["pid"], signal.SIGTERM)
            state_file.unlink(missing_ok=True)
            print(json.dumps({"running": False, "stopped_pid": state["pid"]}))
            return
        print(json.dumps({"running": True, **state, "urls": urls(state["host"], state["port"])}))
        return
    if running:
        print(json.dumps({"already_running": True, **state, "urls": urls(state["host"], state["port"])}))
        return

    host = args.host or cfg["serve"]["host"]
    port = args.port or cfg["serve"]["port"]
    handler = functools.partial(QuietHandler, directory=str(root))
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, p), handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"no free port in {port}-{port + 19}")
    state = {"pid": os.getpid(), "host": host, "port": p, "root": str(root)}
    state_file.write_text(json.dumps(state))
    print(json.dumps({"serving": True, **state, "urls": urls(host, p)}), flush=True)
    try:
        httpd.serve_forever()
    finally:
        state_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

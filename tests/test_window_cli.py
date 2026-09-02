#!/usr/bin/env python3
"""Regression tests for opening the existing always-on viewer."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vault_radar", ROOT / "radar.py")
assert SPEC and SPEC.loader
RADAR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADAR)


class VaultHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/vault":
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, format, *args):
        del format, args


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_main(*args: str):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "argv", ["radar", *args]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = RADAR.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def main() -> int:
    failures = 0
    import tempfile as _tempfile
    bare_home = Path(_tempfile.mkdtemp(prefix="vault-radar-bare-"))
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), VaultHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with mock.patch.object(RADAR, "open_window") as opened, mock.patch.object(RADAR, "HOME", bare_home):
            code, stdout, stderr = run_main("window", "--port", str(port), "--width", "640")
        expected_url = f"http://localhost:{port}"
        ok = code == 0 and not stderr and expected_url in stdout and opened.call_args_list == [mock.call(expected_url, 640)]
        print(("PASS" if ok else "FAIL") + " window opens the healthy existing server")
        if not ok:
            failures += 1
            print("   got:", {"code": code, "stdout": stdout, "stderr": stderr, "calls": opened.call_args_list})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    port = free_port()
    with mock.patch.object(RADAR, "open_window") as opened:
        code, stdout, stderr = run_main("window", "--port", str(port))
    ok = code == 1 and not stdout and "not reachable" in stderr and not opened.called
    print(("PASS" if ok else "FAIL") + " window refuses when no server is listening")
    if not ok:
        failures += 1
        print("   got:", {"code": code, "stdout": stdout, "stderr": stderr, "calls": opened.call_args_list})


    # -- opening theme: `radar.py theme <name>` records it, `window` opens the viewer on it -----
    import tempfile
    home = Path(tempfile.mkdtemp(prefix="vault-radar-theme-"))
    with mock.patch.object(RADAR, "HOME", home):
        code, stdout, stderr = run_main("theme", "sonar")
        recorded = (home / "theme").read_text(encoding="utf-8").strip() if (home / "theme").exists() else None
        ok = code == 0 and not stderr and recorded == "sonar" and "sonar" in stdout
        print(("PASS" if ok else "FAIL") + " theme: records a built-in name in <home>/theme")
        if not ok:
            failures += 1
            print("   got:", {"code": code, "stdout": stdout, "stderr": stderr, "recorded": recorded})

        code, stdout, stderr = run_main("theme", "no-such-theme")
        recorded = (home / "theme").read_text(encoding="utf-8").strip() if (home / "theme").exists() else None
        ok = code == 1 and "no-such-theme" in stderr and recorded == "sonar"
        print(("PASS" if ok else "FAIL") + " theme: refuses an unknown name and keeps the old one")
        if not ok:
            failures += 1
            print("   got:", {"code": code, "stdout": stdout, "stderr": stderr, "recorded": recorded})

        code, stdout, stderr = run_main("theme")
        ok = code == 0 and stdout.strip() == "sonar"
        print(("PASS" if ok else "FAIL") + " theme: with no name prints the recorded one")
        if not ok:
            failures += 1
            print("   got:", {"code": code, "stdout": stdout, "stderr": stderr})

        port = free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), VaultHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(RADAR, "open_window") as opened:
                code, stdout, stderr = run_main("window", "--port", str(port))
            expected_url = f"http://localhost:{port}/?theme=sonar"
            ok = code == 0 and opened.call_args_list == [mock.call(expected_url, 520)]
            print(("PASS" if ok else "FAIL") + " window: opens the viewer on the recorded theme")
            if not ok:
                failures += 1
                print("   got:", {"code": code, "stdout": stdout, "stderr": stderr, "calls": opened.call_args_list})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print(f"\n2 cases, {failures} failed ({Path(__file__).resolve()})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

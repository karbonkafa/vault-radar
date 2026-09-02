#!/usr/bin/env python3
"""vault-radar — watch, in real time, which files your coding agent actually reads.

Three subcommands:
    radar.py hook              read a Claude Code hook event on stdin, append it to the log
    radar.py serve [options]   serve the live viewer at http://localhost:7777
    radar.py install           print the settings.json snippet that wires the hook up

No third-party dependencies. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

HOME = Path(os.environ.get("VAULT_RADAR_HOME") or (Path.home() / ".vault-radar")).expanduser()
EVENTS = HOME / "events.jsonl"
UI_DIR = Path(__file__).resolve().parent / "ui"


def _ratio(raw: str) -> float:
    """A bad VAULT_RADAR_CPT must not raise at import time — that would make
    every single hook invocation exit non-zero in front of the user."""
    try:
        value = float(raw)
    except ValueError:
        return 3.8
    return value if value > 0 else 3.8


# Rough character-per-token ratio. English ~4.0, Turkish ~3.6.
CHARS_PER_TOKEN = _ratio(os.environ.get("VAULT_RADAR_CPT", "3.8"))

READ_TOOLS = {"Read", "NotebookRead"}
SCAN_TOOLS = {"Grep", "Glob"}


# ──────────────────────────────────────────── hook


def _extract(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn a raw hook payload into radar events: usually one, none if uninteresting."""
    event = payload.get("hook_event_name", "")
    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or ""

    if event == "UserPromptSubmit":
        return [{"kind": "prompt", "text": (payload.get("prompt") or "")[:400]}]

    if event == "Stop":
        return [{"kind": "stop"}]

    if event != "PostToolUse":
        return []

    if tool in READ_TOOLS or tool.lower() in {"readfile", "view", "opendocument"}:
        path = tin.get("file_path") or tin.get("notebook_path")
        if not path:
            return []
        ev = {"kind": "read", "path": _absolute(path, cwd), "tool": tool}
        chars = _returned_chars(payload.get("tool_response"))
        if chars is not None:
            ev["chars"] = chars  # what actually entered the context: an offset/limit Read is partial
        return [ev]

    if tool == "Bash":
        return _from_shell(tin.get("command") or "", payload.get("tool_response"), cwd)

    if tool in SCAN_TOOLS or tool.lower() in {"search", "codesearch"}:
        return [
            {
                "kind": "scan",
                "tool": tool,
                "pattern": tin.get("pattern") or tin.get("glob") or "",
                "hits": _scan_hits(payload.get("tool_response"), cwd),
            }
        ]

    _note_tool(tool)
    return []


def _absolute(path: str, cwd: str) -> str:
    """Anchor a path the way the shell would have: ~ and $VARS expanded, relative to cwd."""
    p = os.path.expandvars(os.path.expanduser(path))
    if cwd and not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def _returned_chars(response: Any) -> Optional[int]:
    """Size of the content the Read tool handed back, if the response carries it."""
    if isinstance(response, dict):
        inner = response.get("file")
        if isinstance(inner, dict) and isinstance(inner.get("content"), str):
            return len(inner["content"])
    return None


_SHELL_READ = re.compile(r"\b(?:cat|bat|head|tail|less|more|sed)\b\s+([^|;&<>\n]+)")
_SHELL_SCAN = re.compile(r"\b(?:rg|grep|ag|ack|fd|find)\b")


def _from_shell(command: str, response: Any, cwd: str) -> List[Dict[str, Any]]:
    """Agents often read through the shell (`cat notes/x.md`) instead of the Read tool.

    Without this the radar looks broken on exactly the sessions that do the most work.
    Every `cat`/`head`/`sed` in the command counts, with every file it names; flags,
    pipes and heredocs are dropped. Only paths that exist on disk are kept, so a word
    inside a quoted script does not become a phantom read. A shell `grep`/`rg` takes
    its hits from the command's own stdout.
    """
    if not command:
        return []

    events: List[Dict[str, Any]] = []
    paths: List[str] = []
    for m in _SHELL_READ.finditer(command):
        segment = m.group(1)
        try:
            tokens = shlex.split(segment)
        except ValueError:  # unbalanced quote: fall back to whitespace
            tokens = segment.split()
        if any(t.startswith("-i") or t == "--in-place" for t in tokens):
            continue  # sed -i rewrites the file; nothing entered the context
        for token in tokens:
            if token.startswith("-") or token in {"|", ";", "&&"}:
                continue
            if "." not in os.path.basename(token) and "/" not in token:
                continue
            full = _absolute(token, cwd)
            if os.path.isfile(full) and full not in paths:
                paths.append(full)

    stdout = response.get("stdout") if isinstance(response, dict) else None
    for full in paths:
        ev: Dict[str, Any] = {"kind": "read", "path": full, "tool": "Bash", "via": "shell"}
        if len(paths) == 1 and isinstance(stdout, str):
            ev["chars"] = len(stdout)  # `head -40 x.md` returned 40 lines, not the file
        events.append(ev)

    if _SHELL_SCAN.search(command):
        events.append(
            {
                "kind": "scan",
                "tool": "Bash",
                "pattern": command[:80],
                "hits": _scan_hits(response, cwd),
                "via": "shell",
            }
        )

    return events


def _note_tool(tool: str) -> None:
    """Record tool names we do not recognise, so the matcher can be widened.

    Claude Code renames and merges tools between releases; guessing the names
    is how this stops working silently. Writing them down makes it visible.
    """
    if not tool:
        return
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        seen = HOME / "tools-seen.txt"
        known = set(seen.read_text(encoding="utf-8").split()) if seen.exists() else set()
        if tool not in known:
            with seen.open("a", encoding="utf-8") as fh:
                fh.write(tool + "\n")
    except Exception:
        pass


_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _scan_hits(response: Any, cwd: str = "") -> List[str]:
    """Best-effort extraction of file paths from a Grep/Glob response or a shell's stdout.

    Structured responses (`filenames`) are taken as they are. Text is read line by
    line: `path:12:match` (grep -n), `path:3` (grep -c) or a bare path (grep -l, glob,
    find, rg's file headings). Every candidate is anchored to cwd and must exist as a
    file, which is what keeps matched *content* from being mistaken for a path.
    """
    text = ""
    if isinstance(response, str):
        text = response
    elif isinstance(response, dict):
        for key in ("filenames", "files", "matches"):
            value = response.get(key)
            if isinstance(value, list):
                return [_absolute(str(v), cwd) for v in value if isinstance(v, (str, os.PathLike))][:400]
        for key in ("stdout", "output", "content", "result"):
            if isinstance(response.get(key), str):
                text = response[key]
                break
    seen, out = set(), []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _WIN_DRIVE.match(line):  # keep C:\ together with its path
            candidate = line[:2] + line[2:].split(":", 1)[0]
        else:
            candidate = line.split(":", 1)[0]
        full = _absolute(candidate.strip(), cwd)
        if full in seen or not os.path.isfile(full):
            continue
        seen.add(full)
        out.append(full)
        if len(out) >= 400:
            break
    return out


def cmd_hook(_args: argparse.Namespace) -> int:
    """Read one hook payload on stdin and append a radar event. Never blocks Claude."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # a malformed payload must never break the agent's turn

    try:
        events = _extract(payload)
        if not events:
            return 0
        stamp = dict(ts=time.time(), session=payload.get("session_id", ""), cwd=payload.get("cwd", ""))
        for event in events:
            event.update(stamp)
        line = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
        HOME.mkdir(parents=True, exist_ok=True)
        # One O_APPEND write() per hook call. Several Claude Code sessions append to
        # this file at the same time, and a buffered text writer splits a long
        # line (a Grep with hundreds of hits) into chunks that interleave.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        fd = os.open(str(EVENTS), flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass  # radar is an observer; it is never allowed to fail loudly
    return 0


# ──────────────────────────────────────────── vault scan


SKIP_DIRS = {".git", "node_modules", ".obsidian", "__pycache__", ".venv", "venv"}


WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def scan_vault(root: Path, exts: List[str]) -> List[Dict[str, Any]]:
    """List every tracked file under root with its size and token estimate."""
    files: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if not any(name.endswith(e) for e in exts):
                continue
            full = Path(dirpath) / name
            try:
                chars = full.stat().st_size
            except OSError:
                continue
            files.append(
                {
                    "path": str(full.relative_to(root)),
                    "abs": str(full),
                    "chars": chars,
                    "tokens": round(chars / CHARS_PER_TOKEN),
                }
            )
    files.sort(key=lambda f: f["path"])
    return files


def scan_links(root: Path, files: List[Dict[str, Any]]) -> List[List[int]]:
    """Resolve [[wikilinks]] between files into index pairs.

    Targets are matched on basename, which is how Obsidian resolves them too.
    Links pointing outside the vault are dropped.
    """
    by_stem: Dict[str, int] = {}
    for i, f in enumerate(files):
        by_stem.setdefault(Path(f["path"]).stem, i)

    seen = set()
    edges: List[List[int]] = []
    for i, f in enumerate(files):
        try:
            text = Path(f["abs"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in WIKILINK.findall(text):
            j = by_stem.get(Path(raw.strip()).stem)
            if j is None or j == i:
                continue
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
            edges.append([i, j])
    return edges


# ──────────────────────────────────────────── server


def tail(path: Path, start_at_end: bool) -> Iterator[str]:
    """Yield lines appended to path, waiting for the file to appear if needed.

    Uses readline() rather than iteration: calling tell() inside a `for line in fh`
    loop over a text file raises OSError ("telling position disabled by next() call"),
    which would silently kill the stream after the first event.
    """
    pos = 0
    while True:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                if start_at_end:
                    fh.seek(0, os.SEEK_END)
                    start_at_end = False
                else:
                    # The log can shrink under us: /api/reset unlinks it and the
                    # next hook recreates it at size 0. Without this the stale
                    # offset seeks past EOF and the stream is dead for good.
                    try:
                        if os.fstat(fh.fileno()).st_size < pos:
                            pos = 0
                    except OSError:
                        pos = 0
                    fh.seek(pos)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    if line.endswith("\n"):
                        yield line
                    else:  # partial write; rewind and wait for the rest
                        fh.seek(pos)
                        break
                    pos = fh.tell()
                pos = max(pos, fh.tell()) if not start_at_end else pos
        time.sleep(0.35)


def current_turn(limit: int = 4000) -> List[str]:
    """Return the events of the turn in progress: everything after the last prompt.

    Read as a whole so a viewer opened halfway through a turn still shows the
    files already touched.
    """
    if not EVENTS.exists():
        return []
    try:
        # split("\n"), not splitlines(): splitlines() also breaks on U+2028 and
        # U+0085, which json.dumps(ensure_ascii=False) emits verbatim, and half a
        # JSON line reaches the viewer as an unparseable SSE frame.
        lines = EVENTS.read_text(encoding="utf-8").split("\n")[-limit:]
    except OSError:
        return []
    # Several Claude Code sessions share one log. Anchor on the most recent
    # prompt and keep only that session's events, otherwise a second terminal
    # silently steals the viewer.
    start, session = 0, None
    for i in range(len(lines) - 1, -1, -1):
        try:
            row = json.loads(lines[i])
        except ValueError:
            continue
        if row.get("kind") == "prompt":
            start, session = i, row.get("session")
            break

    out = []
    for ln in lines[start:]:
        if not ln.strip():
            continue
        try:
            if session and json.loads(ln).get("session") not in (session, None, ""):
                continue
        except ValueError:
            continue
        out.append(ln)
    return out


LOOPBACK_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def _is_loopback(hostport: str) -> bool:
    """True for a Host/Origin authority that names this machine's loopback."""
    host = hostport.strip()
    if host.startswith("["):  # [::1]:7777
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    host = host.lower()
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def make_handler(vault: Path, exts: List[str], alias: Optional[Path] = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self) -> bool:
            """Loopback-only server, so only loopback names may address it.

            Blocks DNS rebinding (evil.com re-pointed at 127.0.0.1 arrives with
            its own Host) and cross-site fetches from a page you are browsing
            (those carry an Origin). Same-origin requests from the viewer send
            Host: localhost:PORT and either no Origin or the same one.
            """
            if not _is_loopback(self.headers.get("Host", "")):
                return False
            origin = self.headers.get("Origin")
            if origin and not _is_loopback(origin.split("://", 1)[-1]):
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._allowed():
                return self._send(403, b"forbidden", "text/plain")
            route = self.path.split("?", 1)[0]

            if route in ("/", "/index.html"):
                try:
                    page = (UI_DIR / "index.html").read_bytes()
                except OSError:
                    return self._send(500, b"ui/index.html not found", "text/plain")
                return self._send(200, page, "text/html; charset=utf-8")

            if route == "/api/vault":
                files = scan_vault(vault, exts)
                body = json.dumps(
                    {
                        "root": str(vault),
                        "root_alias": str(alias or vault),  # as typed at --vault, symlinks intact
                        "cpt": CHARS_PER_TOKEN,
                        "files": files,
                        "edges": scan_links(vault, files),
                        "total_tokens": sum(f["tokens"] for f in files),
                    },
                    ensure_ascii=False,
                ).encode()
                return self._send(200, body, "application/json; charset=utf-8")

            if route == "/api/stream":
                return self._stream()

            return self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed():
                return self._send(403, b"forbidden", "text/plain")
            # Drain any body, otherwise it is parsed as the next keep-alive request.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if 0 < length <= 1 << 20:
                self.rfile.read(length)
            if self.path.split("?", 1)[0] == "/api/reset":
                try:
                    EVENTS.unlink(missing_ok=True)
                except OSError:
                    pass
                return self._send(200, b'{"ok":true}', "application/json")
            return self._send(404, b"not found", "text/plain")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # Replay the turn already in progress, so a window opened mid-turn
            # is not blank. Without this the viewer only ever shows the NEXT turn.
            try:
                for line in current_turn():
                    self.wfile.write(b"data: " + line.encode() + b"\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            last_beat = time.time()
            try:
                for line in tail(EVENTS, start_at_end=True):
                    self.wfile.write(b"data: " + line.strip().encode() + b"\n\n")
                    self.wfile.flush()
                    now = time.time()
                    if now - last_beat > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_beat = now
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def open_window(url: str, width: int = 520) -> None:
    """Open the viewer as a chromeless window docked to the right of the screen.

    Falls back to the default browser if no Chromium-family browser is found.
    """
    import shutil
    import subprocess
    import webbrowser

    chrome_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("microsoft-edge"),
    ]
    chrome = next((p for p in chrome_paths if p and Path(p).exists()), None)
    if not chrome:
        webbrowser.open(url)
        return

    sw, sh = screen_size()
    try:
        subprocess.Popen(
            [
                chrome,
                f"--app={url}",
                f"--window-position={max(0, sw - width)},0",
                f"--window-size={width},{max(600, sh - 100)}",
                "--user-data-dir=" + str(Path(HOME) / "browser-profile"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        webbrowser.open(url)


def screen_size() -> tuple:
    """Best-effort screen dimensions; falls back to 1920x1080."""
    import re
    import subprocess

    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=6,
            ).stdout
            m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080


def cmd_serve(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    alias = Path(os.path.abspath(os.path.expanduser(args.vault)))
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 1
    exts = [e if e.startswith(".") else "." + e for e in args.ext.split(",")]
    files = scan_vault(vault, exts)
    HOME.mkdir(parents=True, exist_ok=True)
    EVENTS.touch(exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(vault, exts, alias))
    print(f"vault-radar · {len(files)} files · ~{sum(f['tokens'] for f in files):,} tokens")
    print(f"vault  : {vault}")
    print(f"events : {EVENTS}")
    url = f"http://localhost:{args.port}"
    print(f"open   : {url}")
    if not args.no_open:
        threading.Timer(0.6, open_window, args=(url, args.width)).start()
        print("        (opening a docked window — pass --no-open to skip)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


# ──────────────────────────────────────────── install


# Bash is in the matcher because the agent reads through the shell too
# (`cat notes/x.md`); _from_shell turns those into read/scan events.
MATCHER = "Read|Grep|Glob|NotebookRead|Bash"


def snippet(cmd: str) -> Dict[str, Any]:
    """Build the settings.json block for `cmd`.

    Substituted into the data, never into the rendered JSON text: a Windows
    path (C:\\Users\\me\\radar.py) pasted into finished JSON produces invalid
    escapes and settings.json stops parsing.
    """
    entry = [{"type": "command", "command": cmd + " hook"}]
    return {
        "hooks": {
            "PostToolUse": [{"matcher": MATCHER, "hooks": entry}],
            "UserPromptSubmit": [{"hooks": entry}],
            "Stop": [{"hooks": entry}],
        }
    }


def cmd_install(_args: argparse.Namespace) -> int:
    # python3 is not a working command on Windows; a path with spaces in it
    # ("~/My Projects/vault-radar") has to stay one argument.
    def quote(part: str) -> str:
        return '"%s"' % part if " " in part else part

    python = sys.executable if os.name == "nt" else "python3"
    cmd = "{} {}".format(quote(python), quote(str(Path(__file__).resolve())))
    text = json.dumps(snippet(cmd), indent=2)
    print("Add this to ~/.claude/settings.json (merge with any existing \"hooks\" block):\n")
    print(text)
    print("\nThen restart Claude Code and run:  python3 radar.py serve --vault <path>")
    return 0


# ──────────────────────────────────────────── cli


def main() -> int:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("hook", help="consume one hook payload on stdin").set_defaults(fn=cmd_hook)

    serve = sub.add_parser("serve", help="serve the live viewer")
    serve.add_argument("--vault", required=True, help="directory to watch")
    serve.add_argument("--port", type=int, default=7777)
    serve.add_argument("--ext", default=".md", help="comma-separated extensions (default: .md)")
    serve.add_argument("--no-open", action="store_true", help="do not open a window")
    serve.add_argument("--width", type=int, default=520, help="docked window width (default: 520)")
    serve.set_defaults(fn=cmd_serve)

    sub.add_parser("install", help="print the settings.json snippet").set_defaults(fn=cmd_install)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

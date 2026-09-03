#!/usr/bin/env python3
"""vault-radar — watch, in real time, which files your coding agent actually reads.

Five subcommands:
    radar.py hook              read a Claude Code hook event on stdin, append it to the log
    radar.py serve [options]   serve the live viewer at http://localhost:7777
    radar.py window [options]  open the viewer already served on localhost
    radar.py install           print the settings.json snippet that wires the hook up
    radar.py follow [SESSION]  pin the viewer and the Obsidian plugin to one session

No third-party dependencies. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.parse
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import unquote

HOME = Path(os.environ.get("VAULT_RADAR_HOME") or (Path.home() / ".vault-radar")).expanduser()
EVENTS = HOME / "events.jsonl"
# Another viewer can ride on this server: VAULT_RADAR_UI names a directory whose
# index.html is served instead of ours. The events, the API and the hooks stay the same.
UI_DIR = Path(os.environ.get("VAULT_RADAR_UI") or (Path(__file__).resolve().parent / "ui")).expanduser()
FOLLOW = HOME / "follow"
THEMES_DIR = HOME / "themes"


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
    """Turn Claude, Codex, Kimi, or Kai hook payloads into radar events."""
    if payload.get("session_id") == "test-session":
        return []  # Kai's hook doctor/test probe is not a user turn.
    raw_event = payload.get("hook_event_name", "")
    event = {
        "post_tool_call": "PostToolUse",
        "pre_llm_call": "UserPromptSubmit",
        "on_session_end": "Stop",
    }.get(raw_event, raw_event)
    tool = payload.get("tool_name") or ""
    tin = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or ""
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}

    response = payload.get("tool_response")
    if response is None:
        response = payload.get("tool_output")
    if response is None:
        response = extra.get("result")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except (TypeError, ValueError):
            pass

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or extra.get("user_message") or ""
        if isinstance(prompt, list):  # Kimi sends the prompt as content blocks
            prompt = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in prompt
            )
        return [{"kind": "prompt", "text": str(prompt)[:400]}]

    if event == "Stop":
        return [{"kind": "stop"}]

    if event != "PostToolUse":
        return []

    tool_key = tool.lower()
    if tool in READ_TOOLS or tool_key in {"readfile", "read_file", "view", "opendocument"}:
        path = tin.get("file_path") or tin.get("notebook_path") or tin.get("path")
        if not path:
            return []
        ev = {"kind": "read", "path": _absolute(path, cwd), "tool": tool}
        chars = _returned_chars(response)
        if chars is not None:
            ev["chars"] = chars  # what actually entered the context: an offset/limit Read is partial
        return [ev]

    if tool_key in {"bash", "terminal", "shell", "exec_command"}:
        return _from_shell(tin.get("command") or tin.get("cmd") or "", response, cwd)

    if tool in SCAN_TOOLS or tool_key in {"search", "codesearch", "search_files"}:
        return [
            {
                "kind": "scan",
                "tool": tool,
                "pattern": tin.get("pattern") or tin.get("glob") or "",
                "hits": _scan_hits(response, cwd),
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
    """Size of the content the read tool handed back, if the response carries it.

    Claude Code wraps it as ``{"file": {"content": ...}}``, Kai's ``read_file`` as
    ``{"content": ...}``; Kimi hands back the text itself.
    """
    if isinstance(response, dict):
        inner = response.get("file")
        if isinstance(inner, dict) and isinstance(inner.get("content"), str):
            return len(inner["content"])
        if isinstance(response.get("content"), str):
            return len(response["content"])
        return None
    if isinstance(response, str):
        return len(response)
    return None


_SHELL_READ = re.compile(r"\b(?:cat|bat|head|tail|less|more|sed)\b\s+([^|;&<>\n]+)")
_SHELL_SCAN = re.compile(r"\b(?:rg|grep|ag|ack|fd|find)\b")
_SHELL_SEARCH = re.compile(r"\b(?:rg|grep|ag|ack)\b\s+([^|;&<>\n]+)")


def _named_search_files(command: str, cwd: str) -> List[str]:
    """Existing files named as arguments of the command's grep/rg/ag/ack segments.

    Same token rules as the shell reader: flags dropped, a bare word without a dot
    or a slash ignored, only paths that exist on disk kept — so the pattern itself
    is not mistaken for a file.
    """
    named: List[str] = []
    for m in _SHELL_SEARCH.finditer(command):
        segment = m.group(1)
        try:
            tokens = shlex.split(segment)
        except ValueError:  # unbalanced quote: fall back to whitespace
            tokens = segment.split()
        for token in tokens:
            if token.startswith("-"):
                continue
            if "." not in os.path.basename(token) and "/" not in token:
                continue
            full = _absolute(token, cwd)
            if os.path.isfile(full) and full not in named:
                named.append(full)
    return named


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

    stdout = None
    if isinstance(response, dict):  # Claude Code: stdout; Kai's terminal: output
        for key in ("stdout", "output"):
            if isinstance(response.get(key), str):
                stdout = response[key]
                break
    for full in paths:
        ev: Dict[str, Any] = {"kind": "read", "path": full, "tool": "Bash", "via": "shell"}
        if len(paths) == 1 and isinstance(stdout, str):
            ev["chars"] = len(stdout)  # `head -40 x.md` returned 40 lines, not the file
        events.append(ev)

    if _SHELL_SCAN.search(command):
        hits = _scan_hits(response, cwd)
        # `grep foo x.md` prints its matches without the filename, so stdout carries
        # no path and the file never turned yellow. When the command's grep/rg names
        # exactly one file and returned anything at all, that file is the hit.
        text = stdout if isinstance(stdout, str) else response if isinstance(response, str) else ""
        if text.strip():
            named = _named_search_files(command, cwd)
            if len(named) == 1 and named[0] not in hits:
                hits.append(named[0])
        events.append(
            {
                "kind": "scan",
                "tool": "Bash",
                "pattern": command[:80],
                "hits": hits,
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
        for key in ("stdout", "output", "content", "result", "matches_text"):
            if isinstance(response.get(key), str):  # matches_text: Kai's search_files
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


def _append_events(events: List[Dict[str, Any]]) -> None:
    """Append events to the log with one O_APPEND write().

    Several sessions append to this file at the same time, and a buffered text
    writer splits a long line (a Grep with hundreds of hits) into chunks that
    interleave.
    """
    line = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
    HOME.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(str(EVENTS), flags, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


# Cursor's before-hooks are gates: an observer must answer, or the agent waits on it.
CURSOR_GATES = {"beforeReadFile", "beforeShellExecution", "beforeMCPExecution", "beforeTabFileRead"}


def _reply(agent: Optional[str], event: Optional[str]) -> Optional[Dict[str, Any]]:
    """What the calling agent expects on stdout. None means print nothing."""
    if agent == "cursor":
        if event in CURSOR_GATES:
            return {"permission": "allow"}
        if event == "beforeSubmitPrompt":
            return {"continue": True}
        if event == "stop":
            return {}
        return None
    if agent == "antigravity":
        return {}
    return None


def _say(reply: Optional[Dict[str, Any]]) -> None:
    if reply is not None:
        sys.stdout.write(json.dumps(reply))
        sys.stdout.flush()


def _guess_agent(payload: Dict[str, Any]) -> str:
    """Which agent sent this payload, from its shape. `--agent` or VAULT_RADAR_AGENT win."""
    if any(key in payload for key in ("toolCall", "conversationId", "invocationNum")):
        return "antigravity"
    if "conversation_id" in payload:
        return "cursor"
    raw = str(payload.get("hook_event_name") or "")
    if raw in ("post_tool_call", "pre_llm_call", "on_session_end") or isinstance(payload.get("extra"), dict):
        return "kai"
    tool = str(payload.get("tool_name") or "").lower()
    if tool in ("exec_command", "shell", "local_shell", "apply_patch"):
        return "codex"
    if (str(payload.get("session_id") or "").startswith("session_") or "tool_output" in payload
            or isinstance(payload.get("prompt"), list)):
        return "kimi"
    return "claude"


def _workspace(payload: Dict[str, Any]) -> str:
    """The directory relative paths are anchored to: cwd, else the first workspace root."""
    if isinstance(payload.get("cwd"), str) and payload["cwd"]:
        return payload["cwd"]
    for key in ("workspace_roots", "workspacePaths"):
        roots = payload.get(key)
        if isinstance(roots, list) and roots and isinstance(roots[0], str):
            return roots[0]
    return os.environ.get("CURSOR_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR") or ""


def _from_cursor(payload: Dict[str, Any], event: str) -> List[Dict[str, Any]]:
    """Cursor hooks (3.17): one payload per event, fields named per event."""
    cwd = _workspace(payload)
    if event == "beforeReadFile":
        path = payload.get("file_path")
        if not path:
            return []
        ev: Dict[str, Any] = {"kind": "read", "path": _absolute(str(path), cwd), "tool": event}
        if isinstance(payload.get("content"), str):
            ev["chars"] = len(payload["content"])  # what the agent is about to see
        return [ev]
    if event == "afterShellExecution":
        output = payload.get("output") if isinstance(payload.get("output"), str) else ""
        return _from_shell(str(payload.get("command") or ""), {"stdout": output}, cwd)
    if event == "beforeSubmitPrompt":
        return [{"kind": "prompt", "text": str(payload.get("prompt") or "")[:400]}]
    if event == "stop":
        return [{"kind": "stop"}]
    return []


def _from_antigravity(payload: Dict[str, Any], event: str) -> List[Dict[str, Any]]:
    """Antigravity hooks (2.5): `toolCall` payloads carry the args but never the tool's output."""
    cwd = _workspace(payload)
    if event == "PreInvocation":
        n = payload.get("invocationNum")
        return [{"kind": "prompt", "text": "antigravity · invocation %s" % n if n is not None else "antigravity"}]
    if event == "Stop":
        return [{"kind": "stop"}]
    if event != "PostToolUse":
        return []
    call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    name = str(call.get("name") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    if name in ("view_file", "view_code_item", "view_file_outline"):
        path = args.get("AbsolutePath") or args.get("File")
        if not path:
            return []
        return [{"kind": "read", "path": _absolute(str(path), cwd), "tool": name}]
    if name == "run_command":
        return _from_shell(str(args.get("CommandLine") or ""), None, str(args.get("Cwd") or cwd))
    if name in ("grep_search", "find_by_name", "codebase_search"):
        pattern = args.get("Query") or args.get("Pattern") or ""
        return [{"kind": "scan", "tool": name, "pattern": str(pattern), "hits": []}]  # no output in the payload
    _note_tool(name)
    return []


USAGE_CACHE = HOME / "usage.json"


def _usage_record(line: str) -> Optional[Dict[str, Any]]:
    """The usage dict inside one transcript line, or None."""
    if '"usage"' not in line:
        return None
    try:
        rec = json.loads(line)
    except Exception:
        return None  # a tail read usually cuts its first line in half
    if not isinstance(rec, dict):
        return None
    usage = (rec.get("message") or {}).get("usage") if isinstance(rec.get("message"), dict) else None
    if not isinstance(usage, dict):
        usage = rec.get("usage")
    return usage if isinstance(usage, dict) else None


def _is_prompt(line: str) -> bool:
    """A user turn typed by the person: a user record whose content is text, not tool results."""
    if '"user"' not in line:
        return False
    try:
        rec = json.loads(line)
    except Exception:
        return False
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return False
    content = (rec.get("message") or {}).get("content") if isinstance(rec.get("message"), dict) else None
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return bool(content) and all(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def _transcript_usage(path: Any) -> Optional[Dict[str, int]]:
    """Token use from an agent transcript. Two numbers from its last usage record: the
    context as it stands now (input + cache creation + cache read) and that turn's
    output. Two more across the whole file: output tokens in total and prompts typed.
    The whole-file pair is kept in a small cache keyed on the transcript path and only
    the bytes appended since the last hook are read, so a long session stays cheap.
    A missing or odd file yields None."""
    try:
        if not path:
            return None
        fp = Path(str(path)).expanduser()
        size = fp.stat().st_size
        cache: Dict[str, Any] = {}
        try:
            cache = json.loads(USAGE_CACHE.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        key = str(fp)
        state = cache.get(key) if isinstance(cache.get(key), dict) else None
        if not state or int(state.get("offset", 0)) > size:
            state = {"offset": 0, "total_out": 0, "turns": 0, "partial": ""}
        last: Optional[Dict[str, Any]] = None
        with fp.open("rb") as fh:
            fh.seek(int(state["offset"]))
            chunk = fh.read()
        text = str(state.get("partial", "")) + chunk.decode("utf-8", "replace")
        lines = text.split("\n")
        state["partial"] = lines.pop()  # an unfinished last line waits for the next hook
        for line in lines:
            usage = _usage_record(line)
            if usage:
                state["total_out"] += int(usage.get("output_tokens") or 0)
                last = usage
            elif _is_prompt(line):
                state["turns"] += 1
        state["offset"] = size - len(state["partial"].encode("utf-8"))
        state["last"] = last or state.get("last")
        cache[key] = state
        try:
            USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            USAGE_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            pass  # the cache is a saving, not a requirement
        usage = state.get("last")
        if not isinstance(usage, dict):
            return None
        n = lambda k: int(usage.get(k) or 0)  # noqa: E731
        return {"context": n("input_tokens") + n("cache_creation_input_tokens") + n("cache_read_input_tokens"),
                "output": n("output_tokens"), "total_out": int(state["total_out"]), "turns": int(state["turns"])}
    except Exception:
        return None


def cmd_hook(args: argparse.Namespace) -> int:
    """Read one hook payload on stdin and append a radar event. Never blocks the agent."""
    agent = getattr(args, "agent", None) or os.environ.get("VAULT_RADAR_AGENT") or None
    event = getattr(args, "event", None) or None
    reply = _reply(agent, event)  # decided from the flags, so a bad payload still gets its answer
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except Exception:
        _say(reply)
        return 0  # a malformed payload must never break the agent's turn

    try:
        agent = agent or _guess_agent(payload)
        event = event or str(payload.get("hook_event_name") or "")
        if reply is None:
            reply = _reply(agent, event)
        if agent == "cursor":
            events = _from_cursor(payload, event)
        elif agent == "antigravity":
            events = _from_antigravity(payload, event)
        else:
            events = _extract(payload)
        if events:
            session = payload.get("session_id") or payload.get("conversation_id") or payload.get("conversationId") or ""
            cwd = _workspace(payload) if agent in ("cursor", "antigravity") else payload.get("cwd", "")
            stamp = dict(ts=time.time(), session=str(session), cwd=cwd, agent=agent)
            # A subagent's tool calls fire the same hooks and carry these two fields. The
            # viewers draw those reads apart: they never entered the main context.
            for key in ("agent_id", "agent_type"):
                if payload.get(key):
                    stamp[key] = str(payload[key])
            usage = _transcript_usage(payload.get("transcript_path"))
            if usage:
                stamp["usage"] = usage
            for ev in events:
                ev.update(stamp)
            _append_events(events)
    except Exception:
        pass  # radar is an observer; it is never allowed to fail loudly
    _say(reply)
    return 0


# ──────────────────────────────────────────── vault scan


SKIP_DIRS = {".git", "node_modules", ".obsidian", "__pycache__", ".venv", "venv"}


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


WIKILINK = re.compile(r"\[\[([^\]|#]+)")
# [text](target) and [text](<target with spaces>), with an optional "title"
MDLINK = re.compile(r"\[[^\]]*\]\(<([^>]+)>\)|\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def scan_links(root: Path, files: List[Dict[str, Any]]) -> List[List[int]]:
    """Resolve [[wikilinks]] and [text](path.md) links between files into index pairs.

    A target is tried as a path relative to the linking file, then to the vault
    root, then by basename, which is how Obsidian resolves a bare wikilink. URLs,
    anchors and mail links are dropped, as are links pointing outside the vault.
    Without markdown links a vault written in that style is a cloud of dots.
    """
    by_path: Dict[str, int] = {f["path"]: i for i, f in enumerate(files)}
    by_stem: Dict[str, int] = {}
    for i, f in enumerate(files):
        by_stem.setdefault(Path(f["path"]).stem, i)

    def resolve(raw: str, here: Path) -> Optional[int]:
        target = unquote(raw.strip()).split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith("mailto:"):
            return None
        for base in (here.parent, Path("")):
            j = by_path.get(os.path.normpath(str(base / target)))
            if j is not None:
                return j
        return by_stem.get(Path(target).stem)

    seen = set()
    edges: List[List[int]] = []
    for i, f in enumerate(files):
        try:
            text = Path(f["abs"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        here = Path(f["path"])
        targets = WIKILINK.findall(text) + [a or b for a, b in MDLINK.findall(text)]
        for raw in targets:
            j = resolve(raw, here)
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
                    # The log can shrink under us: deleted by hand, then recreated
                    # at size 0 by the next hook. Without this the stale
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


def pinned_session() -> Optional[str]:
    """The session the radar is pinned to, or None to follow whichever prompted last.

    The pin is the contents of ``~/.vault-radar/follow``: a session id, or a unique
    prefix of one. The Obsidian plugin reads the same file, so one ``radar.py follow``
    pins both. Without it, every prompt from any session takes the display over,
    which on a machine running several sessions means it never holds still.
    """
    try:
        pin = FOLLOW.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pin or None


def _same_session(row: Dict[str, Any], session: Optional[str]) -> bool:
    """True if the event belongs to the session, or names no session at all."""
    sid = row.get("session")
    if not sid or not session:
        return True
    return str(sid).startswith(session)


def current_turn(limit: int = 4000) -> List[str]:
    """Return the events of the turn in progress: everything after the last prompt,
    or after the last RESET if that came later.

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
    # silently steals the viewer. A pin (see pinned_session) names the session
    # to anchor on instead, so a busy neighbour cannot steal it at all.
    pin = pinned_session()
    start, session, reset_at = 0, pin, None
    for i in range(len(lines) - 1, -1, -1):
        try:
            row = json.loads(lines[i])
        except ValueError:
            continue
        if row.get("kind") == "reset" and reset_at is None:
            reset_at = i  # RESET was pressed during this turn: replay from there
        if row.get("kind") == "prompt" and _same_session(row, pin):
            start, session = i, (row.get("session") or pin)
            break
    if reset_at is not None:
        start = max(start, reset_at + 1)

    out = []
    for ln in lines[start:]:
        if not ln.strip():
            continue
        try:
            if session and not _same_session(json.loads(ln), session):
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


def load_themes() -> Dict[str, Dict[str, Any]]:
    """Load user themes without letting names escape the theme directory."""
    themes: Dict[str, Dict[str, Any]] = {}
    if not THEMES_DIR.is_dir():
        return themes
    for path in sorted(THEMES_DIR.glob("*.json")):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", path.stem):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            themes[path.stem] = value
    return themes


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
                        "follow": pinned_session(),
                        "files": files,
                        "edges": scan_links(vault, files),
                        "total_tokens": sum(f["tokens"] for f in files),
                    },
                    ensure_ascii=False,
                ).encode()
                return self._send(200, body, "application/json; charset=utf-8")

            if route == "/api/themes":
                body = json.dumps(load_themes(), ensure_ascii=False).encode()
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
                # RESET clears displays, not the log: a marker every open stream
                # sees, and the point a fresh stream replays from. Deleting the
                # file here once wiped a day's events for every agent.
                try:
                    _append_events([{"kind": "reset", "ts": time.time()}])
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
                    pin = pinned_session()  # re-read: the pin can change mid-stream
                    if pin:
                        try:
                            if not _same_session(json.loads(line), pin):
                                continue
                        except ValueError:
                            pass
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
        threading.Timer(0.6, open_window, args=(viewer_url(args.port), args.width)).start()
        print("        (opening a docked window — pass --no-open to skip)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


BUILTIN_THEME_NAMES = ("radar", "obsidian", "sonar", "sonar-green", "constellation", "blueprint")


def _theme_file() -> Path:
    return Path(HOME) / "theme"


def recorded_theme() -> Optional[str]:
    """The opening theme recorded by `radar.py theme <name>`; None means the page default."""
    try:
        name = _theme_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def known_themes() -> List[str]:
    names = list(BUILTIN_THEME_NAMES)
    try:
        names += [n for n in load_themes() if n not in names]
    except Exception:
        pass
    return names


def viewer_url(port: int) -> str:
    """Viewer URL for window/serve: the recorded opening theme rides along as ?theme=."""
    base = f"http://localhost:{port}"
    theme = recorded_theme()
    return f"{base}/?theme={theme}" if theme else base


def cmd_theme(args: argparse.Namespace) -> int:
    """Record the opening theme, or print the recorded one."""
    if not args.name:
        print(recorded_theme() or "radar")
        return 0
    names = known_themes()
    if args.name not in names:
        print("unknown theme: %s (known: %s)" % (args.name, ", ".join(names)), file=sys.stderr)
        return 1
    path = _theme_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args.name + "\n", encoding="utf-8")
    print(args.name)
    return 0


def cmd_window(args: argparse.Namespace) -> int:
    """Open the existing viewer without starting or owning a server."""
    import urllib.error
    import urllib.request

    url = f"http://localhost:{args.port}"
    try:
        with urllib.request.urlopen(url + "/api/vault", timeout=args.timeout) as response:
            status = response.status
    except (OSError, urllib.error.URLError):
        print(f"vault-radar server is not reachable at {url}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"vault-radar server at {url} returned HTTP {status}", file=sys.stderr)
        return 1
    target = viewer_url(args.port)
    open_window(target, args.width)
    print(f"opened existing viewer: {target}")
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


def cmd_follow(args: argparse.Namespace) -> int:
    """Pin the viewer and the Obsidian plugin to one session, or release the pin."""
    if args.off:
        try:
            FOLLOW.unlink()
        except FileNotFoundError:
            pass
        print("following the last prompt")
        return 0
    session = (args.session or "").strip()
    if args.last:
        try:
            lines = EVENTS.read_text(encoding="utf-8").split("\n")
        except OSError:
            lines = []
        for ln in reversed(lines):
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if row.get("kind") == "prompt" and row.get("session"):
                session = str(row["session"])
                break
        if not session:
            print("no prompt in the log yet", file=sys.stderr)
            return 1
    if session:
        HOME.mkdir(parents=True, exist_ok=True)
        FOLLOW.write_text(session + "\n", encoding="utf-8")
        print(f"pinned to session {session}")
        return 0
    pin = pinned_session()
    print(f"pinned to session {pin}" if pin else "following the last prompt")
    return 0


# ──────────────────────────────────────────── agents: who is hooked, who touches the vault

HOOKED = HOME / "hooked.json"
OFFERS = HOME / "offers.json"
HOOK_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
SELF = str(Path(__file__).resolve())

# lsof's command name -> agent surface. Runtimes (python, node) are not surfaces: Kai and
# Claude Code run as those and are hooked already, so an unknown runtime is ignored.
AGENT_PROCESSES = [
    ("cursor", ("Cursor",)),
    ("antigravity", ("Antigravity",)),
    ("codex", ("Codex", "codex")),
    ("vscode", ("Code", "Visual Studio Code")),
    ("windsurf", ("Windsurf",)),
    ("zed", ("zed", "Zed")),
    ("claude", ("Claude",)),
    ("kai", ("kai", "Kai")),
    ("kimi", ("Kimi", "kimi")),
]


def _agent_of(command: str) -> Optional[str]:
    for agent, names in AGENT_PROCESSES:
        for name in names:
            if re.match(r"^" + re.escape(name) + r"(?:\b|$)", command):
                return agent
    return None


def _parse_lsof(text: str, vault: str) -> List[Dict[str, Any]]:
    """`lsof -F pcn` output -> one entry per agent surface with the vault files it holds open."""
    vault = os.path.normpath(os.path.expanduser(vault))
    found: Dict[str, Dict[str, Any]] = {}
    pid: Optional[int] = None
    command = ""
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
            command = ""
        elif tag == "c":
            command = value
        elif tag == "n" and pid is not None:
            path = os.path.normpath(value)
            if not path.startswith(vault + os.sep):
                continue
            if path[len(vault) + 1:].split(os.sep)[0] in (".git", ".obsidian"):
                continue
            agent = _agent_of(command)
            if not agent:
                continue
            entry = found.setdefault(agent, {"agent": agent, "pid": pid, "command": command, "files": []})
            if path not in entry["files"]:
                entry["files"].append(path)
    return list(found.values())


def _lsof_vault(vault: str) -> List[Dict[str, Any]]:
    try:
        proc = subprocess.run(["lsof", "+D", os.path.expanduser(vault), "-F", "pcn"],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_lsof(proc.stdout, vault)  # lsof exits 1 when nothing is open; that is not an error


# IDEs hold no vault file open between reads; they record the folder they have open under
# ~/Library/Application Support/<app>/User/workspaceStorage/<hash>/workspace.json instead.
WORKSPACE_STORES = [("cursor", "Cursor"), ("antigravity", "Antigravity IDE"),
                    ("vscode", "Code"), ("windsurf", "Windsurf")]


def _default_app_support() -> str:
    return os.environ.get("VAULT_RADAR_APP_SUPPORT") or str(Path.home() / "Library" / "Application Support")


def _folder_of_uri(uri: Any) -> Optional[str]:
    if not isinstance(uri, str):
        return None
    if uri.startswith("file://"):
        uri = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
    return os.path.normpath(uri) if uri.startswith("/") else None


def _workspace_hits(vault: str, app_support: Optional[str] = None) -> List[Dict[str, Any]]:
    """IDE workspace records that point at the vault (or inside it) -> one entry per agent."""
    vault = os.path.normpath(os.path.expanduser(vault))
    root = Path(os.path.expanduser(app_support or _default_app_support()))
    found: Dict[str, Dict[str, Any]] = {}
    for agent, dirname in WORKSPACE_STORES:
        for ws in sorted((root / dirname / "User" / "workspaceStorage").glob("*/workspace.json")):
            data = _load_json(ws, None)
            folder = _folder_of_uri(data.get("folder")) if isinstance(data, dict) else None
            if not folder or not (folder == vault or folder.startswith(vault + os.sep)):
                continue
            found.setdefault(agent, {"agent": agent, "evidence": ["workspace"], "workspace": str(ws), "folder": folder})
    return list(found.values())


def _vault_users(vault: str, lsof_text: Optional[str] = None, app_support: Optional[str] = None) -> List[Dict[str, Any]]:
    """lsof evidence (files held open now) merged with workspace evidence, one entry per agent."""
    merged: Dict[str, Dict[str, Any]] = {}
    lsof_entries = _parse_lsof(lsof_text, vault) if lsof_text is not None else _lsof_vault(vault)
    for entry in lsof_entries:
        entry.setdefault("evidence", ["lsof"])
        merged[entry["agent"]] = entry
    for entry in _workspace_hits(vault, app_support):
        cur = merged.get(entry["agent"])
        if cur is None:
            merged[entry["agent"]] = entry
        else:
            cur["evidence"] = sorted(set(cur.get("evidence", [])) | {"workspace"})
            cur["workspace"], cur["folder"] = entry["workspace"], entry["folder"]
    return list(merged.values())


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _hooked() -> Dict[str, Any]:
    reg = _load_json(HOOKED, {})
    agents = reg.get("agents") if isinstance(reg, dict) else None
    return agents if isinstance(agents, dict) else {}


def _notify(title: str, text: str) -> None:
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"'
                        % (text.replace('"', "'"), title.replace('"', "'"))],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _offer(found: List[Dict[str, Any]], checks_dir: Path, notify: bool) -> int:
    """One draft kaiChecks item per unhooked surface seen holding vault files. Asked once."""
    hooked = _hooked()
    offers = _load_json(OFFERS, {})
    offered = offers.get("offered") if isinstance(offers, dict) and isinstance(offers.get("offered"), dict) else {}
    program_path = checks_dir / "radar-hook-coverage.json"
    program = _load_json(program_path, None)
    if not isinstance(program, dict) or not isinstance(program.get("items"), list):
        program = {"program": "Radar hook kapsamı: bütün ajanlar ve modeller",
                   "source": "radar.py agents offer — vault dosyası açık tutan hook'suz ajanlar", "items": []}
    new: List[str] = []
    for entry in found:
        agent = entry.get("agent") if isinstance(entry, dict) else None
        if not agent or agent in hooked or agent in offered:
            continue
        files = [str(f) for f in (entry.get("files") or [])]
        stamp = time.strftime("%Y-%m-%d %H:%M")
        if files:
            seen = "%s (pid %s) vault'ta %d dosya açık tuttu: %s" % (entry.get("command", ""), entry.get("pid"), len(files), ", ".join(files[:3]))
        else:
            seen = "çalışma alanı olarak %s kayıtlı (%s)" % (entry.get("folder", "?"), entry.get("workspace", "workspace.json"))
        recipe = ("`%s hook-install %s`" % (SELF, agent)) if agent in RECIPES else "tarif yok, elle bağlanır"
        program["items"].append({
            "id": "offer-" + agent,
            "title": "%s vault'u kullanıyor — hook'lansın mı?" % agent,
            "criterion": "%s: %s. Onay verilirse %s koşar ve olay üretimi "
                         "sondayla doğrulanır; reddedilirse bu madde silinir, ~/.vault-radar/offers.json kaydı kalır ve bir daha sorulmaz."
                         % (stamp, seen, recipe),
            "state": "draft", "at": time.strftime("%Y-%m-%d"), "by": "radar.py agents offer",
            "priority": len(program["items"]) + 1,
        })
        offered[agent] = {"at": stamp, "pid": entry.get("pid"), "command": entry.get("command"), "files": files[:10],
                          "evidence": entry.get("evidence"), "folder": entry.get("folder")}
        new.append(agent)
    if new:
        _dump_json(program_path, program)
        _dump_json(OFFERS, {"version": 1, "offered": offered})
        if notify:
            _notify("vault-radar", "%s vault'u kullanıyor ama hook'lu değil — kaiChecks'te soru var" % ", ".join(new))
    print(json.dumps({"offered": new}))
    return 0


def _recipe_cursor(existing: Any) -> Dict[str, Any]:
    cfg = existing if isinstance(existing, dict) else {}
    cfg.setdefault("version", 1)
    hooks = cfg.get("hooks") if isinstance(cfg.get("hooks"), dict) else {}
    cfg["hooks"] = hooks
    for event in ("beforeReadFile", "afterShellExecution", "beforeSubmitPrompt", "stop"):
        entries = hooks.get(event) if isinstance(hooks.get(event), list) else []
        hooks[event] = entries
        if not any(isinstance(h, dict) and "radar.py hook --agent cursor" in str(h.get("command", "")) for h in entries):
            entries.append({"command": "%s %s hook --agent cursor --event %s" % (HOOK_PY, SELF, event), "timeout": 5})
    return cfg


def _recipe_antigravity(existing: Any) -> Dict[str, Any]:
    cfg = existing if isinstance(existing, dict) else {}

    def handler(event: str) -> Dict[str, Any]:
        return {"type": "command", "command": "%s %s hook --agent antigravity --event %s" % (HOOK_PY, SELF, event), "timeout": 5}

    cfg["vault-radar"] = {"enabled": True,
                          "PostToolUse": [{"matcher": "*", "hooks": [handler("PostToolUse")]}],
                          "PreInvocation": [handler("PreInvocation")],
                          "Stop": [handler("Stop")]}
    return cfg


RECIPES = {"cursor": ("~/.cursor/hooks.json", _recipe_cursor),
           "antigravity": ("~/.gemini/config/hooks.json", _recipe_antigravity)}
MANUAL = {"claude": "~/.claude/settings.json — `radar.py install` prints the snippet",
          "kai": "~/.kai/config.yaml hooks:",
          "codex": "~/.codex/hooks.json",
          "kimi": "~/.kimi-code/config.toml [[hooks]]"}


def cmd_agents(args: argparse.Namespace) -> int:
    checks_dir = Path(os.path.expanduser(args.checks_dir))
    if args.what == "classify":
        if not args.vault:
            print("--vault is required", file=sys.stderr)
            return 2
        print(json.dumps(_vault_users(args.vault, sys.stdin.read(), args.app_support), ensure_ascii=False))
        return 0
    if args.what == "offer":
        try:
            found = json.loads(sys.stdin.read() or "[]")
        except ValueError:
            found = []
        return _offer(found if isinstance(found, list) else [], checks_dir, notify=not args.no_notify)
    if args.what == "watch":
        if not args.vault:
            print("--vault is required", file=sys.stderr)
            return 2
        return _offer(_vault_users(args.vault, None, args.app_support), checks_dir, notify=not args.no_notify)
    if args.what == "install-watch":
        if not args.vault:
            print("--vault is required", file=sys.stderr)
            return 2
        plist = Path.home() / "Library" / "LaunchAgents" / "ai.karbon.vault-radar-watch.plist"
        prog = [HOOK_PY, SELF, "agents", "watch", "--vault", os.path.expanduser(args.vault), "--checks-dir", str(checks_dir)]
        body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
                "<plist version=\"1.0\"><dict>\n<key>Label</key><string>ai.karbon.vault-radar-watch</string>\n"
                "<key>ProgramArguments</key><array>\n" + "".join("<string>%s</string>\n" % a for a in prog) + "</array>\n"
                "<key>StartInterval</key><integer>300</integer>\n<key>RunAtLoad</key><true/>\n"
                "<key>StandardOutPath</key><string>%s</string>\n<key>StandardErrorPath</key><string>%s</string>\n"
                "</dict></plist>\n" % (HOME / "watch.log", HOME / "watch.log"))
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(body, encoding="utf-8")
        print("wrote %s\nload with: launchctl bootstrap gui/$(id -u) %s" % (plist, plist))
        return 0
    return 2


def cmd_hook_install(args: argparse.Namespace) -> int:
    if args.list:
        reg = _hooked()
        for agent in sorted(set(reg) | set(RECIPES) | set(MANUAL)):
            where = reg.get(agent, {}).get("config") or (RECIPES[agent][0] if agent in RECIPES else MANUAL.get(agent, ""))
            print("%-12s %-11s %s" % (agent, "hooked" if agent in reg else "not hooked", where))
        return 0
    agent = args.agent
    if not agent:
        print("agent name or --list required", file=sys.stderr)
        return 2
    if agent in RECIPES:
        default_path, recipe = RECIPES[agent]
        path = Path(os.path.expanduser(args.config or default_path))
        existing = _load_json(path, None)
        new = recipe(json.loads(json.dumps(existing)) if isinstance(existing, dict) else None)
        if isinstance(existing, dict) and json.dumps(new, sort_keys=True) == json.dumps(existing, sort_keys=True):
            print("%s: already installed at %s" % (agent, path))
        else:
            if path.exists():
                backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(path, backup)
                print("backup: %s" % backup)
            _dump_json(path, new)
            print("%s: hook config written to %s" % (agent, path))
        reg = _load_json(HOOKED, {})
        agents = reg.get("agents") if isinstance(reg, dict) and isinstance(reg.get("agents"), dict) else {}
        entry = agents.get(agent) if isinstance(agents.get(agent), dict) else {}
        entry.update({"config": str(path), "since": entry.get("since") or time.strftime("%Y-%m-%dT%H:%M"),
                      "verified": entry.get("verified") or "config written, no live probe yet"})
        agents[agent] = entry
        _dump_json(HOOKED, {"version": 1, "agents": agents})
        return 0
    if agent in MANUAL:
        print("%s: %s — %s" % (agent, "hooked" if agent in _hooked() else "not hooked", MANUAL[agent]))
        return 0
    print("unknown agent %r; known: %s" % (agent, ", ".join(sorted(set(RECIPES) | set(MANUAL)))), file=sys.stderr)
    return 2



def main() -> int:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    hook = sub.add_parser("hook", help="consume one hook payload on stdin")
    hook.add_argument("--agent", help="who is calling: claude | kai | kimi | codex | cursor | antigravity (guessed from the payload when omitted)")
    hook.add_argument("--event", help="the hook event, for agents whose payload does not name it (Antigravity) or whose reply depends on it (Cursor)")
    hook.set_defaults(fn=cmd_hook)

    serve = sub.add_parser("serve", help="serve the live viewer")
    serve.add_argument("--vault", required=True, help="directory to watch")
    serve.add_argument("--port", type=int, default=7777)
    serve.add_argument("--ext", default=".md", help="comma-separated extensions (default: .md)")
    serve.add_argument("--no-open", action="store_true", help="do not open a window")
    serve.add_argument("--width", type=int, default=520, help="docked window width (default: 520)")
    serve.set_defaults(fn=cmd_serve)

    window = sub.add_parser("window", help="open the viewer from an already-running server")
    window.add_argument("--port", type=int, default=7777, help="existing viewer port (default: 7777)")
    window.add_argument("--width", type=int, default=520, help="docked window width (default: 520)")
    window.add_argument("--timeout", type=float, default=2.0, help="health-check timeout in seconds")
    window.set_defaults(fn=cmd_window)

    sub.add_parser("install", help="print the settings.json snippet").set_defaults(fn=cmd_install)

    follow = sub.add_parser("follow", help="pin the viewer and the Obsidian plugin to one session")
    follow.add_argument("session", nargs="?", help="session id, or a unique prefix of one")
    follow.add_argument("--last", action="store_true", help="pin to the session that prompted last")
    follow.add_argument("--off", action="store_true", help="follow the last prompt again (default)")
    follow.set_defaults(fn=cmd_follow)

    agents = sub.add_parser("agents", help="who holds vault files open, who is hooked; offer to hook the rest")
    agents.add_argument("what", choices=["classify", "offer", "watch", "install-watch"])
    agents.add_argument("--vault", help="vault root (classify, watch, install-watch)")
    agents.add_argument("--checks-dir", default=str(Path(os.environ.get("KAI_HOME") or "~/.kai").expanduser() / "checks"),
                        help="where the kaiChecks programme files live")
    agents.add_argument("--no-notify", action="store_true", help="no macOS notification on a new offer")
    agents.add_argument("--app-support", help="IDE workspace records root (default ~/Library/Application Support)")
    agents.set_defaults(fn=cmd_agents)

    theme = sub.add_parser("theme", help="record the opening theme used by window and serve (no name: print it)")
    theme.add_argument("name", nargs="?", help="radar | obsidian | sonar | sonar-green | constellation | blueprint | a ~/.vault-radar/themes name")
    theme.set_defaults(fn=cmd_theme)

    install = sub.add_parser("hook-install", help="write an agent's radar hook config, idempotently")
    install.add_argument("agent", nargs="?", help="cursor | antigravity (recipes); claude | kai | codex | kimi (manual)")
    install.add_argument("--config", help="config file to write instead of the agent's default")
    install.add_argument("--list", action="store_true", help="print the registry")
    install.set_defaults(fn=cmd_hook_install)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

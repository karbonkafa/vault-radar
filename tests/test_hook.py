#!/usr/bin/env python3
"""Hook regression test: recorded payloads in, expected events out.

Runs ``radar.py hook`` as a subprocess against a temporary vault, one payload per
case, and compares the events it appends with what a reader of the graph should see.
Then one ``radar.py serve`` on a free port, for what the viewer's RESET may touch.
Payload shapes are the ones each agent actually sends: Claude Code (hooks reference),
Kai (``agent/shell_hooks.py`` ``_serialize_payload`` plus its tools' JSON results),
Kimi Code and Codex (observed in ``~/.vault-radar/events.jsonl``). Stdlib only.

    python3 tests/test_hook.py            # the radar.py next to this directory
    RADAR=/path/to/radar.py python3 tests/test_hook.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RADAR = os.environ.get("RADAR") or os.path.join(os.path.dirname(HERE), "radar.py")

A = "foo line1\nline2\nline3\nfoo again\nline5\nline6\n"
B = "b1\nb2\nfoo\n"


def build_vault(root):
    files = {
        "notes/a.md": A,
        "notes/b.md": B,
        "README.md": "# readme\n",
        "LICENSE": "MIT\n",
        "index.md": "index\n",
        "notlar/göç planı.md": "göç\n",
    }
    for rel, text in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)


def server_checks(env, tv, events_path):
    """RESET clears displays, never the shared log. Returns [(name, ok, detail)]."""
    import socket
    import time
    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = "http://127.0.0.1:%d" % port
    err_path = os.path.join(os.path.dirname(events_path), "serve.err")
    err_fh = open(err_path, "w")
    proc = subprocess.Popen([sys.executable, RADAR, "serve", "--vault", tv, "--port", str(port), "--no-open"],
                            env=env, stdout=subprocess.DEVNULL, stderr=err_fh)
    results = []

    def open_stream():
        sock = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        sock.sendall(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port).encode())
        return sock

    def collect(sock, wait):
        """The `data:` frames a stream delivers within `wait` seconds."""
        buf, deadline = b"", time.time() + wait
        sock.settimeout(0.2)
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
        out = []
        for line in buf.decode("utf-8", "replace").split("\n"):
            if line.startswith("data: "):
                try:
                    out.append(json.loads(line[6:]))
                except ValueError:
                    pass
        return out

    try:
        for _ in range(100):  # up to 5 s for the server to come up
            try:
                urllib.request.urlopen(base + "/api/vault", timeout=1.0).read()
                break
            except Exception:
                time.sleep(0.05)
        else:
            err_fh.close()
            return [("serve: starts on a free port", False, open(err_path).read()[-300:])]

        seed = [json.dumps({"kind": "prompt", "text": "turn nine", "ts": time.time(), "session": "S9", "cwd": tv}),
                json.dumps({"kind": "read", "path": os.path.join(tv, "notes/a.md"), "tool": "Read",
                            "ts": time.time(), "session": "S9", "cwd": tv})]
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(seed) + "\n")
        with open(events_path, encoding="utf-8") as fh:
            before = fh.read()

        sock = open_stream()
        kinds = [(f.get("kind"), f.get("session")) for f in collect(sock, 1.0)]
        sock.close()
        results.append(("serve: a new stream replays the turn in progress",
                        kinds == [("prompt", "S9"), ("read", "S9")], kinds))

        watcher = open_stream()
        collect(watcher, 1.0)  # drain its replay; what follows is live
        resp = urllib.request.urlopen(
            urllib.request.Request(base + "/api/reset", data=b"", method="POST"), timeout=2.0)
        status = resp.status
        after = None
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as fh:
                after = fh.read()
        results.append(("reset: the log is kept, nothing deleted",
                        status == 200 and after is not None and after.startswith(before),
                        "status=%s exists=%s" % (status, after is not None)))

        seen = [f.get("kind") for f in collect(watcher, 2.0)]
        watcher.close()
        results.append(("reset: a stream that was open gets a reset frame", "reset" in seen, seen))

        sock = open_stream()
        got = [f.get("kind") for f in collect(sock, 1.0)]
        sock.close()
        results.append(("reset: a fresh stream replays nothing of the old turn",
                        [k for k in got if k != "reset"] == [], got))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err_fh.close()
    return results


def main():
    tmp = tempfile.mkdtemp(prefix="vault-radar-test-")
    tv = os.path.join(tmp, "tv")
    build_vault(tv)
    home = os.path.join(tmp, "home")
    env = dict(os.environ, VAULT_RADAR_HOME=home, TV=tv)  # TV: the env-var case below
    events_path = os.path.join(home, "events.jsonl")

    def p(rel):
        return os.path.normpath(os.path.join(tv, rel))

    # --- expected-event helpers -------------------------------------------------
    def rd(rel, chars=None, tool="Read", shell=False, agent=None):
        ev = {"kind": "read", "path": p(rel), "tool": "Bash" if shell else tool}
        if shell:
            ev["via"] = "shell"
        if chars is not None:
            ev["chars"] = chars
        if agent:
            ev["agent_id"], ev["agent_type"] = agent
        return ev

    def sc(pattern, hits, tool="Grep", shell=False, agent=None):
        ev = {"kind": "scan", "tool": "Bash" if shell else tool, "pattern": pattern,
              "hits": [p(h) for h in hits]}
        if shell:
            ev["via"] = "shell"
        if agent:
            ev["agent_id"], ev["agent_type"] = agent
        return ev

    # --- payload helpers ----------------------------------------------------------
    def claude(tool, tin, resp, cwd=tv, **more):
        d = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tin,
             "tool_response": resp, "session_id": "S1", "cwd": cwd}
        d.update(more)
        return d

    def sh(cmd, stdout="", cwd=tv, **more):
        return claude("Bash", {"command": cmd},
                      {"stdout": stdout, "stderr": "", "interrupted": False,
                       "isImage": False, "noOutputExpected": False}, cwd, **more)

    def kai(event, tool, args, **extra):
        return {"hook_event_name": event, "tool_name": tool, "tool_input": args,
                "session_id": "20260902_000000_kai", "cwd": tv, "extra": extra}

    kai_search = json.dumps({
        "total_count": 3,
        "matches_format": "path-grouped: each file path on its own line, followed by "
                          "indented '<line>: <content>' rows for matches in that file",
        "matches_text": p("notes/a.md") + "\n  1: foo line1\n  4: foo again\n"
                        + p("notes/b.md") + "\n  3: foo\n",
    })
    kai_read = json.dumps({"content": "1|foo line1\n2|line2\n"})

    cases = [
        # -- Claude Code: shell reads and scans -------------------------------------
        ("cat a; echo; cat b", sh("cat notes/a.md; echo ==; cat notes/b.md", A + "==\n" + B),
         [rd("notes/a.md", shell=True), rd("notes/b.md", shell=True)]),
        ("cat a b README", sh("cat notes/a.md notes/b.md README.md"),
         [rd("notes/a.md", shell=True), rd("notes/b.md", shell=True), rd("README.md", shell=True)]),
        ("sed -n 1,2p a (chars)", sh("sed -n '1,2p' notes/a.md", "foo line1\nline2\n"),
         [rd("notes/a.md", 16, shell=True)]),
        ("sed -i (write, no read)", sh("sed -i '' 's/x/y/' notes/a.md"), []),
        ("quoted path with space", sh("cat 'notlar/göç planı.md'", "göç\n"),
         [rd("notlar/göç planı.md", 4, shell=True)]),
        ("cat LICENSE (no ext, no slash)", sh("cat LICENSE", "MIT\n"), []),
        ("cat ./LICENSE (slash)", sh("cat ./LICENSE", "MIT\n"), [rd("LICENSE", 4, shell=True)]),
        ("cat $TV/notes/a.md (env var)", dict(sh('cat "$TV/notes/a.md"', A)), [rd("notes/a.md", 44, shell=True)]),
        ("cat ~ path (missing file)", sh("cat ~/definitely-missing-xyz.md"), []),
        ("grep -rn (2 hits)", sh("grep -rn foo notes", "notes/a.md:1:foo line1\nnotes/a.md:4:foo again\nnotes/b.md:3:foo\n"),
         [sc("grep -rn foo notes", ["notes/a.md", "notes/b.md"], shell=True)]),
        ("grep -rl ./ (2 hits)", sh("grep -rl foo .", "./notes/a.md\n./notes/b.md\n"),
         [sc("grep -rl foo .", ["notes/a.md", "notes/b.md"], shell=True)]),
        ("rg grouped output", sh("rg foo", "notes/a.md\n1:foo line1\n4:foo again\n\nnotes/b.md\n3:foo\n"),
         [sc("rg foo", ["notes/a.md", "notes/b.md"], shell=True)]),
        ("heredoc write", sh("cat > out.md <<'EOF'\nhello\nEOF"), []),
        ("script mentioning cat", sh("python3 - <<'EOF'\nprint('cat dog.txt')\nEOF", "cat dog.txt\n"), []),
        ("head | grep", sh("head -3 notes/a.md | grep foo", "foo line1\n"),
         [rd("notes/a.md", 10, shell=True), sc("head -3 notes/a.md | grep foo", [], shell=True)]),
        ("git log --grep (scan, no hits)", sh("git log --grep=fix", "abc fix thing\n"),
         [sc("git log --grep=fix", [], shell=True)]),
        # -- a grep that names one file prints no filename: the hit is the argument ----
        ("grep single file, relative (hit)", sh("grep -n foo notes/a.md", "1:foo line1\n4:foo again\n"),
         [sc("grep -n foo notes/a.md", ["notes/a.md"], shell=True)]),
        ("grep single file, absolute (hit)", sh("grep -n foo " + p("notes/a.md"), "1:foo line1\n"),
         [sc("grep -n foo " + p("notes/a.md"), ["notes/a.md"], shell=True)]),
        ("rg single file (hit)", sh("rg foo notes/b.md", "3:foo\n"),
         [sc("rg foo notes/b.md", ["notes/b.md"], shell=True)]),
        ("grep single file, no match (no hit)", sh("grep -n zzz notes/b.md", ""),
         [sc("grep -n zzz notes/b.md", [], shell=True)]),
        ("wc -l (nothing)", sh("wc -l notes/a.md", "6 notes/a.md\n"), []),
        # -- Claude Code: structured tools ------------------------------------------
        ("Read tool w/ content", claude("Read", {"file_path": p("notes/a.md"), "offset": 1, "limit": 2},
                                        {"type": "text", "file": {"filePath": p("notes/a.md"), "content": "foo line1\nline2\n",
                                                                  "numLines": 2, "startLine": 1, "totalLines": 6}}),
         [rd("notes/a.md", 16)]),
        ("Read tool relative path", claude("Read", {"file_path": "notes/b.md"},
                                           {"type": "text", "file": {"filePath": "notes/b.md", "content": B}}),
         [rd("notes/b.md", 10)]),
        ("Grep tool relative filenames", claude("Grep", {"pattern": "foo"},
                                                {"mode": "files_with_matches", "numFiles": 2, "filenames": ["notes/a.md", "notes/b.md"]}),
         [sc("foo", ["notes/a.md", "notes/b.md"])]),
        ("Glob absolute", claude("Glob", {"pattern": "**/*.md"},
                                 {"filenames": [p("index.md")], "durationMs": 3, "numFiles": 1, "truncated": False}),
         [sc("**/*.md", ["index.md"], tool="Glob")]),
        ("cwd missing, absolute ok", sh("cat " + p("index.md"), "index\n", cwd=""), [rd("index.md", 6, shell=True)]),
        ("subagent Read (agent fields)", claude("Read", {"file_path": p("notes/a.md")},
                                                {"type": "text", "file": {"filePath": p("notes/a.md"), "content": A}},
                                                agent_id="agent-abc", agent_type="Explore"),
         [rd("notes/a.md", 44, agent=("agent-abc", "Explore"))]),
        ("subagent Bash cat", sh("cat notes/b.md", B, agent_id="agent-abc", agent_type="Explore"),
         [rd("notes/b.md", 10, shell=True, agent=("agent-abc", "Explore"))]),
        ("subagent grep (scan)", sh("grep -rl foo notes", "notes/a.md\n", agent_id="agent-abc", agent_type="general-purpose"),
         [sc("grep -rl foo notes", ["notes/a.md"], shell=True, agent=("agent-abc", "general-purpose"))]),
        ("prompt", {"hook_event_name": "UserPromptSubmit", "prompt": "hello", "session_id": "S1", "cwd": tv},
         [{"kind": "prompt", "text": "hello"}]),
        ("stop", {"hook_event_name": "Stop", "session_id": "S1", "cwd": tv}, [{"kind": "stop"}]),
        # -- Kai: its own event names, tool names and JSON-string results -------------
        ("Kai read_file {content} (partial read)", kai("post_tool_call", "read_file", {"path": "notes/a.md", "offset": 1, "limit": 2}, result=kai_read, status="ok"),
         [rd("notes/a.md", len("1|foo line1\n2|line2\n"), tool="read_file")]),
        ("Kai search_files matches_text", kai("post_tool_call", "search_files", {"pattern": "foo", "path": "notes"}, result=kai_search, status="ok"),
         [sc("foo", ["notes/a.md", "notes/b.md"], tool="search_files")]),
        ("Kai terminal grep (output key)", kai("post_tool_call", "terminal", {"command": "grep -rn foo notes"},
                                               result=json.dumps({"output": "notes/a.md:1:foo line1\nnotes/b.md:3:foo\n", "exit_code": 0, "error": None})),
         [sc("grep -rn foo notes", ["notes/a.md", "notes/b.md"], shell=True)]),
        ("Kai terminal sed -n (chars from output)", kai("post_tool_call", "terminal", {"command": "sed -n '1,2p' notes/a.md"},
                                                        result=json.dumps({"output": "foo line1\nline2\n", "exit_code": 0, "error": None})),
         [rd("notes/a.md", 16, shell=True)]),
        ("Kai pre_llm_call (user_message)", {"hook_event_name": "pre_llm_call", "tool_name": None, "tool_input": None,
                                             "session_id": "20260902_000000_kai", "cwd": tv,
                                             "extra": {"user_message": "radar kai i takip ediyor mu", "conversation_history": [{"role": "user", "content": "x" * 5000}] * 40}},
         [{"kind": "prompt", "text": "radar kai i takip ediyor mu"}]),
        ("Kai on_session_end", {"hook_event_name": "on_session_end", "tool_name": None, "tool_input": None,
                                "session_id": "20260902_000000_kai", "cwd": tv, "extra": {"completed": True}},
         [{"kind": "stop"}]),
        # -- `kai hooks doctor` / `kai hooks test` send kai_cli/hooks.py's _DEFAULT_PAYLOADS
        #    through the real hook: session "test-session". A probe is not a turn. ----------
        ("Kai doctor pre_llm_call (probe, no event)",
         {"hook_event_name": "pre_llm_call", "tool_name": None, "tool_input": None, "session_id": "test-session",
          "cwd": tv, "extra": {"user_message": "What is the weather?", "conversation_history": [],
                               "is_first_turn": True, "model": "gpt-4", "platform": "cli"}},
         []),
        ("Kai doctor on_session_end (probe, no event)",
         {"hook_event_name": "on_session_end", "tool_name": None, "tool_input": None, "session_id": "test-session",
          "cwd": tv, "extra": {"task_id": "test-task", "turn_id": "test-turn", "completed": True, "failed": False,
                               "interrupted": False, "turn_exit_reason": "text_response(stop)", "model": "gpt-4",
                               "platform": "cli"}},
         []),
        ("Kai doctor post_tool_call (probe, no event)",
         {"hook_event_name": "post_tool_call", "tool_name": "terminal", "tool_input": {"command": "echo hello"},
          "session_id": "test-session", "cwd": tv,
          "extra": {"task_id": "test-task", "tool_call_id": "test-call", "result": '{"output": "hello"}',
                    "duration_ms": 42}},
         []),
        # -- Kimi Code ---------------------------------------------------------------
        ("Kimi prompt as content blocks", {"hook_event_name": "UserPromptSubmit", "prompt": [{"type": "text", "text": "read the file please"}],
                                           "session_id": "session_kimi", "cwd": tv},
         [{"kind": "prompt", "text": "read the file please"}]),
        ("Kimi ReadFile (path key, text output)", {"hook_event_name": "PostToolUse", "tool_name": "ReadFile", "tool_input": {"path": "notes/a.md"},
                                                   "tool_output": A, "session_id": "session_kimi", "cwd": tv},
         [rd("notes/a.md", 44, tool="ReadFile")]),
        # -- Codex-style shell tool names ---------------------------------------------
        ("Codex exec_command (cmd key)", {"hook_event_name": "PostToolUse", "tool_name": "exec_command", "tool_input": {"cmd": "cat notes/a.md"},
                                          "tool_response": {"stdout": A}, "session_id": "01a0-codex", "cwd": tv},
         [rd("notes/a.md", 44, shell=True)]),
        ("Codex shell grep -rl", {"hook_event_name": "PostToolUse", "tool_name": "shell", "tool_input": {"command": "grep -rl foo notes"},
                                  "tool_response": {"stdout": "notes/a.md\n"}, "session_id": "01a0-codex", "cwd": tv},
         [sc("grep -rl foo notes", ["notes/a.md"], shell=True)]),
        # -- never fail loudly ---------------------------------------------------------
        ("malformed payload", "not json", []),
    ]

    failures = 0
    for name, payload, expected in cases:
        before = 0
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as fh:
                before = sum(1 for _ in fh)
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run([sys.executable, RADAR, "hook"], input=raw, text=True,
                              capture_output=True, env=env)
        got = []
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as fh:
                lines = [l for l in fh.read().split("\n")[before:] if l.strip()]
            for line in lines:
                ev = json.loads(line)
                for k in ("ts", "session", "cwd", "agent"):
                    ev.pop(k, None)
                if "pattern" in ev:
                    ev["pattern"] = ev["pattern"][:30]
                got.append(ev)
        exp = []
        for ev in expected:
            ev = dict(ev)
            if "pattern" in ev:
                ev["pattern"] = ev["pattern"][:30]
            exp.append(ev)
        ok = proc.returncode == 0 and proc.stdout == "" and proc.stderr == "" and got == exp
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures += 1
            print("   rc=%s out=%r err=%r" % (proc.returncode, proc.stdout, proc.stderr[-200:]))
            print("   expected:", json.dumps(exp, ensure_ascii=False))
            print("   got:     ", json.dumps(got, ensure_ascii=False))
    server = server_checks(env, tv, events_path)
    for name, ok, detail in server:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures += 1
            print("   got:     ", json.dumps(detail, ensure_ascii=False))
    print("\n%d cases, %d failed (%s)" % (len(cases) + len(server), failures, RADAR))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

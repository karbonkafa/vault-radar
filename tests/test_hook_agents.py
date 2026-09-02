#!/usr/bin/env python3
"""Hook regression test, part two: Cursor and Antigravity payloads, and the agent tag.

Cursor (3.17, ``~/.cursor/hooks.json``) sends one JSON payload per event on stdin and,
for the ``before*`` events, expects a JSON decision on stdout; an observer answers
``{"permission": "allow"}`` / ``{"continue": true}`` / ``{}`` and exit 0. Antigravity
(2.5, ``~/.gemini/config/hooks.json``) sends ``toolCall`` / ``invocationNum`` payloads
without an event name and expects ``{}`` on stdout. Both are invoked with
``radar.py hook --agent <name> --event <name>`` so the hook never has to guess.

Every event the hook appends now carries ``agent``: claude | kai | kimi | codex |
cursor | antigravity, inferred from the payload when ``--agent`` is not given.

    python3 tests/test_hook_agents.py
    RADAR=/path/to/radar.py python3 tests/test_hook_agents.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RADAR = os.environ.get("RADAR") or os.path.join(os.path.dirname(HERE), "radar.py")

A = "foo line1\nline2\nline3\nfoo again\nline5\nline6\n"


def build_vault(root):
    os.makedirs(os.path.join(root, "notes"))
    with open(os.path.join(root, "notes", "a.md"), "w", encoding="utf-8") as fh:
        fh.write(A)


def main():
    tmp = tempfile.mkdtemp(prefix="vault-radar-agents-")
    tv = os.path.join(tmp, "tv")
    build_vault(tv)
    home = os.path.join(tmp, "home")
    env = dict(os.environ, VAULT_RADAR_HOME=home)
    for key in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "VAULT_RADAR_AGENT", "KAI_SESSION_ID"):
        env.pop(key, None)
    events_path = os.path.join(home, "events.jsonl")

    def p(rel):
        return os.path.normpath(os.path.join(tv, rel))

    def cursor(event, **fields):
        d = {"hook_event_name": event, "conversation_id": "conv-cursor", "generation_id": "g1",
             "workspace_roots": [tv]}
        d.update(fields)
        return d

    def antigravity(**fields):
        d = {"conversationId": "conv-ag", "workspacePaths": [tv], "modelName": "gemini-3.6-flash-medium",
             "transcriptPath": "~/.gemini/antigravity/brain/conv-ag/.system_generated/logs/transcript.jsonl"}
        d.update(fields)
        return d

    def read(rel, agent, session, chars=None, tool="Read", shell=False):
        ev = {"kind": "read", "path": p(rel), "tool": "Bash" if shell else tool, "agent": agent,
              "session": session, "cwd": tv}
        if shell:
            ev["via"] = "shell"
        if chars is not None:
            ev["chars"] = chars
        return ev

    ALLOW = {"permission": "allow"}
    CONT = {"continue": True}

    # (name, extra argv, payload, expected events, expected stdout ("" or a JSON object))
    cases = [
        # -- Cursor: explicit --agent/--event, decision JSON on stdout ---------------------
        ("Cursor beforeReadFile", ["--agent", "cursor", "--event", "beforeReadFile"],
         cursor("beforeReadFile", file_path=p("notes/a.md"), content=A, attachments=[]),
         [read("notes/a.md", "cursor", "conv-cursor", chars=44, tool="beforeReadFile")], ALLOW),
        ("Cursor beforeShellExecution (no event, allow)", ["--agent", "cursor", "--event", "beforeShellExecution"],
         cursor("beforeShellExecution", command="cat notes/a.md", cwd=tv, sandbox=False), [], ALLOW),
        ("Cursor afterShellExecution cat", ["--agent", "cursor", "--event", "afterShellExecution"],
         cursor("afterShellExecution", command="cat notes/a.md", output=A, duration=12, sandbox=False),
         [read("notes/a.md", "cursor", "conv-cursor", chars=44, shell=True)], ""),
        ("Cursor afterShellExecution grep -l", ["--agent", "cursor", "--event", "afterShellExecution"],
         cursor("afterShellExecution", command="grep -rl foo notes", output="notes/a.md\n", duration=3, sandbox=False),
         [{"kind": "scan", "tool": "Bash", "pattern": "grep -rl foo notes", "hits": [p("notes/a.md")], "via": "shell",
           "agent": "cursor", "session": "conv-cursor", "cwd": tv}], ""),
        ("Cursor beforeSubmitPrompt", ["--agent", "cursor", "--event", "beforeSubmitPrompt"],
         cursor("beforeSubmitPrompt", prompt="what did I write about swarms?", attachments=[]),
         [{"kind": "prompt", "text": "what did I write about swarms?", "agent": "cursor", "session": "conv-cursor", "cwd": tv}], CONT),
        ("Cursor afterFileEdit (write, no event)", ["--agent", "cursor", "--event", "afterFileEdit"],
         cursor("afterFileEdit", file_path=p("notes/a.md"), edits=[{"old_string": "x", "new_string": "y"}]), [], ""),
        ("Cursor stop", ["--agent", "cursor", "--event", "stop"],
         cursor("stop", status="completed", loop_count=1),
         [{"kind": "stop", "agent": "cursor", "session": "conv-cursor", "cwd": tv}], {}),
        # -- Antigravity: toolCall payloads, {} on stdout ------------------------------------
        ("Antigravity view_file", ["--agent", "antigravity", "--event", "PostToolUse"],
         antigravity(toolCall={"name": "view_file", "args": {"AbsolutePath": p("notes/a.md")}}, stepIdx=1),
         [read("notes/a.md", "antigravity", "conv-ag", tool="view_file")], {}),
        ("Antigravity run_command cat (no output in payload)", ["--agent", "antigravity", "--event", "PostToolUse"],
         antigravity(toolCall={"name": "run_command", "args": {"CommandLine": "cat notes/a.md", "Cwd": tv}}, stepIdx=2),
         [read("notes/a.md", "antigravity", "conv-ag", shell=True)], {}),
        ("Antigravity grep_search (hits unknown)", ["--agent", "antigravity", "--event", "PostToolUse"],
         antigravity(toolCall={"name": "grep_search", "args": {"Query": "foo", "SearchPath": tv}}, stepIdx=3),
         [{"kind": "scan", "tool": "grep_search", "pattern": "foo", "hits": [], "agent": "antigravity", "session": "conv-ag", "cwd": tv}], {}),
        ("Antigravity write_to_file (no event)", ["--agent", "antigravity", "--event", "PostToolUse"],
         antigravity(toolCall={"name": "write_to_file", "args": {"TargetFile": p("notes/a.md"), "CodeContent": "x"}}, stepIdx=4), [], {}),
        ("Antigravity PreInvocation as prompt", ["--agent", "antigravity", "--event", "PreInvocation"],
         antigravity(invocationNum=3, initialNumSteps=10),
         [{"kind": "prompt", "text": "antigravity · invocation 3", "agent": "antigravity", "session": "conv-ag", "cwd": tv}], {}),
        ("Antigravity Stop", ["--agent", "antigravity", "--event", "Stop"],
         antigravity(),
         [{"kind": "stop", "agent": "antigravity", "session": "conv-ag", "cwd": tv}], {}),
        # -- the four agents already wired: agent inferred from the payload ------------------
        ("Claude Read tagged claude", [],
         {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": p("notes/a.md")},
          "tool_response": {"file": {"content": A}}, "session_id": "S1", "cwd": tv},
         [read("notes/a.md", "claude", "S1", chars=44)], ""),
        ("Kai read_file tagged kai", [],
         {"hook_event_name": "post_tool_call", "tool_name": "read_file", "tool_input": {"path": "notes/a.md"},
          "session_id": "20260902_000000_kai", "cwd": tv, "extra": {"result": json.dumps({"content": "1|foo line1\n2|line2\n"})}},
         [read("notes/a.md", "kai", "20260902_000000_kai", chars=20, tool="read_file")], ""),
        ("Kimi ReadFile tagged kimi", [],
         {"hook_event_name": "PostToolUse", "tool_name": "ReadFile", "tool_input": {"path": "notes/a.md"},
          "tool_output": A, "session_id": "session_kimi", "cwd": tv},
         [read("notes/a.md", "kimi", "session_kimi", chars=44, tool="ReadFile")], ""),
        ("Codex exec_command tagged codex", [],
         {"hook_event_name": "PostToolUse", "tool_name": "exec_command", "tool_input": {"cmd": "cat notes/a.md"},
          "tool_response": {"stdout": A}, "session_id": "01a0-codex", "cwd": tv},
         [read("notes/a.md", "codex", "01a0-codex", chars=44, shell=True)], ""),
        ("--agent overrides the guess", ["--agent", "kimi"],
         {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": p("notes/a.md")},
          "tool_response": {"file": {"content": A}}, "session_id": "S2", "cwd": tv},
         [read("notes/a.md", "kimi", "S2", chars=44)], ""),
        # -- never fail loudly, whatever the agent -------------------------------------------
        ("Cursor malformed payload still allows", ["--agent", "cursor", "--event", "beforeReadFile"], "not json", [], ALLOW),
    ]

    failures = 0
    for name, argv, payload, expected, stdout_expected in cases:
        before = 0
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as fh:
                before = sum(1 for _ in fh)
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run([sys.executable, RADAR, "hook"] + argv, input=raw, text=True,
                              capture_output=True, env=env)
        got = []
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as fh:
                lines = [l for l in fh.read().split("\n")[before:] if l.strip()]
            for line in lines:
                ev = json.loads(line)
                ev.pop("ts", None)
                got.append(ev)
        out = proc.stdout.strip()
        if stdout_expected == "":
            stdout_ok = out == ""
        else:
            try:
                stdout_ok = json.loads(out) == stdout_expected
            except ValueError:
                stdout_ok = False
        ok = proc.returncode == 0 and proc.stderr == "" and stdout_ok and got == expected
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures += 1
            print("   rc=%s out=%r err=%r" % (proc.returncode, proc.stdout, proc.stderr[-200:]))
            print("   expected:", json.dumps(expected, ensure_ascii=False))
            print("   got:     ", json.dumps(got, ensure_ascii=False))
    print("\n%d cases, %d failed (%s)" % (len(cases), failures, RADAR))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

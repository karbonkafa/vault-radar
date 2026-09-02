#!/usr/bin/env python3
"""The agent side of radar.py: who is hooked, who touches the vault unhooked, how to hook them.

    radar.py agents classify        lsof -F pcn on stdin -> JSON list of agent surfaces holding vault files
    radar.py agents offer           for each unhooked surface, one draft kaiChecks item + offers.json entry
    radar.py hook-install <agent>   write the agent's hook config idempotently, record it in hooked.json
    radar.py hook-install --list    print the registry

Everything runs against VAULT_RADAR_HOME and explicit --checks-dir / --config paths, so the
test never touches the real home. Stdlib only.

    python3 tests/test_agents_cli.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RADAR = os.environ.get("RADAR") or os.path.join(os.path.dirname(HERE), "radar.py")

LSOF = (
    "p123\ncCursor Helper (Plugin)\nn/Users/k/karbon-vault/01-projects/projects.md\n"
    "n/Users/k/karbon-vault/00-self/identity.md\n"
    "p456\ncObsidian\nn/Users/k/karbon-vault/.obsidian/workspace.json\n"
    "p789\ncmdworker_shared\nn/Users/k/karbon-vault/05-library/x.md\n"
    "p321\ncCode Helper (Plugin)\nn/Users/k/karbon-vault/02-infra/stack.md\n"
    "p654\ncAntigravity Helper\nn/Users/k/karbon-vault/01-projects/aperta.md\n"
    "p987\ncgit\nn/Users/k/karbon-vault/.git/index\n"
)


def run(args, stdin="", env=None):
    return subprocess.run([sys.executable, RADAR] + args, input=stdin, text=True, capture_output=True, env=env)


def main():
    tmp = tempfile.mkdtemp(prefix="vault-radar-agents-cli-")
    home = os.path.join(tmp, "home")
    checks = os.path.join(tmp, "checks")
    env = dict(os.environ, VAULT_RADAR_HOME=home)
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures += 1
            if detail:
                print("   " + detail[:600])

    # -- classify -----------------------------------------------------------------------
    proc = run(["agents", "classify", "--vault", "/Users/k/karbon-vault"], LSOF, env)
    try:
        found = json.loads(proc.stdout)
    except ValueError:
        found = None
    check("classify: exits 0 with JSON", proc.returncode == 0 and isinstance(found, list),
          "rc=%s out=%r err=%r" % (proc.returncode, proc.stdout[:200], proc.stderr[-200:]))
    by = {f.get("agent"): f for f in (found or [])}
    check("classify: Cursor helper -> cursor with both files",
          by.get("cursor", {}).get("pid") == 123 and len(by.get("cursor", {}).get("files", [])) == 2, json.dumps(found))
    check("classify: Code helper -> vscode, Antigravity helper -> antigravity",
          by.get("vscode", {}).get("pid") == 321 and by.get("antigravity", {}).get("pid") == 654, json.dumps(found))
    check("classify: Obsidian, mdworker, git dropped",
          set(by) == {"cursor", "vscode", "antigravity"}, json.dumps(found))

    # -- workspace evidence (IDEs keep no vault file open; they record the folder) ---------
    app = os.path.join(tmp, "app-support")
    def ws(ide, key, folder):
        d = os.path.join(app, ide, "User", "workspaceStorage", key); os.makedirs(d)
        with open(os.path.join(d, "workspace.json"), "w", encoding="utf-8") as fh:
            json.dump({"folder": folder}, fh)
    ws("Cursor", "a1", "file:///Users/k/karbon-vault")
    ws("Antigravity IDE", "b2", "file:///Users/k/karbon-vault")
    ws("Code", "c3", "file:///Users/k/other-repo")
    ws("Windsurf", "d4", "file:///Users/k/karbon%20vault")
    proc = run(["agents", "classify", "--vault", "/Users/k/karbon-vault", "--app-support", app], "", env)
    try:
        wfound = json.loads(proc.stdout)
    except ValueError:
        wfound = None
    wby = {f.get("agent"): f for f in (wfound or [])}
    check("workspace: exits 0, Cursor and Antigravity found from workspace.json with no lsof hits",
          proc.returncode == 0 and set(wby) == {"cursor", "antigravity"}
          and all("workspace" in f.get("evidence", []) for f in (wfound or [])),
          "rc=%s out=%r err=%r" % (proc.returncode, proc.stdout[:300], proc.stderr[-300:]))
    check("workspace: entry carries the workspace.json path, no pid",
          wby.get("cursor", {}).get("pid") is None and "workspaceStorage" in str(wby.get("cursor", {}).get("workspace", "")), json.dumps(wfound))
    proc = run(["agents", "classify", "--vault", "/Users/k/karbon vault", "--app-support", app], "", env)
    try:
        sfound = {f.get("agent") for f in json.loads(proc.stdout)}
    except ValueError:
        sfound = None
    check("workspace: percent-encoded folder matches a vault path with a space", sfound == {"windsurf"}, "rc=%s out=%r" % (proc.returncode, proc.stdout[:300]))
    proc = run(["agents", "classify", "--vault", "/Users/k/karbon-vault", "--app-support", app], LSOF, env)
    try:
        mfound = json.loads(proc.stdout)
    except ValueError:
        mfound = []
    mby = {f.get("agent"): f for f in mfound}
    check("workspace: lsof and workspace evidence merge into one entry per agent",
          [f["agent"] for f in mfound].count("cursor") == 1 and mby.get("cursor", {}).get("pid") == 123
          and set(mby.get("cursor", {}).get("evidence", [])) == {"lsof", "workspace"} and set(mby) == {"cursor", "vscode", "antigravity"},
          json.dumps(mfound))

    # -- offer --------------------------------------------------------------------------
    os.makedirs(home)
    with open(os.path.join(home, "hooked.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "agents": {"cursor": {"config": "~/.cursor/hooks.json"}}}, fh)
    proc = run(["agents", "offer", "--checks-dir", checks, "--no-notify"], json.dumps(found or []), env)
    prog_path = os.path.join(checks, "radar-hook-coverage.json")
    prog = json.load(open(prog_path, encoding="utf-8")) if os.path.exists(prog_path) else {"items": []}
    ids = [i["id"] for i in prog["items"]]
    check("offer: exits 0, draft items for the unhooked surfaces only",
          proc.returncode == 0 and ids == ["offer-vscode", "offer-antigravity"], "rc=%s ids=%s err=%r" % (proc.returncode, ids, proc.stderr[-200:]))
    item = next((i for i in prog["items"] if i["id"] == "offer-vscode"), {})
    check("offer: item is a valid kaiChecks draft with evidence",
          item.get("state") == "draft" and all(isinstance(item.get(k), str) for k in ("title", "criterion", "at", "by"))
          and "02-infra/stack.md" in item.get("criterion", "") and "vscode" in item.get("title", ""), json.dumps(item, ensure_ascii=False))
    try:
        offers = json.load(open(os.path.join(home, "offers.json"), encoding="utf-8"))
    except (OSError, ValueError):
        offers = {}
    check("offer: offers.json records both", set(offers.get("offered", {})) == {"vscode", "antigravity"}, json.dumps(offers))
    proc2 = run(["agents", "offer", "--checks-dir", checks, "--no-notify"], json.dumps(found or []), env)
    prog2 = json.load(open(prog_path, encoding="utf-8"))
    check("offer: second run adds nothing", proc2.returncode == 0 and [i["id"] for i in prog2["items"]] == ids, str([i["id"] for i in prog2["items"]]))

    # -- hook-install ---------------------------------------------------------------------
    cfg = os.path.join(tmp, "cursor-hooks.json")
    proc = run(["hook-install", "cursor", "--config", cfg], "", env)
    cur = json.load(open(cfg, encoding="utf-8")) if os.path.exists(cfg) else {}
    cmds = [h["command"] for ev in cur.get("hooks", {}).values() for h in ev]
    check("hook-install cursor: writes version 1 + four events calling radar.py hook --agent cursor",
          proc.returncode == 0 and cur.get("version") == 1 and set(cur.get("hooks", {})) == {"beforeReadFile", "afterShellExecution", "beforeSubmitPrompt", "stop"}
          and all("radar.py hook --agent cursor --event" in c for c in cmds), "rc=%s out=%r err=%r cfg=%s" % (proc.returncode, proc.stdout[:200], proc.stderr[-200:], json.dumps(cur)[:300]))
    before = open(cfg, encoding="utf-8").read() if os.path.exists(cfg) else ""
    proc2 = run(["hook-install", "cursor", "--config", cfg], "", env)
    after = open(cfg, encoding="utf-8").read() if os.path.exists(cfg) else ""
    check("hook-install cursor: second run is a no-op and says so",
          proc2.returncode == 0 and before == after and "already" in proc2.stdout.lower(), "out=%r" % proc2.stdout[:200])
    try:
        reg = json.load(open(os.path.join(home, "hooked.json"), encoding="utf-8"))
    except (OSError, ValueError):
        reg = {}
    check("hook-install: registry keeps the earlier entry and records cursor's config path",
          reg.get("agents", {}).get("cursor", {}).get("config") == cfg, json.dumps(reg))
    with open(cfg, "w", encoding="utf-8") as fh:  # someone else's hooks must survive
        json.dump({"version": 1, "hooks": {"stop": [{"command": "echo bye"}]}}, fh)
    proc3 = run(["hook-install", "cursor", "--config", cfg], "", env)
    try:
        cur3 = json.load(open(cfg, encoding="utf-8"))
    except (OSError, ValueError):
        cur3 = {"hooks": {"stop": []}}
    check("hook-install cursor: merges into an existing file, keeps foreign hooks, backs it up",
          proc3.returncode == 0 and any(h["command"] == "echo bye" for h in cur3["hooks"]["stop"])
          and any("radar.py" in h["command"] for h in cur3["hooks"]["stop"]) and any(n.startswith("cursor-hooks.json.bak-") for n in os.listdir(tmp)),
          json.dumps(cur3)[:300] + " files=" + str(os.listdir(tmp)))
    acfg = os.path.join(tmp, "antigravity-hooks.json")
    proc = run(["hook-install", "antigravity", "--config", acfg], "", env)
    ag = json.load(open(acfg, encoding="utf-8")) if os.path.exists(acfg) else {}
    vr = ag.get("vault-radar", {})
    check("hook-install antigravity: vault-radar block with PostToolUse *, PreInvocation, Stop",
          proc.returncode == 0 and vr.get("enabled") is True and vr.get("PostToolUse", [{}])[0].get("matcher") == "*"
          and all(k in vr for k in ("PreInvocation", "Stop")), json.dumps(ag)[:300])
    proc = run(["hook-install", "--list"], "", env)
    check("hook-install --list: prints the registry with cursor and antigravity",
          proc.returncode == 0 and "cursor" in proc.stdout and "antigravity" in proc.stdout, "out=%r" % proc.stdout[:300])
    proc = run(["hook-install", "nosuchagent"], "", env)
    check("hook-install: unknown agent exits 2 with a list of known ones", proc.returncode == 2 and "cursor" in (proc.stderr + proc.stdout), "rc=%s" % proc.returncode)

    print("\n%d checks failed" % failures if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

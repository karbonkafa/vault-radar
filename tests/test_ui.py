#!/usr/bin/env python3
"""UI test harness for demo state and graph interaction behavior.

Run ``radar.py serve`` on a free port against a temporary vault, open
``/?demo=1&still=6000`` in headless Chromium, wait one second for the demo
trace to settle, then evaluate the demo state and graph rendering in a real
browser. The theme check intentionally fails until the page exposes its active
theme and paints the canvas background with that theme.

    ~/.venvs/playwright/bin/python tests/test_ui.py

Stdlib plus the playwright venv at ``~/.venvs/playwright``.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RADAR = os.environ.get("RADAR") or os.path.join(os.path.dirname(HERE), "radar.py")
PYTHON = os.environ.get("PYTHON") or sys.executable
PLAYWRIGHT = os.environ.get("PLAYWRIGHT") or os.path.expanduser("~/.venvs/playwright/bin/python")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def free_port_already_taken(err_path: str) -> str:
    if os.path.exists(err_path):
        with open(err_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()[-400:]
    return ""


def serve_in_background(vault: str, port: int):
    err_path = os.path.join(tempfile.gettempdir(), "vault-radar-test-ui.serve.err")
    err_fh = open(err_path, "w")
    proc = subprocess.Popen(
        [PYTHON, RADAR, "serve", "--vault", vault, "--port", str(port), "--no-open"],
        stdout=subprocess.DEVNULL, stderr=err_fh,
    )
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if proc.poll() is not None:
            err_fh.close()
            raise RuntimeError("serve died before being reachable:\n" + free_port_already_taken(err_path))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        proc.wait(timeout=5)
        err_fh.close()
        raise RuntimeError("serve did not open the port:\n" + free_port_already_taken(err_path))
    return proc, err_fh, base


COUNT_STATES = """() => {
  // Top-level `let N` in the page script is in the global lexical scope, so it
  // is reachable by name from evaluate; shadowing it with a local `const N`
  // put the local in its own temporal dead zone (the worker's crash).
  const nodes = N;
  if (!Array.isArray(nodes)) return null;
  const counts = {};
  for (const n of nodes) {
    const k = (n == null) ? 'null' : (n.st == null ? 'null' : n.st);
    counts[k] = (counts[k] || 0) + 1;
  }
  return counts;
}"""


GRAPH_SNAPSHOT = """(label) => {
  const node = N.find(n => n.label === label);
  if (!node) return null;
  const arcs = [];
  const originalArc = cx.arc;
  cx.arc = function(x, y, radius, start, end, counterclockwise) {
    const transform = this.getTransform();
    const point = transform.transformPoint({x, y});
    arcs.push({x, y, radius, screenX: point.x / DPR, screenY: point.y / DPR});
    return originalArc.call(this, x, y, radius, start, end, counterclockwise);
  };
  try {
    draw();
  } finally {
    cx.arc = originalArc;
  }
  const arc = arcs.find(a => Math.abs(a.x - node.x) < 0.01 && Math.abs(a.y - node.y) < 0.01);
  return arc ? {radius: arc.radius, x: arc.screenX, y: arc.screenY} : null;
}"""


HOVER_LABEL = """(label) => {
  let globalLabel = null;
  if (typeof hoverLabel !== 'undefined') {
    if (typeof hoverLabel === 'string') globalLabel = hoverLabel;
    else if (hoverLabel && typeof hoverLabel.textContent === 'string') globalLabel = hoverLabel.textContent;
    else if (hoverLabel != null) globalLabel = String(hoverLabel);
  }
  const visibleText = [...document.body.querySelectorAll('*')]
    .filter(el => !['SCRIPT', 'STYLE', 'CANVAS'].includes(el.tagName))
    .filter(el => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' &&
             Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
    })
    .map(el => el.textContent || '')
    .find(text => text.includes(label));
  return {globalLabel, visibleText: visibleText || null};
}"""


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vault-radar-ui-test-")
    vault = os.path.join(tmp, "vault")
    os.makedirs(vault)
    port = free_port()
    proc, err_fh, base = serve_in_background(vault, port)
    try:
        # Demo runs from the URL itself; no /api/vault needed. still=6000 freezes
        # the trace after applying every event with t <= 6000 ms, so by 1 s wall
        # the prompt + read (index.md) + scan (six files) + read
        # (notes/agent-swarm-writeup.md) have all fired.
        url = base + "/?demo=1&still=6000"
        playwright_main = """
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1]
with sync_playwright() as p:
    browser = p.chromium.launch()
    try:
        page = browser.new_page()
        def theme_snapshot(name):
            page.goto(URL + "&theme=" + name)
            page.wait_for_timeout(100)
            try:
                theme_bg = page.evaluate("THEME.bg")
            except Exception:
                theme_bg = None
            canvas_bg = page.evaluate("() => {" +
                "const pixel = document.getElementById('cv').getContext('2d').getImageData(1, 1, 1, 1).data;" +
                "return '#' + [...pixel.slice(0, 3)].map(value => value.toString(16).padStart(2, '0')).join('');" +
                "}")
            return {"theme_bg": theme_bg, "canvas_bg": canvas_bg}

        themes = {
            "obsidian": theme_snapshot("obsidian"),
            "radar": theme_snapshot("radar"),
        }
        page.goto(URL)
        page.wait_for_timeout(1000)
        counts = page.evaluate(%r)
        page.evaluate("clearTurn(); agent.on = false; draw()")
        index_snapshot = page.evaluate(%r, "index.md")
        log_snapshot = page.evaluate(%r, "log.md")
        canvas = page.locator("#cv").bounding_box()
        page.mouse.move(canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] / 2)
        page.mouse.wheel(0, -500)
        page.wait_for_timeout(100)
        zoomed_index_snapshot = page.evaluate(%r, "index.md")
        page.mouse.move(canvas["x"] + zoomed_index_snapshot["x"],
                        canvas["y"] + zoomed_index_snapshot["y"])
        page.wait_for_timeout(100)
        hover = page.evaluate(%r, "index.md")
        import json
        print("RESULT=" + json.dumps({
            "themes": themes,
            "counts": counts,
            "index": index_snapshot,
            "log": log_snapshot,
            "zoomed_index": zoomed_index_snapshot,
            "hover": hover,
        }))
    finally:
        browser.close()
""" % (COUNT_STATES, GRAPH_SNAPSHOT, GRAPH_SNAPSHOT, GRAPH_SNAPSHOT, HOVER_LABEL)
        cp = subprocess.run(
            [PLAYWRIGHT, "-c", playwright_main, url],
            capture_output=True, text=True,
        )
        out = (cp.stdout or "") + "\n" + (cp.stderr or "")
        if cp.returncode != 0:
            print("FAIL playwright did not exit 0")
            print(out.strip())
            return 1
        line = next((l for l in cp.stdout.splitlines() if l.startswith("RESULT=")), None)
        if not line:
            print("FAIL playwright did not report results")
            print(out.strip())
            return 1
        import json as _json
        result = _json.loads(line[len("RESULT="):])
        counts = result["counts"]
        # Scaffold expectation, per the kanban task body:
        #   read : 2  (index.md, notes/agent-swarm-writeup.md)
        #   scan : 5  (six scan hits -> one is also read, so five stay "scan")
        # Pageeval returns read/scan and any other states as their own keys.
        read_count = counts.get("read") or 0
        scan_count = counts.get("scan") or 0
        demo_ok = read_count == 2 and scan_count == 5
        print(("PASS " if demo_ok else "FAIL ") +
              "demo: 2 reads (index, agent-swarm-writeup), 5 scans")
        if not demo_ok:
            print("   got:", counts)

        failures = []
        themes = result["themes"]
        theme_ok = (
            themes["obsidian"]["theme_bg"] == "#222222" and
            themes["radar"]["theme_bg"] == "#08090a" and
            themes["obsidian"]["theme_bg"] != themes["radar"]["theme_bg"] and
            themes["obsidian"]["canvas_bg"] == themes["obsidian"]["theme_bg"] and
            themes["radar"]["canvas_bg"] == themes["radar"]["theme_bg"]
        )
        print(("PASS " if theme_ok else "FAIL ") +
              "theme switch: obsidian and radar expose and paint distinct backgrounds")
        if not theme_ok:
            failures.append("theme switch")
            print("   got:", themes)

        index_snapshot = result["index"]
        log_snapshot = result["log"]
        radius_ok = (index_snapshot is not None and log_snapshot is not None and
                     index_snapshot["radius"] > log_snapshot["radius"])
        print(("PASS " if radius_ok else "FAIL ") +
              "degree radius: index.md (7 edges) is larger than log.md (1 edge)")
        if not radius_ok:
            failures.append("degree radius")
            print("   got:", {"index.md": index_snapshot, "log.md": log_snapshot})

        zoomed = result["zoomed_index"]
        zoom_ok = (index_snapshot is not None and zoomed is not None and
                   (abs(zoomed["x"] - index_snapshot["x"]) > 0.5 or
                    abs(zoomed["y"] - index_snapshot["y"]) > 0.5))
        print(("PASS " if zoom_ok else "FAIL ") +
              "wheel zoom: index.md screen coordinates change")
        if not zoom_ok:
            failures.append("wheel zoom")
            print("   got:", {"before": index_snapshot, "after": zoomed})

        hover = result["hover"]
        hover_text = " ".join(str(value or "") for value in hover.values())
        hover_ok = "index.md" in hover_text
        print(("PASS " if hover_ok else "FAIL ") +
              "hover label: index.md is visible under the pointer")
        if not hover_ok:
            failures.append("hover label")
            print("   got:", hover)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err_fh.close()
    if not demo_ok:
        failures.append("demo state")
    print("FAILED %d" % len(failures) if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

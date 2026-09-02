# Contributing

Small project, short rules.

## Run it

Python 3.9+, no dependencies — and it stays that way. Please don't add a
third-party package to `radar.py`.

```bash
python3 radar.py install                  # print the hook config
python3 radar.py serve --vault ~/notes    # then open http://localhost:7777
```

## Test it

`python3 tests/test_hook.py` runs the hook against recorded Claude Code, Kai, Kimi and
Codex payloads (stdlib only, temp vault). The rest is by hand before opening a PR:

- `echo 'not json' | python3 radar.py hook` — must exit 0, print nothing. The hook
  may never block or throw; a bad payload is swallowed silently.
- UI: open the viewer with `?demo=1` and watch the recorded trace replay.
- Hook: run a real Claude Code turn, check `~/.vault-radar/events.jsonl`.
- Plugin: `./install-obsidian-plugin.sh <vault>`, reload Obsidian, open the graph.

## Wanted

Bug fixes, better `Grep` hit extraction, Windows/Linux fixes, viewer polish,
compatibility fixes for new Obsidian releases.

**The plugin is fragile.** `obsidian-plugin/main.js` reaches into Obsidian
internals (`renderer.nodeLookup`, `node.color`, `renderer.changed()`) — no public
API, any release can move it. Keep every call wrapped so failure degrades to no
colouring, never a broken graph, and name the version you tested on in the PR.

Never commit personal paths, vault contents, `.obsidian/`, or event logs.

# vault-radar

**Watch, in real time, which files your coding agent actually reads.**

You ask Claude Code a question. It answers. But which of your 74 notes did it
open — and which 69 did it never touch? `vault-radar` puts that on screen while
it happens.

![vault-radar](assets/cover.gif)

```
❯ multi-agent videomda ne anlatmıştım?

  ▪ index.md                                   1.102   ← read
  ▫ themes/multi-agent.md                      1.188   ← read
  ▫ videos/multi-agent-claude-4-bolduk.md      1.419   ← read
  ▫ insights/token-verimliligi-talebi.md         728   ← read
  ░ log.md                                     1.222   ← grep saw it, never opened
  · 69 more files                                      ← never touched

  6.366 read   /   79.650 total   =   12,5× less
```

## Why

Agent memory systems are sold on a promise: the agent doesn't read everything,
it navigates. That promise is usually invisible — you see the answer, never the
path. `vault-radar` makes the path visible, so you can tell whether your index
is doing its job or your agent is brute-forcing the folder.

## Install

Requires Python 3.9+. No dependencies.

```bash
git clone https://github.com/selmakcby/vault-radar
cd vault-radar
python3 radar.py install        # prints the hook config
```

Merge the printed block into `~/.claude/settings.json`, restart Claude Code, then:

```bash
python3 radar.py serve --vault ~/notes
# open http://localhost:7777
```

Park the browser window on the right half of your screen and work as usual.

## How it works

Three moving parts, no magic:

1. **A hook.** Claude Code fires `PostToolUse` after every tool call. `radar.py hook`
   reads the payload on stdin and appends one JSON line to `~/.vault-radar/events.jsonl`.
   It catches `Read`, `Grep`, `Glob` and `Bash` (agents that `cat` or `grep` from the shell are tracked too), plus `UserPromptSubmit` and `Stop`.
   A shell `cat`/`head`/`sed` counts every file it names; a shell `grep`/`rg` takes its hits from the command's own output.
2. **A server.** `radar.py serve` indexes your vault once (path, size, token estimate,
   and the `[[wikilinks]]` and `[text](path.md)` links between notes, which are the graph's edges)
   and tails the event log, pushing each new line over Server-Sent Events.
3. **A viewer.** A single HTML file. Every file in your vault is one row; rows light
   up as the agent touches them, and a small sprite walks from the prompt down to
   whatever is being read.

The hook never blocks and never throws — a malformed payload is swallowed silently.
Radar observes; it is not allowed to break your turn.

## States

| | meaning |
|---|---|
| dim | never touched this turn |
| yellow | a `grep`/`glob` matched it — the agent saw the **name**, not the content |
| orange | actually opened and read into context |
| blue | read by a **subagent** only — it never entered the main context; a main-thread read of the same file turns it orange |

The yellow state is the interesting one. A search that returns forty filenames
costs almost nothing; opening forty files costs everything. Radar shows the
difference.

The blue state is the other half of that argument. A subagent's tool calls fire the
same hook, and Claude Code stamps those payloads with `agent_id` and `agent_type`;
the hook keeps both, and both viewers count those reads apart. `READ TK` and the
ratio are main-thread only — a file an Explore agent read cost that agent's context,
not yours, which is the point of delegating.

## Inside Obsidian's own graph

The standalone viewer draws its own graph. If you already live in Obsidian, you
can skip it: the bundled plugin tints **Obsidian's real graph view** as the agent
works.

```bash
./install-obsidian-plugin.sh ~/path/to/vault
```

Then in Obsidian: **Settings → Community plugins → turn off Restricted mode**,
reload the vault, open the graph, and give your agent a prompt.

The plugin reads the same `events.jsonl` directly — Obsidian runs in Electron,
so it has Node's `fs`. **You do not need `radar.py serve` running** for this;
the hook alone is enough.

| colour | meaning |
|---|---|
| pale peach, enlarged | being read right now |
| orange | read this turn |
| amber | a search matched the name, never opened |
| blue | read by a subagent only |
| default | untouched |

### The robot

A small sprite is parented to the graph's own Pixi container, so it pans and
zooms with everything else. When the agent opens a file, the robot **walks the
edges** to get there: the plugin runs a breadth-first search over
`renderer.links` and animates node to node along the real route. If two files
are not connected it hops straight across, which is itself informative — it
means that page is not reachable from where the agent just was.

Toggle it, resize it, or change its speed in the plugin settings.

A status-bar item counts files and estimated tokens for the current turn, with
delegated reads counted separately.
Commands: **Clear radar highlighting**, **Reconnect to event log**.

**Turn off Restricted mode.** If the toggle is awkward to find, the flag lives at
`.obsidian/app.json` as `{"safeMode": false}` — Obsidian must be closed when you edit it.
No paid plan is involved: Obsidian is free for personal use and community plugins
are part of that. Sync, Publish and Catalyst are the paid products, and none of
them are needed here.

> ⚠️ **This part leans on Obsidian internals.** Verified working on **Obsidian 1.13.7**:
> `renderer.nodeLookup[path]` resolves a node, `node.color = {a, rgb}` is what
> `node.getFillColor()` reads, and `renderer.changed()` schedules the redraw.
> None of that is public API. Every call is wrapped — if a release moves it, the
> plugin stops colouring and writes `~/.vault-radar/obsidian-debug.json` describing
> what it found instead. It will not break your vault or your graph.

## Options

```bash
radar.py serve --vault ~/notes --port 7777 --ext .md,.txt
```

| flag / env | default | meaning |
|---|---|---|
| `--vault` | required | directory to watch |
| `--port` | `7777` | viewer port |
| `--ext` | `.md` | comma-separated extensions to track |
| `--no-open` | off | do not open the docked window |
| `--width` | `520` | docked window width in px |
| `VAULT_RADAR_HOME` | `~/.vault-radar` | where the event log lives |
| `VAULT_RADAR_CPT` | `3.8` | characters per token (≈4.0 English, ≈3.6 Turkish) |

Token counts are **estimates**: from the bytes the tool actually returned when the
payload carries them (a partial `Read`, a `head -40`), otherwise from file size. Not
real tokenizer output; there for proportion, not for billing.

### Several sessions at once

Every session on the machine appends to the same log. By default the viewer and the
Obsidian plugin follow whichever session prompted last and clear on each new prompt,
which on a machine running several sessions means the display never holds still.
Pin it:

```bash
radar.py follow --last          # the session that prompted last
radar.py follow a1b2c3d4        # a session id, or a unique prefix of one
radar.py follow --off           # back to following the last prompt
```

The pin is the file `~/.vault-radar/follow`; the plugin reads the same file, so one
command pins both. While pinned, other sessions' prompts neither switch nor clear the
display, and the status bar says `pinned`.

## Demo mode

Open the viewer with `?demo=1`, or click **DEMO**, to replay a recorded trace
against a fake vault. Useful for screenshots and for checking the UI without
wiring hooks up.

## Limitations

- Reads are attributed by vault root (the resolved path and the one given to
  `--vault`); an absolute path under neither is reported as outside. Only relative
  paths, which the hook emits when a payload carries no `cwd`, fall back to suffix
  matching, and never on the bare filename alone.
- `Grep` hit extraction is best-effort: it parses the tool response, whose exact
  shape is not part of any public contract and may change.
- Several Claude Code sessions write to the same log. By default the display follows
  whichever session prompted last, so a busy neighbour takes it over on every prompt;
  `radar.py follow` pins it to one session (see Options).
- Local only, binds to `127.0.0.1`.

## Licence

MIT.

---

Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are
trademarks of Anthropic; this project only reads the hook payloads their CLI
already emits.

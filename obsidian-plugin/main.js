'use strict';

/**
 * Vault Radar — Obsidian plugin
 *
 * Tails the event log written by the vault-radar hook and tints nodes in
 * Obsidian's own graph view as the agent reads them.
 *
 * The graph renderer is NOT part of Obsidian's public API. Everything that
 * touches it is guarded and degrades to "do nothing" rather than throwing.
 */

const { Plugin, PluginSettingTab, Setting, Notice } = require('obsidian');
const fs = require('fs');
const path = require('path');
const os = require('os');

const COLOR = {
  read: 0xff7a45,   // opened and read into context
  scan: 0xe3b341,   // a grep/glob matched the name only
  delegated: 0x6cb6ff, // read by a subagent only — never entered the main context
  active: 0xffe0cc, // currently being read
};

const DEFAULTS = {
  eventsPath: path.join(os.homedir(), '.vault-radar', 'events.jsonl'),
  pollMs: 400,
  showScan: true,
  fadeOnPrompt: true,
  robot: true,
  robotSize: 38,
  robotSpeed: 0.045,
};

module.exports = class VaultRadarPlugin extends Plugin {
  async onload() {
    this.settings = Object.assign({}, DEFAULTS, await this.loadData());
    this.marks = new Map();      // relative path -> 'read' | 'scan'
    this.painted = new Set();    // ids currently tinted, so they can be cleared
    // Verification writes are opt-in: drop a `verify` file next to the log.
    try { this.verifyFlag = fs.existsSync(path.join(path.dirname(this.settings.eventsPath), 'verify')); }
    catch (_) { this.verifyFlag = false; }
    this.activePath = null;
    this.session = null;
    this.pin = null;             // session the radar is pinned to (the `follow` file), or null
    this.readTokens = 0;
    this.readTk = new Map();     // relative path -> tokens counted for it this turn
    this.agentTk = new Map();    // the same, for reads made by subagents
    this.agentTokens = 0;
    this.offset = 0;

    this.status = this.addStatusBarItem();
    this.status.setText('radar: —');

    this.addSettingTab(new RadarSettingTab(this.app, this));

    this.addCommand({
      id: 'vault-radar-clear',
      name: 'Clear radar highlighting',
      callback: () => this.clearMarks(true),
    });
    this.addCommand({
      id: 'vault-radar-reconnect',
      name: 'Reconnect to event log',
      callback: () => { this.offset = 0; this.seekToEnd(); new Notice('Vault Radar: reconnected'); },
    });

    this.addCommand({
      id: 'vault-radar-diagnose',
      name: 'Write graph diagnostics to disk',
      callback: () => { this.dumpDiagnostics('manual'); this.verifyPaint();
        new Notice('Vault Radar: diagnostics written'); },
    });

    this.app.workspace.onLayoutReady(() => {
      this.seekToEnd();
      window.setTimeout(() => this.maybeDiagnose(), 2000);
      // Re-dump once a graph view actually appears, so a failed paint is explainable.
      this.registerEvent(this.app.workspace.on('layout-change', () => {
        if (!this.dumpedWithGraph && this.renderers().length) {
          this.dumpedWithGraph = true;
          window.setTimeout(() => this.dumpDiagnostics('graph-open'), 1200);
        }
      }));
      this.registerInterval(window.setInterval(() => this.poll(), this.settings.pollMs));
    });
  }

  onunload() {
    this.clearMarks(true);
    this.removeRobot();
  }

  // ── event log ──────────────────────────────────────────────

  seekToEnd() {
    try {
      this.offset = fs.statSync(this.settings.eventsPath).size;
    } catch (_) {
      this.offset = 0;
    }
  }

  poll() {
    let size;
    try {
      size = fs.statSync(this.settings.eventsPath).size;
    } catch (_) {
      this.status.setText('radar: no log');
      return;
    }
    if (size < this.offset) this.offset = 0;   // log was reset
    if (size === this.offset) return;

    let chunk = '';
    let fd = null;
    try {
      fd = fs.openSync(this.settings.eventsPath, 'r');
      const buf = Buffer.alloc(size - this.offset);
      const got = fs.readSync(fd, buf, 0, buf.length, this.offset);
      chunk = buf.toString('utf8', 0, got);
    } catch (_) {
      return;
    } finally {
      // A throwing readSync (the log path pointing at a directory, say) used to
      // skip the close and leak one descriptor per poll.
      if (fd !== null) { try { fs.closeSync(fd); } catch (_) { /* ignore */ } }
    }

    // Advance only past complete lines. Counting the separators by hand drifts
    // the offset by a byte and every later read starts mid-record.
    const lastNewline = chunk.lastIndexOf('\n');
    if (lastNewline === -1) return;                  // nothing complete yet
    const complete = chunk.slice(0, lastNewline + 1);
    this.offset += Buffer.byteLength(complete, 'utf8');

    this.pin = this.readPin();
    for (const line of complete.split('\n')) {
      if (!line.trim()) continue;
      try { this.handle(JSON.parse(line)); } catch (_) { /* skip malformed */ }
    }
    this.repaint();
    if (this.verifyFlag && this.marks.size) this.verifyPaint();
  }

  handle(ev) {
    // Several Claude Code sessions share one log; follow whichever prompted last,
    // unless a pin (the `follow` file next to the log) names the session to keep.
    if (this.pin && ev.session && !String(ev.session).startsWith(this.pin)) return;
    if (ev.kind === 'prompt') this.session = ev.session || null;
    else if (this.session && ev.session && ev.session !== this.session) return;

    if (ev.kind === 'prompt') {
      this.botAt = null;
      if (this.settings.fadeOnPrompt) this.clearMarks(false);
      this.readTokens = 0;
      this.readTk.clear();
      this.agentTokens = 0;
      this.agentTk.clear();
      this.status.setText('radar: ' + (this.pin ? 'pinned · ' : '') + String(ev.text || '').slice(0, 34));
      return;
    }
    if (ev.kind === 'scan' && this.settings.showScan) {
      for (const hit of ev.hits || []) {
        const rel = this.toRelative(hit);
        const cur = rel && this.marks.get(rel);
        if (rel && cur !== 'read' && cur !== 'delegated') this.marks.set(rel, 'scan');
      }
      return;
    }
    if (ev.kind === 'read') {
      const rel = this.toRelative(ev.path);
      if (!rel) return;
      // A read by a subagent (the event carries agent_id) never entered the main
      // context: its own colour, its own count, and a later main-thread read of the
      // same file turns it orange.
      const delegated = !!ev.agent_id;
      if (!delegated || this.marks.get(rel) !== 'read') this.marks.set(rel, delegated ? 'delegated' : 'read');
      this.activePath = rel;
      this.walkTo(rel);
      // Count what the tool returned when the hook saw it — a partial Read is partial
      // context. Partial reads add up, capped at the whole file.
      const full = this.estimateTokens(rel);
      const got = (ev.chars != null && isFinite(ev.chars)) ? Math.round(ev.chars / 3.8) : full;
      const book = delegated ? this.agentTk : this.readTk;
      book.set(rel, Math.min(full, (book.get(rel) || 0) + got));
      this.readTokens = 0;
      for (const v of this.readTk.values()) this.readTokens += v;
      this.agentTokens = 0;
      for (const v of this.agentTk.values()) this.agentTokens += v;
      const n = [...this.marks.values()].filter((v) => v === 'read').length;
      const d = [...this.marks.values()].filter((v) => v === 'delegated').length;
      this.status.setText(`radar: ${n} files · ~${this.readTokens.toLocaleString()} tk`
        + (d ? ` · ${d} delegated ~${this.agentTokens.toLocaleString()} tk` : ''));
      return;
    }
    if (ev.kind === 'stop') {
      this.activePath = null;
    }
  }

  /** The session the radar is pinned to — the `follow` file next to the log — or null. */
  readPin() {
    try {
      const pin = fs.readFileSync(path.join(path.dirname(this.settings.eventsPath), 'follow'), 'utf8').trim();
      return pin || null;
    } catch (_) { return null; }
  }

  estimateTokens(rel) {
    const f = this.app.vault.getAbstractFileByPath(rel);
    return f && f.stat ? Math.round(f.stat.size / 3.8) : 0;
  }

  /** Map a path from the log onto a vault-relative path, or null. */
  toRelative(p) {
    if (!p) return null;
    p = String(p);
    for (const base of this.vaultRoots()) {
      if (p.startsWith(base + '/') || p.startsWith(base + '\\')) {
        const rel = p.slice(base.length + 1).split('\\').join('/');
        return this.app.vault.getAbstractFileByPath(rel) ? rel : null;
      }
    }
    // An absolute path outside the vault is outside — no suffix fallback. Matching
    // the bare filename lit this vault's README.md for every other repo's README.md.
    if (path.isAbsolute(p)) return null;
    // Relative (the hook had no cwd): longest suffix of at least two components.
    const parts = p.split(/[/\\]/);
    for (let i = 0; i < parts.length - 1; i++) {
      const cand = parts.slice(i).join('/');
      if (this.app.vault.getAbstractFileByPath(cand)) return cand;
    }
    return parts.length === 1 && this.app.vault.getAbstractFileByPath(p) ? p : null;
  }

  /** The vault's base path as Obsidian reports it, plus its symlink-resolved form. */
  vaultRoots() {
    if (this._roots) return this._roots;
    const roots = [];
    try {
      const base = this.app.vault.adapter.getBasePath();
      if (base) {
        roots.push(base.replace(/[/\\]+$/, ''));
        try { roots.push(fs.realpathSync(base).replace(/[/\\]+$/, '')); } catch (_) { /* ignore */ }
      }
    } catch (_) { /* not a desktop adapter */ }
    this._roots = [...new Set(roots)];
    return this._roots;
  }


  /** Developer affordance: if a flag file exists, open the graph view and dump.
   *  Normal users never create the flag, so nothing opens on its own. */
  async maybeDiagnose() {
    const flag = path.join(path.dirname(this.settings.eventsPath), 'diagnose');
    let wanted = false;
    try { wanted = fs.existsSync(flag); } catch (_) { /* ignore */ }
    if (!wanted) { this.dumpDiagnostics('onload'); return; }
    try {
      if (!this.renderers().length) {
        const leaf = this.app.workspace.getLeaf(true);
        await leaf.setViewState({ type: 'graph', active: true });
        await new Promise((r) => window.setTimeout(r, 2500));
      }
      this.dumpDiagnostics('diagnose-flag');
      window.setTimeout(() => this.probeDraw(), 1500);
      try { fs.unlinkSync(flag); } catch (_) { /* ignore */ }
    } catch (e) {
      this.dumpDiagnostics('diagnose-error:' + String(e));
    }
  }

  /** Write what the graph internals actually look like, and prove whether a
   *  paint round-trip works — so failures are diagnosable without a console. */
  dumpDiagnostics(tag) {
    const proto = (o) => {
      try { return Object.getOwnPropertyNames(Object.getPrototypeOf(o))
        .filter((k) => { try { return typeof o[k] === 'function'; } catch (_) { return false; } })
        .slice(0, 40); } catch (_) { return []; }
    };
    try {
      const out = { tag, at: new Date().toISOString(),
                    obsidian: (require('obsidian').apiVersion || '?'), leaves: [] };
      for (const type of ['graph', 'localgraph']) {
        for (const leaf of this.app.workspace.getLeavesOfType(type)) {
          const v = leaf && leaf.view; if (!v) continue;
          const r = v.renderer, eng = v.dataEngine || v.engine;
          const e = { type };
          if (r) {
            e.nodeCount = Array.isArray(r.nodes) ? r.nodes.length : null;
            e.hasNodeLookup = !!r.nodeLookup;
            e.lookupSample = r.nodeLookup ? Object.keys(r.nodeLookup).slice(0, 6) : null;
            e.rendererProto = proto(r);
            const n = r.nodes && r.nodes[0];
            if (n) {
              e.nodeProto = proto(n);
              e.circleHasTint = n.circle ? ('tint' in n.circle) : false;
              e.circleProto = n.circle ? proto(n.circle).slice(0, 20) : null;
              // Round-trip test: colour one node, read it back, then put the
              // node's own colour straight back. Without the restore, every
              // load left one node of the user's graph tinted for good — it is
              // not in `painted`, so nothing ever cleared it again.
              const prevColor = n.color;
              const prevTint = n.circle && 'tint' in n.circle ? n.circle.tint : null;
              try {
                n.color = { a: 1, rgb: 0xff7a45 };
                if (n.circle && 'tint' in n.circle) n.circle.tint = 0xff7a45;
                e.writeOk = JSON.stringify(n.color);
                for (const fn of ['changed', 'onIframeResize', 'update', 'render', '_dirty']) {
                  e['can_' + fn] = typeof r[fn] === 'function';
                }
              } catch (err) { e.writeErr = String(err); }
              try {
                n.color = prevColor;
                if (prevTint !== null && n.circle) n.circle.tint = prevTint;
                if (typeof r.changed === 'function') r.changed();
              } catch (_) { /* ignore */ }
            }
          }
          if (eng) {
            e.engineProto = proto(eng);
            e.optionKeys = eng.options ? Object.keys(eng.options).slice(0, 25) : null;
            e.optColorGroups = eng.options ? JSON.stringify(eng.options.colorGroups) : null;
          }
          out.leaves.push(e);
        }
      }
      fs.writeFileSync(path.join(path.dirname(this.settings.eventsPath), 'obsidian-debug.json'),
        JSON.stringify(out, null, 2), 'utf8');
    } catch (err) {
      try { fs.writeFileSync(path.join(path.dirname(this.settings.eventsPath), 'obsidian-debug.json'),
        JSON.stringify({ tag, error: String(err && err.stack || err) }, null, 2), 'utf8'); } catch (_) {}
    }
  }


  // ── the robot ──────────────────────────────────────────────
  //
  //  A sprite of our own, parented to `renderer.hanger` so it pans and zooms
  //  with the graph. Pixi is not requireable from a plugin, so the Graphics
  //  class is borrowed from an object already in the scene (`link.line`).

  /** Borrow Pixi's Sprite and Texture classes from an object already in the scene.
   *  `pixi.js` is not requireable from a plugin, and `link.line` is a Sprite —
   *  not a Graphics — so the robot is drawn on a canvas and uploaded as a texture. */
  spriteClasses(r) {
    try {
      const l = (r.links || []).find((x) => x && x.line && x.line.texture);
      if (!l) return null;
      const Sprite = l.line.constructor;
      const Texture = l.line.texture.constructor;
      if (typeof Texture.from !== 'function') return null;
      return { Sprite, Texture };
    } catch (_) { return null; }
  }

  /** Paint the robot into an offscreen canvas. Plain 2D — no Pixi drawing API needed. */
  robotCanvas(size) {
    const dpr = 4;                       // crisp when the graph is zoomed in
    const cv = document.createElement('canvas');
    cv.width = cv.height = size * dpr;
    const x = cv.getContext('2d');
    x.scale(dpr, dpr);
    const S = size, r = S * 0.26;

    x.shadowColor = 'rgba(255,122,69,.9)';
    x.shadowBlur = S * 0.35;

    // antenna
    x.strokeStyle = '#ffe0cc';
    x.lineWidth = S * 0.07;
    x.beginPath(); x.moveTo(S / 2, S * 0.20); x.lineTo(S / 2, S * 0.08); x.stroke();
    x.fillStyle = '#ffe0cc';
    x.beginPath(); x.arc(S / 2, S * 0.07, S * 0.075, 0, 7); x.fill();

    // body
    const g = x.createLinearGradient(0, S * 0.2, 0, S * 0.9);
    g.addColorStop(0, '#ff9a63');
    g.addColorStop(1, '#d9541c');
    x.fillStyle = g;
    x.beginPath();
    if (x.roundRect) x.roundRect(S * 0.16, S * 0.20, S * 0.68, S * 0.66, r);
    else x.rect(S * 0.16, S * 0.20, S * 0.68, S * 0.66);
    x.fill();
    x.shadowBlur = 0;

    // visor
    x.fillStyle = '#120a06';
    x.beginPath();
    if (x.roundRect) x.roundRect(S * 0.26, S * 0.33, S * 0.48, S * 0.26, S * 0.10);
    else x.rect(S * 0.26, S * 0.33, S * 0.48, S * 0.26);
    x.fill();

    // eyes
    x.fillStyle = '#8ef7c8';
    x.beginPath(); x.arc(S * 0.39, S * 0.46, S * 0.055, 0, 7); x.fill();
    x.beginPath(); x.arc(S * 0.61, S * 0.46, S * 0.055, 0, 7); x.fill();

    // feet
    x.fillStyle = '#a84416';
    x.fillRect(S * 0.26, S * 0.86, S * 0.14, S * 0.07);
    x.fillRect(S * 0.60, S * 0.86, S * 0.14, S * 0.07);

    return cv;
  }

  ensureRobot(r) {
    if (this.bot && this.bot.__renderer === r && !this.bot._destroyed && this.bot.parent) return this.bot;
    if (this.bot) this.removeRobot();   // otherwise the old sprite is orphaned in the old scene
    const cls = this.spriteClasses(r);
    if (!cls || !r.hanger || typeof r.hanger.addChild !== 'function') return null;
    try {
      const tex = cls.Texture.from(this.robotCanvas(this.settings.robotSize));
      const bot = new cls.Sprite(tex);
      bot.width = this.settings.robotSize;
      bot.height = this.settings.robotSize;
      if (bot.anchor && typeof bot.anchor.set === 'function') bot.anchor.set(0.5);
      bot.zIndex = 9999;
      r.hanger.addChild(bot);
      bot.__renderer = r;
      this.bot = bot;
      return bot;
    } catch (_) { return null; }
  }

  removeRobot() {
    try {
      if (this.bot && this.bot.parent) this.bot.parent.removeChild(this.bot);
      if (this.bot && typeof this.bot.destroy === 'function') this.bot.destroy();
    } catch (_) { /* ignore */ }
    this.bot = null;
  }

  /** Shortest edge path between two node ids, or null if they are not connected. */
  edgePath(r, fromId, toId) {
    if (!fromId || fromId === toId) return toId ? [toId] : null;
    const adj = new Map();
    for (const l of r.links || []) {
      const a = l.source && l.source.id, b = l.target && l.target.id;
      if (!a || !b) continue;
      if (!adj.has(a)) adj.set(a, []);
      if (!adj.has(b)) adj.set(b, []);
      adj.get(a).push(b);
      adj.get(b).push(a);
    }
    const prev = new Map([[fromId, null]]);
    let frontier = [fromId];
    while (frontier.length) {
      const next = [];
      for (const u of frontier) {
        for (const v of adj.get(u) || []) {
          if (prev.has(v)) continue;
          prev.set(v, u);
          if (v === toId) {
            const path = [];
            for (let cur = toId; cur; cur = prev.get(cur)) path.unshift(cur);
            return path.slice(1);       // drop the node we are standing on
          }
          next.push(v);
        }
      }
      frontier = next;
    }
    return null;                        // no route: caller draws a straight hop
  }

  /** Send the robot to a node, walking edges when a route exists. */
  walkTo(targetId) {
    if (!this.settings.robot) return;
    const r = this.renderers()[0];
    if (!r || !r.nodeLookup || !r.nodeLookup[targetId]) return;
    const bot = this.ensureRobot(r);
    if (!bot) return;

    const route = this.edgePath(r, this.botAt, targetId) || [targetId];
    const points = route.map((id) => r.nodeLookup[id]).filter(Boolean);
    if (!points.length) return;

    if (this.botAt == null) {           // first appearance: start on the target
      const n = points[points.length - 1];
      bot.x = n.x; bot.y = n.y;
    }
    this.legs = points;
    this.leg = 0;
    this.legT = 0;
    this.legStart = null;
    this.botAt = targetId;
    this.startWalk(r);
  }

  startWalk(r) {
    if (this.walking) return;
    this.walking = true;
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

    const step = () => {
      if (!this.bot || !this.legs || this.leg >= this.legs.length) {
        this.walking = false;
        this.legStart = null;
        return;
      }
      const to = this.legs[this.leg];
      if (!this.legStart) this.legStart = { x: this.bot.x, y: this.bot.y };

      this.legT = Math.min(1, this.legT + this.settings.robotSpeed);
      const t = ease(this.legT);
      this.bot.x = this.legStart.x + (to.x - this.legStart.x) * t;
      this.bot.y = this.legStart.y + (to.y - this.legStart.y) * t;

      if (this.legT >= 1) {
        this.bot.x = to.x;
        this.bot.y = to.y;
        this.leg += 1;
        this.legT = 0;
        this.legStart = null;
      }
      try { if (typeof r.changed === 'function') r.changed(); } catch (_) { /* ignore */ }
      window.requestAnimationFrame(step);
    };
    window.requestAnimationFrame(step);
  }

  /** Where the robot is right now — used to verify a real run. */
  robotState() {
    return this.bot
      ? { at: this.botAt, x: Math.round(this.bot.x), y: Math.round(this.bot.y),
          parented: !!this.bot.parent, legs: (this.legs || []).length, leg: this.leg }
      : { at: null, missing: true };
  }

  // ── graph view ─────────────────────────────────────────────

  renderers() {
    const out = [];
    for (const type of ['graph', 'localgraph']) {
      for (const leaf of this.app.workspace.getLeavesOfType(type)) {
        const r = leaf && leaf.view && leaf.view.renderer;
        if (r && Array.isArray(r.nodes)) out.push(r);
      }
    }
    return out;
  }



  /** Report whether the robot really made it into the scene with a real size. */
  probeDraw() {
    const out = { at: new Date().toISOString() };
    try {
      const r = this.renderers()[0];
      if (!r) { out.error = 'no graph open'; }
      else {
        const cls = this.spriteClasses(r);
        out.gotClasses = !!cls;
        if (cls) { out.sprite = cls.Sprite.name; out.texture = cls.Texture.name; }
        const bot = this.ensureRobot(r);
        out.created = !!bot;
        if (bot) {
          const n = r.nodes && r.nodes[0];
          if (n) { bot.x = n.x; bot.y = n.y; }
          out.bot = { parented: !!bot.parent, x: Math.round(bot.x), y: Math.round(bot.y),
                      w: Math.round(bot.width), h: Math.round(bot.height),
                      visible: bot.visible, alpha: bot.alpha,
                      childIndex: bot.parent ? bot.parent.children.indexOf(bot) : -1,
                      siblings: bot.parent ? bot.parent.children.length : -1 };
          try { if (typeof r.changed === 'function') r.changed(); } catch (_) {}
        }
      }
    } catch (e) { out.fatal = String(e && e.stack || e).slice(0, 400); }
    try { fs.writeFileSync(path.join(path.dirname(this.settings.eventsPath), 'obsidian-draw.json'),
      JSON.stringify(out, null, 2), 'utf8'); } catch (_) {}
  }

  /** Colour the graph.
   *
   *  Verified against Obsidian 1.13.7: `renderer.nodeLookup` maps a vault path to
   *  a node, `node.color = {a, rgb}` is what `node.getFillColor()` reads, and
   *  `renderer.changed()` schedules the redraw. Everything is guarded — if a
   *  future release moves these, the plugin stops colouring and nothing else.
   */
  repaint() {
    let painted = false;
    // Worked out once, up front: clearing the set inside the loop left every
    // renderer after the first with its old tints still on, because the second
    // pass had nothing left to clear (graph and local graph open together).
    const stale = [];
    for (const id of this.painted) if (!this.marks.has(id)) stale.push(id);
    const nowPainted = new Set();
    for (const r of this.renderers()) {
      const lookup = r.nodeLookup;
      if (!lookup) continue;
      try {
        for (const id of stale) this.paintNode(lookup[id], null);
        for (const [id, state] of this.marks) {
          const hex = id === this.activePath ? COLOR.active
            : state === 'read' ? COLOR.read
            : state === 'delegated' ? COLOR.delegated : COLOR.scan;
          if (this.paintNode(lookup[id], hex)) nowPainted.add(id);
        }
        if (typeof r.changed === 'function') r.changed();
        painted = true;
        this.strategy = 'nodeLookup';
      } catch (_) { /* internals moved */ }
    }
    // Nothing was painted: keep the old set, or the tints still on screen
    // would stop being tracked and could never be cleared.
    if (painted) this.painted = nowPainted;
    if (!painted && !this.dumped) { this.dumped = true; this.dumpDiagnostics('paint-failed'); }
    return painted;
  }

  paintNode(node, hex) {
    if (!node) return false;
    try {
      node.color = hex === null ? null : { a: 1, rgb: hex };
      if (node.circle && 'tint' in node.circle) node.circle.tint = hex === null ? 0xffffff : hex;
      return true;
    } catch (_) { return false; }
  }

  /** Record which nodes actually ended up coloured, so a run can be verified. */
  verifyPaint() {
    const res = { at: new Date().toISOString(), strategy: this.strategy || null,
                  marks: [...this.marks.entries()], coloured: [], missing: [],
                  robot: this.robotState() };
    for (const r of this.renderers()) {
      if (!r.nodeLookup) continue;
      for (const id of this.marks.keys()) {
        const n = r.nodeLookup[id];
        if (n && n.color) res.coloured.push({ id, rgb: n.color.rgb });
        else res.missing.push(id);
      }
      break;
    }
    try {
      fs.writeFileSync(path.join(path.dirname(this.settings.eventsPath), 'obsidian-verify.json'),
        JSON.stringify(res, null, 2), 'utf8');
    } catch (_) { /* ignore */ }
  }

  clearMarks(announce) {
    this.marks.clear();
    this.activePath = null;
    this.repaint();
    if (announce) this.status.setText('radar: —');
  }
};

class RadarSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();

    new Setting(containerEl)
      .setName('Event log path')
      .setDesc('The file the vault-radar hook appends to.')
      .addText((t) => t
        .setValue(this.plugin.settings.eventsPath)
        .onChange(async (v) => {
          this.plugin.settings.eventsPath = v.trim();
          this.plugin.seekToEnd();   // tail the new file; offset 0 replays months of log
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('Highlight grep matches')
      .setDesc('Tint files a search matched by name but never opened.')
      .addToggle((t) => t
        .setValue(this.plugin.settings.showScan)
        .onChange(async (v) => {
          this.plugin.settings.showScan = v;
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('Reset on each prompt')
      .setDesc('Clear highlighting when a new prompt starts.')
      .addToggle((t) => t
        .setValue(this.plugin.settings.fadeOnPrompt)
        .onChange(async (v) => {
          this.plugin.settings.fadeOnPrompt = v;
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('Show the robot')
      .setDesc('A sprite that walks the graph edges to whatever is being read.')
      .addToggle((t) => t
        .setValue(this.plugin.settings.robot)
        .onChange(async (v) => {
          this.plugin.settings.robot = v;
          if (!v) this.plugin.removeRobot();
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('Robot size')
      .addSlider((s) => s
        .setLimits(10, 60, 2)
        .setValue(this.plugin.settings.robotSize)
        .setDynamicTooltip()
        .onChange(async (v) => {
          this.plugin.settings.robotSize = v;
          this.plugin.removeRobot();
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('Walk speed')
      .setDesc('Higher is faster. 0.045 covers one edge in about half a second.')
      .addSlider((s) => s
        .setLimits(1, 15, 1)
        .setValue(Math.round(this.plugin.settings.robotSpeed * 200))
        .setDynamicTooltip()
        .onChange(async (v) => {
          this.plugin.settings.robotSpeed = v / 200;
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('Poll interval (ms)')
      .addText((t) => t
        .setValue(String(this.plugin.settings.pollMs))
        .onChange(async (v) => {
          const n = parseInt(v, 10);
          if (Number.isFinite(n) && n >= 100) {
            this.plugin.settings.pollMs = n;
            await this.plugin.saveData(this.plugin.settings);
          }
        }));
  }
}

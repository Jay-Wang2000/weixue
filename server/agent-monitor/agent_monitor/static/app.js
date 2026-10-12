"use strict";
(() => {
  const QS = new URLSearchParams(location.search);
  const TOKEN = QS.get("token") || "";
  const SEEN_KEY = "wx-last-seen";
  const THEME_KEY = "wx-theme";
  const LIVE_STALE_S = 30;
  const FOLD_GAP_MS = 15 * 60 * 1000;
  const STEPS_MAX = 6;
  const chatsShown = () => (window.matchMedia("(max-width: 899px)").matches ? 4 : 7);
  const SEGMENTS = [["all", "Everything"], ["ha", "HA actions"], ["approvals", "Approvals"], ["failed", "Failed"],
                    ["cancelled", "Cancelled"]];
  const TAGS = { ha: "HA", server: "SRV", app: "APP", memory: "MEM", device: "DEV" };
  const ROOMS = ["living_room", "dining", "kitchen", "bedroom", "study", "office", "bathroom", "hall", "garage",
                 "garden", "working_area"];
  const S = {
    tz: "Europe/Dublin", overview: null, overviewAt: 0, cost: null, chats: [], problems: null, home: null,
    groups: [], nextBefore: null, segments: null, paged: false, pending: null, newCount: 0,
    filters: { segment: "all", chat: "", q: "", state: QS.get("state") || "", kind: QS.get("kind") || "" },
    lifecycle: false, chatsAll: false, since: null, sinceData: null, lastMsg: 0, connected: false,
    traces: new Map(), openTraces: new Set(), traceTab: new Map(), traceSel: new Map(), unfolded: new Set(),
    fresh: new Set(), linked: null, loaded: false,
  };
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ DOM and formatting helpers

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false || c === "") continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const fill = (el, ...nodes) => { if (el) el.replaceChildren(...nodes.flat(Infinity).filter(Boolean)); };
  const glyph = (kind, title) => h("span", { class: `g g-${kind}`, title: title || null, "aria-hidden": title ? null : "true" });
  const join = (parts, sep = " · ") => parts.filter((p) => p !== null && p !== undefined && p !== "" && p !== false).join(sep);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
  const epoch = (ts) => { const t = Date.parse(ts); return isNaN(t) ? null : t; };
  const nowMs = () => Date.now();

  function fmt(ts, opts) {
    const t = typeof ts === "number" ? ts : epoch(ts);
    if (t === null) return "";
    return new Intl.DateTimeFormat("en-GB", { timeZone: S.tz, hour12: false, ...opts }).format(new Date(t));
  }
  const hm = (ts) => fmt(ts, { hour: "2-digit", minute: "2-digit" });
  const hms = (ts) => fmt(ts, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const dayKey = (ts) => fmt(ts, { year: "numeric", month: "2-digit", day: "2-digit" });
  const dayLabel = (ts) => fmt(ts, { weekday: "short", day: "numeric", month: "short" });
  const weekday = (ts) => fmt(ts, { weekday: "short" });

  function whenShort(ts) {
    const t = epoch(ts);
    if (t === null) return "";
    if (dayKey(t) === dayKey(nowMs())) return hm(t);
    if (nowMs() - t < 6 * 86400000) return `${weekday(t)} ${hm(t)}`;
    return `${fmt(t, { day: "numeric", month: "short" })} ${hm(t)}`;
  }
  function rel(ts) {
    const t = epoch(ts);
    if (t === null) return "";
    const s = Math.max(0, (nowMs() - t) / 1000);
    if (s < 45) return "now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (dayKey(t) === dayKey(nowMs()) || s < 6 * 3600) {
      const m = Math.round(s / 60);
      return `${Math.floor(m / 60)} h ${m % 60} min ago`;
    }
    return weekday(t);
  }
  const age = (s) => s === null || s === undefined ? "" : s < 60 ? `${Math.max(0, Math.round(s))} s` :
    s < 3600 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
  const dur = (ms) => ms === null || ms === undefined ? "" : ms < 1000 ? `${Math.round(ms)} ms` :
    ms < 60000 ? `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s` : `${Math.round(ms / 60000)} min`;
  const usd = (v, digits) => v === null || v === undefined ? "–" : `$${Number(v).toFixed(digits ?? (v >= 1 ? 2 : 4))}`;
  const num = (v) => v === null || v === undefined ? "–" : Number(v).toLocaleString("en-GB");
  const kilo = (v) => !v ? "0" : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1000 ? `${Math.round(v / 1000)}k` : String(v);
  const short = (v, n = 90) => { if (v === null || v === undefined) return ""; const s = typeof v === "string" ? v : JSON.stringify(v); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const model = (m) => m ? m.replace(/^claude-/, "") : "";
  const shortId = (id) => !id ? "" : id.length > 14 ? `${id.slice(0, 10)}…${id.slice(-4)}` : id;
  const relSpan = (ts, cls) => h("small", { class: cls || null, "data-rel": ts, text: rel(ts) });

  // ------------------------------------------------------------------ network

  const authHeaders = () => TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};
  async function getJson(path) {
    const resp = await fetch(path, { headers: authHeaders(), cache: "no-store" });
    if (!resp.ok) throw new Error(`http ${resp.status}`);
    return resp.json();
  }
  const streamUrl = () => TOKEN ? `/api/stream?token=${encodeURIComponent(TOKEN)}` : "/api/stream";

  // ------------------------------------------------------------------ theme

  function applyTheme(t) {
    const root = document.documentElement;
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    document.querySelectorAll(".theme button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.t === (t || "auto"))));
  }
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
    applyTheme(QS.get("theme") || saved || "auto");
    document.querySelectorAll(".theme button").forEach((b) => b.addEventListener("click", () => {
      applyTheme(b.dataset.t);
      try { localStorage.setItem(THEME_KEY, b.dataset.t); } catch (e) { /* per-viewer convenience only */ }
    }));
  }

  // ------------------------------------------------------------------ last look

  function readSeen() {
    try {
      const v = localStorage.getItem(SEEN_KEY);
      return v && epoch(v) !== null ? v : null;
    } catch (e) { return null; }
  }
  function writeSeen() {
    try { localStorage.setItem(SEEN_KEY, new Date().toISOString()); } catch (e) { /* falls back to midnight */ }
  }

  // ------------------------------------------------------------------ derived facts

  const userText = (g) => { const t = (g.turns || []).find((x) => x.kind === "user") || (g.turns || [])[0]; return t && t.text; };
  const replyText = (g) => { const t = [...(g.turns || [])].reverse().find((x) => x.final_text); return t ? t.final_text : g.summary; };
  const isOpen = (g) => !["done", "failed", "cancelled"].includes(g.state);
  const heldCall = (g) => (g.tool_calls || []).find((c) => c.status === "pending" && isOpen(g));
  const decided = (g) => (g.tool_calls || []).filter((c) => c.approval_decision);
  const problemLabel = (g) => (g.reasons && g.reasons.length && g.severity === "notice" ? g.reasons[0].reason : g.code);
  const chatTitle = (id) => { const c = S.chats.find((x) => x.chat_id === id); return c ? (c.title || "untitled chat") : null; };

  function common(key) {
    const counts = new Map();
    for (const g of S.groups) { const v = key(g); if (v) counts.set(v, (counts.get(v) || 0) + 1); }
    let best = null, n = 0;
    for (const [v, c] of counts) if (c > n) { best = v; n = c; }
    return best;
  }

  function decisionText(c) {
    const verb = c.approval_decision === "approve" ? "approved" : c.approval_decision === "deny" ? "denied" : c.approval_decision;
    return join([verb, c.decided_via ? `on the ${c.decided_via}` : ""], " ");
  }
  function heldFor(c) {
    const a = epoch(c.started_at), b = epoch(c.decided_at);
    return a !== null && b !== null ? Math.max(0, (b - a) / 1000) : null;
  }

  function deliveryPhrase(g) {
    const ds = (g.deliveries || []).filter((d) => d.state !== "superseded");
    const d = ds[ds.length - 1];
    if (!d) return "";
    const ack = (d.acks || [])[d.acks.length - 1];
    const outcome = ack ? ack.outcome : d.playback;
    if (outcome === "cut_off" || outcome === "cut") return `cut off${ack && ack.cut_at_ms ? " at " + dur(ack.cut_at_ms) : ""}`;
    if (d.tier === "SILENT") return outcome === "shown" ? "shown silently" : "sent silently";
    if (outcome === "played") return d.tier === "WHEN_IDLE" ? "spoken when idle, played to the end" : "spoken, played to the end";
    return join([d.tier === "WHEN_IDLE" ? "spoken when idle" : d.tier.toLowerCase(), d.state === "delivered" ? "" : d.state]);
  }

  function outcomeLine(g) {
    if (g.state === "failed") {
      const t = [...(g.turns || [])].reverse().find((x) => x.state === "failed" || x.terminal_reason);
      const reason = (t && t.terminal_reason) || g.outcome || "failed";
      return h("div", { class: "outcome bad", text: join([`failed · ${reason}`, g.duration_ms ? `after ${dur(g.duration_ms)}` : ""], " ") });
    }
    if (g.state === "cancelled") {
      const t = (g.turns || []).find((x) => x.interrupted_by);
      return h("div", { class: "outcome cancel", text: join(["cancelled by you", t ? `on the ${t.interrupted_by}` : ""], " ") });
    }
    return null;
  }

  // ------------------------------------------------------------------ verdict bar and clock

  function renderVerdict() {
    const o = S.overview;
    if (!o) return;
    const v = o.verdict || { state: "ok", held: 0, down: [] };
    const held = (o.mcp && o.mcp.pending_ours) || [];
    const inflight = (o.agent && o.agent.turns_in_flight) || 0;
    const box = document.querySelector(".verdict");
    box.className = `verdict ${v.state === "attention" ? "attn" : "ok"}`;
    const why = [];
    if (v.state === "attention") {
      $("v-glyph").className = `g ${held.length ? "g-held" : "g-bad"}`;
      $("v-word").textContent = "Needs a look";
      if (held.length) {
        why.push(["Approval waiting ", h("b", { "data-held-age": "1", text: age(heldAge(held[0])) })]);
        why.push([heldWhat(held[0])]);
      }
      for (const label of v.down) {
        const tile = (o.tiles || []).find((t) => t.label === label);
        why.push([h("b", { text: label }), ` ${tile ? tile.word : "down"}`, tile && tile.code ? ` (${tile.code})` : ""]);
      }
      if (inflight) why.push([plural(inflight, "turn") + " in flight"]);
    } else {
      $("v-glyph").className = "g g-ok";
      $("v-word").textContent = "All good";
      why.push([inflight ? `Busy · ${plural(inflight, "turn")} in flight` : "Idle · nothing in flight"]);
      const last = S.groups.length ? (S.groups[0].updated_at || S.groups[0].created_at) : null;
      if (last) why.push(["last turn ", h("b", { "data-rel": last, text: rel(last) })]);
      const degraded = (o.tiles || []).filter((t) => t.state === "warn").map((t) => t.label);
      if (degraded.length) why.push([`${degraded.join(", ")} degraded`]);
      if (S.problems) why.push([S.problems.failures ? `${plural(S.problems.failures, "failure")} in 24 h` : "no failures in 24 h"]);
    }
    const parts = [];
    why.forEach((p, i) => { if (i) parts.push(" · "); parts.push(...p); });
    fill($("v-why"), parts);
  }

  function renderClock() {
    const now = nowMs();
    fill($("clock"), h("b", { text: hm(now) }), ` ${dayLabel(now)} · ${S.tz.split("/").pop()}`);
    const live = $("live"), text = $("live-text");
    if (!S.connected && !S.lastMsg) { live.className = "live"; text.textContent = "connecting"; return; }
    const quiet = (now - S.lastMsg) / 1000;
    live.className = `live ${S.connected && quiet < LIVE_STALE_S ? "on" : "stale"}`;
    text.textContent = S.connected ? `Live · ${age(quiet)}` : `Reconnecting · ${age(quiet)}`;
  }

  // ------------------------------------------------------------------ held-approval card

  const heldAge = (a) => (a.age_s || 0) + (nowMs() - S.overviewAt) / 1000;
  const heldLeft = (a) => a.expires_in_s === null || a.expires_in_s === undefined ? null :
    Math.max(0, a.expires_in_s - (nowMs() - S.overviewAt) / 1000);
  function heldWhat(a) {
    const g = S.groups.find((x) => x.task_id === a.task_id);
    const said = g && userText(g);
    return said ? `“${short(said, 60)}”` : join([a.tool, a.target], " ");
  }

  function renderHeld() {
    const o = S.overview;
    const held = (o && o.mcp && o.mcp.pending_ours) || [];
    const card = $("held");
    card.hidden = !held.length;
    if (!held.length) { fill(card); return; }
    const a = held[0];
    const g = S.groups.find((x) => x.task_id === a.task_id);
    const said = g && userText(g);
    const m = o.mcp;
    fill(card,
      h("div", {},
        h("h2", {}, glyph("held"), "Held for your approval"),
        h("div", { class: "what" }, said ? h("q", { text: said }) : h("code", { text: a.tool })),
        h("div", { class: "tool" }, h("code", { text: a.tool }), a.target ? [" · ", h("code", { text: a.target })] : null,
          g ? ` · in ${chatTitle(g.chat_id) || "a chat"}` : "", a.created_at ? ` · asked ${hms(a.created_at)}` : ""),
        held.length > 1 ? h("div", { class: "more-held", text: `+${plural(held.length - 1, "more held call")}` }) : null),
      h("div", {}, h("div", { class: "meter" }, h("i", { id: "held-meter" })),
        h("div", { class: "meter-l" }, h("span", { id: "held-age" }), h("span", { id: "held-left" }))),
      h("div", { class: "where" }, h("b", { text: "Decide on the device panel or in Home Assistant" }),
        join([m.policy_version !== null && m.policy_version !== undefined ? `Policy v${m.policy_version}` : "",
              m.pending_not_ours !== null && m.pending_not_ours !== undefined ? `${m.pending_not_ours} left for you in HA` : ""])));
    tickHeld();
  }

  function tickHeld() {
    const o = S.overview;
    const held = (o && o.mcp && o.mcp.pending_ours) || [];
    if (!held.length) return;
    const a = held[0];
    const ageS = heldAge(a), left = heldLeft(a);
    const total = left === null ? null : ageS + left;
    const meter = $("held-meter");
    if (meter) meter.style.width = total ? `${Math.min(100, (ageS / total) * 100).toFixed(1)}%` : "0";
    if ($("held-age")) $("held-age").textContent = `held ${age(ageS)}`;
    if ($("held-left")) $("held-left").textContent = left === null ? "no expiry known" : `expires in ${age(left)} of ${age(total)}`;
    document.querySelectorAll("[data-held-age]").forEach((el) => { el.textContent = age(ageS); });
    for (const p of held) document.querySelectorAll(`[data-held-task="${CSS.escape(p.task_id || "")}"]`).forEach((el) => { el.textContent = age(heldAge(p)); });
  }

  // ------------------------------------------------------------------ context band

  function cell(cls, title, right, ...body) {
    return h("div", { class: `cell ${cls || ""}` }, h("h3", {}, title, right ? h("span", { class: right.cls || null, text: right.text || right }) : null), body);
  }

  function connectionsCell(o) {
    const tiles = o.tiles || [];
    const bad = tiles.filter((t) => t.state === "bad"), warn = tiles.filter((t) => t.state === "warn");
    const g = bad.length ? "bad" : warn.length ? "warn" : tiles.length ? "ok" : "off";
    const word = bad.length ? `${bad.length} down` : warn.length ? `${warn.length} degraded` : `All ${tiles.length} up`;
    const probe = tiles.find((t) => t.id === "ha_mcp");
    const facts = tiles.map((t) => h("li", {}, h("code", { text: t.label }), ` ${t.word}`,
      t.age_s !== null && t.age_s !== undefined ? ` · evidence ${age(t.age_s)} old` : "", t.code ? ` · ${t.code}` : "",
      tileDetail(t)));
    const lanes = h("details", { class: "lanes-d", id: "lanes" },
      h("summary", { text: S.history ? `24 h history · ${plural(S.history.lanes.reduce((n, l) => n + l.flips, 0), "change")}` : "24 h history" }),
      h("div", { id: "lanes-body" }, S.history ? lanesNode(S.history) : h("div", { class: "k", text: "Loading…" })),
      h("ul", { class: "tile-facts" }, facts));
    lanes.open = S.lanesOpen;
    lanes.addEventListener("toggle", () => { S.lanesOpen = lanes.open; if (lanes.open) loadHistory(); });
    return cell(`conncell ${bad.length ? "attn" : ""}`, "Connections", probe && probe.age_s !== null ? `probe ${age(probe.age_s)}` : null,
      h("div", { class: `big ${bad.length ? "b" : warn.length ? "w" : ""}` }, glyph(g), word),
      h("ul", { class: "conn" }, tiles.map((t) => h("li", { class: t.state === "bad" ? "bad" : t.state === "warn" ? "warn" : null, title: join([t.word, t.code]) },
        glyph(t.state === "ok" ? "ok" : t.state === "bad" ? "bad" : t.state === "warn" ? "warn" : "off"), h("span", { text: t.label }),
        h("span", { text: t.state === "ok" ? (t.value || t.word) : t.word })))),
      lanes);
  }

  function tileDetail(t) {
    const d = t.detail || {};
    if (t.id === "device" && Array.isArray(d.devices)) {
      return d.devices.map((x, i) => ` · device ${i + 1}: ${x.word}${x.last_poll_seconds_ago !== null && x.last_poll_seconds_ago !== undefined ? `, last poll ${age(x.last_poll_seconds_ago)} ago` : ""}`);
    }
    if (t.id === "bridge") return join([d.tools ? ` · ${d.tools} tools (${d.tools_source || "?"})` : "", d.ping_first_byte_ms_max ? `ping max ${d.ping_first_byte_ms_max} ms` : ""]);
    if (t.id === "agent") return Object.entries(d).filter(([, v]) => v === false).map(([k]) => ` · ${k} check failing`);
    return "";
  }

  function lanesNode(hist) {
    if (!hist.lanes.length) return h("div", { class: "k", text: "No probe history stored yet." });
    const start = epoch(hist.from), end = epoch(hist.to), span = Math.max(1, end - start);
    return h("div", { class: "lanes" },
      hist.lanes.map((l) => [h("span", { class: "ln", text: l.label }),
        h("div", { class: "lane", role: "img", "aria-label": `${l.label}, last ${hist.hours} h` },
          l.segments.map((s) => h("i", { class: s.state === "ok" ? null : s.state, title: `${s.state} ${whenShort(s.from)}–${whenShort(s.to)}`,
            style: { width: `${(((epoch(s.to) - epoch(s.from)) / span) * 100).toFixed(2)}%` } }))),
        h("span", { class: "nw", text: l.latency_ms !== null && l.latency_ms !== undefined ? `${l.latency_ms} ms` : "" })]),
      h("div", { class: "lane-x" }, h("span", { text: hm(start) }), h("span", { text: hm(start + span / 2) }), h("span", { text: "now" })));
  }

  async function loadHistory() {
    try {
      S.history = await getJson(`/api/health/history?hours=24`);
      S.historyAt = nowMs();
      fill($("lanes-body"), lanesNode(S.history));
      const sum = document.querySelector("#lanes summary");
      if (sum) sum.textContent = `24 h history · ${plural(S.history.lanes.reduce((n, l) => n + l.flips, 0), "change")}`;
    } catch (e) { fill($("lanes-body"), h("div", { class: "k", text: "History not available." })); }
  }

  function approvalsCell(o) {
    const m = o.mcp || {};
    const held = m.pending_ours || [];
    const last = m.last_decision;
    const lastText = last ? join([(last.title || last.target || last.tool).replace(/[.。!?！？]+$/u, ""), decisionText(last), whenShort(last.decided_at)], ", ") : "no decisions yet";
    const facts = join([m.wait_seconds !== null && m.wait_seconds !== undefined ? `hold ${m.wait_seconds} s` : "",
                        m.tool_count !== null && m.tool_count !== undefined ? `${m.tool_count} tools` : "",
                        m.pending_not_ours !== null && m.pending_not_ours !== undefined ? `${m.pending_not_ours} left in HA` : ""]);
    return cell(`need ${held.length ? "attn" : ""}`, "Approvals", m.policy_version !== null && m.policy_version !== undefined ? `policy v${m.policy_version}` : null,
      held.length ? h("div", { class: "big w" }, glyph("held"), `${held.length} waiting`) : h("div", { class: "big" }, glyph("ok"), "None waiting"),
      held.length ? h("div", { class: "sub", text: join([held[0].tool, held[0].target, heldLeft(held[0]) !== null ? `${age(heldLeft(held[0]))} left` : ""]) })
        : h("div", { class: "sub", text: `Last: ${lastText}` }),
      h("div", { class: "sub ph", text: held.length ? join([held[0].target || held[0].tool]) : `Last: ${last ? join([whenShort(last.decided_at), last.decided_via ? `on ${last.decided_via}` : ""], " ") : "none"}` }),
      facts ? h("div", { class: "sub2", text: facts }) : null);
  }

  function problemsCell() {
    const p = S.problems;
    if (!p) return cell("need", "Problems · 24 h", null, h("div", { class: "big", text: "–" }));
    const failures = p.groups.filter((g) => g.severity === "failure");
    const notices = p.groups.filter((g) => g.severity === "notice");
    const big = p.failures ? h("div", { class: "big" }, glyph("bad"), h("span", { class: "split" }, plural(p.failures, "failure"),
        p.notices ? h("small", { text: `+${p.notices} notices` }) : null))
      : h("div", { class: "big" }, glyph("ok"), h("span", { class: "split" }, "No failures", p.notices ? h("small", { text: `+${p.notices} notices` }) : null));
    const rows = failures.slice(0, 2).map((g) => h("li", {}, glyph("bad"), h("code", { text: g.code }), h("span", { class: "n", text: `×${g.count} · ${whenShort(g.last_ts)}` })));
    if (notices.length) rows.push(h("li", { class: "note" }, glyph("note"), h("code", { text: problemLabel(notices[0]) }), h("span", { class: "n", text: `×${notices[0].count} · notice` })));
    const ph = join([p.notices ? `+${p.notices} notices` : "", ...failures.slice(0, 2).map((g) => `${g.code} ×${g.count}`)]);
    return cell("need", "Problems · 24 h", `${plural(p.rows, "row")}`, big, h("ul", { class: "prob" }, rows), h("div", { class: "sub ph", text: ph || "none" }));
  }

  function lastDays(n) {
    const out = [];
    const today = epoch(S.overview && S.overview.now) || nowMs();
    for (let i = n - 1; i >= 0; i--) {
      const t = today - i * 86400000;
      out.push({ key: dayKey(t), t });
    }
    return out;
  }
  const isoDay = (key) => { const [d, m, y] = key.split("/"); return `${y}-${m}-${d}`; };

  function todayCell(o) {
    const c = S.cost || {};
    const t = (c.totals && c.totals.today) || {};
    const counts = o.counts || {};
    const tokIn = (t.input_tokens || 0) + (t.cache_read_tokens || 0) + (t.cache_creation_tokens || 0);
    const byDay = Object.fromEntries((c.by_day || []).map((d) => [d.day, d.total.cost_usd || 0]));
    const days = lastDays(7).map((d) => ({ ...d, v: byDay[isoDay(d.key)] || 0 }));
    const max = Math.max(0.0001, ...days.map((d) => d.v));
    const top = days.reduce((a, b) => (b.v > a.v ? b : a), days[0]);
    const turns = counts.turns_today;
    return cell("", "Today", S.tz.split("/").pop(),
      h("div", { class: "big", text: usd(counts.cost_today_usd ?? t.cost_usd, 2) }),
      h("div", { class: "sub", text: join([turns !== null && turns !== undefined ? plural(turns, "turn") : "", tokIn ? `${kilo(tokIn)} tok in` : "",
        t.cache_hit_pct !== null && t.cache_hit_pct !== undefined ? `${Math.round(t.cache_hit_pct)}% cached` : ""]) }),
      h("div", { class: "sub ph", text: join([turns !== null && turns !== undefined ? plural(turns, "turn") : "", t.cache_hit_pct !== null && t.cache_hit_pct !== undefined ? `${Math.round(t.cache_hit_pct)}% cached` : ""]) }),
      h("div", { class: "spark", role: "img", "aria-label": "Cost per day, last 7 days" },
        days.map((d, i) => h("i", { class: i === days.length - 1 ? "t" : null, title: `${dayLabel(d.t)} ${usd(d.v, 2)}`, style: { height: `${Math.max(2, (d.v / max) * 100).toFixed(0)}%` } }))),
      h("div", { class: "sparkx" }, h("span", { text: weekday(days[0].t) }), top.v > 0 ? h("span", { text: `${weekday(top.t)} ${usd(top.v, 2)}` }) : null, h("span", { text: "Today" })));
  }

  const domain = (e) => e.entity_id.split(".")[0];
  function homeFacts(home) {
    const es = home.entities || [];
    const lights = es.filter((e) => domain(e) === "light");
    const on = lights.filter((e) => e.state === "on");
    const temps = es.filter((e) => e.attributes && e.attributes.current_temperature !== undefined)
      .map((e) => `${e.name || e.entity_id} ${e.attributes.current_temperature} °C`);
    const others = es.filter((e) => ["cover", "lock"].includes(domain(e)))
      .map((e) => { const pos = e.attributes && e.attributes.current_position; return `${e.name || e.entity_id} ${pos !== undefined && pos !== 0 && pos !== 100 ? pos + "%" : e.state}`; });
    const hvac = es.filter((e) => domain(e) === "climate").map((e) => `${e.name || "Heating"} ${(e.attributes && e.attributes.hvac_action) || e.state}`);
    return { lights, on, temps, others, hvac };
  }

  function homeCell() {
    const home = S.home;
    if (!home || !home.enabled) return null;
    const f = homeFacts(home);
    const fetched = epoch(home.fetched_at);
    const head = fetched ? { text: `${home.stale ? "stale · " : ""}snapshot ${age((nowMs() - fetched) / 1000)} ago`, cls: home.stale ? "w" : null } : { text: "no snapshot yet", cls: "w" };
    const word = home.code ? "No snapshot" : f.lights.length ? `${f.on.length} light${f.on.length === 1 ? "" : "s"} on` : plural((home.entities || []).length, "entity", "entities");
    return cell("", "Home", head,
      h("div", { class: "big", text: word }),
      h("div", { class: "sub", text: join(f.temps.slice(0, 2).length ? f.temps.slice(0, 2) : f.others.slice(0, 2)) || "–" }),
      h("div", { class: "sub ph", text: join([...f.temps.slice(0, 1), ...f.others.slice(0, 1)]) || "–" }),
      h("div", { class: "sub2", text: join([...f.others.slice(0, 2), ...f.hvac.slice(0, 1), home.error ? `last attempt failed: ${home.error}` : "",
        home.period_s ? `every ${Math.round(home.period_s / 60)} min` : ""]) }));
  }

  function renderBand() {
    const o = S.overview;
    if (!o) return;
    const banner = $("banner");
    banner.hidden = !(o.db && o.db.banner);
    banner.textContent = (o.db && o.db.banner) || "";
    const home = homeCell();
    const band = $("context");
    band.className = `context ${home ? "" : "no-home"}`;
    fill(band, connectionsCell(o), approvalsCell(o), problemsCell(), todayCell(o), home);
  }

  // ------------------------------------------------------------------ since you last looked

  let sinceTimer = null;
  function scheduleSince() {
    if (sinceTimer) return;
    sinceTimer = setTimeout(() => { sinceTimer = null; loadSince(); }, 1500);
  }
  async function loadSince() {
    const q = new URLSearchParams({ hours: "24" });
    if (S.since) q.set("since", S.since);
    try { S.sinceData = await getJson(`/api/since?${q}`); renderAway(); } catch (e) { /* the next section update retries */ }
  }

  function sinceSentence(d) {
    const parts = [];
    parts.push(d.turns ? [h("b", { text: plural(d.turns, "voice turn") }), ` in ${plural(d.chats, "chat")}`] : ["No voice turns"]);
    if (d.ha_changes) parts.push([h("b", { text: plural(d.ha_changes, "change") }), " in Home Assistant"]);
    if (d.approvals && d.approvals.decided) parts.push([h("b", { text: plural(d.approvals.decided, "approval") }), " decided"]);
    for (const p of (d.problems || []).filter((x) => x.severity === "failure")) {
      if (p.source === "home_snapshot") parts.push([h("b", { text: `${plural(p.count, "home snapshot")} failed` }), p.recovered ? " and recovered" : ""]);
      else parts.push([h("b", { text: `${p.code} ×${p.count}` })]);
    }
    if (d.cancelled) parts.push([`${d.cancelled} cancelled`]);
    const out = [];
    parts.forEach((p, i) => { if (i) out.push(i === parts.length - 1 ? " and " : ", "); out.push(...p); });
    out.push(". ");
    const held = (S.overview && S.overview.mcp && S.overview.mcp.pending_ours) || [];
    out.push(held.length ? h("b", { text: held.length === 1 ? "One HA call is waiting on you." : `${held.length} HA calls are waiting on you.` }) : "Nothing waiting on you.");
    return out;
  }

  function renderAway() {
    const d = S.sinceData;
    if (!d) return;
    const sinceT = epoch(d.since);
    const day = dayKey(sinceT) === dayKey(nowMs()) ? "today" : dayKey(sinceT) === dayKey(nowMs() - 86400000) ? "yesterday" : weekday(sinceT);
    const lede = d.default ? "Since midnight" : `Since you last looked · ${day} ${hm(sinceT)}`;
    const from = epoch(d.pulse_from), to = epoch(d.now), span = Math.max(1, to - from);
    const pos = (t) => `${Math.min(100, Math.max(0, ((t - from) / span) * 100)).toFixed(2)}%`;
    const ticks = (d.pulse || []).map((t) => h("i", { class: `tick ${t.kind === "ok" ? "" : t.kind}`, title: `${hm(t.ts)} ${t.kind}`, style: { left: pos(epoch(t.ts)) },
      onclick: t.task_id ? () => { location.hash = `task=${t.task_id}`; } : null }));
    const seen = sinceT > from ? [h("div", { class: "shade", style: { left: pos(sinceT) } }), h("div", { class: "seen", style: { left: pos(sinceT) } }, h("span", { text: d.default ? "midnight" : "last looked" }))] : [];
    const labels = [];
    for (let t = Math.ceil(from / 3600000) * 3600000; t < to - 3 * 3600000; t += 3600000) {
      if (Number(fmt(t, { hour: "2-digit" })) % 6 === 0) labels.push(h("span", { text: hm(t), style: { left: pos(t) } }));
    }
    labels.push(h("span", { class: "end", text: "now", style: { left: "100%" } }));
    fill($("away"),
      h("div", {}, h("div", { class: "lede", text: lede }), h("div", { class: "head" }, sinceSentence(d))),
      h("div", {}, h("div", { class: "pulse", role: "img", "aria-label": "Activity, last 24 hours" }, seen, ticks), h("div", { class: "pulsex" }, labels)));
  }

  // ------------------------------------------------------------------ filters

  function renderFilters() {
    const seg = S.segments || {};
    const keyOf = { all: "everything", ha: "ha", approvals: "approvals", failed: "failed", cancelled: "cancelled" };
    fill($("seg"), SEGMENTS.map(([k, label]) => h("button", { type: "button", "aria-pressed": String(S.filters.segment === k),
      onclick: () => { if (S.filters.segment !== k) { S.filters.segment = k; refetch(); renderFilters(); } } },
      label, seg[keyOf[k]] !== undefined ? h("span", { text: num(seg[keyOf[k]]) }) : null)));
    const select = $("f-chat");
    const current = S.filters.chat;
    fill(select, h("option", { value: "", text: `All chats · ${S.chats.length}` }),
      S.chats.map((c) => h("option", { value: c.chat_id, text: c.title || "untitled chat" })));
    select.value = current;
    const pill = $("newpill");
    pill.hidden = !S.newCount;
    pill.textContent = `${S.newCount} new · jump to top`;
  }

  const filtered = () => S.filters.segment !== "all" || S.filters.chat || S.filters.q || S.filters.state || S.filters.kind;

  function timelineQuery(before) {
    const q = new URLSearchParams({ limit: "30" });
    const f = S.filters;
    if (f.segment !== "all") q.set("segment", f.segment);
    for (const k of ["chat", "q", "state", "kind"]) if (f[k]) q.set(k, f[k]);
    if (before) q.set("before", before);
    return `/api/timeline?${q}`;
  }

  let fetchSeq = 0;
  async function refetch() {
    const seq = ++fetchSeq;
    try {
      const data = await getJson(timelineQuery());
      if (seq !== fetchSeq) return;
      S.paged = false; S.pending = null; S.newCount = 0;
      S.groups = data.groups; S.nextBefore = data.next_before; S.segments = data.segments || S.segments;
      renderAfterTimeline(false);
      firstLoad();
    } catch (e) { /* kept until the next update */ }
  }
  async function older() {
    if (!S.nextBefore) return;
    try {
      const data = await getJson(timelineQuery(S.nextBefore));
      const known = new Set(S.groups.map((g) => g.task_id));
      S.groups = S.groups.concat(data.groups.filter((g) => !known.has(g.task_id)));
      S.nextBefore = data.next_before; S.paged = true;
      renderAfterTimeline(true);
    } catch (e) { /* the button stays */ }
  }

  function mergeFirstPage(data) {
    const byId = new Set(data.groups.map((g) => g.task_id));
    const oldest = data.groups.length ? data.groups[data.groups.length - 1].created_at : "";
    const rest = S.paged ? S.groups.filter((g) => !byId.has(g.task_id) && g.created_at < oldest) : [];
    S.groups = data.groups.concat(rest);
    if (!S.paged) S.nextBefore = data.next_before;
    S.segments = data.segments || S.segments;
  }

  let liveFetch = null;
  function onTimeline(data) {
    if (filtered()) {
      if (!liveFetch) liveFetch = setTimeout(async () => {
        liveFetch = null;
        try { applyLive(await getJson(timelineQuery())); } catch (e) { /* next update */ }
      }, 500);
      return;
    }
    applyLive(data);
  }

  function applyLive(data) {
    const known = new Set(S.groups.map((g) => g.task_id));
    const fresh = data.groups.filter((g) => !known.has(g.task_id)).map((g) => g.task_id);
    if (S.loaded && fresh.length && window.scrollY > 240) {
      S.pending = data;
      S.newCount = fresh.length;
      renderFilters();
      return;
    }
    S.fresh = S.loaded ? new Set(fresh) : new Set();
    mergeFirstPage(data);
    renderAfterTimeline(true);
    firstLoad();
  }

  function firstLoad() {
    if (S.loaded) return;
    S.loaded = true;
    openFromHash();
  }

  function jumpToTop() {
    if (S.pending) {
      const data = S.pending;
      const known = new Set(S.groups.map((g) => g.task_id));
      S.fresh = new Set(data.groups.filter((g) => !known.has(g.task_id)).map((g) => g.task_id));
      S.pending = null;
      mergeFirstPage(data);
    }
    S.newCount = 0;
    renderAfterTimeline(false);
    window.scrollTo(0, 0);
  }

  function renderAfterTimeline(keepScroll) {
    if (keepScroll) withAnchor(renderFeed); else renderFeed();
    renderFilters();
    renderVerdict();
    renderHeld();
    renderRail();
    scheduleSince();
  }

  // Keeps the first visible feed item at the same screen position across a re-render.
  function withAnchor(fn) {
    if (window.scrollY < 1) { fn(); return; }
    const items = [...document.querySelectorAll("#feed .ev[data-key]")];
    const anchor = items.find((el) => el.getBoundingClientRect().bottom > 130);
    const key = anchor && anchor.dataset.key, top = anchor && anchor.getBoundingClientRect().top;
    fn();
    if (!key) return;
    const again = [...document.querySelectorAll("#feed .ev[data-key]")].find((el) => el.dataset.key === key);
    if (again) window.scrollBy(0, again.getBoundingClientRect().top - top);
  }

  // ------------------------------------------------------------------ feed

  const norm = (t) => (t || "").toLowerCase().replace(/[\s\p{P}]+/gu, "");
  const foldable = (g) => ["done", "cancelled"].includes(g.state) && !(g.tool_calls || []).some((c) => !c.read_only || c.approval_id || c.approval_decision);

  function feedItems() {
    const items = S.groups.map((g) => ({ type: "task", g, ts: g.created_at, key: g.task_id }));
    const errorsShown = S.problems && !S.filters.chat && !S.filters.q && !S.filters.kind && !S.filters.state &&
      ["all", "failed"].includes(S.filters.segment);
    if (errorsShown) {
      const oldest = S.nextBefore && S.groups.length ? S.groups[S.groups.length - 1].created_at : "";
      for (const grp of S.problems.groups) {
        if (grp.severity !== "failure" && !S.lifecycle) continue;
        for (const w of grp.windows) {
          if (w.task_ids.length || w.to < oldest) continue;
          items.push({ type: "err", grp, w, ts: w.to, key: `err:${grp.source}:${grp.code}:${w.from}` });
        }
      }
    }
    items.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
    const out = [];
    for (const it of items) {
      const prev = out[out.length - 1];
      if (it.type === "task" && prev && foldable(it.g)) {
        const head = prev.type === "fold" ? prev.members[prev.members.length - 1] : prev.type === "task" ? prev.g : null;
        if (head && foldable(head) && head.chat_id === it.g.chat_id && norm(userText(head)) && norm(userText(head)) === norm(userText(it.g)) &&
            epoch(head.created_at) - epoch(it.g.created_at) <= FOLD_GAP_MS) {
          if (prev.type === "fold") prev.members.push(it.g);
          else out[out.length - 1] = { type: "fold", members: [prev.g, it.g], ts: prev.ts, key: `fold:${prev.g.task_id}` };
          continue;
        }
      }
      out.push(it);
    }
    return out.flatMap((it) => it.type === "fold" && S.unfolded.has(it.key) ? it.members.map((g) => ({ type: "task", g, ts: g.created_at, key: g.task_id, unfoldOf: it.key })) : [it]);
  }

  function renderFeed() {
    const items = feedItems();
    const feed = $("feed");
    if (!items.length) {
      fill(feed, h("p", { class: "empty", text: filtered() ? "Nothing matches these filters." : "No tasks yet." }));
    } else {
      const days = [];
      for (const it of items) {
        const k = dayKey(it.ts);
        if (!days.length || days[days.length - 1].key !== k) days.push({ key: k, ts: it.ts, items: [] });
        days[days.length - 1].items.push(it);
      }
      const usual = { model: common((g) => g.model), mode: common((g) => g.mode) };
      fill(feed, days.map((d) => [dayHeader(d), h("ol", { class: "feed" }, d.items.map((it) =>
        it.type === "task" ? taskItem(it.g, usual, it.unfoldOf) : it.type === "fold" ? foldItem(it) : errItem(it)))]));
    }
    $("older").hidden = !S.nextBefore;
    if (S.nextBefore && S.groups.length) $("older").textContent = `Older than ${whenShort(S.groups[S.groups.length - 1].created_at)}`;
    S.fresh = new Set();
    restoreTraces();
  }

  function dayHeader(d) {
    const tasks = d.items.flatMap((it) => it.type === "task" ? [it.g] : it.type === "fold" ? it.members : []);
    const turns = tasks.reduce((n, g) => n + ((g.counts && g.counts.turns) || 0), 0);
    const tools = tasks.reduce((n, g) => n + ((g.counts && g.counts.tool_calls) || 0), 0);
    const cost = tasks.reduce((n, g) => n + (g.cost_usd || 0), 0);
    const today = dayKey(nowMs()), yesterday = dayKey(nowMs() - 86400000);
    const label = d.key === today ? `Today · ${dayLabel(d.ts)}` : d.key === yesterday ? `Yesterday · ${dayLabel(d.ts)}` : dayLabel(d.ts);
    return h("div", { class: "day" }, h("h2", { text: label }), h("span", { class: "k", text: join([plural(turns, "turn"), plural(tools, "tool call"), usd(cost)]) }));
  }

  function itemGlyph(g) {
    if (g.state === "failed") return glyph("bad", "failed");
    if (g.state === "cancelled") return glyph("cancel", "cancelled");
    if (isOpen(g)) return glyph(heldCall(g) ? "held" : "run", heldCall(g) ? "held for approval" : "in flight");
    if (decided(g).length) return glyph("held", "held for approval");
    return glyph("ok", "done");
  }

  function stepNodes(g) {
    const rows = [];
    const calls = [...(g.tool_calls || [])].sort((a, b) => (a.started_at || "").localeCompare(b.started_at || ""));
    for (const t of (g.turns || []).filter((x) => x.kind !== "user")) {
      rows.push({ ts: t.created_at, node: h("li", { class: "step" }, h("span", { class: "tag", text: t.kind === "steer" ? "YOU" : "EVT" }),
        h("span", { class: "what", text: t.text ? `“${t.text}”` : t.kind }), h("span", { class: "res", text: hm(t.created_at) })) });
    }
    for (const c of calls) {
      if (c.approval_decision || (c.approval_id && c.status === "pending")) {
        const pending = !c.approval_decision;
        const heldS = heldFor(c);
        rows.push({ ts: c.started_at, node: h("li", { class: "step" }, h("span", { class: "tag hold", text: "HOLD" }),
          h("span", { class: "what", text: pending ? "Held by policy · waiting for your decision" : join(["Held by policy", `${decisionText(c)}${heldS !== null ? ` after ${age(heldS)}` : ""}`]) }),
          h("span", { class: `res ${pending ? "w" : c.approval_decision === "deny" ? "bad" : ""}`, text: pending ? "waiting" : hms(c.decided_at) })) });
      }
      const target = join([c.target, c.args_summary && !c.target ? short(c.args_summary, 80) : ""], " ");
      const status = c.status === "ok" ? `ok${c.duration_ms !== null && c.duration_ms !== undefined ? " " + dur(c.duration_ms) : ""}` : c.status;
      rows.push({ ts: c.started_at, node: h("li", { class: "step" }, h("span", { class: "tag", text: TAGS[c.kind] || (c.kind || "").toUpperCase() }),
        h("span", { class: "what", title: join([c.tool, target, c.result_summary ? `→ ${c.result_summary}` : ""], " ") }, h("code", { text: c.tool }), " ",
          target ? h("span", { class: "tgt", text: target }) : null, c.read_only ? h("span", { class: "k", text: " read" }) : null),
        h("span", { class: `res ${c.status === "ok" ? "okk" : ["error", "denied", "failed"].includes(c.status) ? "bad" : c.status === "pending" ? "w" : ""}`, text: status })) });
    }
    if (S.lifecycle) {
      for (const t of g.transitions || []) rows.push({ ts: t.ts, node: lifeStep("TASK", `${t.from_state || "∅"} → ${t.to_state}${t.reason ? " · " + t.reason : ""}`, hms(t.ts)) });
      for (const i of g.interactions || []) rows.push({ ts: i.created_at, node: lifeStep("ASK", join([i.kind, i.state, i.via ? `via ${i.via}` : ""]), hms(i.created_at)) });
      for (const d of g.deliveries || []) {
        rows.push({ ts: d.created_at, node: lifeStep("OUT", join([`${d.kind} ${d.tier}`, d.state, d.playback, d.attempt > 1 ? `attempt ${d.attempt}` : ""]), hms(d.created_at)) });
        for (const a of d.acks || []) rows.push({ ts: a.received_at, node: lifeStep("ACK", join([a.outcome, a.cut_at_ms !== null && a.cut_at_ms !== undefined ? `at ${dur(a.cut_at_ms)}` : "", a.reason]), hms(a.received_at)) });
      }
    }
    rows.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
    const nodes = rows.map((r) => r.node);
    if (nodes.length > STEPS_MAX + 1) {
      const extra = nodes.length - STEPS_MAX;
      return [...nodes.slice(0, STEPS_MAX), h("li", { class: "step more-steps" }, h("span"), h("span", { class: "what", text: `+${plural(extra, "more step")} in the trace` }), h("span"))];
    }
    return nodes;
  }
  const lifeStep = (tag, text, when) => h("li", { class: "step" }, h("span", { class: "tag life", text: tag }), h("span", { class: "what", text }), h("span", { class: "res", text: when }));

  function taskItem(g, usual, unfoldOf) {
    const said = userText(g) || g.title || "(no text)";
    const reply = replyText(g);
    const held = heldCall(g);
    const open = isOpen(g);
    const cls = ["ev"];
    if (open) cls.push("inflight");
    else if (g.state === "failed") cls.push("err");
    else if (g.state === "cancelled") cls.push("quiet");
    else if (decided(g).length) cls.push("held");
    if (S.fresh.has(g.task_id)) cls.push("fresh");
    const chips = [];
    const pendingA = ((S.overview && S.overview.mcp && S.overview.mcp.pending_ours) || []).find((a) => a.task_id === g.task_id);
    if (open) chips.push(h("span", { class: "chip w" }, pendingA ? ["in flight · held ", h("span", { "data-held-task": g.task_id, text: age(heldAge(pendingA)) })] : held ? "in flight · held" : "in flight"));
    for (const c of decided(g).slice(0, 1)) chips.push(h("span", { class: `chip ${c.approval_decision === "deny" ? "b" : "w"}`, text: join([decisionText(c), heldFor(c) !== null ? age(heldFor(c)) : ""]) }));
    if ((g.mode && g.mode !== usual.mode) || (g.model && g.model !== usual.model)) chips.push(h("span", { class: "chip", text: join([g.mode !== usual.mode ? g.mode : "", g.model !== usual.model ? model(g.model) : ""]) }));
    if (g.retry_of) chips.push(h("span", { class: "chip", text: "retry" }));
    const u = g.usage || {};
    const tokIn = (u.input_tokens || 0) + (u.cache_read_tokens || 0) + (u.cache_creation_tokens || 0);
    const first = (g.turns || [])[0];
    const chatLine = join([chatTitle(g.chat_id) ? `in ${chatTitle(g.chat_id)}` : "", deliveryPhrase(g),
      open && first && first.ttft_ms ? `first token ${dur(first.ttft_ms)}` : ""]);
    const extraTurns = (g.counts && g.counts.turns) || 0;
    const details = traceDetails(g);
    return h("li", { class: cls.join(" "), "data-key": g.task_id, "data-task": g.task_id },
      h("div", { class: "t" }, h("b", { text: hm(g.created_at) }), relSpan(g.created_at)),
      h("div", { class: "n" }, itemGlyph(g)),
      h("div", { class: "body card" },
        h("div", { class: "said" }, h("q", { text: said }), chips),
        reply && g.state !== "cancelled" ? h("div", { class: "reply", text: reply }) : null,
        outcomeLine(g),
        h("ul", { class: "steps" }, stepNodes(g)),
        chatLine ? h("div", { class: "chat" }, chatLine) : null,
        unfoldOf ? h("button", { type: "button", class: "unfold", text: "fold repeats", onclick: () => { S.unfolded.delete(unfoldOf); withAnchor(renderFeed); } }) : null,
        details),
      h("div", { class: "meta" }, h("span", { class: "c", text: open ? `${usd(g.cost_usd)} so far` : usd(g.cost_usd) }),
        g.duration_ms !== null && g.duration_ms !== undefined ? dur(g.duration_ms) : extraTurns > 1 ? plural(extraTurns, "turn") : "",
        tokIn ? [h("br"), h("span", { class: "x", text: join([`${kilo(tokIn)} in`, u.cache_hit_pct !== null && u.cache_hit_pct !== undefined ? `${Math.round(u.cache_hit_pct)}% cached` : ""]) })] : null));
  }

  function foldItem(it) {
    const ms = it.members;
    const g = ms[0];
    const cost = ms.reduce((n, x) => n + (x.cost_usd || 0), 0);
    const ds = ms.map((x) => x.duration_ms).filter((x) => x !== null && x !== undefined);
    const cancelled = ms.filter((x) => x.state === "cancelled").length;
    const outcome = cancelled ? h("div", { class: "outcome cancel", text: join([cancelled ? `cancelled by you ×${cancelled}` : "", ms.length - cancelled ? `answered ×${ms.length - cancelled}` : ""], ", ") })
      : h("div", { class: "reply", text: replyText(g) || "" });
    const oldest = ms[ms.length - 1];
    return h("li", { class: "ev quiet", "data-key": it.key },
      h("div", { class: "t" }, h("b", { text: hm(g.created_at) }), h("small", { text: `– ${hm(oldest.created_at)}` })),
      h("div", { class: "n" }, glyph("rep", "repeated")),
      h("div", { class: "body card" },
        h("div", { class: "said" }, h("q", { text: userText(g) || g.title }), h("span", { class: "rep", text: `×${ms.length}` }),
          h("span", { class: "group-out" }, ms.map((x) => glyph(x.state === "cancelled" ? "cancel" : "ok", x.state)))),
        outcome,
        h("div", { class: "chat", text: join([chatTitle(g.chat_id) ? `in ${chatTitle(g.chat_id)}` : "", `repeated between ${hm(oldest.created_at)} and ${hm(g.created_at)}`]) }),
        h("button", { type: "button", class: "unfold", text: `show all ${ms.length}`, onclick: () => { S.unfolded.add(it.key); withAnchor(renderFeed); } })),
      h("div", { class: "meta" }, h("span", { class: "c", text: usd(cost) }), ds.length ? (ds.length > 1 ? `${dur(Math.min(...ds))}–${dur(Math.max(...ds))}` : dur(ds[0])) : ""));
  }

  function errItem(it) {
    const { grp, w } = it;
    const failure = grp.severity === "failure";
    const name = grp.source === "home_snapshot" ? "Home snapshot failed" : failure ? `${grp.source} failed` : `${grp.source} notice`;
    const times = w.count === 1 ? `at ${hm(w.from)}` : `${hm(w.from)}–${hm(w.to)}`;
    const recovered = grp.recovered && w === grp.windows[grp.windows.length - 1] ? "recovered since" : "";
    return h("li", { class: `ev ${failure ? "err" : "quiet"}`, "data-key": it.key },
      h("div", { class: "t" }, h("b", { text: hm(w.to) }), relSpan(w.to)),
      h("div", { class: "n" }, glyph(failure ? "bad" : "note", failure ? "failure" : "notice")),
      h("div", { class: "body card" },
        h("div", { class: "said" }, name, w.count > 1 ? h("span", { class: "rep", text: `×${w.count}` }) : null),
        h("div", { class: "reply" }, h("code", { text: `${grp.source} · ${grp.code}` }), " ", join([times, recovered, grp.message ? short(grp.message, 120) : ""]))),
      h("div", { class: "meta" }, h("span", { class: "c", text: plural(w.count, "row") }), failure ? "failure" : "notice"));
  }

  // ------------------------------------------------------------------ trace

  function traceDetails(g) {
    const events = (g.counts ? g.counts.turns + g.counts.tool_calls + g.counts.deliveries : 0) + (g.transitions || []).length;
    const det = h("details", { class: "more", "data-trace": g.task_id },
      h("summary", {}, `Trace · ${plural(events, "event")} · `, h("span", { class: "mono", text: `#task=${shortId(g.task_id)}` })),
      h("div", { class: "trace-body" }));
    det.open = S.openTraces.has(g.task_id);
    det.addEventListener("toggle", () => {
      if (det.open) {
        S.openTraces.add(g.task_id);
        if (location.hash !== `#task=${g.task_id}`) history.replaceState(null, "", `${location.pathname}${location.search}#task=${g.task_id}`);
        loadTrace(g.task_id, g.updated_at);
      } else {
        S.openTraces.delete(g.task_id);
        if (location.hash === `#task=${g.task_id}`) history.replaceState(null, "", `${location.pathname}${location.search}`);
      }
    });
    return det;
  }

  function restoreTraces() {
    for (const id of S.openTraces) {
      const g = S.groups.find((x) => x.task_id === id);
      loadTrace(id, g ? g.updated_at : null);
    }
  }

  async function loadTrace(id, version) {
    const cached = S.traces.get(id);
    if (cached && (!version || cached.updated_at === version)) { drawTrace(id); return; }
    document.querySelectorAll(`details[data-trace="${CSS.escape(id)}"] .trace-body`).forEach((el) => {
      if (!el.firstChild) fill(el, h("div", { class: "trace" }, h("div", { class: "loading", text: "Loading trace…" })));
    });
    try {
      S.traces.set(id, await getJson(`/api/tasks/${encodeURIComponent(id)}`));
      drawTrace(id);
    } catch (e) {
      document.querySelectorAll(`details[data-trace="${CSS.escape(id)}"] .trace-body`).forEach((el) =>
        fill(el, h("div", { class: "trace" }, h("div", { class: "loading", text: "Trace not available." }))));
    }
  }

  function drawTrace(id) {
    const d = S.traces.get(id);
    if (!d) return;
    document.querySelectorAll(`details[data-trace="${CSS.escape(id)}"] .trace-body`).forEach((el) => fill(el, traceNode(d)));
  }

  function traceRows(d) {
    const rows = [];
    for (const t of d.turns || []) {
      rows.push({ id: `turn:${t.turn_id}`, kind: "turn", ts: t.created_at, end: t.ended_at || t.reply_end_at, tag: t.kind === "user" ? "YOU" : t.kind === "steer" ? "YOU" : "EVT",
        ft: t.ttft_ms, text: t.text || t.kind, sub: join([t.final_text ? "" : t.state, t.terminal_reason && t.terminal_reason !== "end_turn" ? t.terminal_reason : ""]), obj: t });
    }
    for (const c of d.tool_calls || []) {
      const end = c.ended_at || (c.duration_ms !== null && c.duration_ms !== undefined && epoch(c.started_at) !== null ? new Date(epoch(c.started_at) + c.duration_ms).toISOString() : null);
      rows.push({ id: `call:${c.tool_use_id}`, kind: "call", ts: c.started_at, end, tag: TAGS[c.kind] || (c.kind || "").toUpperCase(), call: c, gate: c.approval_decision || c.approval_id ? c.decided_at : null, obj: c });
    }
    for (const dl of d.deliveries || []) {
      const ack = (dl.acks || [])[0];
      rows.push({ id: `dlv:${dl.delivery_id}`, kind: "out", ts: dl.created_at, end: ack ? ack.received_at : dl.updated_at, tag: "OUT", dl, ack, obj: dl });
    }
    if (S.lifecycle) {
      for (const t of d.transitions || []) rows.push({ id: `tr:${t.id}`, kind: "life", ts: t.ts, tag: "TASK", text: `${t.from_state || "∅"} → ${t.to_state}${t.reason ? " · " + t.reason : ""}`, obj: t });
      for (const i of d.interactions || []) rows.push({ id: `int:${i.interaction_id}`, kind: "life", ts: i.created_at, end: i.decided_at, tag: "ASK", text: join([i.kind, i.source, i.state, i.via ? `via ${i.via}` : ""]), obj: i });
    }
    return rows.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
  }

  function traceNode(d) {
    const t0 = epoch(d.created_at);
    const t1 = epoch(d.finished_at) || (isOpen(d) ? nowMs() : epoch(d.updated_at)) || t0 + 1;
    const span = Math.max(1, t1 - t0);
    const pct = (t) => `${Math.min(100, Math.max(0, ((t - t0) / span) * 100)).toFixed(2)}%`;
    const width = (a, b) => `${Math.max(0.4, Math.min(100, ((b - a) / span) * 100)).toFixed(2)}%`;
    const u = d.usage || {};
    const tokIn = (u.input_tokens || 0) + (u.cache_read_tokens || 0) + (u.cache_creation_tokens || 0);
    const firstTurn = (d.turns || [])[0];
    const calls = d.tool_calls || [];
    const lastTurn = (d.turns || [])[(d.turns || []).length - 1];
    const stat = (l, v, unit) => h("div", { class: "stat" }, h("div", { class: "l", text: l }), h("div", { class: "v" }, v, unit ? h("small", { text: ` ${unit}` }) : null));
    const durMs = d.duration_ms !== null && d.duration_ms !== undefined ? d.duration_ms : t1 - t0;
    const stats = h("div", { class: "stats" },
      stat("Duration", durMs < 60000 ? (durMs / 1000).toFixed(1) : Math.round(durMs / 60000), durMs < 60000 ? "s" : "min"),
      stat("First token", firstTurn && firstTurn.ttft_ms ? (firstTurn.ttft_ms / 1000).toFixed(1) : "–", firstTurn && firstTurn.ttft_ms ? "s" : ""),
      stat("Cost", usd(d.cost_usd, 3)),
      stat("Input tokens", tokIn >= 1000 ? (tokIn / 1000).toFixed(1) : String(tokIn), tokIn >= 1000 ? "k" : ""),
      stat("Cache hit", u.cache_hit_pct !== null && u.cache_hit_pct !== undefined ? String(u.cache_hit_pct) : "–", u.cache_hit_pct !== null && u.cache_hit_pct !== undefined ? "%" : ""),
      stat("Tool calls", String(calls.length), calls.length ? `· ${calls.filter((c) => c.kind === "ha").length} HA` : ""));
    const tab = S.traceTab.get(d.task_id) || "trace";
    const tabs = h("div", { class: "ttabs", role: "tablist" },
      [["trace", "Trace"], ["conv", "Conversation"], ["raw", "Raw"]].map(([k, label]) => h("button", { type: "button", role: "tab", "aria-selected": String(tab === k),
        onclick: () => { S.traceTab.set(d.task_id, k); drawTrace(d.task_id); } , text: label })),
      h("span", { class: "r" }, join([model(d.model), d.mode]), lastTurn && lastTurn.terminal_reason ? [" · ", h("code", { text: lastTurn.terminal_reason })] : null));
    let body;
    if (tab === "conv") body = convNode(d);
    else if (tab === "raw") body = h("div", { class: "raw" }, h("pre", { text: JSON.stringify(redact(d), null, 2) }));
    else body = waterfall(d, t0, span, pct, width);
    return h("div", { class: "trace" }, stats, tabs, body);
  }

  function waterfall(d, t0, span, pct, width) {
    const rows = traceRows(d);
    const sel = S.traceSel.get(d.task_id) || defaultSel(rows);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f === 1 ? (span / 1000).toFixed(1) : f === 0 ? "0 s" : ((span * f) / 1000).toFixed(span < 10000 ? 1 : 0));
    const rowNodes = rows.map((r) => {
      const a = epoch(r.ts), b = r.end ? epoch(r.end) : null;
      const bars = [];
      let label, durText = "", durCls = "";
      if (r.kind === "turn") {
        bars.push(h("i", { class: "m", style: { left: pct(a), width: width(a, b || a) } }));
        if (r.ft) bars.push(h("i", { class: "ft", style: { left: pct(a + r.ft) } }));
        label = [h("span", { class: "you", text: r.text }), r.sub ? h("span", { class: "k", text: `· ${r.sub}` }) : null];
        durText = b ? dur(b - a) : "";
      } else if (r.kind === "call") {
        const c = r.call;
        if (r.gate) bars.push(h("i", { class: "gate", style: { left: pct(epoch(r.gate)) } }));
        bars.push(h("i", { style: { left: pct(a), width: width(a, b || a) } }));
        label = [h("code", { text: c.tool }), h("span", { class: "k mono", text: join([c.target || short(c.args_summary, 60), c.read_only ? "read" : "writes"]) })];
        durText = c.status === "ok" ? dur(c.duration_ms) : c.status;
        durCls = c.status === "ok" ? "okk" : ["error", "denied", "failed"].includes(c.status) ? "bad" : "";
      } else if (r.kind === "out") {
        bars.push(h("i", { class: "d", style: { left: pct(a), width: width(a, b || a) } }));
        label = [h("span", { class: "k", text: join([`${r.dl.kind} ${r.dl.tier}`, r.dl.state, r.dl.attempt ? `attempt ${r.dl.attempt}` : ""]) })];
        durText = r.ack ? r.ack.outcome : r.dl.playback || "";
      } else {
        bars.push(h("i", { class: "life", style: { left: pct(a) } }));
        label = [h("span", { class: "k", text: r.text })];
      }
      return h("div", { class: `row ${sel === r.id ? "hl" : ""}`, onclick: () => { S.traceSel.set(d.task_id, r.id); drawTrace(d.task_id); } },
        h("span", { class: "when", text: hms(r.ts) }), h("span", { class: "tag", text: r.tag }),
        h("div", {}, h("div", { class: "lbl" }, label), h("div", { class: "wf" }, bars)),
        h("span", { class: `dur ${durCls}`, text: durText }));
    });
    const life = (d.transitions || []).length + (d.deliveries || []).reduce((n, x) => n + (x.acks || []).length, 0) + (d.interactions || []).length;
    const ack = (d.deliveries || []).flatMap((x) => x.acks || []).pop();
    const states = ["created", ...(d.transitions || []).map((t) => t.to_state)].filter((s, i, a) => i === 0 || s !== a[i - 1]);
    const fold = S.lifecycle ? null : h("div", { class: "lifefold", text: `${plural(life, "lifecycle event")} folded: ${states.join(" → ")}${ack ? ` · ${ack.outcome} at ${hms(ack.received_at)}` : ""}. The Lifecycle toggle shows them.` });
    return [h("div", { class: "wfh" }, h("span", { class: "when", text: S.tz.split("/").pop() }), h("span"),
        h("div", { class: "ax" }, ticks.map((t) => h("span", { text: t }))), h("span", { class: "dur", text: "dur" })),
      rowNodes, fold,
      h("div", { class: "legend" }, h("span", {}, h("i", { class: "m" }), "model turn"), h("span", {}, h("i", { class: "t" }), "tool call"),
        h("span", {}, h("i", { class: "g" }), "policy gate"), h("span", {}, h("i", { class: "d" }), "delivery"), h("span", {}, h("i", { class: "f" }), "first token")),
      h("div", { class: "tbot" }, inspector(rows.find((r) => r.id === sel), d), tokenRail(d))];
  }

  function defaultSel(rows) {
    const write = rows.find((r) => r.kind === "call" && r.call.kind === "ha" && !r.call.read_only);
    const call = rows.find((r) => r.kind === "call");
    return (write || call || rows[0] || {}).id;
  }

  function pretty(v) {
    if (v === null || v === undefined || v === "") return "–";
    if (typeof v !== "string") return JSON.stringify(v, null, 2);
    try { return JSON.stringify(JSON.parse(v), null, 2); } catch (e) { return v; }
  }

  function inspector(r, d) {
    if (!r) return h("div", { class: "insp" });
    if (r.kind === "call") {
      const c = r.call;
      const dec = c.approval_decision ? h("span", { class: `dec ${c.approval_decision === "deny" ? "bad" : ""}`, text: decisionText(c) })
        : c.approval_id ? h("span", { class: "dec w", text: "held, waiting" }) : h("span", { class: "dec", text: c.read_only ? "read" : "allowed without a hold" });
      const heldS = heldFor(c);
      return h("div", { class: "insp" },
        h("h5", {}, h("code", { text: c.tool }), dec, h("span", { class: "mono k", text: join([c.decided_via ? `decided_via ${c.decided_via}` : "", heldS !== null ? `held ${age(heldS)}` : "", c.decided_at ? hms(c.decided_at) : ""]) })),
        h("div", { class: "cols" },
          h("div", {}, h("span", { text: "Arguments" }), h("pre", { text: pretty(c.args_summary) })),
          h("div", {}, h("span", { text: `Result${c.duration_ms !== null && c.duration_ms !== undefined ? " · " + dur(c.duration_ms) : ""}` }), h("pre", { text: pretty(c.result_summary) }))));
    }
    if (r.kind === "turn") {
      const t = r.obj;
      return h("div", { class: "insp" },
        h("h5", {}, h("code", { text: t.kind }), h("span", { class: "mono k", text: join([t.mode, model(t.model), t.terminal_reason, t.ttft_ms ? `first token ${dur(t.ttft_ms)}` : "", t.interrupted_by ? `interrupted by ${t.interrupted_by}` : ""]) })),
        h("div", { class: "cols" }, h("div", {}, h("span", { text: "Said" }), h("pre", { text: t.text || "–" })), h("div", {}, h("span", { text: "Reply" }), h("pre", { text: t.final_text || "–" }))));
    }
    return h("div", { class: "insp" }, h("h5", {}, h("code", { text: r.tag })), h("pre", { text: JSON.stringify(redact(r.obj), null, 2) }));
  }

  function tokenRail(d) {
    const u = d.usage || {};
    const cr = u.cache_read_tokens || 0, inp = (u.input_tokens || 0) + (u.cache_creation_tokens || 0), out = u.output_tokens || 0;
    const total = cr + inp + out;
    const w = (v) => ({ width: `${total ? ((v / total) * 100).toFixed(2) : 0}%` });
    const fx = (d.side_effects || []);
    return h("div", { class: "rail-facts" },
      h("h5", { text: `Tokens · ${num(total)} total` }),
      h("div", { class: "tok" }, h("i", { class: "cr", style: w(cr) }), h("i", { class: "in", style: w(inp) }), h("i", { class: "out", style: w(out) })),
      h("ul", { class: "tokl" },
        h("li", {}, h("span", {}, h("i", { class: "cr" }), "cache read"), h("b", { text: num(cr) })),
        h("li", {}, h("span", {}, h("i", { class: "in" }), "input, not cached"), h("b", { text: num(inp) })),
        h("li", {}, h("span", {}, h("i", { class: "out" }), "output"), h("b", { text: num(out) }))),
      h("ul", { class: "ids" },
        h("li", {}, h("span", { text: "Result tier" }), h("span", { text: d.result_tier || "–" })),
        h("li", {}, h("span", { text: "Side effects" }), h("span", { text: fx.length ? join(fx.slice(0, 2).map((x) => join([x.tool, x.target], " "))) + (fx.length > 2 ? ` +${fx.length - 2}` : "") : "none" })),
        h("li", {}, h("span", { text: "Retry of" }), h("span", { text: d.retry_of ? shortId(d.retry_of) : "none" })),
        h("li", {}, h("span", { text: "Task · trace" }), h("span", { text: join([shortId(d.task_id), d.trace_id ? `${d.trace_id.slice(0, 4)}…${d.trace_id.slice(-4)}` : ""]) }))));
  }

  function convNode(d) {
    return h("div", { class: "conv" }, (d.turns || []).map((t) => h("div", { class: "turn" },
      h("h6", { text: join([hms(t.created_at), t.kind, t.mode, model(t.model), t.ttft_ms ? `first token ${dur(t.ttft_ms)}` : "", t.terminal_reason, usd(t.cost_usd)]) }),
      h("div", { class: "u", text: t.text || "–" }), t.final_text ? h("div", { class: "a", text: t.final_text }) : null)));
  }

  function redact(v) {
    if (Array.isArray(v)) return v.map(redact);
    if (v && typeof v === "object") {
      const out = {};
      for (const [k, x] of Object.entries(v)) if (k !== "device_id" && k !== "decided_by_device") out[k] = redact(x);
      return out;
    }
    return v;
  }

  // ------------------------------------------------------------------ deep link

  async function openFromHash() {
    const m = /^#task=([A-Za-z0-9_-]{1,64})$/.exec(location.hash);
    if (!m) { S.linked = null; fill($("linked")); return; }
    const id = m[1];
    S.openTraces.add(id);
    const inFeed = document.querySelector(`#feed .ev[data-task="${CSS.escape(id)}"]`);
    if (inFeed) {
      fill($("linked"));
      const det = inFeed.querySelector("details.more");
      if (det && !det.open) det.open = true;
      inFeed.classList.add("target");
      setTimeout(() => inFeed.classList.remove("target"), 2500);
      inFeed.scrollIntoView({ block: "start", behavior: "smooth" });
      return;
    }
    try {
      const d = await getJson(`/api/tasks/${encodeURIComponent(id)}`);
      S.traces.set(id, d);
      S.linked = d;
      renderLinked();
      $("linked").scrollIntoView({ block: "start" });
    } catch (e) {
      fill($("linked"), h("div", { class: "linked" }, h("h2", {}, "Linked task not found", h("button", { type: "button", text: "close", onclick: closeLinked }))));
    }
  }
  function closeLinked() { S.linked = null; fill($("linked")); history.replaceState(null, "", `${location.pathname}${location.search}`); }
  function renderLinked() {
    if (!S.linked) return;
    const usual = { model: common((g) => g.model), mode: common((g) => g.mode) };
    fill($("linked"), h("div", { class: "linked" }, h("h2", {}, `Linked task · ${dayLabel(S.linked.created_at)}`, h("button", { type: "button", text: "close", onclick: closeLinked })),
      h("ol", { class: "feed" }, taskItem(S.linked, usual))));
    drawTrace(S.linked.task_id);
  }

  // ------------------------------------------------------------------ rail

  function renderRail() { renderChatsRail(); renderProblemsRail(); renderHomeRail(); }

  function renderChatsRail() {
    const chats = S.chats;
    const unread = chats.filter((c) => c.unread).length;
    const shown = S.chatsAll ? chats : chats.slice(0, chatsShown());
    fill($("r-chats"), h("h4", {}, "Chats", h("span", { text: `${chats.length} · ${unread} unread` })),
      chats.length ? h("ul", { class: "chats" }, shown.map((c) => h("li", { class: S.filters.chat === c.chat_id ? "on" : null, title: c.summary || null,
        onclick: () => { S.filters.chat = S.filters.chat === c.chat_id ? "" : c.chat_id; refetch(); renderFilters(); renderChatsRail(); } },
        h("span", { class: `u ${c.unread ? "" : "r"}`, title: c.unread ? "unread" : null }),
        h("span", { class: "nm" }, c.title || "untitled chat", h("small", { text: join([c.current ? "current" : "", c.busy ? "busy" : "", c.queued ? `${c.queued} queued` : "", c.task_state || c.task_outcome || "", c.mode]) })),
        h("span", { class: "tm", text: whenShort(c.last_activity_at).replace(/^(\w{3}) \d\d:\d\d$/, "$1") })))) : h("p", { class: "empty", text: "No chats yet." }),
      chats.length > chatsShown() ? h("button", { type: "button", class: "all", text: S.chatsAll ? "Fewer chats" : `All ${chats.length} chats`, onclick: () => { S.chatsAll = !S.chatsAll; renderChatsRail(); } }) : null);
  }

  function renderProblemsRail() {
    const p = S.problems;
    if (!p) return;
    fill($("r-problems"), h("h4", {}, "Problems · 24 h", h("span", { text: `${plural(p.failures, "failure")} · ${p.notices} notices` })),
      p.groups.length ? h("ul", { class: "probs" }, p.groups.map((g) => {
        const first = g.windows[0], last = g.windows[g.windows.length - 1];
        const when = g.count === 1 ? whenShort(g.last_ts) : `${whenShort(first ? first.from : g.first_ts)}–${hm(g.last_ts)}`;
        return h("li", { class: g.severity === "notice" ? "note" : null },
          glyph(g.severity === "notice" ? "note" : "bad"),
          h("span", { class: "nm" }, h("code", { text: problemLabel(g) }), h("small", {}, join([g.severity === "notice" ? `${g.source} · ${g.code}` : g.source, g.severity === "notice" ? `last ${whenShort(g.last_ts)}` : when, g.severity === "notice" ? "known notice" : g.recovered ? "recovered" : ""]),
            g.task_id ? [" · ", h("a", { href: `#task=${g.task_id}`, text: "open trace" })] : null)),
          h("span", { class: "ct", text: num(g.count) }));
      })) : h("p", { class: "empty", text: "Nothing in the last 24 h." }));
  }

  function roomOf(e) {
    if (e.attributes && e.attributes.area) return String(e.attributes.area);
    const obj = e.entity_id.split(".")[1] || "";
    let best = null, at = Infinity;
    for (const r of ROOMS) { const i = obj.indexOf(r); if (i >= 0 && i < at) { best = r; at = i; } }
    if (best === "working_area") best = "study";
    return best ? best.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "Other";
  }

  function renderHomeRail() {
    const home = S.home;
    const box = $("r-home");
    if (!home || !home.enabled) { box.hidden = true; return; }
    box.hidden = false;
    const rooms = new Map();
    for (const e of home.entities || []) {
      const r = roomOf(e);
      if (!rooms.has(r)) rooms.set(r, []);
      rooms.get(r).push(e);
    }
    const order = [...rooms.keys()].sort((a, b) => (a === "Other") - (b === "Other") || a.localeCompare(b));
    const since = (e) => { const t = epoch(e.last_changed || e.updated_at); return t === null ? "" : age((nowMs() - t) / 1000); };
    const value = (e) => {
      const a = e.attributes || {};
      if (e.state === "on" && a.brightness !== undefined) return `${Math.round(Number(a.brightness) / 2.55)}%`;
      if (a.current_position !== undefined && a.current_position !== 0 && a.current_position !== 100) return `${a.current_position}%`;
      if (domain(e) === "climate") return a.hvac_action || e.state;
      if (["on", "off"].includes(e.state)) return `${e.state === "off" ? "off " : ""}${since(e)}`;
      return `${e.state} ${since(e)}`;
    };
    const active = (e) => ["on", "open", "unlocked", "heat", "heating", "playing"].includes(e.state);
    const fetched = epoch(home.fetched_at);
    fill(box, h("h4", {}, "Home", h("span", { text: join([fetched ? `snapshot ${hm(fetched)}` : "no snapshot", home.stale ? "stale" : "", home.period_s ? `every ${Math.round(home.period_s / 60)} min` : ""]) })),
      (home.entities || []).length ? h("div", { class: "rooms" }, order.map((r) => {
        const es = rooms.get(r);
        const temp = es.find((e) => e.attributes && e.attributes.current_temperature !== undefined);
        return h("div", { class: "room" }, h("b", {}, r, temp ? h("span", { text: `${temp.attributes.current_temperature} °C` }) : null),
          es.filter((e) => !(e === temp && domain(e) === "climate" && es.length > 1)).map((e) => h("div", { class: active(e) ? "on" : null, title: e.entity_id },
            h("em", { text: e.name || e.entity_id }), h("span", { text: value(e) }))));
      })) : h("p", { class: "empty", text: home.code ? "No home snapshot yet." : "No entities selected." }));
  }

  // ------------------------------------------------------------------ sections and wiring

  const renderers = {
    overview: (o) => {
      S.overview = o; S.overviewAt = nowMs(); S.tz = o.tz || S.tz;
      $("tzname").textContent = S.tz;
      renderVerdict(); renderHeld(); renderBand();
      if (S.lanesOpen && nowMs() - (S.historyAt || 0) > 60000) loadHistory();
    },
    timeline: onTimeline,
    cost: (c) => { S.cost = c; renderBand(); },
    chats: (c) => { S.chats = c.chats || []; renderFilters(); renderChatsRail(); if (S.loaded) withAnchor(renderFeed); },
    errors: () => {},
    problems: (p) => { S.problems = p; renderBand(); renderProblemsRail(); renderVerdict(); if (S.loaded) withAnchor(renderFeed); scheduleSince(); },
    home: (d) => { S.home = d; renderBand(); renderHomeRail(); },
  };

  function connect() {
    const es = new EventSource(streamUrl());
    es.addEventListener("open", () => { S.connected = true; S.lastMsg = nowMs(); renderClock(); });
    es.addEventListener("error", () => { S.connected = false; renderClock(); });
    es.addEventListener("heartbeat", () => { S.lastMsg = nowMs(); });
    es.addEventListener("section", (ev) => {
      S.lastMsg = nowMs(); S.connected = true;
      const msg = JSON.parse(ev.data);
      const fn = renderers[msg.section];
      if (fn) fn(msg.data);
      document.body.dataset[`seen${msg.section[0].toUpperCase()}${msg.section.slice(1)}`] = "1";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    S.since = readSeen();
    let qTimer = null;
    $("q").addEventListener("input", (ev) => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => { S.filters.q = ev.target.value.trim(); refetch(); }, 300);
    });
    $("f-chat").addEventListener("change", (ev) => { S.filters.chat = ev.target.value; refetch(); renderChatsRail(); });
    $("lifecycle").addEventListener("change", (ev) => { S.lifecycle = ev.target.checked; withAnchor(renderFeed); for (const id of S.openTraces) drawTrace(id); });
    $("older").addEventListener("click", older);
    $("newpill").addEventListener("click", jumpToTop);
    window.addEventListener("hashchange", openFromHash);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) writeSeen();
      else { S.since = readSeen(); loadSince(); }
    });
    window.addEventListener("pagehide", writeSeen);
    if (filtered()) refetch();
    connect();
    loadSince();
    setInterval(() => { renderClock(); tickHeld(); }, 1000);
    setInterval(() => {
      document.querySelectorAll("[data-rel]").forEach((el) => { el.textContent = rel(el.dataset.rel); });
      renderBand(); renderAway();
    }, 30000);
    renderClock();
  });
})();

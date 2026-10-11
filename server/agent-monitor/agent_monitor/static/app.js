"use strict";
(() => {
  const state = { tz: "Europe/Dublin", open: new Set(), filters: { chat: "", state: "", kind: "" }, paged: false,
                  nextBefore: null, groups: [], chats: [] };
  const $ = (id) => document.getElementById(id);
  const MODEL_COLORS = ["#22B5C4", "#C8963C", "#7E9CD8", "#9FB4BC", "#C05A50", "#6E8A92"];

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const svg = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  };
  const fill = (el, ...nodes) => { el.replaceChildren(...nodes.flat().filter(Boolean)); };
  const empty = (text) => h("div", { class: "empty", text });
  const pill = (text, kind) => h("span", { class: `pill pill-${kind || text}`, text });

  function fmtTime(ts, withDate) {
    if (!ts) return "";
    const d = new Date(ts);
    const opts = { timeZone: state.tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
    if (withDate) Object.assign(opts, { month: "short", day: "numeric" });
    return new Intl.DateTimeFormat("en-GB", opts).format(d);
  }
  const fmtAge = (s) => (s === null || s === undefined) ? "" : s < 60 ? `${Math.round(s)} s` :
    s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
  const usd = (v) => v === null || v === undefined ? "–" : v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;
  const num = (v) => v === null || v === undefined ? "–" : Number(v).toLocaleString("en-GB");
  const ms = (v) => v === null || v === undefined ? "" : v >= 10000 ? `${(v / 1000).toFixed(0)} s` :
    v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`;
  const short = (v, n = 90) => !v ? "" : (typeof v === "string" ? v : JSON.stringify(v)).slice(0, n);

  // ------------------------------------------------------------------ overview

  function renderTiles(tiles) {
    fill($("tiles"), tiles.map((t) => h("div", { class: `tile s-${t.state}`, "data-tile": t.id, "data-state": t.state },
      h("div", { class: "row" }, h("span", { class: "dot" }), h("span", { class: "name", text: t.label })),
      h("div", { class: "row" }, h("span", { class: "word", text: t.word }), t.value ? h("span", { class: "dim", text: t.value }) : null),
      h("div", { class: "sub", text: [t.age_s !== null ? fmtAge(t.age_s) + " ago" : "", t.state === "bad" && t.code ? t.code : ""]
        .filter(Boolean).join(" · ") }))));
  }

  function renderMcp(m) {
    const ours = m.pending_ours || [];
    fill($("mcp"),
      h("dl", { class: "kv" },
        h("dt", { text: "Reachable" }), h("dd", {}, pill(m.reachable ? "yes" : "no", m.reachable ? "ok" : "bad")),
        h("dt", { text: "Tools" }), h("dd", { text: num(m.tool_count) }),
        h("dt", { text: "Policy" }), h("dd", { text: `v${m.policy_version ?? "–"} · wait ${m.wait_seconds ?? "–"} s` }),
        h("dt", { text: "Pending (ours)" }), h("dd", { text: String(ours.length) }),
        h("dt", { text: "Pending (not ours)" }), h("dd", { text: m.pending_not_ours === null || m.pending_not_ours === undefined ? "–" : `${m.pending_not_ours} · left for you in HA` }),
        h("dt", { text: "Evidence" }), h("dd", { class: "dim", text: m.probe_age_s !== null ? `${fmtAge(m.probe_age_s)} ago` : "none" })),
      ours.length ? h("div", { class: "list", style: null },
        ours.map((a) => h("div", { class: "item" },
          h("div", { class: "line" }, h("span", { text: a.tool }), pill(a.state, "pending")),
          h("div", { class: "sub", text: [a.target, a.age_s !== null ? `age ${fmtAge(a.age_s)}` : "",
            a.expires_in_s !== null ? `expires in ${fmtAge(Math.max(0, a.expires_in_s))}` : ""].filter(Boolean).join(" · ") })))) : null);
  }

  function renderSummary(o) {
    const c = o.counts || {};
    fill($("summary"), h("div", { class: "stats" },
      h("div", { class: "stat" }, h("div", { class: "label", text: "Cost today" }), h("div", { class: "big", text: usd(c.cost_today_usd) })),
      h("div", { class: "stat" }, h("div", { class: "label", text: "Open tasks" }), h("div", { class: "big", text: num(c.open_tasks) })),
      h("div", { class: "stat" }, h("div", { class: "label", text: "Errors 24 h" }), h("div", { class: "big", text: num(c.errors_24h) }))),
      h("dl", { class: "kv" },
        h("dt", { text: "Turns in flight" }), h("dd", { text: num(o.agent && o.agent.turns_in_flight) }),
        h("dt", { text: "Held HA calls" }), h("dd", { text: num(o.agent && o.agent.open_holds) }),
        h("dt", { text: "Database" }), h("dd", { text: `${o.db.state} · schema ${o.db.schema ?? "–"}` })));
  }

  function renderOverview(o) {
    state.tz = o.tz || state.tz;
    $("tzname").textContent = state.tz;
    const banner = $("banner");
    banner.hidden = !o.db.banner;
    banner.textContent = o.db.banner || "";
    renderTiles(o.tiles);
    renderMcp(o.mcp);
    renderSummary(o);
    $("home-card").hidden = !o.home_enabled;
    $("clock").textContent = fmtTime(o.now);
  }

  // ------------------------------------------------------------------ timeline

  function decision(c) {
    if (!c.approval_decision) return "";
    const who = c.decided_via ? ` by ${c.decided_via}` : "";
    return `${c.approval_decision === "approve" ? "approved" : c.approval_decision === "deny" ? "denied" : c.approval_decision}${who}${c.decided_at ? " " + fmtTime(c.decided_at) : ""}`;
  }

  function events(g) {
    const rows = [];
    for (const t of g.turns) rows.push({ ts: t.created_at, node: h("div", { class: "what" },
      h("span", { class: "chip", text: t.kind }), pill(t.state), " ", t.model ? h("span", { class: "dim", text: t.model }) : null,
      t.terminal_reason ? h("span", { class: "dim", text: ` · ${t.terminal_reason}` }) : null,
      t.ttft_ms ? h("span", { class: "dim", text: ` · ttft ${ms(t.ttft_ms)}` }) : null,
      t.interrupted_by ? h("span", { class: "dim", text: ` · interrupted by ${t.interrupted_by}` }) : null,
      t.text ? h("div", { class: "args", text: `“${t.text}”` }) : null) });
    for (const c of g.tool_calls) rows.push({ ts: c.started_at, node: h("div", { class: "what", "data-tool": c.tool_use_id },
      h("span", { class: "chip", text: c.kind }), h("strong", { text: c.tool }), c.target ? h("span", { class: "dim", text: ` ${c.target}` }) : null,
      " ", pill(c.status), c.duration_ms !== null ? h("span", { class: "dim", text: ` ${ms(c.duration_ms)}` }) : null,
      decision(c) ? h("span", { class: "dim", text: ` · ${decision(c)}` }) : null,
      c.args_summary ? h("div", { class: "args", text: short(c.args_summary, 160) }) : null,
      c.result_summary ? h("div", { class: "res", text: `→ ${short(c.result_summary, 160)}` }) : null) });
    for (const t of g.transitions) rows.push({ ts: t.ts, node: h("div", { class: "what" },
      h("span", { class: "chip", text: "task" }), `${t.from_state || "∅"} → `, pill(t.to_state), t.reason ? h("span", { class: "dim", text: ` ${t.reason}` }) : null) });
    for (const i of g.interactions) rows.push({ ts: i.created_at, node: h("div", { class: "what" },
      h("span", { class: "chip", text: i.kind }), pill(i.state), h("span", { class: "dim", text: ` ${i.source}${i.via ? " · via " + i.via : ""}${i.decided_at ? " " + fmtTime(i.decided_at) : ""}` })) });
    for (const d of g.deliveries) {
      rows.push({ ts: d.created_at, node: h("div", { class: "what" },
        h("span", { class: "chip", text: "delivery" }), `${d.kind} · ${d.tier} `, pill(d.state),
        d.playback ? h("span", { class: "dim", text: ` · ${d.playback}` }) : null,
        d.attempt ? h("span", { class: "dim", text: ` · attempt ${d.attempt}` }) : null) });
      for (const a of d.acks) rows.push({ ts: a.received_at, node: h("div", { class: "what" },
        h("span", { class: "chip", text: "ack" }), pill(a.outcome), a.cut_at_ms !== null ? h("span", { class: "dim", text: ` at ${ms(a.cut_at_ms)}` }) : null,
        a.reason ? h("span", { class: "dim", text: ` · ${a.reason}` }) : null) });
    }
    rows.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
    return rows.map((r) => h("div", { class: "ev" }, h("span", { class: "t", text: fmtTime(r.ts) }), r.node));
  }

  function taskNode(g) {
    const u = g.usage || {};
    const det = h("details", { class: "task", "data-task": g.task_id, open: state.open.has(g.task_id) },
      h("summary", {},
        h("div", { class: "title" }, g.title || g.task_id),
        h("div", { class: "right" }, pill(g.state), " ", h("span", { class: "dim", text: usd(g.cost_usd) })),
        h("div", { class: "facts" },
          h("span", { text: fmtTime(g.created_at, true) }), h("span", { text: g.mode }), g.model ? h("span", { text: g.model }) : null,
          g.duration_ms !== null ? h("span", { text: ms(g.duration_ms) }) : null,
          h("span", { "data-count": "tool_calls", text: `${g.counts.tool_calls} tool calls` }),
          h("span", { text: `${g.counts.turns} turns` }),
          h("span", { "data-count": "tokens", text: `${num(u.input_tokens + u.cache_read_tokens + u.cache_creation_tokens)} in · ${num(u.output_tokens)} out` }),
          u.cache_hit_pct !== null && u.cache_hit_pct !== undefined ? h("span", { text: `cache ${u.cache_hit_pct}%` }) : null)),
      h("div", { class: "events" }, events(g)));
    det.addEventListener("toggle", () => { det.open ? state.open.add(g.task_id) : state.open.delete(g.task_id); });
    return det;
  }

  function renderTimeline() {
    fill($("timeline"), state.groups.length ? state.groups.map(taskNode) : empty("No tasks yet."));
    $("older").hidden = !state.nextBefore;
  }

  const filtered = () => Object.values(state.filters).some(Boolean);

  async function fetchTimeline(before) {
    const q = new URLSearchParams({ limit: "30" });
    for (const [k, v] of Object.entries(state.filters)) if (v) q.set(k, v);
    if (before) q.set("before", before);
    const resp = await fetch(`/api/timeline?${q}`);
    if (!resp.ok) return;
    const data = await resp.json();
    state.groups = before ? state.groups.concat(data.groups) : data.groups;
    state.nextBefore = data.next_before;
    state.paged = Boolean(before);
    renderTimeline();
  }

  function onTimeline(data) {
    if (filtered() || state.paged) { fetchTimeline(); return; }
    state.groups = data.groups;
    state.nextBefore = data.next_before;
    renderTimeline();
  }

  // ------------------------------------------------------------------ cost

  function chart(byDay) {
    const known = Object.fromEntries(byDay.map((d) => [d.day, d]));
    const latest = byDay.length ? byDay[0].day : null;
    const days = [];
    if (latest) {
      const end = new Date(`${latest}T12:00:00Z`);
      for (let i = 29; i >= 0; i--) {
        const day = new Date(end.getTime() - i * 86400000).toISOString().slice(0, 10);
        days.push(known[day] || { day, total: { cost_usd: 0 }, by_model: {} });
      }
    }
    const models = [...new Set(days.flatMap((d) => Object.keys(d.by_model)))];
    const max = Math.max(0.0001, ...days.map((d) => d.total.cost_usd));
    const W = 600, H = 120, bw = W / 30;
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", preserveAspectRatio: "none", role: "img",
                           "aria-label": "Cost per day by model" });
    days.forEach((d, i) => {
      let y = H;
      models.forEach((m, mi) => {
        const v = (d.by_model[m] || {}).cost_usd || 0;
        const hgt = (v / max) * (H - 6);
        if (hgt <= 0) return;
        y -= hgt;
        const r = svg("rect", { x: i * bw + 1, y, width: Math.max(1, bw - 2), height: hgt, fill: MODEL_COLORS[mi % MODEL_COLORS.length] });
        r.append(svg("title"));
        r.firstChild.textContent = `${d.day} ${m} ${usd(v)}`;
        s.append(r);
      });
    });
    const legend = h("div", { class: "legend" }, models.map((m, mi) =>
      h("span", {}, h("i", { style: null, "data-c": mi }), m)));
    legend.querySelectorAll("i").forEach((el) => { el.style.background = MODEL_COLORS[Number(el.dataset.c) % MODEL_COLORS.length]; });
    return [s, legend];
  }

  function table(head, rows) {
    return h("div", { class: "scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, head.map(([label, n]) => h("th", { class: n ? "n" : null, text: label })))),
      h("tbody", {}, rows.length ? rows.map((r) => h("tr", {}, r.map(([v, n]) => h("td", { class: n ? "n" : null, text: v }))))
        : h("tr", {}, h("td", { colspan: head.length, class: "empty", text: "Nothing in this window." })))));
  }

  function renderCost(c) {
    const t = c.totals || {};
    const stat = (label, x) => h("div", { class: "stat" }, h("div", { class: "label", text: label }),
      h("div", { class: "big", text: usd(x && x.cost_usd) }),
      h("div", { class: "dim", text: x ? `${num(x.input_tokens + x.cache_read_tokens + x.cache_creation_tokens)} in · ${num(x.output_tokens)} out` : "" }));
    const tok = (r) => [[num(r.input_tokens), 1], [num(r.output_tokens), 1], [r.cache_hit_pct === null ? "–" : `${r.cache_hit_pct}%`, 1],
      [num(r.web_search_requests + (r.web_fetch_requests || 0)), 1], [usd(r.cost_usd), 1]];
    const head = [["Input", 1], ["Output", 1], ["Cache hit", 1], ["Web", 1], ["USD (30 d)", 1]];
    fill($("cost"),
      h("div", { class: "stats" }, stat("Today", t.today), stat("7 days", t["7d"]), stat("30 days", t["30d"])),
      ...chart(c.by_day || []),
      h("h2", { text: "Per chat" }),
      table([["Chat"], ...head, ["All time", 1]], (c.chats || []).map((r) => [[r.title || r.chat_id], ...tok(r), [usd(r.cost_all_usd), 1]])),
      h("h2", { text: "Per task", style: null }),
      table([["Task"], ["State"], ...head], (c.tasks || []).map((r) => [[r.title || r.task_id], [r.state], ...tok(r)])),
      h("h2", { text: "Per day" }),
      table([["Day"], ...head], (c.by_day || []).map((r) => [[r.day + (r.utc_day ? " (UTC)" : "")], ...tok(r.total)])));
  }

  // ------------------------------------------------------------------ chats, errors, home

  function renderChats(data) {
    state.chats = data.chats;
    const select = $("f-chat");
    const current = select.value;
    fill(select, h("option", { value: "", text: "all" }), data.chats.map((c) => h("option", { value: c.chat_id, text: c.title || c.chat_id })));
    select.value = current;
    fill($("chats"), data.chats.length ? h("div", { class: "list" }, data.chats.map((c) => h("div", { class: "item" },
      h("div", { class: "line" }, h("strong", { text: c.title || c.chat_id }),
        h("span", {}, c.current ? pill("current", "ok") : null, " ", c.unread ? pill("unread", "warn") : null, " ", c.busy ? pill("busy", "running") : null)),
      h("div", { class: "sub", text: [c.mode, c.task_state ? `task ${c.task_state}` : "", c.queued ? `${c.queued} queued` : "",
        `last ${fmtTime(c.last_activity_at, true)}`].filter(Boolean).join(" · ") }),
      c.summary ? h("div", { class: "sub", text: c.summary }) : null))) : empty("No chats yet."));
  }

  function renderErrors(data) {
    fill($("errors"), data.errors.length ? h("div", { class: "list" }, data.errors.map((e) => h("div", { class: "item" },
      h("div", { class: "line" }, h("strong", { text: `${e.source} · ${e.code}` }), h("span", { class: "dim", text: fmtTime(e.ts, true) })),
      e.message ? h("div", { class: "sub", text: e.message }) : null))) : empty("No errors."));
  }

  function renderHome(data) {
    $("home-card").hidden = !data.enabled;
    if (!data.enabled) return;
    if (data.code) { fill($("home"), empty("No home snapshot yet.")); return; }
    fill($("home"),
      h("div", { class: "sub", text: `fetched ${fmtTime(data.fetched_at, true)}${data.stale ? " · stale" : ""}` }),
      table([["Entity"], ["State"], ["Changed"]], data.entities.map((e) => [[e.name || e.entity_id], [e.state], [fmtTime(e.last_changed, true)]])));
  }

  // ------------------------------------------------------------------ wiring

  const renderers = { overview: renderOverview, timeline: onTimeline, cost: renderCost, chats: renderChats,
                      errors: renderErrors, home: renderHome };

  function connect() {
    const live = $("live");
    const es = new EventSource("/api/stream");
    es.addEventListener("open", () => { live.textContent = "live"; live.className = "pill pill-ok"; });
    es.addEventListener("error", () => { live.textContent = "reconnecting"; live.className = "pill pill-warn"; });
    es.addEventListener("section", (ev) => {
      const msg = JSON.parse(ev.data);
      const fn = renderers[msg.section];
      if (fn) fn(msg.data);
      document.body.dataset[`seen${msg.section[0].toUpperCase()}${msg.section.slice(1)}`] = "1";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const key of ["chat", "state", "kind"]) {
      $(`f-${key}`).addEventListener("change", (ev) => { state.filters[key] = ev.target.value; fetchTimeline(); });
    }
    $("older").addEventListener("click", () => { if (state.nextBefore) fetchTimeline(state.nextBefore); });
    connect();
  });
})();

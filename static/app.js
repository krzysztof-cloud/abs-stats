"use strict";
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const rangeSel = $("#range");
let PALETTE = [];
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const METHODS = { 0: "Direct play", 1: "Direct stream", 2: "Transcode", 3: "Local" };
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let charts = [];

try { const r = localStorage.getItem("abs-range"); if (r !== null) rangeSel.value = r; } catch (e) {}

// ------------------------------------------------------------ helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const days = () => rangeSel.value;
const rangeLabel = () => rangeSel.options[rangeSel.selectedIndex].text.toLowerCase();

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
  return r.json();
}

function dur(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
function clock(sec) {
  if (sec == null) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
const pct = (pos, total) => total ? `${Math.round((pos / total) * 100)}% of the book` : "";
// Playback speed = book time covered / real time spent. Seeking breaks this, so out-of-range values are hidden.
function speed(r) {
  const real = r.time_listening, covered = (r.end_position ?? 0) - (r.start_position ?? 0);
  if (!real || real < 60 || covered <= 0) return null;
  const s = covered / real;
  if (s < 0.5 || s > 4) return null;
  return (Math.round(s * 20) / 20).toFixed(2).replace(/0$/, "");
}
const hours = (sec) => (sec || 0) / 3600;
const fmtHours = (sec) => { const h = hours(sec); return h >= 100 ? Math.round(h).toLocaleString() : h.toFixed(1); };
const n = (v) => (v || 0).toLocaleString();

function ago(ms) {
  if (!ms) return "never";
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return "just now";
  const units = [[31536000, "year"], [2592000, "month"], [604800, "week"], [86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [u, name] of units) if (s >= u) { const v = Math.floor(s / u); return `${v} ${name}${v > 1 ? "s" : ""} ago`; }
}
function when(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined })
    + " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
const dateOnly = (ms) => ms ? new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
function bytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i > 2 ? 1 : 0)} ${u[i]}`;
}

const colorFor = (name) => { let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; };
const avatar = (name, cls = "") => `<span class="avatar ${cls}" style="background:${colorFor(name)}">${esc(String(name || "?")[0].toUpperCase())}</span>`;
const userLink = (id, name) => `<a class="userlink" href="#/user/${esc(id)}">${avatar(name, "sm")}${esc(name)}</a>`;

function cover(id, title, pct) {
  const bar = pct != null ? `<span class="bar"><i style="width:${Math.min(100, Math.max(0, pct * 100))}%"></i></span>` : "";
  const ph = `<span class="ph">${esc(title || "")}</span>`;
  if (!id) return `<span class="cover">${ph}${bar}</span>`;
  return `<span class="cover">${ph}<img loading="lazy" alt="" src="/cover/${esc(id)}" onerror="this.remove()">${bar}</span>`;
}
const itemLink = (id, inner) => id ? `<a href="#/item/${esc(id)}">${inner}</a>` : inner;

function destroyCharts() { charts.forEach((c) => c.destroy()); charts = []; }
function loading() { view.innerHTML = `<div class="loading">Loading…</div>`; }
function fail(e) { view.innerHTML = `<div class="empty-state">Couldn't load this page: ${esc(e.message)}</div>`; }

// ------------------------------------------------------------ home
function statCard(title, list, opts) {
  const top = list[0];
  const coverId = top ? opts.cover(top) : null;
  const bg = coverId ? `<div class="bg" style="background-image:url('/cover/${esc(coverId)}')"></div>` : "";
  const poster = opts.poster && top
    ? `<div class="poster">${itemLink(opts.link ? null : coverId, opts.posterHtml ? opts.posterHtml(top) : cover(coverId, opts.name(top)))}</div>` : "";
  const items = list.length
    ? list.map((r, i) => `<li><span class="rank">${i + 1}</span><span class="name">${opts.href ? `<a href="${opts.href(r)}">${esc(opts.name(r))}</a>` : esc(opts.name(r))}</span><span class="val num">${opts.val(r)}</span></li>`).join("")
    : `<li class="empty">Nothing in ${rangeLabel()}.</li>`;
  return `<div class="stat-card">${bg}<div class="inner">${poster}<div style="flex:1;min-width:0"><h3>${title}</h3><ol>${items}</ol></div></div></div>`;
}

async function pageHome() {
  const [sum, h] = await Promise.all([api(`/api/summary?days=${days()}`), api(`/api/home?days=${days()}`)]);
  const lib = sum.library;
  const titleOpts = (val) => ({ poster: true, cover: (r) => r.id, name: (r) => r.title, href: (r) => `#/item/${r.id}`, val });
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <div class="summary">
        <div><b class="num">${fmtHours(sum.seconds)}</b><span>hours listened</span></div>
        <div><b class="num">${n(sum.sessions)}</b><span>sessions</span></div>
        <div><b class="num">${n(sum.titles)}</b><span>titles played</span></div>
        <div><b class="num">${n(sum.finished)}</b><span>books finished</span></div>
        <div><b class="num">${n(sum.listeners)}</b><span>active listeners</span></div>
        <div><b class="num">${n(lib.items)}</b><span>in library, ${n(Math.round(hours(lib.duration)))} hours</span></div>
      </div>
    </section>
    <section>
      <h2>Listening statistics</h2>
      <div class="stat-grid">
        ${statCard("Most listened", h.top_titles, titleOpts((r) => dur(r.seconds)))}
        ${statCard("Most popular", h.popular, titleOpts((r) => `${r.listeners} listener${r.listeners > 1 ? "s" : ""}`))}
        ${statCard("Top authors", h.top_authors, { poster: true, cover: (r) => r.cover_id, name: (r) => r.name, val: (r) => dur(r.seconds) })}
        ${statCard("Most active listeners", h.top_users, { poster: true, cover: () => h.top_titles[0]?.id, name: (r) => r.name,
            posterHtml: (r) => `<a href="#/user/${esc(r.id)}">${avatar(r.name)}</a>`, link: true, href: (r) => `#/user/${r.id}`, val: (r) => dur(r.seconds) })}
        ${statCard("Top clients", h.top_clients, { cover: () => null, name: (r) => r.name, val: (r) => dur(r.seconds) })}
        ${statCard("Top genres", h.top_genres, { cover: () => null, name: (r) => r.name, val: (r) => dur(r.seconds) })}
      </div>
    </section>
    <section>
      <h2>Recent activity</h2>
      ${h.recent_sessions.length ? `<div class="activity">${h.recent_sessions.map((s) => `
        <div class="act">
          ${itemLink(s.item_id, cover(s.item_id, s.title, s.duration ? s.end_position / s.duration : null))}
          <div class="meta">
            <div class="t">${itemLink(s.item_id, esc(s.title))}</div>
            <div class="s">${esc(s.author || "")}</div>
            <div class="s">${userLink(s.user_id, s.username)} &nbsp;${dur(s.time_listening)} on ${esc(s.client || "unknown client")}</div>
            <div class="s">${ago(s.started_at)}</div>
          </div>
        </div>`).join("")}</div>` : `<div class="empty-state">No listening sessions yet. They appear here after the next sync.</div>`}
    </section>
    <section>
      <h2>Recently added</h2>
      ${shelf(h.recently_added, (i) => ({ id: i.id, t: i.title, s: i.author, s2: ago(i.added_at) }))}
    </section>
    <section>
      <h2>Recently finished</h2>
      ${shelf(h.recently_finished, (i) => ({ id: i.item_id, t: i.title, s: i.username, s2: ago(i.finished_at) }), "No finished books yet.")}
    </section>`;
}

function shelf(list, map, emptyText = "Nothing here yet.") {
  if (!list.length) return `<div class="empty-state">${emptyText}</div>`;
  return `<div class="shelf">${list.map((x) => { const i = map(x); return `
    <div class="card">${itemLink(i.id, cover(i.id, i.t, i.pct))}
      <div class="t">${itemLink(i.id, esc(i.t))}</div>
      <div class="s">${esc(i.s || "")}</div>${i.s2 ? `<div class="s">${esc(i.s2)}</div>` : ""}
    </div>`; }).join("")}</div>`;
}

// ------------------------------------------------------------ history
function historyTable(rows, { showUser = true, showTitle = true } = {}) {
  if (!rows.length) return `<div class="empty-state">No sessions match.</div>`;
  return `<div class="table-wrap"><table>
    <thead><tr><th>Started</th>${showUser ? "<th>User</th>" : ""}${showTitle ? "<th>Title</th>" : ""}<th>Client</th><th>Device</th><th>Method</th><th>Listened</th><th>Starting time</th><th>Ending time</th><th>Speed</th></tr></thead>
    <tbody>${rows.map((r) => {
      const sp = speed(r);
      return `<tr>
        <td class="num" title="${esc(new Date(r.started_at).toString())}">${when(r.started_at)}</td>
        ${showUser ? `<td>${userLink(r.user_id, r.username)}</td>` : ""}
        ${showTitle ? `<td class="title">${itemLink(r.item_id, cover(r.item_id, ""))}<div><div class="t">${itemLink(r.item_id, esc(r.title))}</div><div class="s">${esc(r.author || "")}</div></div></td>` : ""}
        <td>${esc(r.client || "Unknown")}</td>
        <td class="muted">${esc(r.device || "")}</td>
        <td class="muted">${METHODS[r.play_method] ?? ""}</td>
        <td class="num">${dur(r.time_listening)}</td>
        <td class="num" title="${pct(r.start_position, r.duration)}">${clock(r.start_position)}</td>
        <td class="num" title="${pct(r.end_position, r.duration)}">${clock(r.end_position)}</td>
        ${sp ? `<td class="num speed">${sp}x</td>` : `<td class="speed na" title="Can't calculate: the session was very short, or the listener skipped or rewound during it.">n/a</td>`}
      </tr>`; }).join("")}</tbody></table></div>`;
}

async function pageHistory(params) {
  const users = await api("/api/users");
  const state = { search: params.get("search") || "", user: params.get("user") || "", page: 1, per: 50 };
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <h1>History</h1>
      <div class="toolbar" style="margin-top:1rem">
        <input id="h-search" type="search" placeholder="Search title, author, user or client" value="${esc(state.search)}">
        <select id="h-user"><option value="">All users</option>${users.map((u) => `<option value="${esc(u.id)}" ${u.id === state.user ? "selected" : ""}>${esc(u.username)}</option>`).join("")}</select>
        <select id="h-per">${[25, 50, 100, 250].map((p) => `<option ${p === 50 ? "selected" : ""}>${p}</option>`).join("")}</select>
        <span class="info" id="h-info"></span>
      </div>
      <div id="h-table"></div>
      <div class="pager" id="h-pager"></div>
    </section>`;
  const load = async () => {
    const qs = new URLSearchParams({ days: days(), page: state.page, per: state.per, search: state.search, user: state.user });
    const d = await api(`/api/history?${qs}`);
    $("#h-table").innerHTML = historyTable(d.rows);
    $("#h-info").textContent = `${n(d.total)} sessions, ${dur(d.seconds)} in ${rangeLabel()}`;
    const pages = Math.max(1, Math.ceil(d.total / d.per));
    $("#h-pager").innerHTML = `<button id="h-prev" ${state.page <= 1 ? "disabled" : ""}>Previous</button><span>Page ${state.page} of ${pages}</span><button id="h-next" ${state.page >= pages ? "disabled" : ""}>Next</button>`;
    $("#h-prev").onclick = () => { state.page--; load(); };
    $("#h-next").onclick = () => { state.page++; load(); };
  };
  let t;
  $("#h-search").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { state.search = e.target.value; state.page = 1; load(); }, 300); };
  $("#h-user").onchange = (e) => { state.user = e.target.value; state.page = 1; load(); };
  $("#h-per").onchange = (e) => { state.per = +e.target.value; state.page = 1; load(); };
  await load();
}

// ------------------------------------------------------------ graphs
Chart.defaults.font.family = "Figtree, system-ui, sans-serif";
const hoursTick = { callback: (v) => `${v}h` };
const tooltipHours = { callbacks: { label: (c) => `${c.dataset.label ? c.dataset.label + ": " : ""}${dur(c.raw * 3600)}` } };

function stackedBar(el, data, labelFmt) {
  return new Chart(el, {
    type: "bar",
    data: { labels: data.labels.map(labelFmt), datasets: data.series.map((s, i) => ({ label: s.name, data: s.data.map(hours), backgroundColor: PALETTE[i % PALETTE.length], borderRadius: 2, maxBarThickness: 40 })) },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10 } }, tooltip: tooltipHours },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 14 } }, y: { stacked: true, ticks: hoursTick } } },
  });
}
function simpleBar(el, labels, values, horizontal = false) {
  return new Chart(el, {
    type: "bar",
    data: { labels, datasets: [{ data: values.map(hours), backgroundColor: PALETTE[0], borderRadius: 2, maxBarThickness: 36 }] },
    options: { maintainAspectRatio: false, indexAxis: horizontal ? "y" : "x", plugins: { legend: { display: false }, tooltip: tooltipHours },
      scales: horizontal ? { x: { ticks: hoursTick }, y: { grid: { display: false } } } : { x: { grid: { display: false } }, y: { ticks: hoursTick } } },
  });
}
function donut(el, rows, nameFn = (r) => r.name) {
  return new Chart(el, {
    type: "doughnut",
    data: { labels: rows.map(nameFn), datasets: [{ data: rows.map((r) => hours(r.seconds)), backgroundColor: PALETTE, borderColor: css("--surface"), borderWidth: 2 }] },
    options: { maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "right", labels: { boxWidth: 10 } }, tooltip: tooltipHours } },
  });
}
const monthLabel = (m) => { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleDateString(undefined, { month: "short", year: "2-digit" }); };
const hourLabel = (s) => { const [d, h] = s.split(" "); const [y, m, dd] = d.split("-"); return new Date(+y, +m - 1, +dd, +h).toLocaleString(undefined, { weekday: "short", hour: "numeric" }); };
const dayLabel = (d) => { const [y, m, dd] = d.split("-"); return new Date(+y, +m - 1, +dd).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };

async function pageGraphs() {
  const g = await api(`/api/graphs?days=${days()}`);
  const box = (id, title, wide) => `<div class="chart ${wide ? "wide" : ""}"><h3>${title}</h3><div class="box"><canvas id="${id}"></canvas></div></div>`;
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <h1>Graphs</h1>
      <p class="muted">Hours listened in ${rangeLabel()}, unless noted.</p>
      <div class="charts">
        ${box("g-time", { month: "Hours by month", day: "Hours by day", hour: "Hours by hour" }[g.bucket], true)}
        ${box("g-dow", "By day of week")}
        ${box("g-hour", "By hour of day")}
        ${box("g-clients", "By client")}
        ${box("g-devices", "By device")}
        ${box("g-genres", "Top genres")}
        ${box("g-methods", "By play method")}
        ${box("g-months", "Last 12 months, by user", true)}
      </div>
    </section>`;
  charts.push(stackedBar($("#g-time"), g.timeline, { month: monthLabel, day: dayLabel, hour: hourLabel }[g.bucket]));
  charts.push(simpleBar($("#g-dow"), DOW, g.dow));
  charts.push(simpleBar($("#g-hour"), g.hour.map((_, i) => (i % 12 || 12) + (i < 12 ? "a" : "p")), g.hour));
  charts.push(donut($("#g-clients"), g.clients));
  charts.push(donut($("#g-devices"), g.devices));
  charts.push(simpleBar($("#g-genres"), g.genres.map((r) => r.name), g.genres.map((r) => r.seconds), true));
  charts.push(donut($("#g-methods"), g.methods, (r) => METHODS[r.name] ?? "Unknown"));
  charts.push(stackedBar($("#g-months"), g.months, monthLabel));
}

// ------------------------------------------------------------ users
async function pageUsers() {
  const users = await api("/api/users");
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <h1>Users</h1>
      <div class="table-wrap" style="margin-top:1rem"><table>
        <thead><tr><th>User</th><th>Last seen</th><th>Last listened to</th><th>Client</th><th>Sessions</th><th>Total time</th><th>Finished</th></tr></thead>
        <tbody>${users.map((u) => `<tr>
          <td>${userLink(u.id, u.username)} ${u.type !== "user" ? `<span class="tag">${esc(u.type)}</span>` : ""}</td>
          <td class="muted" title="${esc(when(u.last_seen))}">${ago(u.last_seen)}</td>
          <td>${u.last_item ? itemLink(u.last_item, esc(u.last_title)) : `<span class="muted">Nothing yet</span>`}</td>
          <td class="muted">${esc(u.last_client || "")}</td>
          <td class="num">${n(u.sessions)}</td>
          <td class="num">${dur(u.seconds)}</td>
          <td class="num">${n(u.finished)}</td></tr>`).join("")}</tbody>
      </table></div>
    </section>`;
}

async function pageUser(id) {
  const [u, hist] = await Promise.all([api(`/api/user/${id}`), api(`/api/history?days=0&per=25&user=${encodeURIComponent(id)}`)]);
  const bgId = u.in_progress[0]?.item_id || u.finished[0]?.item_id;
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <div class="hero">
        ${bgId ? `<div class="bg" style="background-image:url('/cover/${esc(bgId)}')"></div>` : ""}
        ${avatar(u.username)}
        <div>
          <h1>${esc(u.username)}</h1>
          <div class="facts">
            <span><b>${esc(u.type)}</b> account</span>
            <span>Last seen <b>${ago(u.last_seen)}</b></span>
            <span>Joined <b>${dateOnly(u.created_at)}</b></span>
            <span><b>${n(u.finished_count)}</b> books finished</span>
          </div>
        </div>
      </div>
      <div class="windows">${u.windows.map((w) => `<div><b class="num">${dur(w.seconds)}</b><span>${w.label}, ${n(w.sessions)} sessions</span></div>`).join("")}</div>
    </section>
    <section>
      <h2>Currently listening</h2>
      ${shelf(u.in_progress, (i) => ({ id: i.item_id, t: i.title, s: i.author, s2: `${Math.round((i.progress || 0) * 100)}%, ${ago(i.last_update)}`, pct: i.progress }), "No books in progress.")}
    </section>
    <section class="cols">
      <div><h2>Recently finished</h2>${shelf(u.finished, (i) => ({ id: i.item_id, t: i.title, s: i.author, s2: dateOnly(i.finished_at) }), "No finished books yet.")}</div>
      <div><h2>Clients</h2>${u.clients.length ? `<div class="table-wrap"><table><tbody>${u.clients.map((c) => `<tr><td>${esc(c.name)}</td><td class="num muted">${n(c.sessions)} sessions</td><td class="num">${dur(c.seconds)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty-state">No sessions yet.</div>`}</div>
    </section>
    <section>
      <h2>Recent history</h2>
      ${historyTable(hist.rows, { showUser: false })}
      ${hist.total > 25 ? `<div class="pager"><a href="#/history?user=${esc(id)}">See all ${n(hist.total)} sessions</a></div>` : ""}
    </section>`;
}

// ------------------------------------------------------------ item
async function pageItem(id) {
  const [it, hist] = await Promise.all([api(`/api/item/${id}`), api(`/api/history?days=0&per=50&item=${encodeURIComponent(id)}`)]);
  const st = it.stats;
  const facts = [
    it.narrator && `Narrated by <b>${esc(it.narrator)}</b>`,
    it.series && `<b>${esc(it.series)}</b>`,
    it.duration && `<b>${dur(it.duration)}</b> long`,
    it.num_chapters && `<b>${n(it.num_chapters)}</b> chapters`,
    it.published_year && `Published <b>${esc(it.published_year)}</b>`,
    it.library && `In <b>${esc(it.library)}</b>`,
    it.added_at && `Added <b>${dateOnly(it.added_at)}</b>`,
    it.size && `<b>${bytes(it.size)}</b>`,
  ].filter(Boolean);
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <div class="hero">
        <div class="bg" style="background-image:url('/cover/${esc(id)}')"></div>
        ${cover(id, it.title)}
        <div>
          <h1>${esc(it.title)}</h1>
          ${it.subtitle ? `<div class="muted">${esc(it.subtitle)}</div>` : ""}
          <div style="margin-top:.35rem;font-size:1.05rem">${esc(it.author || "")}</div>
          <div class="facts">${facts.map((f) => `<span>${f}</span>`).join("")}</div>
          <div style="margin-top:.75rem">${(it.genres || []).map((g) => `<span class="tag">${esc(g)}</span>`).join("")}${it.removed ? `<span class="tag">No longer in library</span>` : ""}</div>
        </div>
      </div>
      <div class="windows">
        <div><b class="num">${dur(st.seconds)}</b><span>total listening</span></div>
        <div><b class="num">${n(st.sessions)}</b><span>sessions</span></div>
        <div><b class="num">${n(st.listeners)}</b><span>listeners</span></div>
        <div><b>${st.last_played ? ago(st.last_played) : "never"}</b><span>last played</span></div>
      </div>
    </section>
    <section>
      <h2>Listeners</h2>
      ${it.listeners.length ? `<div class="table-wrap"><table>
        <thead><tr><th>User</th><th>Progress</th><th></th><th>Time listened</th><th>Last update</th></tr></thead>
        <tbody>${it.listeners.map((l) => `<tr>
          <td>${userLink(l.user_id, l.username)}</td>
          <td><div class="progress"><i style="left:0;width:${(l.is_finished ? 1 : l.progress || 0) * 100}%"></i></div></td>
          <td class="num">${l.is_finished ? `Finished ${dateOnly(l.finished_at)}` : `${Math.round((l.progress || 0) * 100)}%`}</td>
          <td class="num">${dur(l.seconds)}</td>
          <td class="muted">${ago(l.last_update)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty-state">Nobody has started this yet.</div>`}
    </section>
    <section>
      <h2>History</h2>
      ${historyTable(hist.rows, { showTitle: false })}
    </section>`;
}

// ------------------------------------------------------------ libraries
async function pageLibraries() {
  const libs = await api("/api/libraries");
  const recents = await Promise.all(libs.map((l) => api(`/api/library/${l.id}`)));
  view.innerHTML = `
    <section style="margin-top:.5rem">
      <h1>Libraries</h1>
      ${libs.length ? `<div class="libs" style="margin-top:1rem">${libs.map((l, i) => `
        <div class="lib">
          <h2 style="margin:0">${esc(l.name)}</h2>
          <div class="muted" style="font-size:.85rem">${l.media_type === "podcast" ? "Podcasts" : "Books"}, last added ${ago(l.last_added)}</div>
          <div class="fan">${recents[i].slice(0, 6).map((r) => itemLink(r.id, cover(r.id, r.title))).join("")}</div>
          <dl>
            <div><dt>Items</dt><dd class="num">${n(l.items)}</dd></div>
            <div><dt>Authors</dt><dd class="num">${n(l.authors)}</dd></div>
            <div><dt>Runtime</dt><dd class="num">${dur(l.duration)}</dd></div>
            <div><dt>Size</dt><dd class="num">${bytes(l.size)}</dd></div>
            <div><dt>Listened</dt><dd class="num">${dur(l.seconds)}</dd></div>
            <div><dt>Last played</dt><dd>${l.last_played ? ago(l.last_played) : "never"}</dd></div>
          </dl>
        </div>`).join("")}</div>` : `<div class="empty-state">No libraries yet. They appear after the first sync.</div>`}
    </section>`;
}

// ------------------------------------------------------------ router + status
const ROUTES = { "": pageHome, history: pageHistory, graphs: pageGraphs, users: pageUsers, libraries: pageLibraries, user: pageUser, item: pageItem };

async function route() {
  destroyCharts();
  const hash = location.hash.replace(/^#\/?/, "");
  const [path, query] = hash.split("?");
  const [name, id] = path.split("/");
  const fn = ROUTES[name] || pageHome;
  const active = { user: "users", item: "none" }[name] ?? name;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", (a.dataset.route === "home" ? "" : a.dataset.route) === active));
  loading();
  try {
    if (id !== undefined) await fn(decodeURIComponent(id));
    else await fn(new URLSearchParams(query || ""));
  } catch (e) { fail(e); }
  window.scrollTo(0, 0);
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const el = $("#status");
    el.classList.toggle("err", !!s.last_error);
    el.textContent = s.syncing ? "Syncing with Audiobookshelf…"
      : s.last_error ? `Last sync failed: ${s.last_error}. Retrying every minute.`
      : s.last_sync ? `Last synced ${ago(s.last_sync)}` : "Waiting for first sync…";
    $("#sync").disabled = s.syncing;
    return s;
  } catch (e) { return null; }
}

$("#sync").onclick = async () => {
  $("#sync").disabled = true;
  await fetch("/api/sync", { method: "POST" });
  const before = (await refreshStatus())?.last_sync;
  const poll = setInterval(async () => {
    const s = await refreshStatus();
    if (s && !s.syncing && s.last_sync !== before) { clearInterval(poll); route(); }
  }, 2000);
  setTimeout(() => clearInterval(poll), 120000);
};
// ------------------------------------------------------------ theme + logo
function logoSvg(c) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
    <circle cx="256" cy="256" r="244" style="fill:${c.ring};stroke:${c.edge};stroke-width:12"/>
    <g transform="translate(256 262) scale(.86) translate(-256 -256)">
      <path d="M120 300 V250 a136 136 0 0 1 272 0 V300" style="fill:none;stroke:${c.fg};stroke-width:28;stroke-linecap:round"/>
      <rect x="92" y="276" width="64" height="120" rx="28" style="fill:${c.fg}"/>
      <rect x="356" y="276" width="64" height="120" rx="28" style="fill:${c.fg}"/>
      <rect x="180" y="306" width="40" height="90" rx="7" style="fill:${c.b1}"/>
      <rect x="236" y="256" width="40" height="140" rx="7" style="fill:${c.b2}"/>
      <rect x="292" y="218" width="40" height="178" rx="7" style="fill:${c.b2}"/>
      <rect x="180" y="326" width="40" height="8" style="fill:${c.ring};opacity:.35"/>
      <rect x="236" y="276" width="40" height="8" style="fill:${c.ring};opacity:.35"/>
      <rect x="292" y="238" width="40" height="8" style="fill:${c.ring};opacity:.35"/>
    </g></svg>`;
}
function applyTheme() {
  PALETTE = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6", "--c7", "--c8"].map(css);
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--hair");
  $("#brand-logo").innerHTML = logoSvg({ ring: "var(--surface)", edge: "var(--line)", fg: "var(--text)", b1: "var(--c2)", b2: "var(--gold)" });
  const fav = logoSvg({ ring: css("--surface"), edge: css("--line"), fg: css("--text"), b1: css("--c2"), b2: css("--gold") });
  const link = $("#favicon");
  link.type = "image/svg+xml";
  link.href = "data:image/svg+xml," + encodeURIComponent(fav);
  const meta = $('meta[name="theme-color"]') || document.head.appendChild(Object.assign(document.createElement("meta"), { name: "theme-color" }));
  meta.content = css("--bg");
}
const themeSel = $("#theme");
themeSel.innerHTML = absTheme.groups.map((g) => `<optgroup label="${esc(g.group)}">${g.items.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</optgroup>`).join("");
themeSel.value = absTheme.choice;
themeSel.onchange = () => { absTheme.set(themeSel.value); applyTheme(); route(); };
try { matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (["system", "seasonal"].includes(absTheme.choice)) { absTheme.set(absTheme.choice); applyTheme(); route(); } }); } catch (e) {}
applyTheme();

rangeSel.onchange = () => { try { localStorage.setItem("abs-range", rangeSel.value); } catch (e) {} route(); };
window.addEventListener("hashchange", route);
setInterval(refreshStatus, 60000);
refreshStatus();
route();

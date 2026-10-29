"use strict";
const $ = (id) => document.getElementById(id);
let latestStatus = null, latestHistory = null;
let historyDirty = true, lastInsights = 0, previousState = null, detailRequest = 0, selectedIncident = null;
const labels = {online: "Online", degraded: "Degraded", offline: "Unreachable", unknown: "Unknown", gap: "Monitoring gap"};
const formatTime = (ts) => new Date(ts * 1000).toLocaleString();
const ms = (value) => Number.isFinite(value) ? `${value.toFixed(1)} ms` : "—";
const duration = (seconds) => {
  if (!Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};
function text(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}
function badge(state, label) { return text("span", label || labels[state] || state, `badge badge-${state}`); }
function emptyRow(body, message, columns = 4) {
  const row = document.createElement("tr"), cell = text("td", message);
  cell.colSpan = columns;
  row.append(cell);
  body.replaceChildren(row);
}
async function api(path, body) {
  const response = await fetch(path, {
    cache: "no-store", signal: AbortSignal.timeout(15000),
    ...(body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-Network-Monitor": "1"}, body: JSON.stringify(body)})
  });
  if (!response.ok) throw new Error(`Request failed (${response.status}): ${(await response.text()).slice(0, 180)}`);
  return response.json();
}
function navigate(view, focus = false) {
  const allowed = ["overview", "trends", "incidents", "devices", "sources", "settings"];
  if (!allowed.includes(view)) view = "overview";
  for (const name of allowed) $("view-" + name).hidden = name !== view;
  for (const button of document.querySelectorAll("[data-view]")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
  if (focus) document.querySelector(`#view-${view} h2`)?.focus({preventScroll: true});
}
function renderStatus(data) {
  latestStatus = data;
  const captions = {online: "All configured checks passed", degraded: "Some checks did not pass", offline: "External checks failed", unknown: "Fresh evidence unavailable"};
  const details = {
    online: "All configured checks passed at the last observation.",
    degraded: "At least one external check passed; another configured check failed or encountered a monitor error.",
    offline: "All external TCP checks failed. This suggests an interruption, but does not prove its cause.",
    unknown: "Fresh, usable external evidence is unavailable. Check observation age and probe errors; this period is not online."
  };
  $("current-title").textContent = labels[data.state];
  $("status-dot").className = `dot ${data.state}`;
  $("status-caption").textContent = captions[data.state];
  $("status-detail").textContent = details[data.state];
  $("current-latency").textContent = data.stale ? "—" : ms(data.last?.latency_ms);
  $("last-check").textContent = data.last ? `Checked ${duration(data.age_seconds)} ago${data.stale ? " · stale / previous run" : ""}` : "No observations yet";
  $("cadence").textContent = `Target ${data.interval_seconds}s · last cycle ${data.last?.cycle_seconds?.toFixed(1) ?? "—"}s`;
  $("sampling-note").textContent = `Probing every ${data.interval_seconds}s (target); dashboard refreshes every 2s. ${data.interval_seconds > 2 ? "For faster detection, set interval_seconds: 2 and timeout_seconds: 1 in app Configuration, then restart. " : ""}Failures shorter than a sampling cycle can be missed. TCP checks are not bandwidth or ICMP packet loss.`;
  $("retention").textContent = `Up to ${data.retention_days} days or ${data.max_samples.toLocaleString()} samples; the earlier limit wins. Pruning every 60 samples. Per-probe timeout ${data.timeout_seconds}s.`;
  $("footer-note").textContent = data.stale ? "No fresh observation" : `Probes ${data.interval_seconds}s · UI 2s · ${formatTime(data.last.ts)}`;
  const probes = $("probes");
  probes.replaceChildren();
  for (const probe of data.last?.probes || []) {
    const row = document.createElement("tr"), outcome = document.createElement("td");
    const state = probe.outcome === "ok" ? "online" : probe.outcome === "fail" ? "offline" : "unknown";
    outcome.append(badge(state, `${data.stale ? "Previous: " : ""}${probe.outcome === "ok" ? "Passed" : probe.outcome === "fail" ? "Failed" : "Monitor error"}`));
    row.append(text("td", `${probe.kind}: ${probe.name}`), outcome, text("td", ms(probe.latency_ms)), text("td", probe.detail || "Connection accepted"));
    probes.append(row);
  }
  if (!probes.children.length) emptyRow(probes, "No observations yet.");
  const alerts = data.alerts;
  const output = alerts.output === "local" ? "Pi / host speaker" : alerts.output === "both" ? "Host speaker + Cast" : alerts.output === "none" ? "No sound" : "Google Cast";
  $("sound-status").textContent = alerts.enabled ?
    `${alerts.muted ? "Muted" : "Enabled"} · ${output}${["cast", "both"].includes(alerts.output) ? " · " + alerts.target : ""}${alerts.phone_service ? " · " + alerts.phone_service : ""} · down after ${alerts.failure_seconds}s / recovery after ${alerts.recovery_seconds}s.` :
    "Disabled. Choose sound_output and/or phone_notify_service, then enable alerts in app Configuration.";
  $("mute").disabled = !alerts.enabled;
  $("mute").textContent = alerts.muted ? "Unmute alerts" : "Mute alerts";
  $("test-sound").disabled = !alerts.enabled || alerts.muted;
  $("sound-error").hidden = !alerts.last_error;
  $("sound-error").textContent = alerts.last_error || "";
  if (data.monitor_error) throw new Error(data.monitor_error);
}
function svg(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}
function inspectBucket(index) {
  const bucket = latestHistory?.buckets[index];
  if (!bucket) return;
  $("bucket-selector").value = String(index);
  $("bucket-detail").textContent = `${formatTime(bucket.start)} – ${formatTime(bucket.end)} · ${bucket.count} samples · TCP mean ${ms(bucket.latency_ms)}, max ${ms(bucket.max_latency_ms)} · ${bucket.offline} unreachable, ${bucket.degraded} degraded, ${bucket.unknown} unknown.`;
}
function renderHistory(data) {
  latestHistory = data;
  const chart = $("chart");
  for (const node of [...chart.children]) if (!["title", "desc"].includes(node.tagName)) node.remove();
  for (const y of [25, 70, 115, 160]) chart.append(svg("line", {x1: 0, x2: 960, y1: y, y2: y, class: "chart-grid"}));
  const span = data.to - data.from;
  const x = (ts) => Math.max(0, Math.min(960, (ts - data.from) / span * 960));
  const ceiling = Math.max(10, ...data.buckets.map(b => b.max_latency_ms || 0));
  for (const bucket of data.buckets) {
    const left = x(data.from + bucket.bucket * data.bucket_seconds);
    const width = Math.max(1, Math.min(960 - left, data.bucket_seconds / span * 960 - .5));
    const state = bucket.offline ? "offline" : bucket.unknown ? "unknown" : bucket.degraded ? "degraded" : "online";
    chart.append(svg("rect", {x: left, y: 180, width, height: 8, class: `chart-${state}`}));
    if (bucket.latency_ms !== null) chart.append(svg("line", {x1: left + width / 2, x2: left + width / 2, y1: 160, y2: 160 - (bucket.latency_ms / ceiling * 140), class: "latency-bar"}));
  }
  for (const interval of data.intervals.filter(i => i.state === "gap")) chart.append(svg("rect", {
    x: x(interval.start), y: 0, width: Math.max(1, x(interval.end) - x(interval.start)), height: 190, class: "gap-overlay"
  }));
  const count = data.buckets.reduce((sum, b) => sum + b.count, 0);
  if (!count) {
    const message = svg("text", {x: 480, y: 90, "text-anchor": "middle", class: "chart-empty"});
    message.textContent = "No observations in this period";
    chart.append(message);
  }
  $("chart-scale").textContent = `TCP · 0–${Math.ceil(ceiling)} ms`;
  $("chart-summary").textContent = `${count.toLocaleString()} samples · ${Math.ceil(data.bucket_seconds)}s buckets`;
  $("chart-description").textContent = `${count} samples. Latency bars show the mean per bucket; status shows the worst sampled state. Blank space has no samples. Use Inspect timeline to explore each bucket.`;
  $("chart-start").textContent = formatTime(data.from);
  $("chart-end").textContent = formatTime(data.to);
  const selector = $("bucket-selector");
  selector.max = String(Math.max(0, data.buckets.length - 1));
  selector.disabled = !count;
  if (count) inspectBucket(Math.min(Number(selector.value), data.buckets.length - 1));
  else $("bucket-detail").textContent = "No samples in this period.";
  renderIncidents();
  renderEvents($("events"), data.events);
  for (const kind of ["intervals", "samples"]) $("export-" + kind).href = `api/export.csv?kind=${kind}&from=${data.from}&to=${data.to}`;
}
function incidentRow(item) {
  const row = document.createElement("button");
  row.className = "incident-row";
  row.setAttribute("aria-label", `${labels[item.state]}, ${formatTime(item.start)}, ${duration(item.duration_seconds)}. Show details`);
  const time = text("time", formatTime(item.start));
  time.dateTime = new Date(item.start * 1000).toISOString();
  row.append(badge(item.state), time, text("span", duration(item.duration_seconds), "row-duration"), text("span", "›", "row-arrow"));
  row.addEventListener("click", () => openIncident(item.id));
  return row;
}
function renderIncidents() {
  if (!latestHistory) return;
  const all = latestHistory.intervals.filter(i => i.state !== "online").reverse();
  const recent = $("recent-incidents");
  recent.replaceChildren();
  for (const item of all.slice(0, 3)) recent.append(incidentRow(item));
  if (!recent.children.length) recent.append(text("p", latestHistory.buckets.length ? "No recorded interruptions in this period." : "No history yet. New observations appear automatically.", "subtle"));
  const filter = $("incident-filter").value;
  const filtered = all.filter(i => filter === "all" || (filter === "unknown" ? ["gap", "unknown"].includes(i.state) : i.state === filter));
  const body = $("all-incidents");
  body.replaceChildren();
  for (const item of filtered.slice(0, 100)) body.append(incidentRow(item));
  if (!body.children.length) body.append(text("p", "No matching incidents in this period.", "subtle"));
  $("incident-note").textContent = `Showing ${Math.min(filtered.length, 100)} of ${filtered.length} matching intervals. Export CSV for all retained intervals in this period.`;
}
function metric(list, label, value) {
  const item = document.createElement("div");
  item.append(text("dt", label), text("dd", value));
  list.append(item);
}
function renderInsights(data) {
  const s = data.current;
  $("outage-count").textContent = s.samples ? String(s.interruptions) : "—";
  $("downtime").textContent = s.samples ? duration(s.seconds.offline) : "—";
  $("coverage").textContent = `${s.coverage_percent.toFixed(1)}% observed coverage`;
  $("outage-comparison").textContent = data.comparable ? `${s.interruptions - data.previous.interruptions >= 0 ? "+" : ""}${s.interruptions - data.previous.interruptions} vs previous period` : "Comparison needs more coverage";
  const metrics = $("trend-metrics");
  metrics.replaceChildren();
  for (const [label, value] of [
    ["Mean TCP latency", ms(s.mean_ms)], ["95th percentile", ms(s.p95_ms)],
    ["Highest TCP latency", ms(s.max_ms)], ["Longest interruption", s.samples ? duration(s.longest_interruption_seconds) : "—"],
    ["Observed downtime", s.samples ? duration(s.seconds.offline) : "—"], ["Degraded time", s.samples ? duration(s.seconds.degraded) : "—"],
    ["Unknown / unobserved", duration(s.seconds.unknown + s.seconds.gap)], ["Observed coverage", `${s.coverage_percent.toFixed(1)}%`]
  ]) metric(metrics, label, value);
  $("comparison-label").textContent = data.previous ? `${data.previous.coverage_percent.toFixed(1)}% previous coverage` : "Not available";
  if (data.comparable) {
    const old = data.previous;
    const latency = s.mean_ms !== null && old.mean_ms !== null ? ` Mean TCP ${ms(s.mean_ms)} vs ${ms(old.mean_ms)}.` : "";
    $("comparison").textContent = `${s.interruptions} interruptions vs ${old.interruptions}; ${duration(s.seconds.offline)} downtime vs ${duration(old.seconds.offline)}.${latency} Only observed time is included.`;
  } else $("comparison").textContent = "Not enough comparable history yet. Both equal-length periods need at least 80% observed coverage before a trend comparison is shown.";
  $("period-title").textContent = data.period_seconds === 86400 ? "Daily breakdown" : "Hourly breakdown";
  const periods = $("periods");
  periods.replaceChildren();
  for (const p of [...data.periods].reverse()) {
    const row = document.createElement("tr");
    for (const value of [formatTime(p.from), p.samples ? p.interruptions : "—", p.samples ? duration(p.seconds.offline) : "—", p.samples ? duration(p.seconds.degraded) : "—", `${p.coverage_percent.toFixed(1)}%`, `${ms(p.mean_ms)} / ${ms(p.p95_ms)}`]) row.append(text("td", value));
    periods.append(row);
  }
}
function renderEvents(element, events) {
  element.replaceChildren();
  for (const item of events) {
    const row = document.createElement("li"), time = text("time", formatTime(item.ts));
    time.dateTime = new Date(item.ts * 1000).toISOString();
    row.append(time, text("span", `${item.kind.replaceAll("_", " ")} · ${item.message}`));
    element.append(row);
  }
  if (!element.children.length) element.append(text("li", "No recorded events in this window."));
}
async function openIncident(id) {
  selectedIncident = id;
  const request = ++detailRequest;
  navigate("incidents");
  $("incident-detail").hidden = false;
  $("detail-title").textContent = "Loading incident…";
  $("detail-body").replaceChildren();
  $("detail-title").focus();
  try {
    const data = await api(`api/incidents/${id}`);
    if (request !== detailRequest) return;
    const item = data.interval, body = $("detail-body");
    $("detail-title").textContent = `${labels[item.state]}${data.active ? " · current interval" : ""}`;
    const grid = document.createElement("dl");
    grid.className = "detail-grid";
    metric(grid, "Started", formatTime(item.start));
    metric(grid, "Observed until", formatTime(item.end));
    metric(grid, "Sampled duration", duration(item.end - item.start));
    metric(grid, "TCP before", ms(data.before?.latency_ms));
    metric(grid, "TCP after", ms(data.after?.latency_ms));
    metric(grid, "Scope", "Home Assistant host");
    body.append(grid, text("p", `${item.reason}. Evidence snapshot ${formatTime(Date.now() / 1000)}; refresh details for newer observations.`, "subtle"));
    if (data.probes.length) {
      body.append(text("h3", "Probe evidence"));
      for (const p of data.probes) body.append(text("p", `${p.kind} · ${p.name}: ${p.passed} passed, ${p.failed} failed, ${p.errors} monitor errors · mean ${ms(p.mean_ms)}`, "subtle"));
      const sample = data.first;
      if (sample) {
        const details = document.createElement("details");
        details.append(text("summary", "First observation details"));
        for (const p of sample.probes) details.append(text("p", `${p.name} · ${p.outcome}${p.detail ? ": " + p.detail : ""}`, "subtle"));
        body.append(details);
      }
    } else body.append(text("p", "No retained probe samples for this interval. A monitoring gap is not evidence that the network was down.", "subtle"));
    body.append(text("h3", "Nearby alert events"), text("p", "30s before through 10m after the interval. These events may include tests or another incident; proximity is not proof of association. API acceptance does not confirm device delivery.", "subtle"));
    const events = document.createElement("ol");
    events.className = "event-log";
    renderEvents(events, data.nearby_alert_events);
    body.append(events);
    body.append(text("p", "Before/after latency uses the nearest retained samples and may cross an unobserved gap. Durations are sampled estimates; no individual client impact is inferred.", "subtle"));
  } catch (error) {
    if (request !== detailRequest) return;
    $("detail-title").textContent = "Incident details unavailable";
    $("detail-body").append(text("p", error.message, "notice"));
  }
}
function showLiveError(error) {
  $("connection-error").textContent = `Live data unavailable: ${error.message}. Check the app log. Displayed history may be stale.`;
  $("connection-error").hidden = false;
  $("current-title").textContent = "Unknown";
  $("status-dot").className = "dot unknown";
  $("status-caption").textContent = "Fresh evidence unavailable";
  $("current-latency").textContent = "—";
  $("last-check").textContent = "Live updates unavailable";
  $("footer-note").textContent = "No fresh observation";
  $("mute").disabled = $("test-sound").disabled = true;
}
async function refreshHistory() {
  const end = latestStatus?.server_time ?? Date.now() / 1000;
  const query = `from=${end - Number($("range").value)}&to=${end}`;
  let needsInsights = historyDirty || Date.now() - lastInsights > 15000 || previousState !== latestStatus?.state;
  const [history, insights] = await Promise.all([
    api(`api/history?${query}`), needsInsights ? api(`api/insights?${query}`) : Promise.resolve(null)
  ]);
  if (!needsInsights && history.intervals.at(-1)?.id !== latestHistory?.intervals.at(-1)?.id) needsInsights = true;
  const freshInsights = insights || (needsInsights ? await api(`api/insights?${query}`) : null);
  renderHistory(history);
  if (freshInsights) { renderInsights(freshInsights); lastInsights = Date.now(); }
  previousState = latestStatus?.state;
  historyDirty = false;
  $("history-error").hidden = true;
}
async function poll() {
  try {
    renderStatus(await api("api/status"));
    $("connection-error").hidden = true;
  } catch (error) { showLiveError(error); }
  try { await refreshHistory(); }
  catch (error) {
    $("history-error").classList.remove("loading");
    $("history-error").setAttribute("role", "alert");
    $("history-error").textContent = `History unavailable: ${error.message}. Previously displayed trends may be stale.`;
    $("history-error").hidden = false;
  }
  setTimeout(poll, 2000);
}
async function act(button, path, body) {
  button.disabled = true;
  try {
    const result = await api(path, body);
    $("action-result").textContent = result.message || (result.muted ? "Alerts muted." : "Alerts unmuted.");
    historyDirty = true;
  } catch (error) { $("action-result").textContent = error.message; }
  finally {
    try { renderStatus(await api("api/status")); }
    catch (error) { showLiveError(error); }
  }
}
for (const button of document.querySelectorAll("[data-view], [data-go]")) button.addEventListener("click", () => navigate(button.dataset.view || button.dataset.go, true));
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
$("range").addEventListener("change", () => {
  historyDirty = true;
  $("history-error").classList.add("loading");
  $("history-error").setAttribute("role", "status");
  $("history-error").textContent = "Loading selected period…";
  $("history-error").hidden = false;
});
$("incident-filter").addEventListener("change", renderIncidents);
$("open-checks").addEventListener("click", () => { $("probe-details").open = !$("probe-details").open; if ($("probe-details").open) $("probe-details").scrollIntoView({block: "nearest"}); });
$("reload-incident").addEventListener("click", () => { if (selectedIncident !== null) openIncident(selectedIncident); });
$("close-incident").addEventListener("click", () => { detailRequest++; selectedIncident = null; $("incident-detail").hidden = true; $("incidents-title").focus(); });
$("bucket-selector").addEventListener("input", (event) => inspectBucket(Number(event.target.value)));
$("chart").addEventListener("click", (event) => {
  if (!latestHistory?.buckets.length) return;
  const rect = $("chart").getBoundingClientRect();
  const timestamp = latestHistory.from + (event.clientX - rect.left) / rect.width * (latestHistory.to - latestHistory.from);
  let nearest = 0, distance = Infinity;
  latestHistory.buckets.forEach((b, i) => { const d = Math.abs((b.start + b.end) / 2 - timestamp); if (d < distance) { nearest = i; distance = d; } });
  $("timeline-inspector").open = true;
  inspectBucket(nearest);
});
$("mute").addEventListener("click", () => act($("mute"), "api/mute", {muted: !latestStatus?.alerts.muted}));
$("test-sound").addEventListener("click", () => act($("test-sound"), "api/test-alerts", {}));
navigate(location.hash.slice(1));
poll();

"use strict";
let telemetryData = null, selectedClient = null, clientRequest = 0;
let activityRequest = 0;
const bytes = value => {
  if (!Number.isFinite(value)) return "Unavailable";
  const units = ["B", "kB", "MB", "GB", "TB"];
  const index = Math.min(4, Math.floor(Math.log10(Math.max(1, value)) / 3));
  return `${(value / 1000 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
};
const bps = value => Number.isFinite(value) ? `${(value / 1e6).toFixed(value < 1e6 ? 3 : 1)} Mbps` : "Unavailable";
const sourceNames = {unifi: "UniFi official API", legacy: "UniFi legacy telemetry", pihole: "Pi-hole DNS", starlink: "Starlink · Home Assistant", internet: "Internet · Home Assistant sensors"};
const telemetryMetricNames = {
  download: "Download (bit/s)", upload: "Upload (bit/s)", latency: "Dish ping (ms)",
  loss: "Dish ping loss (%)", connected: "Dish connected", update: "Update pending",
  restart: "Last restart", obstructed: "Obstructed", sleeping: "Sleeping",
  download_bps: "WAN download (bit/s)", upload_bps: "WAN upload (bit/s)", wan_status: "WAN status",
  queries: "DNS queries", blocked: "Blocked", percent_blocked: "Blocked (%)"
};
function trafficGraph(svg, points, downKey, upKey, start, end, gapSeconds) {
  svg.replaceChildren();
  const values = points.flatMap(p => [p[downKey], p[upKey]]).filter(Number.isFinite);
  if (!values.length) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
    node.setAttribute("x", "15"); node.setAttribute("y", "75"); node.setAttribute("class", "chart-empty");
    node.textContent = "No traffic observations in this period"; svg.append(node); return;
  }
  const max = Math.max(1, ...values), width = 930, height = 115;
  for (const [key, cls] of [[downKey, "traffic-download"], [upKey, "traffic-upload"]]) {
    let segments = [], last = null;
    for (const point of points) {
      if (!Number.isFinite(point[key])) { last = null; continue; }
      const x = 15 + (point.ts - start) / Math.max(1, end - start) * width;
      const y = 130 - point[key] / max * height;
      segments.push(`${last && point.ts - last.ts <= gapSeconds ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`);
      last = point;
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", segments.join(" ")); path.setAttribute("class", cls); svg.append(path);
  }
  const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
  label.setAttribute("x", "15"); label.setAttribute("y", "12"); label.setAttribute("class", "chart-empty");
  label.textContent = `Scale 0–${bps(max)}`; svg.append(label);
}
function renderDevices() {
  if (!telemetryData) return;
  const body = $("device-list"), search = $("device-search").value.toLowerCase();
  body.replaceChildren();
  for (const client of telemetryData.clients) {
    if (![client.name, client.ip, client.connection].join(" ").toLowerCase().includes(search)) continue;
    const row = document.createElement("tr"), name = document.createElement("td");
    const button = text("button", client.name);
    button.addEventListener("click", () => openClient(client.key));
    const activity = text("button", "Traffic & activity");
    activity.addEventListener("click", () => openClient(client.key));
    name.append(button, text("div", client.ip || "IP unavailable", "subtle"), activity);
    row.append(name, text("td", `${client.connection || "Unknown"}${client.signal_dbm != null ? ` · ${client.signal_dbm} dBm` : ""}`),
      text("td", client.stale ? "Unavailable" : `${bps(client.rx_bps)} / ${bps(client.tx_bps)}`),
      text("td", `${client.stale ? "Stale / not currently listed" : "Controller lists connected"} · ${formatTime(client.last_seen)}`));
    body.append(row);
  }
  const inventoryConfigured = telemetryData.sources.unifi.enabled || telemetryData.sources.legacy.enabled;
  $("devices-setup").hidden = inventoryConfigured;
  if (!body.children.length) emptyRow(body, search ? "No devices match this search. Try another name, IP or connection." :
    inventoryConfigured ? "No devices received yet. Check UniFi source status and errors under Sources." :
    "No controller connected. Follow the setup above to load devices.");
  $("devices-status").textContent = inventoryConfigured ?
    `${telemetryData.clients.length} retained devices · telemetry target ${telemetryData.interval_seconds}s · details are snapshots` :
    "Device inventory is not configured. Connection monitoring continues independently.";
}
function renderSources(data) {
  telemetryData = data;
  $("unifi-console-link").hidden = !data.sources.unifi.enabled && !data.sources.legacy.enabled;
  const sources = $("source-list"); sources.replaceChildren();
  for (const [name, source] of Object.entries(data.sources)) {
    if (!source.enabled) continue;
    const panel = document.createElement("section"); panel.className = "panel";
    const title = text("h3", sourceNames[name]);
    panel.append(title, text("p", !source.enabled ? "Not configured" : source.stale ? "Unavailable / stale" : `Fresh · read ${formatTime(source.ts)}`, "subtle"));
    if (source.error) panel.append(text("p", source.error, "notice"));
    if (source.data) {
      const lines = Object.entries(source.data).filter(([key]) => key in telemetryMetricNames)
        .map(([key, value]) => `${telemetryMetricNames[key]}: ${value ?? "Unavailable"}`);
      if (lines.length) panel.append(text("p", `${source.stale ? "Last known (not current):\n" : ""}${lines.join("\n")}`, "subtle source-values"));
      if (source.data.unavailable_metrics?.length) panel.append(text("p", `Unavailable sensors: ${source.data.unavailable_metrics.join(", ")}`, "subtle"));
    }
    if (name === "starlink") panel.append(text("p", "Restart or update pending is context, not proof that an outage was caused by an automatic update. Firmware can temporarily interrupt telemetry; monitoring continues.", "subtle"));
    if (name === "pihole") panel.append(text("p", "Counts cover Pi-hole's retained summary, not the selected chart period. Existing maintenance jobs are not inspected or modified.", "subtle"));
    sources.append(panel);
  }
  if (!sources.children.length) sources.append(text("p", "No optional sources connected. Basic connection monitoring is already working; connect additional sources below.", "subtle"));
  $("discover-sensors").disabled = !data.discovery_enabled;
  $("traffic-panel").hidden = !Object.values(data.sources).some(s => s.enabled);
  const dish = data.sources.starlink, wan = data.sources.legacy, generic = data.sources.internet;
  const active = !generic.stale && Number.isFinite(generic.data?.download) && Number.isFinite(generic.data?.upload) ? generic :
    !dish.stale && Number.isFinite(dish.data?.download) && Number.isFinite(dish.data?.upload) ? dish :
    !wan.stale && Number.isFinite(wan.data?.download_bps) && Number.isFinite(wan.data?.upload_bps) ? wan : null;
  $("traffic-down").textContent = bps(active === wan ? active?.data.download_bps : active?.data.download);
  $("traffic-up").textContent = bps(active === wan ? active?.data.upload_bps : active?.data.upload);
  $("traffic-caption").textContent = active ? `${active === generic ? "Configured internet sensors" : active === dish ? "Starlink terminal" : "UniFi WAN counters"} · read ${formatTime(active.ts)} · actual traffic, not a speed test` :
    "Current traffic unavailable. Map your internet sensors or configure supported router counters in Sources.";
  const dns = data.sources.pihole;
  $("dns-summary").textContent = !dns.stale ? `${dns.data?.queries?.toLocaleString() ?? "—"} / ${dns.data?.blocked?.toLocaleString() ?? "—"}` : "Unavailable";
  const equipment = $("infrastructure"); equipment.replaceChildren();
  for (const device of data.sources.unifi.data?.infrastructure || []) {
    equipment.append(text("p", `${device.name || "Equipment"} · ${device.error || `CPU ${device.cpu_percent ?? "—"}% · memory ${device.memory_percent ?? "—"}% · uptime ${duration(device.uptime_seconds)}`}${data.sources.unifi.stale ? " · stale" : ""}`));
    for (const radio of device.radios || []) equipment.append(text("p", `${radio.frequency_ghz} GHz radio · transmit retries ${radio.tx_retries_percent ?? "unavailable"}%`, "subtle"));
  }
  if (!equipment.children.length) equipment.append(text("p", "No equipment statistics available.", "subtle"));
  renderDevices();
}
async function openClient(key) {
  selectedClient = key;
  const request = ++clientRequest;
  activityRequest++;
  $("client-detail").hidden = false; $("client-title").textContent = "Loading device…";
  $("client-evidence-status").textContent = "Loading controller evidence…";
  $("client-activity-results").hidden = true;
  $("client-applications").replaceChildren(); $("client-categories").replaceChildren();
  $("client-activity").disabled = true;
  $("client-activity-note").textContent = telemetryData?.activity_enabled ?
    "Loads only on request; the selected window applies to router applications, not DNS." :
    "Enable unifi_activity_enabled in app Configuration, save and restart to allow application reads with your local read-only UniFi account.";
  $("client-body").replaceChildren(); $("client-queries").replaceChildren();
  $("client-dns-note").textContent = "DNS loads only on request; queries are not browsing history.";
  $("client-dns").disabled = true;
  try {
    const data = await api(`api/clients/${encodeURIComponent(key)}`);
    if (request !== clientRequest) return;
    const client = data.client;
    $("client-title").textContent = client.name;
    $("client-evidence-status").textContent = `${client.stale ? "Stale controller inventory" : "Controller lists connected"} · ${client.ip || "IP unavailable"} · ${client.connection || "Unknown connection"} · details read ${formatTime(Date.now() / 1000)}`;
    const body = $("client-body"), grid = document.createElement("dl"); grid.className = "detail-grid";
    for (const [label, value] of [["IP", client.ip], ["MAC", client.mac], ["Connection", client.connection],
      ["Controller RX", client.stale ? "Unavailable" : bps(client.rx_bps)], ["Controller TX", client.stale ? "Unavailable" : bps(client.tx_bps)],
      ["Signal", client.signal_dbm != null ? `${client.signal_dbm} dBm` : null], ["Channel", client.channel],
      ["Link speed", client.link_mbps != null ? `${client.link_mbps} Mbps` : null],
      ["AP / switch", client.ap || client.uplink], ["Port", client.port], ["Wi-Fi standard", client.wifi_standard]]) {
      const item = document.createElement("div"); item.append(text("dt", label), text("dd", value ?? "Unavailable")); grid.append(item);
    }
    body.append(text("p", client.stale ? "Stale / not currently listed; not a current internet test." : "Controller lists this client as connected."), grid);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 960 150"); svg.setAttribute("class", "device-chart");
    svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Client RX blue and TX green in the last 30 minutes");
    const now = Date.now() / 1000;
    trafficGraph(svg, data.samples, "rx_bps", "tx_bps", now - 1800, now, telemetryData.interval_seconds * 3);
    body.append(text("h3", "Client traffic · last 30 minutes"), svg, text("p", "Blue: controller RX · Green: controller TX · Includes possible LAN traffic; gaps are unobserved.", "subtle"));
    body.append(text("p", data.scope, "subtle"), text("h3", "Observed connection changes"));
    for (const event of data.events) body.append(text("p", `${formatTime(event.ts)} · ${event.event}`, "subtle"));
    if (!data.events.length) body.append(text("p", "No retained changes observed. This is not a guarantee of uninterrupted connectivity.", "subtle"));
    body.append(text("h3", "Shared network context"), text("p", "Recent Home Assistant probe incidents overlapping this observed IP binding. Individual client impact and cause are unconfirmed.", "subtle"));
    for (const interval of data.network_context) {
      const button = text("button", `${labels[interval.state] || interval.state} · ${formatTime(interval.start)}`, "text-button");
      button.addEventListener("click", () => { navigate("incidents", true); openIncident(interval.id); });
      body.append(button, document.createElement("br"));
    }
    if (!data.network_context.length) body.append(text("p", "No retained shared incidents overlap this binding window.", "subtle"));
    $("client-dns").disabled = client.stale || !telemetryData.sources.pihole.enabled;
    $("client-activity").disabled = !activityAvailable();
    if (telemetryData.activity_enabled && !telemetryData.sources.legacy.enabled) {
      $("client-activity-note").textContent = "Configure the dedicated local read-only UniFi username and password under Sources to load application evidence.";
    }
    $("client-title").focus({preventScroll: true});
  } catch (error) {
    if (request === clientRequest) {
      $("client-evidence-status").textContent = `Device details unavailable: ${error.message}`;
      $("client-body").replaceChildren();
    }
  }
}
function activityAvailable() {
  const client = telemetryData?.clients.find(client => client.key === selectedClient);
  return client && !client.stale && telemetryData.activity_enabled && telemetryData.sources.legacy.enabled;
}
function renderActivity(data) {
  $("client-activity-results").hidden = false;
  $("client-activity-note").textContent = `${formatTime(data.from)} – ${formatTime(data.to)} · fetched ${formatTime(data.fetched)}. Snapshot does not refresh automatically.`;
  const body = $("client-applications"); body.replaceChildren();
  const groups = new Map();
  let total = 0, complete = true;
  const applications = [...data.applications].sort((a, b) => (b.total_bytes ?? -1) - (a.total_bytes ?? -1));
  for (const app of applications) {
    const name = app.application || `Application ID ${app.application_id}`;
    const category = app.category || `Category ID ${app.category_id}`;
    const row = document.createElement("tr"), cell = text("td", name);
    cell.append(text("div", category, "subtle"));
    row.append(cell, text("td", bytes(app.total_bytes)),
      text("td", `${bytes(app.received_bytes)} / ${bytes(app.transmitted_bytes)}`),
      text("td", Number.isFinite(app.controller_activity_seconds) ?
        `${duration(app.controller_activity_seconds)} · router estimate, not screen time` : "Not reported"));
    body.append(row);
    if (Number.isFinite(app.total_bytes)) total += app.total_bytes; else complete = false;
    const group = groups.get(app.category_id) || {name: category, total: 0, complete: true};
    if (Number.isFinite(app.total_bytes)) group.total += app.total_bytes; else group.complete = false;
    groups.set(app.category_id, group);
  }
  $("activity-summary").textContent = applications.length ?
    `${applications.length} reported application rows · ${complete ? bytes(total) + " summed reported bytes" : "Some byte totals unavailable"}. Not a complete WAN usage total.` :
    "No visible application records. Traffic Identification, API support, permissions or classification delay may limit results; this does not prove no use.";
  if (!applications.length) emptyRow(body, "No application activity returned by the controller.");
  const categories = $("client-categories"); categories.replaceChildren();
  for (const group of [...groups.values()].sort((a, b) => b.total - a.total)) {
    const line = text("p", group.name);
    line.append(text("strong", group.complete ? bytes(group.total) : "Incomplete totals")); categories.append(line);
  }
  if (!groups.size) categories.append(text("p", "No category measurements available.", "subtle"));
  $("activity-limits").textContent = `${data.scope} ${data.warning} ${data.identity_note}${data.private_mac ? " This is a locally administered/private MAC." : ""}`;
  if (data.catalog_error) $("client-activity-note").textContent += ` ${data.catalog_error}`;
}
async function loadClientActivity() {
  const key = selectedClient, client = clientRequest, request = ++activityRequest;
  const seconds = Number($("activity-window").value);
  $("client-activity").disabled = true;
  $("client-activity-results").hidden = true;
  $("client-activity-note").textContent = "Reading this device's router-classified application snapshot…";
  try {
    const data = await api(`api/clients/${encodeURIComponent(key)}/activity?seconds=${seconds}`, undefined, 25000);
    if (client !== clientRequest || request !== activityRequest) return;
    renderActivity(data);
  } catch (error) {
    if (client === clientRequest && request === activityRequest) $("client-activity-note").textContent = `Router activity unavailable: ${error.message}`;
  } finally {
    if (client === clientRequest && request === activityRequest) {
      $("client-activity").disabled = !activityAvailable();
    }
  }
}
async function loadClientDNS() {
  const key = selectedClient, request = clientRequest;
  $("client-queries").replaceChildren();
  $("client-dns").disabled = true; $("client-dns-note").textContent = "Loading up to 100 recent DNS requests…";
  try {
    const data = await api(`api/clients/${encodeURIComponent(key)}/dns`);
    if (request !== clientRequest) return;
    $("client-dns-note").textContent = `${data.matched_by} · since ${formatTime(data.from)} · fetched ${formatTime(data.fetched)}. ${data.warning}`;
    const body = $("client-queries"); body.replaceChildren();
    for (const q of data.queries) {
      const row = document.createElement("tr");
      row.append(text("td", formatTime(q.ts)), text("td", `${q.domain || "Hidden"} · ${q.type || "—"}`),
        text("td", q.status || "Unknown"), text("td", `${q.reply || "Unknown"} · ${ms(q.reply_ms)}`)); body.append(row);
    }
    if (!body.children.length) emptyRow(body, "No visible DNS requests in this binding window. This does not mean no activity.");
  } catch (error) {
    if (request === clientRequest) $("client-dns-note").textContent = `DNS unavailable: ${error.message}`;
  } finally { if (request === clientRequest) $("client-dns").disabled = false; }
}
async function pollTelemetry() {
  try {
    renderSources(await api("api/telemetry")); $("telemetry-error").hidden = true;
    const end = latestStatus?.server_time ?? Date.now() / 1000, start = end - Number($("range").value);
    const history = await api(`api/traffic?from=${start}&to=${end}`);
    const dish = history.points.filter(p => p.source === "starlink");
    const generic = history.points.filter(p => p.source === "internet");
    const useGeneric = generic.some(p => Number.isFinite(p.dish_download));
    const useDish = !useGeneric && dish.some(p => Number.isFinite(p.dish_download));
    const points = useGeneric ? generic : useDish ? dish : history.points.filter(p => p.source === "legacy");
    trafficGraph($("traffic-chart"), points, useGeneric || useDish ? "dish_download" : "wan_download", useGeneric || useDish ? "dish_upload" : "wan_upload", start, end,
      Math.max(history.bucket_seconds * 1.5, telemetryData.interval_seconds * 3));
    $("traffic-history-label").textContent = `${useGeneric ? "Configured internet sensors" : useDish ? "Starlink terminal" : "UniFi WAN counters"} · ${formatTime(start)} – ${formatTime(end)} · ${history.bucket_seconds}s averaged buckets`;
  } catch (error) {
    $("telemetry-error").textContent = `Telemetry unavailable: ${error.message}. Check source configuration.`;
    $("telemetry-error").hidden = false;
    $("traffic-down").textContent = $("traffic-up").textContent = $("dns-summary").textContent = "Unavailable";
    $("traffic-caption").textContent = "Telemetry refresh failed; chart and device list may be stale.";
    $("devices-status").textContent = "Refresh failed; previously displayed device information may be stale.";
  }
  setTimeout(pollTelemetry, 10000);
}
$("device-search").addEventListener("input", renderDevices);
$("client-refresh").addEventListener("click", () => { if (selectedClient) openClient(selectedClient); });
$("client-close").addEventListener("click", () => { clientRequest++; selectedClient = null; $("client-detail").hidden = true; $("devices-title").focus(); });
$("client-dns").addEventListener("click", loadClientDNS);
$("client-activity").addEventListener("click", loadClientActivity);
$("activity-window").addEventListener("change", () => {
  activityRequest++;
  $("client-activity-results").hidden = true;
  $("client-activity-note").textContent = "Window changed. Load router traffic & activity for a new snapshot.";
  $("client-activity").disabled = !activityAvailable();
});
$("discover-sensors").addEventListener("click", async () => {
  $("discover-sensors").disabled = true;
  $("discovery-status").textContent = "Reading available traffic sensor candidates from Home Assistant…";
  $("discovery-results").replaceChildren();
  try {
    const data = await api("api/discover", {});
    $("discovery-status").textContent = `${data.note}${data.truncated ? " Only the first 200 candidates are shown." : ""}`;
    for (const candidate of data.candidates) $("discovery-results").append(text("p", `${candidate.entity_id} · ${candidate.unit || candidate.kind}`, "subtle source-values"));
    if (!data.candidates.length) $("discovery-results").append(text("p", "No matching sensors. Add a supported router or provider integration in Home Assistant, then try again.", "subtle"));
  } catch (error) { $("discovery-status").textContent = `Discovery unavailable: ${error.message}`; }
  finally { $("discover-sensors").disabled = !telemetryData?.discovery_enabled; }
});
pollTelemetry();

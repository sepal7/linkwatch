const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag = "div", content = "") {
    this.tag = tag;
    this.content = String(content);
    this.children = [];
    this.dataset = {};
    this.value = "";
  }
  append(...children) { this.children.push(...children); }
  focus() {}
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) {
    this.listeners ??= {};
    this.listeners[name] = callback;
  }
  replaceChildren(...children) { this.children = [...children]; }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return this.content + this.children.map(child => child.textContent).join(" "); }
}

function dashboard() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    $: element,
    document: {createElement: tag => new Element(tag)},
    text: (tag, value) => new Element(tag, value),
    formatTime: ts => `time:${ts}`,
    duration: seconds => `${seconds}s`,
    ms: value => String(value),
    emptyRow: (body, message) => body.replaceChildren(new Element("tr", message)),
    console
  });
  const source = readFileSync(path.join(__dirname, "..", "network_monitor", "app", "static", "telemetry.js"), "utf8");
  vm.runInContext(source.slice(0, source.indexOf('$("device-search").addEventListener')), context);
  return {element, context, run: source => vm.runInContext(source, context),
    set: (name, value) => vm.runInContext(`${name} = ${JSON.stringify(value)}`, context)};
}

function app(application, category, bytes, extra = {}) {
  return {application, category, total_bytes: bytes, application_id: 190, category_id: 13,
    catalog_id: 852158, received_bytes: null, transmitted_bytes: null, ...extra};
}

function snapshot(applications) {
  return {applications, from: 1, to: 2, fetched: 3, scope: "scope", warning: "warning", identity_note: "identity"};
}

test("services rank by bytes; protocol, unresolved and sensitive category evidence is not discarded", () => {
  const d = dashboard();
  d.set("fixture", snapshot([
    app("Example messaging", "Messaging", 100, {controller_activity_seconds: 900}),
    app("Example video", "Media", 700, {controller_activity_seconds: 1200}),
    app("Example transport", "Network protocols", 150),
    app(null, "TopSites-Adult", 50)
  ]));
  d.run("renderActivity(fixture)");
  const primary = d.element("client-applications");
  assert.equal(primary.children.length, 2);
  assert.match(primary.children[0].textContent, /Example video.*700 B.*70.0%/);
  assert.match(primary.children[1].textContent, /Example messaging.*100 B.*10.0%/);
  assert.doesNotMatch(primary.textContent, /TopSites|protocols|900s|1200s|Application ID/);
  assert.match(d.element("activity-coverage").textContent, /Named services: 800 B.*Network protocols: 150 B.*Unidentified services: 50 B/);
  assert.match(d.element("activity-summary").textContent, /1.0 kB traffic.*2 named services/);
  assert.equal(d.element("service-table").hidden, false);
  const technical = d.element("client-traffic-technical");
  assert.equal(technical.children.length, 4);
  assert.match(technical.textContent, /TopSites-Adult/);
  assert.match(technical.textContent, /overlapping estimate, not screen time/);
});

test("unknown-only and incomplete snapshots do not claim inactivity or zero use", () => {
  const d = dashboard();
  d.set("fixture", snapshot([app(null, "Web services", 100), app("Named", "Media", null)]));
  d.run("renderActivity(fixture)");
  assert.match(d.element("activity-summary").textContent, /totals incomplete/);
  assert.match(d.element("client-applications").textContent, /Named.*Unavailable.*Unavailable/);
  d.set("fixture", snapshot([app(null, "Unknown", 100)]));
  d.run("renderActivity(fixture)");
  assert.equal(d.element("client-applications").children.length, 0);
  assert.equal(d.element("service-table").hidden, true);
  assert.match(d.element("activity-summary").textContent, /100 B traffic.*services unidentified/);
  assert.match(d.element("activity-coverage").textContent, /Unidentified services: 100 B/);
  assert.equal(d.element("client-traffic-technical").children.length, 1);
});

test("DNS search is case-insensitive across loaded domains, types and outcomes with bounded counts", () => {
  const d = dashboard();
  d.set("loadedDNS", {from: 10, fetched: 20, limit: 100, queries: [
    {ts: 11, domain: "game.example", type: "A", status: "FORWARDED"},
    {ts: 12, domain: "ads.example", type: "AAAA", status: "GRAVITY"},
    {ts: 13, domain: "<example>", type: "HTTPS", status: "CACHE"}
  ]});
  for (const [search, expected] of [["GAME", "game.example"], ["aaaa", "ads.example"],
    ["gravity", "ads.example"], ["<example>", "<example>"]]) {
    d.element("client-dns-search").value = search;
    d.run("renderClientDNS()");
    assert.match(d.element("client-dns-count").textContent, /1 matching \/ 3 loaded.*time:10.*time:20/);
    assert.match(d.element("client-dns-count").textContent, /loaded snapshot only \(limit 100\)/);
    assert.ok(d.element("client-queries").textContent.includes(expected));
  }
  d.element("client-dns-search").value = "missing";
  d.run("renderClientDNS()");
  assert.match(d.element("client-dns-count").textContent, /0 matching \/ 3 loaded/);
  assert.match(d.element("client-queries").textContent, /No loaded queries match/);
  d.element("client-dns-search").value = " ";
  d.run("renderClientDNS()");
  assert.equal(d.element("client-queries").children.length, 3);
  d.set("loadedDNS", {from: 10, fetched: 20, queries: []});
  d.run("renderClientDNS()");
  assert.match(d.element("client-queries").textContent, /No DNS queries in this binding window/);
});

test("DNS actions require a visible group-impact preview and only send the confirmed token once", async () => {
  const d = dashboard(), calls = [];
  d.context.api = async (...args) => {
    calls.push(args);
    if (args[0].endsWith("/prepare")) {
      return {token: "synthetic-confirmation", domain: "example.com", action: "deny", expires_in: 60,
        groups: [{id: 7, name: "Example kids"}], impact: "ALL clients, not only this device."};
    }
    return {message: "Creation verified, not effective blocking."};
  };
  d.set("selectedClient", "synthetic-client");
  d.set("controlOptions", {dns_enabled: true, csrf: "synthetic-csrf"});
  d.set("loadedDNS", {from: 1, fetched: 2, queries: [{domain: "example.com", type: "A", status: "FORWARDED"}]});
  await d.run("previewDNSAction('example.com', 'deny')");
  assert.equal(calls.length, 1);
  assert.equal(d.element("dns-action-confirmation").hidden, false);
  assert.match(d.element("dns-action-scope").textContent, /DENY exact domain example.com for ALL clients in: Example kids \(ID 7\)/);
  assert.match(d.element("dns-action-impact").textContent, /not only this device/);
  await d.run("applyDNSAction()");
  await d.run("applyDNSAction()");
  assert.equal(calls.length, 2);
  assert.equal(calls[1][0], "api/clients/synthetic-client/dns-actions/apply");
  assert.equal(JSON.stringify(calls[1][1]), '{"token":"synthetic-confirmation"}');
  assert.equal(calls[1][3]["X-Linkwatch-Control"], "synthetic-csrf");
  assert.equal(d.element("dns-action-confirmation").hidden, true);
  assert.match(d.element("dns-action-status").textContent, /not effective blocking/);
});

test("cancelled or expired DNS confirmations cannot submit; unknown writes are shown without retry", async () => {
  const d = dashboard(), calls = [];
  d.context.api = async (...args) => { calls.push(args); throw Error("Outcome unverified"); };
  d.set("selectedClient", "synthetic-client");
  d.set("controlOptions", {dns_enabled: true, csrf: "synthetic-csrf"});
  const plan = {key: "synthetic-client", token: "synthetic-confirmation", expires: Date.now() + 60000};
  d.set("dnsPlan", plan);
  d.run("cancelDNSAction()");
  await d.run("applyDNSAction()");
  assert.equal(calls.length, 0);
  d.set("dnsPlan", {...plan, expires: 0});
  await d.run("applyDNSAction()");
  assert.equal(calls.length, 0);
  assert.match(d.element("dns-action-status").textContent, /expired/);
  d.set("dnsPlan", plan);
  await d.run("applyDNSAction()");
  assert.equal(calls.length, 1);
  assert.match(d.element("dns-action-status").textContent, /not verified.*Inspect Pi-hole/);
  await d.run("applyDNSAction()");
  assert.equal(calls.length, 1);
});

test("manual router controls preview the exact shared flag and only submit one confirmed token", async () => {
  const d = dashboard(), calls = [];
  d.context.api = async (...args) => {
    calls.push(args);
    if (args[0].endsWith("/prepare")) return {
      token: "router-confirmation", action: "unblock", name: "Example client",
      mac: "02:00:00:00:00:01", ip: "192.0.2.10", blocked: true, private_mac: true, expires_in: 60,
      impact: "Unblock clears the shared flag EVEN if set outside Linkwatch; LAN may be affected."
    };
    return {message: "Shared flag verified as UNBLOCKED; effective access not tested."};
  };
  d.set("selectedClient", "example-key");
  d.set("controlOptions", {router_enabled: true, csrf: "synthetic-csrf",
    router_targets: [{key: "example-key", mac: "02:00:00:00:00:01"}]});
  await d.run("previewRouterAction('unblock')");
  assert.equal(calls.length, 1);
  assert.equal(d.element("router-action-confirmation").hidden, false);
  assert.match(d.element("router-action-scope").textContent, /Unblock router block.*Example client.*02:00:00:00:00:01.*192.0.2.10.*BLOCKED.*private/);
  assert.match(d.element("router-action-impact").textContent, /EVEN if set outside/);
  assert.equal(d.element("router-action-apply").textContent, "Confirm: Unblock router block");
  await d.run("applyRouterAction()");
  await d.run("applyRouterAction()");
  assert.equal(calls.length, 2);
  assert.equal(JSON.stringify(calls[1][1]), '{"token":"router-confirmation"}');
  assert.equal(calls[1][2], 35000);
  assert.equal(calls[1][3]["X-Linkwatch-Control"], "synthetic-csrf");
  assert.match(d.element("router-action-status").textContent, /effective access not tested/);
});

test("router cancellation, expiry, selection change and unverified outcomes never auto-retry", async () => {
  const d = dashboard(), calls = [];
  d.set("selectedClient", "example-key");
  d.set("controlOptions", {router_enabled: true, csrf: "synthetic-csrf",
    router_targets: [{key: "example-key", mac: "02:00:00:00:00:01"}]});
  const plan = {key: "example-key", token: "synthetic", expires: Date.now() + 60000};
  d.context.api = async (...args) => { calls.push(args); throw Error("command accepted; readback unverified"); };
  d.set("routerPlan", plan); d.run("cancelRouterAction()");
  await d.run("applyRouterAction()");
  d.set("routerPlan", {...plan, expires: 0});
  await d.run("applyRouterAction()");
  assert.match(d.element("router-action-status").textContent, /expired/);
  d.set("routerPlan", {...plan, key: "another-device"});
  await d.run("applyRouterAction()");
  assert.equal(calls.length, 0);
  d.set("routerPlan", plan);
  await d.run("applyRouterAction()");
  await d.run("applyRouterAction()");
  assert.equal(calls.length, 1);
  assert.match(d.element("router-action-status").textContent, /unverified.*Inspect UniFi before retrying/);
  d.run("controlOptions.router_enabled = false; renderRouterControls()");
  assert.equal(d.element("router-action-buttons").hidden, true);
});

test("nonzero all-unidentified traffic has no empty service table and keeps catalog errors and every byte", () => {
  const d = dashboard();
  const fixture = snapshot(Array.from({length: 20}, (_, i) =>
    app(null, null, 3305000, {application_id: i + 1, category_id: 13})));
  fixture.catalogs = {
    applications: {status: "error", code: "http_403", message: "API key lacks catalog permission", requested: 20, matched: 0},
    categories: {status: "error", code: "timeout", message: "Catalog request timed out", requested: 1, matched: 0}
  };
  d.set("fixture", fixture); d.run("renderActivity(fixture)");
  assert.equal(d.element("service-table").hidden, true);
  assert.equal(d.element("client-applications").children.length, 0);
  assert.equal(d.element("client-traffic-technical").children.length, 20);
  assert.equal(d.element("activity-summary").textContent, "66.1 MB traffic · service names unavailable");
  assert.match(d.element("catalog-status").textContent, /Services: API key lacks catalog permission \(http_403\).*Categories: Catalog request timed out \(timeout\)/);
  assert.equal(d.element("catalog-details").open, false);
  d.set("fixture", snapshot([])); d.run("renderActivity(fixture)");
  assert.equal(d.element("service-table").hidden, true);
  assert.equal(d.element("catalog-details").hidden, true);
  assert.equal(d.element("activity-summary").textContent, "No traffic reported for this window");
});

test("DNS effective flags refresh from off to group 2; actions stay visible but gated without auto-enabling", async () => {
  const d = dashboard();
  d.set("loadedDNS", {from: 10, fetched: 20, limit: 100, queries: [
    {ts: 11, domain: "example.com", type: "A", status: "FORWARDED"}]});
  const options = {controls_schema: 2, dns_enabled: false, router_enabled: false, router_targets: [],
    csrf: null, dns: {enabled: false, group_ids: [2], reason: "disabled", saved_settings: "match"}};
  d.context.api = async () => structuredClone(options);
  await d.run("loadControlOptions()");
  const buttons = () => d.element("client-queries").children[0].children[4].children[0].children;
  assert.equal(buttons().length, 2);
  assert.equal(buttons()[0].disabled, true);
  assert.match(d.element("dns-actions-setup").textContent, /off in the running app/);
  options.dns.saved_settings = "different";
  await d.run("loadControlOptions()");
  assert.match(d.element("dns-actions-setup").textContent, /Saved DNS settings differ.*restart Linkwatch/);
  assert.equal(buttons()[0].disabled, true);
  options.dns.enabled = options.dns_enabled = true;
  options.csrf = "new-runtime-token";
  options.dns.saved_settings = "match"; options.dns.reason = "ready";
  await d.run("loadControlOptions()");
  assert.equal(buttons()[0].disabled, false);
  assert.match(d.element("dns-actions-setup").textContent, /on.*groups 2/);
  assert.match(d.element("dns-control-diagnostic").textContent, /enabled=true; group IDs=\[2\].*match running app/);
  d.context.api = async () => { throw Error("offline"); };
  await d.run("loadControlOptions()");
  assert.equal(buttons()[0].disabled, true);
  assert.match(d.element("dns-actions-setup").textContent, /settings unavailable.*offline/);
});

test("old control API or failed refresh cannot masquerade as disabled saved settings", async () => {
  const d = dashboard();
  d.context.api = async () => ({dns_enabled: false});
  await d.run("loadControlOptions()");
  assert.match(d.element("dns-actions-setup").textContent, /version mismatch/);
  assert.doesNotMatch(d.element("dns-actions-setup").textContent, /restart needed/);
});

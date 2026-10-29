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
  assert.match(d.element("activity-summary").textContent, /1.0 kB reported across all traffic/);
  const technical = d.element("client-traffic-technical");
  assert.equal(technical.children.length, 4);
  assert.match(technical.textContent, /TopSites-Adult/);
  assert.match(technical.textContent, /overlapping estimate, not screen time/);
});

test("unknown-only and incomplete snapshots do not claim inactivity or zero use", () => {
  const d = dashboard();
  d.set("fixture", snapshot([app(null, "Web services", 100), app("Named", "Media", null)]));
  d.run("renderActivity(fixture)");
  assert.match(d.element("activity-summary").textContent, /totals unavailable/);
  assert.match(d.element("client-applications").textContent, /Named.*Unavailable.*Unavailable/);
  d.set("fixture", snapshot([app(null, "Unknown", 100)]));
  d.run("renderActivity(fixture)");
  assert.match(d.element("client-applications").textContent, /No recognizable services.*not evidence of inactivity/);
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
    assert.match(d.element("client-dns-count").textContent, /1 matching \/ 3 loaded \(maximum 100\).*time:10.*time:20/);
    assert.match(d.element("client-dns-count").textContent, /not full DNS history/);
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
  assert.match(d.element("client-queries").textContent, /does not mean no activity/);
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

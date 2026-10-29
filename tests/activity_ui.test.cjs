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
  return {element, run: source => vm.runInContext(source, context),
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

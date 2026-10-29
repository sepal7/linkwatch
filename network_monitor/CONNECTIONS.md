# Optional data sources

Linkwatch works without any of these connections. Configure only the hardware
you have. There is no dependency on UniFi, Pi-hole or Starlink.

All endpoints, credentials and entity mappings belong in the app's Home Assistant
**Configuration** tab, never in the public repository. Save and restart after
changes. Keep backups private: they contain app options and locally retained data.

## Discovery

**If Devices and Sources are empty:** that is expected before setup. Open
**Home Assistant → Settings → Apps → Linkwatch → Configuration**, enter the
options for the sources you want, **Save**, then **Restart** Linkwatch. Source
panels appear once configured; allow a polling cycle and check their errors.
Devices currently require the UniFi connector below. Pi-hole adds DNS evidence,
not a standalone device inventory. HA sensor discovery does not scan your LAN,
create integrations or automatically connect to your router.

The **Sources** view shows configured connections, their capabilities, last
successful read and any errors. Unconfigured vendors do not clutter the dashboard.
Connections are activated only by explicit configuration; discovery never enables
them automatically or guesses credentials.

Set `ha_discovery_enabled: true`, restart, then choose **Sources → Discover Home
Assistant sensors**. This performs an authenticated, read-only request to Home
Assistant's state list transiently and returns candidates with traffic-rate units, plus recognizable
Starlink sensor IDs. Candidates are not proof that a sensor measures your WAN;
choose the right link and direction yourself. Renamed Starlink entities may need
manual mapping. No LAN scanning, port sweeps or background discovery is performed.
Only selected entity IDs and units are returned to the dashboard, not the full
Home Assistant state payload. Responses above 2 MiB are rejected; use manual
mapping if your Home Assistant state list exceeds this limit.

## Any router or internet provider

Use an existing local Home Assistant router/provider integration that exposes
traffic sensors. Map **whole-WAN actual traffic**, not a client interface, Wi-Fi
link speed or speed-test result:

```yaml
internet_entities:
  - download=sensor.internet_download_rate
  - upload=sensor.internet_upload_rate
  - latency=sensor.internet_ping
  - loss=sensor.internet_packet_loss
```

These are placeholder entity IDs: replace them with your own. Only map available
metrics. Empty lists disable the connector. Rate units supported: `bit/s`, `bps`,
`kbit/s`, `Mbit/s`, `Gbit/s`, `B/s`, `kB/s`, `MB/s`. Latency accepts `ms` or `s`;
loss accepts `%`. Other units are explicitly unavailable, not guessed.
Optional `connected=binary_sensor...` is also supported.

HA sensors marked unknown/unavailable are excluded. When HA provides
`last_reported`, readings older than 120 seconds are also excluded. On versions
without that field, Linkwatch can only confirm retrieval freshness, not the
upstream measurement age.

## UniFi

### Official local API

1. In **UniFi Network → Integrations**, create a local API key with the minimum
   permissions available for your Network version. Do not assume the key itself
   is read-only: Linkwatch limits its calls to reads.
2. Set `unifi_url` to your controller's **HTTPS origin** (no path) and
   `unifi_api_key` to the key.
3. Leave `unifi_site_id` empty for a single-site controller. With multiple sites,
   configure the site UUID from your local API documentation.

The official adapter follows the Network **10.6.109** schema: paginated
`/proxy/network/integration/v1/sites`, clients, devices and device statistics.
Other versions may work; incompatible schemas produce explicit errors.
It reads clients and up to eight equipment statistics per poll (three concurrent
statistics requests), with a 500-record limit per collection. It does not retrieve
configuration rules or execute device actions.

The official API in that version does **not** expose detailed per-client traffic,
signal strength or per-client internet tests. Those fields stay unavailable.
Device CPU, memory, uptime and radio retry percentages are displayed when exposed.

### Optional legacy telemetry

For richer client evidence, additionally create a **dedicated local read-only
account** and set:

```yaml
unifi_username: ""
unifi_password: ""
unifi_legacy_site: default
```

Fill credentials only in your private app Configuration. The legacy connector
can also run without an official API key.
It authenticates using `/api/auth/login` then reads only
`/proxy/network/api/s/{site}/stat/sta` and `/stat/health`.
No block, reconnect, reboot, upgrade, rule or configuration endpoints are called.
It never changes maintenance/update settings.

Client traffic rates are calculated from successive controller RX/TX byte
counters. Resets, reboots, first samples and long gaps produce **unavailable**,
not zero or a spike. **Client RX/TX is controller perspective and can include LAN
traffic**; it is not labeled as internet-only upload/download.
Where the WAN health response exposes numeric RX/TX counters, their deltas
produce WAN download/upload; otherwise these remain unavailable.
Signal, channel, wired link rate, port and AP/switch association are shown only
if returned. Link speed is negotiated capacity, not actual throughput.

Legacy endpoints are version-dependent and are not an official compatibility
guarantee. A failure is isolated from official inventory and connectivity probes.
No automatic alternate-account or insecure-login fallback is attempted.

### TLS certificates

TLS verification is on. Use a valid controller certificate, or set
`unifi_tls_fingerprint` to the **64 hexadecimal characters of its SHA-256
certificate fingerprint**, verified through a trusted channel. Pinning validates
that exact certificate; never copy a fingerprint from an untrusted connection.
Certificate renewal changes the fingerprint and requires a deliberate update.
Linkwatch does not disable certificate checking globally.

## Pi-hole v6

Set `pihole_url` to the local HTTP/HTTPS origin (no `/admin` or `/api` path) and
`pihole_password` to an application password generated in Pi-hole's settings.
Keep application-password write access disabled where supported. Linkwatch does
not assume Pi-hole's permission settings provide a granular read-only token.
HTTPS uses normal certificate verification by default. For a self-signed local
certificate, set `pihole_tls_fingerprint` to its **64 hexadecimal SHA-256
certificate fingerprint**, verified through a trusted channel such as an
authenticated SSH session on that host. Enter it without colons. The fingerprint
requires an HTTPS URL and pins that exact certificate for authentication,
summary and on-demand query requests, including reauthentication. A mismatched
certificate is rejected; there is no verification-disabled fallback. Update the
pin deliberately after verifying a renewed certificate. Leave it empty for
normal certificate verification. Never copy a pin from an untrusted connection.

An HTTPS-only server does not need HTTP enabled: keep its HTTPS URL. Connection
refusal indicates an address/port/listener problem, not an incorrect password;
TLS errors occur before API authentication. HTTP 401/403 instead indicates an
authentication/authorization failure. Source errors distinguish these cases
without exposing endpoints, passwords or certificate contents.

HTTP sends credentials and DNS data
unencrypted on the LAN; use only on a trusted network, preferably choose HTTPS.

It uses `/api/auth` for session authentication, `GET /api/stats/summary` for
totals, and `GET /api/queries` only when you request device details.
Expired sessions are cleared and authentication is retried on the next request.
No blocking toggle, gravity update, maintenance job, script, SSH, cron, package
upgrade or configuration call is made.

Summary counters cover Pi-hole's retained summary, **not** the selected chart
window. Device DNS requests are limited to the newest 100 in at most 30 minutes,
since Linkwatch continuously observed that unique client/IP binding. Queries
are cached in memory for 15 seconds and never written to Linkwatch's database.
The Pi-hole remains responsible for its own query retention and privacy settings.

Only current unique IPv4/IPv6 addresses exposed by the client inventory are
matched. IP reuse, collector gaps and client reappearance reset the binding
window. Router-forwarded DNS can hide the real client; alternate resolvers,
additional IPv6 addresses and encrypted DNS can bypass this view. No name-only
or speculative matching is performed.

DNS `GRAVITY`, `DENYLIST`, `CACHE`, `FORWARDED`, errors and other status values
are shown as Pi-hole reports them. A blocked query is a policy decision, not an
internet outage. An allowed DNS answer is not proof of a successful connection.
Domains are DNS activity, **not** browsing history, page URLs or application
content. DNS counts cannot establish bandwidth per domain.

## Starlink

Use Home Assistant's [official Starlink integration](https://www.home-assistant.io/integrations/starlink/).
Its local connection uses the dish API, not just the HTTP status page.
In bypass mode a route may be required: configure it deliberately on your
network if needed. Linkwatch does not add routes or change Starlink settings.

Map available sensor IDs from that integration, for example:

```yaml
starlink_entities:
  - download=sensor.starlink_downlink_throughput
  - upload=sensor.starlink_uplink_throughput
  - latency=sensor.starlink_ping
  - loss=sensor.starlink_ping_drop_rate
  - connected=binary_sensor.starlink_connected
  - update=binary_sensor.starlink_update_available
  - restart=sensor.starlink_last_restart
  - obstructed=binary_sensor.starlink_obstructed
  - sleeping=binary_sensor.starlink_sleep
```

These are examples, not guaranteed entity names. Missing optional sensors should
be omitted, not replaced with guessed names. Source failure or stale sensors are
shown explicitly. No location, terminal serial number, account ID, stow, restart,
sleep-schedule control or firmware-update command is requested individually or
invoked. Discovery may transiently receive unrelated states in HA's list response;
it discards their values and does not return or retain them.

Starlink performs its own automatic updates. Update availability, last restart,
obstruction and sleep are useful **context**, not proof of why an outage occurred.
Linkwatch continues its independent probes during dish reboots; missing dish
telemetry is not counted as successful connectivity. It does not suppress those
outages as “planned maintenance” or promise to distinguish an update from power
loss. Your existing router/DNS maintenance scripts are likewise left alone;
their operation cannot be inferred merely from an overlapping outage.

## Polling and retention

`telemetry_interval_seconds` defaults to **15**, configurable from 10–300.
Connectivity probes retain their independent two-second default.
Sources have six-second HTTP deadlines, a 20-second whole-collector deadline,
bounded response sizes and independent errors. A cycle waits for its bounded
collectors; if it exceeds the target interval, the next starts after completion.
No overlapping cycles, catch-up bursts or repeated authentication loops are used.
The browser fetches cached summaries every ten seconds; it does not poll each
hardware source itself. Device details are snapshots with a Refresh button.

Traffic history uses up to 240 averaged buckets per source in the selected
window. The graph chooses one source, identified beneath it, without summing
Starlink and router traffic (which would double-count the same link).
Current rate preference is mapped generic internet sensors, then Starlink, then
available legacy WAN counters. Historical graph selection is labeled separately.

Extra SQLite tables retain selected source numbers/status values (50,000 rows),
client rates/signal (100,000 rows across all clients), and observed client-list
changes (10,000 rows), each for **at most seven days** or your shorter
`retention_days`. Caps can shorten retention. Missing periods are not interpolated.
Inventory and IP-binding continuity are in memory and re-established after
restart; previous DNS ownership is not assumed across restarts. Stored history
still contains opaque client keys; full client names/IPs are not stored in these
telemetry history tables.

Appearance/absence from a connected-client list is recorded as **observed**,
not a precisely timed physical disconnect. Collector outages create an unknown
period, not a flurry of invented client outages. Wireless evidence is not a
device-origin internet speed/latency test. Linkwatch does not run intrusive
throughput tests, inspect encrypted traffic, or identify every application's
activity.

## Privacy

- Configuration and API secrets remain in private Supervisor options, never
  returned by Linkwatch's dashboard API or included in error logs.
- Raw device payloads, serials, location and full Home Assistant state payloads
  are not persisted. Only explicitly mapped HA sensor values are retained.
- Private device names, IPs, MACs and requested domains are visible to authorized
  Home Assistant dashboard users when the relevant connector is enabled.
- DNS detail is memory-only, briefly cached and limited; do not share screenshots
  or logs without reviewing them. Backups and exports can be sensitive.
- No analytics service, cloud router login or telemetry upload is used.
  Existing external probes and optional phone/Cast alerts retain their documented
  provider/delivery limitations.
- Public tests use fabricated documentation addresses and domains, never a
  household's actual device inventory or credentials.

## Compatibility references

- [Official UniFi local API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API)
- [UniFi Network 10.6.109 OpenAPI](https://developer.ui.com/network/v10.6.109/openapi.json)
- [Pi-hole API and version-specific local docs](https://docs.pi-hole.net/api/)
- [Pi-hole session authentication](https://docs.pi-hole.net/api/auth/)

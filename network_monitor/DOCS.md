# Linkwatch

Live internet health, outage history, and local or Nest Hub sound alerts.
Formerly Home Network Monitor; the internal slug and history location are
unchanged for in-place upgrades.

Optional router, DNS and internet-provider telemetry is described in
[Connect data sources](CONNECTIONS.md). No particular router, DNS server or
internet provider is required. All connectors default to off.

## Configuration

The defaults are enough to begin observing external connectivity. Change options
on this app's **Configuration** tab, save, and restart.

| Option | Default | Meaning |
| --- | --- | --- |
| `interval_seconds` | `2` | Target time between sampling starts, 2–300 seconds. |
| `timeout_seconds` | `1` | Per-probe timeout, 1–10 seconds. Probes run concurrently. |
| `external_targets` | `1.1.1.1:443`, `8.8.8.8:53` | 2–8 TCP destinations, using at least two distinct public IPs, preferably different providers. IPv6 uses `[address]:port`. No hostnames or private IPs here. |
| `router_host` | empty | Optional router IP. Leave empty to disable. |
| `router_port` | `80` | A known listening TCP port on the router, not an ICMP check. |
| `dns_server` | empty | Optional explicit resolver IP; leave empty to disable DNS health checks. |
| `dns_test_name` | `example.com` | Public hostname expected to return public IPv4 A addresses. |
| `retention_days` | `7` | Retain up to 1–90 days of history. |
| `max_samples` | `200000` | Also cap samples to 1,000–1,000,000 rows. The earlier limit wins. |
| `alert_failure_seconds` | `15` | Continuous observed external failure needed before a down sound, 0–600 seconds. |
| `alert_recovery_seconds` | `10` | Continuous restored external reachability needed before recovery sound, 0–600 seconds. |
| `alerts_enabled` | `false` | Enable automatic alert requests and the test button. |
| `sound_output` | `cast` | `local` for host speakers, `cast` for a media player, `both`, or `none` for phone-only. Defaults preserve existing Cast behavior. |
| `media_player` | empty | Google Cast entity ID, such as `media_player.living_room_display`. Required for enabled `cast`/`both`, not `local`. |
| `phone_notify_service` | empty | Optional Companion notification action, such as `notify.mobile_app_my_phone`. Empty disables phone notifications. |

Use the full options object shown in the Configuration UI; change only the
values you need. Invalid options stop startup with an error in the app log,
rather than silently falling back to different targets.

The defaults contact Cloudflare and Google TCP endpoints. They do not download
web pages. DNS tests, when explicitly enabled, send the configured test hostname
to the configured resolver. Those providers can observe the requests.

### Reading the evidence

**Online:** every configured check passed. **Degraded:** at least one external
TCP target passed, but another check failed or encountered a monitor error.
**Unreachable:** every external TCP probe failed. This suggests an interruption,
but a destination policy, firewall or endpoint failure can produce the same
evidence. **Unknown:** no fresh observation, no usable external results, or
failed probes mixed with local probe-mechanism errors.

A router's TCP port can be closed while the router still forwards traffic.
DNS responses only pass when the requested A lookup yields nonempty public
addresses; NXDOMAIN, empty responses, local sinkhole addresses and timeouts do
not pass. These checks do not validate DNSSEC or every name on the internet.
No optional router or DNS check is needed to start.

The overview defaults to a **rolling 30 minutes**, not a full day. Status and
graph refresh approximately every two seconds after each request finishes.
Five-minute and one-hour views provide closer inspection; 24-hour and seven-day
views are secondary options for investigating recurring problems. Summary
statistics refresh about every 15 seconds or when a new state interval appears.
New installs probe every two seconds with a one-second timeout. Updating does
not override existing options: for a faster cadence set `interval_seconds: 2`
and `timeout_seconds: 1` in app Configuration, save and restart. A one-second
timeout is more sensitive to slow responses than the old two-second setting.

The graph uses at most 901 buckets for windows up to one hour (two-second
buckets for the 30-minute view), and 241 for longer windows. Latency bars
show average successful external TCP connection time; the scale uses the maximum
observed latency. Status columns prioritize unreachable, then unknown, degraded,
online within each bucket. A partly failing bucket is not all online. Gaps are
shown separately and no line connects across unobserved periods.
Click the graph to inspect the nearest sampled bucket, or expand **Inspect
timeline** and use its keyboard-accessible slider. A bucket lists sample counts,
failures, degraded/unknown counts and mean/maximum latency; it does not invent
samples for blank periods.

### Trends and incident details

**Trends** shows observed downtime, degraded time, longest sampled interruption,
mean/p95/maximum successful TCP latency and monitoring coverage. Coverage means
the proportion of the selected window with retained online, degraded or offline
interval evidence, not an uptime percentage. Unknown/unobserved time is separate,
including time before installation, after the last sample, or no longer retained.
Overlapping intervals after clock changes are counted once, with gaps/unknown
taking precedence. Interval/event retention can reduce coverage independently
of retained samples.

Previous-period comparisons require two equal-length windows, each with at least
80% observed coverage. Comparisons are of observed totals, not normalized outage
rates. The p95 is the nearest-rank 95th percentile of each sample's mean successful
external TCP time, not the percentile of every individual probe or packet.
Hourly/daily rows use UTC boundaries with labels in the browser's local time,
and clip the first/last rows to the selected window. Offline spans crossing a
row boundary count in both rows; the period rows are not additive incident counts.

Open an item in **Incidents** or **Recent interruptions** for its retained
full-interval details, probe pass/fail/error counts, first observation and nearest
retained TCP measurements before and after it. The list is clipped to the
selected window; details show the retained interval, which can extend outside
that window. Details are a snapshot; **Refresh details** reloads current evidence.
Sound events are shown from 30 seconds before through 10 minutes after the
interval and may include another incident or a test sound. They are labeled
nearby events, not causally attributed delivery. A monitoring gap has no probe
evidence. Optional device telemetry is separate evidence; shared incidents do not
prove that a particular client lost internet access.

Phone alerts use the configured `notify.mobile_app_...` action through Core's
supported API, with the same debounce as sound. See
[phone setup and Local Push limitations](#phone-notifications).
An empty `phone_notify_service` disables phone requests. Use `sound_output: none`
and a phone action for phone-only alerts. Dashboard mute covers all outputs.
Test requests are labeled as tests; a phone notification is not presented as
an actual network failure.

Observations occur once per cycle; failures shorter than the sampling interval
can be missed. The cadence is a target, not a real-time guarantee. If probing
takes longer than the interval, the next cycle starts after completion, with no
overlapping probes or synthetic catch-up samples. The dashboard shows the last
actual cycle length. Times use the host's wall clock; alert debounce uses a
monotonic clock. Keep Home Assistant's time correct.

### Outages, alerts and gaps

Raw state changes are recorded regardless of alert settings, including a
one-sample interruption shorter than alert debounce. Interval durations are
sample-based estimates, ending at the next state observation or latest sample.
They are not exact physical failure timestamps.

Sound alert state is separate. A down request occurs after the configured
failure dwell; a recovery request only follows an announced down state, after
the recovery dwell. Degraded counts as restored external reachability.
Unknown observations and missed cycles reset pending dwell, rather than filling
unobserved time with assumed failure or recovery.

Restart resets alert dwell and records a gap from the last sample until the new
session observes again. An outage spanning restart is split by that gap; the
app cannot know what happened while stopped. It does not play an assumed
recovery at startup. A new sustained failure can generate a new down request
after restart. Dashboard mute persists, but requests skipped while muted or
disabled are not replayed later. Unmuting during an already-announced outage
does not replay the down sound; the test control is available.

No sample becomes fresh just because the app restarted. Observations older than
the greater of three target intervals or interval plus two timeouts are stale.
While a worker is stopped, the status is unknown immediately. Scheduler gaps
and backwards clock changes are recorded explicitly when sampling resumes.
Forward/backward clock adjustments can affect wall-clock interval lengths;
they are not evidence of network uptime.

### Sound delivery

For a speaker on your Pi, follow the
[host speaker setup](#host-speaker-setup). Set `sound_output: local`,
enable alerts and select an available analog/USB/HDMI output in the app page's
Audio settings. The Pi has no internal speaker. Local playback uses `paplay`
through Supervisor's PulseAudio setup; no external token, network media fetch
or Cast receiver is involved. No automatic cable detection is attempted.
Audio still needs working speakers, volume and supported host hardware. Local
test success is not a listening test.

For Cast, follow the [Nest Hub setup](#nest-hub-setup).
The app uses the Supervisor-provided token only for the internal Core API and calls:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_display
data:
  media_content_id: media-source://media_source/local/home-network-monitor/recovery.wav
  media_content_type: audio/wav
```

This same action in **Developer tools > Actions** is useful for troubleshooting.
Replace the example entity with your device. Use `down.wav` for the failure
sound. No automation is required: the app implements debounce and calls the API.

Networking checks never wait for audio playback. A bounded separate worker
handles delivery, with an eight-second timeout for each output. `both` invokes
the outputs concurrently; failure of one does not prevent the other. A local
player is terminated on timeout or shutdown. Requests older than 30
seconds are dropped, not played late. No automatic replay/retry is performed
after failure. The dashboard/event log records API rejection, unavailable
tokens, sound preparation errors, queue overflow and accepted requests.
“Accepted” means Home Assistant accepted the action, not that audible delivery
was verified. The app cannot detect physical silence, muted volume or a Cast
receiver's internet dependency.

### Host speaker setup

The Pi has no internal speaker. Connect powered speakers to a supported
3.5 mm, USB or HDMI output; a cable alone makes no sound.

1. Set `sound_output: local` and `alerts_enabled: true` in app Configuration.
2. Select your output under **Audio** on the app page. If analog/headphones is
   not exposed by your host OS, use a supported USB audio adapter or HDMI.
3. Save, restart and use **Settings & logs → Test alerts**. Listen at the speaker
   and confirm its power, volume and mute settings.

Local playback needs no WAN and does not automatically detect a cable. A
stopped host, power loss or failed speaker prevents it. Linkwatch cannot
force-enable unsupported audio hardware.

### Nest Hub setup

1. Add your device with Home Assistant's **Google Cast** integration and find
   its `media_player` entity under **Settings → Devices & services**.
2. Check **Media → My media**. The app creates `home-network-monitor/down.wav`
   and `recovery.wav` in Home Assistant's media directory.
3. Set a local Home Assistant URL under **Settings → System → Network** that
   the Hub can fetch, typically `http://<HA-LAN-IP>:8123`. Cast may not resolve
   local hostnames. HTTPS requires a trusted, matching certificate.
4. Configure `sound_output: cast`, your `media_player` entity and
   `alerts_enabled: true`. Save, restart, test and listen. Use `both` for
   independent host-speaker and Cast requests.

Home Assistant resolves the `media-source://` URI to a signed fetchable URL;
the Hub does not fetch sound through authenticated ingress. No cloud TTS,
external bearer token or extra automation is required.

**WAN-outage sound is best effort.** The default Cast receiver may need internet
to launch even for local audio. LAN/Wi-Fi/router failure also prevents delivery.
Playback may interrupt existing media; the app neither restores it nor adjusts
volume. Test your actual Hub during a controlled WAN-only outage. This is not a
safety alarm, and a Hub browser dashboard is not promised.

### Phone notifications

1. Sign in with the Home Assistant Companion app and allow phone notifications.
2. Find and manually test `notify.mobile_app_<your_phone>` in **Developer tools
   → Actions**.
3. Set that exact action in `phone_notify_service` and enable `alerts_enabled`.
   Use `sound_output: none` for phone-only, or `local` for host speakers too.
   Save, restart and use **Test alerts**.

Foreground use is not required, but phone permissions and background restrictions
still apply. Ordinary push may require home internet and fail or arrive late
during an outage. **Companion Local Push** can work without WAN while the phone
and Home Assistant remain on a working LAN; configure and test it separately.
Android persistent-connection/battery settings and iOS internal URL/Wi-Fi SSIDs
matter. See [Companion Local Push](https://companion.home-assistant.io/docs/notifications/local).

Local Push does not help away from home or with a failed LAN. Linkwatch does not
bypass Do Not Disturb, force critical alerts or guarantee delivery. Normal push
may route notification content through external push providers. Dashboard mute
covers every output, and test requests are labeled as tests.

## Storage, exports and backups

History is in `/data/history.sqlite`, persisted by Supervisor across app restarts
and updates. SQLite uses WAL and one transaction per sampling cycle, without
calling Home Assistant Recorder for every probe. Default two-second sampling
means roughly 43,200 sample transactions per day; longer intervals reduce SD
card writes at the expense of missing brief interruptions. Use reliable storage
and Home Assistant backups.
At this cadence, the default 200,000-sample cap retains about 4.6 days, even
though the time cap is seven days. Set `max_samples` to at least 302,400 if you
want a full seven days at two-second cadence, subject to interval/event caps.

Pruning runs on startup and every 60 samples. Time and sample-count limits both
apply (up to 59 temporary extra rows between prunes). Events and intervals also
have a 10,000-row cap. Very frequent state changes can therefore shorten detailed
interval/event history before sample history expires. Oldest clipped intervals
start at the retained sample boundary. Row space is reused; database file size
does not immediately shrink when lowering retention. A WAL journal size limit
applies after checkpoints. Long CSV readers can temporarily delay WAL reuse.

Use **Export intervals CSV** for state durations including gaps, or **Export raw
samples CSV** for UTC timestamps, observed state, mean TCP latency and per-probe
JSON evidence. Exports follow the selected window and clip intervals to it.
They stream a consistent read transaction rather than loading all samples in
memory. Dashboard events show the most recent 100 events in the window and the
incident list the most recent 100 matching non-online intervals; CSV includes all retained rows
of the selected kind in that window. No percentage “uptime” is fabricated.

The app requests **cold backups** so SQLite closes before Supervisor backs up
its data. Select the app in a Home Assistant backup and restore through
Supervisor. Restart/backup downtime is recorded as a monitoring gap. Uninstalling
and deleting app data removes history.

## Access and permissions

The web server listens only inside the container on port 8099, with no host port
mapping. Production requests must come from the Supervisor ingress peer
`172.30.32.2`; forwarded IP headers do not bypass this check. Home Assistant
handles browser authentication. Dashboard changes require JSON and a custom
request header. There is no separate login, external analytics, CDN or cloud
dashboard dependency.

`audio: true` connects to Supervisor's PulseAudio service so the app can play
to the host output selected on the app page. No raw device or GPIO access is
requested. `homeassistant_api: true` is required for Cast, phone requests and
optional selected sensor reads/discovery. **This grants the app
administrator access to Core through the proxy**, even though this app only
uses playback, configured Companion notifications, selected state reads and
opt-in sensor discovery. Supervisor does
not offer a service-specific token scope.
No additional Supervisor API role, host networking, hardware privileges, Docker
socket or Home Assistant configuration mount is requested. The media directory
is mounted writable only to create the app's two sound assets; existing files
outside its own `home-network-monitor` folder are not modified.

Do not expose container port 8099 directly or disable ingress restrictions.
History/CSV can include configured local addresses; consider that before sharing
exports. Personal network settings are never committed as defaults.

## Troubleshooting

- **Repository is empty or the app is missing:** verify the repository default
  branch contains `repository.yaml` and `network_monitor/config.yaml`, refresh
  the app store, and ensure the host architecture is aarch64 or amd64.
- **Image build fails:** initial installation needs working internet and access
  to Docker Hub/Python package downloads. Inspect the installation log; this
  is separate from dashboard operation after installation.
- **Unknown or old data:** inspect the app log and last observation time.
  A stopped app, stale browser connection, monitor error or restart gap is not
  an internet outage.
- **Always degraded:** inspect the per-probe detail. Use known listening router
  ports and destinations that allow TCP connections from your network.
- **Sound accepted but silent:** verify the entity, volume, Cast integration,
  local Home Assistant URL and media browser. Test the action manually online.
  Then test a WAN-only outage without interrupting the LAN. Default Cast
  receiver startup can depend on WAN; there is no guarantee of outage playback.
- **Playback HTTP error:** check Core's logs and the dashboard alert error.
  No external access token is required or accepted in app configuration.
- **Local sound is silent:** check the app's Audio output selector, powered
  speakers, hardware volume, and `sound_output: local` (or `both`). Inspect the
  event log for local player errors. If analog/headphones output is unavailable
  on your host, try supported USB audio or HDMI; the app does not force-enable
  the Raspberry Pi's analog driver. No internet is needed for local playback.

## Local installation fallback

If repository installation is unavailable, download this repository as a ZIP
and copy **only the `network_monitor` directory** into Home Assistant's local
apps/addons directory. With the Samba share app, this is commonly the
`addons` share, resulting in `addons/network_monitor/config.yaml` alongside
the Dockerfile and `app` folder. This is **not** the `config/custom_components`
directory and not the File editor's default configuration root.

Refresh/reload the app store and look for **Local apps** (older versions:
**Local add-ons**), then install and start **Linkwatch**.
Do not install both the repository and local copies: they would maintain
separate histories and could both send sounds.

## Development

Use Python 3.13. In a virtual environment, install
`network_monitor/requirements.txt`, `pyyaml` and `ruff`, then run:

```shell
python -m unittest discover -s tests -q
ruff check .
node --check network_monitor/app/static/dashboard.js
node --check network_monitor/app/static/telemetry.js
```

For loopback-only development, create a private `options.json` containing `{}`
outside the repository and run `python -m network_monitor.app --development
--options <options.json> --data <data-directory> --media <media-directory> --port
8099`. Open `http://127.0.0.1:8099`. Defaults run real public TCP probes; optional
connectors run only when privately configured. Never enable development mode in
Home Assistant. CI checks service behavior and ARM64/amd64 image builds; these
do not replace installation and alert listening tests on real hardware.

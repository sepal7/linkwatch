# Linkwatch

**Live internet health, outage history, speaker alerts and phone notifications.**

Linkwatch is a local-first **Home Assistant OS app** (formerly called an add-on)
that watches your connection, surfaces brief interruptions on a rolling live
graph, and keeps the evidence for later investigation. It can sound an alert
through speakers connected to your Home Assistant host, a Google Home / Nest
Hub, and the Home Assistant Companion app on your phone. No laptop or cloud
dashboard is needed.

**Version 1.1.1 is experimental.** Automated service tests and multi-architecture
image build checks are provided. The initial version has been user-reported
working on Home Assistant OS; each update and audible playback during a real
WAN outage still need device verification. API acceptance does not prove a Hub
played a sound.

## Install on Home Assistant OS

The app supports **aarch64** (including Raspberry Pi 4 with 64-bit Home Assistant
OS) and **amd64**. It is not a custom integration installed through HACS.

1. Open **Settings > Apps > App store**. Older Home Assistant versions call this
   **Add-ons > Add-on store**.
2. Open the store's **three-dot menu > Repositories**, add
   **`https://github.com/sepal7/linkwatch`**, and close the dialog.
3. Refresh the store if necessary. Open **Linkwatch > Install**.
   The first installation builds the image on your Home Assistant host and
   requires internet access to download the Python base image and packages.
   On a Pi, allow several minutes and watch the installation log.
4. Leave **Start on boot** enabled, start the app, then select **Open Web UI**.
   Optionally enable **Show in sidebar**. Observations begin immediately.
5. Configure optional router/DNS checks or Nest Hub alerts on the app's
   **Configuration** tab. Save and restart the app after changing options.

The repository must have `repository.yaml` and the `network_monitor` directory
published on its **default branch**. A local checkout or unpublished feature
branch alone cannot be installed by adding the repository URL.

If the store does not find the app, see the [local installation fallback](network_monitor/DOCS.md#local-installation-fallback).

## Update an existing installation

**Moving from the former `sepal7/home-network-monitor` repository:** this
repository has been renamed to `sepal7/linkwatch`. Home Assistant derives its
repository ID from the URL, so adding the new URL creates a separate app identity
even though the internal slug is unchanged.

Back up the old app, copy any options you want to reuse, and export history CSVs
before uninstalling it or deleting its data. Add the new repository URL above
and install **Linkwatch 1.1.1**. The fresh installation starts with empty history;
settings and history do not automatically migrate from the old repository ID.
A backup of the old app is for recovering that old installation, not an
automatic import into the new one. Do not run both monitors with alerts enabled.

For later updates from **this** repository, use the app store's three-dot menu
to **Check for updates**, then open **Linkwatch > Update**. Restart and reopen
the web UI; reload the browser if it still shows the old layout. In-place updates
under the same repository URL preserve configured options and stored history.

If no update appears, use the **app store's** check-for-updates command, not just
the browser refresh or the general Settings > Updates page. Confirm the
repository is listed and check the app's installed and available version.
A local app copied into `/addons` is not subscribed to GitHub updates: update
its source using the [local installation instructions](network_monitor/DOCS.md#local-installation-fallback)
or use the repository installation above. If the repository fails to load,
check **Settings > System > Logs > Supervisor** for the error before changing
anything else. A GitHub release or tag is not required for app store updates.

The overview now defaults to a **rolling last 30 minutes**, refreshing roughly
every **2 seconds**. Five-minute and one-hour live views are available; day/week
views are secondary options for investigating patterns. The new-install probe
defaults are `interval_seconds: 2` and `timeout_seconds: 1`. Existing installs
keep their prior settings: change those two options in app Configuration and
restart if you want faster detection. A shorter timeout can classify a slow
endpoint as failed more readily. Sound debounce remains unchanged.

## What you get

- Concurrent, bounded TCP checks against independent public IP destinations,
  independent of DNS. Optional router TCP and explicit DNS A-answer checks.
- A compact dark, offline-capable dashboard with a rolling live timeline and
  separate Trends, Incidents, and Settings & logs views.
- Coverage-aware interruption duration, degraded time, mean/p95/maximum TCP
  latency, hourly/daily breakdowns and equal-period comparisons.
- Clickable incidents with probe evidence, nearby alert events and latency
  before/after. Individual client impact is not inferred without telemetry.
- Raw brief interruptions retained separately from debounced sound alerts.
- Separate unknown/stale states and explicit restart/missed-sampling gaps.
  Stopped monitoring is never reported as uptime.
- SQLite persistence in the app's `/data`, bounded time/sample/event retention,
  and streaming CSV exports for both samples and intervals.
- Locally generated, distinct failure and recovery sounds; optional Cast
  or direct host-speaker playback, persisted dashboard mute and a test control.

**Interpretation matters:** all external checks failing is evidence of lost
reachability to those endpoints, not proof that every internet service is down.
One passing external check plus any failed configured check is **degraded**.
Probe mechanism failures become **unknown** when external reachability cannot
be established. TCP latency is not ICMP latency, bandwidth or packet loss.
No cloud application's specific errors are inferred from these checks.

## Pi / host speaker setup

The Pi 4 has no built-in speaker. Connect **powered speakers to its 3.5 mm
audio jack**, a **USB speaker**, or an **HDMI display with speakers**. A cable
on its own cannot make sound; supported outputs depend on the host and OS.

1. In Linkwatch's **Configuration**, set `sound_output: local` and
   `alerts_enabled: true`. No `media_player` entity is needed for local output.
2. Select the intended **output device under Audio on the app page**. Choose
   the analog/headphones output for 3.5 mm, or the appropriate USB/HDMI output.
   If your OS does not expose analog audio, use a supported USB audio adapter
   or HDMI instead. This app cannot enable unsupported hardware outputs.
3. Save, restart, open **Settings & logs**, and use **Test alerts**.
   Confirm it is audible at the speaker and the speaker is powered/unmuted.

Local playback uses Home Assistant's PulseAudio service, not Cast, cloud TTS,
or a WAN media URL. It can therefore work during a WAN-only outage. It does
not automatically detect a cable or override your chosen host audio device.
Actual output on your Pi still needs a listening test; a successful playback
process is not proof of audible sound. A stopped Home Assistant host, power
loss, muted device or failed speaker will prevent alerts.

## Nest Hub setup

1. Add your device using Home Assistant's **Google Cast** integration. Find its
   `media_player` entity in **Settings > Devices & services**.
2. Ensure **Media > My media** works. The app creates its own
   `home-network-monitor/down.wav` and `recovery.wav` in Home Assistant's media
   directory. It does not expose an unauthenticated media server.
3. Under **Settings > System > Network**, configure a Home Assistant local URL
   the Hub can fetch. Usually use **`http://<HA-LAN-IP>:8123`**. Do not rely on
   `homeassistant.local`: Cast devices may not resolve local hostnames. For
   HTTPS, the device must trust the certificate and the hostname must match.
4. In the app Configuration, set `sound_output: cast`, `media_player` (for example
   `media_player.living_room_display`) and `alerts_enabled: true`. Save and
   restart. Use **Test alerts** under **Settings & logs** and listen at the Hub.
      Select `sound_output: both` to request host and Cast playback independently.

For Cast, the app calls Home Assistant's supported `media_player.play_media` API with a
local **`media-source://`** URI. Home Assistant resolves and signs the fetchable
media URL for Cast; the Hub does **not** fetch audio from authenticated ingress.
There is no TTS, external bearer token or extra automation to configure.

**Outage sound is best effort, not guaranteed:** Google's default Cast receiver
may require WAN access to launch even for local audio. A router, Wi-Fi or LAN
failure can also prevent Cast playback. Playback may interrupt current media;
this app does not restore it or change the device volume. Test your actual Hub
with a controlled WAN-only outage. Do not rely on this as a safety alarm.
The dashboard is intended for Home Assistant's browser UI; a Nest Hub browser
dashboard is not promised.

## Phone notifications

1. Sign in to your Home Assistant instance with the official **Home Assistant
   Companion app** on your phone and allow notifications in the phone's settings.
2. In Home Assistant **Developer tools > Actions**, find your phone's action,
   for example `notify.mobile_app_my_phone`. First test that action manually.
3. Set `phone_notify_service` to that exact action name in Linkwatch
   Configuration and enable `alerts_enabled`. For phone-only alerts use
   `sound_output: none`; for a Pi speaker plus phone use `sound_output: local`.
   Save and restart, then use **Test alerts** under **Settings & logs**.

Down/recovery notifications use the same configured debounce as the sounds.
Dashboard mute mutes all outputs. Tests are labeled as tests, not actual outages.
The phone does not need the app open in the foreground, but OS notification
permissions and background restrictions still apply.

Normal push may need the home's internet connection and can fail or arrive late
during an outage. **Companion Local Push can work without WAN when your phone
and Home Assistant remain connected on a working LAN**, but it must be configured
and tested on the phone. On Android, configure the Companion app's persistent
connection for home Wi-Fi or always; battery/background restrictions can affect
it. On iOS, Local Push requires a configured internal URL and internal Wi-Fi
SSIDs. See the [Companion Local Push documentation](https://companion.home-assistant.io/docs/notifications/local).
Local Push does not help when you are away from home or the LAN itself is down.
Linkwatch does not change those phone settings, force critical alerts, bypass
Do Not Disturb, or promise delivery. Home Assistant API acceptance only confirms
the action request, not the phone's receipt. Normal push may route notification
content through external push providers; use Local Push where suitable.

See [configuration, operation, backup and troubleshooting](network_monitor/DOCS.md).

## Development and verification

Python 3.13 is used in the image. From the repository root:

```shell
python -m venv .venv
# Activate the virtual environment for your platform.
python -m pip install -r network_monitor/requirements.txt pyyaml ruff
python -m unittest discover -s tests -v
ruff check .
node --check network_monitor/app/static/dashboard.js
```

For a loopback-only local run, create an `options.json` containing `{}` outside
the repository, then use:

```shell
python -m network_monitor.app --development --options <options.json> --data <data-directory> --media <media-directory> --port 8099
```

Open `http://127.0.0.1:8099`. This runs real probes against the public defaults,
but does not contact Home Assistant unless alerts are explicitly enabled and a
Supervisor token is present. Never enable `--development` inside Home Assistant.

The [verification workflow](.github/workflows/verify.yml) runs tests, builds
both `linux/arm64` and `linux/amd64` images, and tests the real host-audio player
against a synthetic PulseAudio sink, without publishing images. The app
repository installer builds locally; a container registry account is not needed.
Building an ARM image is not the same as installing it on Home Assistant OS.

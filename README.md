# Linkwatch

**Your connection, devices and DNS—in one local view.**

Linkwatch is a Home Assistant OS app that records brief internet interruptions,
shows a live 30-minute timeline and sends optional speaker or phone alerts.
It works with any internet provider. No special router or DNS server is needed.

## Install

1. In Home Assistant, open **Settings → Apps → App store → ⋮ → Repositories**.
2. Add **`https://github.com/sepal7/linkwatch`**.
3. Install **Linkwatch**, enable **Start on boot**, then **Start → Open Web UI**.

Supports **64-bit Raspberry Pi / aarch64** and **amd64**. Not a HACS integration.
The first install builds locally and may take a few minutes.

## Add what you have

| Optional connection | What it adds |
| --- | --- |
| Home Assistant traffic sensors | Actual upload/download from your existing router or provider integration |
| UniFi | Devices, equipment health and optional on-demand application/category traffic |
| Pi-hole v6 | DNS totals and on-demand per-device queries and block decisions |
| Starlink through Home Assistant | Dish traffic, latency, loss, obstruction, restart and update context |

Open **Sources** to discover existing Home Assistant sensor candidates.
Discovery is opt-in; it does not scan your LAN or change device settings.
Every connector is optional and disabled by default.
**Devices & activity** puts router application snapshots beside DNS decisions.
Enable `unifi_activity_enabled` to use the local read-only connection. Traffic
is evidence, not screen time or proof of what someone saw.
Optional confirmed controls add kids-group DNS allow/deny and manual UniFi
network block/unblock. Both start **disabled**; router unblocking can clear an
administrator's shared block flag. [Control setup and impact](network_monitor/CONNECTIONS.md#optional-manual-router-controls).

**[Connect data sources →](network_monitor/CONNECTIONS.md)**

## Alerts

Use your Home Assistant host's selected speaker output, Google Cast,
or the Companion app's phone notification action. Enable alerts in app
Configuration, then use **Settings & logs → Test alerts**.

**[Alert setup and configuration →](network_monitor/DOCS.md)**

## Updates & privacy

Use the **app store's ⋮ → Check for updates**, then update Linkwatch.
Back up before updating. Updates from this same repository preserve app data.
Moving from the old `home-network-monitor` repository is a fresh installation:
export/back up first; old history and settings do not automatically transfer.

Data stays in your Home Assistant app storage. Never post credentials,
device inventories or DNS logs to public issues. Optional phone push may use
external push providers. **[Privacy and data limits →](network_monitor/CONNECTIONS.md#privacy)**

**Experimental:** service tests and ARM64/amd64 image builds are automated.
Hardware compatibility and actual alert delivery still need testing on your
devices. Missing/stale observations are never presented as healthy uptime.

[Operation & troubleshooting](network_monitor/DOCS.md) ·
[Changelog](network_monitor/CHANGELOG.md)

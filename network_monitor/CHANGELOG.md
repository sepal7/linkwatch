# Changelog

## 1.3.0

- Device Traffic & activity entry with on-demand router application/category
  snapshots beside existing DNS decisions, in the same restrained local UI.
- Opt-in `unifi_activity_enabled` uses the existing local read-only connection.
  Windows: 30 minutes, 1 hour and 24 hours. Application payloads are not
  persisted; a maximum of 16 snapshots are cached in memory for up to 60 seconds.
- Explicit controller estimates, unknown fields, exact-MAC identity checks,
  classification gaps and unsupported API states; no screen-time or complete
  internet-destination claims.
- Safe configured-origin link to manage restrictions in UniFi. No router write
  credentials, pause/resume actions, rule edits or automatic enforcement.

## 1.2.2

- Optional `pihole_tls_fingerprint` for HTTPS-only Pi-hole servers with
  self-signed certificates. Normal verification remains the default; an exact
  trusted SHA-256 pin applies to authentication, session renewal and DNS reads.
- Distinguish connection refusal, TLS verification, certificate-pin mismatch
  and timeout errors without exposing private connection details.

## 1.2.1

- More display periods: 1, 10 and 15 minutes; 2, 3, 6 and 12 hours; 2 and 3 days.
  The default remains 30 minutes; these do not change probe frequency or retention.
- Visible, step-by-step source setup and a device-inventory connection guide.
- Distinct messages for an unconfigured controller, empty inventory and searches
  with no matches.

## 1.2.0

- Optional local UniFi official inventory/equipment and legacy read-only
  telemetry, with explicit missing-field and stale-source states.
- Optional Pi-hole v6 summary and on-demand, bounded DNS detail tied to a
  continuously observed unique client/IP binding.
- Generic Home Assistant internet sensors and optional Starlink sensors for
  traffic and dish health, including update/restart context without claiming
  an outage's cause.
- Opt-in discovery of existing HA traffic sensor candidates; no LAN scanning
  or automatic configuration changes.
- Consolidated traffic, device details, observed connection changes and Sources
  views; bounded local numeric history, no persistent DNS query copy.
- Shorter public README and separate connection/privacy documentation.

## 1.1.1

- Moved the public repository to `sepal7/linkwatch` and updated installation
  links and repository metadata.
- Documented fresh installation from the renamed repository and the resulting
  separate Home Assistant app identity. Old settings/history do not transfer
  automatically; back up and export before removing the old installation.
- Clarified app-store update checks and the difference between repository and
  local installations.

## 1.1.0

- Renamed to Linkwatch with an original connection/pulse logo, while preserving
  the repository URL, internal slug and stored history for existing installs.
- Optional local host-speaker alerts through Supervisor PulseAudio for analog,
  USB or HDMI outputs; `local`, `cast` and independent `both` playback modes.
- Optional Companion app phone notifications, including phone-only mode and
  clearly labeled tests. Delivery errors are isolated per output and logged.
- Simplified dark overview inspired by network-console interfaces, with separate
  Trends, Incidents, and Settings & logs views.
- Rolling 30-minute graph by default, approximately two-second dashboard
  refresh, and two-second graph buckets for this live window.
- New-install probe defaults are two-second intervals and one-second timeouts;
  existing configured options are preserved.
- Coverage-aware downtime, degraded time, latency percentiles, hourly/daily
  breakdowns and previous-period comparisons when coverage is sufficient.
- Clickable incidents with retained probe evidence, surrounding latency and
  nearby (not automatically attributed) sound events.
- No database migration or device-level telemetry connection is required.

## 1.0.0

- Initial experimental Home Assistant OS app for aarch64 and amd64.
- Local dashboard, concurrent TCP/DNS observations, bounded SQLite history,
  monitoring gaps, sampled interval durations and CSV exports.
- Separate debounced failure/recovery sounds via Home Assistant media-source
  and Cast, with persisted mute, test control and visible API errors.

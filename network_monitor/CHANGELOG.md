# Changelog

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

# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users and purpose

Linkwatch serves Home Assistant OS users who need live connection health,
retained interruption evidence and optional sounds without keeping a laptop
running. The user requested an original name/logo and local Pi audio output,
as well as Nest Hub alerts and optional Companion app phone notifications.

## Operating context

Installed as a Supervisor-managed app. The dashboard runs within authenticated
Home Assistant ingress and must remain usable over the LAN when WAN is down.
The first target is a 64-bit Raspberry Pi 4; amd64 is also supported.

## Capabilities and constraints

Raw sampled failures are distinct from debounced sound notifications. Unknown,
stale observations and restart gaps must not become uptime. History is local,
persistent and retention-bounded. No live device setup or validation is assumed.
Cast sound delivery during outages remains best effort until device-tested.
Local sound uses the host's Supervisor audio output with powered analog,
USB or HDMI speakers. There is no built-in speaker or cable-detection claim.
Phone alerts use the user's registered Companion notification action. Standard
push may fail during WAN loss; configured Local Push requires a working LAN.
No actual phone delivery or physical speaker audibility has been verified.

## Product principles

- Show the evidence and its limits rather than a misleading uptime percentage.
- Keep monitoring independent of sound delivery and external services.
- Preserve brief observed interruptions even when alerts are debounced.

## Open decisions and assumptions

The user confirmed the initial installation works. They requested a simpler
UniFi/Starlink-inspired interface: concise status, a rolling last-30-minute graph
and details on click rather than long explanations on the overview. Fast live
error visibility is primary; historical trends are secondary. Dark neutral
surfaces and restrained blue controls follow their supplied references.
Optional read-only UniFi and Pi-hole collectors add controller and DNS evidence;
generic Home Assistant rate sensors support any provider, and optional Starlink
sensors add dish context. No vendor is required. Opt-in discovery lists candidate
HA sensors without scanning the LAN or enabling connections automatically.
Ethernet connectivity, DNS decisions and controller association alone are not
proof of a device's internet experience. Maintenance scripts and automatic
updates are never modified. No private endpoints or device data belong in the
public repository.

Device activity extends this same visual world rather than replacing the outage
dashboard. Opt-in router application snapshots and DNS decisions remain separate
evidence streams with explicit periods. Traffic does not prove active gaming,
screen time or viewed content. Application snapshots are briefly memory-only;
no full destination capture, HTTPS interception or browsing profile is added.
The user explicitly approved optional confirmed manual router block/unblock
after being told that the shared flag can include administrator blocks and
blocking may affect LAN access too. This is not app-owned pause/resume.
The device view prioritizes exact controller-named services ranked by bytes,
with named/protocol/unidentified coverage visible. Raw IDs, unverified category
text and overlapping time estimates belong in expandable technical evidence.
Names must match the category/application composite, not a local app ID alone.
Separately enabled DNS actions can create exact allow/deny rules only for
explicitly configured existing enabled non-Default kids groups, after a fresh
group-wide impact preview and confirmation. Existing rules and memberships
are never edited. Selected-client applicability requires an explicit exact-IP
Pi-hole client entry; unknown mappings stop writes. Rule readback verification
is distinct from effective enforcement. UniFi monitoring credentials remain
read-only; a separate opt-in control account can change only the shared
per-MAC block flag, with fresh state/safeguards and one-shot confirmation.
An explicit client-MAC allowlist and configured HA host IPv4 protect against
household lockout. Infrastructure is excluded and unverified outcomes never
trigger retries. No automatic unblock, rule edits, schedules or time limits.

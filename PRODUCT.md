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
Client telemetry is not connected, and Ethernet connectivity alone is not a
source of device-specific internet failure evidence.

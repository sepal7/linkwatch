# Dashboard design

The user-provided UniFi and Starlink screenshots establish the direction:
dark neutral surfaces, fine borders, restrained blue controls and short labels.
No logos or vendor-specific artwork are copied. The dashboard operates inside
Home Assistant ingress with no CDN, external fonts or network dependency.
The original Linkwatch mark uses two link brackets around a pulse on a blue
rounded square. `mark.svg` is the scalable source; app-store PNGs use the same
mark. The repository slug remains unchanged for existing installations.

The default view answers what is happening now: a compact status strip,
rolling 30-minute graph and three recent interruptions. Status and graph refresh
roughly every two seconds; window statistics refresh every 15 seconds or on a
new state interval. Five-minute/hour views are available; longer periods are
secondary choices for investigation, not the default.

Navigation separates Overview, Trends, Incidents, and Settings & logs. Connection
evidence expands inline. Incidents open an inline evidence panel, not a blocking
modal. Sound setup, device telemetry limitations and event logs stay out of the
overview. Every interactive metric leads to details.

System typography, a 1280px maximum width, 32px desktop/18px mobile gutters,
44px primary controls and visible focus keep the surface usable. On mobile,
the four-part status strip becomes two columns and long evidence tables scroll
within keyboard-focusable regions. State always includes a text label.
Semantic green, amber, red and gray mark online, degraded, unreachable and
unknown/gaps respectively. Coverage never represents unobserved time as online.

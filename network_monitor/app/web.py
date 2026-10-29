import asyncio
import json
import math
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .service import Monitor
from .insights import incident, insights
from .telemetry import SourceError, Telemetry
from .activity import Activity
from .actions import ActionConflict
from .dns_actions import DNSActions
from .router_actions import RouterActions

STATIC = Path(__file__).parent / "static"


def window(request: web.Request, default_hours=24) -> tuple[float, float]:
    try:
        end = float(request.query.get("to", time.time()))
        start = float(request.query.get("from", end - default_hours * 3600))
        if not (math.isfinite(start) and math.isfinite(end) and
                0 <= start < end and end - start <= 90 * 86400):
            raise ValueError
        return start, end
    except (ValueError, OverflowError):
        raise web.HTTPBadRequest(text="Use finite epoch seconds with 0 < range <= 90 days") from None


def create_app(monitor: Monitor, *, development=False,
               telemetry: Telemetry | None = None) -> web.Application:
    @web.middleware
    async def ingress(request, handler):
        peer = request.transport.get_extra_info("peername") if request.transport else None
        actual_ip = peer[0] if peer else None
        allowed = {"127.0.0.1", "::1"} if development else {"172.30.32.2"}
        if actual_ip not in allowed:
            raise web.HTTPForbidden(text="Home Assistant ingress required")
        if request.method == "POST":
            # A custom header and JSON disallow cross-origin form submissions.
            if request.headers.get("X-Network-Monitor") != "1":
                raise web.HTTPForbidden(text="Missing app request header")
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType(text="Use application/json")
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'self'"
        )
        return response

    app = web.Application(middlewares=[ingress], client_max_size=1024)
    activity = Activity(telemetry) if telemetry is not None else None
    dns_actions = DNSActions(telemetry) if telemetry is not None else None
    router_actions = RouterActions(telemetry) if telemetry is not None else None
    control_csrf = secrets.token_urlsafe(32)
    if activity:
        app.on_cleanup.append(activity.close)
    if dns_actions:
        app.on_cleanup.append(dns_actions.close)
    if router_actions:
        app.on_cleanup.append(router_actions.close)

    async def status(request):
        return web.json_response(monitor.status(time.time()))

    async def history(request):
        start, end = window(request)
        return web.json_response(monitor.store.history(
            start, end, points=900 if end - start <= 3600 else 240))

    async def trends(request):
        start, end = window(request)
        return web.json_response(insights(monitor.store, start, end))

    async def incident_detail(request):
        try:
            interval_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="Incident ID must be an integer") from None
        result = incident(monitor.store, interval_id)
        if result is None:
            raise web.HTTPNotFound(text="Interval is no longer retained")
        return web.json_response(result)

    async def export(request):
        start, end = window(request)
        kind = request.query.get("kind", "intervals")
        if kind not in ("samples", "intervals"):
            raise web.HTTPBadRequest(text="kind must be samples or intervals")
        response = web.StreamResponse(headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="network-{kind}.csv"',
            "Cache-Control": "no-store",
        })
        await response.prepare(request)
        batch = bytearray()
        for index, row in enumerate(monitor.store.export_rows(start, end, kind)):
            batch.extend(monitor.store.csv_line(row))
            if index % 128 == 0:
                await response.write(bytes(batch))
                batch.clear()
                await asyncio.sleep(0)
        if batch:
            await response.write(bytes(batch))
        await response.write_eof()
        return response

    async def mute(request):
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(data, dict) or type(data.get("muted")) is not bool:
            raise web.HTTPBadRequest(text="muted must be boolean")
        monitor.store.set_setting("muted", str(data["muted"]).lower())
        monitor.store.log(time.time(), "mute", f"Alerts muted: {data['muted']}")
        return web.json_response({"muted": monitor.alerts.muted})

    async def test_sound(request):
        if not monitor.config.alerts_enabled or monitor.alerts.muted:
            raise web.HTTPConflict(text="Enable alerts in app configuration and unmute first")
        monitor.alerts.enqueue("test", time.time())
        return web.json_response({"message": "Test queued for configured outputs; check devices and the event log"},
                                 status=202)

    async def sources(request):
        if telemetry is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        return web.json_response(telemetry.snapshot())

    async def traffic(request):
        if telemetry is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        start, end = window(request)
        return web.json_response(telemetry.history(start, end))

    async def client(request):
        if telemetry is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        result = telemetry.client_detail(request.match_info["key"])
        if result is None and router_actions and monitor.config.unifi_controls_enabled:
            target = next((target for target in router_actions.targets()
                           if target["key"] == request.match_info["key"]), None)
            if target:
                result = {"client": {**target, "name": "Configured control target", "stale": True},
                          "samples": [], "events": [], "network_context": [],
                          "scope": "Not in connected inventory. Only an explicit fresh-router-verified unblock "
                                   "can be used here; this is not a current activity observation."}
        if result is None:
            raise web.HTTPNotFound(text="Client is no longer retained")
        return web.json_response(result)

    async def dns(request):
        if telemetry is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        try:
            result = await telemetry.dns(request.match_info["key"])
        except SourceError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from None
        except (ValueError, TypeError, KeyError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="DNS query unavailable; inspect Pi-hole connectivity and API version") from None
        except aiohttp.ClientError:
            raise web.HTTPServiceUnavailable(text="Pi-hole connection unavailable") from None
        return web.json_response(result)

    async def client_activity(request):
        if activity is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        try:
            seconds = int(request.query.get("seconds", "1800"))
        except ValueError:
            raise web.HTTPBadRequest(text="Activity window must be integer seconds") from None
        if seconds not in (1800, 3600, 86400):
            raise web.HTTPBadRequest(text="Choose 1800, 3600 or 86400 activity seconds")
        try:
            return web.json_response(await activity.snapshot(request.match_info["key"], seconds))
        except SourceError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from None
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
            raise web.HTTPServiceUnavailable(
                text="Application traffic unavailable; check UniFi local credentials, permissions and API compatibility") from None

    async def unifi_console(request):
        if not monitor.config.unifi_url:
            raise web.HTTPServiceUnavailable(text="Configure a UniFi HTTPS origin first")
        # Only the configured, validated origin; never accept a redirect target from the request.
        raise web.HTTPFound(location=monitor.config.unifi_url)

    async def control_options(request):
        enabled = bool(dns_actions and monitor.config.pihole_actions_enabled)
        router_enabled = bool(router_actions and monitor.config.unifi_controls_enabled)
        return web.json_response({"dns_enabled": enabled,
                                  "router_enabled": router_enabled,
                                  "router_targets": router_actions.targets() if router_enabled else [],
                                  "csrf": control_csrf if enabled or router_enabled else None})

    async def confirmed_action(request):
        router = request.match_info["kind"] == "router-actions"
        actions = router_actions if router else dns_actions
        if actions is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        # A same-origin-only readable random token supplements JSON/custom headers.
        # Do not trust forwarded Host/Origin values to authorize a write.
        csrf = request.headers.get("X-Linkwatch-Control", "")
        if (request.headers.get("Sec-Fetch-Site") not in (None, "same-origin") or not csrf.isascii() or
                not secrets.compare_digest(csrf, control_csrf)):
            raise web.HTTPForbidden(text="Open Linkwatch through Home Assistant and preview the action again")
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        operation = request.match_info["operation"]
        allowed = {"prepare": {"action"} if router else {"domain", "action"}, "apply": {"token"}}
        if operation not in allowed:
            raise web.HTTPNotFound()
        if not isinstance(data, dict) or set(data) != allowed[operation]:
            raise web.HTTPBadRequest(text="Use only the required action fields")
        try:
            key = request.match_info["key"]
            if operation == "prepare":
                result = (await actions.prepare(key, data["action"]) if router else
                          await actions.prepare(key, data["domain"], data["action"]))
            else:
                result = await actions.apply(key, data["token"])
            return web.json_response(result)
        except ActionConflict as exc:
            raise web.HTTPConflict(text=str(exc)) from None
        except SourceError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from None
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError, TypeError, KeyError):
            raise web.HTTPServiceUnavailable(
                text=("Router action unavailable; check separate control credentials, permissions and infrastructure safeguards"
                      if router else "DNS action unavailable; check Pi-hole API access, group configuration and exact client membership")) from None

    async def discover(request):
        if telemetry is None:
            raise web.HTTPServiceUnavailable(text="Telemetry collector not started")
        try:
            return web.json_response(await telemetry.discover())
        except SourceError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from None
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            raise web.HTTPServiceUnavailable(text="Sensor discovery failed; check the Home Assistant API") from None

    async def asset(request):
        name = request.match_info.get("name", "index.html")
        if name not in ("index.html", "style.css", "dashboard.js", "telemetry.js", "mark.svg"):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_get("/api/status", status)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/insights", trends)
    app.router.add_get("/api/incidents/{id}", incident_detail)
    app.router.add_get("/api/export.csv", export)
    app.router.add_get("/api/telemetry", sources)
    app.router.add_get("/api/traffic", traffic)
    app.router.add_get("/api/clients/{key}", client)
    app.router.add_get("/api/clients/{key}/dns", dns)
    app.router.add_get("/api/clients/{key}/activity", client_activity)
    app.router.add_get("/unifi-console", unifi_console)
    app.router.add_get("/api/controls", control_options)
    app.router.add_post("/api/clients/{key}/{kind:dns-actions|router-actions}/{operation}", confirmed_action)
    app.router.add_post("/api/discover", discover)
    app.router.add_post("/api/mute", mute)
    app.router.add_post("/api/test-sound", test_sound)
    app.router.add_post("/api/test-alerts", test_sound)
    app.router.add_get("/", asset)
    app.router.add_get("/{name}", asset)
    return app

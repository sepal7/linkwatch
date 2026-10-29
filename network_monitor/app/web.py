import asyncio
import json
import math
import time
from pathlib import Path

from aiohttp import web

from .service import Monitor
from .insights import incident, insights

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


def create_app(monitor: Monitor, *, development=False) -> web.Application:
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

    async def asset(request):
        name = request.match_info.get("name", "index.html")
        if name not in ("index.html", "style.css", "dashboard.js", "mark.svg"):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_get("/api/status", status)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/insights", trends)
    app.router.add_get("/api/incidents/{id}", incident_detail)
    app.router.add_get("/api/export.csv", export)
    app.router.add_post("/api/mute", mute)
    app.router.add_post("/api/test-sound", test_sound)
    app.router.add_post("/api/test-alerts", test_sound)
    app.router.add_get("/", asset)
    app.router.add_get("/{name}", asset)
    return app

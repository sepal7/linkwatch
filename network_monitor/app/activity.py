"""On-demand controller activity evidence, never browsing or screen history."""

import asyncio
import re
import time

import aiohttp

from .telemetry import SourceError, client_key, number, object_data, safe_text


class Activity:
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.lock = asyncio.Lock()
        self.cache = {}
        self.expiry = None

    def prune(self):
        now = time.monotonic()
        self.cache = {k: v for k, v in self.cache.items() if now - v["created"] < 60}
        self.expiry = None
        if self.cache:
            delay = max(0.1, min(v["created"] + 60 - now for v in self.cache.values()))
            self.expiry = asyncio.get_running_loop().call_later(delay, self.prune)

    async def close(self, app=None):
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None
        self.cache.clear()

    def identity(self, key):
        t = self.telemetry
        if not t.config.unifi_activity_enabled:
            raise SourceError("Enable unifi_activity_enabled in app Configuration for on-demand application reads")
        if not t.sources["legacy"]["enabled"]:
            raise SourceError("Application traffic needs the dedicated local read-only UniFi username and password")
        device = t.devices.get(key)
        if not device:
            raise SourceError("Device no longer retained")
        mac = device.get("mac")
        if (not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac)
                or client_key(mac) != key):
            raise SourceError("Device has no verified controller MAC identity")
        if (not device["present"] or time.time() - device["last_seen"] >
                t.config.telemetry_interval_seconds * 3):
            raise SourceError("Device inventory is stale; activity attribution is paused")
        if not any(s["enabled"] and not s["error"] and s["ts"] is not None and
                   time.time() - s["ts"] <= t.config.telemetry_interval_seconds * 3
                   for n, s in t.sources.items() if n in ("unifi", "legacy")):
            raise SourceError("Controller inventory is unavailable; activity attribution is paused")
        return mac.lower(), device["binding_start"]

    async def names(self, kind, identifiers):
        t = self.telemetry
        if not t.config.unifi_api_key or not identifiers:
            return {}
        result = await t.request(
            t.session, "GET", t.config.unifi_url.rstrip("/") +
            "/proxy/network/integration/v1/dpi/" + kind,
            headers={"X-API-KEY": t.config.unifi_api_key}, ssl=t.fingerprint,
            params={"filter": "id.in(" + ",".join(str(i) for i in sorted(identifiers)) + ")",
                    "limit": 200, "offset": 0})
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) > 200:
            raise SourceError("Application name catalog schema is unsupported")
        names = {}
        for row in rows:
            row = object_data(row)
            identifier, name = row.get("id"), safe_text(row.get("name"), 120)
            if type(identifier) is int and identifier in identifiers and name:
                names[identifier] = name
        return names

    async def snapshot(self, key, seconds):
        if type(seconds) is not int or seconds not in (1800, 3600, 86400):
            raise SourceError("Choose an activity window of 30 minutes, 1 hour or 24 hours")
        async with asyncio.timeout(20), self.lock:
            identity = self.identity(key)
            now = time.time()
            tick = time.monotonic()
            self.cache = {k: v for k, v in self.cache.items() if tick - v["created"] < 60}
            cached = self.cache.get((key, seconds))
            if cached and cached["identity"] == identity and tick - cached["created"] < 30:
                return cached["data"]
            mac = identity[0]
            start = now - seconds
            result = await self.telemetry.legacy_request(
                "/v2/api/site/" + self.telemetry.config.unifi_legacy_site + "/traffic/" + mac,
                params={"start": int(start * 1000), "end": int(now * 1000),
                        "includeUnidentified": "true", "mac": mac})
            clients = result.get("client_usage_by_app")
            if not isinstance(clients, list) or len(clients) > 500:
                raise SourceError("Controller application traffic is unsupported; check Network version and Traffic Identification")
            matching = []
            for row in clients:
                row = object_data(row)
                returned_mac = object_data(row.get("client")).get("mac")
                if not isinstance(returned_mac, str):
                    raise SourceError("Controller activity client MAC is missing")
                if returned_mac.lower() == mac:
                    matching.append(row)
            if len(matching) > 1:
                raise SourceError("Controller returned ambiguous duplicate device activity")
            rows = matching[0].get("usage_by_app") if matching else []
            if not isinstance(rows, list) or len(rows) > 200:
                raise SourceError("Application response exceeds 200 rows or has an unsupported schema")
            applications = []
            seen = set()
            for row in rows:
                row = object_data(row)
                app, category = row.get("application"), row.get("category")
                if (type(app) is not int or type(category) is not int or
                        app < 0 or category < 0 or (app, category) in seen):
                    raise SourceError("Application IDs are invalid or duplicated; cannot safely total traffic")
                seen.add((app, category))
                item = {"application_id": app, "category_id": category}
                for source, target in (("bytes_received", "received_bytes"),
                                       ("bytes_transmitted", "transmitted_bytes"),
                                       ("total_bytes", "total_bytes"),
                                       ("activity_seconds", "controller_activity_seconds")):
                    raw = row.get(source)
                    value = number(raw)
                    if raw is not None and (value is None or value < 0):
                        raise SourceError("Controller returned an invalid activity measurement")
                    item[target] = value
                applications.append(item)
            catalog_error = ("API key not configured; application/category IDs are shown"
                             if applications and not self.telemetry.config.unifi_api_key else None)
            app_names, category_names = {}, {}
            try:
                app_names, category_names = await asyncio.gather(
                    self.names("applications", {r["application_id"] for r in applications}),
                    self.names("categories", {r["category_id"] for r in applications}))
            except (SourceError, aiohttp.ClientError, TimeoutError, ValueError, TypeError):
                catalog_error = "Name catalogs unavailable; numeric IDs are shown, not guessed labels"
            for item in applications:
                item["application"] = app_names.get(item["application_id"])
                item["category"] = category_names.get(item["category_id"])
            if self.identity(key) != identity:
                raise SourceError("Device identity changed during activity read; reload device details")
            data = {
                "from": start, "to": now, "fetched": time.time(), "applications": applications,
                "catalog_error": catalog_error,
                "status": "reported" if applications else "no_visible_activity",
                "scope": "UniFi controller-classified traffic for this MAC in the requested window. "
                         "The API does not provide complete destination flows or a verified WAN-only scope.",
                "warning": "Not browsing history, screen content or proof of active gaming/viewing. "
                           "Controller activity seconds are an opaque estimate and must not be added across applications. "
                           "Background traffic, encrypted tunnels, classification delay and unrecognized apps limit visibility. "
                           "An empty result does not mean no internet use. Existing router and DNS restrictions are not changed.",
                "identity_note": "Matched by exact controller MAC, not a person. Private/randomized MAC changes create "
                                 "separate devices; do not merge by name or IP. This window can predate Linkwatch observation.",
                "private_mac": bool(int(mac[:2], 16) & 2),
            }
            if len(self.cache) >= 16:
                oldest = min(self.cache, key=lambda k: self.cache[k]["created"])
                del self.cache[oldest]
            self.cache[(key, seconds)] = {"identity": identity, "data": data, "created": time.monotonic()}
            if self.expiry is None:
                self.prune()
            return data

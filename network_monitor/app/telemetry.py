"""Optional read-only collectors. Device payloads stay local and never enter logs."""

import asyncio
import hashlib
import ipaddress
import json
import math
import time
from datetime import datetime
from urllib.parse import quote

import aiohttp

from .config import Config
from .storage import Store


class SourceError(ValueError):
    pass


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


def safe_text(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def object_data(value):
    if not isinstance(value, dict):
        raise SourceError("API object schema is unsupported")
    return value


def client_key(value):
    return hashlib.sha256(value.lower().encode()).hexdigest()[:24]


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


class Telemetry:
    def __init__(self, config: Config, store: Store, token: str):
        self.config, self.store, self.token = config, store, token
        self.sources = {}
        self.session = None
        self.unifi_session = None
        self.pihole_sid = None
        self.pihole_auth_lock = asyncio.Lock()
        self.previous_counters = {}
        self.previous_clients = None
        self.devices = {}
        self.dns_lock = asyncio.Lock()
        self.dns_cache = {}
        self.fingerprint = (aiohttp.Fingerprint(bytes.fromhex(config.unifi_tls_fingerprint))
                            if config.unifi_tls_fingerprint else True)
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS telemetry_time ON telemetry(ts);
            CREATE TABLE IF NOT EXISTS client_events (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, client TEXT NOT NULL, event TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS client_events_key ON client_events(client,ts);
            CREATE TABLE IF NOT EXISTS client_samples (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, client TEXT NOT NULL,
                rx_bps REAL, tx_bps REAL, signal REAL);
            CREATE INDEX IF NOT EXISTS client_samples_key ON client_samples(client,ts);
            CREATE INDEX IF NOT EXISTS client_samples_time ON client_samples(ts);
        """)
        for name, enabled in (
                ("unifi", bool(config.unifi_url and config.unifi_api_key)),
                ("legacy", bool(config.unifi_url and config.unifi_username)),
                ("pihole", bool(config.pihole_url)),
                ("starlink", bool(config.starlink_entities)),
                ("internet", bool(config.internet_entities))):
            self.sources[name] = {"enabled": enabled, "ts": None, "error": None, "data": None}

    async def request(self, session, method, url, *, array=False, **kwargs):
        async with session.request(method, url, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=6), **kwargs) as response:
            if response.status >= 300:
                raise SourceError(f"HTTP {response.status}; check credentials, permissions and API version")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise SourceError("Response exceeds the 2 MiB safety limit")
            try:
                result = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                raise SourceError("API did not return valid JSON") from None
            if not isinstance(result, list if array else dict):
                raise SourceError("API returned an unexpected response shape")
            return result

    def rate(self, key, now, down, up, uptime=None):
        previous = self.previous_counters.get(key)
        self.previous_counters[key] = (now, down, up, uptime)
        if (previous is None or down is None or up is None or previous[1] is None or
                previous[2] is None or not 0 < now - previous[0] <= self.config.telemetry_interval_seconds * 3 or
                down < previous[1] or up < previous[2] or
                (uptime is not None and previous[3] is not None and uptime < previous[3])):
            return None, None
        seconds = now - previous[0]
        return (down - previous[1]) * 8 / seconds, (up - previous[2]) * 8 / seconds

    async def official_list(self, path):
        data = []
        for offset in range(0, 500, 100):
            result = await self.request(
                self.session, "GET", self.config.unifi_url.rstrip("/") +
                "/proxy/network/integration/v1/" + path,
                headers={"X-API-KEY": self.config.unifi_api_key},
                params={"offset": offset, "limit": 100}, ssl=self.fingerprint)
            rows = result.get("data")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise SourceError("UniFi collection schema is unsupported")
            data.extend(rows)
            count = result.get("totalCount")
            if type(count) is not int or count < 0:
                raise SourceError("UniFi pagination count is missing or invalid")
            if len(data) >= count:
                return data
        raise SourceError("More than 500 records; narrow the configured site")

    async def unifi(self):
        sites = await self.official_list("sites")
        site = self.config.unifi_site_id
        if not site:
            if len(sites) != 1:
                raise SourceError("Set unifi_site_id when the controller has multiple sites")
            site = sites[0].get("id", "")
        if not any(row.get("id") == site for row in sites):
            raise SourceError("Configured UniFi site not found")
        rows, infrastructure = await asyncio.gather(
            self.official_list(f"sites/{quote(site, safe='')}/clients"),
            self.official_list(f"sites/{quote(site, safe='')}/devices"))
        devices = []
        for row in rows:
            identity = row.get("macAddress") or row.get("id")
            if not isinstance(identity, str):
                raise SourceError("UniFi client identity is missing")
            devices.append({
                "key": client_key(identity), "name": safe_text(row.get("name")) or "Unnamed client",
                "ip": safe_text(row.get("ipAddress")), "mac": safe_text(row.get("macAddress")),
                "connection": safe_text(row.get("type")), "connected_at": epoch(row.get("connectedAt")),
                "uplink": safe_text(row.get("uplinkDeviceId")), "origin": "official",
            })
        semaphore = asyncio.Semaphore(3)

        async def device_stats(device):
            async with semaphore:
                try:
                    stats = await self.request(
                        self.session, "GET", self.config.unifi_url.rstrip("/") +
                        f"/proxy/network/integration/v1/sites/{quote(site, safe='')}/devices/" +
                        quote(str(device.get("id", "")), safe="") + "/statistics/latest",
                        headers={"X-API-KEY": self.config.unifi_api_key}, ssl=self.fingerprint)
                    return {"name": safe_text(device.get("name")), "id": device.get("id"),
                            "cpu_percent": number(stats.get("cpuUtilizationPct")),
                            "memory_percent": number(stats.get("memoryUtilizationPct")),
                            "uptime_seconds": number(stats.get("uptimeSec")),
                            "radios": [{"frequency_ghz": number(object_data(r).get("frequencyGHz")),
                                        "tx_retries_percent": number(object_data(r).get("txRetriesPct"))}
                                       for r in object_data(stats.get("interfaces", {})).get("radios", [])[:8]]}
                except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
                    return {"name": safe_text(device.get("name")), "id": device.get("id"),
                            "error": "Device statistics unavailable"}

        stats = await asyncio.gather(*(device_stats(d) for d in infrastructure[:8]))
        return {"clients": devices, "infrastructure": stats, "infrastructure_total": len(infrastructure),
                "capability": "Inventory; per-client traffic is not in this API"}

    async def legacy(self):
        base = self.config.unifi_url.rstrip("/")
        if not self.unifi_session.cookie_jar:
            await self.request(self.unifi_session, "POST", base + "/api/auth/login",
                               json={"username": self.config.unifi_username,
                                     "password": self.config.unifi_password}, ssl=self.fingerprint)
        path = base + "/proxy/network/api/s/" + quote(self.config.unifi_legacy_site, safe="")
        try:
            clients = await self.request(self.unifi_session, "GET", path + "/stat/sta",
                                         ssl=self.fingerprint)
            health = await self.request(self.unifi_session, "GET", path + "/stat/health",
                                        ssl=self.fingerprint)
        except SourceError:
            self.unifi_session.cookie_jar.clear()
            self.previous_counters.clear()
            raise
        for result in (clients, health):
            if object_data(result.get("meta")).get("rc") != "ok" or not isinstance(result.get("data"), list):
                raise SourceError("Legacy UniFi response schema is unsupported")
        now = time.time()
        rows = []
        if len(clients["data"]) > 500:
            raise SourceError("Legacy site exceeds the 500-client limit")
        for row in clients["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("mac"), str):
                raise SourceError("Legacy client identity is missing")
            key = client_key(row["mac"])
            # Keep controller RX/TX labels; these counters can include LAN traffic.
            rx = number(row.get("wired-rx_bytes" if row.get("is_wired") else "rx_bytes"))
            tx = number(row.get("wired-tx_bytes" if row.get("is_wired") else "tx_bytes"))
            rx_bps, tx_bps = self.rate(key, now, rx, tx, number(row.get("uptime")))
            rows.append({
                "key": key, "name": safe_text(row.get("name") or row.get("hostname")) or "Unnamed client",
                "ip": safe_text(row.get("ip")), "mac": row["mac"][:64],
                "connection": "WIRED" if row.get("is_wired") else "WIRELESS",
                "connected_at": number(row.get("assoc_time")), "origin": "legacy",
                "rx_bps": rx_bps, "tx_bps": tx_bps,
                "signal_dbm": number(row.get("signal")), "channel": number(row.get("channel")),
                "link_mbps": number(row.get("wired_rate_mbps")),
                "ap": safe_text(row.get("ap_mac") or row.get("sw_mac")),
                "port": number(row.get("sw_port")), "wifi_standard": safe_text(row.get("radio_proto")),
                "satisfaction": number(row.get("satisfaction")),
            })
        for key in self.previous_counters.keys() - {row["key"] for row in rows} - {"wan"}:
            del self.previous_counters[key]
        wan = next((row for row in health["data"]
                    if isinstance(row, dict) and row.get("subsystem") == "wan"), {})
        down, up = self.rate("wan", now, number(wan.get("rx_bytes")),
                             number(wan.get("tx_bytes")), number(wan.get("uptime")))
        return {"clients": rows, "download_bps": down, "upload_bps": up,
                "wan_status": safe_text(wan.get("status")),
                "capability": "Version-dependent counters; client RX/TX includes local traffic"}

    async def pihole_get(self, path, **params):
        async with self.pihole_auth_lock:
            if not self.pihole_sid:
                auth = await self.request(
                    self.session, "POST", self.config.pihole_url.rstrip("/") + "/api/auth",
                    json={"password": self.config.pihole_password})
                auth_session = object_data(auth.get("session"))
                if not auth_session.get("valid") or not isinstance(auth_session.get("sid"), str):
                    raise SourceError("Pi-hole did not authorize an API session")
                self.pihole_sid = auth_session["sid"]
        try:
            return await self.request(
                self.session, "GET", self.config.pihole_url.rstrip("/") + "/api/" + path,
                headers={"X-FTL-SID": self.pihole_sid}, params=params)
        except SourceError:
            self.pihole_sid = None
            raise

    async def pihole(self):
        result = await self.pihole_get("stats/summary")
        queries = result.get("queries")
        if not isinstance(queries, dict):
            raise SourceError("Pi-hole v6 summary schema is unsupported")
        return {"queries": number(queries.get("total")), "blocked": number(queries.get("blocked")),
                "percent_blocked": number(queries.get("percent_blocked")),
                "scope": "Pi-hole retained summary, not the selected chart period"}

    async def starlink(self):
        return await self.read_entities(self.config.starlink_entities)

    async def internet(self):
        return await self.read_entities(self.config.internet_entities)

    async def read_entities(self, mappings):
        if not self.token:
            raise SourceError("Home Assistant API token unavailable")
        values, unavailable = {}, []
        async def read_sensor(mapping):
            metric, entity = mapping.split("=")
            row = await self.request(
                self.session, "GET", "http://supervisor/core/api/states/" + entity,
                headers={"Authorization": f"Bearer {self.token}"})
            return metric, row

        readings = await asyncio.gather(*(read_sensor(m) for m in mappings))
        for metric, row in readings:
            state = row.get("state")
            reported = epoch(row.get("last_reported"))
            if state in ("unknown", "unavailable", None) or (
                    reported is not None and time.time() - reported > 120):
                unavailable.append(metric)
                continue
            if metric in ("connected", "update", "obstructed", "sleeping"):
                if state not in ("on", "off"):
                    unavailable.append(metric)
                else:
                    values[metric] = state == "on"
            elif metric == "restart":
                if epoch(state) is not None:
                    values[metric] = state[:80]
                else:
                    unavailable.append(metric)
            else:
                try:
                    value = float(state)
                except (ValueError, TypeError):
                    unavailable.append(metric)
                    continue
                unit = object_data(row.get("attributes", {})).get("unit_of_measurement")
                factors = ({"bit/s": 1, "bps": 1, "kbit/s": 1000, "Mbit/s": 1e6,
                            "Gbit/s": 1e9, "B/s": 8, "kB/s": 8000, "MB/s": 8e6}
                           if metric in ("download", "upload") else
                           {"ms": 1, "s": 1000} if metric == "latency" else {"%": 1})
                if (unit not in factors or not math.isfinite(value) or value < 0 or
                        metric == "loss" and value > 100):
                    unavailable.append(metric)
                else:
                    converted = value * factors[unit]
                    if math.isfinite(converted):
                        values[metric] = converted
                    else:
                        unavailable.append(metric)
        if not values:
            raise SourceError("No usable sensor values; check mappings and HA integration")
        return {**values, "unavailable_metrics": unavailable}

    async def discover(self):
        if not self.config.ha_discovery_enabled:
            raise SourceError("Enable ha_discovery_enabled in app Configuration before discovery")
        if not self.token or self.session is None:
            raise SourceError("Home Assistant API connection is unavailable")
        states = await self.request(
            self.session, "GET", "http://supervisor/core/api/states", array=True,
            headers={"Authorization": f"Bearer {self.token}"})
        candidates = []
        units = {"bit/s", "bps", "kbit/s", "Mbit/s", "Gbit/s", "B/s", "kB/s", "MB/s"}
        for row in states:
            if not isinstance(row, dict):
                continue
            entity = row.get("entity_id", "")
            attributes = row.get("attributes", {})
            if not isinstance(entity, str) or not isinstance(attributes, dict):
                continue
            unit = attributes.get("unit_of_measurement")
            is_rate = entity.startswith("sensor.") and unit in units
            is_dish = (entity.startswith(("sensor.starlink_", "binary_sensor.starlink_")) and
                       any(entity.endswith("_" + suffix) for suffix in (
                           "ping", "ping_drop_rate", "connected", "update_available",
                           "last_restart", "obstructed", "sleep")))
            if is_rate or is_dish:
                candidates.append({"entity_id": entity, "unit": unit if isinstance(unit, str) else None,
                                   "kind": "Traffic rate" if is_rate else "Possible Starlink sensor"})
        return {"candidates": candidates[:200], "truncated": len(candidates) > 200,
                "note": "Candidates only, not verified WAN metrics. Choose which entities represent your "
                        "whole internet link and map download/upload explicitly. No LAN scanning or auto-configuration."}

    def commit_clients(self, now):
        official = self.sources["unifi"]
        legacy = self.sources["legacy"]
        valid = [s for s in (official, legacy) if s["data"] and not s["error"] and
                 s["ts"] is not None and now - s["ts"] <= self.config.telemetry_interval_seconds * 3]
        if not valid:
            self.previous_clients = None
            self.previous_counters.clear()
            return
        # Legacy has richer telemetry; official inventory remains independently usable.
        rows = valid[-1]["data"]["clients"]
        observed = {row["key"]: row for row in rows}
        if self.previous_clients is not None:
            for key in observed.keys() ^ self.previous_clients:
                if key in observed:
                    event = "Appeared in connected-client list"
                else:
                    event = "Absent from connected-client list; cause unconfirmed"
                self.store.db.execute("INSERT INTO client_events(ts,client,event) VALUES (?,?,?)",
                                      (now, key, event))
        for key, row in observed.items():
            previous = self.devices.get(key, {})
            binding_start = previous.get("binding_start", now)
            if (previous.get("ip") != row.get("ip") or self.previous_clients is None or
                    key not in self.previous_clients or
                    now - previous.get("last_seen", 0) > self.config.telemetry_interval_seconds * 3):
                binding_start = now
            self.devices[key] = {**row, "last_seen": now, "binding_start": binding_start,
                                 "present": True}
        for key in self.devices.keys() - observed.keys():
            self.devices[key]["present"] = False
        self.previous_clients = set(observed)
        self.devices = dict(sorted(self.devices.items(),
                                   key=lambda item: item[1]["last_seen"], reverse=True)[:500])

    async def poll_source(self, name, collect):
        source = self.sources[name]
        try:
            async with asyncio.timeout(20):
                result = await collect()
        except (aiohttp.ClientError, TimeoutError, SourceError, ValueError, TypeError, KeyError) as exc:
            # Never include raw exception text, URLs, credentials or response bodies.
            message = str(exc) if isinstance(exc, SourceError) else (
                f"{type(exc).__name__}; check connection, certificate and API compatibility")
            if source["error"] != message:
                self.store.log(time.time(), "source_error", f"{name}: {message}")
            source["error"] = message
        else:
            if source["error"]:
                self.store.log(time.time(), "source_recovered",
                               f"{name}: reads recovered; missing observations remain unknown")
            if name == "starlink" and source["data"]:
                old = source["data"]
                if old.get("restart") and result.get("restart") and old["restart"] != result["restart"]:
                    self.store.log(time.time(), "source_context",
                                   "starlink: reported restart time changed; cause unconfirmed")
                if result.get("update") is not None and old.get("update") != result["update"]:
                    self.store.log(time.time(), "source_context",
                                   "starlink: update availability changed; not proof of an outage cause")
            source.update(ts=time.time(), error=None, data=result)

    def sample(self, now):
        with self.store.db:
            self.commit_clients(now)
            for key, device in self.devices.items():
                if device["present"] and now - device["last_seen"] < self.config.telemetry_interval_seconds:
                    self.store.db.execute(
                        "INSERT INTO client_samples(ts,client,rx_bps,tx_bps,signal) VALUES (?,?,?,?,?)",
                        (now, key, device.get("rx_bps"), device.get("tx_bps"), device.get("signal_dbm")))
            for name in ("legacy", "starlink", "internet", "pihole"):
                source = self.sources[name]
                if (source["ts"] is None or source["error"] or
                        now - source["ts"] > self.config.telemetry_interval_seconds * 3):
                    continue
                # Persist only selected numeric/status values, never credentials/raw payloads or DNS logs.
                data = {k: v for k, v in source["data"].items()
                        if k not in ("clients", "capability", "scope")}
                self.store.db.execute("INSERT INTO telemetry(ts,source,data) VALUES (?,?,?)",
                                      (now, name, json.dumps(data)))
            cutoff = now - min(self.config.retention_days, 7) * 86400
            for table, cap in (("telemetry", 50000), ("client_events", 10000), ("client_samples", 100000)):
                self.store.db.execute(f"DELETE FROM {table} WHERE ts<?", (cutoff,))
                self.store.db.execute(
                    f"DELETE FROM {table} WHERE id <= COALESCE("
                    f"(SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?),0)", (cap,))

    def snapshot(self):
        now = time.time()
        sources = {}
        for name, source in self.sources.items():
            stale = source["ts"] is None or now - source["ts"] > self.config.telemetry_interval_seconds * 3
            sources[name] = {**source, "stale": stale or bool(source["error"])}
            if sources[name]["data"]:
                sources[name]["data"] = {k: v for k, v in source["data"].items() if k != "clients"}
        valid_inventory = any(not sources[name]["stale"] for name in ("unifi", "legacy"))
        clients = [{**row, "stale": not valid_inventory or not row["present"]}
                   for row in self.devices.values()]
        return {"sources": sources, "clients": clients,
                "discovery_enabled": self.config.ha_discovery_enabled,
                "interval_seconds": self.config.telemetry_interval_seconds}

    def history(self, start, end):
        # Aggregate all retained points into at most 240 buckets per source; do not silently truncate.
        seconds = max(1, math.ceil((end - start) / 240))
        rows = self.store.db.execute("""
            SELECT source, CAST((ts-?)/? AS INTEGER) AS bucket,
                MIN(ts) AS ts, MAX(ts) AS last_ts, COUNT(*) AS samples,
                AVG(json_extract(data,'$.download')) AS dish_download,
                AVG(json_extract(data,'$.upload')) AS dish_upload,
                AVG(json_extract(data,'$.download_bps')) AS wan_download,
                AVG(json_extract(data,'$.upload_bps')) AS wan_upload
            FROM telemetry WHERE ts>=? AND ts<=? GROUP BY source,bucket ORDER BY ts
        """, (start, seconds, start, end))
        return {"bucket_seconds": seconds, "points": [dict(row) for row in rows]}

    def dns_binding(self, key):
        device = self.devices.get(key)
        if not device:
            raise SourceError("Client no longer available")
        if not self.sources["pihole"]["enabled"]:
            raise SourceError("Pi-hole is not configured")
        if not device["present"] or time.time() - device["last_seen"] > self.config.telemetry_interval_seconds * 3:
            raise SourceError("Client identity is stale; DNS attribution is paused")
        if not any(s["enabled"] and not s["error"] and s["ts"] is not None and
                   time.time() - s["ts"] <= self.config.telemetry_interval_seconds * 3
                   for n, s in self.sources.items() if n in ("unifi", "legacy")):
            raise SourceError("Client source is unavailable; DNS attribution is paused")
        try:
            ip = str(ipaddress.ip_address(device.get("ip", "")))
        except ValueError:
            raise SourceError("Client has no usable IP for DNS matching") from None
        if sum(d.get("ip") == ip and d.get("present") for d in self.devices.values()) != 1:
            raise SourceError("IP is shared or ambiguous; cannot assign DNS queries")
        return device, ip

    async def dns(self, key):
        async with self.dns_lock:
            device, ip = self.dns_binding(key)
            cached = self.dns_cache.get(key)
            if (cached and time.time() - cached["fetched"] < 15 and cached["ip"] == ip and
                    cached["binding_start"] == device["binding_start"]):
                return cached
            start = max(device["binding_start"], time.time() - 1800)
            result = await self.pihole_get("queries", client_ip=ip, length=100,
                                          **{"from": start, "until": time.time()})
            current, current_ip = self.dns_binding(key)
            if current_ip != ip or current["binding_start"] != device["binding_start"]:
                raise SourceError("Client identity changed during DNS read; retry with fresh details")
            queries = result.get("queries")
            if not isinstance(queries, list):
                raise SourceError("Pi-hole query schema is unsupported")
            clean = []
            for query in queries[:100]:
                query = object_data(query)
                if object_data(query.get("client", {})).get("ip") != ip:
                    continue
                ts = number(query.get("time"))
                if ts is None or ts < start:
                    continue
                reply = object_data(query.get("reply", {}))
                reply_seconds = number(reply.get("time"))
                clean.append({
                    "ts": ts, "domain": safe_text(query.get("domain"), 253),
                    "type": safe_text(query.get("type"), 20),
                    "status": safe_text(query.get("status"), 80),
                    "reply": safe_text(reply.get("type"), 40),
                    "reply_ms": reply_seconds * 1000 if reply_seconds is not None and reply_seconds > 0 else None,
                })
            data = {"queries": clean, "from": start, "fetched": time.time(), "ip": ip,
                    "binding_start": device["binding_start"],
                    "limit": 100, "matched_by": "Current unique IP, since continuously observed binding",
                    "warning": "DNS requests are not browsing history or proof of a successful connection. "
                               "Router-forwarded DNS, alternate resolvers, IPv6 and encrypted DNS can hide client activity."}
            self.dns_cache = {k: v for k, v in self.dns_cache.items()
                              if time.time() - v["fetched"] < 60}
            self.dns_cache[key] = data
            return data

    def client_detail(self, key):
        device = self.devices.get(key)
        if device is None:
            return None
        events = [dict(row) for row in self.store.db.execute(
            "SELECT ts,event FROM client_events WHERE client=? ORDER BY id DESC LIMIT 50", (key,))]
        samples = [dict(row) for row in self.store.db.execute("""
            SELECT MIN(ts) AS ts, AVG(rx_bps) AS rx_bps, AVG(tx_bps) AS tx_bps,
                AVG(signal) AS signal FROM client_samples WHERE client=? AND ts>=?
            GROUP BY CAST(ts/15 AS INTEGER) ORDER BY ts
        """, (key, time.time() - 1800))]
        network_context = [dict(row) for row in self.store.db.execute("""
            SELECT id,start,end,state FROM intervals WHERE end>=? AND state!='online'
            ORDER BY id DESC LIMIT 20
        """, (max(device["binding_start"], time.time() - 1800),))]
        inventory_ok = any(s["enabled"] and not s["error"] and s["ts"] is not None and
                           time.time() - s["ts"] <= self.config.telemetry_interval_seconds * 3
                           for n, s in self.sources.items() if n in ("unifi", "legacy"))
        return {"client": {**device, "stale": not inventory_ok or time.time() - device["last_seen"] >
                           self.config.telemetry_interval_seconds * 3 or not device["present"]},
                "events": events, "samples": samples, "network_context": network_context,
                "scope": "Controller observations, not a probe originating from this device. "
                         "Absence, low signal or a DNS block alone does not prove internet failure."}

    async def run(self):
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session, \
                aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as unifi_session:
            self.session, self.unifi_session = session, unifi_session
            if not any(s["enabled"] for s in self.sources.values()):
                await asyncio.Event().wait()
            while True:
                started = time.monotonic()
                await asyncio.gather(*(self.poll_source(name, getattr(self, name))
                                       for name, source in self.sources.items() if source["enabled"]))
                self.sample(time.time())
                await asyncio.sleep(max(0, self.config.telemetry_interval_seconds -
                                        (time.monotonic() - started)))

    def completed(self, task):
        if not task.cancelled() and task.exception() is not None:
            for source in self.sources.values():
                if source["enabled"]:
                    source["error"] = "Collector stopped; restart app and inspect compatibility"
            self.store.log(time.time(), "source_error", "Optional telemetry worker stopped")

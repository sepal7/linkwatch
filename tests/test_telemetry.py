import asyncio
import json
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from network_monitor.app.alerts import Alerts
from network_monitor.app.config import Config
from network_monitor.app.service import Monitor
from network_monitor.app.storage import Store
from network_monitor.app.telemetry import SourceError, Telemetry, client_key
from network_monitor.app.web import create_app


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite", 7, 1000)
        self.store.start(time.time())
        self.config = Config(unifi_url="https://controller.example", unifi_api_key="test-key",
                             pihole_url="http://dns.example", pihole_password="test-password")
        self.telemetry = Telemetry(self.config, self.store, "test-supervisor")
        self.telemetry.session = object()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_config_compatibility_validation_and_secret_repr(self):
        self.assertEqual(Config().starlink_entities, [])
        self.assertNotIn("test-key", repr(self.config))
        self.assertNotIn("dns.example", repr(self.config))
        for kwargs in (
                {"unifi_url": "http://controller.example"},
                {"pihole_url": "http://user:password@dns.example"},
                {"pihole_url": "http://dns.example/api"},
                {"unifi_tls_fingerprint": "bad"},
                {"unifi_username": "readonly"},
                {"starlink_entities": ["location=sensor.location"]},
                {"starlink_entities": ["download=sensor.a", "download=sensor.b"]},
                {"telemetry_interval_seconds": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)

    def test_dashboard_views_are_siblings_not_hidden_inside_each_other(self):
        class Views(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.parents = [], {}

            def handle_starttag(self, tag, attrs):
                identifier = dict(attrs).get("id", "")
                if identifier.startswith("view-"):
                    self.parents[identifier] = self.stack[-1]
                if tag not in ("meta", "link", "input", "img", "br"):
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()

        parser = Views()
        parser.feed((Path(__file__).resolve().parents[1] /
                     "network_monitor/app/static/index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.parents), 6)
        self.assertEqual(set(parser.parents.values()), {"main"})

    async def test_dashboard_periods_and_setup_guidance(self):
        class Periods(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_range, self.values, self.defaults = False, [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "select":
                    self.in_range = attrs.get("id") == "range"
                if tag == "option" and self.in_range:
                    self.values.append(int(attrs["value"]))
                    if "selected" in attrs:
                        self.defaults.append(int(attrs["value"]))

            def handle_endtag(self, tag):
                if tag == "select":
                    self.in_range = False

        html = (Path(__file__).resolve().parents[1] /
                "network_monitor/app/static/index.html").read_text(encoding="utf-8")
        parser = Periods()
        parser.feed(html)
        self.assertEqual(parser.values, [60, 300, 600, 900, 1800, 3600, 7200,
                                         10800, 21600, 43200, 86400, 172800, 259200, 604800])
        self.assertEqual(parser.defaults, [1800])
        self.assertIn('id="devices-setup"', html)
        self.assertIn("unifi_api_key", html)
        self.assertIn("Save and Restart", html)
        monitor = Monitor(self.config, self.store, Alerts(self.config, self.store, "", object()))
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry))) as client:
            now = time.time()
            for seconds in parser.values:
                for endpoint in ("history", "insights", "traffic"):
                    with self.subTest(seconds=seconds, endpoint=endpoint):
                        response = await client.get(f"/api/{endpoint}?from={now-seconds}&to={now}")
                        self.assertEqual(response.status, 200)

    def test_rates_reset_gaps_and_reboots_are_not_spikes_or_zero(self):
        t = self.telemetry
        self.assertEqual(t.rate("test", 0, 100, 100, 100), (None, None))
        self.assertEqual(t.rate("test", 10, 1100, 600, 110), (800, 400))
        self.assertEqual(t.rate("test", 20, 50, 50, 1), (None, None))
        self.assertEqual(t.rate("test", 200, 50000, 50000, 181), (None, None))
        self.assertEqual(t.rate("test", 199, 55000, 55000), (None, None))

    async def test_unifi_supported_inventory_pagination_and_no_controls(self):
        requests = []

        async def mock(session, method, url, **kwargs):
            requests.append((method, url, kwargs))
            if url.endswith("/sites"):
                return {"data": [{"id": "site-one"}], "totalCount": 1}
            if url.endswith("/clients"):
                offset = kwargs["params"]["offset"]
                rows = [{"id": str(i), "name": f"Test client {i}", "ipAddress": f"192.0.2.{i+1}",
                         "type": "WIRED", "macAddress": f"02:00:00:00:00:{i:02x}"}
                        for i in range(offset, min(offset + 100, 101))]
                return {"data": rows, "totalCount": 101}
            return {"data": [], "totalCount": 0}

        self.telemetry.request = mock
        result = await self.telemetry.unifi()
        self.assertEqual(len(result["clients"]), 101)
        self.assertTrue(all(method == "GET" for method, _, _ in requests))
        self.assertEqual(requests[0][2]["headers"]["X-API-KEY"], "test-key")
        self.assertNotIn("rx_bps", result["clients"][0])

    async def test_official_multisite_requires_explicit_selection(self):
        self.telemetry.official_list = AsyncMock(return_value=[{"id": "a"}, {"id": "b"}])
        with self.assertRaisesRegex(SourceError, "multiple sites"):
            await self.telemetry.unifi()

    async def test_official_pagination_rejects_missing_or_invalid_total(self):
        for result in ({"data": []}, {"data": [], "totalCount": "many"}):
            self.telemetry.request = AsyncMock(return_value=result)
            with self.assertRaisesRegex(SourceError, "pagination count"):
                await self.telemetry.official_list("sites")

    async def test_legacy_auth_and_read_endpoints_only(self):
        self.telemetry.config = Config(unifi_url="https://controller.example",
                                       unifi_username="reader", unifi_password="test-password")
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            self.telemetry.unifi_session = session
            self.telemetry.request = AsyncMock(side_effect=[
                {}, {"meta": {"rc": "ok"}, "data": [{"mac": "02:00:00:00:00:01",
                                                    "rx_bytes": 100, "tx_bytes": 200}]},
                {"meta": {"rc": "ok"}, "data": [{"subsystem": "wan", "rx_bytes": 1000,
                                                "tx_bytes": 2000, "uptime": 20}]}])
            result = await self.telemetry.legacy()
            calls = self.telemetry.request.await_args_list
            self.assertTrue(calls[0].args[2].endswith("/api/auth/login"))
            self.assertEqual([c.args[1] for c in calls], ["POST", "GET", "GET"])
            self.assertTrue(calls[1].args[2].endswith("/stat/sta"))
            self.assertTrue(calls[2].args[2].endswith("/stat/health"))
            self.assertIsNone(result["clients"][0]["rx_bps"])
            self.assertIsNone(result["download_bps"])

    async def test_pihole_session_auth_header_and_reauth_after_failure(self):
        self.telemetry.request = AsyncMock(side_effect=[
            {"session": {"valid": True, "sid": "test-sid"}},
            {"queries": {"total": 10, "blocked": 3, "percent_blocked": 30}},
            SourceError("HTTP 401; check credentials")])
        result = await self.telemetry.pihole()
        self.assertEqual(result["queries"], 10)
        calls = self.telemetry.request.await_args_list
        self.assertEqual(calls[0].args[1], "POST")
        self.assertEqual(calls[1].kwargs["headers"], {"X-FTL-SID": "test-sid"})
        with self.assertRaises(SourceError):
            await self.telemetry.pihole()
        self.assertIsNone(self.telemetry.pihole_sid)

    async def test_starlink_allowlist_units_stale_update_and_no_location(self):
        t = Telemetry(Config(starlink_entities=[
            "download=sensor.dish_down", "upload=sensor.dish_up", "loss=sensor.dish_loss",
            "restart=sensor.dish_restart", "update=binary_sensor.dish_update"]), self.store, "token")
        t.session = object()
        t.request = AsyncMock(side_effect=[
            {"state": "12.5", "attributes": {"unit_of_measurement": "Mbit/s", "latitude": 99}},
            {"state": "500", "attributes": {"unit_of_measurement": "kbit/s"}},
            {"state": "unavailable"},
            {"state": "2026-01-01T00:00:00+00:00"},
            {"state": "on"},
        ])
        result = await t.starlink()
        self.assertEqual(result["download"], 12500000)
        self.assertEqual(result["upload"], 500000)
        self.assertTrue(result["update"])
        self.assertIn("loss", result["unavailable_metrics"])
        self.assertNotIn("latitude", json.dumps(result))
        self.assertTrue(all(c.args[1] == "GET" and "/states/" in c.args[2]
                            for c in t.request.await_args_list))

    async def test_starlink_rejects_stale_unknown_units_and_nonfinite_data(self):
        t = Telemetry(Config(starlink_entities=["download=sensor.dish"]), self.store, "token")
        t.session = object()
        for row in ({"state": "nan", "attributes": {"unit_of_measurement": "bit/s"}},
                    {"state": "1e308", "attributes": {"unit_of_measurement": "Gbit/s"}},
                    {"state": "1", "attributes": None},
                    {"state": "1", "attributes": {"unit_of_measurement": "unrecognized"}},
                    {"state": "1", "last_reported": "2020-01-01T00:00:00Z",
                     "attributes": {"unit_of_measurement": "bit/s"}}):
            t.request = AsyncMock(return_value=row)
            with self.assertRaises(SourceError):
                await t.starlink()

        t.config = Config(starlink_entities=["loss=sensor.dish_loss"])
        t.request = AsyncMock(return_value={"state": "101", "attributes": {"unit_of_measurement": "%"}})
        with self.assertRaises(SourceError):
            await t.starlink()

    def observe(self, rows, now=None):
        now = now or time.time()
        self.telemetry.sources["unifi"].update(ts=now, error=None, data={"clients": rows})
        self.telemetry.sample(now)

    async def test_client_dns_uses_only_current_binding_and_filters_returned_rows(self):
        now = time.time()
        key = client_key("02:00:00:00:00:01")
        self.observe([{"key": key, "name": "Synthetic client", "ip": "192.0.2.10"}], now)
        self.telemetry.pihole_get = AsyncMock(return_value={"queries": [
            {"time": now + 1, "domain": "example.org", "status": "GRAVITY",
             "client": {"ip": "192.0.2.10"}, "reply": {"type": "IP", "time": 0.001}},
            {"time": now - 1, "domain": "old-binding.example", "client": {"ip": "192.0.2.10"}},
            {"time": now + 1, "domain": "other.example", "client": {"ip": "192.0.2.11"}},
        ]})
        result = await self.telemetry.dns(key)
        self.assertEqual(len(result["queries"]), 1)
        self.assertEqual(result["queries"][0]["status"], "GRAVITY")
        self.assertEqual(result["queries"][0]["reply_ms"], 1)
        await self.telemetry.dns(key)
        self.telemetry.pihole_get.assert_awaited_once()
        self.assertEqual(self.telemetry.pihole_get.call_args.kwargs["from"], now)
        self.observe([{"key": key, "name": "Synthetic client", "ip": "192.0.2.12"}], now + 2)
        self.assertEqual(self.telemetry.devices[key]["binding_start"], now + 2)
        self.telemetry.devices[key]["binding_start"] = now + 5
        await self.telemetry.dns(key)
        self.assertEqual(self.telemetry.pihole_get.await_count, 2)

    async def test_shared_or_stale_ip_does_not_get_dns_attribution(self):
        self.observe([{"key": "a", "ip": "192.0.2.10"}, {"key": "b", "ip": "192.0.2.10"}])
        with self.assertRaisesRegex(SourceError, "shared"):
            await self.telemetry.dns("a")
        self.telemetry.devices["a"]["last_seen"] = 0
        with self.assertRaisesRegex(SourceError, "stale"):
            await self.telemetry.dns("a")

    async def test_dns_rechecks_binding_after_inflight_request(self):
        self.observe([{"key": "a", "ip": "192.0.2.10"}])

        async def changed(*args, **kwargs):
            self.observe([{"key": "a", "ip": "192.0.2.11"}])
            return {"queries": []}

        self.telemetry.pihole_get = changed
        with self.assertRaisesRegex(SourceError, "identity changed"):
            await self.telemetry.dns("a")
        self.assertEqual(self.telemetry.dns_cache, {})

    async def test_dns_unknown_response_time_is_not_zero_latency(self):
        now = time.time()
        self.observe([{"key": "a", "ip": "192.0.2.10"}], now)
        self.telemetry.pihole_get = AsyncMock(return_value={"queries": [
            {"time": now + 1, "client": {"ip": "192.0.2.10"}, "reply": reply}
            for reply in ({}, {"time": 0}, {"time": 0.012})]})
        result = await self.telemetry.dns("a")
        self.assertEqual([q["reply_ms"] for q in result["queries"]], [None, None, 12])

    def test_client_absence_and_source_failure_are_distinct(self):
        now = time.time()
        self.observe([{"key": "a", "ip": "192.0.2.10"}], now)
        self.observe([], now + 15)
        detail = self.telemetry.client_detail("a")
        self.assertIn("cause unconfirmed", detail["events"][0]["event"])
        self.telemetry.sources["unifi"]["error"] = "Source unavailable"
        self.telemetry.sample(now + 30)
        count = self.store.db.execute("SELECT COUNT(*) FROM client_events").fetchone()[0]
        self.observe([{"key": "a", "ip": "192.0.2.10"}], now + 45)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM client_events").fetchone()[0], count)
        self.assertEqual(self.telemetry.devices["a"]["binding_start"], now + 45)

    async def test_one_collector_failure_is_sanitized_and_others_continue(self):
        async def fail():
            raise aiohttp.ClientConnectionError("private secret from controller.example")

        await asyncio.gather(
            self.telemetry.poll_source("unifi", fail),
            self.telemetry.poll_source("pihole", AsyncMock(return_value={"queries": 42})))
        self.assertEqual(self.telemetry.sources["pihole"]["data"]["queries"], 42)
        status = json.dumps(self.telemetry.snapshot())
        self.assertNotIn("private secret", status)
        self.assertNotIn("controller.example", status)
        self.assertNotIn("test-key", status)
        logs = str(list(self.store.db.execute("SELECT message FROM events")))
        self.assertNotIn("private secret", logs)

    def test_history_aggregates_entire_window_and_prunes(self):
        now = time.time()
        with self.store.db:
            self.store.db.executemany("INSERT INTO telemetry(ts,source,data) VALUES (?,'starlink',?)",
                                     ((now - 3600 + i * 10, json.dumps({"download": i, "upload": 1}))
                                      for i in range(360)))
            self.store.db.execute("INSERT INTO telemetry(ts,source,data) VALUES (?,'starlink','{}')",
                                  (now - 8 * 86400,))
        history = self.telemetry.history(now - 3600, now)
        self.assertLessEqual(len(history["points"]), 241)
        self.assertEqual(sum(p["samples"] for p in history["points"]), 360)
        self.telemetry.sample(now)
        self.assertGreater(self.store.db.execute("SELECT MIN(ts) FROM telemetry").fetchone()[0],
                           now - 7 * 86400)

    async def test_request_limits_redirect_and_error_payload_privacy(self):
        calls = []

        async def handler(request):
            calls.append(request)
            if request.path == "/redirect":
                raise web.HTTPFound("/secret")
            if request.path == "/big":
                return web.Response(body=b"x" * (2 * 1024 * 1024 + 1))
            return web.json_response({"private": "do not log"}, status=401)

        app = web.Application()
        app.router.add_get("/{path}", handler)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            for path, error in (("redirect", "HTTP 302"), ("big", "safety limit"), ("secret", "HTTP 401")):
                with self.subTest(path=path), self.assertRaisesRegex(SourceError, error):
                    await self.telemetry.request(session, "GET", str(server.make_url("/" + path)))
            self.assertEqual(len(calls), 3)

    async def test_ingress_routes_and_missing_client(self):
        monitor = Monitor(self.config, self.store, Alerts(self.config, self.store, "", object()))
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry))) as client:
            response = await client.get("/api/telemetry")
            self.assertEqual(response.status, 200)
            self.assertNotIn("test-password", await response.text())
            self.assertEqual((await client.get("/api/traffic?from=nan")).status, 400)
            self.assertEqual((await client.get("/api/clients/missing")).status, 404)
            self.assertEqual((await client.get("/api/clients/missing/dns")).status, 503)
            self.assertEqual((await client.get("/telemetry.js")).status, 200)
            self.assertEqual((await client.post("/api/discover", json={})).status, 403)
            response = await client.post("/api/discover", json={},
                                         headers={"X-Network-Monitor": "1"})
            self.assertEqual(response.status, 503)
            self.assertIn("ha_discovery_enabled", await response.text())

    async def test_generic_sensor_mapping_and_discovery_are_opt_in_and_filtered(self):
        t = Telemetry(Config(internet_entities=["download=sensor.wan_down"],
                             ha_discovery_enabled=True), self.store, "token")
        t.session = object()
        t.request = AsyncMock(side_effect=[
            {"state": "125", "attributes": {"unit_of_measurement": "MB/s"}},
            [{"entity_id": "sensor.wan_down", "state": "125",
              "attributes": {"unit_of_measurement": "MB/s", "private": "do not return"}},
             {"entity_id": "sensor.home_alarm", "state": "private",
              "attributes": {"unit_of_measurement": "V"}},
             {"entity_id": "sensor.starlink_ping", "state": "10",
              "attributes": {"unit_of_measurement": "ms", "location": "private"}}]])
        self.assertEqual((await t.internet())["download"], 1e9)
        result = await t.discover()
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("home_alarm", json.dumps(result))
        self.assertTrue(t.request.await_args_list[1].kwargs["array"])
        self.assertEqual(t.request.await_args_list[1].args[1], "GET")

    async def test_gap_resets_ip_binding_even_without_intermediate_failed_poll(self):
        now = time.time()
        self.observe([{"key": "a", "ip": "192.0.2.10"}], now - 60)
        self.observe([{"key": "a", "ip": "192.0.2.10"}], now)
        self.assertEqual(self.telemetry.devices["a"]["binding_start"], now)
        self.telemetry.sources["unifi"]["error"] = "Source unavailable"
        with self.assertRaisesRegex(SourceError, "source is unavailable"):
            await self.telemetry.dns("a")
        self.assertTrue(self.telemetry.client_detail("a")["client"]["stale"])

    def test_numeric_history_and_events_survive_restart_but_identity_does_not(self):
        now = time.time()
        self.observe([{"key": "a", "ip": "192.0.2.10", "rx_bps": 8000, "tx_bps": 4000}], now - 15)
        self.observe([], now)
        reopened = Telemetry(self.config, self.store, "token")
        self.assertEqual(reopened.devices, {})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM client_samples").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM client_events").fetchone()[0], 1)
        self.assertTrue(reopened.snapshot()["sources"]["unifi"]["stale"])

    async def test_starlink_update_and_restart_are_context_not_failure_cause(self):
        source = self.telemetry.sources["starlink"]
        source.update(data={"restart": "2026-01-01T00:00:00Z", "update": True})
        await self.telemetry.poll_source("starlink", AsyncMock(
            return_value={"restart": "2026-01-02T00:00:00Z", "update": False}))
        messages = [row[0] for row in self.store.db.execute(
            "SELECT message FROM events WHERE kind='source_context'")]
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("cause unconfirmed" in m for m in messages))
        self.assertTrue(any("not proof" in m for m in messages))


if __name__ == "__main__":
    unittest.main()

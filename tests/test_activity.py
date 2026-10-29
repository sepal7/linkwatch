import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from network_monitor.app.activity import Activity
from network_monitor.app.alerts import Alerts
from network_monitor.app.config import Config
from network_monitor.app.service import Monitor
from network_monitor.app.storage import Store
from network_monitor.app.telemetry import SourceError, Telemetry, client_key
from network_monitor.app.web import create_app


MAC = "02:00:00:00:00:01"
KEY = client_key(MAC)


def report(mac=MAC, rows=None):
    return {"client_usage_by_app": [{"client": {"mac": mac}, "usage_by_app": rows if rows is not None else [
        {"application": 101, "category": 7, "total_bytes": 9000,
         "bytes_received": 8000, "bytes_transmitted": 1000, "activity_seconds": 25}]}]}

def catalog(rows, *, offset=0, total=None):
    return {"data": rows, "count": len(rows), "totalCount": len(rows) if total is None else total,
            "offset": offset, "limit": 200}


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "activity.sqlite", 7, 1000)
        self.config = Config(unifi_url="https://controller.example", unifi_api_key="synthetic-key",
                             unifi_username="reader", unifi_password="synthetic-password",
                             unifi_activity_enabled=True)
        self.telemetry = Telemetry(self.config, self.store, "")
        self.telemetry.sources["legacy"].update(
            ts=time.time(), error=None, data={"clients": [{"key": KEY, "mac": MAC, "ip": "192.0.2.10"}]})
        self.telemetry.sample(time.time())
        self.activity = Activity(self.telemetry)
        self.telemetry.legacy_request = AsyncMock(return_value=report())
        self.telemetry.request = AsyncMock(side_effect=[
            catalog([{"id": (7 << 16) + 101, "name": "Example application"}]),
            catalog([{"id": 7, "name": "Example category"}])])

    async def asyncTearDown(self):
        await self.activity.close()
        self.store.close()
        self.temp.cleanup()

    async def test_exact_mac_window_units_names_and_no_persistence(self):
        result = await self.activity.snapshot(KEY, 1800)
        call = self.telemetry.legacy_request.call_args
        self.assertEqual(call.args[0], "/v2/api/site/default/traffic/" + MAC)
        self.assertEqual(call.kwargs["params"]["mac"], MAC)
        self.assertEqual(call.kwargs["params"]["end"] - call.kwargs["params"]["start"], 1800000)
        self.assertEqual(call.kwargs["params"]["includeUnidentified"], "true")
        app = result["applications"][0]
        self.assertEqual(app["application"], "Example application")
        self.assertEqual(app["catalog_id"], (7 << 16) + 101)
        self.assertEqual(self.telemetry.request.await_args_list[0].kwargs["params"]["filter"], "id.in(458853)")
        self.assertEqual(app["category"], "Example category")
        self.assertEqual(app["received_bytes"], 8000)
        self.assertEqual(app["controller_activity_seconds"], 25)
        self.assertIn("WAN-only", result["scope"])
        self.assertIn("not be added", result["warning"])
        self.assertIn("predate", result["identity_note"])
        self.assertTrue(result["private_mac"])
        for call in self.telemetry.request.await_args_list:
            self.assertEqual(call.args[1], "GET")
            self.assertTrue(call.kwargs["ssl"])
            self.assertIn("id.in(", call.kwargs["params"]["filter"])
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn("Example application", dump)
        self.assertNotIn("synthetic-password", json.dumps(result))

    async def test_opt_in_and_verified_current_identity_required(self):
        self.assertFalse(Config().unifi_activity_enabled)
        disabled = Telemetry(Config(), self.store, "")
        with self.assertRaisesRegex(SourceError, "Enable unifi_activity"):
            await Activity(disabled).snapshot(KEY, 1800)
        for change, message in (({"mac": None}, "verified controller MAC"),
                                ({"mac": "02:00:00:00:00:02"}, "verified controller MAC"),
                                ({"last_seen": 0}, "stale"),
                                ({"present": False}, "stale")):
            original = self.telemetry.devices[KEY].copy()
            self.telemetry.devices[KEY].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(SourceError, message):
                await self.activity.snapshot(KEY, 1800)
            self.telemetry.devices[KEY] = original
        self.telemetry.legacy_request.assert_not_awaited()

    async def test_inventory_outage_and_missing_legacy_credentials_stop_reads(self):
        self.telemetry.sources["legacy"]["error"] = "Source unavailable"
        with self.assertRaisesRegex(SourceError, "inventory is unavailable"):
            await self.activity.snapshot(KEY, 1800)
        self.telemetry.sources["legacy"]["enabled"] = False
        with self.assertRaisesRegex(SourceError, "read-only UniFi"):
            await self.activity.snapshot(KEY, 1800)

    async def test_does_not_assign_other_devices_or_infer_no_use(self):
        self.telemetry.legacy_request.return_value = report("02:00:00:00:00:02")
        result = await self.activity.snapshot(KEY, 1800)
        self.assertEqual(result["applications"], [])
        self.assertEqual(result["status"], "no_visible_activity")
        self.assertIn("does not mean no", result["warning"])

    async def test_schema_limits_duplicates_and_negative_counts_are_explicit_errors(self):
        cases = [
            {"data": []}, {"client_usage_by_app": "unsupported"},
            report(rows=[{}]), report(rows=[{"application": 1, "category": 2, "total_bytes": -1}]),
            report(rows=[{"application": 1, "category": 2, "total_bytes": "1000"}]),
            report(rows=[{"application": 1, "category": 2, "total_bytes": float("inf")}]),
            report(rows=[{"application": True, "category": 2}]),
            report(rows=[{"application": 65536, "category": 2}]),
            report(rows=[{"application": 1, "category": 32768}]),
            report(rows=[{"application": 1, "category": 2}] * 2),
            report(rows=[{"application": i, "category": 2} for i in range(201)]),
            {"client_usage_by_app": [{"client": {"mac": None}}]},
            {"client_usage_by_app": report()["client_usage_by_app"] * 2},
            {"client_usage_by_app": report()["client_usage_by_app"] * 501},
        ]
        for result in cases:
            self.telemetry.legacy_request.return_value = result
            with self.subTest(result=str(result)[:80]), self.assertRaises(SourceError):
                await self.activity.snapshot(KEY, 1800)
        self.assertEqual(self.activity.cache, {})

    async def test_missing_numbers_remain_unknown_and_catalog_failure_does_not_invent_names(self):
        self.telemetry.legacy_request.return_value = report(rows=[{"application": 101, "category": 7}])
        self.telemetry.request = AsyncMock(side_effect=SourceError("HTTP 403"))
        result = await self.activity.snapshot(KEY, 3600)
        app = result["applications"][0]
        self.assertIsNone(app["total_bytes"])
        self.assertIsNone(app["controller_activity_seconds"])
        self.assertIsNone(app["application"])
        self.assertIn("numeric IDs", result["catalog_error"])

    async def test_no_api_key_is_supported_with_ids_and_no_guessed_labels(self):
        self.telemetry.config = Config(unifi_url="https://controller.example",
                                       unifi_username="reader", unifi_password="synthetic",
                                       unifi_activity_enabled=True)
        result = await self.activity.snapshot(KEY, 1800)
        self.assertIsNone(result["applications"][0]["application"])
        self.assertIn("API key not configured", result["catalog_error"])
        self.telemetry.request.assert_not_awaited()

    async def test_composite_ids_never_reuse_same_local_id_across_categories(self):
        self.telemetry.legacy_request.return_value = report(rows=[
            {"application": 9, "category": 0, "total_bytes": 100},
            {"application": 9, "category": 13, "total_bytes": 200},
            {"application": 9, "category": 24, "total_bytes": 300},
        ])

        async def labels(session, method, url, **kwargs):
            if url.endswith("/applications"):
                self.assertEqual(kwargs["params"]["filter"], "id.in(9,851977,1572873)")
                return catalog([{"id": 9, "name": "Example messaging"},
                                {"id": 851977, "name": "Example web service"}])
            return catalog([{"id": 0, "name": "Messaging"}, {"id": 13, "name": "Web services"},
                            {"id": 24, "name": "Example category"}])

        self.telemetry.request.side_effect = labels
        apps = (await self.activity.snapshot(KEY, 1800))["applications"]
        self.assertEqual([app["application"] for app in apps],
                         ["Example messaging", "Example web service", None])
        self.assertEqual(sum(app["total_bytes"] for app in apps), 600)

    async def test_unknown_sentinels_never_receive_service_names(self):
        self.telemetry.legacy_request.return_value = report(rows=[
            {"application": 65535, "category": 13, "total_bytes": 100},
            {"application": 1, "category": 255, "total_bytes": 200},
        ])
        self.telemetry.request.side_effect = None
        self.telemetry.request.return_value = catalog([{"id": 13, "name": "Web services"}])
        apps = (await self.activity.snapshot(KEY, 1800))["applications"]
        self.assertTrue(all(app["unidentified"] and app["application"] is None for app in apps))
        self.telemetry.request.assert_awaited_once()
        self.assertTrue(self.telemetry.request.call_args.args[2].endswith("/categories"))

    async def test_catalog_pagination_reads_server_clamped_pages(self):
        self.telemetry.request.side_effect = [
            catalog([{"id": 458853, "name": "First service"}], total=2),
            catalog([{"id": 458854, "name": "Second service"}], offset=1, total=2)]
        names = await self.activity.names("applications", {458853, 458854})
        self.assertEqual(names, {458853: "First service", 458854: "Second service"})
        self.assertEqual([call.kwargs["params"]["offset"] for call in self.telemetry.request.await_args_list],
                         [0, 1])

    async def test_catalog_rejects_wrong_namespace_ignored_filter_and_incomplete_pages(self):
        cases = [
            [catalog([{"id": 101, "name": "Wrong namespace"}])],
            [catalog([{"id": 458853, "name": "First"}], total=500)],
            [{"data": [{"id": 458853, "name": "First"}]}],
            [catalog([{"id": 458853, "name": "First"}], total=2), catalog([], offset=1, total=2)],
            [catalog([{"id": 458853, "name": "First"}], total=2),
             catalog([{"id": 458853, "name": "Repeated"}], offset=1, total=2)],
            [catalog([{"id": 458853, "name": "First"}], total=2),
             catalog([{"id": 458854, "name": "Second"}], offset=1, total=3)],
        ]
        for responses in cases:
            with self.subTest(responses=responses):
                self.telemetry.request.side_effect = responses
                with self.assertRaises(SourceError):
                    await self.activity.names("applications", {458853, 458854})

    async def test_catalog_page_limit_rejects_truncation_not_partial_labels(self):
        async def labels(session, method, url, **kwargs):
            offset = kwargs["params"]["offset"]
            return catalog([{"id": offset, "name": "Example label"}], offset=offset, total=9)

        self.telemetry.request.side_effect = labels
        with self.assertRaisesRegex(SourceError, "eight-page"):
            await self.activity.names("applications", set(range(9)))
        self.assertEqual(self.telemetry.request.await_count, 8)

    async def test_catalogs_fail_independently_without_discarding_valid_service_names(self):
        async def labels(session, method, url, **kwargs):
            if url.endswith("/categories"):
                raise SourceError("HTTP 403")
            return catalog([{"id": 458853, "name": "Exact service"}])

        self.telemetry.request.side_effect = labels
        result = await self.activity.snapshot(KEY, 1800)
        self.assertEqual(result["applications"][0]["application"], "Exact service")
        self.assertIsNone(result["applications"][0]["category"])
        self.assertIsNotNone(result["catalog_error"])

    async def test_identity_change_during_read_rejects_result(self):
        async def changed(*args, **kwargs):
            self.telemetry.devices[KEY]["binding_start"] += 1
            return report()

        self.telemetry.legacy_request.side_effect = changed
        with self.assertRaisesRegex(SourceError, "identity changed"):
            await self.activity.snapshot(KEY, 1800)
        self.assertEqual(self.activity.cache, {})

    async def test_cache_is_bounded_expires_without_new_reads_and_is_binding_scoped(self):
        await self.activity.snapshot(KEY, 1800)
        await self.activity.snapshot(KEY, 1800)
        self.telemetry.legacy_request.assert_awaited_once()
        self.telemetry.devices[KEY]["binding_start"] += 1
        self.telemetry.request.side_effect = None
        self.telemetry.request.return_value = catalog([])
        await self.activity.snapshot(KEY, 1800)
        self.assertEqual(self.telemetry.legacy_request.await_count, 2)
        expires = self.activity.cache[(KEY, 1800)]["created"] + 61
        self.activity.expiry.cancel()
        with patch("network_monitor.app.activity.time.monotonic", return_value=expires):
            self.activity.prune()
        self.assertEqual(self.activity.cache, {})
        self.assertIsNone(self.activity.expiry)

    async def test_cache_maximum_sixteen_snapshots(self):
        self.telemetry.request.side_effect = None
        self.telemetry.request.return_value = catalog([])
        for i in range(17):
            mac = f"02:00:00:00:01:{i:02x}"
            key = client_key(mac)
            self.telemetry.devices[key] = {**self.telemetry.devices[KEY], "mac": mac}
            self.telemetry.legacy_request.return_value = report(mac)
            await self.activity.snapshot(key, 1800)
        self.assertEqual(len(self.activity.cache), 16)

    async def test_close_clears_payload_and_timer(self):
        await self.activity.snapshot(KEY, 1800)
        handle = self.activity.expiry
        await self.activity.close()
        self.assertTrue(handle.cancelled())
        self.assertEqual(self.activity.cache, {})
        self.assertIsNone(self.activity.expiry)

    async def test_api_disabled_missing_identity_and_source_errors_are_not_success(self):
        monitor = Monitor(self.config, self.store, Alerts(self.config, self.store, "", object()))
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry))) as client:
            response = await client.get("/api/clients/unknown/activity")
            self.assertEqual(response.status, 503)
            self.telemetry.legacy_request.side_effect = SourceError("HTTP 403; check credentials and permissions")
            response = await client.get(f"/api/clients/{KEY}/activity")
            self.assertEqual(response.status, 503)
            self.assertNotIn(self.config.unifi_url, await response.text())
        disabled = Monitor(Config(), self.store, Alerts(Config(), self.store, "", object()))
        async with TestClient(TestServer(create_app(disabled, development=True))) as client:
            self.assertEqual((await client.get(f"/api/clients/{KEY}/activity")).status, 503)
            self.assertEqual((await client.get("/unifi-console")).status, 503)

    async def test_windows_errors_and_ingress_console_link_do_not_add_writes(self):
        monitor = Monitor(self.config, self.store, Alerts(self.config, self.store, "", object()))
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry))) as client:
            for value in ("nan", "0", "120", "604800"):
                self.assertEqual((await client.get(f"/api/clients/{KEY}/activity?seconds={value}")).status, 400)
            for seconds in (1800, 3600, 86400):
                self.telemetry.request.side_effect = None
                self.telemetry.request.return_value = catalog([])
                response = await client.get(f"/api/clients/{KEY}/activity?seconds={seconds}")
                self.assertEqual(response.status, 200)
            response = await client.get("/unifi-console?target=https://untrusted.example", allow_redirects=False)
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers["Location"], self.config.unifi_url)
            self.assertEqual((await client.post(f"/api/clients/{KEY}/pause", json={},
                              headers={"X-Network-Monitor": "1"})).status, 404)
        async with TestClient(TestServer(create_app(monitor, telemetry=self.telemetry))) as client:
            for path in (f"/api/clients/{KEY}/activity", "/unifi-console"):
                self.assertEqual((await client.get(path, allow_redirects=False)).status, 403)

    async def test_legacy_read_uses_existing_tls_auth_session_and_only_read_paths(self):
        telemetry = Telemetry(self.config, self.store, "")
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            telemetry.unifi_session = session
            telemetry.request = AsyncMock(side_effect=[{}, report(), report()])
            await asyncio.gather(
                telemetry.legacy_request("/v2/api/site/default/traffic/" + MAC),
                telemetry.legacy_request("/v2/api/site/default/traffic/" + MAC))
            calls = telemetry.request.await_args_list
            self.assertEqual([c.args[1] for c in calls], ["POST", "GET", "GET"])
            self.assertTrue(calls[0].args[2].endswith("/api/auth/login"))
            self.assertTrue(all(c.kwargs["ssl"] for c in calls))
            self.assertNotIn("cmd/", str(calls))
            telemetry.request.side_effect = SourceError("HTTP 401")
            with self.assertRaises(SourceError):
                await telemetry.legacy_request("/api/s/default/stat/sta")
            self.assertFalse(telemetry.legacy_authenticated)
            telemetry.request.side_effect = [{}, report()]
            await telemetry.legacy_request("/api/s/default/stat/sta")
            self.assertTrue(telemetry.legacy_authenticated)


if __name__ == "__main__":
    unittest.main()

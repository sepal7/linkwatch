import asyncio
import csv
import errno
import io
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import dns.exception
import dns.resolver
import yaml
from aiohttp.test_utils import AioHTTPTestCase

from network_monitor.app.alerts import Alerts, create_sounds
from network_monitor.app.config import Config, parse_target
from network_monitor.app.debounce import Debounce
from network_monitor.app.probes import Result, classify, dns_probe, measure, tcp
from network_monitor.app.service import Monitor
from network_monitor.app.storage import Store
from network_monitor.app.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def probes(outcome="ok"):
    return [Result("1.1.1.1:443", "external", outcome, 12.5 if outcome == "ok" else None).json(),
            Result("8.8.8.8:53", "external", outcome, 20.5 if outcome == "ok" else None).json()]


class ConfigTests(unittest.TestCase):
    def test_manifest_matches_runtime_defaults(self):
        manifest = yaml.safe_load((ROOT / "network_monitor" / "config.yaml").read_text())
        self.assertEqual(Config(**manifest["options"]), Config())
        self.assertEqual(set(manifest["options"]), set(manifest["schema"]))
        self.assertIn("aarch64", manifest["arch"])
        self.assertTrue(manifest["ingress"])
        self.assertTrue(manifest["homeassistant_api"])
        self.assertTrue(manifest["audio"])
        self.assertEqual(manifest["name"], "Linkwatch")
        for name in ("icon.png", "logo.png"):
            image = (ROOT / "network_monitor" / name).read_bytes()
            self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
            import struct
            self.assertEqual(struct.unpack(">II", image[16:24]), (256, 256))
        self.assertNotIn("ports", manifest)
        self.assertNotIn("privileged", manifest)
        self.assertNotIn("hassio_api", manifest)
        self.assertEqual(manifest["boot"], "auto")
        self.assertEqual(manifest["backup"], "cold")
        repository = yaml.safe_load((ROOT / "repository.yaml").read_text())
        self.assertEqual(repository["url"], "https://github.com/sepal7/linkwatch")
        self.assertEqual(manifest["url"], repository["url"])

    def test_input_validation(self):
        for values in [
            {"interval_seconds": True}, {"timeout_seconds": 0}, {"router_port": 65536},
            {"external_targets": ["example.com:443", "8.8.8.8:53"]},
            {"external_targets": ["10.0.0.1:443", "8.8.8.8:53"]},
            {"external_targets": ["1.1.1.1:443", "1.1.1.1:53"]},
            {"external_targets": ["1.1.1.1:443"]}, {"dns_server": "example.com"},
            {"alerts_enabled": True}, {"media_player": "sensor.my_sensor"},
            {"dns_test_name": "bad name"}, {"retention_days": 91},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Config(**values)
        self.assertEqual(parse_target("[2606:4700:4700::1111]:443"),
                         ("2606:4700:4700::1111", 443))


class ClassificationTests(unittest.TestCase):
    def test_all_outcomes(self):
        for outcomes, expected in [
            ([], "unknown"), (["ok", "ok"], "online"),
            (["fail", "ok"], "degraded"), (["error", "ok"], "degraded"),
            (["fail", "fail"], "offline"), (["error", "fail"], "unknown"),
            (["error", "error"], "unknown"),
        ]:
            with self.subTest(outcomes=outcomes):
                self.assertEqual(classify([Result(str(i), "external", r)
                                           for i, r in enumerate(outcomes)]), expected)
        self.assertEqual(classify([Result("one", "external", "ok"),
                                   Result("dns", "dns", "fail")]), "degraded")
        self.assertEqual(classify([Result("router", "router", "ok")]), "unknown")


class DebounceTests(unittest.TestCase):
    def test_failure_recovery_and_no_repeats(self):
        d = Debounce(15, 10)
        self.assertIsNone(d.observe("online", 0))
        self.assertIsNone(d.observe("offline", 1))
        self.assertIsNone(d.observe("offline", 15.999))
        self.assertEqual(d.observe("offline", 16), "down")
        self.assertIsNone(d.observe("offline", 100))
        self.assertIsNone(d.observe("degraded", 101))
        self.assertIsNone(d.observe("online", 110.999))
        self.assertEqual(d.observe("online", 111), "recovery")
        self.assertIsNone(d.observe("online", 200))

    def test_brief_failure_is_recordable_but_not_alerted(self):
        d = Debounce(15, 10)
        self.assertIsNone(d.observe("offline", 0))
        self.assertIsNone(d.observe("online", 5))
        self.assertIsNone(d.observe("online", 100))
        self.assertFalse(d.announced_down)

    def test_unknown_breaks_pending_dwell_but_not_announced_failure(self):
        d = Debounce(10, 5)
        d.observe("offline", 0)
        d.observe("unknown", 9)
        self.assertIsNone(d.observe("offline", 20))
        self.assertEqual(d.observe("offline", 30), "down")
        d.observe("online", 31)
        d.observe("unknown", 35)
        self.assertIsNone(d.observe("online", 100))
        self.assertEqual(d.observe("degraded", 105), "recovery")

    def test_zero_debounce(self):
        d = Debounce(0, 0)
        self.assertEqual(d.observe("offline", 0), "down")
        self.assertEqual(d.observe("online", 1), "recovery")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite"
        self.store = Store(self.path, 7, 1000)
        self.store.start(100)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, ts, state="online"):
        return self.store.record(ts, state, probes("fail" if state == "offline" else "ok"), 15)

    def test_raw_outage_latency_and_export(self):
        self.record(100)
        self.record(105, "offline")
        self.record(110)
        history = self.store.history(90, 120)
        offline = [i for i in history["intervals"] if i["state"] == "offline"]
        self.assertEqual(offline[0]["duration_seconds"], 5)
        self.assertEqual(self.store.last["latency_ms"], 16.5)
        rows = list(self.store.export_rows(90, 120, "samples"))
        csv_text = b"".join(self.store.csv_line(r) for r in rows).decode()
        parsed = list(csv.reader(io.StringIO(csv_text)))
        self.assertEqual(len(parsed), 4)
        self.assertEqual(json.loads(parsed[1][3]), probes())
        self.assertIn("+00:00", parsed[1][0])

    def test_history_clips_duration_and_buckets_are_bounded(self):
        for ts in range(100, 1100, 5):
            self.record(ts, "offline")
        history = self.store.history(500, 700, 10)
        self.assertLessEqual(len(history["buckets"]), 11)
        self.assertEqual(sum(b["count"] for b in history["buckets"]), 41)
        self.assertEqual(history["intervals"][0]["duration_seconds"], 200)

    def test_restart_gap_not_uptime_or_outage(self):
        self.record(100, "offline")
        self.record(105, "offline")
        self.store.close()
        self.store = Store(self.path, 7, 1000)
        self.store.start(200)
        self.record(205)
        history = self.store.history(90, 220)
        gap = [i for i in history["intervals"] if i["state"] == "gap"]
        self.assertEqual(gap[-1]["start"], 105)
        self.assertEqual(gap[-1]["end"], 205)
        offline = [i for i in history["intervals"] if i["state"] == "offline"]
        self.assertEqual(offline[0]["end"], 105)

    def test_missed_cycle_and_clock_backwards_create_gap(self):
        self.record(100)
        self.record(105)
        self.assertTrue(self.record(150))
        history = self.store.history(90, 160)
        self.assertTrue(any(i["state"] == "gap" and i["duration_seconds"] == 45
                            for i in history["intervals"]))
        self.assertTrue(self.record(140))

    def test_retention_and_maximum_rows_and_settings(self):
        for ts in range(100, 1300):
            self.record(ts)
        with self.store.db:
            self.store.prune(1300)
        self.assertLessEqual(self.store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1000)
        self.store.set_setting("muted", "true")
        self.assertEqual(self.store.setting("muted"), "true")
        self.store.close()
        self.store = Store(self.path, 1, 1000)
        with self.store.db:
            self.store.prune(1000000)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
        self.assertEqual(self.store.setting("muted"), "true")

    def test_event_and_interval_retention_caps(self):
        with self.store.db:
            for i in range(10100):
                self.store.event(100 + i, "test", "event")
                self.store.db.execute(
                    "INSERT INTO intervals(start,end,state,reason) VALUES (?,?,'unknown','test')",
                    (100 + i, 101 + i))
            self.store.prune(10200)
        for table in ("events", "intervals"):
            self.assertLessEqual(self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 10000)

    def test_export_is_consistent_while_retention_changes(self):
        self.record(100)
        self.record(105)
        rows = self.store.export_rows(90, 150, "samples")
        next(rows)
        next(rows)  # Acquire the read snapshot before concurrent writes.
        self.record(110)
        with self.store.db:
            self.store.prune(1000000)
        remaining = list(rows)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0][0], "1970-01-01T00:01:45+00:00")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_success_failure_and_mechanism_error(self):
        writer = MagicMock()
        with patch("network_monitor.app.probes.asyncio.open_connection",
                   new=AsyncMock(return_value=(None, writer))):
            self.assertEqual((await tcp("one", "external", "1.1.1.1", 443, 1)).outcome, "ok")
            writer.close.assert_called_once()
        for error, expected in [
            (TimeoutError(), "fail"), (ConnectionRefusedError(), "fail"),
            (OSError(errno.ENETUNREACH, "no route"), "fail"),
            (PermissionError(errno.EACCES, "denied"), "error"),
            (OSError(errno.EMFILE, "no descriptors"), "error"),
        ]:
            with self.subTest(error=error), patch(
                "network_monitor.app.probes.asyncio.open_connection", new=AsyncMock(side_effect=error)
            ):
                self.assertEqual((await tcp("one", "external", "1.1.1.1", 443, 1)).outcome, expected)

    async def test_dns_validates_public_answers(self):
        for addresses, expected in [
            (["93.184.216.34"], "ok"), ([], "fail"), (["0.0.0.0"], "fail"),
            (["127.0.0.1"], "fail"), (["192.0.2.1"], "fail"),
        ]:
            answer = [MagicMock(address=address) for address in addresses]
            with self.subTest(addresses=addresses), patch(
                "network_monitor.app.probes.dns.asyncresolver.Resolver"
            ) as factory:
                factory.return_value.resolve = AsyncMock(return_value=answer)
                self.assertEqual((await dns_probe("1.1.1.1", "example.com", 1)).outcome, expected)
        for error in (dns.resolver.NXDOMAIN(), dns.exception.Timeout()):
            with patch("network_monitor.app.probes.dns.asyncresolver.Resolver") as factory:
                factory.return_value.resolve = AsyncMock(side_effect=error)
                self.assertEqual((await dns_probe("1.1.1.1", "example.com", 1)).outcome, "fail")

    async def test_checks_are_concurrent_not_sequential(self):
        async def fake(*args):
            await asyncio.sleep(.05)
            return Result(args[0], args[1], "ok")
        with patch("network_monitor.app.probes.tcp", side_effect=fake):
            start = time.monotonic()
            results = await measure(Config(external_targets=[
                "1.1.1.1:443", "8.8.8.8:53", "9.9.9.9:443", "208.67.222.222:443"]))
            self.assertEqual(len(results), 4)
            self.assertLess(time.monotonic() - start, .17)


class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite", 7, 1000)
        self.store.start(time.time())
        self.config = Config(alerts_enabled=True, media_player="media_player.test_hub")

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_sound_assets_are_real_different_pcm(self):
        directory = Path(self.temp.name) / "sounds"
        create_sounds(directory)
        values = []
        for name in ("down", "recovery"):
            with wave.open(str(directory / f"{name}.wav")) as sound:
                self.assertEqual(sound.getframerate(), 22050)
                self.assertEqual(sound.getsampwidth(), 2)
                self.assertGreater(sound.getnframes(), 22050)
                values.append(sound.readframes(sound.getnframes()))
        self.assertNotEqual(values[0], values[1])

    async def test_api_contract_and_error_not_hidden(self):
        session = MagicMock()
        response = MagicMock()
        response.status = 200
        session.post.return_value.__aenter__.return_value = response
        alerts = Alerts(self.config, self.store, "fake-test-token", session)
        await alerts.play("down")
        url = session.post.call_args.args[0]
        payload = session.post.call_args.kwargs
        self.assertEqual(url, "http://supervisor/core/api/services/media_player/play_media")
        self.assertEqual(payload["json"]["media_content_id"],
                         "media-source://media_source/local/home-network-monitor/down.wav")
        self.assertEqual(payload["headers"]["Authorization"], "Bearer fake-test-token")
        response.status = 503
        with self.assertRaisesRegex(ValueError, "503"):
            await alerts.play("recovery")

    async def test_worker_failure_isolated_and_success_logged(self):
        alerts = Alerts(self.config, self.store, "fake-test-token", MagicMock())
        alerts.play = AsyncMock(side_effect=aiohttp.ClientConnectionError("test unavailable"))
        task = asyncio.create_task(alerts.run())
        try:
            alerts.enqueue("down", time.time())
            await asyncio.wait_for(alerts.queue.join(), 1)
            self.assertIn("test unavailable", alerts.last_error)
            alerts.play = AsyncMock()
            alerts.enqueue("recovery", time.time())
            await asyncio.wait_for(alerts.queue.join(), 1)
            self.assertIsNone(alerts.last_error)
            self.assertTrue(any(e["kind"] == "alert_requested"
                                for e in self.store.history(0, time.time()+1)["events"]))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_sampling_run_records_brief_failure_independent_of_sound_worker(self):
        alerts = Alerts(self.config, self.store, "fake-test-token", MagicMock())
        config = Config(alerts_enabled=True, media_player="media_player.test_hub",
                        alert_failure_seconds=0, alert_recovery_seconds=0)
        monitor = Monitor(config, self.store, alerts)
        measurements = [
            [Result("one", "external", "fail"), Result("two", "external", "fail")],
            [Result("one", "external", "ok", 1), Result("two", "external", "ok", 2)],
        ]
        with patch("network_monitor.app.service.measure", new=AsyncMock(side_effect=measurements)), patch(
            "network_monitor.app.service.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError()])
        ):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.run()
        self.assertFalse(monitor.running)
        self.assertEqual(alerts.queue.get_nowait()[0], "down")
        self.assertEqual(alerts.queue.get_nowait()[0], "recovery")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)

    async def test_mute_and_expired_requests_and_missing_token(self):
        alerts = Alerts(self.config, self.store, "", MagicMock())
        with self.assertRaisesRegex(ValueError, "SUPERVISOR_TOKEN"):
            await alerts.play("down")
        self.store.set_setting("muted", "true")
        alerts.enqueue("down", time.time())
        self.assertTrue(alerts.queue.empty())
        self.store.set_setting("muted", "false")
        alerts.enqueue("down", time.time()-60)
        alerts.play = AsyncMock()
        task = asyncio.create_task(alerts.run())
        try:
            await asyncio.wait_for(alerts.queue.join(), 1)
            alerts.play.assert_not_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class WebTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite", 7, 1000)
        self.store.start(time.time())
        self.config = Config()
        self.alerts = Alerts(self.config, self.store, "", MagicMock())
        self.monitor = Monitor(self.config, self.store, self.alerts)
        return create_app(self.monitor, development=True)

    async def tearDownAsync(self):
        await super().tearDownAsync()
        self.store.close()
        self.temp.cleanup()

    async def test_status_unknown_until_this_session_observes(self):
        response = await self.client.get("/api/status")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["state"], "unknown")
        self.monitor.running = True
        self.store.record(time.time(), "online", probes(), 15)
        self.assertEqual((await (await self.client.get("/api/status")).json())["state"], "online")
        self.assertEqual(self.monitor.status(time.time()+100)["state"], "unknown")
        self.assertEqual(self.monitor.status(time.time()-100)["state"], "unknown")

    async def test_dashboard_and_security_headers(self):
        response = await self.client.get("/")
        self.assertIn("Linkwatch", await response.text())
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        for path in ("/style.css", "/dashboard.js", "/mark.svg"):
            self.assertEqual((await self.client.get(path)).status, 200)
        self.assertEqual((await self.client.get("/config.yaml")).status, 404)

    async def test_history_and_streaming_csv_validation(self):
        now = time.time()
        self.store.record(now, "offline", probes("fail"), 15)
        response = await self.client.get(f"/api/history?from={now-100}&to={now+1}")
        history = await response.json()
        self.assertEqual(history["buckets"][0]["offline"], 1)
        response = await self.client.get(f"/api/export.csv?kind=samples&from={now-100}&to={now+1}")
        self.assertEqual(response.status, 200)
        self.assertIn("offline", await response.text())
        self.assertIn("attachment", response.headers["Content-Disposition"])
        for query in ("from=nan", "to=inf", "from=10&to=2", "from=0&to=999999999"):
            self.assertEqual((await self.client.get(f"/api/history?{query}")).status, 400)
        self.assertEqual((await self.client.get("/api/export.csv?kind=bad")).status, 400)

    async def test_live_window_preserves_short_failures_and_insight_api(self):
        now = time.time()
        for i in range(900):
            ts = now - 1800 + i * 2
            self.store.record(ts, "offline" if i == 500 else "online",
                              probes("fail" if i == 500 else "ok"), 15)
        response = await self.client.get(f"/api/history?from={now-1800}&to={now}")
        history = await response.json()
        self.assertEqual(history["bucket_seconds"], 2)
        self.assertEqual(sum(b["offline"] for b in history["buckets"]), 1)
        self.assertGreater(len(history["buckets"]), 850)
        response = await self.client.get(f"/api/insights?from={now-1800}&to={now}")
        data = await response.json()
        self.assertEqual(data["current"]["interruptions"], 1)
        self.assertEqual(data["current"]["seconds"]["offline"], 2)
        offline = next(i for i in history["intervals"] if i["state"] == "offline")
        response = await self.client.get(f"/api/incidents/{offline['id']}")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["first"]["state"], "offline")
        self.assertEqual((await self.client.get("/api/incidents/9999999")).status, 404)
        self.assertEqual((await self.client.get("/api/incidents/bad")).status, 400)
        self.assertEqual((await self.client.get("/api/insights?from=nan")).status, 400)

    async def test_mutation_guards_and_persistent_mute(self):
        self.assertEqual((await self.client.post("/api/mute", json={"muted": True})).status, 403)
        headers = {"X-Network-Monitor": "1"}
        self.assertEqual((await self.client.post("/api/mute", data="{}", headers=headers)).status, 415)
        self.assertEqual((await self.client.post("/api/mute", json={"muted": "true"}, headers=headers)).status, 400)
        self.assertEqual((await self.client.post("/api/mute", json={"muted": True}, headers=headers)).status, 200)
        self.assertTrue(self.alerts.muted)
        self.assertEqual((await self.client.post("/api/test-sound", json={}, headers=headers)).status, 409)

    async def test_phone_only_alert_test_route_and_legacy_alias(self):
        self.alerts.config = Config(alerts_enabled=True, sound_output="none",
                                   phone_notify_service="notify.mobile_app_test_phone")
        self.monitor.config = self.alerts.config
        self.assertEqual((await self.client.post("/api/test-alerts", json={})).status, 403)
        headers = {"X-Network-Monitor": "1"}
        for path in ("/api/test-alerts", "/api/test-sound"):
            self.assertEqual((await self.client.post(path, json={}, headers=headers)).status, 202)
            self.assertEqual(self.alerts.queue.get_nowait()[0], "test")

    async def test_no_forwarded_header_can_bypass_production_ingress(self):
        production = create_app(self.monitor)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(production)) as client:
            for path in ("/", "/api/status", "/api/history", "/api/insights",
                         "/api/incidents/1", "/api/export.csv"):
                response = await client.get(path, headers={
                    "X-Forwarded-For": "172.30.32.2", "X-Real-IP": "172.30.32.2"})
                self.assertEqual(response.status, 403)

    async def test_real_ingress_peer_is_accepted(self):
        production = create_app(self.monitor)
        request = MagicMock()
        request.transport.get_extra_info.return_value = ("172.30.32.2", 12345)
        request.method = "GET"
        from aiohttp import web
        handler = AsyncMock(return_value=web.Response(text="allowed"))
        response = await production.middlewares[0](request, handler)
        self.assertEqual(response.text, "allowed")
        handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

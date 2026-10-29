import asyncio
import base64
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from network_monitor.app.actions import ActionConflict
from network_monitor.app.alerts import Alerts
from network_monitor.app.config import Config
from network_monitor.app.router_actions import RouterActions
from network_monitor.app.service import Monitor
from network_monitor.app.storage import Store
from network_monitor.app.telemetry import SourceError, Telemetry, client_key
from network_monitor.app.web import create_app


MAC = "02:00:00:00:00:01"
KEY = client_key(MAC)


class RouterActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "actions.sqlite", 7, 1000)
        self.config = Config(
            unifi_url="https://192.0.2.1", unifi_api_key="synthetic-key",
            unifi_username="synthetic-reader", unifi_password="synthetic-read-password",
            unifi_controls_enabled=True, unifi_control_username="synthetic-controller",
            unifi_control_password="synthetic-write-password", unifi_control_ha_ip="192.0.2.254",
            unifi_control_client_macs=[MAC], unifi_tls_fingerprint="01" * 32)
        self.telemetry = Telemetry(self.config, self.store, "")
        self.telemetry.devices[KEY] = {"key": KEY, "mac": MAC, "ip": "192.0.2.10", "present": True,
                                       "last_seen": time.time(), "binding_start": 1}
        self.telemetry.sources["unifi"].update(ts=time.time(), error=None)
        self.user = {"_id": "synthetic-client-id", "mac": MAC, "ip": "192.0.2.10",
                     "blocked": False, "name": "Example client"}
        self.devices = [{"mac": "02:00:00:00:01:01", "ip": "192.0.2.1"}]
        self.networks = [{"purpose": "corporate", "ip_subnet": "192.0.2.1/24"}]
        self.commands, self.sessions = [], []
        self.issue_cookie = True
        self.csrf = "synthetic-csrf"
        self.command_error = self.readback_error = self.auth_error = None
        self.update_flag = True
        self.telemetry.request = AsyncMock(side_effect=self.request)
        self.actions = RouterActions(self.telemetry)

    async def asyncTearDown(self):
        await self.actions.close()
        self.store.close()
        self.temp.cleanup()

    async def request(self, session, method, url, **kwargs):
        self.assertIs(kwargs["ssl"], self.telemetry.fingerprint)
        self.assertTrue(url.startswith(self.config.unifi_url))
        if url.endswith("/api/auth/login"):
            self.assertEqual(method, "POST")
            self.assertEqual(kwargs["json"], {"username": "synthetic-controller", "password": "synthetic-write-password"})
            self.sessions.append(session)
            if self.auth_error:
                raise self.auth_error
            if self.issue_cookie:
                claims = base64.urlsafe_b64encode(json.dumps({"csrfToken": self.csrf}).encode()).decode().rstrip("=")
                session.cookie_jar.update_cookies({"TOKEN": "synthetic." + claims + ".signature"},
                                                 response_url=URL(self.config.unifi_url))
            return {"unique_id": "synthetic-admin"}
        if url.endswith("/cmd/stamgr"):
            self.assertEqual(method, "POST")
            self.assertEqual(kwargs["headers"], {"X-CSRF-Token": "synthetic-csrf"})
            self.assertEqual(set(kwargs["json"]), {"cmd", "mac"})
            self.assertIn(kwargs["json"]["cmd"], ("block-sta", "unblock-sta"))
            self.assertEqual(kwargs["json"]["mac"], MAC)
            self.commands.append(kwargs["json"])
            if self.command_error:
                raise self.command_error
            if self.update_flag:
                self.user["blocked"] = kwargs["json"]["cmd"] == "block-sta"
            return {"meta": {"rc": "ok"}, "data": []}
        self.assertEqual(method, "GET")
        if url.endswith("/stat/user/" + MAC):
            if self.commands and self.readback_error:
                raise self.readback_error
            return {"meta": {"rc": "ok"}, "data": [dict(self.user)]}
        if url.endswith("/stat/device"):
            return {"meta": {"rc": "ok"}, "data": self.devices}
        if url.endswith("/rest/networkconf"):
            return {"meta": {"rc": "ok"}, "data": self.networks}
        self.fail("Unexpected endpoint")

    async def test_configuration_default_off_explicit_targets_and_separate_account(self):
        self.assertFalse(Config().unifi_controls_enabled)
        for kwargs in ({"unifi_controls_enabled": True}, {"unifi_control_client_macs": ["bad"]},
                       {"unifi_control_client_macs": [MAC, MAC.upper()]},
                       {"unifi_control_client_macs": ["ff:ff:ff:ff:ff:ff"]},
                       {"unifi_control_client_macs": ["00:00:00:00:00:00"]},
                       {"unifi_control_ha_ip": "127.0.0.1"},
                       {"unifi_control_ha_ip": "https://example.com"},
                       {"unifi_control_username": "same"},
                       {"unifi_username": "same", "unifi_password": "x",
                        "unifi_control_username": "SAME", "unifi_control_password": "x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)
        self.telemetry.config = Config()
        with self.assertRaisesRegex(SourceError, "disabled"):
            await self.actions.prepare(KEY, "block")
        self.telemetry.request.assert_not_awaited()

    async def test_preview_no_command_and_confirmed_write_once_with_verified_flag(self):
        self.telemetry.unifi_session = object()
        original_session = self.telemetry.unifi_session
        self.telemetry.legacy_authenticated = True
        plan = await self.actions.prepare(KEY, "block")
        self.assertFalse(plan["blocked"])
        self.assertTrue(plan["private_mac"])
        self.assertIn("EVEN if set outside", plan["impact"])
        self.assertIn("local-network", plan["impact"])
        self.assertEqual(self.commands, [])
        result = await self.actions.apply(KEY, plan["token"])
        self.assertEqual(result["status"], "flag_verified")
        self.assertIn("Effective access was not tested", result["message"])
        self.assertEqual(self.commands, [{"cmd": "block-sta", "mac": MAC}])
        self.assertIs(original_session, self.telemetry.unifi_session)
        self.assertTrue(self.telemetry.legacy_authenticated)
        self.assertEqual(len(self.sessions), 2)
        self.assertIsNot(self.sessions[0], self.sessions[1])
        self.assertTrue(all(session.closed for session in self.sessions))
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        dump = "\n".join(self.store.db.iterdump())
        self.assertIn("shared flag verified", dump)
        for sensitive in (MAC, "192.0.2.10", "Example client", "synthetic", plan["token"]):
            self.assertNotIn(sensitive, dump)

    async def test_unblock_external_flag_without_inventory_after_restart(self):
        self.user["blocked"] = True
        self.telemetry.devices.clear()
        plan = await self.actions.prepare(KEY, "unblock")
        self.assertTrue(plan["blocked"])
        result = await self.actions.apply(KEY, plan["token"])
        self.assertFalse(result["blocked"])
        self.assertEqual(self.commands, [{"cmd": "unblock-sta", "mac": MAC}])
        self.assertEqual(self.user["blocked"], False)

    async def test_action_and_target_whitelist(self):
        for action in ("pause", "delete", "kick-sta", "unblock-sta", {}, None):
            with self.subTest(action=action), self.assertRaises(SourceError):
                await self.actions.prepare(KEY, action)
        with self.assertRaises(ActionConflict):
            await self.actions.prepare(client_key("02:00:00:00:00:02"), "block")
        self.telemetry.request.assert_not_awaited()

    async def test_already_blocked_or_unblocked_never_sends_command(self):
        for action, blocked in (("block", True), ("unblock", False)):
            self.user["blocked"] = blocked
            with self.assertRaisesRegex(ActionConflict, "already"):
                await self.actions.prepare(KEY, action)
        self.assertEqual(self.commands, [])

    async def test_stale_absent_unavailable_or_mismatched_selection_cannot_block(self):
        original = dict(self.telemetry.devices[KEY])
        for change in ({"present": False}, {"last_seen": time.time() - 1000},
                       {"ip": "192.0.2.11"}, {"mac": "02:00:00:00:00:02"}):
            self.telemetry.devices[KEY] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ActionConflict):
                await self.actions.prepare(KEY, "block")
        self.telemetry.devices[KEY] = original
        self.telemetry.sources["unifi"]["error"] = "Source unavailable"
        with self.assertRaises(ActionConflict):
            await self.actions.prepare(KEY, "block")
        self.assertEqual(self.commands, [])

    async def test_infrastructure_ha_gateway_controller_dns_are_protected(self):
        for ip in ("192.0.2.254", "192.0.2.1", "127.0.0.1", "0.0.0.0", "224.0.0.1"):
            self.user["ip"] = ip
            with self.subTest(ip=ip), self.assertRaises(ActionConflict):
                await self.actions.prepare(KEY, "block")
        self.user["ip"] = "192.0.2.10"
        self.devices.append({"mac": MAC, "ip": self.user["ip"]})
        with self.assertRaises(ActionConflict):
            await self.actions.prepare(KEY, "block")
        self.devices.pop()
        self.telemetry.config = replace(self.config, dns_server="192.0.2.10")
        with self.assertRaises(ActionConflict):
            await self.actions.prepare(KEY, "block")
        self.assertEqual(self.commands, [])

    async def test_unknown_flag_identity_and_missing_safeguards_are_not_guessed(self):
        original = dict(self.user)
        for change in ({"blocked": None}, {"blocked": 0}, {"_id": None}, {"mac": "bad"}, {"ip": ""}):
            self.user = {**original, **change}
            with self.subTest(change=change), self.assertRaises(SourceError):
                await self.actions.prepare(KEY, "block")
        self.user = original
        for devices, networks in (([], self.networks), (self.devices, []),
                                  (self.devices, [{"purpose": "corporate"}])):
            self.devices, self.networks = devices, networks
            with self.assertRaises(SourceError):
                await self.actions.prepare(KEY, "block")
        self.assertEqual(self.commands, [])

    async def test_missing_csrf_auth_denial_and_permission_errors_do_not_write(self):
        self.issue_cookie = False
        with self.assertRaisesRegex(SourceError, "CSRF"):
            await self.actions.prepare(KEY, "block")
        self.issue_cookie = True
        self.auth_error = SourceError("HTTP 403; check credentials and permissions")
        with self.assertRaisesRegex(SourceError, "403"):
            await self.actions.prepare(KEY, "block")
        self.auth_error = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.actions.prepare(KEY, "block")
        self.assertEqual(self.commands, [])

    async def test_concurrent_flag_ip_id_or_binding_changes_invalidate_confirmation(self):
        for field, value in (("blocked", True), ("ip", "192.0.2.11"), ("_id", "replacement")):
            original = self.user[field]
            plan = await self.actions.prepare(KEY, "block")
            self.user[field] = value
            with self.subTest(field=field), self.assertRaises(ActionConflict):
                await self.actions.apply(KEY, plan["token"])
            self.user[field] = original
        plan = await self.actions.prepare(KEY, "block")
        self.telemetry.devices[KEY]["binding_start"] = 2
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        self.assertEqual(self.commands, [])

    async def test_expiry_foreign_device_replay_and_concurrent_apply(self):
        plan = await self.actions.prepare(KEY, "block")
        self.actions.pending[plan["token"]] = replace(self.actions.pending[plan["token"]], created=time.monotonic() - 61)
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        plan = await self.actions.prepare(KEY, "block")
        other = "02:00:00:00:00:02"
        self.telemetry.config = replace(self.config, unifi_control_client_macs=[MAC, other])
        with self.assertRaises(ActionConflict):
            await self.actions.apply(client_key(other), plan["token"])
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        plan = await self.actions.prepare(KEY, "block")
        results = await asyncio.gather(self.actions.apply(KEY, plan["token"]),
                                       self.actions.apply(KEY, plan["token"]), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ActionConflict) for result in results), 1)
        self.assertEqual(len(self.commands), 1)

    async def test_command_timeout_or_rejection_never_retries_or_claims_success(self):
        for error in (TimeoutError(), SourceError("HTTP 403"), aiohttp.ClientConnectionError()):
            plan = await self.actions.prepare(KEY, "block")
            self.command_error = error
            count = len(self.commands)
            with self.assertRaisesRegex(SourceError, "outcome unverified"):
                await self.actions.apply(KEY, plan["token"])
            self.assertEqual(len(self.commands), count + 1)
            with self.assertRaises(ActionConflict):
                await self.actions.apply(KEY, plan["token"])

    async def test_accepted_command_with_bad_readback_distinct_from_verified(self):
        for error in (TimeoutError(), SourceError("Readback unavailable"), None):
            self.user["blocked"] = False
            self.commands.clear()
            self.readback_error = None
            plan = await self.actions.prepare(KEY, "block")
            self.readback_error = error
            self.update_flag = False
            with self.assertRaisesRegex(SourceError, "accepted the command, but readback is unverified"):
                await self.actions.apply(KEY, plan["token"])
            self.assertEqual(len(self.commands), 1)

    async def test_api_csrf_ingress_schema_and_recovery_target(self):
        monitor = Monitor(self.config, self.store, Alerts(self.config, self.store, "", None))
        async with TestClient(TestServer(create_app(monitor, development=True, telemetry=self.telemetry))) as client:
            options = await (await client.get("/api/controls")).json()
            self.assertTrue(options["router_enabled"])
            self.assertEqual(options["router_targets"], [{"key": KEY, "mac": MAC}])
            headers = {"X-Network-Monitor": "1", "X-Linkwatch-Control": options["csrf"]}
            url = f"/api/clients/{KEY}/router-actions/"
            for h in ({}, {"X-Network-Monitor": "1"}, {**headers, "Sec-Fetch-Site": "cross-site"},
                      {**headers, "Sec-Fetch-Site": "same-site"}, {**headers, "X-Linkwatch-Control": "é"}):
                response = await client.post(url + "prepare", json={"action": "block"}, headers=h)
                self.assertEqual(response.status, 403)
            for body in ({"action": "block", "mac": MAC}, {"action": "block", "cmd": "delete"},
                         {"token": "fake"}, [], None):
                response = await client.post(url + "prepare", json=body, headers=headers)
                self.assertIn(response.status, (400, 415))
            self.telemetry.request.assert_not_awaited()
            response = await client.post(url + "prepare", json={"action": "block"}, headers=headers)
            self.assertEqual(response.status, 200)
            plan = await response.json()
            response = await client.post(url + "apply", json={"token": plan["token"]}, headers=headers)
            self.assertEqual(response.status, 200)
            self.telemetry.devices.clear()
            response = await client.get(f"/api/clients/{KEY}")
            self.assertEqual(response.status, 200)
            detail = await response.json()
            self.assertTrue(detail["client"]["stale"])
            self.assertEqual(detail["samples"], [])
        self.assertEqual(len(self.commands), 1)
        async with TestClient(TestServer(create_app(monitor, telemetry=self.telemetry))) as client:
            response = await client.get("/api/controls", headers={"X-Forwarded-For": "172.30.32.2"})
            self.assertEqual(response.status, 403)

    async def test_real_http_auth_cookies_csrf_and_fixed_command_contract(self):
        calls = []

        async def handler(request):
            calls.append((request.method, request.path))
            if request.path == "/api/auth/login":
                self.assertEqual((await request.json())["username"], self.config.unifi_control_username)
                response = web.json_response({"unique_id": "synthetic-admin"})
                claims = base64.urlsafe_b64encode(b'{"csrfToken":"synthetic-csrf"}').decode().rstrip("=")
                response.set_cookie("TOKEN", "synthetic." + claims + ".signature")
                return response
            self.assertIn("TOKEN", request.cookies)
            if request.path.endswith("/cmd/stamgr"):
                self.assertEqual(request.headers["X-CSRF-Token"], "synthetic-csrf")
                self.assertEqual(await request.json(), {"cmd": "block-sta", "mac": MAC})
                self.user["blocked"] = True
                return web.json_response({"meta": {"rc": "ok"}, "data": []})
            data = ([self.user] if request.path.endswith("/stat/user/" + MAC) else
                    self.devices if request.path.endswith("/stat/device") else self.networks)
            return web.json_response({"meta": {"rc": "ok"}, "data": data})

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        async with TestServer(app) as server:
            async def local_request(session, method, url, **kwargs):
                return await Telemetry.request(self.telemetry, session, method,
                                               str(server.make_url(url.removeprefix(self.config.unifi_url))), **kwargs)
            self.telemetry.request = local_request
            plan = await self.actions.prepare(KEY, "block")
            self.assertFalse(any(path.endswith("/cmd/stamgr") for _, path in calls))
            result = await self.actions.apply(KEY, plan["token"])
            self.assertTrue(result["blocked"])
            self.assertEqual(sum(path.endswith("/cmd/stamgr") for _, path in calls), 1)
            self.assertEqual(sum(path == "/api/auth/login" for _, path in calls), 2)

    async def test_semantic_command_rejection_is_unverified_and_not_retried(self):
        plan = await self.actions.prepare(KEY, "block")

        async def rejected(session, method, url, **kwargs):
            if url.endswith("/cmd/stamgr"):
                self.commands.append(kwargs["json"])
                return {"meta": {"rc": "error", "msg": "not retained or exposed"}, "data": []}
            return await self.request(session, method, url, **kwargs)

        self.telemetry.request.side_effect = rejected
        with self.assertRaisesRegex(SourceError, "did not accept"):
            await self.actions.apply(KEY, plan["token"])
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(self.user["blocked"])
        self.assertNotIn("not retained or exposed", "\n".join(self.store.db.iterdump()))

    async def test_source_failure_before_submit_and_cancelled_write_never_retry(self):
        plan = await self.actions.prepare(KEY, "block")
        self.telemetry.sources["unifi"]["error"] = "Read unavailable"
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        self.assertEqual(self.commands, [])
        self.telemetry.sources["unifi"]["error"] = None
        plan = await self.actions.prepare(KEY, "block")
        self.command_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.actions.apply(KEY, plan["token"])
        self.assertEqual(len(self.commands), 1)
        self.assertIn("request cancelled; outcome unverified", "\n".join(self.store.db.iterdump()))
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])

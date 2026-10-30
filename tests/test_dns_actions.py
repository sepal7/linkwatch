import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from network_monitor.app.alerts import Alerts
from network_monitor.app.config import Config
from network_monitor.app.dns_actions import ActionConflict, DNSActions, exact_domain
from network_monitor.app.service import Monitor
from network_monitor.app.storage import Store
from network_monitor.app.telemetry import SourceError, Telemetry, client_key
from network_monitor.app.web import create_app


MAC = "02:00:00:00:00:01"
KEY = client_key(MAC)


class DNSActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "actions.sqlite", 7, 1000)
        self.config = Config(pihole_url="https://dns.example", pihole_password="synthetic",
                             pihole_actions_enabled=True, pihole_action_group_ids=[7],
                             unifi_url="https://controller.example", unifi_api_key="synthetic")
        self.telemetry = Telemetry(self.config, self.store, "")
        self.telemetry.sources["unifi"].update(
            ts=time.time(), error=None, data={"clients": [{"key": KEY, "mac": MAC, "ip": "192.0.2.10"}]})
        self.telemetry.sample(time.time())
        self.groups = [{"id": 0, "name": "Default", "enabled": True, "date_modified": 1},
                       {"id": 7, "name": "Example children", "enabled": True, "date_modified": 2}]
        self.clients = [{"id": 5, "client": "192.0.2.10", "groups": [0, 7], "date_modified": 3}]
        self.rules = []
        self.actions = DNSActions(self.telemetry)
        self.telemetry.pihole_get = AsyncMock(side_effect=self.get)
        self.telemetry.pihole_request = AsyncMock(side_effect=self.write)
        self.telemetry.pihole_sid = "synthetic-sid"

    async def asyncTearDown(self):
        await self.actions.close()
        self.store.close()
        self.temp.cleanup()

    async def get(self, path, **params):
        if path == "groups":
            return {"groups": self.groups}
        if path == "clients/192.0.2.10":
            return {"clients": self.clients}
        if path == "domains/exact/example.com":
            return {"domains": self.rules}
        raise AssertionError("Unexpected API path " + path)

    async def write(self, method, path, **kwargs):
        self.assertEqual(method, "POST")
        self.assertIn(path, ("domains/allow/exact", "domains/deny/exact"))
        payload = kwargs["json"]
        self.assertEqual(payload["groups"], [7])
        self.rules = [{**payload, "type": path.split("/")[1], "kind": "exact", "id": 11}]
        return {"domains": self.rules, "processed": {"success": [{"item": payload["domain"]}], "errors": []}}

    async def test_default_off_configuration_never_falls_back_to_default(self):
        self.assertFalse(Config().pihole_actions_enabled)
        for kwargs in ({"pihole_action_group_ids": [0]}, {"pihole_action_group_ids": [True]},
                       {"pihole_action_group_ids": [1, 1]}, {"pihole_action_group_ids": "7"},
                       {"pihole_actions_enabled": True}, {"pihole_action_group_ids": list(range(1, 10))}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)
        self.telemetry.config = Config()
        with self.assertRaisesRegex(SourceError, "disabled"):
            await self.actions.prepare(KEY, "example.com", "allow")
        self.telemetry.pihole_get.assert_not_awaited()
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_domain_and_action_whitelist(self):
        self.assertEqual(exact_domain("EXAMPLE.COM."), "example.com")
        for domain in ("https://example.com", "*.example.com", "example.com/path", "example..com",
                       "example.com?x=1", "-example.com", "192.0.2.1", "localhost", "é.example",
                       "example.com\n", "example.com..", "", None, ["example.com"]):
            with self.subTest(domain=domain), self.assertRaises(SourceError):
                exact_domain(domain)
        for action in ("delete", "regex", "allow/exact", {}, None):
            with self.subTest(action=action), self.assertRaises(SourceError):
                await self.actions.prepare(KEY, "example.com", action)
        self.telemetry.pihole_get.assert_not_awaited()

    async def test_preview_is_read_only_exact_scope_and_single_use_apply_verifies_creation(self):
        plan = await self.actions.prepare(KEY, "EXAMPLE.COM.", "deny")
        self.assertEqual(plan["domain"], "example.com")
        self.assertEqual(plan["groups"], [{"id": 7, "name": "Example children"}])
        self.assertIn("ALL clients", plan["impact"])
        self.assertIn("overridden", plan["impact"])
        self.telemetry.pihole_request.assert_not_awaited()
        result = await self.actions.apply(KEY, plan["token"])
        self.assertEqual(result["status"], "created_verified")
        self.telemetry.pihole_request.assert_awaited_once()
        self.assertEqual(self.telemetry.pihole_request.call_args.kwargs["headers"],
                         {"X-FTL-SID": "synthetic-sid"})
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        dump = "\n".join(self.store.db.iterdump())
        self.assertIn("creation verified", dump)
        for sensitive in ("example.com", "192.0.2.10", "Example children", MAC, "synthetic", plan["token"]):
            self.assertNotIn(sensitive, dump)

    async def test_existing_rules_of_any_scope_are_never_modified(self):
        for action in ("allow", "deny"):
            for enabled in (True, False):
                self.rules = [{"domain": "example.com", "type": action, "kind": "exact",
                               "enabled": enabled, "groups": [0, 20], "comment": "Administrator rule"}]
                with self.subTest(action=action, enabled=enabled), self.assertRaisesRegex(
                        ActionConflict, "already exists"):
                    await self.actions.prepare(KEY, "example.com", "deny")
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_missing_disabled_default_or_ambiguous_group_stops_before_write(self):
        for groups in ([], [self.groups[0]], [{**self.groups[1], "enabled": False}],
                       [{**self.groups[1], "name": "Default"}], [self.groups[1]] * 2,
                       [{**self.groups[1], "date_modified": None}]):
            self.groups = groups
            with self.subTest(groups=groups), self.assertRaises(SourceError):
                await self.actions.prepare(KEY, "example.com", "allow")
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_exact_ip_and_explicit_all_group_membership_required(self):
        original = self.clients[0]
        for clients in ([], [original] * 2, [{**original, "client": MAC}],
                        [{**original, "client": "192.0.2.0/24"}], [{**original, "groups": [0]}],
                        [{**original, "groups": [True, 7]}]):
            self.clients = clients
            with self.subTest(clients=clients), self.assertRaises(ActionConflict):
                await self.actions.prepare(KEY, "example.com", "allow")
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_stale_changed_client_group_and_existing_rule_rechecked_before_apply(self):
        async def changed_and_apply(change):
            self.rules = []
            self.groups[1].update(name="Example children", date_modified=2)
            self.clients[0].update(groups=[0, 7], date_modified=3)
            self.telemetry.devices[KEY].update(present=True, last_seen=time.time())
            plan = await self.actions.prepare(KEY, "example.com", "allow")
            change()
            with self.assertRaises(SourceError):
                await self.actions.apply(KEY, plan["token"])

        for change in (lambda: self.groups[1].update(name="Renamed", date_modified=4),
                       lambda: self.clients[0].update(groups=[0]),
                       lambda: self.telemetry.devices[KEY].update(present=False),
                       lambda: self.telemetry.devices[KEY].update(binding_start=time.time()),
                       lambda: self.rules.append({"domain": "example.com"})):
            await changed_and_apply(change)
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_confirmation_expiry_foreign_key_and_bounded_pending_payloads(self):
        plan = await self.actions.prepare(KEY, "example.com", "allow")
        with self.assertRaises(ActionConflict):
            await self.actions.apply("different-key", plan["token"])
        with self.assertRaises(ActionConflict):
            await self.actions.apply(KEY, plan["token"])
        for _ in range(17):
            await self.actions.prepare(KEY, "example.com", "allow")
        self.assertEqual(len(self.actions.pending), 16)
        tick = max(p.created for p in self.actions.pending.values()) + 61
        self.actions.expiry.cancel()
        with patch("network_monitor.app.dns_actions.time.monotonic", return_value=tick):
            self.actions.prune()
        self.assertEqual(self.actions.pending, {})
        self.assertIsNone(self.actions.expiry)
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_semantic_errors_timeouts_and_readback_mismatch_never_retry_or_rollback(self):
        for mode in ("error", "timeout", "wrong_groups", "wrong_type", "opposite_rule", "wrong_comment"):
            self.rules = []
            plan = await self.actions.prepare(KEY, "example.com", "allow")

            async def write(method, path, **kwargs):
                if mode == "timeout":
                    raise TimeoutError
                result = await self.write(method, path, **kwargs)
                if mode == "error":
                    result["processed"]["errors"] = [{"item": "example.com", "error": "Internal private text"}]
                if mode == "wrong_groups":
                    self.rules[0]["groups"] = [0, 7]
                if mode == "wrong_type":
                    self.rules[0]["type"] = "deny"
                if mode == "opposite_rule":
                    self.rules.append({**self.rules[0], "type": "deny"})
                if mode == "wrong_comment":
                    self.rules[0]["comment"] = "Administrator change"
                return result

            self.telemetry.pihole_request = AsyncMock(side_effect=write)
            with self.subTest(mode=mode), self.assertRaisesRegex(SourceError, "outcome unverified"):
                await self.actions.apply(KEY, plan["token"])
            self.telemetry.pihole_request.assert_awaited_once()
            self.assertNotIn(plan["token"], self.actions.pending)
        self.assertNotIn("Internal private text", "\n".join(self.store.db.iterdump()))

    async def test_concurrent_apply_same_token_sends_one_write(self):
        plan = await self.actions.prepare(KEY, "example.com", "allow")
        results = await asyncio.gather(self.actions.apply(KEY, plan["token"]),
                                       self.actions.apply(KEY, plan["token"]), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ActionConflict) for r in results), 1)
        self.telemetry.pihole_request.assert_awaited_once()

    async def test_real_http_auth_transport_has_only_fixed_gets_and_one_exact_post(self):
        requests = []

        async def handler(request):
            requests.append((request.method, request.path))
            if request.path == "/api/auth":
                self.assertEqual(await request.json(), {"password": "synthetic"})
                return web.json_response({"session": {"valid": True, "sid": "synthetic-session"}})
            self.assertEqual(request.headers["X-FTL-SID"], "synthetic-session")
            path = request.path.removeprefix("/api/")
            if request.method == "POST":
                payload = await request.json()
                self.rules = [{**payload, "type": path.split("/")[1], "kind": "exact"}]
                return web.json_response({"processed": {"success": [{"item": payload["domain"]}],
                                                         "errors": []}}, status=201)
            return web.json_response(await self.get(path))

        app = web.Application()
        app.router.add_route("*", "/api/{path:.*}", handler)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            config = Config(pihole_url=str(server.make_url("")).rstrip("/"), pihole_password="synthetic",
                            pihole_actions_enabled=True, pihole_action_group_ids=[7])
            self.telemetry.config = config
            self.telemetry.session = session
            self.telemetry.pihole_sid = None
            del self.telemetry.pihole_get
            del self.telemetry.pihole_request
            plan = await self.actions.prepare(KEY, "example.com", "allow")
            result = await self.actions.apply(KEY, plan["token"])
            self.assertEqual(result["status"], "created_verified")
        self.assertEqual([path for method, path in requests if method != "GET"],
                         ["/api/auth", "/api/domains/allow/exact"])
        self.assertTrue(all(path in ("/api/auth", "/api/groups", "/api/clients/192.0.2.10",
                                    "/api/domains/exact/example.com", "/api/domains/allow/exact")
                            for _, path in requests))

    async def test_api_ingress_csrf_whitelist_and_disabled_behavior(self):
        monitor = Monitor(self.config, self.store, Alerts(self.config, self.store, "", object()))
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry))) as client:
            path = f"/api/clients/{KEY}/dns-actions/prepare"
            body = {"domain": "example.com", "action": "allow"}
            response = await client.get("/api/controls")
            options = await response.json()
            self.assertTrue(options["dns_enabled"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)
            headers = {"X-Network-Monitor": "1", "X-Linkwatch-Control": options["csrf"]}
            self.assertEqual((await client.post(path, json=body)).status, 403)
            self.assertEqual((await client.post(path, json=body,
                              headers={"X-Network-Monitor": "1"})).status, 403)
            for fetch in ("cross-site", "same-site", "none"):
                self.assertEqual((await client.post(path, json=body,
                                  headers={**headers, "Sec-Fetch-Site": fetch})).status, 403)
            for bad in ({**body, "groups": [0]}, {**body, "url": "https://other.example"}, {}):
                self.assertEqual((await client.post(path, json=bad, headers=headers)).status, 400)
            self.assertEqual((await client.post(path.replace("prepare", "delete"), json=body,
                              headers=headers)).status, 404)
            self.telemetry.pihole_get.assert_not_awaited()
            response = await client.post(path, json=body, headers={**headers, "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(response.status, 200)
            plan = await response.json()
            response = await client.post(path.replace("prepare", "apply"),
                                         json={"token": plan["token"]}, headers=headers)
            self.assertEqual(response.status, 200)
            response = await client.post(path.replace("prepare", "apply"),
                                         json={"token": plan["token"]}, headers=headers)
            self.assertEqual(response.status, 409)
            self.assertNotIn("synthetic", await response.text())
        async with TestClient(TestServer(create_app(monitor, telemetry=self.telemetry))) as client:
            self.assertEqual((await client.get("/api/controls")).status, 403)
            self.assertEqual((await client.post(path, json=body, headers=headers)).status, 403)
        monitor = Monitor(Config(), self.store, Alerts(Config(), self.store, "", object()))
        async with TestClient(TestServer(create_app(monitor, development=True))) as client:
            options = await (await client.get("/api/controls")).json()
            self.assertFalse(options["dns_enabled"])
            self.assertFalse(options["router_enabled"])
            self.assertIsNone(options["csrf"])
            self.assertEqual(options["dns"]["saved_settings"], "unavailable")
            self.assertNotIn("pihole_password", json.dumps(options))

    async def test_effective_group_two_and_provable_saved_restart_status_without_secrets(self):
        config = replace(self.config, pihole_action_group_ids=[2])
        self.telemetry.config = config
        monitor = Monitor(config, self.store, Alerts(config, self.store, "", object()))
        path = Path(self.temp.name) / "options.json"
        def save(flag, groups):
            path.write_text(json.dumps({"pihole_actions_enabled": flag, "pihole_action_group_ids": groups,
                                         "pihole_password": "private-not-returned"}), encoding="utf-8")
        save(True, [2])
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry, options_path=path))) as client:
            for flag, groups, status in ((True, [2], "match"), (False, [2], "different"),
                                         (True, [3], "different"), (True, 2, "invalid"),
                                         ("true", [2], "invalid")):
                save(flag, groups)
                response = await client.get("/api/controls")
                options = await response.json()
                self.assertEqual(options["controls_schema"], 2)
                self.assertTrue(options["dns_enabled"])
                self.assertEqual(options["dns"]["group_ids"], [2])
                self.assertEqual(options["dns"]["saved_settings"], status)
                self.assertNotIn("private-not-returned", json.dumps(options))
            path.unlink()
            options = await (await client.get("/api/controls")).json()
            self.assertEqual(options["dns"]["saved_settings"], "unavailable")
        self.telemetry.pihole_get.assert_not_awaited()
        self.telemetry.pihole_request.assert_not_awaited()

    async def test_running_disabled_saved_enabled_is_reported_not_implicitly_enabled(self):
        config = replace(self.config, pihole_actions_enabled=False, pihole_action_group_ids=[2])
        self.telemetry.config = config
        path = Path(self.temp.name) / "options.json"
        path.write_text('{"pihole_actions_enabled":true,"pihole_action_group_ids":[2]}', encoding="utf-8")
        monitor = Monitor(config, self.store, Alerts(config, self.store, "", object()))
        async with TestClient(TestServer(create_app(
                monitor, development=True, telemetry=self.telemetry, options_path=path))) as client:
            options = await (await client.get("/api/controls")).json()
            self.assertFalse(options["dns_enabled"])
            self.assertEqual(options["dns"]["reason"], "disabled")
            self.assertEqual(options["dns"]["saved_settings"], "different")
            self.assertIsNone(options["csrf"])
        with self.assertRaisesRegex(SourceError, "disabled"):
            await self.actions.prepare(KEY, "example.com", "allow")

    def test_group_two_loads_from_saved_options_without_coercing_scalars(self):
        path = Path(self.temp.name) / "load-options.json"
        options = {"pihole_url": "https://dns.example", "pihole_password": "synthetic",
                   "pihole_actions_enabled": True, "pihole_action_group_ids": [2]}
        path.write_text(json.dumps(options), encoding="utf-8")
        loaded = Config.load(path)
        self.assertTrue(loaded.pihole_actions_enabled)
        self.assertEqual(loaded.pihole_action_group_ids, [2])
        options["pihole_action_group_ids"] = 2
        path.write_text(json.dumps(options), encoding="utf-8")
        with self.assertRaises(ValueError):
            Config.load(path)


if __name__ == "__main__":
    unittest.main()

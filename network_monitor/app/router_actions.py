"""Explicit manual UniFi shared-block controls, isolated from monitoring credentials."""

import asyncio
import base64
import ipaddress
import json
import secrets
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import aiohttp

from .actions import ActionConflict, Confirmations
from .telemetry import SourceError, client_key, object_data, safe_text


@dataclass(frozen=True)
class RouterPlan:
    key: str
    action: str
    state: tuple
    created: float
    operation: str


class RouterActions(Confirmations):
    def targets(self):
        return [{"key": client_key(mac), "mac": mac.lower()}
                for mac in self.telemetry.config.unifi_control_client_macs]

    def target(self, key):
        if not self.telemetry.config.unifi_controls_enabled:
            raise SourceError("UniFi controls are disabled; use a separate control account and explicit client MAC list")
        matches = [target["mac"] for target in self.targets() if target["key"] == key]
        if len(matches) != 1:
            raise ActionConflict("This MAC is not explicitly configured for router controls")
        return matches[0]

    @asynccontextmanager
    async def connection(self):
        # Never reuse, upgrade or clear the working read-only account's cookie jar.
        config = self.telemetry.config
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            result = await self.telemetry.request(
                session, "POST", config.unifi_url.rstrip("/") + "/api/auth/login",
                ssl=self.telemetry.fingerprint,
                json={"username": config.unifi_control_username, "password": config.unifi_control_password})
            if result.get("error") or object_data(result.get("meta", {})).get("rc", "ok") != "ok":
                raise SourceError("UniFi control authentication was rejected")
            yield session

    def csrf(self, session):
        for cookie in session.cookie_jar:
            if cookie.key != "TOKEN" or len(cookie.value) > 16384:
                continue
            try:
                payload = cookie.value.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                token = claims.get("csrfToken")
                if isinstance(token, str) and 0 < len(token) <= 512 and token.isascii() and all(
                        32 < ord(char) < 127 for char in token):
                    return token
            except (ValueError, IndexError, AttributeError):
                break
        # The JWT is only a TLS-delivered CSRF carrier, never a local authorization decision.
        raise SourceError("UniFi control session has no supported CSRF token; no command sent")

    async def read(self, session, suffix):
        site = quote(self.telemetry.config.unifi_legacy_site, safe="")
        result = await self.telemetry.request(
            session, "GET", self.telemetry.config.unifi_url.rstrip("/") +
            "/proxy/network/api/s/" + site + suffix, ssl=self.telemetry.fingerprint)
        if object_data(result.get("meta", {})).get("rc") != "ok":
            raise SourceError("UniFi rejected the fixed control-state read; check control account permissions")
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) > 512 or any(not isinstance(row, dict) for row in rows):
            raise SourceError("UniFi control-state collection schema or size is unsupported")
        return rows

    async def protected_addresses(self):
        config = self.telemetry.config
        protected = {str(ipaddress.ip_address(config.unifi_control_ha_ip))}
        for ip in (config.router_host, config.dns_server):
            if ip:
                protected.add(str(ipaddress.ip_address(ip)))
        # Fail closed if configured monitoring infrastructure cannot be resolved.
        for url in (config.unifi_url, config.pihole_url):
            if not url:
                continue
            host = urlsplit(url).hostname
            try:
                protected.add(str(ipaddress.ip_address(host)))
            except ValueError:
                addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
                if not addresses:
                    raise SourceError("Cannot identify a configured infrastructure host; router action stopped")
                protected.update(str(ipaddress.ip_address(row[4][0])) for row in addresses)
        return protected

    def current_user(self, rows, mac):
        if len(rows) != 1 or str(rows[0].get("mac", "")).lower() != mac:
            raise ActionConflict("Router did not return one exact current MAC identity")
        row = rows[0]
        if not isinstance(row.get("_id"), str) or not row["_id"] or type(row.get("blocked")) is not bool:
            raise SourceError("Router client identity or explicit blocked flag is unavailable; no state inferred")
        try:
            ip = str(ipaddress.ip_address(row.get("ip", "")))
        except ValueError:
            raise ActionConflict("Router client has no usable current IP for infrastructure safeguards") from None
        return row["_id"], mac, ip, row["blocked"]

    def binding(self, key, state):
        device = self.telemetry.devices.get(key)
        if device is None or not device.get("present"):
            return None
        if str(device.get("mac", "")).lower() != state[1] or device.get("ip") != state[2]:
            raise ActionConflict("Selected inventory identity and current router MAC/IP disagree; refresh details")
        return device.get("binding_start")

    def block_selection(self, key, state):
        device = self.telemetry.devices.get(key)
        fresh = any(s["enabled"] and not s["error"] and s["ts"] is not None and
                    0 <= time.time() - s["ts"] <= self.telemetry.config.telemetry_interval_seconds * 3
                    for n, s in self.telemetry.sources.items() if n in ("unifi", "legacy"))
        if (not device or not device.get("present") or not fresh or
                not 0 <= time.time() - device["last_seen"] <= self.telemetry.config.telemetry_interval_seconds * 3):
            raise ActionConflict("Blocking requires a fresh connected-device inventory selection")
        if self.binding(key, state) != state[4]:
            raise ActionConflict("Selected device binding changed; preview again")

    async def inspect(self, session, key):
        mac = self.target(key)
        users, devices, networks, protected = await asyncio.gather(
            self.read(session, "/stat/user/" + mac),
            self.read(session, "/stat/device"),
            self.read(session, "/rest/networkconf"),
            self.protected_addresses())
        state = self.current_user(users, mac)
        if not devices or not networks:
            raise SourceError("Gateway/infrastructure safeguards require complete router device and network reads")
        infrastructure = set()
        for device in devices:
            device_mac = device.get("mac")
            if not isinstance(device_mac, str) or not device_mac:
                raise SourceError("Infrastructure identity schema is unsupported")
            infrastructure.add(device_mac.lower())
            if device.get("ip"):
                try:
                    protected.add(str(ipaddress.ip_address(device["ip"])))
                except ValueError:
                    raise SourceError("Infrastructure IP schema is unsupported") from None
        for network in networks:
            if network.get("ip_subnet"):
                try:
                    protected.add(str(ipaddress.ip_interface(network["ip_subnet"]).ip))
                except ValueError:
                    raise SourceError("Router gateway schema is unsupported") from None
            elif network.get("purpose") not in ("wan", "vlan-only"):
                raise SourceError("A routed network has no supported gateway; router action stopped")
        ip = ipaddress.ip_address(state[2])
        if ip.version != 4:
            raise SourceError("Router controls currently require a reported IPv4 client address for safeguards")
        if (mac in infrastructure or state[2] in protected or ip.is_multicast or ip.is_unspecified or
                ip.is_loopback or users[0].get("is_gateway") or users[0].get("is_unifi") or
                users[0].get("is_infrastructure")):
            raise ActionConflict("Infrastructure, gateway, controller, DNS and Home Assistant hosts cannot be controlled")
        if sum(str(d.get("mac", "")).lower() == mac for d in self.telemetry.devices.values()) > 1:
            raise ActionConflict("Selected MAC is ambiguous in retained inventory")
        if sum(d.get("ip") == state[2] and d.get("present") for d in self.telemetry.devices.values()) > 1:
            raise ActionConflict("Selected IP is shared in current inventory")
        binding = self.binding(key, state)
        return state + (binding,), safe_text(users[0].get("name") or users[0].get("hostname"), 120)

    async def prepare(self, key, action):
        if not isinstance(action, str) or action not in ("block", "unblock"):
            raise SourceError("Router action must be block or unblock")
        self.target(key)
        async with asyncio.timeout(20), self.lock, self.connection() as session:
            state, name = await self.inspect(session, key)
            if state[3] == (action == "block"):
                raise ActionConflict("Router already reports " + ("BLOCKED" if state[3] else "UNBLOCKED") +
                                     "; no change is needed")
            self.csrf(session)
            if action == "block":
                self.block_selection(key, state)
            plan = RouterPlan(key, action, state, time.monotonic(), secrets.token_hex(8))
            return {"token": self.remember(plan), "operation": plan.operation,
                    "action": action, "name": name or "Configured client", "mac": state[1], "ip": state[2],
                    "blocked": state[3], "expires_in": 60,
                    "private_mac": bool(int(state[1][:2], 16) & 2),
                    "impact": "Block network access may interrupt local-network/Wi-Fi access as well as internet. "
                              "Unblock router block clears the shared block flag, EVEN if set outside Linkwatch. "
                              "Neither action edits separate firewall, schedule or DNS rules. "
                              "There is no automatic unblock or timer. A MAC identifies a device interface, not a person; "
                              "private/randomized MAC changes create separate targets."}

    def audit(self, plan, outcome):
        self.telemetry.store.log(time.time(), "router_action",
                                 f"Router {plan.action} operation {plan.operation}: {outcome}. "
                                 "No client metadata retained.")

    async def apply(self, key, token):
        self.target(key)
        plan = self.take(key, token)
        attempted = accepted = False
        try:
            async with asyncio.timeout(30), self.lock, self.connection() as session:
                if time.monotonic() - plan.created >= 60:
                    raise ActionConflict("Confirmation expired while waiting; preview again")
                state, _ = await self.inspect(session, key)
                if state != plan.state:
                    raise ActionConflict("Router block state or selected MAC/IP binding changed; preview again")
                if plan.action == "block":
                    self.block_selection(key, state)
                if time.monotonic() - plan.created >= 60:
                    raise ActionConflict("Confirmation expired during preflight; preview again")
                csrf = self.csrf(session)
                self.audit(plan, "write requested; outcome pending")
                attempted = True
                # Only these two commands are possible. No body/path comes from the caller.
                command = "block-sta" if plan.action == "block" else "unblock-sta"
                result = await self.telemetry.request(
                    session, "POST", self.telemetry.config.unifi_url.rstrip("/") + "/proxy/network/api/s/" +
                    quote(self.telemetry.config.unifi_legacy_site, safe="") + "/cmd/stamgr",
                    ssl=self.telemetry.fingerprint, headers={"X-CSRF-Token": csrf},
                    json={"cmd": command, "mac": plan.state[1]})
                if object_data(result.get("meta", {})).get("rc") != "ok":
                    raise SourceError("Router did not accept the requested command")
                accepted = True
                rows = await self.read(session, "/stat/user/" + plan.state[1])
                actual = self.current_user(rows, plan.state[1])
                if actual[:3] != plan.state[:3] or actual[3] != (plan.action == "block"):
                    raise SourceError("Router readback did not verify the requested shared block state")
                self.audit(plan, "shared flag verified; effective access not tested")
                return {"operation": plan.operation, "status": "flag_verified",
                        "blocked": actual[3],
                        "message": "Router shared block flag verified as " + ("BLOCKED" if actual[3] else "UNBLOCKED") +
                                   ". Effective access was not tested; separate firewall, schedule and DNS rules remain. "
                                   "Linkwatch will not automatically change this flag."}
        except asyncio.CancelledError:
            self.audit(plan, "request cancelled; outcome unverified" if attempted else "preflight cancelled; no command sent")
            raise
        except (SourceError, aiohttp.ClientError, OSError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            self.audit(plan, ("command accepted; readback unverified" if accepted else "outcome unverified")
                       if attempted else "preflight stopped; no command sent")
            if attempted:
                prefix = "Router accepted the command, but readback is unverified. " if accepted else "Router command outcome unverified. "
                reason = str(exc) + ". " if isinstance(exc, SourceError) else ""
                raise SourceError(prefix + reason +
                                  "Inspect the device in UniFi before retrying; no automatic retry was made") from None
            raise

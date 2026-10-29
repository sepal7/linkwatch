"""Confirmed create-only exact DNS rules for explicitly selected existing groups."""

import asyncio
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from .telemetry import SourceError, object_data
from .actions import ActionConflict, Confirmations


@dataclass(frozen=True)
class DNSPlan:
    key: str
    domain: str
    action: str
    state: tuple
    created: float
    operation: str


def exact_domain(value):
    if not isinstance(value, str) or not value.isascii():
        raise SourceError("Choose an ASCII DNS hostname; use its punycode form for international names")
    value = value.lower().removesuffix(".")
    if (len(value) > 253 or "." not in value or
            not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in value.split("."))):
        raise SourceError("Choose an exact DNS hostname, not a URL, wildcard, address or regular expression")
    if all(label.isdigit() for label in value.split(".")):
        raise SourceError("DNS actions require a domain name, not an IP address")
    return value


class DNSActions(Confirmations):
    def enabled(self):
        config = self.telemetry.config
        if not config.pihole_actions_enabled or not config.pihole_action_group_ids:
            raise SourceError("DNS actions are disabled; configure existing non-Default kids groups first")

    def binding(self, key):
        device, ip = self.telemetry.dns_binding(key)
        return ip, device["binding_start"]

    async def inspect(self, key, domain):
        self.enabled()
        binding = self.binding(key)
        results = await asyncio.gather(
            self.telemetry.pihole_get("groups"),
            self.telemetry.pihole_get("clients/" + quote(binding[0], safe="")),
            self.telemetry.pihole_get("domains/exact/" + quote(domain, safe="")),
            return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
            if not isinstance(result, dict) or result.get("error"):
                raise SourceError("Pi-hole rejected the DNS action preflight")
        groups = results[0].get("groups")
        clients = results[1].get("clients")
        rules = results[2].get("domains")
        if not isinstance(groups, list) or len(groups) > 256:
            raise SourceError("Pi-hole group schema or size is unsupported")
        selected, seen = [], set()
        for group in groups:
            group = object_data(group)
            identifier = group.get("id")
            if type(identifier) is not int or identifier in seen:
                raise SourceError("Pi-hole group identity is ambiguous")
            seen.add(identifier)
            if identifier not in self.telemetry.config.pihole_action_group_ids:
                continue
            name, modified = group.get("name"), group.get("date_modified")
            if (identifier == 0 or group.get("enabled") is not True or
                    not isinstance(name, str) or not name or len(name) > 120 or
                    name.casefold() == "default" or type(modified) is not int):
                raise ActionConflict("Configured kids group is disabled, Default or unsupported; review Pi-hole groups")
            selected.append((identifier, name, modified))
        if len(selected) != len(self.telemetry.config.pihole_action_group_ids):
            raise ActionConflict("A configured kids group no longer exists; no Default fallback is allowed")
        if not isinstance(clients, list) or len(clients) != 1:
            raise ActionConflict("An explicit Pi-hole client entry for this exact IP is required to verify group scope")
        client = object_data(clients[0])
        membership = client.get("groups")
        if (client.get("client") != binding[0] or not isinstance(membership, list) or
                any(type(identifier) is not int or identifier < 0 for identifier in membership) or
                type(client.get("id")) is not int or type(client.get("date_modified")) is not int):
            raise ActionConflict("Cannot verify exact client-IP group membership; no DNS rule was changed")
        # IP entries take precedence over subnet/MAC/hostname matches in Pi-hole.
        if not set(identifier for identifier, _, _ in selected).issubset(membership):
            raise ActionConflict("This exact Pi-hole client IP is not in every configured kids group")
        if not isinstance(rules, list) or len(rules) > 8:
            raise SourceError("Pi-hole exact-domain schema is unsupported")
        if rules:
            # Never replace, move, enable, reassign or delete any existing administrator rule.
            raise ActionConflict("An exact allow/deny entry already exists for this domain; manage it in Pi-hole")
        if self.binding(key) != binding:
            raise ActionConflict("Client identity changed during preflight; reload device details")
        return (binding, tuple(sorted(selected)), client["id"], client["date_modified"], tuple(sorted(membership)))

    async def prepare(self, key, domain, action):
        if not isinstance(action, str) or action not in ("allow", "deny"):
            raise SourceError("DNS action must be allow or deny")
        domain = exact_domain(domain)
        async with asyncio.timeout(20), self.lock:
            state = await self.inspect(key, domain)
            plan = DNSPlan(key, domain, action, state, time.monotonic(), secrets.token_hex(8))
            return {"token": self.remember(plan), "operation": plan.operation, "domain": domain, "action": action,
                    "groups": [{"id": identifier, "name": name} for identifier, name, _ in state[1]],
                    "expires_in": 60,
                    "impact": "Creates one exact-domain rule for ALL clients in these groups, not only this device. "
                              "No subdomains, group memberships or existing rules are changed. "
                              "Allow can bypass existing DNS filters; deny may be overridden by allow rules. "
                              "Caches, existing connections and alternate/encrypted DNS can bypass or delay its effect. "
                              "This does not change UniFi restrictions or guarantee access/blocking."}

    def audit(self, plan, outcome):
        self.telemetry.store.log(time.time(), "dns_action",
                                 f"DNS {plan.action} operation {plan.operation}: {outcome}; "
                                 f"group count {len(plan.state[1])}. No domain/client metadata retained.")

    async def apply(self, key, token):
        self.enabled()
        plan = self.take(key, token)
        attempted = False
        try:
            async with asyncio.timeout(20), self.lock:
                if time.monotonic() - plan.created >= 60:
                    raise ActionConflict("Confirmation expired while waiting; preview again")
                if await self.inspect(key, plan.domain) != plan.state:
                    raise ActionConflict("Group or client state changed; preview and confirm the new scope")
                if self.binding(key) != plan.state[0]:
                    raise ActionConflict("Client identity changed; preview again")
                if time.monotonic() - plan.created >= 60:
                    raise ActionConflict("Confirmation expired during preflight; preview again")
                if not isinstance(self.telemetry.pihole_sid, str) or not self.telemetry.pihole_sid:
                    raise SourceError("Pi-hole session changed; preview again before writing")
                self.audit(plan, "write requested; outcome pending")
                attempted = True
                result = await self.telemetry.pihole_request(
                    "POST", "domains/" + plan.action + "/exact",
                    headers={"X-FTL-SID": self.telemetry.pihole_sid},
                    json={"domain": plan.domain, "groups": [g[0] for g in plan.state[1]],
                          "enabled": True, "comment": "Linkwatch operation " + plan.operation})
                processed = object_data(result.get("processed"))
                if (result.get("error") or processed.get("errors") != [] or
                        processed.get("success") != [{"item": plan.domain}]):
                    raise SourceError("Pi-hole did not confirm the requested rule creation")
                # Read back from the same API. Never retry a write on timeout or uncertain response.
                result = await self.telemetry.pihole_get("domains/exact/" + quote(plan.domain, safe=""))
                rules = result.get("domains")
                if result.get("error") or not isinstance(rules, list) or len(rules) != 1:
                    raise SourceError("DNS rule readback is ambiguous")
                rule = object_data(rules[0])
                if (rule.get("domain") != plan.domain or rule.get("type") != plan.action or
                        rule.get("kind") != "exact" or rule.get("enabled") is not True or
                        not isinstance(rule.get("groups"), list) or
                        any(type(group) is not int for group in rule["groups"]) or
                        sorted(rule["groups"]) != sorted(g[0] for g in plan.state[1]) or
                        rule.get("comment") != "Linkwatch operation " + plan.operation):
                    raise SourceError("DNS rule readback did not match the confirmed scope")
                self.audit(plan, "creation verified")
                return {"operation": plan.operation, "status": "created_verified",
                        "message": "Exact DNS rule creation verified for the confirmed groups. "
                                   "This verifies configuration, not effective blocking or access. "
                                   "Manage changes/removal of this rule in Pi-hole."}
        except asyncio.CancelledError:
            self.audit(plan, "request cancelled; outcome unverified" if attempted else "preflight cancelled; no write sent")
            raise
        except (SourceError, aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
            self.audit(plan, "outcome unverified; inspect Pi-hole before retrying" if attempted else "preflight stopped; no write sent")
            if attempted:
                raise SourceError("DNS action outcome unverified. Inspect the domain in Pi-hole before retrying; "
                                  "Linkwatch will not retry, remove or overwrite any rule") from None
            raise

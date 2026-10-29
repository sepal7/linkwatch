"""Short-lived, single-use confirmations shared by the fixed action handlers."""

import asyncio
import secrets
import time

from .telemetry import SourceError


class ActionConflict(SourceError):
    pass


class Confirmations:
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.lock = asyncio.Lock()
        self.pending = {}
        self.expiry = None

    def prune(self):
        now = time.monotonic()
        self.pending = {token: plan for token, plan in self.pending.items() if now - plan.created < 60}
        self.expiry = None
        if self.pending:
            delay = max(0.1, min(plan.created + 60 - now for plan in self.pending.values()))
            self.expiry = asyncio.get_running_loop().call_later(delay, self.prune)

    def remember(self, plan):
        self.pending = {k: v for k, v in self.pending.items() if time.monotonic() - v.created < 60}
        if len(self.pending) >= 16:
            del self.pending[min(self.pending, key=lambda k: self.pending[k].created)]
        token = secrets.token_urlsafe(32)
        self.pending[token] = plan
        if self.expiry is None:
            self.prune()
        return token

    def take(self, key, token):
        plan = self.pending.pop(token, None) if isinstance(token, str) else None
        if not plan or plan.key != key or time.monotonic() - plan.created >= 60:
            raise ActionConflict("Confirmation expired, already used or belongs to another device; preview again")
        return plan

    async def close(self, app=None):
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None
        self.pending.clear()

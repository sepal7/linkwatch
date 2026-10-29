import asyncio
import logging
import time

from .alerts import Alerts
from .config import Config
from .debounce import Debounce
from .probes import classify, measure
from .storage import Store

LOGGER = logging.getLogger(__name__)


class Monitor:
    def __init__(self, config: Config, store: Store, alerts: Alerts):
        self.config, self.store, self.alerts = config, store, alerts
        self.debounce = Debounce(config.alert_failure_seconds, config.alert_recovery_seconds)
        self.error: str | None = None
        self.running = False

    async def run(self):
        self.running = True
        previous_tick = None
        try:
            while True:
                tick = time.monotonic()
                if previous_tick is not None and tick - previous_tick > self.config.stale_after:
                    self.debounce.observe("unknown", tick)
                results = await measure(self.config)
                state = classify(results)
                now = time.time()
                gap = self.store.record(now, state, [r.json() for r in results],
                                        self.config.stale_after)
                if gap:
                    self.debounce.observe("unknown", tick)
                alert = self.debounce.observe(state, time.monotonic())
                if alert:
                    self.alerts.enqueue(alert, now)
                previous_tick = tick
                # Never overlap samples or invent ticks to catch up after delays.
                elapsed = time.monotonic() - tick
                await asyncio.sleep(max(0.1, self.config.interval_seconds - elapsed))
        finally:
            self.running = False

    def status(self, now: float):
        latest = self.store.last
        age = max(0, now - latest["ts"]) if latest else None
        stale = (not self.running or self.store.active_id is None or
                 latest is None or now < latest["ts"] or age > self.config.stale_after)
        return {
            "state": "unknown" if stale else latest["state"],
            "stale": stale,
            "age_seconds": age,
            "last": latest,
            "monitor_error": self.error,
            "interval_seconds": self.config.interval_seconds,
            "timeout_seconds": self.config.timeout_seconds,
            "stale_after_seconds": self.config.stale_after,
            "retention_days": self.config.retention_days,
            "max_samples": self.config.max_samples,
            "alerts": {
                "enabled": self.config.alerts_enabled,
                "muted": self.alerts.muted,
                "target": self.config.media_player,
                "output": self.config.sound_output,
                "phone_service": self.config.phone_notify_service,
                "failure_seconds": self.config.alert_failure_seconds,
                "recovery_seconds": self.config.alert_recovery_seconds,
                "last_error": self.alerts.last_error,
            },
            "server_time": now,
        }

    def completed(self, task: asyncio.Task):
        if not task.cancelled() and (exc := task.exception()):
            self.error = f"Monitoring stopped: {type(exc).__name__}: {exc}"
            LOGGER.error("%s", self.error)
            self.store.log(time.time(), "monitor_error", self.error)

class Debounce:
    """Alert state is independent of every raw observed state transition."""

    def __init__(self, failure_seconds: int, recovery_seconds: int):
        self.failure_seconds = failure_seconds
        self.recovery_seconds = recovery_seconds
        self.announced_down = False
        self.candidate: str | None = None
        self.since = 0.0

    def observe(self, state: str, monotonic_now: float) -> str | None:
        if state not in ("online", "degraded", "offline"):
            self.candidate = None
            return None
        desired = "down" if state == "offline" else "recovery"
        if (desired == "down") == self.announced_down:
            self.candidate = None
            return None
        if self.candidate != desired:
            self.candidate, self.since = desired, monotonic_now
        wait = self.failure_seconds if desired == "down" else self.recovery_seconds
        if monotonic_now - self.since < wait:
            return None
        self.announced_down = desired == "down"
        self.candidate = None
        return desired

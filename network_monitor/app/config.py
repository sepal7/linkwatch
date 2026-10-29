import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    interval_seconds: int = 2
    timeout_seconds: int = 1
    external_targets: list[str] = field(
        default_factory=lambda: ["1.1.1.1:443", "8.8.8.8:53"]
    )
    router_host: str = ""
    router_port: int = 80
    dns_server: str = ""
    dns_test_name: str = "example.com"
    retention_days: int = 7
    max_samples: int = 200000
    alert_failure_seconds: int = 15
    alert_recovery_seconds: int = 10
    alerts_enabled: bool = False
    sound_output: str = "cast"
    media_player: str = ""
    phone_notify_service: str = ""

    def __post_init__(self):
        bounds = {
            "interval_seconds": (2, 300), "timeout_seconds": (1, 10),
            "router_port": (1, 65535), "retention_days": (1, 90),
            "max_samples": (1000, 1000000),
            "alert_failure_seconds": (0, 600), "alert_recovery_seconds": (0, 600),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in {low}..{high}")
        if type(self.alerts_enabled) is not bool:
            raise ValueError("alerts_enabled must be boolean")
        for name in ("router_host", "dns_server", "dns_test_name",
                     "media_player", "sound_output", "phone_notify_service"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be text")
        if not isinstance(self.external_targets, list) or not 2 <= len(self.external_targets) <= 8:
            raise ValueError("Configure 2..8 external IP:port targets")
        hosts = set()
        for target in self.external_targets:
            host, _ = parse_target(target)
            if not ipaddress.ip_address(host).is_global:
                raise ValueError("External targets must use public IP addresses")
            hosts.add(host)
        if len(hosts) < 2:
            raise ValueError("Use at least two different external IP addresses")
        if self.router_host:
            ipaddress.ip_address(self.router_host)
        if self.dns_server:
            ipaddress.ip_address(self.dns_server)
        if not re.fullmatch(r"(?=.{1,253}\.?$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.?", self.dns_test_name):
            raise ValueError("dns_test_name must be a DNS hostname")
        if self.media_player and not re.fullmatch(r"media_player\.[a-z0-9_]+", self.media_player):
            raise ValueError("media_player must be a media_player entity ID")
        if self.sound_output not in ("local", "cast", "both", "none"):
            raise ValueError("sound_output must be local, cast, both or none")
        if self.phone_notify_service and not re.fullmatch(
                r"notify\.mobile_app_[a-z0-9_]+", self.phone_notify_service):
            raise ValueError("phone_notify_service must be notify.mobile_app_<device>")
        if self.alerts_enabled and self.sound_output in ("cast", "both") and not self.media_player:
            raise ValueError("Enabled Cast alerts require media_player")
        if self.alerts_enabled and self.sound_output == "none" and not self.phone_notify_service:
            raise ValueError("Enable at least one alert output")

    @property
    def stale_after(self):
        return max(self.interval_seconds * 3, self.interval_seconds + self.timeout_seconds * 2)

    @classmethod
    def load(cls, path: Path):
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def parse_target(value: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError("Target must be IP:port")
    try:
        host, port = value.rsplit(":", 1)
        host = str(ipaddress.ip_address(host.strip("[]")))
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("Port outside 1..65535")
        return host, port
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid target {value!r}; use IP:port (or [IPv6]:port)") from exc

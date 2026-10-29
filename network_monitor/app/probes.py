import asyncio
import ipaddress
import time
from dataclasses import asdict, dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

from .config import Config, parse_target


@dataclass
class Result:
    name: str
    kind: str
    outcome: str
    latency_ms: float | None = None
    detail: str = ""

    def json(self):
        return asdict(self)


async def tcp(name: str, kind: str, host: str, port: int, timeout: int) -> Result:
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        # Do not let a peer delay the next observation by holding close open.
        return Result(name, kind, "ok", round((time.monotonic() - started) * 1000, 1))
    except (TimeoutError, ConnectionError) as exc:
        return Result(name, kind, "fail", detail=type(exc).__name__)
    except OSError as exc:
        # Route/host unreachable are network observations; descriptor exhaustion,
        # permissions, unsupported address families etc. are monitor failures.
        import errno
        outcome = "fail" if exc.errno in {
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT,
            errno.ECONNREFUSED, errno.ECONNRESET,
        } else "error"
        return Result(name, kind, outcome, detail=f"{type(exc).__name__}: {exc}")


async def dns_probe(server: str, name: str, timeout: int) -> Result:
    started = time.monotonic()
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [server]
    resolver.timeout = timeout
    resolver.lifetime = timeout
    try:
        answer = await resolver.resolve(name, "A", lifetime=timeout, search=False)
        addresses = [ipaddress.ip_address(item.address) for item in answer]
        if not addresses or not all(address.is_global for address in addresses):
            return Result(server, "dns", "fail", detail="No usable public A answers")
        return Result(server, "dns", "ok", round((time.monotonic() - started) * 1000, 1),
                      ", ".join(map(str, addresses)))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
            dns.resolver.NoNameservers, dns.exception.Timeout) as exc:
        return Result(server, "dns", "fail", detail=type(exc).__name__)
    except (OSError, dns.exception.DNSException) as exc:
        return Result(server, "dns", "error", detail=f"{type(exc).__name__}: {exc}")


async def measure(config: Config) -> list[Result]:
    work = [
        tcp(target, "external", *parse_target(target), config.timeout_seconds)
        for target in config.external_targets
    ]
    if config.router_host:
        work.append(tcp(config.router_host, "router", config.router_host,
                        config.router_port, config.timeout_seconds))
    if config.dns_server:
        work.append(dns_probe(config.dns_server, config.dns_test_name, config.timeout_seconds))
    return list(await asyncio.gather(*work))


def classify(results: list[Result]) -> str:
    external = [r for r in results if r.kind == "external"]
    if not external or all(r.outcome == "error" for r in external):
        return "unknown"
    if any(r.outcome == "ok" for r in external):
        return "online" if all(r.outcome == "ok" for r in results) else "degraded"
    if any(r.outcome == "error" for r in external):
        return "unknown"
    return "offline"

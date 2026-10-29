import argparse
import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .alerts import Alerts, create_sounds
from .config import Config
from .service import Monitor
from .storage import Store
from .web import create_app
from .telemetry import Telemetry

LOGGER = logging.getLogger(__name__)


async def serve(args):
    data = args.data
    data.mkdir(parents=True, exist_ok=True)
    config = Config.load(args.options)
    store = Store(data / "history.sqlite", config.retention_days, config.max_samples)
    store.start(time.time())
    with store.db:
        store.prune(time.time())
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(config, store, os.environ.get("SUPERVISOR_TOKEN", ""), session,
                        sound_directory=data / "sounds")
        try:
            create_sounds(alerts.sound_directory)
        except OSError as exc:
            alerts.local_ready = False
            alerts.error(time.time(), f"Cannot prepare host-audio sound files: {exc}")
        try:
            create_sounds(args.media / "home-network-monitor")
        except OSError as exc:
            alerts.media_ready = False
            alerts.error(time.time(), f"Cannot prepare local sound files: {exc}")
        monitor = Monitor(config, store, alerts)
        telemetry = Telemetry(config, store, os.environ.get("SUPERVISOR_TOKEN", ""))
        app = create_app(monitor, development=args.development, telemetry=telemetry)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1" if args.development else "0.0.0.0", args.port)
        await site.start()
        tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(alerts.run()),
                 asyncio.create_task(telemetry.run())]
        tasks[0].add_done_callback(monitor.completed)
        tasks[1].add_done_callback(alerts.completed)
        tasks[2].add_done_callback(telemetry.completed)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                # Windows local development; Linux app uses the loop handlers.
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        LOGGER.info("Linkwatch started on port %s (development=%s)", args.port, args.development)
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.log(time.time(), "stop", "Monitoring stopped")
            await runner.cleanup()
            store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--options", type=Path, default=Path("/data/options.json"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--media", type=Path, default=Path("/media"))
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--development", action="store_true",
                        help="Allow loopback only, for local development; never use in HA")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()

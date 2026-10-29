import asyncio
import logging
import math
import struct
import time
import wave
from pathlib import Path

import aiohttp

from .config import Config
from .storage import Store

LOGGER = logging.getLogger(__name__)


def create_sounds(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for kind, notes in {"down": (660, 440, 220), "recovery": (440, 660, 880)}.items():
        path = directory / f"{kind}.wav"
        # Generated locally; no downloads, speech service or copyrighted audio.
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(22050)
            frames = bytearray()
            for frequency in notes:
                for index in range(6615):
                    envelope = min(index / 220, (6614 - index) / 440, 1)
                    value = int(9000 * envelope * math.sin(2 * math.pi * frequency * index / 22050))
                    frames.extend(struct.pack("<h", value))
                frames.extend(b"\0\0" * 1102)
            audio.writeframes(frames)


class Alerts:
    def __init__(self, config: Config, store: Store, token: str,
                 session: aiohttp.ClientSession, sound_directory: Path = Path("/data/sounds")):
        self.config, self.store, self.token, self.session = config, store, token, session
        self.sound_directory = sound_directory
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=32)
        self.last_error: str | None = None
        self.media_ready = True
        self.local_ready = True

    @property
    def muted(self):
        return self.store.setting("muted", "false") == "true"

    def enqueue(self, kind: str, now: float):
        if not self.config.alerts_enabled or self.muted:
            self.store.log(now, "alert_skipped", f"{kind}: alerts disabled or muted")
            return
        try:
            self.queue.put_nowait((kind, now))
        except asyncio.QueueFull:
            self.error(now, "Alert queue full; request dropped")

    def error(self, now: float, message: str):
        self.last_error = message
        LOGGER.error("%s", message)
        self.store.log(now, "alert_error", message)

    async def play_cast(self, kind: str):
        if not self.media_ready:
            raise ValueError("Local sound files unavailable; check the app startup log")
        await self.core_post("media_player/play_media", {
            "entity_id": self.config.media_player,
            "media_content_id": f"media-source://media_source/local/home-network-monitor/{kind}.wav",
            "media_content_type": "audio/wav",
        })

    async def core_post(self, action: str, payload: dict):
        if not self.token:
            raise ValueError("SUPERVISOR_TOKEN is unavailable")
        async with self.session.post(
            f"http://supervisor/core/api/services/{action}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status >= 300:
                # Do not log response content; integrations may include sensitive URLs.
                raise ValueError(f"Home Assistant action API returned HTTP {response.status}")
            response.release()

    async def notify_phone(self, kind: str):
        messages = {
            "down": ("Connection interrupted",
                     f"All external checks failed for at least {self.config.alert_failure_seconds}s. "
                     "Observed from Home Assistant; the cause is unconfirmed."),
            "recovery": ("Connection restored",
                         f"External reachability returned for at least {self.config.alert_recovery_seconds}s."),
            "test": ("Test notification", "Your Linkwatch phone notification action is working."),
        }
        title, message = messages[kind]
        action = self.config.phone_notify_service.replace(".", "/", 1)
        await self.core_post(action, {
            "title": f"Linkwatch · {title}", "message": message,
            "data": {"tag": "linkwatch-connectivity"},
        })

    async def play_local(self, kind: str):
        if not self.local_ready:
            raise ValueError("Local sound files unavailable; check the app startup log")
        try:
            process = await asyncio.create_subprocess_exec(
                "paplay", "--client-name=Linkwatch",
                str(self.sound_directory / f"{kind}.wav"),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                limit=4096,
            )
        except OSError as exc:
            raise ValueError(f"Cannot start local audio player: {exc}") from exc
        try:
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=8)
            except TimeoutError:
                raise ValueError("Local audio playback timed out after 8 seconds") from None
            if process.returncode:
                detail = stderr.decode("utf-8", errors="replace").strip()[:300]
                raise ValueError(f"Local audio output failed ({process.returncode}): {detail}")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def play(self, kind: str):
        outputs = (["local", "cast"] if self.config.sound_output == "both" else
                   [] if self.config.sound_output == "none" else [self.config.sound_output])
        sound_kind = "recovery" if kind == "test" else kind
        work = [self.play_local(sound_kind) if output == "local" else self.play_cast(sound_kind)
                for output in outputs]
        if self.config.phone_notify_service:
            outputs.append("phone")
            work.append(self.notify_phone(kind))
        results = await asyncio.gather(*work, return_exceptions=True)
        errors = []
        for output, result in zip(outputs, results):
            if isinstance(result, (aiohttp.ClientError, TimeoutError, ValueError)):
                errors.append(f"{output}: {result}")
            elif isinstance(result, BaseException):
                raise result
            else:
                self.store.log(time.time(), "alert_output",
                               f"{kind}: {output} request accepted; device delivery unconfirmed")
        if errors:
            raise ValueError("; ".join(errors))

    async def run(self):
        while True:
            kind, queued = await self.queue.get()
            try:
                now = time.time()
                if self.muted or now - queued > 30:
                    self.store.log(now, "alert_skipped", f"{kind}: muted or request expired")
                    continue
                await self.play(kind)
                self.last_error = None
                self.store.log(time.time(), "alert_requested",
                               f"{kind}: configured alert requests completed; device delivery unconfirmed")
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                self.error(time.time(), str(exc))
            finally:
                self.queue.task_done()

    def completed(self, task: asyncio.Task):
        if not task.cancelled() and (exc := task.exception()):
            self.error(time.time(), f"Alert worker stopped: {type(exc).__name__}: {exc}")
